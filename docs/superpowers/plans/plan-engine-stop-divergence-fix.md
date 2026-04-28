# Engine-Stop-Divergence Fix Implementation Plan

> **For agentic workers:** use the `implementation-team` skill to execute this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Initialize `pos.current_stop` from `trail_ema[entry_bar]` (Pine v5 cur_stop semantics) when `trail_ema_period > 0`, while keeping `stop_loss` and `initial_stop` at the IRB wick (Pine `f_qty(ep, sp)` sizing anchor).

**Architecture:** Single-touch override at fill time inside `IRBBacktester._check_pending_fill`. Plumbed via a per-bar `self._cur_trail_ema` stash mirroring the existing `_cur_atr` pattern. Behavior gated on `env.trail_ema_period > 0`, so the live champion (ATR-trail mode, `trail_ema_period == 0`) is bit-identical to today.

**Tech Stack:** Python 3.10, pytest, pre-commit hooks (ruff + mypy).

**Source spec:** `docs/superpowers/specs/2026-04-27-engine-stop-divergence-fix-design.md`

**Worktree:** `.worktrees/parity-engine-fixes` (off `feat/risk-reduction-0.33pct`).

---

## Phases

- [ ] **Phase 1: TDD the EMA-stop override** — Tasks 1–4 drive the override and its three guards via failing tests.
- [ ] **Phase 2: Regression smoke** — Task 5 adds the 2022 Pine-aligned PF ≥ 1.0 integration test (data-availability gated).
- [ ] **Phase 3: Operator validation** — Task 6 runs the manual smoke against the full 10yr dataset and writes the summary commit.

---

## Task 1: TDD — `current_stop` uses EMA at fill (happy path)

**Files:**
- Modify: `novatrade/backtest/engine.py:357-362` (`_process_bar` — stash `_cur_trail_ema`)
- Modify: `novatrade/backtest/engine.py:715-732` (`_check_pending_fill` — override `pos.current_stop`)
- Test: `tests/test_backtest_engine.py` (append new class `TestInitialStopFromEmaTrail`)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_backtest_engine.py`:

```python
class TestInitialStopFromEmaTrail:
    """Pine v5 initializes the live trigger stop from EMA(40), not the IRB
    wick. These tests guard that semantics in the Python engine.

    Spec: docs/superpowers/specs/2026-04-27-engine-stop-divergence-fix-design.md
    """

    def _make_env(self, **overrides) -> BacktestEnvironment:
        defaults = dict(
            warmup_bars=5,
            initial_equity=100_000.0,
            trail_ema_period=40,
            atr_sl_floor_multiplier=0.0,
            sl_spread_buffer_pips=0.0,
            partial_exit_enabled=False,
            use_simple_trend_filter=True,
        )
        defaults.update(overrides)
        return BacktestEnvironment(**defaults)  # type: ignore[arg-type]

    def _setup_pending(
        self,
        env: BacktestEnvironment,
        side: TradeSide,
        entry_price: float,
        wick_stop: float,
        cur_trail_ema: float,
    ) -> IRBBacktester:
        """Create a backtester with a _PendingOrder injected and the
        per-bar trail_ema stash set, ready for a _check_pending_fill call.
        """
        from novatrade.backtest.engine import _PendingOrder

        bt = IRBBacktester(env=env)
        bt._state = (
            StrategyState.PENDING_LONG if side == TradeSide.LONG else StrategyState.PENDING_SHORT
        )
        bt._pending = _PendingOrder(
            side=side,
            entry_price=entry_price,
            stop_loss=wick_stop,
            volume=0.10,
            bar_placed=0,
            irb_bar=0,
        )
        bt._cur_atr = 0.0010  # arbitrary non-zero
        bt._cur_trail_ema = cur_trail_ema
        return bt

    def test_initial_stop_uses_trail_ema_when_enabled(self):
        """Long fill with trail_ema_period>0 → current_stop = trail_ema, not wick."""
        env = self._make_env(trail_ema_period=40)
        wick_stop = 1.09800  # 20 pips below entry
        ema_at_entry = 1.09950  # 5 pips below entry — closer than wick
        bt = self._setup_pending(
            env,
            TradeSide.LONG,
            entry_price=1.10000,
            wick_stop=wick_stop,
            cur_trail_ema=ema_at_entry,
        )

        # Bar that triggers fill (high crosses entry_price)
        fill_bar = _candle(o=1.09990, h=1.10010, low=1.09990, c=1.10005, ts=3600.0)
        bt._check_pending_fill(1, fill_bar)

        assert bt._position is not None
        # Live trigger stop must be the EMA value (Pine cur_stop semantics)
        assert bt._position.current_stop == pytest.approx(ema_at_entry, abs=1e-6)
        # Sizing/R anchors must remain at the wick
        assert bt._position.stop_loss == pytest.approx(wick_stop, abs=1e-6)
        assert bt._position.initial_stop == pytest.approx(wick_stop, abs=1e-6)
