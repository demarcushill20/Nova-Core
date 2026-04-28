# Pine v5 Exit-Timing Parity Audit Implementation Plan

> **For agentic workers:** use the `implementation-team` skill to execute this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the audit defined in `docs/superpowers/specs/2026-04-28-exit-timing-parity-audit-design.md` (commit `29ed00b`) — a tiered, empirical-probe parity audit between Python's `IRBBacktester` exit logic and Pine v5's S_LONG/S_SHORT exit blocks. Output: `docs/parity/exit-timing-audit.md` with measured impact per divergence + tier assignment, vault summary, memory pointer. Audit-only; fixes ship in a separate plan.

**Architecture:** Add `env.parity_audit_toggles: frozenset[str]` to `BacktestEnvironment`. Each toggle string gates one suspect behavior in `_manage_position` / `_close_position` / cooldown enforcement. Empty set ≡ current behavior (live regression test enforced). Toggles are diagnostic-only; production configs reject them via schema validation. A measurement harness (`scripts/parity_audit.py`) drives each probe against `data/irb_novatrade_irb_v5_results_extracted.csv` and emits impact metrics (pine_only/python_only resolved, ΔPF, Δtrades).

**Tech Stack:** Python 3.11+, dataclasses (Environment), pydantic (config_schema), pytest, ruff/mypy via pre-commit, Obsidian vault for plan + summary, Fusion Memory for cross-session pointer.

**Source spec:** `docs/superpowers/specs/2026-04-28-exit-timing-parity-audit-design.md` (commit `29ed00b`)

**Vault pointer:** `10-plans/plan-pine-exit-timing-audit.md` (status: backlog)

**Worktree (recommended):** `.worktrees/pine-exit-timing-audit` off `main`. Use `/worktree` before kickoff.

---

## Phases

- [ ] **Phase 1: Toggle scaffold + live regression test** — add `parity_audit_toggles` field, plumb through schema, add empty-set ≡ current-behavior test
- [ ] **Phase 2: D2 probe — STAG/TIME_STOP next-bar-open exit** — `d2_strategy_close_next_open` toggle (hypothesized Tier-1)
- [ ] **Phase 3: D3 probe — zero cooldown** — `d3_zero_cooldown` toggle (hypothesized Tier-1)
- [ ] **Phase 4: D1 probe — post-ratchet stop fill** — `d1_post_ratchet_stop_fill` toggle (hypothesized Tier-2)
- [ ] **Phase 5: D12 probe — gap-through stop fill price** — `d12_gap_fill_at_open` toggle (hypothesized Tier-2)
- [ ] **Phase 6: D10 code-walk — same-bar TRAILING_STOP edge** — verify equivalence (hypothesized Tier-3)
- [ ] **Phase 7: Tier-3 code-walk verification (D4–D9, D11)** — line-ref documentation entries
- [ ] **Phase 8: Measurement harness — `scripts/parity_audit.py`** — driver script
- [ ] **Phase 9: Run probes, capture impact data** — baseline + each toggle individually + paired top candidates
- [ ] **Phase 10: Compose findings doc + vault summary + memory pointer** — populate `docs/parity/exit-timing-audit.md`, vault note, memory update
- [ ] **Phase 11: Self-review + handoff** — full test suite green, fix-plan-brainstorm queued

---

## Phase 1: Toggle scaffold + live regression test

### Task 1.1: Add `parity_audit_toggles` field to `BacktestEnvironment`

**Files:**
- Modify: `novatrade/backtest/environment.py:106-260` (the `BacktestEnvironment` dataclass)

- [ ] **Step 1: Add the field**

In `novatrade/backtest/environment.py`, locate the `BacktestEnvironment` dataclass (line 106). Add the following block immediately after the `sl_spread_buffer_pips` field (~line 231, before the `# --- Measurement vs inference ---` comment block):

```python
    # --- Parity-audit toggles (diagnostic only; live configs MUST leave empty) ---
    # Each entry is a string label gating one suspect behavior in _manage_position /
    # _close_position / cooldown enforcement, used by the Pine v5 exit-timing audit.
    # An empty set MUST produce bit-identical results to the current engine — enforced
    # by `tests/test_backtest_engine.py::test_parity_audit_toggle_default_is_no_op`.
    # Production configs reject this field via config_schema validation.
    parity_audit_toggles: frozenset[str] = field(default_factory=frozenset)
```

- [ ] **Step 2: Verify imports**

Confirm `field` is already imported from `dataclasses`. If not, add: `from dataclasses import dataclass, field`.

- [ ] **Step 3: Run unit tests**

Run: `pytest tests/test_backtest_engine.py -q`
Expected: all green (field added but unused).

- [ ] **Step 4: Commit**

```bash
git add novatrade/backtest/environment.py
git commit -m "feat(parity): add parity_audit_toggles field to BacktestEnvironment"
```

