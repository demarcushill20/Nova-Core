---
title: "IRB Bridge Risk-Based Sizing + Partial-Exit Reliability Implementation Plan"
type: implementation-plan
plan_id: irb-bridge-risk-sizing
status: backlog
priority: high
progress: "0/7"
confidence: high
updated: 2026-06-01
date: 2026-06-01
date_created: 2026-06-01
source: nova-core-memory
tags:
  - "#type/plan"
  - "#status/backlog"
  - "#project/nova-core"
---

# IRB Bridge Risk-Based Sizing + Partial-Exit Reliability Implementation Plan

> **For agentic workers:** use the `implementation-team` skill to execute this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Note:** canonical store is the Obsidian vault (`10-plans/`) per NovaCore governance, but the vault content-safety scanner false-positives on the `SECRET =` / `TOKEN =` env-var lines and the webhook-JSON examples in this plan. There are no real secrets here. This repo copy is the authoritative artifact; sync to the vault manually if the scanner is later relaxed.

**Goal:** Make the IRB demo bridge risk a configurable % of live account equity per trade (default 1%), and make its partial-exit / runner handling robust against same-bar, out-of-order, and duplicate webhook alerts.

**Architecture:** The bridge (`services/irb-bridge/irb_bridge.py`) currently maps every entry to a fixed `BASE_LOT=0.10` and tracks position state with a fragile sign-flip heuristic (`map_to_lot`). We replace this with: (1) a pure `risk_based_lot(entry, stop, equity, cfg)` that sizes from real equity + stop distance; (2) a pure `compute_desired_lot(state, ...)` state machine keyed off the alert `comment` (entry vs exit) with a monotonic exit-fraction guard so out-of-order/duplicate exit alerts can never re-open a position; (3) a serialized `reconcile()` coroutine (single `asyncio.Lock`) that always reads fresh broker positions; and (4) a Pine change adding `entry`+`stop` prices to entry alerts so the bridge can compute risk. Pure functions are extracted so they unit-test without MetaApi.

**Tech Stack:** Python 3 (Flask + metaapi_cloud_sdk + asyncio), pytest, Pine Script v5 (TradingView).

---

## Context the implementer needs

- **Symptom 1 (sizing):** Every entry trades exactly 0.10 lots (~$1/pip on EURUSD). With IRB's tight ~2.8-pip stops that is ~$1–3 risk, not the intended 1% of equity. Root cause: `map_to_lot` (`irb_bridge.py:159-185`) returns `cur_sign * BASE_LOT * (abs(position_size)/ref)`, and `ref` is reset to `abs(position_size)` on every entry, so the ratio is always 1.0 -> fixed 0.10 lots. Account equity and stop distance are never consulted.
- **Symptom 2 (partials):** Partial exits fire but are unreliable. Observed in `irb_bridge.log` on 2026-06-01: a same-bar TP1+runner pair raced on stale `current` reads; a 0.05-lot residual bled into the next entry; and an out-of-order delivery (runner before TP1) reset `ref` so the TP1 alert was mis-mapped to a full +0.10 phantom re-entry.
- **Webhook contract today** (`f_alert_msg`, Pine line 253-254): `{"secret","position_size","comment","action"}`. `comment` is one of `entry_long`/`entry_short`/`exit_long_tp1`/`exit_long_runner`/`exit_short_tp1`/`exit_short_runner`/`time_stop_long`/`time_stop_short`.
- **Pine sizing params:** `riskPct` default 1.0 (`:49`), `partialExitPct=50` at `partialRR=1.0` (`:105-106`), runner->BE at 1R, ATR x2 trail.
- **EURUSD sizing math:** account in USD, contract size 100,000 units/lot. Loss if stopped = `lot * 100000 * |entry - stop|` (in USD). So for risk cash `R`: `lot = R / (100000 * |entry - stop|)`.
- **Do NOT** queue a `TASKS/<plan_id>.md` entry for this plan — per memory `project_tasks_autonomous_claim`, that auto-claims and bypasses implementation-team's validate/review/verify. Execute via `implementation-team` directly.
- **Live-service caveat:** this is the demo IRB bridge. The Pine alert JSON change requires the operator to re-paste the alert message in TradingView, and the bridge restart is operator-gated (`sudo systemctl restart irb-bridge`).

## File Structure