```

- [ ] **Step 2: Run the test and confirm it fails for the right reason**

Run: `PYTHONPATH=. python3 -m pytest tests/test_backtest_engine.py::TestInitialStopFromEmaTrail::test_initial_stop_uses_trail_ema_when_enabled -v`

Expected: FAIL with `assert <wick_stop> == <ema_at_entry>`. The current engine sets `current_stop = p.stop_loss = wick_stop`, so the EMA value is ignored. This is the right failure — confirms the test exercises the missing behavior.

- [ ] **Step 3: Implement minimal override**

Edit `novatrade/backtest/engine.py` lines 357-362 to also stash `_cur_trail_ema`. Replace:

```python
    ) -> None:
        """Process a single H1 bar."""
        # Stash current-bar ATR so _check_pending_fill can stamp entry_atr
        # on the new position without changing the call signature.
        self._cur_atr = atr_h1[i] if i < len(atr_h1) and not math.isnan(atr_h1[i]) else 0.0
        self._detect_day_boundary(i, bar)
```

with:

```python
    ) -> None:
        """Process a single H1 bar."""
        # Stash current-bar indicators so _check_pending_fill can stamp them
        # on the new position without changing the call signature.
        self._cur_atr = atr_h1[i] if i < len(atr_h1) and not math.isnan(atr_h1[i]) else 0.0
        self._cur_trail_ema = (
            trail_ema[i]
            if (
                trail_ema is not None
                and self.env.trail_ema_period > 0
                and i < len(trail_ema)
                and not math.isnan(trail_ema[i])
            )
            else float("nan")
        )
        self._detect_day_boundary(i, bar)
```

Then in `_check_pending_fill` (around line 715-732), after the existing `self._position = _OpenPosition(...)` block and before `self._state = ...`, add the override:

```python
            # Open position
            self._position = _OpenPosition(
                side=p.side,
                entry_price=fill_price,
                stop_loss=p.stop_loss,
                volume=p.volume,
                entry_bar=i,
                current_stop=p.stop_loss,
                best_close=bar.close,
                initial_stop=p.stop_loss,  # v5
                initial_volume=p.volume,  # v5
                entry_atr=getattr(self, "_cur_atr", 0.0),  # v5 stagnation guard
            )
            # Pine v5 parity: when EMA-trail is the trail mode, the live
            # trigger stop is initialized from trail_ema (Pine cur_stop), not
            # the IRB wick. stop_loss/initial_stop stay at the wick because
            # they drive sizing and R-multiple math (Pine f_qty(ep, sp)).
            ema_stop = getattr(self, "_cur_trail_ema", float("nan"))
            if not math.isnan(ema_stop):
                self._position.current_stop = ema_stop
                if (
                    p.side == TradeSide.LONG and ema_stop >= fill_price
                ) or (p.side == TradeSide.SHORT and ema_stop <= fill_price):
                    log.debug(
                        "bar %d: EMA wrong-sided at entry (%s fill=%.5f ema=%.5f) — instant SL likely",
                        i,
                        p.side.value,
                        fill_price,
                        ema_stop,
                    )
            self._state = StrategyState.LONG if p.side == TradeSide.LONG else StrategyState.SHORT
            self._pending = None
            self._trades_today += 1