### Task 1.2: Add field to `IRBStrategyConfig` with live-config rejection

**Files:**
- Modify: `novatrade/cli/config_schema.py`

- [ ] **Step 1: Add the field with validation**

Add to `IRBStrategyConfig` (near `cooldown_bars` at line 97):

```python
    parity_audit_toggles: frozenset[str] = Field(default_factory=frozenset)

    @field_validator("parity_audit_toggles", mode="before")
    @classmethod
    def _validate_parity_toggles(cls, v):
        if v is None:
            return frozenset()
        if isinstance(v, (list, tuple, set, frozenset)):
            return frozenset(str(x) for x in v)
        raise ValueError(
            "parity_audit_toggles must be a list/set of strings (or omitted). "
            "Live configs MUST leave this empty/omitted."
        )
```

Add to the `to_environment_kwargs` method:

```python
            "parity_audit_toggles": self.parity_audit_toggles,
```

- [ ] **Step 2: Add live-config rejection at config-load entry point** (skip if no separate live-loader exists; document the requirement in audit doc instead).

```bash
grep -n "load_strategy_config\|load_config\|load_live" novatrade/cli/config_schema.py
```

If a live-config loader exists:

```python
def load_live_strategy_config(path: str) -> IRBStrategyConfig:
    cfg = _load_strategy_config(path)
    if cfg.parity_audit_toggles:
        raise ValueError(
            f"Config {path} sets parity_audit_toggles={cfg.parity_audit_toggles!r}. "
            "Parity-audit toggles are diagnostic-only and MUST NOT be set in live "
            "configs. Use scripts/parity_audit.py to inject toggles for measurement."
        )
    return cfg
```

- [ ] **Step 3: Run unit tests**

Run: `pytest tests/ -q -k "config"`
Expected: no failures.

- [ ] **Step 4: Commit**

```bash
git add novatrade/cli/config_schema.py
git commit -m "feat(parity): plumb parity_audit_toggles through IRBStrategyConfig"
```

### Task 1.3: Live-regression test — empty toggles ≡ current behavior

**Files:**
- Modify: `tests/test_backtest_engine.py`

- [ ] **Step 1: Identify smoke-test fixtures**

```bash
grep -n "def test_initial_stop_uses_wick_when_trail_ema_disabled\|@pytest.fixture" tests/test_backtest_engine.py | head -20
```

- [ ] **Step 2: Append the regression test class**

```python
class TestParityAuditToggleNoOp:
    """Live-safety regression: empty parity_audit_toggles MUST produce bit-identical
    results. If this test fails, a parity-audit toggle has leaked into the default
    code path and live behavior may have changed."""

    def test_empty_toggles_match_current_behavior(self):
        env_default = build_test_environment()
        assert env_default.parity_audit_toggles == frozenset()

        result = run_smoke_backtest(env_default)

        env_explicit_empty = build_test_environment(
            parity_audit_toggles=frozenset()
        )
        result_explicit = run_smoke_backtest(env_explicit_empty)

        assert result.completed_trades == result_explicit.completed_trades
        assert result.final_equity == result_explicit.final_equity

    def test_unknown_toggle_is_no_op(self):
        env = build_test_environment(
            parity_audit_toggles=frozenset({"this_toggle_does_not_exist"})
        )
        result = run_smoke_backtest(env)
        env_default = build_test_environment()
        result_default = run_smoke_backtest(env_default)
        assert result.completed_trades == result_default.completed_trades
        assert result.final_equity == result_default.final_equity
```

If `build_test_environment` and `run_smoke_backtest` don't exist, extract them from `test_initial_stop_uses_wick_when_trail_ema_disabled` and place at module top.

- [ ] **Step 3: Run regression test**

Run: `pytest tests/test_backtest_engine.py::TestParityAuditToggleNoOp -v`
Expected: PASS.

- [ ] **Step 4: Run full suite**