- `services/irb-bridge/irb_bridge.py` — **Modify.** Add `SizingCfg`, `risk_based_lot`, `compute_desired_lot`, `_round_step`, module-level `reconcile()` coroutine; add `Broker.get_equity`; serialize reconcile with a lock; rewrite webhook handler; guard side-effectful module globals behind `__main__` so pure functions import cleanly.
- `services/irb-bridge/tests/test_irb_bridge.py` — **Create.** Unit tests for sizing math, the state machine (entry/partial/monotonic/out-of-order/reset), and reconcile serialization via a `FakeConn`.
- `services/irb-bridge/tests/__init__.py` — **Create.** Empty, makes the test dir importable.
- `configs/pinescript/irb_v5_baseline_webhook.pine` — **Modify.** Add `f_alert_entry()` builder emitting `entry`+`stop`; switch the two entry call sites to it.
- `services/irb-bridge/README.md` — **Modify.** Document the new env vars and webhook contract.

---

### Task 1: Pure risk-based lot sizing

**Files:**
- Modify: `services/irb-bridge/irb_bridge.py`
- Create: `services/irb-bridge/tests/__init__.py` (empty)
- Test: `services/irb-bridge/tests/test_irb_bridge.py`

- [ ] **Step 1: Create the empty test package init**

```python
# services/irb-bridge/tests/__init__.py
```

- [ ] **Step 2: Write the failing test**

```python
# services/irb-bridge/tests/test_irb_bridge.py
import os
import pytest

# Pure functions must import without MetaApi/env side effects.
from irb_bridge import SizingCfg, risk_based_lot, _round_step

CFG = SizingCfg(risk_pct=1.0, contract_size=100_000, min_lot=0.01,
                max_lot=50.0, lot_step=0.01, base_lot=0.10)


def test_round_step():
    assert _round_step(35.714, 0.01) == 35.71
    assert _round_step(0.004, 0.01) == 0.0


def test_risk_based_lot_one_percent_eurusd():
    # equity 100k, 1% = $1000 risk; stop 28 points (2.8 pips) = 0.00028
    lot = risk_based_lot(entry=1.08500, stop=1.08472, equity=100_000, cfg=CFG)
    # raw = 1000 / (100000 * 0.00028) = 35.714 -> 35.71
    assert lot == 35.71


def test_risk_based_lot_clamped_to_max():
    cfg = SizingCfg(1.0, 100_000, 0.01, 5.0, 0.01, 0.10)
    assert risk_based_lot(1.08500, 1.08472, 100_000, cfg) == 5.0


def test_risk_based_lot_floors_to_min():
    # huge stop -> tiny raw lot -> min_lot floor
    assert risk_based_lot(1.08500, 1.00000, 100_000, CFG) == 0.01


def test_risk_based_lot_fallback_on_bad_inputs():
    assert risk_based_lot(None, 1.0, 100_000, CFG) == 0.10        # missing entry
    assert risk_based_lot(1.085, 1.085, 100_000, CFG) == 0.10     # entry == stop
    assert risk_based_lot(1.085, 1.084, None, CFG) == 0.10        # equity unavailable
    assert risk_based_lot(1.085, 1.084, 0.0, CFG) == 0.10         # zero equity
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `cd services/irb-bridge && python -m pytest tests/test_irb_bridge.py -v`
Expected: FAIL — `ImportError: cannot import name 'SizingCfg'` (and the module currently raises `KeyError` on `os.environ["IRB_WEBHOOK_SECRET"]` at import — Task 5 fixes the import-time env reads; for now add the pure code at the TOP of the module above the env reads so the import of these names succeeds, see Step 4).

- [ ] **Step 4: Add the sizing config + pure functions near the top of `irb_bridge.py`**

Insert immediately after the existing imports (before any `os.environ[...]` required-key reads):

```python
from dataclasses import dataclass


@dataclass
class SizingCfg:
    risk_pct: float
    contract_size: float
    min_lot: float
    max_lot: float
    lot_step: float
    base_lot: float


def _round_step(x: float, step: float) -> float:
    """Round x to the nearest broker lot step."""
    return round(round(x / step) * step, 8)