```

- [ ] **Step 4: Run test and confirm GREEN, plus full backtest suite**

Run: `PYTHONPATH=. python3 -m pytest tests/test_backtest_engine.py::TestInitialStopFromEmaTrail::test_initial_stop_uses_trail_ema_when_enabled -v`
Expected: PASS

Run: `PYTHONPATH=. python3 -m pytest tests/test_backtest_engine.py tests/test_backtest_edge_cases.py tests/test_backtest_environment.py tests/test_backtest_metrics.py -q`
Expected: 99+ tests pass, 0 fail.

- [ ] **Step 5: Commit**

```bash
git add novatrade/backtest/engine.py tests/test_backtest_engine.py
git commit -m "$(cat <<'EOF'
feat(backtest): EMA-trail initial stop matches Pine v5 cur_stop semantics

When trail_ema_period > 0, initialize pos.current_stop from trail_ema
at the entry bar instead of the IRB wick. Mirrors Pine v5
'cur_stop := math.max(nz(cur_stop, ema_stop_long), ema_stop_long)'
on the first bar in LONG/SHORT state.

stop_loss and initial_stop remain at the wick to preserve Pine's
f_qty(ep, sp) sizing semantics and R-multiple denominator.

Live champion (ATR-trail, trail_ema_period == 0) is unaffected.

Spec: docs/superpowers/specs/2026-04-27-engine-stop-divergence-fix-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: TDD — wick stop preserved when `trail_ema_period == 0`

**Files:**
- Test: `tests/test_backtest_engine.py` (append to `TestInitialStopFromEmaTrail`)

- [ ] **Step 1: Write the failing test**

Append inside the `TestInitialStopFromEmaTrail` class:

```python
    def test_initial_stop_uses_wick_when_trail_ema_disabled(self):
        """Live-champion regression guard: trail_ema_period=0 keeps wick stop."""
        env = self._make_env(trail_ema_period=0)
        wick_stop = 1.09800
        bt = self._setup_pending(
            env,
            TradeSide.LONG,
            entry_price=1.10000,
            wick_stop=wick_stop,
            cur_trail_ema=float("nan"),  # _process_bar stashes NaN when disabled
        )
        fill_bar = _candle(o=1.09990, h=1.10010, low=1.09990, c=1.10005, ts=3600.0)
        bt._check_pending_fill(1, fill_bar)

        assert bt._position is not None
        # Override must NOT fire when trail_ema_period == 0
        assert bt._position.current_stop == pytest.approx(wick_stop, abs=1e-6)
        assert bt._position.stop_loss == pytest.approx(wick_stop, abs=1e-6)
```

- [ ] **Step 2: Run the test and confirm it passes**

Run: `PYTHONPATH=. python3 -m pytest tests/test_backtest_engine.py::TestInitialStopFromEmaTrail::test_initial_stop_uses_wick_when_trail_ema_disabled -v`

Expected: PASS. The Task 1 implementation already handles this correctly — when `trail_ema_period == 0`, `_process_bar` stashes NaN, the NaN guard in `_check_pending_fill` skips the override. This test is a regression guard, not a driver.

If FAIL: implementation is wrong (check the `not math.isnan(ema_stop)` guard); fix before continuing.

- [ ] **Step 3: Commit**