Run: `pytest tests/test_backtest_engine.py -q`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add tests/test_backtest_engine.py
git commit -m "test(parity): live-regression for parity_audit_toggles no-op default"
```

---

## Phase 2: D2 probe — STAG/TIME_STOP next-bar-open exit

**Hypothesis:** Pine's `strategy.close` (lines 741, 751) fills at next bar's open. Python's `_close_position` (lines 891, 912) fills at current bar's close. Pine stays in-position 1 bar longer → Bucket E. Hypothesized Tier-1.

### Task 2.1: Add deferred-exit field to `Position` dataclass

**Files:**
- Modify: `novatrade/backtest/engine.py` (find via `grep -n "class Position" novatrade/backtest/engine.py`)

- [ ] **Step 1: Add fields**

Append to the `Position` dataclass:

```python
    # D2 probe: deferred exit (when parity_audit_toggles contains "d2_strategy_close_next_open")
    deferred_exit_reason: ExitReason | None = None
    deferred_exit_fire_at_bar: int = -1
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_backtest_engine.py -q`
Expected: all green.

### Task 2.2: Write failing test for D2 probe

**Files:**
- Modify: `tests/test_backtest_engine.py`

- [ ] **Step 1: Add the test**

```python
class TestD2StagTimeStopNextBarOpen:
    """D2 probe: when toggle d2_strategy_close_next_open is set, STAG_EXIT and
    TIME_STOP fire on the NEXT bar's open instead of the current bar's close."""

    def test_d2_defers_stag_exit_to_next_bar_open(self):
        env = build_test_environment(
            use_stagnation_guard=True,
            stag_bars=12,
            stag_atr_mult=0.3,
            parity_audit_toggles=frozenset({"d2_strategy_close_next_open"}),
        )
        result = run_synthetic_stag_window(env)
        assert len(result.completed_trades) == 1
        trade = result.completed_trades[0]
        assert trade.exit_reason == ExitReason.STAG_EXIT
        assert trade.exit_bar_index == 13
        bar_13_open = result.candles[13].open
        assert trade.exit_price == pytest.approx(bar_13_open, abs=1e-9)

    def test_d2_default_off_keeps_current_bar_close(self):
        env = build_test_environment(
            use_stagnation_guard=True,
            stag_bars=12,
            stag_atr_mult=0.3,
        )
        result = run_synthetic_stag_window(env)
        trade = result.completed_trades[0]
        assert trade.exit_bar_index == 12
        bar_12_close = result.candles[12].close
        assert trade.exit_price == pytest.approx(bar_12_close, abs=1e-9)
```

If `run_synthetic_stag_window` doesn't exist, add a 20-bar mildly-adverse-trend helper that triggers STAG on bar 12.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_backtest_engine.py::TestD2StagTimeStopNextBarOpen -v`
Expected: FAIL.

### Task 2.3: Implement `d2_strategy_close_next_open` toggle

**Files:**
- Modify: `novatrade/backtest/engine.py:842-988` (`_manage_position`)

- [ ] **Step 1: Defer STAG exit when toggle is set**

In `_manage_position`, locate STAG check (line 887–892) and replace inner `if`:

```python
            if pos.peak_fav < stag_threshold and adverse:
                if "d2_strategy_close_next_open" in self.env.parity_audit_toggles:
                    pos.deferred_exit_reason = ExitReason.STAG_EXIT
                    pos.deferred_exit_fire_at_bar = i + 1
                    return
                self._close_position(i, bar.close, ExitReason.STAG_EXIT)
                return
```

- [ ] **Step 2: Defer TIME_STOP exit when toggle is set**

Replace TIME_STOP check (~line 911–913):

```python
        if pos.bars_held >= self.env.time_stop_bars:
            if "d2_strategy_close_next_open" in self.env.parity_audit_toggles:
                pos.deferred_exit_reason = ExitReason.TIME_STOP
                pos.deferred_exit_fire_at_bar = i + 1
                return
            self._close_position(i, bar.close, ExitReason.TIME_STOP)
            return
```

- [ ] **Step 3: Fire deferred exit at top of `_manage_position`**

Add immediately after the entry-bar guard (line 860–861):

```python
        # D2 probe: fire deferred STAG/TIME_STOP exit on this bar's open
        if pos.deferred_exit_reason is not None and i >= pos.deferred_exit_fire_at_bar:
            reason = pos.deferred_exit_reason
            pos.deferred_exit_reason = None
            self._close_position(i, bar.open, reason)
            return
```

- [ ] **Step 4: Run D2 + regression + full suite**

```
pytest tests/test_backtest_engine.py::TestD2StagTimeStopNextBarOpen -v
pytest tests/test_backtest_engine.py::TestParityAuditToggleNoOp -v
pytest tests/test_backtest_engine.py -q
```

Expected: all PASS / green.

- [ ] **Step 5: Commit**

```bash
git add novatrade/backtest/engine.py tests/test_backtest_engine.py
git commit -m "feat(parity): D2 probe — STAG/TIME_STOP next-bar-open exit toggle"
```

---

## Phase 3: D3 probe — zero cooldown

**Hypothesis:** Pine sets `state := S_FLAT` on close; next bar's signal can transition immediately. Python `cooldown_bars: 1` blocks bar i+1. Hypothesized Tier-1.

### Task 3.1: Write failing test

```python
class TestD3ZeroCooldown:
    def test_d3_bypasses_cooldown(self):
        env = build_test_environment(
            cooldown_bars=1,
            parity_audit_toggles=frozenset({"d3_zero_cooldown"}),
        )
        result = run_synthetic_re_entry_window(env)
        trade_count = len(result.completed_trades) + (1 if result.in_flight else 0)
        assert trade_count == 2

    def test_d3_default_off_enforces_cooldown(self):
        env = build_test_environment(cooldown_bars=1)
        result = run_synthetic_re_entry_window(env)
        trade_count = len(result.completed_trades) + (1 if result.in_flight else 0)
        assert trade_count == 1
```