def risk_based_lot(entry, stop, equity, cfg: SizingCfg) -> float:
    """Lot that risks cfg.risk_pct of `equity` if price travels entry->stop.

    Falls back to cfg.base_lot when entry/stop/equity are missing or invalid,
    so a bad alert or a failed equity read never blocks a trade — it just
    sizes conservatively at the fixed base lot."""
    try:
        entry = float(entry)
        stop = float(stop)
        equity = float(equity)
    except (TypeError, ValueError):
        return cfg.base_lot
    stop_dist = abs(entry - stop)
    if stop_dist <= 0 or equity <= 0:
        return cfg.base_lot
    risk_cash = equity * (cfg.risk_pct / 100.0)
    raw = risk_cash / (cfg.contract_size * stop_dist)
    lot = _round_step(raw, cfg.lot_step)
    return round(max(cfg.min_lot, min(lot, cfg.max_lot)), 2)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd services/irb-bridge && python -m pytest tests/test_irb_bridge.py -v`
Expected: 5 passed (the `_round_step`, one-percent, clamp, floor, and fallback tests).

> Note: if the module still raises `KeyError` on import because of the top-level `os.environ["IRB_WEBHOOK_SECRET"]` read, temporarily set dummy env in the test shell (`IRB_WEBHOOK_SECRET=x METAAPI_TOKEN=x METAAPI_ACCOUNT_ID=x`). Task 5 removes the import-time requirement permanently.

- [ ] **Step 6: Commit**

```bash
git add services/irb-bridge/irb_bridge.py services/irb-bridge/tests/__init__.py services/irb-bridge/tests/test_irb_bridge.py
git commit -m "feat(irb-bridge): pure risk-based lot sizing from equity + stop distance"
```

---

### Task 2: Comment-keyed state machine with monotonic exit guard

**Files:**
- Modify: `services/irb-bridge/irb_bridge.py`
- Test: `services/irb-bridge/tests/test_irb_bridge.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_irb_bridge.py`:

```python
from irb_bridge import compute_desired_lot

EMPTY = {"side": 0, "entry_units": 0.0, "full_lot": 0.0, "last_fraction": 0.0}


def test_entry_sets_full_lot():
    lot, st = compute_desired_lot(
        EMPTY, position_size=10_000_000, comment="entry_long",
        entry=1.08500, stop=1.08472, equity=100_000, cfg=CFG)
    assert lot == 35.71
    assert st["side"] == 1
    assert st["entry_units"] == 10_000_000
    assert st["full_lot"] == 35.71
    assert st["last_fraction"] == 1.0


def test_partial_scales_to_half():
    st = {"side": 1, "entry_units": 10_000_000, "full_lot": 35.71, "last_fraction": 1.0}
    lot, st2 = compute_desired_lot(
        st, position_size=5_000_000, comment="exit_long_tp1",
        entry=None, stop=None, equity=100_000, cfg=CFG)
    assert lot == _round_step(35.71 * 0.5, 0.01)   # 17.86
    assert st2["last_fraction"] == 0.5


def test_runner_to_flat():
    st = {"side": 1, "entry_units": 10_000_000, "full_lot": 35.71, "last_fraction": 0.5}
    lot, st2 = compute_desired_lot(
        st, position_size=0, comment="exit_long_runner",
        entry=None, stop=None, equity=100_000, cfg=CFG)
    assert lot == 0.0
    assert st2["side"] == 0
    assert st2["last_fraction"] == 0.0


def test_runner_before_tp1_cannot_reopen():
    # Out-of-order: runner (pos=0) arrives first, then the stale TP1 (pos=5M).
    st = {"side": 1, "entry_units": 10_000_000, "full_lot": 35.71, "last_fraction": 1.0}
    lot1, st = compute_desired_lot(st, 0, "exit_long_runner", None, None, 100_000, CFG)
    assert lot1 == 0.0
    lot2, st = compute_desired_lot(st, 5_000_000, "exit_long_tp1", None, None, 100_000, CFG)
    assert lot2 == 0.0   # monotonic guard: cannot grow back to 0.5


def test_duplicate_tp1_is_idempotent():
    st = {"side": 1, "entry_units": 10_000_000, "full_lot": 35.71, "last_fraction": 1.0}
    lot1, st = compute_desired_lot(st, 5_000_000, "exit_long_tp1", None, None, 100_000, CFG)
    lot2, st = compute_desired_lot(st, 5_000_000, "exit_long_tp1", None, None, 100_000, CFG)
    assert lot1 == lot2 == _round_step(35.71 * 0.5, 0.01)


def test_new_entry_resets_state():
    st = {"side": 1, "entry_units": 10_000_000, "full_lot": 35.71, "last_fraction": 0.5}
    lot, st2 = compute_desired_lot(
        st, -10_000_000, "entry_short", 1.08500, 1.08528, 100_000, CFG)
    assert lot < 0
    assert st2["side"] == -1
    assert st2["last_fraction"] == 1.0