```bash
git add tests/test_backtest_engine.py
git commit -m "$(cat <<'EOF'
test(backtest): regression guard for wick stop when EMA-trail disabled

Confirms trail_ema_period=0 (live-champion ATR-trail mode) keeps the
existing wick-based current_stop initialization. Locks in the live-system
invariant from the engine-stop-divergence fix.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: TDD — fall back to wick during EMA warmup

**Files:**
- Test: `tests/test_backtest_engine.py` (append to `TestInitialStopFromEmaTrail`)

- [ ] **Step 1: Write the failing test**

Append:

```python
    def test_initial_stop_falls_back_to_wick_when_ema_is_nan(self):
        """If EMA isn't computed yet (warmup race), fall back to wick stop."""
        env = self._make_env(trail_ema_period=40)
        wick_stop = 1.09800
        bt = self._setup_pending(
            env,
            TradeSide.LONG,
            entry_price=1.10000,
            wick_stop=wick_stop,
            cur_trail_ema=float("nan"),
        )
        fill_bar = _candle(o=1.09990, h=1.10010, low=1.09990, c=1.10005, ts=3600.0)
        bt._check_pending_fill(1, fill_bar)

        assert bt._position is not None
        assert bt._position.current_stop == pytest.approx(wick_stop, abs=1e-6)
```

- [ ] **Step 2: Run the test and confirm it passes**

Run: `PYTHONPATH=. python3 -m pytest tests/test_backtest_engine.py::TestInitialStopFromEmaTrail::test_initial_stop_falls_back_to_wick_when_ema_is_nan -v`

Expected: PASS. The `not math.isnan(ema_stop)` guard from Task 1 handles this. Regression guard.

If FAIL: NaN handling is missing; verify the `getattr(..., float("nan"))` and `not math.isnan` guards in `_check_pending_fill`.

- [ ] **Step 3: Commit**

```bash
git add tests/test_backtest_engine.py
git commit -m "$(cat <<'EOF'
test(backtest): regression guard for NaN EMA fallback to wick stop

Confirms that when the per-bar trail_ema stash is NaN (warmup race or
attribute missing), _check_pending_fill falls back to the IRB wick stop
rather than crashing or setting current_stop to NaN.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: TDD — unconditional override on wrong-sided EMA

**Files:**
- Test: `tests/test_backtest_engine.py` (append to `TestInitialStopFromEmaTrail`)

- [ ] **Step 1: Write the failing test**

Append:

```python
    def test_initial_stop_unconditional_when_ema_wrong_sided_long(self):
        """Pine-faithful: if EMA >= fill_price for a long, current_stop is
        still set to EMA (instant SL on next adverse tick). Pine does not
        clamp; Python must not either.
        """
        env = self._make_env(trail_ema_period=40)
        wick_stop = 1.09800  # 20 pips below entry
        ema_above_entry = 1.10050  # 5 pips ABOVE entry — wrong side
        bt = self._setup_pending(
            env,
            TradeSide.LONG,
            entry_price=1.10000,
            wick_stop=wick_stop,
            cur_trail_ema=ema_above_entry,
        )
        fill_bar = _candle(o=1.09990, h=1.10010, low=1.09990, c=1.10005, ts=3600.0)
        bt._check_pending_fill(1, fill_bar)

        assert bt._position is not None
        # No clamping to wick; EMA used unconditionally per Pine semantics
        assert bt._position.current_stop == pytest.approx(ema_above_entry, abs=1e-6)
        # Wick still the sizing anchor
        assert bt._position.initial_stop == pytest.approx(wick_stop, abs=1e-6)

    def test_initial_stop_unconditional_when_ema_wrong_sided_short(self):
        """Symmetric guard for short side."""
        env = self._make_env(trail_ema_period=40)
        wick_stop = 1.10200  # 20 pips above entry
        ema_below_entry = 1.09950  # 5 pips BELOW entry — wrong side for short
        bt = self._setup_pending(
            env,
            TradeSide.SHORT,
            entry_price=1.10000,
            wick_stop=wick_stop,
            cur_trail_ema=ema_below_entry,
        )
        fill_bar = _candle(o=1.10010, h=1.10010, low=1.09990, c=1.09995, ts=3600.0)
        bt._check_pending_fill(1, fill_bar)

        assert bt._position is not None
        assert bt._position.current_stop == pytest.approx(ema_below_entry, abs=1e-6)
        assert bt._position.initial_stop == pytest.approx(wick_stop, abs=1e-6)
```