If `run_synthetic_re_entry_window` doesn't exist, add a 30-bar two-signals-N-and-N+1 helper.

- [ ] Run test to verify FAIL.

### Task 3.2: Implement `d3_zero_cooldown` toggle

**Files:**
- Modify: `novatrade/backtest/engine.py:451`

Replace cooldown check:

```python
        # --- v5: Cooldown bars ---
        if (
            e.cooldown_bars > 0
            and (i - self._last_flat_bar) <= e.cooldown_bars
            and "d3_zero_cooldown" not in e.parity_audit_toggles
        ):
            self._rejections.circuit_breaker += 1
            return
```

- [ ] Run D3 + regression: `pytest tests/test_backtest_engine.py::TestD3ZeroCooldown tests/test_backtest_engine.py::TestParityAuditToggleNoOp -v`
- [ ] Commit:

```bash
git add novatrade/backtest/engine.py tests/test_backtest_engine.py
git commit -m "feat(parity): D3 probe — zero-cooldown toggle"
```

---

## Phase 4: D1 probe — post-ratchet stop fill

**Hypothesis:** Pine ratchets `cur_stop` *before* `strategy.exit`. Python checks pre-ratchet stop, then ratchets. Hypothesized Tier-2.

### Task 4.1: Write failing test

```python
class TestD1PostRatchetStopFill:
    def test_d1_uses_post_ratchet_stop_for_long(self):
        env = build_test_environment(
            trail_ema_period=40,
            parity_audit_toggles=frozenset({"d1_post_ratchet_stop_fill"}),
        )
        result = run_synthetic_ratchet_collision_long(env)
        trade = result.completed_trades[0]
        assert trade.exit_reason == ExitReason.TRAILING_STOP
        assert trade.exit_price == pytest.approx(1.10500, abs=1e-5)
```

`run_synthetic_ratchet_collision_long` constructs a window where bar i has old_stop=1.10000, EMA=1.10500, wick low=1.09950 (touches old_stop and would touch new ratcheted stop).

- [ ] Run test to verify FAIL or PASS (informative — D1 may already match in some cases).

### Task 4.2: Implement `d1_post_ratchet_stop_fill` toggle

**Files:**
- Modify: `novatrade/backtest/engine.py:_manage_position`

- [ ] **Step 1: Add early-ratchet branch at top of `_manage_position`**

Immediately after the deferred-exit check from Task 2.3 Step 3:

```python
        # D1 probe: ratchet BEFORE the stop-loss check so a same-bar wick-hit
        # fills at post-ratchet stop level (Pine cur_stop semantics).
        if "d1_post_ratchet_stop_fill" in self.env.parity_audit_toggles:
            self._ratchet_trail_only(i, bar, atr_h1, trail_ema)
```

- [ ] **Step 2: Add the ratchet-only helper**

Add `_ratchet_trail_only` method to the engine class:

```python
    def _ratchet_trail_only(
        self,
        i: int,
        bar: Candle,
        atr_h1: list[float],
        trail_ema: list[float] | None,
    ) -> None:
        """Update pos.current_stop via EMA-trail or ATR-trail without firing
        any exit. Used by D1 probe to ratchet BEFORE the stop check."""
        pos = self._position
        if pos is None:
            return

        use_ema_trail = (
            self.env.trail_ema_period > 0
            and trail_ema is not None
            and i < len(trail_ema)
        )

        if use_ema_trail and trail_ema is not None:
            ema_val = trail_ema[i]
            if math.isnan(ema_val):
                return
            if pos.side == TradeSide.LONG:
                if ema_val > pos.current_stop:
                    pos.current_stop = ema_val
            else:
                if ema_val < pos.current_stop:
                    pos.current_stop = ema_val
        else:
            atr_val = atr_h1[i] if i < len(atr_h1) and not math.isnan(atr_h1[i]) else 0
            if atr_val <= 0:
                return
            if pos.side == TradeSide.LONG:
                pos.best_close = max(pos.best_close, bar.close)
                new_trail = pos.best_close - self.env.trail_atr_multiplier * atr_val
                if new_trail > pos.current_stop:
                    pos.current_stop = new_trail
            else:
                pos.best_close = min(pos.best_close, bar.close)
                new_trail = pos.best_close + self.env.trail_atr_multiplier * atr_val
                if new_trail < pos.current_stop:
                    pos.current_stop = new_trail
```

- [ ] **Step 3: Run D1 + regression + full suite**