def test_exit_with_no_open_trade_is_flat():
    lot, st = compute_desired_lot(EMPTY, 5_000_000, "exit_long_tp1", None, None, 100_000, CFG)
    assert lot == 0.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd services/irb-bridge && python -m pytest tests/test_irb_bridge.py -k "entry or partial or runner or duplicate or flat" -v`
Expected: FAIL — `ImportError: cannot import name 'compute_desired_lot'`.

- [ ] **Step 3: Implement `compute_desired_lot` in `irb_bridge.py`**

Add directly below `risk_based_lot`:

```python
def compute_desired_lot(state: dict, position_size: float, comment: str,
                        entry, stop, equity, cfg: SizingCfg):
    """Return (signed_desired_lot, new_state).

    Entry alerts (comment startswith 'entry') are the ONLY events that set the
    reference position and the risk-based full lot. Exit alerts merely scale the
    stored full lot by the surviving fraction, and that fraction is monotonically
    non-increasing within a trade — so an out-of-order or duplicate exit alert can
    never re-open or grow a position."""
    comment = comment or ""
    cur_sign = 1 if position_size > 0 else -1 if position_size < 0 else 0

    if comment.startswith("entry") and cur_sign != 0:
        full_lot = risk_based_lot(entry, stop, equity, cfg)
        new_state = {
            "side": cur_sign,
            "entry_units": abs(position_size),
            "full_lot": full_lot,
            "last_fraction": 1.0,
        }
        return round(cur_sign * full_lot, 2), new_state

    # Exit / partial / time-stop alert.
    side = state.get("side", 0)
    entry_units = state.get("entry_units", 0.0) or 0.0
    full_lot = state.get("full_lot", 0.0) or 0.0
    if side == 0 or entry_units <= 0 or full_lot <= 0:
        return 0.0, {"side": 0, "entry_units": 0.0, "full_lot": 0.0, "last_fraction": 0.0}

    frac = 0.0 if position_size == 0 else abs(position_size) / entry_units
    frac = min(frac, state.get("last_fraction", 1.0))   # monotonic non-increasing
    frac = max(0.0, min(frac, 1.0))

    new_state = dict(state)
    new_state["last_fraction"] = frac
    if frac <= 0.0:
        new_state["side"] = 0
    desired = round(side * _round_step(full_lot * frac, cfg.lot_step), 2)
    return desired, new_state
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd services/irb-bridge && python -m pytest tests/test_irb_bridge.py -v`
Expected: all tests pass (Task 1 + Task 2, ~12 tests).

- [ ] **Step 5: Commit**

```bash
git add services/irb-bridge/irb_bridge.py services/irb-bridge/tests/test_irb_bridge.py
git commit -m "feat(irb-bridge): comment-keyed state machine with monotonic exit guard"
```

---

### Task 3: Broker equity read with DRY/failure fallback

**Files:**
- Modify: `services/irb-bridge/irb_bridge.py:71-100` (the `Broker` class)

- [ ] **Step 1: Add `get_equity` to the `Broker` class**

Insert this method into `Broker` (after `submit_target`, around line 103):

```python
    def get_equity(self):
        """Live account equity in USD, or None if unavailable (caller falls
        back to base lot). In DRY_RUN, returns IRB_DRY_EQUITY (default 100k)."""
        if DRY_RUN:
            return float(os.environ.get("IRB_DRY_EQUITY", "100000"))
        if self._connection is None:
            log.error("get_equity: MetaApi not connected")
            return None
        fut = asyncio.run_coroutine_threadsafe(self._get_equity(), self.loop)
        try:
            return fut.result(timeout=10)
        except Exception:
            log.exception("get_equity failed")
            return None

    async def _get_equity(self):
        info = await self._connection.get_account_information()
        return float(info["equity"])
```

- [ ] **Step 2: Verify it imports / smoke in DRY_RUN**

Run:
```bash
cd services/irb-bridge
IRB_WEBHOOK_SECRET=x METAAPI_TOKEN=x METAAPI_ACCOUNT_ID=x IRB_DRY_RUN=true \
  python -c "import irb_bridge; b=irb_bridge.Broker(); import time; time.sleep(0.2); print('equity', b.get_equity())"
```
Expected: prints `equity 100000.0` (DRY_RUN path; no MetaApi connection attempted).

> If this errors on the import-time `os.environ["IRB_WEBHOOK_SECRET"]` read, that is removed in Task 5; the dummy env above satisfies it in the meantime.

- [ ] **Step 3: Commit**

```bash
git add services/irb-bridge/irb_bridge.py
git commit -m "feat(irb-bridge): read live account equity from MetaApi with DRY/failure fallback"
```

---

### Task 4: Serialize reconcile + fresh-state reads

**Files:**
- Modify: `services/irb-bridge/irb_bridge.py:102-153` (extract `_reconcile` into a module-level `reconcile()` coroutine guarded by a lock)
- Test: `services/irb-bridge/tests/test_irb_bridge.py`

- [ ] **Step 1: Write the failing serialization test**

Append to `tests/test_irb_bridge.py`:

```python
import asyncio
from irb_bridge import reconcile