- [ ] **Step 2: Run the tests and confirm they pass**

Run: `PYTHONPATH=. python3 -m pytest tests/test_backtest_engine.py::TestInitialStopFromEmaTrail -v`

Expected: PASS for both wrong-sided tests. The Task 1 implementation has no clamping — `self._position.current_stop = ema_stop` runs regardless of side. The `log.debug` line fires for wrong-sided cases but doesn't change behavior.

If FAIL: confirm there is no clamping/side-check guard around `self._position.current_stop = ema_stop`.

- [ ] **Step 3: Commit**

```bash
git add tests/test_backtest_engine.py
git commit -m "$(cat <<'EOF'
test(backtest): regression guard for unconditional EMA override

Pine-faithful semantics: when trail_ema is on the wrong side of fill_price
(EMA above entry for a long, or below entry for a short), Python must NOT
clamp to the wick — Pine sets cur_stop to ema_stop_long unconditionally
and books an instant SL when adverse.

Two tests cover long and short symmetry.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Integration smoke — Pine-aligned 2022 PF ≥ 1.0

**Files:**
- Test: `tests/test_backtest_engine.py` (new test class `TestPineAlignedParitySmoke`)

- [ ] **Step 1: Write the integration test**

Append at end of file:

```python
class TestPineAlignedParitySmoke:
    """End-to-end smoke: Pine-aligned config + EMA-stop fix should produce
    PF >= 1.0 on the 2022 EURUSD H1+H4 slice. Pine reports PF 2.06 / +$11k
    for that year; PF >= 1.0 is a generous floor confirming the fix moves
    the engine into Pine's profitable regime.

    Data-availability gated: data/candles/EURUSD_H1_10yr.csv is in /data/
    (gitignored), so this test is skipped in CI but runs locally for
    developers and as a /finishing-a-development-branch gate.
    """

    DATA_PATH = Path("data/candles/EURUSD_H1_10yr.csv")
    CFG_PATH = Path("configs/strategies/irb_v5_m5_pine_aligned.yaml")

    @pytest.mark.skipif(
        not DATA_PATH.exists() or not CFG_PATH.exists(),
        reason="10yr historical CSV or pine-aligned config not available locally",
    )
    def test_pine_aligned_2022_pf_crosses_one(self):
        from dataclasses import replace
        from datetime import datetime, timezone

        from novatrade.backtest.metrics import ExitReason
        from novatrade.cli.commands.data import aggregate_h1_to_h4
        from novatrade.cli.config_schema import StrategyConfig

        # Load full H1 series, slice to 2022 with 30d warmup
        warmup_start = datetime(2021, 12, 1, tzinfo=timezone.utc).timestamp()
        end = datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp()
        h1_all: list[Candle] = []
        with self.DATA_PATH.open() as f:
            import csv

            for r in csv.DictReader(f):
                ts_str = r["timestamp"].strip()
                ts = (
                    float(ts_str)
                    if ts_str.replace(".", "").isdigit()
                    else datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    .replace(tzinfo=timezone.utc)
                    .timestamp()
                )
                if not (warmup_start <= ts < end):
                    continue
                h1_all.append(
                    Candle(
                        timestamp=ts,
                        open=float(r["open"]),
                        high=float(r["high"]),
                        low=float(r["low"]),
                        close=float(r["close"]),
                        volume=float(r.get("volume") or 0),
                        symbol="EURUSD",
                        timeframe="H1",
                    )
                )
        h1 = sorted(h1_all, key=lambda c: c.timestamp)
        h4 = aggregate_h1_to_h4(h1)
        assert len(h1) > 6000, f"expected ~8800 H1 bars, got {len(h1)}"

        cfg = StrategyConfig.from_yaml(self.CFG_PATH)
        env_kwargs = cfg.to_environment_kwargs()
        # The Pine source has the EMA-stack filter always-on; pine_aligned.yaml
        # already sets use_ema_stack_filter: true. Belt-and-braces here.
        env_kwargs["use_ema_stack_filter"] = True

        base = BacktestEnvironment.for_v5_m5()
        valid = {k: v for k, v in env_kwargs.items() if k in base.__dataclass_fields__}
        env = replace(
            base,
            primary_timeframe="H1",
            higher_timeframe="H4",
            h1_to_h4_ratio=4,
            h1_bars_per_day=24,
            h4_bars_per_day=6,
            min_volume=1.0,
            max_volume=1.0,  # fixed-1-lot to match Pine sizing semantics
            **valid,
        )

        bt = IRBBacktester(env=env)
        res = bt.run(h1, h4)

        # In-2022 trades only (filter on entry_bar timestamp)
        jan22 = datetime(2022, 1, 1, tzinfo=timezone.utc).timestamp()
        trades_2022 = [
            t
            for t in res.trades
            if 0 <= t.entry_bar < len(h1) and h1[t.entry_bar].timestamp >= jan22
        ]
        assert len(trades_2022) > 0, "no 2022 trades — config or data path broken"

        wins = [t for t in trades_2022 if t.pnl_usd > 0]
        losses = [t for t in trades_2022 if t.pnl_usd < 0]
        gross_w = sum(t.pnl_usd for t in wins)
        gross_l = -sum(t.pnl_usd for t in losses)
        pf = gross_w / gross_l if gross_l > 0 else float("inf")

        # Headline assertion: PF crosses 1.0 (Pine reports PF 2.06 for 2022)
        assert pf >= 1.0, (
            f"Pine-aligned 2022 PF={pf:.3f} < 1.0 — engine-stop-divergence fix "
            f"did not close the parity gap. Pine baseline: PF 2.06."
        )
        # Trade count within ±20% of Pine's 108
        assert 86 <= len(trades_2022) <= 130, (
            f"Pine-aligned 2022 trade count {len(trades_2022)} outside ±20% of Pine 108"
        )
        # Sanity: no SL trade lost more than 50 pips at fixed 1 lot ($500)
        for t in trades_2022:
            if t.exit_reason == ExitReason.STOP_LOSS:
                assert t.pnl_pips > -50, (
                    f"trade {t.trade_id}: SL hit at {t.pnl_pips:.1f} pips — runaway stop?"
                )