```
pytest tests/test_backtest_engine.py -q
```

Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add novatrade/backtest/engine.py tests/test_backtest_engine.py
git commit -m "feat(parity): D1 probe — post-ratchet stop fill toggle"
```

---

## Phase 5: D12 probe — gap-through stop fill price

**Hypothesis:** When a bar gaps through the stop level, Pine may fill at gap-open vs Python at stop-level. Hypothesized Tier-2.

### Task 5.1: Write failing test

```python
class TestD12GapFillAtStopLevel:
    def test_d12_long_gap_fills_at_open(self):
        env = build_test_environment(
            parity_audit_toggles=frozenset({"d12_gap_fill_at_open"}),
        )
        result = run_synthetic_gap_through_long(env)
        trade = result.completed_trades[0]
        assert trade.exit_price == pytest.approx(1.09500, abs=1e-5)

    def test_d12_default_off_fills_at_stop_level(self):
        env = build_test_environment()
        result = run_synthetic_gap_through_long(env)
        trade = result.completed_trades[0]
        assert trade.exit_price == pytest.approx(1.10000, abs=1e-5)
```

`run_synthetic_gap_through_long`: entry bar sets stop=1.10000; next bar opens at 1.09500.

### Task 5.2: Implement `d12_gap_fill_at_open` toggle

**Files:**
- Modify: `novatrade/backtest/engine.py:866-873`

Replace stop-loss check:

```python
        # --- Check stop-loss hit intra-bar ---
        if pos.side == TradeSide.LONG:
            if bar.low <= pos.current_stop:
                exit_price = pos.current_stop
                if (
                    "d12_gap_fill_at_open" in self.env.parity_audit_toggles
                    and bar.open < pos.current_stop
                ):
                    exit_price = bar.open
                self._close_position(i, exit_price, ExitReason.STOP_LOSS)
                return
        else:
            if bar.high >= pos.current_stop:
                exit_price = pos.current_stop
                if (
                    "d12_gap_fill_at_open" in self.env.parity_audit_toggles
                    and bar.open > pos.current_stop
                ):
                    exit_price = bar.open
                self._close_position(i, exit_price, ExitReason.STOP_LOSS)
                return
```

- [ ] Run D12 + regression + full suite. All green.
- [ ] Commit:

```bash
git add novatrade/backtest/engine.py tests/test_backtest_engine.py
git commit -m "feat(parity): D12 probe — gap-through stop fill toggle"
```

---

## Phase 6: D10 code-walk — same-bar TRAILING_STOP edge

### Task 6.1: Code-walk verification

**Files:**
- Create: `data/parity_audit/d10_code_walk.md`

```bash
mkdir -p data/parity_audit
```

Write the verification:

```markdown
# D10 Code-Walk: Same-Bar TRAILING_STOP Edge

## Pine (lines 725-733)
On bar close: cur_stop ratchets to max(cur_stop, ema_stop_long), then strategy.exit registers an order at cur_stop. The order fires intra-bar against this bar's high/low.

## Python (lines 866-872, 951-955)
Bar processing:
1. Stop-loss check at line 866-872: bar.low <= pos.current_stop → exit at pos.current_stop (pre-ratchet).
2. Ratchet at line 951-955: if new_trail > pos.current_stop, update. If bar.low <= NEW pos.current_stop AND bar.low > old_stop, exit at TRAILING_STOP.

## Equivalence Argument
- Case A: bar.low <= old_stop. Python step 1 fires at old_stop. Pine fires at post-ratchet cur_stop. DIVERGENCE — captured by D1.
- Case B: old_stop < bar.low <= new_stop. Python step 2 fires at new_stop. Pine fires at new_stop. EQUIVALENT.
- Case C: bar.low > new_stop. No exit either side. EQUIVALENT.

## Verdict
D10's strict `bar.low > old_stop` gate excludes only `bar.low == old_stop`, already handled by the prior stop-check at line 866 (`<=`). Tier-3 verified-equivalent given D1 captures Case-A separately.
```

- [ ] Commit:

```bash
git add data/parity_audit/d10_code_walk.md
git commit -m "docs(parity): D10 code-walk verification (Tier-3)"
```

---

## Phase 7: Tier-3 code-walk verification (D4–D9, D11)

### Task 7.1: Compose Tier-3 verifications

**Files:**
- Create: `data/parity_audit/tier3_verifications.md`

```markdown
# Tier-3 Verified-Equivalent Findings (D4–D9, D11)

## D4 — peak_fav definition
- Pine: `irb_v5_stag.pine:721` — high-based for longs.
- Python: `engine.py:877` — high-based.
- **Equivalent.** No partial in v5 → position_avg_price ≡ entry_price.

## D5 — STAG adverse condition
- Pine line 739, Python line 889. **Equivalent under v5 config.**

## D6 — STAG firing at exact `pos_bars == STAG_BARS`
- Pine line 737, Python line 887. **Equivalent.**

## D7 — TIME_STOP not-gated-on-STAG
- Pine: `if not evt_stag and pos_bars >= TIME_STOP`. Python line 911: no gate.
- **Equivalent under config (stag_bars=12 < time_stop_bars=40 never collide).**
- **Config-invariant flag:** if stag_bars >= time_stop_bars, this becomes Tier-1.