class FakeConn:
    """Minimal MetaApi RPC stand-in with realistic async settle delay."""
    def __init__(self):
        self.positions = []
        self._id = 0

    async def get_positions(self):
        await asyncio.sleep(0)
        return [dict(p) for p in self.positions]

    async def create_market_buy_order(self, sym, vol):
        await asyncio.sleep(0.01)            # settle delay where races happen
        self._id += 1
        self.positions.append({"id": str(self._id), "symbol": sym,
                               "type": "POSITION_TYPE_BUY", "volume": vol})

    async def create_market_sell_order(self, sym, vol):
        await asyncio.sleep(0.01)
        self._id += 1
        self.positions.append({"id": str(self._id), "symbol": sym,
                               "type": "POSITION_TYPE_SELL", "volume": vol})

    async def close_position(self, pid):
        await asyncio.sleep(0.01)
        self.positions = [p for p in self.positions if p["id"] != pid]

    async def close_position_partially(self, pid, vol):
        await asyncio.sleep(0.01)
        for p in self.positions:
            if p["id"] == pid:
                p["volume"] = round(p["volume"] - vol, 2)


def _net(conn):
    return sum(p["volume"] if p["type"] == "POSITION_TYPE_BUY" else -p["volume"]
               for p in conn.positions)


def test_concurrent_reconciles_serialize():
    async def run():
        conn = FakeConn()
        lock = asyncio.Lock()
        # entry to 0.10 and partial to 0.05 fire almost simultaneously
        await asyncio.gather(
            reconcile(conn, 0.10, "EURUSD", 0.005, lock),
            reconcile(conn, 0.05, "EURUSD", 0.005, lock),
        )
        return conn
    conn = asyncio.run(run())
    assert _net(conn) == pytest.approx(0.05, abs=0.005)


def test_sign_flip_closes_then_opens():
    async def run():
        conn = FakeConn()
        lock = asyncio.Lock()
        await reconcile(conn, 0.10, "EURUSD", 0.005, lock)
        await reconcile(conn, -0.10, "EURUSD", 0.005, lock)
        return conn
    conn = asyncio.run(run())
    assert _net(conn) == pytest.approx(-0.10, abs=0.005)
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd services/irb-bridge && python -m pytest tests/test_irb_bridge.py -k "reconcile or sign_flip" -v`
Expected: FAIL — `ImportError: cannot import name 'reconcile'`.

- [ ] **Step 3: Extract a module-level `reconcile()` coroutine and add a lock**

Add this module-level coroutine (place it above the `Broker` class):

```python
async def reconcile(conn, desired: float, symbol: str, epsilon: float, lock):
    """Drive the broker net position for `symbol` to `desired` lots.

    Serialized by `lock` so concurrent alerts never act on stale state: each
    invocation re-reads positions only after the previous one has fully
    completed (orders acked)."""
    async with lock:
        positions = await conn.get_positions()
        sym = [p for p in positions if p["symbol"] == symbol]
        current = sum((p["volume"] if p["type"] == "POSITION_TYPE_BUY" else -p["volume"])
                      for p in sym)
        log.info("reconcile %s: current %+.2f -> desired %+.2f", symbol, current, desired)

        same_sign = (current >= 0) == (desired >= 0)
        if not same_sign or abs(desired) < epsilon:
            for p in sym:
                await conn.close_position(p["id"])
            if abs(desired) < epsilon:
                return
            current = 0.0
            sym = []

        delta = desired - current
        if abs(delta) < epsilon:
            log.info("already in line — no order")
            return
        if delta > 0 and desired > 0:
            await conn.create_market_buy_order(symbol, round(delta, 2))
        elif delta < 0 and desired < 0:
            await conn.create_market_sell_order(symbol, round(-delta, 2))
        else:
            await _reduce(conn, sym, abs(delta), epsilon)


async def _reduce(conn, sym_positions, volume_to_close: float, epsilon: float) -> None:
    remaining = volume_to_close
    for p in sym_positions:
        if remaining < epsilon:
            break
        chunk = min(p["volume"], remaining)
        if chunk >= p["volume"] - epsilon:
            await conn.close_position(p["id"])
        else:
            await conn.close_position_partially(p["id"], round(chunk, 2))
        remaining -= chunk