```

Add the import at top of `tests/test_backtest_engine.py` if not present:

```python
from pathlib import Path
```

- [ ] **Step 2: Run locally; confirm pass or skip**

Run: `PYTHONPATH=. python3 -m pytest tests/test_backtest_engine.py::TestPineAlignedParitySmoke -v`

Expected (with data present locally): PASS, with `pf >= 1.0` reported.
Expected (without data): SKIPPED with reason listed.

If PASS but pf < 1.0 surfaces somewhere else, escalate — the EMA-stop fix did not fully close the gap. Spread double-counting follow-up may be needed.

- [ ] **Step 3: Commit**

```bash
git add tests/test_backtest_engine.py
git commit -m "$(cat <<'EOF'
test(parity): 2022 Pine-aligned PF >= 1.0 integration smoke

Loads the 2022 EURUSD H1 slice (with 30d warmup), runs the engine with
configs/strategies/irb_v5_m5_pine_aligned.yaml at fixed-1-lot sizing,
asserts PF >= 1.0 (Pine reports 2.06 for 2022), trade count within +/-20%
of Pine's 108, and no SL exit > 50 pips.

Data-availability gated: skipped when data/candles/EURUSD_H1_10yr.csv is
absent (it's gitignored under /data/). Acts as a developer-machine smoke
and a /finishing-a-development-branch gate.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Manual smoke + summary commit

**Files:**
- Run: `scripts/parity_fixed_sizing.py`
- Modify: `OUTPUT/parity_recovery_summary_20260427.md` (append "Phase 4: EMA-stop fix" section)

- [ ] **Step 1: Run the parity smoke**

Run: `PYTHONPATH=. python3 scripts/parity_fixed_sizing.py 2>&1 | tee LOGS/parity_fixed_sizing_emastopfix.log`

Expected output: at least one fixed-sizing scenario shows PF ≥ 1.0. The "Pine-equivalent sizing (1% risk, max=1.0 lot)" or "Fixed 1.0 lot (min=max=1.0)" rows are the leading candidates. Compare avg_loss to Pine's ~7.6 pips: should be in the same ballpark (~7–10 pips), not 16 pips.

If no scenario reaches PF ≥ 1.0, **STOP**: file an issue and escalate. The spread double-counting follow-up may be needed before declaring parity recovered.

- [ ] **Step 2: Update the recovery summary**

Append to `OUTPUT/parity_recovery_summary_20260427.md`:

```markdown
---

## Phase 4: EMA-trail initial stop fix (commit ${SHA1})

The EMA-stop fix sets `pos.current_stop = trail_ema[entry_bar]` when
`trail_ema_period > 0`, matching Pine v5 `cur_stop` semantics. The wick
remains the sizing anchor (`stop_loss`/`initial_stop`) per Pine
`f_qty(ep, sp)`.

Empirical result on the 10yr Pine-aligned config (fixed 1-lot sizing):

| Metric | Before | After | Pine target |
|---|---:|---:|---:|
| PF | 0.675 | **<actual>** | 1.078 |
| avg_loss | $164 (~16 pips) | **<actual>** | ~$76 (~7.6 pips) |
| avg_win | $488 (~49 pips) | **<actual>** | ~$394 (~39 pips) |
| Net P&L | −$40,292 | **<actual>** | +$8,887 |

5 unit tests + 1 integration smoke locked in. Live champion (ATR-trail,
trail_ema_period == 0) bit-identical to before — verified by regression
test `test_initial_stop_uses_wick_when_trail_ema_disabled`.

**Status:** PF ≥ 1.0 ✅ / ❌ (fill in based on smoke run)
```

Replace `${SHA1}` with the commit hash from Task 1 and `<actual>` with the numbers from Step 1's log.

- [ ] **Step 3: Commit summary update**

```bash
git add OUTPUT/parity_recovery_summary_20260427.md LOGS/parity_fixed_sizing_emastopfix.log
git commit -m "$(cat <<'EOF'
docs(parity): summarize Phase 4 EMA-stop fix results

Appends actual post-fix numbers from scripts/parity_fixed_sizing.py to the
parity recovery summary, completing the 4-phase walkthrough from the
original 22,286-trade -103% baseline through to the EMA-stop fix.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 4: Final regression sweep**

Run: `PYTHONPATH=. python3 -m pytest tests/test_backtest_engine.py tests/test_backtest_environment.py tests/test_backtest_edge_cases.py tests/test_backtest_metrics.py tests/test_live_strategy_engine.py -q`

Expected: all 152+ tests pass (148 baseline + 5 new).

If any test fails: STOP, investigate, do not proceed to merge.

---

## Done-criteria

- 5 new unit tests + 1 integration test, all passing locally (integration may skip in CI).
- `scripts/parity_fixed_sizing.py` shows PF ≥ 1.0 on at least one fixed-sizing scenario.
- All pre-existing tests still green.
- Live-champion regression test confirms ATR-trail mode unchanged.
- 6 commits on the `parity-engine-fixes` branch (one per task).
- Spec referenced in commit messages.

After this plan completes, the next steps are:
1. `dual-code-review` (Codex + Opus) on the engine.py diff.
2. `finishing-a-development-branch` to validate pre-ship gates and present merge options.
3. Operator approval before merge.
