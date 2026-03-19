"""Comprehensive tests for NovaTrade AutoResearch Phase 0 modules.

Covers:
  - novatrade.evaluation: fill_model, spread_model, slippage_model,
    commission_model, session_calendar, intrabar_policy, gates, fitness
  - novatrade.storage: data_snapshot, experiment_db, experiment_identity, artifacts
  - novatrade.cli.config_schema: StrategyConfig, ParameterBounds
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

# ---------------------------------------------------------------------------
# CLI imports
# ---------------------------------------------------------------------------
from novatrade.cli.config_schema import ParameterBounds, StrategyConfig
from novatrade.evaluation.commission_model import (
    DEFAULT_COMMISSION_MODEL,
    CommissionModelConfig,
)

# ---------------------------------------------------------------------------
# Evaluation imports
# ---------------------------------------------------------------------------
from novatrade.evaluation.fill_model import DEFAULT_FILL_MODEL, FillModelConfig
from novatrade.evaluation.fitness import (
    FitnessResult,
    compute_promotion_score,
    compute_scout_score,
)
from novatrade.evaluation.gates import (
    GateConfig,
    GateResult,
    GateResults,
    evaluate_stage_a,
    evaluate_stage_b,
    evaluate_stage_c_stub,
)
from novatrade.evaluation.intrabar_policy import (
    DEFAULT_INTRABAR_POLICY,
    AmbiguityResolution,
    IntrabarPolicyConfig,
)
from novatrade.evaluation.session_calendar import (
    DEFAULT_SESSION_CALENDAR,
    Session,
    SessionCalendarConfig,
)
from novatrade.evaluation.slippage_model import DEFAULT_SLIPPAGE_MODEL, SlippageModelConfig
from novatrade.evaluation.spread_model import DEFAULT_SPREAD_MODEL, SpreadModelConfig

# ---------------------------------------------------------------------------
# Candle import (real dataclass, lightweight)
# ---------------------------------------------------------------------------
from novatrade.models import Candle
from novatrade.storage.artifacts import (
    load_config_snapshot,
    save_config_snapshot,
    save_experiment_log,
    save_trade_log,
)

# ---------------------------------------------------------------------------
# Storage imports
# ---------------------------------------------------------------------------
from novatrade.storage.data_snapshot import (
    fingerprint_candles,
    list_snapshots,
    load_snapshot,
    save_snapshot,
)
from novatrade.storage.experiment_db import ExperimentDB, ExperimentRecord
from novatrade.storage.experiment_identity import (
    compute_config_hash,
    compute_experiment_id,
    get_engine_sha,
)

# ===================================================================
# Helpers
# ===================================================================


def _make_metrics(**overrides) -> SimpleNamespace:
    """Create a mock BacktestMetrics-like object with sensible defaults."""
    defaults = dict(
        total_completed_trades=100,
        total_bars=24 * 22 * 12,  # ~12 months of hourly data
        top_3_trades_pct_of_profit=20.0,
        max_drawdown_pct=5.0,
        profit_factor=1.8,
        net_result_pips=500.0,
        expectancy_r=0.5,
        win_rate=0.55,
        max_consecutive_losses=4,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_environment(**overrides) -> dict:
    """Create a mock BacktestEnvironment-like dict with realistic defaults."""
    defaults = dict(
        avg_spread_pips=1.0,
        slippage_pips=0.5,
        commission_per_lot_usd=0.0,
        stop_first_on_ambiguity=True,
    )
    defaults.update(overrides)
    return defaults


def _make_candles(
    n: int, *, symbol: str = "EURUSD", timeframe: str = "H1", base_ts: float = 1_700_000_000.0
) -> list[Candle]:
    """Generate n synthetic candles."""
    candles = []
    for i in range(n):
        ts = base_ts + i * 3600
        o = 1.1000 + i * 0.0001
        candles.append(
            Candle(
                timestamp=ts,
                open=o,
                high=o + 0.0010,
                low=o - 0.0005,
                close=o + 0.0003,
                volume=float(1000 + i),
                symbol=symbol,
                timeframe=timeframe,
            )
        )
    return candles


# ===================================================================
# Sacred Evaluation Layer Tests
# ===================================================================


class TestFillModel:
    """Fill model config: defaults, immutability, versioning."""

    def test_defaults_are_conservative(self):
        fm = FillModelConfig()
        assert fm.stop_first_on_ambiguity is True
        assert fm.fill_on_touch is True
        assert fm.gap_fill_at_open is True
        assert fm.use_high_low_check is True

    def test_default_singleton_matches(self):
        assert DEFAULT_FILL_MODEL == FillModelConfig()

    def test_frozen_cannot_mutate(self):
        fm = FillModelConfig()
        with pytest.raises(AttributeError):
            fm.stop_first_on_ambiguity = False  # type: ignore[misc]

    def test_version_hash_deterministic(self):
        h1 = FillModelConfig().version_hash()
        h2 = FillModelConfig().version_hash()
        assert h1 == h2
        assert len(h1) == 16

    def test_version_hash_changes_on_config_change(self):
        h_default = FillModelConfig().version_hash()
        h_altered = FillModelConfig(stop_first_on_ambiguity=False).version_hash()
        assert h_default != h_altered


class TestSpreadModel:
    """Spread model config: defaults, immutability, versioning."""

    def test_defaults(self):
        sm = SpreadModelConfig()
        assert sm.pair == "EURUSD"
        assert sm.avg_spread_pips == 1.0
        assert sm.use_session_spreads is False

    def test_frozen_cannot_mutate(self):
        with pytest.raises(AttributeError):
            DEFAULT_SPREAD_MODEL.avg_spread_pips = 999.0  # type: ignore[misc]

    def test_version_hash_deterministic(self):
        assert SpreadModelConfig().version_hash() == SpreadModelConfig().version_hash()

    def test_version_hash_changes_on_config_change(self):
        h1 = SpreadModelConfig().version_hash()
        h2 = SpreadModelConfig(avg_spread_pips=2.0).version_hash()
        assert h1 != h2


class TestSlippageModel:
    """Slippage model config: defaults, immutability, versioning."""

    def test_defaults(self):
        sm = SlippageModelConfig()
        assert sm.slippage_pips == 0.5
        assert sm.stress_slippage_pips == 1.5

    def test_frozen_cannot_mutate(self):
        with pytest.raises(AttributeError):
            DEFAULT_SLIPPAGE_MODEL.slippage_pips = 0.0  # type: ignore[misc]

    def test_version_hash_deterministic(self):
        assert SlippageModelConfig().version_hash() == SlippageModelConfig().version_hash()

    def test_version_hash_changes_on_config_change(self):
        h1 = SlippageModelConfig().version_hash()
        h2 = SlippageModelConfig(slippage_pips=1.0).version_hash()
        assert h1 != h2


class TestCommissionModel:
    """Commission model config: defaults, immutability, versioning."""

    def test_defaults_oanda_zero_commission(self):
        cm = CommissionModelConfig()
        assert cm.commission_per_lot_usd == 0.0
        assert cm.commission_per_side is False

    def test_frozen_cannot_mutate(self):
        with pytest.raises(AttributeError):
            DEFAULT_COMMISSION_MODEL.commission_per_lot_usd = 7.0  # type: ignore[misc]

    def test_version_hash_deterministic(self):
        assert CommissionModelConfig().version_hash() == CommissionModelConfig().version_hash()

    def test_version_hash_changes_on_config_change(self):
        h1 = CommissionModelConfig().version_hash()
        h2 = CommissionModelConfig(commission_per_lot_usd=7.0).version_hash()
        assert h1 != h2


class TestSessionCalendar:
    """Session calendar: session lookup, weekend detection, versioning."""

    def test_get_session_london(self):
        cal = SessionCalendarConfig()
        assert cal.get_session(10) == Session.LONDON

    def test_get_session_new_york(self):
        cal = SessionCalendarConfig()
        assert cal.get_session(18) == Session.NEW_YORK

    def test_get_session_asian(self):
        cal = SessionCalendarConfig()
        assert cal.get_session(3) == Session.ASIAN

    def test_get_session_overlap(self):
        cal = SessionCalendarConfig()
        # Hour 14 is both London (8-16) and NY (13-21) -> overlap
        assert cal.get_session(14) == Session.OVERLAP_LON_NY

    def test_get_session_none_outside_all(self):
        cal = SessionCalendarConfig()
        # Hour 22 is past NY close (21) and before Asian open (0)
        assert cal.get_session(22) is None

    def test_is_weekend_friday_night(self):
        # Friday 2024-01-05 22:00 UTC (after 21:00 weekend start)
        dt = datetime(2024, 1, 5, 22, 0, tzinfo=timezone.utc)
        assert DEFAULT_SESSION_CALENDAR.is_weekend(dt.timestamp()) is True

    def test_is_weekend_saturday(self):
        # Saturday 2024-01-06 12:00 UTC
        dt = datetime(2024, 1, 6, 12, 0, tzinfo=timezone.utc)
        assert DEFAULT_SESSION_CALENDAR.is_weekend(dt.timestamp()) is True

    def test_is_weekend_sunday_before_open(self):
        # Sunday 2024-01-07 20:00 UTC (before 22:00 open)
        dt = datetime(2024, 1, 7, 20, 0, tzinfo=timezone.utc)
        assert DEFAULT_SESSION_CALENDAR.is_weekend(dt.timestamp()) is True

    def test_is_not_weekend_sunday_after_open(self):
        # Sunday 2024-01-07 23:00 UTC (after 22:00 open)
        dt = datetime(2024, 1, 7, 23, 0, tzinfo=timezone.utc)
        assert DEFAULT_SESSION_CALENDAR.is_weekend(dt.timestamp()) is False

    def test_is_not_weekend_wednesday(self):
        # Wednesday 2024-01-03 15:00 UTC
        dt = datetime(2024, 1, 3, 15, 0, tzinfo=timezone.utc)
        assert DEFAULT_SESSION_CALENDAR.is_weekend(dt.timestamp()) is False

    def test_frozen_cannot_mutate(self):
        with pytest.raises(AttributeError):
            DEFAULT_SESSION_CALENDAR.london_open_utc = 9  # type: ignore[misc]

    def test_version_hash_deterministic(self):
        assert SessionCalendarConfig().version_hash() == SessionCalendarConfig().version_hash()


class TestIntrabarPolicy:
    """Intrabar policy: conservative defaults, immutability, versioning."""

    def test_defaults_are_conservative(self):
        ip = IntrabarPolicyConfig()
        assert ip.same_bar_sl_tp == AmbiguityResolution.STOP_FIRST
        assert ip.gap_fill_policy == "open_price"
        assert ip.weekend_gap_policy == "monday_open"

    def test_frozen_cannot_mutate(self):
        with pytest.raises(AttributeError):
            DEFAULT_INTRABAR_POLICY.gap_fill_policy = "stop_price"  # type: ignore[misc]

    def test_version_hash_deterministic(self):
        assert IntrabarPolicyConfig().version_hash() == IntrabarPolicyConfig().version_hash()

    def test_version_hash_changes_on_config_change(self):
        h1 = IntrabarPolicyConfig().version_hash()
        h2 = IntrabarPolicyConfig(same_bar_sl_tp=AmbiguityResolution.TARGET_FIRST).version_hash()
        assert h1 != h2


# ===================================================================
# Gate Tests
# ===================================================================


class TestGateStageA:
    """Stage A validity gates."""

    def test_passes_with_good_metrics(self):
        m = _make_metrics()
        results = evaluate_stage_a(m)
        assert all(r.passed for r in results)

    def test_fails_on_too_few_trades(self):
        m = _make_metrics(total_completed_trades=10)
        results = evaluate_stage_a(m)
        trade_gate = [r for r in results if r.gate == "A.min_trades"][0]
        assert trade_gate.passed is False

    def test_fails_on_insufficient_months(self):
        # Only ~1 month of bars
        m = _make_metrics(total_bars=24 * 22)
        results = evaluate_stage_a(m)
        month_gate = [r for r in results if r.gate == "A.min_active_months"][0]
        assert month_gate.passed is False

    def test_fails_on_month_concentration(self):
        # top_3_trades_pct_of_profit = 60% -> concentration = 0.60 > 0.40
        m = _make_metrics(top_3_trades_pct_of_profit=60.0)
        results = evaluate_stage_a(m)
        conc_gate = [r for r in results if r.gate == "A.max_month_pnl_concentration"][0]
        assert conc_gate.passed is False

    def test_fails_on_crash_drawdown(self):
        m = _make_metrics(max_drawdown_pct=55.0)
        results = evaluate_stage_a(m)
        crash_gate = [r for r in results if r.gate == "A.no_crash"][0]
        assert crash_gate.passed is False

    def test_lookahead_always_passes(self):
        m = _make_metrics()
        results = evaluate_stage_a(m)
        la_gate = [r for r in results if r.gate == "A.no_lookahead"][0]
        assert la_gate.passed is True

    def test_custom_gate_config(self):
        cfg = GateConfig(min_trades=200)
        m = _make_metrics(total_completed_trades=100)
        results = evaluate_stage_a(m, config=cfg)
        trade_gate = [r for r in results if r.gate == "A.min_trades"][0]
        assert trade_gate.passed is False


class TestGateStageB:
    """Stage B realism gates."""

    def test_passes_with_good_environment(self):
        env = _make_environment()
        results = evaluate_stage_b(env)
        assert all(r.passed for r in results)

    def test_passes_if_spread_equals_min_threshold(self):
        """Spread gate uses >= (consistent with slippage/commission gates)."""
        env = _make_environment(avg_spread_pips=0.0)
        results = evaluate_stage_b(env)
        spread_gate = [r for r in results if r.gate == "B.spread_applied"][0]
        assert spread_gate.passed is True

    def test_fails_if_spread_below_custom_threshold(self):
        """Spread gate fails when below a non-zero custom threshold."""
        from novatrade.evaluation.gates import GateConfig

        env = _make_environment(avg_spread_pips=0.5)
        custom_config = GateConfig(min_avg_spread_pips=1.0)
        results = evaluate_stage_b(env, config=custom_config)
        spread_gate = [r for r in results if r.gate == "B.spread_applied"][0]
        assert spread_gate.passed is False

    def test_fails_if_fill_model_not_conservative(self):
        env = _make_environment(stop_first_on_ambiguity=False)
        results = evaluate_stage_b(env)
        fill_gate = [r for r in results if r.gate == "B.fill_model_conservative"][0]
        assert fill_gate.passed is False

    def test_no_impossible_fills_always_passes(self):
        env = _make_environment()
        results = evaluate_stage_b(env)
        nif_gate = [r for r in results if r.gate == "B.no_impossible_fills"][0]
        assert nif_gate.passed is True

    def test_accepts_object_environment(self):
        env = SimpleNamespace(
            avg_spread_pips=1.0,
            slippage_pips=0.5,
            commission_per_lot_usd=0.0,
            stop_first_on_ambiguity=True,
        )
        results = evaluate_stage_b(env)
        assert all(r.passed for r in results)


class TestGateStageC:
    """Stage C robustness stubs."""

    def test_stub_returns_not_evaluated(self):
        results = evaluate_stage_c_stub()
        assert len(results) == 4
        assert all(not r.passed for r in results)
        assert all("NOT_EVALUATED" in r.reason for r in results)

    def test_stub_gate_names(self):
        results = evaluate_stage_c_stub()
        gate_names = {r.gate for r in results}
        assert "C.monte_carlo" in gate_names
        assert "C.walk_forward" in gate_names
        assert "C.cost_stress" in gate_names
        assert "C.parameter_stability" in gate_names


class TestGateResults:
    """GateResults aggregation."""

    def test_passed_when_all_pass(self):
        gr = GateResults(
            results=[
                GateResult(gate="A", passed=True, reason="ok"),
                GateResult(gate="B", passed=True, reason="ok"),
            ]
        )
        assert gr.passed is True
        assert gr.failed_at is None

    def test_failed_at_returns_first_failure(self):
        gr = GateResults(
            results=[
                GateResult(gate="A", passed=True, reason="ok"),
                GateResult(gate="B", passed=False, reason="bad"),
                GateResult(gate="C", passed=False, reason="bad2"),
            ]
        )
        assert gr.passed is False
        assert gr.failed_at == "B"

    def test_empty_results_does_not_pass(self):
        """Empty GateResults should not count as passed (no gates were evaluated)."""
        gr = GateResults()
        assert gr.passed is False
        assert gr.failed_at is None


# ===================================================================
# Fitness Tests
# ===================================================================


class TestScoutScore:
    """Scout score computation."""

    def test_returns_zero_for_few_trades(self):
        m = _make_metrics(total_completed_trades=29)
        assert compute_scout_score(m) == 0.0

    def test_exactly_30_trades_is_not_zero(self):
        m = _make_metrics(total_completed_trades=30)
        score = compute_scout_score(m)
        assert score > 0.0

    def test_positive_for_good_metrics(self):
        m = _make_metrics()
        score = compute_scout_score(m)
        assert 0.0 < score <= 1.0

    def test_caps_profit_factor_at_3(self):
        m_high = _make_metrics(profit_factor=10.0)
        m_cap = _make_metrics(profit_factor=3.0)
        # Both should have the same PF component since 10 is capped at 3
        # The overall scores should be equal (all other params equal)
        assert compute_scout_score(m_high) == compute_scout_score(m_cap)

    def test_inf_profit_factor_treated_as_3(self):
        m_inf = _make_metrics(profit_factor=float("inf"))
        m_cap = _make_metrics(profit_factor=3.0)
        assert compute_scout_score(m_inf) == compute_scout_score(m_cap)

    def test_penalty_for_consecutive_losses_above_8(self):
        m_ok = _make_metrics(max_consecutive_losses=7)
        m_bad = _make_metrics(max_consecutive_losses=10)
        assert compute_scout_score(m_ok) > compute_scout_score(m_bad)

    def test_penalty_for_high_drawdown(self):
        m_ok = _make_metrics(max_drawdown_pct=5.0)
        m_bad = _make_metrics(max_drawdown_pct=15.0)
        assert compute_scout_score(m_ok) > compute_scout_score(m_bad)

    def test_score_bounded_zero_to_one(self):
        m = _make_metrics(
            profit_factor=10.0,
            expectancy_r=5.0,
            win_rate=1.0,
            net_result_pips=10000.0,
        )
        score = compute_scout_score(m)
        assert 0.0 <= score <= 1.0


class TestPromotionScore:
    """Promotion score computation."""

    def test_empty_oos_scores_returns_zero(self):
        assert compute_promotion_score([], holdout_score=0.5, is_median=0.5, complexity=3) == 0.0

    def test_basic_computation(self):
        oos = [0.6, 0.65, 0.7]
        score = compute_promotion_score(oos, holdout_score=0.6, is_median=0.65, complexity=3)
        assert 0.0 < score <= 1.0

    def test_holdout_bonus_applied(self):
        oos = [0.6, 0.6, 0.6]
        # holdout >= median * 0.8 -> bonus +0.05
        score_with = compute_promotion_score(oos, holdout_score=0.6, is_median=0.6, complexity=3)
        # holdout far below -> no bonus
        score_without = compute_promotion_score(oos, holdout_score=0.1, is_median=0.6, complexity=3)
        assert score_with > score_without

    def test_complexity_penalty(self):
        oos = [0.6, 0.6, 0.6]
        # complexity=3 -> no penalty (below 5)
        s_simple = compute_promotion_score(oos, holdout_score=0.6, is_median=0.6, complexity=3)
        # complexity=15 -> penalty = (15-5)*0.02 = 0.20
        s_complex = compute_promotion_score(oos, holdout_score=0.6, is_median=0.6, complexity=15)
        assert s_simple > s_complex

    def test_instability_penalty_high_std(self):
        oos_stable = [0.6, 0.61, 0.59]
        oos_unstable = [0.3, 0.9, 0.5, 0.8, 0.2]
        s_stable = compute_promotion_score(oos_stable, holdout_score=0.6, is_median=0.6, complexity=3)
        s_unstable = compute_promotion_score(oos_unstable, holdout_score=0.6, is_median=0.6, complexity=3)
        assert s_stable > s_unstable

    def test_score_clamped_to_zero_one(self):
        # Even with massive penalty, floor is 0.0
        score = compute_promotion_score([0.1], holdout_score=0.0, is_median=0.1, complexity=100)
        assert score == 0.0


class TestFitnessResult:
    """FitnessResult dataclass."""

    def test_defaults(self):
        fr = FitnessResult()
        assert fr.scout_score == 0.0
        assert fr.promotion_score is None
        assert fr.gate_results.passed is False  # empty results = not evaluated = not passed
        assert fr.details == {}


# ===================================================================
# Data Snapshot Tests
# ===================================================================


class TestFingerprint:
    """Candle fingerprinting."""

    def test_deterministic(self):
        candles = _make_candles(10)
        assert fingerprint_candles(candles) == fingerprint_candles(candles)

    def test_changes_when_data_changes(self):
        c1 = _make_candles(10)
        c2 = _make_candles(10)
        # Mutate one candle
        c2[5] = Candle(
            timestamp=c2[5].timestamp,
            open=9.9999,
            high=c2[5].high,
            low=c2[5].low,
            close=c2[5].close,
            volume=c2[5].volume,
            symbol=c2[5].symbol,
            timeframe=c2[5].timeframe,
        )
        assert fingerprint_candles(c1) != fingerprint_candles(c2)

    def test_length_is_16(self):
        fp = fingerprint_candles(_make_candles(5))
        assert len(fp) == 16


class TestDataSnapshot:
    """Save/load round-trip for candle snapshots."""

    def test_save_and_load_roundtrip(self, tmp_path: Path):
        h1 = _make_candles(20, timeframe="H1")
        h4 = _make_candles(5, timeframe="H4", base_ts=1_700_000_000.0)
        snap = save_snapshot(h1, h4, "EURUSD", tmp_path)

        h1_loaded, h4_loaded, meta = load_snapshot(snap.snapshot_id, tmp_path)
        assert len(h1_loaded) == 20
        assert len(h4_loaded) == 5
        assert meta.symbol == "EURUSD"
        assert meta.snapshot_id == snap.snapshot_id

    def test_fingerprint_matches_on_reload(self, tmp_path: Path):
        h1 = _make_candles(10, timeframe="H1")
        h4 = _make_candles(3, timeframe="H4")
        snap = save_snapshot(h1, h4, "EURUSD", tmp_path)
        # load_snapshot verifies fingerprint internally; no exception = pass
        load_snapshot(snap.snapshot_id, tmp_path)

    def test_list_snapshots_finds_saved(self, tmp_path: Path):
        h1 = _make_candles(10, timeframe="H1")
        h4 = _make_candles(3, timeframe="H4")
        save_snapshot(h1, h4, "EURUSD", tmp_path)
        snapshots = list_snapshots(tmp_path)
        assert len(snapshots) == 1
        assert snapshots[0].symbol == "EURUSD"

    def test_list_snapshots_empty_dir(self, tmp_path: Path):
        assert list_snapshots(tmp_path) == []

    def test_list_snapshots_nonexistent_dir(self, tmp_path: Path):
        assert list_snapshots(tmp_path / "nope") == []

    def test_load_snapshot_raises_on_missing(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            load_snapshot("nonexistent_id", tmp_path)

    def test_save_empty_h1_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="h1_candles must not be empty"):
            save_snapshot([], _make_candles(3, timeframe="H4"), "EURUSD", tmp_path)

    def test_save_empty_h4_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="h4_candles must not be empty"):
            save_snapshot(_make_candles(3, timeframe="H1"), [], "EURUSD", tmp_path)


# ===================================================================
# Experiment DB Tests
# ===================================================================


class TestExperimentDB:
    """SQLite experiment storage."""

    @pytest.fixture
    def db(self, tmp_path: Path) -> Iterator[ExperimentDB]:
        edb = ExperimentDB(tmp_path / "test.db")
        yield edb
        edb.close()

    def _make_record(self, eid: str = "exp001", **kw) -> ExperimentRecord:
        defaults = dict(
            experiment_id=eid,
            strategy_config_hash="cfg123",
            doctrine_hash="doc456",
            dataset_hash="dat789",
            engine_sha="abc123def456",
            campaign_id="camp_alpha",
            scout_score=0.75,
            status="valid",
        )
        defaults.update(kw)
        return ExperimentRecord(**defaults)  # type: ignore[arg-type]

    def test_insert_and_get(self, db: ExperimentDB):
        rec = self._make_record()
        db.insert(rec)
        fetched = db.get("exp001")
        assert fetched is not None
        assert fetched.experiment_id == "exp001"
        assert fetched.scout_score == 0.75

    def test_get_nonexistent_returns_none(self, db: ExperimentDB):
        assert db.get("nonexistent") is None

    def test_get_best_returns_highest_scout_score(self, db: ExperimentDB):
        db.insert(self._make_record("e1", scout_score=0.5, campaign_id="c1"))
        db.insert(self._make_record("e2", scout_score=0.9, campaign_id="c1"))
        db.insert(self._make_record("e3", scout_score=0.7, campaign_id="c1"))
        best = db.get_best("c1")
        assert best is not None
        assert best.experiment_id == "e2"

    def test_get_best_invalid_metric_raises(self, db: ExperimentDB):
        with pytest.raises(ValueError):
            db.get_best("c1", metric="invalid_field")

    def test_list_by_campaign(self, db: ExperimentDB):
        db.insert(self._make_record("e1", campaign_id="c1"))
        db.insert(self._make_record("e2", campaign_id="c1"))
        db.insert(self._make_record("e3", campaign_id="c2"))
        results = db.list_by_campaign("c1")
        assert len(results) == 2
        assert all(r.campaign_id == "c1" for r in results)

    def test_count_by_campaign(self, db: ExperimentDB):
        db.insert(self._make_record("e1", campaign_id="c1"))
        db.insert(self._make_record("e2", campaign_id="c1"))
        db.insert(self._make_record("e3", campaign_id="c2"))
        assert db.count_by_campaign("c1") == 2
        assert db.count_by_campaign("c2") == 1
        assert db.count_by_campaign("c_none") == 0

    def test_count_by_status(self, db: ExperimentDB):
        db.insert(self._make_record("e1", campaign_id="c1", status="valid"))
        db.insert(self._make_record("e2", campaign_id="c1", status="valid"))
        db.insert(self._make_record("e3", campaign_id="c1", status="ranked"))
        counts = db.count_by_status("c1")
        assert counts["valid"] == 2
        assert counts["ranked"] == 1

    def test_export_tsv(self, db: ExperimentDB, tmp_path: Path):
        db.insert(self._make_record("e1", campaign_id="c1", scout_score=0.8))
        db.insert(self._make_record("e2", campaign_id="c1", scout_score=0.6))
        tsv_path = tmp_path / "export.tsv"
        count = db.export_tsv("c1", tsv_path)
        assert count == 2
        assert tsv_path.exists()
        lines = tsv_path.read_text().strip().split("\n")
        assert len(lines) == 3  # header + 2 rows
        assert "experiment_id" in lines[0]
        # Verify tab-separated
        assert "\t" in lines[1]

    def test_get_leaderboard_only_ranked_promoted(self, db: ExperimentDB):
        db.insert(self._make_record("e1", campaign_id="c1", status="ranked", scout_score=0.8))
        db.insert(self._make_record("e2", campaign_id="c1", status="promoted", scout_score=0.9))
        db.insert(self._make_record("e3", campaign_id="c1", status="valid", scout_score=0.95))
        board = db.get_leaderboard("c1")
        assert len(board) == 2
        assert board[0].experiment_id == "e2"  # highest scout_score among ranked/promoted
        assert all(r.status in ("ranked", "promoted") for r in board)

    def test_get_leaderboard_no_campaign_filter(self, db: ExperimentDB):
        db.insert(self._make_record("e1", campaign_id="c1", status="ranked", scout_score=0.7))
        db.insert(self._make_record("e2", campaign_id="c2", status="promoted", scout_score=0.8))
        board = db.get_leaderboard()
        assert len(board) == 2

    def test_invalid_status_raises(self):
        with pytest.raises(ValueError, match="Invalid status"):
            ExperimentRecord(
                experiment_id="bad",
                strategy_config_hash="x",
                doctrine_hash="y",
                dataset_hash="z",
                engine_sha="w",
                status="INVALID_STATUS",
            )

    def test_gate_bool_roundtrip(self, db: ExperimentDB):
        rec = self._make_record("e_gates", gate_a_passed=True, gate_b_passed=False, gate_c_passed=None)
        db.insert(rec)
        fetched = db.get("e_gates")
        assert fetched is not None
        assert fetched.gate_a_passed is True
        assert fetched.gate_b_passed is False
        assert fetched.gate_c_passed is None


# ===================================================================
# Experiment Identity Tests
# ===================================================================


class TestExperimentIdentity:
    """Deterministic experiment identity hashing."""

    def test_compute_experiment_id_deterministic(self):
        args = ("cfg1", "dat2", "fill3", "eng4", "doc5")
        assert compute_experiment_id(*args) == compute_experiment_id(*args)

    def test_compute_experiment_id_different_inputs(self):
        id1 = compute_experiment_id("a", "b", "c", "d", "e")
        id2 = compute_experiment_id("a", "b", "c", "d", "f")
        assert id1 != id2

    def test_compute_experiment_id_length(self):
        eid = compute_experiment_id("a", "b", "c", "d", "e")
        assert len(eid) == 16

    def test_compute_config_hash_deterministic(self):
        cfg = {"ema_period": 20, "irb_threshold": 0.45}
        assert compute_config_hash(cfg) == compute_config_hash(cfg)

    def test_compute_config_hash_different_inputs(self):
        h1 = compute_config_hash({"ema_period": 20})
        h2 = compute_config_hash({"ema_period": 30})
        assert h1 != h2

    def test_compute_config_hash_order_independent(self):
        h1 = compute_config_hash({"a": 1, "b": 2})
        h2 = compute_config_hash({"b": 2, "a": 1})
        assert h1 == h2

    def test_get_engine_sha_returns_string(self):
        sha = get_engine_sha()
        assert isinstance(sha, str)
        assert len(sha) > 0


# ===================================================================
# Artifacts Tests
# ===================================================================


class TestArtifacts:
    """Artifact save/load for configs, trades, and logs."""

    def test_save_config_snapshot_creates_file(self, tmp_path: Path):
        cfg = {"ema_period": 20, "irb_threshold": 0.45}
        path = save_config_snapshot(cfg, tmp_path, "exp_001")
        assert path.exists()
        assert path.name == "exp_001.json"

    def test_load_config_snapshot_reads_back(self, tmp_path: Path):
        cfg = {"ema_period": 20, "irb_threshold": 0.45}
        save_config_snapshot(cfg, tmp_path, "exp_002")
        loaded = load_config_snapshot(tmp_path, "exp_002")
        assert loaded == cfg

    def test_load_config_snapshot_missing_returns_none(self, tmp_path: Path):
        assert load_config_snapshot(tmp_path, "nonexistent") is None

    def test_save_trade_log_creates_file(self, tmp_path: Path):
        trades = [
            {"entry_price": 1.10, "exit_price": 1.11, "result_pips": 10.0},
            {"entry_price": 1.12, "exit_price": 1.11, "result_pips": -10.0},
        ]
        path = save_trade_log(trades, tmp_path, "exp_003")
        assert path.exists()
        loaded = json.loads(path.read_text())
        assert len(loaded) == 2

    def test_save_experiment_log_creates_file(self, tmp_path: Path):
        path = save_experiment_log("output here", "errors here", tmp_path, "exp_004")
        assert path.exists()
        content = path.read_text()
        assert "output here" in content
        assert "errors here" in content
        assert "STDOUT" in content
        assert "STDERR" in content


# ===================================================================
# Config Schema Tests
# ===================================================================


class TestParameterBounds:
    """ParameterBounds validation."""

    def test_create_bounds(self):
        b = ParameterBounds(min_val=0.3, max_val=0.6, step=0.01)
        assert b.min_val == 0.3
        assert b.max_val == 0.6
        assert b.step == 0.01

    def test_bounds_frozen(self):
        b = ParameterBounds(min_val=0.3, max_val=0.6)
        with pytest.raises(Exception):  # noqa: B017 — pydantic ValidationError on frozen
            b.min_val = 0.5  # type: ignore[misc]


class TestStrategyConfig:
    """StrategyConfig schema: defaults, validation, serialisation."""

    def test_defaults(self):
        cfg = StrategyConfig()
        assert cfg.irb_threshold == 0.45
        assert cfg.ema_period == 20
        assert cfg.atr_period == 14
        assert cfg.name == "irb_baseline"

    def test_defaults_match_environment_defaults(self):
        """Default StrategyConfig values are the same as BacktestEnvironment defaults."""
        cfg = StrategyConfig()
        env_kw = cfg.to_environment_kwargs()
        assert env_kw["irb_threshold"] == 0.45
        assert env_kw["ema_period"] == 20
        assert env_kw["warmup_bars"] == 34

    def test_bounds_reject_out_of_range(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            StrategyConfig(irb_threshold=0.01)  # below min 0.30

        with pytest.raises(ValidationError):
            StrategyConfig(irb_threshold=0.99)  # above max 0.60

        with pytest.raises(ValidationError):
            StrategyConfig(ema_period=5)  # below min 10

    def test_content_hash_deterministic(self):
        cfg = StrategyConfig()
        assert cfg.content_hash() == cfg.content_hash()
        assert len(cfg.content_hash()) == 16

    def test_content_hash_changes_on_param_change(self):
        h1 = StrategyConfig().content_hash()
        h2 = StrategyConfig(ema_period=30).content_hash()
        assert h1 != h2

    def test_to_yaml_from_yaml_roundtrip(self, tmp_path: Path):
        cfg = StrategyConfig(ema_period=25, irb_threshold=0.50, name="test_strat")
        yaml_path = tmp_path / "config.yaml"
        cfg.to_yaml(yaml_path)
        assert yaml_path.exists()
        loaded = StrategyConfig.from_yaml(yaml_path)
        assert loaded.ema_period == 25
        assert loaded.irb_threshold == 0.50
        assert loaded.name == "test_strat"

    def test_from_yaml_empty_file_gives_defaults(self, tmp_path: Path):
        yaml_path = tmp_path / "empty.yaml"
        yaml_path.write_text("")
        cfg = StrategyConfig.from_yaml(yaml_path)
        assert cfg.ema_period == 20  # default

    def test_from_environment_extracts_correctly(self):
        env = SimpleNamespace(
            irb_threshold=0.50,
            ema_period=30,
            atr_period=14,
            adx_period=14,
            trend_slope_threshold=0.4,
            adx_threshold=20.0,
            overextension_threshold=2.0,
            trail_atr_multiplier=1.5,
            trigger_window_bars=20,
            time_stop_bars=40,
            mtf_lookback=5,
            warmup_bars=34,
            session_filter="london",
            # Non-strategy fields (should be ignored)
            risk_fraction=0.02,
            engine_version="1.0",
        )
        cfg = StrategyConfig.from_environment(env)
        assert cfg.irb_threshold == 0.50
        assert cfg.ema_period == 30
        assert cfg.session_filter == "london"

    def test_optimizable_params_excludes_risk_fraction(self):
        assert "risk_fraction" not in StrategyConfig.OPTIMIZABLE_PARAMS

    def test_optimizable_params_are_all_in_bounds(self):
        for param in StrategyConfig.OPTIMIZABLE_PARAMS:
            assert param in StrategyConfig.PARAMETER_BOUNDS

    def test_to_environment_kwargs_correct_keys(self):
        cfg = StrategyConfig()
        kw = cfg.to_environment_kwargs()
        # Should contain exactly the optimizable params
        for param in StrategyConfig.OPTIMIZABLE_PARAMS:
            assert param in kw
        # Should NOT contain metadata or filter keys
        assert "name" not in kw
        assert "version" not in kw
        assert "filters_enabled" not in kw

    def test_frozen_config(self):
        cfg = StrategyConfig()
        with pytest.raises(Exception):  # noqa: B017 — pydantic ValidationError on frozen
            cfg.ema_period = 30  # type: ignore[misc]

    def test_content_hash_includes_filters(self):
        h1 = StrategyConfig().content_hash()
        h2 = StrategyConfig(filters_enabled=["london_only"]).content_hash()
        assert h1 != h2