```

Then change the `Broker` to own a lock and delegate. In `Broker.__init__`, add `self._lock = None` (the lock must be created on the broker's own loop). In `_run_loop`, after `asyncio.set_event_loop(self.loop)`, create it: `self._lock = asyncio.Lock()`. Replace the old `_reconcile` / `_reduce` methods with a thin delegate:

```python
    def submit_target(self, desired_lot: float) -> None:
        asyncio.run_coroutine_threadsafe(self._reconcile(desired_lot), self.loop)

    async def _reconcile(self, desired: float) -> None:
        try:
            if DRY_RUN:
                log.info("[DRY_RUN] would reconcile %s to %+.2f lots", SYMBOL, desired)
                return
            if self._connection is None:
                log.error("MetaApi not connected — dropping reconcile %s", desired)
                return
            await reconcile(self._connection, desired, SYMBOL, LOT_EPSILON, self._lock)
        except Exception:
            log.exception("reconcile failed for desired=%s", desired)
```

Delete the old inline `_reconcile` body and the old `_reduce` method (now module-level).

- [ ] **Step 4: Run to verify it passes**

Run: `cd services/irb-bridge && python -m pytest tests/test_irb_bridge.py -v`
Expected: all tests pass, including `test_concurrent_reconciles_serialize` (net 0.05) and `test_sign_flip_closes_then_opens` (net -0.10).

- [ ] **Step 5: Commit**

```bash
git add services/irb-bridge/irb_bridge.py services/irb-bridge/tests/test_irb_bridge.py
git commit -m "fix(irb-bridge): serialize reconcile under a lock, read fresh broker state each pass"
```

---

### Task 5: Wire the webhook handler + make module import-safe

**Files:**
- Modify: `services/irb-bridge/irb_bridge.py` (config block `:44-52`, `map_to_lot` removal `:159-185`, `webhook` handler `:203-234`, `__main__` block `:237+`)

- [ ] **Step 1: Make config import-safe and add sizing env vars**

Replace the top-level required-key reads (the `os.environ[...]` lookups for the webhook secret, MetaApi token, and account id) with `.get()` defaults so the module imports under pytest without those values present:

```python
SECRET = os.environ.get("IRB_WEBHOOK_SECRET", "")
METAAPI_TOKEN = os.environ.get("METAAPI_TOKEN", "")
METAAPI_ACCOUNT_ID = os.environ.get("METAAPI_ACCOUNT_ID", "")
```

Add, alongside the existing `BASE_LOT` line:

```python
RISK_PCT = float(os.environ.get("IRB_RISK_PCT", "1.0"))
CONTRACT_SIZE = float(os.environ.get("IRB_CONTRACT_SIZE", "100000"))
MIN_LOT = float(os.environ.get("IRB_MIN_LOT", "0.01"))
MAX_LOT = float(os.environ.get("IRB_MAX_LOT", "50.0"))
LOT_STEP = float(os.environ.get("IRB_LOT_STEP", "0.01"))

SIZING = SizingCfg(risk_pct=RISK_PCT, contract_size=CONTRACT_SIZE,
                   min_lot=MIN_LOT, max_lot=MAX_LOT, lot_step=LOT_STEP, base_lot=BASE_LOT)
```

Move `broker = Broker()` out of module scope: delete the bare `broker = Broker()` line and instead lazily create it. Add near the top: `broker = None`. In the `__main__` block (and only there), set `broker = Broker()` before `app.run(...)`. The webhook handler guards `if broker is None`.

> Rationale: instantiating `Broker()` at import spawns a thread + (in live mode) a MetaApi connection — unacceptable under pytest. Guarding it keeps the pure functions and Flask app importable.

- [ ] **Step 2: Delete `map_to_lot` and rewrite the webhook handler**

Remove the entire `map_to_lot` function (`:159-185`). Replace the body of `webhook()` after the secret check + `position_size` parse with:

```python
    comment = str(payload.get("comment", ""))

    def _opt_float(key):
        try:
            return float(payload[key])
        except (KeyError, TypeError, ValueError):
            return None

    entry = _opt_float("entry")
    stop = _opt_float("stop")

    equity = broker.get_equity() if broker is not None else None
    state = load_state()
    desired, new_state = compute_desired_lot(
        state, position_size, comment, entry, stop, equity, SIZING)
    save_state(new_state)

    log.info(
        "webhook ok: pos_size=%s comment=%r action=%r entry=%s stop=%s equity=%s -> target %+.2f lots",
        position_size, comment, payload.get("action"), entry, stop, equity, desired,
    )

    if broker is not None:
        broker.submit_target(desired)
    return jsonify(status="accepted", target_lot=round(desired, 2)), 200