## D8 — Breakeven & trail-delay paths
- Pine: neither feature. Python: gated on `breakeven_r > 0` / `trail_delay_bars > 0`. v5 sets both to 0.
- **Equivalent — dead code.**

## D9 — Partial-exit path
- Pine: no partial-exit. Python: gated on `partial_exit_enabled`. v5: `false`.
- **Equivalent — dead code.**

## D11 — Initial-stop init at entry bar
- Already fixed by `f602c45` and `fc1eac3`. Regression-protected by `tests/test_backtest_engine.py::test_initial_stop_uses_wick_when_trail_ema_disabled`.
- **Equivalent — regression-protected.**
```

- [ ] Run regression test: `pytest tests/test_backtest_engine.py::test_initial_stop_uses_wick_when_trail_ema_disabled -v`
- [ ] Commit:

```bash
git add data/parity_audit/tier3_verifications.md
git commit -m "docs(parity): Tier-3 verifications D4-D9, D11"
```

---

## Phase 8: Measurement harness — `scripts/parity_audit.py`

### Task 8.1: Create the harness

**Files:**
- Create: `scripts/parity_audit.py`

```python
"""Pine v5 exit-timing parity-audit measurement harness.

Drives the IRBBacktester with a given parity_audit_toggles set against the
pine_aligned config and 10yr EURUSD H1 dataset, joins against the Pine baseline
trade log, and reports impact metrics.

Usage:
    python scripts/parity_audit.py --baseline
    python scripts/parity_audit.py --toggle d2_strategy_close_next_open
    python scripts/parity_audit.py --toggle d2_strategy_close_next_open d3_zero_cooldown
    python scripts/parity_audit.py --all-individual
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

from scripts.parity_match import (
    load_pine_baseline,
    load_python_results,
    partition_trades,
)
from novatrade.cli.config_schema import IRBStrategyConfig
from novatrade.backtest.runner import run_backtest_from_config

CONFIG_PATH = Path("configs/strategies/irb_v5_m5_pine_aligned.yaml")
PINE_BASELINE_PATH = Path("data/irb_novatrade_irb_v5_results_extracted.csv")
RESULTS_DIR = Path("data/parity_audit")

KNOWN_TOGGLES = (
    "d1_post_ratchet_stop_fill",
    "d2_strategy_close_next_open",
    "d3_zero_cooldown",
    "d12_gap_fill_at_open",
)

log = logging.getLogger("parity_audit")


def run_audit(toggles: frozenset[str]) -> dict:
    cfg = IRBStrategyConfig.from_yaml(CONFIG_PATH)
    cfg = cfg.copy(update={"parity_audit_toggles": toggles})
    result = run_backtest_from_config(cfg)
    pine = load_pine_baseline(PINE_BASELINE_PATH)
    python_trades = load_python_results(result)
    matched, pine_only, python_only = partition_trades(pine, python_trades)
    return {
        "toggles": sorted(toggles),
        "trades": len(python_trades),
        "pf": float(result.profit_factor),
        "matched": len(matched),
        "pine_only": len(pine_only),
        "python_only": len(python_only),
        "coverage_pct": 100.0 * len(matched) / len(pine),
        "net_pnl": float(result.net_pnl),
    }


def diff_against_baseline(baseline: dict, probe: dict) -> dict:
    return {
        "toggles": probe["toggles"],
        "delta_trades": probe["trades"] - baseline["trades"],
        "delta_pf": probe["pf"] - baseline["pf"],
        "pine_only_resolved": baseline["pine_only"] - probe["pine_only"],
        "python_only_resolved": baseline["python_only"] - probe["python_only"],
        "coverage_pct_delta": probe["coverage_pct"] - baseline["coverage_pct"],
    }


def main() -> int:  # noqa: C901
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--toggle", nargs="*", default=[])
    p.add_argument("--baseline", action="store_true")
    p.add_argument("--all-individual", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    RESULTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S")

    log.info("Running baseline (no toggles)...")
    baseline = run_audit(frozenset())
    log.info("Baseline: trades=%d pf=%.3f coverage=%.1f%%",
             baseline["trades"], baseline["pf"], baseline["coverage_pct"])
    results = [baseline]

    if args.all_individual:
        for tog in KNOWN_TOGGLES:
            log.info("Running probe: %s", tog)
            probe = run_audit(frozenset({tog}))
            probe["delta"] = diff_against_baseline(baseline, probe)
            results.append(probe)
    elif args.toggle:
        toggle_set = frozenset(args.toggle)
        log.info("Running probe with toggles: %s", sorted(toggle_set))
        probe = run_audit(toggle_set)
        probe["delta"] = diff_against_baseline(baseline, probe)
        results.append(probe)

    out_path = RESULTS_DIR / f"results_{timestamp}.json"
    out_path.write_text(json.dumps(results, indent=2))
    log.info("Results written to %s", out_path)

    print("\n=== PARITY AUDIT RESULTS ===")
    print(f"Baseline: trades={baseline['trades']} pf={baseline['pf']:.3f} "
          f"coverage={baseline['coverage_pct']:.1f}%")
    for r in results[1:]:
        d = r["delta"]
        print(f"  {','.join(r['toggles']):<60s} "
              f"Δtrades={d['delta_trades']:+d} ΔPF={d['delta_pf']:+.3f} "
              f"pine_only_resolved={d['pine_only_resolved']:+d} "
              f"python_only_resolved={d['python_only_resolved']:+d}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 1: Verify imports**

```bash
grep -rn "def run_backtest_from_config\|def partition_trades\|def load_pine_baseline" scripts/ novatrade/ | head
```

If any name doesn't exist, refactor (extract into reusable function in `scripts/parity_match.py` or import from existing entry-point).

- [ ] **Step 2: Format + lint**

```bash
ruff format scripts/parity_audit.py
ruff check scripts/parity_audit.py
```

- [ ] **Step 3: Smoke-test (baseline only)**

Run: `python scripts/parity_audit.py --baseline`
Expected: trades ≈ 1066, PF ≈ 0.888, coverage ≈ 56.6%. If outside ±5/0.005/0.5%, STOP and investigate baseline drift.

- [ ] **Step 4: Commit**

```bash
git add scripts/parity_audit.py
git commit -m "feat(parity): measurement harness scripts/parity_audit.py"
```

---

## Phase 9: Run probes, capture impact data

### Task 9.1: Confirm baseline + pin reference

```bash
python scripts/parity_audit.py --baseline
cp data/parity_audit/results_*.json data/parity_audit/baseline_reference.json
git add data/parity_audit/baseline_reference.json
git commit -m "data(parity): pin baseline reference for audit run"
```

Expected baseline: trades 1066±5, PF 0.888±0.005, coverage 56.6%±0.5%.

### Task 9.2: Run each probe in isolation

```bash
python scripts/parity_audit.py --all-individual
git add data/parity_audit/results_*.json
git commit -m "data(parity): individual-probe impact measurements"
```

### Task 9.3: Run paired probes (D2+D3 interaction)

```bash
python scripts/parity_audit.py --toggle d2_strategy_close_next_open d3_zero_cooldown
git add data/parity_audit/results_*.json
git commit -m "data(parity): D2+D3 paired-probe interaction measurement"
```

Compute interaction: `paired_resolved - sum_of_individuals`. If `>5`: positive interaction. If `<-5`: cancellation. Document in audit doc.

---

## Phase 10: Compose findings doc + vault summary + memory pointer

### Task 10.1: Create `docs/parity/exit-timing-audit.md`

```bash
mkdir -p docs/parity
sha256sum data/irb_novatrade_irb_v5_results_extracted.csv
```

Embed the SHA in the doc header. Compose the doc using the schema:

```markdown
### D{N} — {Title}                                           [Tier-{1|2|3}]

**Pine source:** `configs/pinescript/irb_v5_stag.pine:LINE` — code excerpt
**Python source:** `novatrade/backtest/engine.py:LINE` — code excerpt
**Divergence:** {1-3 sentences}
**Hypothesized impact:** {direction + bucket}
**Probe:** {toggle name + behavior change} (or N/A for Tier-3)
**Measured impact:** {pine_only_resolved, python_only_resolved, ΔPF, Δtrades from results JSON}
**Status:** documented
**Notes:** edge cases, dependencies, interactions
```

Header includes: audit method, tiering thresholds, baseline metrics, Pine baseline SHA, link to design spec, link to latest results JSON. For Tier-3 entries, fold in `data/parity_audit/tier3_verifications.md` and `data/parity_audit/d10_code_walk.md`. Use measured impact from Phase 9 — NO PLACEHOLDERS.

### Task 10.2: Re-tier based on measured impact

For each D{N}: total_resolved = pine_only_resolved + python_only_resolved.
- ≥10 → Tier-1
- 1–9 → Tier-2
- 0 → Tier-3

Update tier labels and re-sort entries (Tier-1 first).

**Verify success criterion:** spec requires ≥1 Tier-1. If zero:
1. Sum total resolved across all probes.
2. If total < 453 (50% of remaining 906): flag Risk #3 triggered, recommend Pine TV log capture fallback.
3. Else: document as "diffuse leak."

### Task 10.3: Write vault summary

Use `mcp__nova-vault__vault_write` to write `Engineering/parity/Pine v5 Exit-Timing Audit.md`:

Frontmatter:

```yaml
title: "Pine v5 Exit-Timing Audit"
type: engineering-note
date: 2026-04-XX
date_created: 2026-04-XX
tags: ["#area/parity", "#project/nova-core"]
status: complete
```

Body: Tier-1 findings only (one paragraph each), plus link "See `docs/parity/exit-timing-audit.md` (commit `<HASH>`) for full findings."

### Task 10.4: Update memory pointer

**Files (outside repo):**
- Modify: `/home/nova/.claude/projects/-home-nova-nova-core/memory/project_pine_parity_state.md`
- Modify: `/home/nova/.claude/projects/-home-nova-nova-core/memory/MEMORY.md`

Append to `project_pine_parity_state.md`:

```markdown

**Exit-timing audit (<DATE>):** see `docs/parity/exit-timing-audit.md` for tiered findings. Tier-1: <LIST>. Probe scaffold lives at `env.parity_audit_toggles` (gated off in production via config_schema validation). Fix-plan brainstorm queued.
```

Update `MEMORY.md` index entry for `project_pine_parity_state.md`:

```markdown
- [Pine v5 parity state (<DATE>)](project_pine_parity_state.md) — Audit complete; Tier-1 fixes queued for separate brainstorm
```

### Task 10.5: Commit doc

```bash
git add docs/parity/exit-timing-audit.md
git commit -m "docs(parity): exit-timing audit findings — tiered, measured-impact"
```

Verify vault note via `vault_read path: "Engineering/parity/Pine v5 Exit-Timing Audit.md"`. Memory updates do not need git commits (auto-memory lives outside repo).

---

## Phase 11: Self-review + handoff

### Task 11.1: Run full test suite

```bash
pytest tests/test_backtest_engine.py -q
pytest tests/test_backtest_engine.py::TestParityAuditToggleNoOp tests/test_backtest_engine.py::test_initial_stop_uses_wick_when_trail_ema_disabled -v
```

Expected: all green.

### Task 11.2: Confirm live champion is unaffected

```bash
python -m novatrade.cli.backtest --config configs/strategies/irb_v5_m5_champion.yaml --output /tmp/champion_post_audit.json
```

Diff against pre-audit golden if available; otherwise compare key metrics (trade count, PF, net pnl) against the live-champion memory snapshot in `project_pine_parity_state.md`.

Append to `docs/parity/exit-timing-audit.md`:

```markdown
## Live-Champion Verification

Live config (`irb_v5_m5_champion.yaml`) re-run post-audit produces metrics identical to pre-audit baseline. Verified <DATE> against <REFERENCE>. Audit changes do not affect live behavior.
```

### Task 11.3: Queue fix-plan-brainstorm

Append to `project_pine_parity_state.md`:

```markdown

**Next session:** Fix-plan brainstorm. Entry move:
> /brainstorm Fix Pine v5 exit-timing parity Tier-1 findings: <LIST>. See docs/parity/exit-timing-audit.md (commit <HASH>) for measured impact and probe code. Each fix is gated off via parity_audit_toggles already; promote to default behavior + write TDD red-green-refactor for each.
```

Optionally write `TASKS/fix-pine-exit-timing-tier1.md` if surfacing in TASKS/ pipeline:

```bash
cat > TASKS/fix-pine-exit-timing-tier1.md <<'EOF'
# Fix Pine v5 Exit-Timing Tier-1 Findings

**Source:** docs/parity/exit-timing-audit.md (commit <HASH>)
**Action:** /brainstorm fix plan for Tier-1 divergences <LIST>.

Tier-1 findings are already gated as parity_audit_toggles. Fix-plan brainstorm produces TDD-ordered tasks promoting each toggle to default behavior with red-green-refactor and live-regression preservation.
EOF
git add TASKS/fix-pine-exit-timing-tier1.md
git commit -m "tasks(parity): queue fix-plan brainstorm for Tier-1 findings"
```

Final check + announcement:

```bash
git status
git log --oneline -15
```

Announce: "Audit complete. Live champion regression green. Tier-1: <LIST>. Findings doc at docs/parity/exit-timing-audit.md. Vault summary written. Fix-plan brainstorm queued. Next move: fresh session + /brainstorm."

---

## Self-Review

- **Spec coverage:** every D1–D12 has a probe (D1, D2, D3, D12), code-walk (D4–D11), or both. ✓
- **Live-safety:** Phase 1 establishes regression test gate; every toggle uses string-membership-check on empty default. ✓
- **Type consistency:** `parity_audit_toggles` is `frozenset[str]` everywhere; toggle string labels match across env / config_schema / engine.py / harness. ✓
- **TDD:** every probe phase = failing test → implement → verify. ✓
- **DRY:** ratchet logic extracted to `_ratchet_trail_only`; result-loading reused from `parity_match.py`. ✓
- **YAGNI:** Tier-3 candidates don't get toggle code; only D1, D2, D3, D12 (hypothesized Tier-1/2). ✓
- **Frequent commits:** each phase ends with at least one commit. ✓