```

Update `load_state()`'s default to the new schema:

```python
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("bridge_state.json corrupt — resetting")
    return {"side": 0, "entry_units": 0.0, "full_lot": 0.0, "last_fraction": 0.0}
```

Update the `/irb/health` JSON to surface the new config: add `risk_pct=RISK_PCT, max_lot=MAX_LOT` to the `jsonify(...)`.

- [ ] **Step 3: Add a focused webhook integration test (DRY_RUN, no broker)**

Append to `tests/test_irb_bridge.py`:

```python
def test_webhook_end_to_end_sizes_and_scales(tmp_path, monkeypatch):
    import json as _json
    import irb_bridge as ib
    monkeypatch.setattr(ib, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(ib, "SECRET", "sek")
    monkeypatch.setattr(ib, "broker", None)          # no real broker; equity -> None -> base_lot
    monkeypatch.setattr(ib, "SIZING",
                        ib.SizingCfg(1.0, 100_000, 0.01, 50.0, 0.01, 0.10))
    client = ib.app.test_client()

    def post(body):
        return client.post("/irb/webhook", data=_json.dumps(body),
                           content_type="application/json")

    # entry with no broker -> equity None -> base_lot 0.10 full
    r = post({"secret": "sek", "position_size": "10000000",
              "comment": "entry_long", "action": "buy",
              "entry": "1.08500", "stop": "1.08472"})
    assert r.status_code == 200
    assert r.get_json()["target_lot"] == 0.10

    # tp1 -> half of base_lot
    r = post({"secret": "sek", "position_size": "5000000",
              "comment": "exit_long_tp1", "action": "sell"})
    assert r.get_json()["target_lot"] == 0.05

    # out-of-order: runner already flat, late tp1 cannot reopen
    post({"secret": "sek", "position_size": "0", "comment": "exit_long_runner", "action": "sell"})
    r = post({"secret": "sek", "position_size": "5000000", "comment": "exit_long_tp1", "action": "sell"})
    assert r.get_json()["target_lot"] == 0.0

    # bad secret rejected
    assert post({"secret": "nope", "position_size": "0", "comment": "x"}).status_code == 401
```

- [ ] **Step 4: Run the full suite**

Run: `cd services/irb-bridge && python -m pytest tests/test_irb_bridge.py -v`
Expected: all tests pass with NO env vars set (module now imports cleanly).

- [ ] **Step 5: Commit**

```bash
git add services/irb-bridge/irb_bridge.py services/irb-bridge/tests/test_irb_bridge.py
git commit -m "feat(irb-bridge): risk-based webhook handler, import-safe module, drop map_to_lot"
```

---

### Task 6: Pine — emit entry + stop on entry alerts

**Files:**
- Modify: `configs/pinescript/irb_v5_baseline_webhook.pine:253-254` (add builder), `:257` and `:267` (entry call sites)

- [ ] **Step 1: Add an entry-specific alert builder**

After the existing `f_alert_msg(_comment) =>` function (line 253-254), add:

```pine
// Entry alerts additionally carry the fill price and initial stop so the
// bridge can size the position to a fixed % of account equity.
f_alert_entry(_comment, _entry, _stop) =>
    '{"secret":"' + webhookSecret + '","position_size":"{{strategy.position_size}}","comment":"' + _comment + '","action":"{{strategy.order.action}}","entry":"' + str.tostring(_entry, format.mintick) + '","stop":"' + str.tostring(_stop, format.mintick) + '"}'
```

- [ ] **Step 2: Switch the two entry call sites to the new builder**

Line 257 (long entry) — change `alert_message=f_alert_msg("entry_long")` to:

```pine
    strategy.entry("Long", strategy.long, qty=pendingLongQty, stop=pendingLongEntry, alert_message=f_alert_entry("entry_long", pendingLongEntry, pendingLongStop))
```

Line 267 (short entry) — change `alert_message=f_alert_msg("entry_short")` to:

```pine
    strategy.entry("Short", strategy.short, qty=pendingShortQty, stop=pendingShortEntry, alert_message=f_alert_entry("entry_short", pendingShortEntry, pendingShortStop))
```

Leave all exit / time-stop call sites on `f_alert_msg` (they don't need entry/stop).

- [ ] **Step 3: Verify the Pine compiles**

Manual: paste into TradingView Pine editor on a EURUSD chart and confirm "Saved / compiled successfully" with no errors. Use the `pinescript-reference` skill to confirm `str.tostring(value, format.mintick)` signature if uncertain. Trigger one entry on the bar replay and confirm the emitted alert JSON contains `"entry"` and `"stop"` keys with sane prices.

- [ ] **Step 4: Commit**

```bash
git add configs/pinescript/irb_v5_baseline_webhook.pine
git commit -m "feat(pine): emit entry+stop prices on IRB entry alerts for risk-based bridge sizing"
```

---

### Task 7: Docs, deploy, and live verification

**Files:**
- Modify: `services/irb-bridge/README.md`

- [ ] **Step 1: Document the new env vars + contract in README**

Add a "Risk-based sizing" section listing: `IRB_RISK_PCT` (default 1.0), `IRB_MIN_LOT` (0.01), `IRB_MAX_LOT` (50.0), `IRB_LOT_STEP` (0.01), `IRB_CONTRACT_SIZE` (100000), `IRB_DRY_EQUITY` (100000, DRY only). Document the entry-alert JSON now includes `entry` and `stop`. Note that the bridge sizes `lot = equity * IRB_RISK_PCT/100 / (IRB_CONTRACT_SIZE * |entry-stop|)`, clamped to `[IRB_MIN_LOT, IRB_MAX_LOT]`, and falls back to `IRB_BASE_LOT` if equity/entry/stop are unavailable.

- [ ] **Step 2: Decide and document the MAX_LOT cap**

With a ~$100k demo and ~2.8-pip stops, 1% risk ≈ 35 lots. Confirm `IRB_MAX_LOT` is set high enough not to silently throttle 1% risk (default 50.0 is fine for $100k). If the operator wants a hard nominal cap, set it lower and note in README that trades may then risk <1%.

- [ ] **Step 3: Set env in the systemd unit and reload (operator-gated)**

Add `Environment=` lines to `irb-bridge.service` for `IRB_RISK_PCT=1.0` and any non-default clamps, then:

```bash
sudo cp services/irb-bridge/irb-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
```

- [ ] **Step 4: Reset bridge state (stale schema) and restart (operator-gated)**

The old `bridge_state.json` uses the old schema; remove it so the bridge starts clean:

```bash
rm -f services/irb-bridge/bridge_state.json
sudo systemctl restart irb-bridge
curl -s http://127.0.0.1:8081/irb/health | python -m json.tool
```
Expected: health JSON shows `risk_pct: 1.0`, `max_lot: 50.0`, and `dry_run` per current config.

- [ ] **Step 5: Re-deploy the TradingView alert (operator action)**

In TradingView, open the IRB v5 baseline alert, replace the alert message with the new `f_alert_entry`-generated JSON (entry alerts now include `entry`+`stop`). Save. (The alert message is regenerated from the recompiled script; just re-create the alert from the updated strategy.)

- [ ] **Step 6: Live verification on the next trade**

Tail the log through one full trade cycle:

```bash
tail -f services/irb-bridge/irb_bridge.log
```
Confirm:
- Entry line shows non-zero `entry=`/`stop=`/`equity=` and a `target` lot consistent with `equity * 1% / (100000 * |entry-stop|)` (e.g. ~30–40 lots on a $100k demo with a ~2.8-pip stop), NOT 0.10.
- TP1 line reconciles to ~50% of the entry lot.
- Runner line reconciles to 0.00 and `bridge_state.json` shows `"side": 0`.
- No residual position carries into the following entry (`current` is `0.00` at the next entry's reconcile).

- [ ] **Step 7: Commit docs**

```bash
git add services/irb-bridge/README.md services/irb-bridge/irb-bridge.service
git commit -m "docs(irb-bridge): document risk-based sizing env + deploy/verify runbook"
```

---

## Phases

- [ ] **Phase 1: Pure risk-based lot sizing** — `risk_based_lot()` sizes from equity + stop distance with safe fallbacks (TDD).
- [ ] **Phase 2: State machine** — comment-keyed `compute_desired_lot()` with monotonic exit-fraction guard against out-of-order/duplicate alerts (TDD).
- [ ] **Phase 3: Broker equity read** — `Broker.get_equity()` from MetaApi with DRY/failure fallback.
- [ ] **Phase 4: Serialize reconcile** — module-level `reconcile()` under an `asyncio.Lock` reading fresh broker state (TDD with `FakeConn`).
- [ ] **Phase 5: Webhook wiring** — risk-based handler, import-safe module, `map_to_lot` removed, end-to-end Flask test.
- [ ] **Phase 6: Pine contract** — emit `entry`+`stop` on entry alerts via `f_alert_entry`.
- [ ] **Phase 7: Docs + deploy + live verify** — README, env, state reset, restart, TV alert re-deploy, one-trade verification.
