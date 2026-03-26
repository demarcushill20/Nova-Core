"""Comprehensive tests for the Progress Scoring System (Phase 1 Autonomy)."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from novatrade.autonomy.collectors.base import BaseCollector
from novatrade.autonomy.collectors.performance import PerformanceCollector
from novatrade.autonomy.collectors.pipeline import PipelineCollector
from novatrade.autonomy.collectors.risk_engine import RiskCollector
from novatrade.autonomy.collectors.strategy import StrategyCollector
from novatrade.autonomy.collectors.system_health import SystemHealthCollector
from novatrade.autonomy.progress_scorer import ProgressScorer
from novatrade.autonomy.schemas import (
    AlertLevel,
    DimensionScore,
    ProgressReport,
    ScoreTrend,
    ScoringConfig,
    SubMetric,
)

# =====================================================================
# Schema tests
# =====================================================================


def test_sub_metric_valid():
    m = SubMetric(name="test", value=50.0)
    assert m.name == "test"
    assert m.value == 50.0
    assert m.raw_value is None
    assert m.description == ""


def test_sub_metric_bounds_lower():
    with pytest.raises(ValidationError):
        SubMetric(name="bad", value=-1.0)


def test_sub_metric_bounds_upper():
    with pytest.raises(ValidationError):
        SubMetric(name="bad", value=101.0)


def test_sub_metric_with_raw_value():
    m = SubMetric(name="x", value=75.0, raw_value=3.5, description="test desc")
    assert m.raw_value == 3.5
    assert m.description == "test desc"


def test_dimension_score_alert_green():
    d = DimensionScore(name="test", score=85.0)
    assert d.alert_level == AlertLevel.GREEN


def test_dimension_score_alert_yellow():
    d = DimensionScore(name="test", score=55.0)
    assert d.alert_level == AlertLevel.YELLOW


def test_dimension_score_alert_red():
    d = DimensionScore(name="test", score=20.0)
    assert d.alert_level == AlertLevel.RED


def test_dimension_score_threshold_40():
    d = DimensionScore(name="test", score=40.0)
    assert d.alert_level == AlertLevel.YELLOW


def test_dimension_score_threshold_70():
    d = DimensionScore(name="test", score=70.0)
    assert d.alert_level == AlertLevel.GREEN


def test_dimension_score_threshold_0():
    d = DimensionScore(name="test", score=0.0)
    assert d.alert_level == AlertLevel.RED


def test_dimension_score_threshold_100():
    d = DimensionScore(name="test", score=100.0)
    assert d.alert_level == AlertLevel.GREEN


def test_progress_report_overall_alert_green():
    r = ProgressReport(overall_score=75.0)
    assert r.overall_alert == AlertLevel.GREEN


def test_progress_report_overall_alert_yellow():
    r = ProgressReport(overall_score=50.0)
    assert r.overall_alert == AlertLevel.YELLOW


def test_progress_report_overall_alert_red():
    r = ProgressReport(overall_score=30.0)
    assert r.overall_alert == AlertLevel.RED


def test_scoring_config_default_weights_sum():
    cfg = ScoringConfig()
    total = sum(cfg.weights.values())
    assert abs(total - 1.0) < 1e-9


def test_scoring_config_default_retention():
    """Alert thresholds are fixed at 40/70 in model validators, not in config."""
    cfg = ScoringConfig()
    assert cfg.history_retention_days == 7
    # Verify thresholds are NOT configurable attributes
    assert not hasattr(cfg, "red_threshold")
    assert not hasattr(cfg, "yellow_threshold")


def test_scoring_config_custom_weights():
    cfg = ScoringConfig(weights={"a": 0.5, "b": 0.5})
    assert cfg.weights == {"a": 0.5, "b": 0.5}


def test_score_trend_defaults():
    t = ScoreTrend(current=60.0)
    assert t.direction == "stable"
    assert t.avg_1h is None


# =====================================================================
# BaseCollector tests
# =====================================================================


def test_base_collector_safe_score_clamps_low():
    class Dummy(BaseCollector):
        async def collect(self):
            return DimensionScore(name="d", score=0.0)

    c = Dummy()
    assert c._safe_score(-10) == 0.0


def test_base_collector_safe_score_clamps_high():
    class Dummy(BaseCollector):
        async def collect(self):
            return DimensionScore(name="d", score=0.0)

    c = Dummy()
    assert c._safe_score(200) == 100.0


def test_base_collector_safe_score_passthrough():
    class Dummy(BaseCollector):
        async def collect(self):
            return DimensionScore(name="d", score=0.0)

    c = Dummy()
    assert c._safe_score(42.5) == 42.5


# =====================================================================
# SystemHealthCollector tests
# =====================================================================


@pytest.fixture
def sys_collector(tmp_path):
    return SystemHealthCollector(str(tmp_path))


@pytest.mark.asyncio
async def test_sys_health_all_services_up(sys_collector):
    """All 4 services active = score 100."""

    async def mock_exec(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"active\n", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        result = await sys_collector.collect()

    svc = next(m for m in result.sub_metrics if m.name == "service_status")
    assert svc.value == 100.0


@pytest.mark.asyncio
async def test_sys_health_two_services_up(sys_collector):
    """2 of 4 services active = score 50."""
    call_count = 0

    async def mock_exec(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        proc = MagicMock()
        if call_count <= 2:
            proc.communicate = AsyncMock(return_value=(b"active\n", b""))
        else:
            proc.communicate = AsyncMock(return_value=(b"inactive\n", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        result = await sys_collector.collect()

    svc = next(m for m in result.sub_metrics if m.name == "service_status")
    assert svc.value == 50.0


@pytest.mark.asyncio
async def test_sys_health_all_services_down(sys_collector):
    """No services active = score 0."""

    async def mock_exec(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"inactive\n", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        result = await sys_collector.collect()

    svc = next(m for m in result.sub_metrics if m.name == "service_status")
    assert svc.value == 0.0


@pytest.mark.asyncio
async def test_sys_health_no_orphaned_tasks(sys_collector, tmp_path):
    """No stale tasks = score 100."""
    tasks_dir = tmp_path / "TASKS"
    tasks_dir.mkdir()
    # Create a recent task file
    f = tasks_dir / "001_test.md"
    f.write_text("task content")

    async def mock_exec(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"inactive\n", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        result = await sys_collector.collect()

    ot = next(m for m in result.sub_metrics if m.name == "orphaned_tasks")
    assert ot.value == 100.0


@pytest.mark.asyncio
async def test_sys_health_stale_tasks(sys_collector, tmp_path):
    """3 stale tasks = 100 - 60 = 40."""
    tasks_dir = tmp_path / "TASKS"
    tasks_dir.mkdir()
    old_time = time.time() - 4 * 3600  # 4 hours ago
    for i in range(3):
        f = tasks_dir / f"task_{i}.md"
        f.write_text("content")
        import os

        os.utime(f, (old_time, old_time))

    async def mock_exec(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"inactive\n", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        result = await sys_collector.collect()

    ot = next(m for m in result.sub_metrics if m.name == "orphaned_tasks")
    assert ot.value == 40.0


@pytest.mark.asyncio
async def test_sys_health_done_tasks_ignored(sys_collector, tmp_path):
    """Tasks ending in .done should not count as orphaned."""
    tasks_dir = tmp_path / "TASKS"
    tasks_dir.mkdir()
    old_time = time.time() - 4 * 3600
    f = tasks_dir / "task.md.done"
    f.write_text("done task")
    import os

    os.utime(f, (old_time, old_time))

    async def mock_exec(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"inactive\n", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        result = await sys_collector.collect()

    ot = next(m for m in result.sub_metrics if m.name == "orphaned_tasks")
    assert ot.value == 100.0


@pytest.mark.asyncio
async def test_sys_health_heartbeat_fresh(sys_collector, tmp_path):
    """Heartbeat updated within the last hour = 100."""
    hb = tmp_path / "HEARTBEAT.md"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    hb.write_text(f"# Heartbeat\nLast update: {ts}\n")

    async def mock_exec(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"inactive\n", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        result = await sys_collector.collect()

    up = next(m for m in result.sub_metrics if m.name == "uptime_hours")
    assert up.value == 100.0


@pytest.mark.asyncio
async def test_sys_health_heartbeat_missing(sys_collector):
    """No HEARTBEAT.md = 0."""

    async def mock_exec(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"inactive\n", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        result = await sys_collector.collect()

    up = next(m for m in result.sub_metrics if m.name == "uptime_hours")
    assert up.value == 0.0


@pytest.mark.asyncio
async def test_sys_health_error_rate_clean(sys_collector, tmp_path):
    """No ERROR lines = 100."""
    logs_dir = tmp_path / "LOGS"
    logs_dir.mkdir()
    log_f = logs_dir / "test.log"
    log_f.write_text("INFO all good\nINFO still good\n")

    async def mock_exec(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"inactive\n", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        result = await sys_collector.collect()

    er = next(m for m in result.sub_metrics if m.name == "error_rate")
    assert er.value == 100.0


@pytest.mark.asyncio
async def test_sys_health_error_rate_high(sys_collector, tmp_path):
    """5 ERROR lines = 100 - 25 = 75."""
    logs_dir = tmp_path / "LOGS"
    logs_dir.mkdir()
    log_f = logs_dir / "test.log"
    lines = ["ERROR something\n" for _ in range(5)]
    log_f.write_text("".join(lines))

    async def mock_exec(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"inactive\n", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        result = await sys_collector.collect()

    er = next(m for m in result.sub_metrics if m.name == "error_rate")
    assert er.value == 75.0


# =====================================================================
# PipelineCollector tests
# =====================================================================


@pytest.fixture
def pipe_collector(tmp_path):
    return PipelineCollector(str(tmp_path))


@pytest.mark.asyncio
async def test_pipeline_healthy_metrics(pipe_collector, tmp_path):
    """High success rate = high connectivity score."""
    state_dir = tmp_path / "STATE"
    state_dir.mkdir()
    metrics = {
        "contract_success": {"_total": 100},
        "contract_failure": {"_total": 5},
    }
    (state_dir / "metrics.json").write_text(json.dumps(metrics))

    # Create OUTPUT dir with recent file
    out_dir = tmp_path / "OUTPUT"
    out_dir.mkdir()
    (out_dir / "signal.json").write_text("{}")

    # Create STATE/novatrade with recent files
    nt_dir = state_dir / "novatrade"
    nt_dir.mkdir()
    (nt_dir / "feed.json").write_text("{}")

    result = await pipe_collector.collect()
    conn = next(m for m in result.sub_metrics if m.name == "pipeline_connectivity")
    # 100 / 105 ~= 95.2
    assert conn.value > 90.0


@pytest.mark.asyncio
async def test_pipeline_no_metrics_file(pipe_collector, tmp_path):
    """No metrics.json = neutral scores."""
    result = await pipe_collector.collect()
    conn = next(m for m in result.sub_metrics if m.name == "pipeline_connectivity")
    assert conn.value == 50.0  # neutral when no data


@pytest.mark.asyncio
async def test_pipeline_high_rejection(pipe_collector, tmp_path):
    """High retry rate = low rejection score."""
    state_dir = tmp_path / "STATE"
    state_dir.mkdir()
    metrics = {
        "contract_success": {"_total": 10},
        "contract_failure": {"_total": 10},
        "retry_issued": {"_total": 15},
    }
    (state_dir / "metrics.json").write_text(json.dumps(metrics))

    result = await pipe_collector.collect()
    rej = next(m for m in result.sub_metrics if m.name == "rejection_rate")
    # rejection_pct = 15/20 * 100 = 75, score = 100-75 = 25
    assert rej.value == 25.0


@pytest.mark.asyncio
async def test_pipeline_degraded_feed(pipe_collector, tmp_path):
    """Stale state dir = low feed health."""
    state_dir = tmp_path / "STATE"
    state_dir.mkdir()
    nt_dir = state_dir / "novatrade"
    nt_dir.mkdir()
    f = nt_dir / "old_feed.json"
    f.write_text("{}")
    import os

    old_time = time.time() - 8 * 3600  # 8 hours ago
    os.utime(f, (old_time, old_time))

    result = await pipe_collector.collect()
    feed = next(m for m in result.sub_metrics if m.name == "feed_health")
    assert feed.value == 0.0  # stale > 6h


# =====================================================================
# StrategyCollector tests
# =====================================================================


@pytest.fixture
def strat_collector(tmp_path):
    return StrategyCollector(str(tmp_path))


@pytest.mark.asyncio
async def test_strategy_active_trading(strat_collector, tmp_path):
    """5+ trades in 24h = score 100."""
    state_dir = tmp_path / "STATE" / "novatrade"
    state_dir.mkdir(parents=True)
    trades = [{"timestamp": time.time() - i * 3600} for i in range(6)]
    (state_dir / "trade_log.json").write_text(json.dumps(trades))

    result = await strat_collector.collect()
    tc = next(m for m in result.sub_metrics if m.name == "trades_last_24h")
    assert tc.value == 100.0


@pytest.mark.asyncio
async def test_strategy_no_trades(strat_collector, tmp_path):
    """0 trades = 20 (not critical — may be market conditions)."""
    state_dir = tmp_path / "STATE" / "novatrade"
    state_dir.mkdir(parents=True)
    (state_dir / "trade_log.json").write_text("[]")

    result = await strat_collector.collect()
    tc = next(m for m in result.sub_metrics if m.name == "trades_last_24h")
    assert tc.value == 20.0


@pytest.mark.asyncio
async def test_strategy_no_trade_log(strat_collector):
    """Missing trade log = 0 trades = 20."""
    result = await strat_collector.collect()
    tc = next(m for m in result.sub_metrics if m.name == "trades_last_24h")
    assert tc.value == 20.0


@pytest.mark.asyncio
async def test_strategy_silent_failure_weekend(strat_collector):
    """Weekend = no silent failure expected = 100."""
    # Mock weekend
    weekend = datetime(2026, 3, 28, 12, 0, 0, tzinfo=timezone.utc)  # Saturday
    with patch("novatrade.autonomy.collectors.strategy.datetime") as mock_dt:
        mock_dt.now.return_value = weekend
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = await strat_collector.collect()

    sf = next(m for m in result.sub_metrics if m.name == "silent_failure_detected")
    assert sf.value == 100.0


@pytest.mark.asyncio
async def test_strategy_backtest_no_data(strat_collector):
    """No backtest dir = placeholder 50."""
    result = await strat_collector.collect()
    bt = next(m for m in result.sub_metrics if m.name == "backtest_live_alignment")
    assert bt.value == 50.0


# =====================================================================
# RiskCollector tests
# =====================================================================


@pytest.fixture
def risk_collector(tmp_path):
    return RiskCollector(str(tmp_path))


@pytest.mark.asyncio
async def test_risk_all_present(risk_collector, tmp_path):
    """All risk state files present and valid."""
    state_dir = tmp_path / "STATE" / "novatrade"
    state_dir.mkdir(parents=True)
    (state_dir / "daily_loss_tracker.json").write_text('{"max": 500}')
    (state_dir / "lot_history.json").write_text("[]")
    (state_dir / "trading_days.json").write_text("[]")
    (state_dir / "request_counter.json").write_text("{}")

    result = await risk_collector.collect()
    assert result.score >= 70.0


@pytest.mark.asyncio
async def test_risk_missing_state_dir(risk_collector):
    """No STATE/novatrade = all 0."""
    result = await risk_collector.collect()
    dd = next(m for m in result.sub_metrics if m.name == "drawdown_enforcement")
    assert dd.value == 0.0


@pytest.mark.asyncio
async def test_risk_halt_valid_json(risk_collector, tmp_path):
    """Valid JSON state files = 100."""
    state_dir = tmp_path / "STATE" / "novatrade"
    state_dir.mkdir(parents=True)
    (state_dir / "daily_loss_tracker.json").write_text('{"limit": 500}')
    (state_dir / "lot_history.json").write_text("[]")

    result = await risk_collector.collect()
    halt = next(m for m in result.sub_metrics if m.name == "halt_state_persistence")
    assert halt.value == 100.0


@pytest.mark.asyncio
async def test_risk_capital_with_lot_history(risk_collector, tmp_path):
    """lot_history.json exists = 100."""
    state_dir = tmp_path / "STATE" / "novatrade"
    state_dir.mkdir(parents=True)
    (state_dir / "lot_history.json").write_text("[]")

    result = await risk_collector.collect()
    cap = next(m for m in result.sub_metrics if m.name == "capital_allocation")
    assert cap.value == 100.0


@pytest.mark.asyncio
async def test_risk_gate_no_logs(risk_collector):
    """No LOGS dir = neutral 50."""
    result = await risk_collector.collect()
    gate = next(m for m in result.sub_metrics if m.name == "risk_gate_pass_rate")
    assert gate.value == 50.0


# =====================================================================
# PerformanceCollector tests
# =====================================================================


@pytest.fixture
def perf_collector(tmp_path):
    return PerformanceCollector(str(tmp_path))


@pytest.mark.asyncio
async def test_perf_no_data(perf_collector):
    """No equity history = all placeholders (50)."""
    result = await perf_collector.collect()
    sharpe = next(m for m in result.sub_metrics if m.name == "sharpe_ratio_30d")
    assert sharpe.value == 50.0


@pytest.mark.asyncio
async def test_perf_good_equity(perf_collector, tmp_path):
    """Steadily rising equity = good Sharpe, low drawdown."""
    state_dir = tmp_path / "STATE" / "novatrade"
    state_dir.mkdir(parents=True)
    # Steadily increasing equity
    data = [{"equity": 10000 + i * 50} for i in range(30)]
    (state_dir / "equity_history.json").write_text(json.dumps(data))

    result = await perf_collector.collect()
    sharpe = next(m for m in result.sub_metrics if m.name == "sharpe_ratio_30d")
    assert sharpe.value >= 50.0

    dd = next(m for m in result.sub_metrics if m.name == "max_drawdown_30d")
    assert dd.value == 100.0  # no drawdown in monotonic increase


@pytest.mark.asyncio
async def test_perf_bad_equity(perf_collector, tmp_path):
    """Declining equity = bad scores."""
    state_dir = tmp_path / "STATE" / "novatrade"
    state_dir.mkdir(parents=True)
    data = [{"equity": 10000 - i * 200} for i in range(30)]
    (state_dir / "equity_history.json").write_text(json.dumps(data))

    result = await perf_collector.collect()
    sharpe = next(m for m in result.sub_metrics if m.name == "sharpe_ratio_30d")
    assert sharpe.value <= 50.0  # negative Sharpe = 20


@pytest.mark.asyncio
async def test_perf_drawdown_above_ftmo(perf_collector, tmp_path):
    """Drawdown > 5% = score 0."""
    state_dir = tmp_path / "STATE" / "novatrade"
    state_dir.mkdir(parents=True)
    # Peak at 10000, then drop to 9000 = 10% dd
    data = [
        {"equity": 10000},
        {"equity": 10000},
        {"equity": 9000},
    ]
    (state_dir / "equity_history.json").write_text(json.dumps(data))

    result = await perf_collector.collect()
    dd = next(m for m in result.sub_metrics if m.name == "max_drawdown_30d")
    assert dd.value == 0.0


@pytest.mark.asyncio
async def test_perf_win_rate_stable(perf_collector, tmp_path):
    """Consistent wins = high stability score."""
    state_dir = tmp_path / "STATE" / "novatrade"
    state_dir.mkdir(parents=True)
    # Alternating +10 / -5 pattern = consistent win rate
    data = []
    val = 10000
    for i in range(20):
        val += 10 if i % 3 != 2 else -5
        data.append({"equity": val})
    (state_dir / "equity_history.json").write_text(json.dumps(data))

    result = await perf_collector.collect()
    wr = next(m for m in result.sub_metrics if m.name == "win_rate_stability")
    assert wr.value >= 50.0


# =====================================================================
# ProgressScorer engine tests
# =====================================================================


@pytest.fixture
def scorer(tmp_path):
    return ProgressScorer(base_path=str(tmp_path))


def _make_dim(name: str, score: float) -> DimensionScore:
    return DimensionScore(name=name, score=score)


@pytest.mark.asyncio
async def test_scorer_all_perfect(scorer):
    """All collectors return 100 -> overall 100."""

    async def perfect_collect():
        return DimensionScore(name="test", score=100.0)

    for _name, coll in scorer._collectors.items():
        coll.collect = perfect_collect

    report = await scorer.score()
    assert report.overall_score == 100.0
    assert report.overall_alert == AlertLevel.GREEN


@pytest.mark.asyncio
async def test_scorer_all_zero(scorer):
    """All collectors return 0 -> overall 0."""

    async def zero_collect():
        return DimensionScore(name="test", score=0.0)

    for _name, coll in scorer._collectors.items():
        coll.collect = zero_collect

    report = await scorer.score()
    assert report.overall_score == 0.0
    assert report.overall_alert == AlertLevel.RED


@pytest.mark.asyncio
async def test_scorer_mixed_scores(scorer):
    """Mixed scores with default weights."""
    scores = {
        "system_health": 80.0,
        "execution_pipeline": 60.0,
        "strategy_validity": 40.0,
        "risk_engine": 90.0,
        "performance_stability": 50.0,
    }

    for name, coll in scorer._collectors.items():
        s = scores[name]

        async def make_collect(score=s):
            return DimensionScore(name="test", score=score)

        coll.collect = make_collect

    report = await scorer.score()
    # Weighted: 80*0.2 + 60*0.25 + 40*0.25 + 90*0.2 + 50*0.1 = 16+15+10+18+5 = 64
    assert abs(report.overall_score - 64.0) < 0.5


@pytest.mark.asyncio
async def test_scorer_custom_weights(tmp_path):
    """Custom weights change the overall score."""
    config = ScoringConfig(
        weights={
            "system_health": 1.0,
            "execution_pipeline": 0.0,
            "strategy_validity": 0.0,
            "risk_engine": 0.0,
            "performance_stability": 0.0,
        }
    )
    scorer = ProgressScorer(config=config, base_path=str(tmp_path))

    scores = {
        "system_health": 90.0,
        "execution_pipeline": 10.0,
        "strategy_validity": 10.0,
        "risk_engine": 10.0,
        "performance_stability": 10.0,
    }

    for name, coll in scorer._collectors.items():
        s = scores[name]

        async def make_collect(score=s):
            return DimensionScore(name="test", score=score)

        coll.collect = make_collect

    report = await scorer.score()
    # Only system_health matters (weight 1.0), rest are 0
    assert report.overall_score == 90.0


@pytest.mark.asyncio
async def test_scorer_one_collector_fails(scorer):
    """One collector raises -> graceful degradation with warning."""

    async def failing_collect():
        raise RuntimeError("connection lost")

    async def ok_collect():
        return DimensionScore(name="test", score=80.0)

    first = True
    for _name, coll in scorer._collectors.items():
        if first:
            coll.collect = failing_collect
            first = False
        else:
            coll.collect = ok_collect

    report = await scorer.score()
    assert len(report.warnings) == 1
    assert "failed" in report.warnings[0].lower()
    assert report.overall_score > 0  # other collectors contributed


@pytest.mark.asyncio
async def test_scorer_all_collectors_fail(scorer):
    """All collectors fail -> report still generated with all 0."""

    async def fail():
        raise RuntimeError("boom")

    for coll in scorer._collectors.values():
        coll.collect = fail

    report = await scorer.score()
    assert report.overall_score == 0.0
    assert len(report.warnings) == 5


@pytest.mark.asyncio
async def test_scorer_has_all_dimensions(scorer):
    """Report contains all 5 expected dimensions."""

    async def stub():
        return DimensionScore(name="test", score=50.0)

    for coll in scorer._collectors.values():
        coll.collect = stub

    report = await scorer.score()
    assert set(report.dimensions.keys()) == {
        "system_health",
        "execution_pipeline",
        "strategy_validity",
        "risk_engine",
        "performance_stability",
    }


@pytest.mark.asyncio
async def test_scorer_persists_history(scorer, tmp_path):
    """Score run persists to history file."""

    async def stub():
        return DimensionScore(name="test", score=50.0)

    for coll in scorer._collectors.values():
        coll.collect = stub

    await scorer.score()

    history_path = tmp_path / "STATE" / "progress_history.json"
    assert history_path.exists()
    data = json.loads(history_path.read_text())
    assert len(data) == 1
    assert data[0]["overall_score"] == 50.0


# =====================================================================
# Trend tests
# =====================================================================


@pytest.mark.asyncio
async def test_trend_no_history(scorer):
    """No prior history -> 'stable' direction."""

    async def stub():
        return DimensionScore(name="test", score=60.0)

    for coll in scorer._collectors.values():
        coll.collect = stub

    report = await scorer.score()
    for trend in report.trends.values():
        assert trend.direction == "stable"


@pytest.mark.asyncio
async def test_trend_improving(scorer, tmp_path):
    """Current much higher than 6h avg -> 'improving'."""
    # Seed history with low scores
    state_dir = tmp_path / "STATE"
    state_dir.mkdir(parents=True)
    past = datetime.now(timezone.utc) - timedelta(hours=3)
    history_entry = {
        "overall_score": 30.0,
        "generated_at": past.isoformat(),
        "dimensions": {
            "system_health": {"score": 30.0},
            "execution_pipeline": {"score": 30.0},
            "strategy_validity": {"score": 30.0},
            "risk_engine": {"score": 30.0},
            "performance_stability": {"score": 30.0},
        },
    }
    (state_dir / "progress_history.json").write_text(json.dumps([history_entry]))

    async def high():
        return DimensionScore(name="test", score=80.0)

    for coll in scorer._collectors.values():
        coll.collect = high

    report = await scorer.score()
    for _name, trend in report.trends.items():
        assert trend.direction == "improving"


@pytest.mark.asyncio
async def test_trend_degrading(scorer, tmp_path):
    """Current much lower than 6h avg -> 'degrading'."""
    state_dir = tmp_path / "STATE"
    state_dir.mkdir(parents=True)
    past = datetime.now(timezone.utc) - timedelta(hours=3)
    history_entry = {
        "overall_score": 90.0,
        "generated_at": past.isoformat(),
        "dimensions": {
            "system_health": {"score": 90.0},
            "execution_pipeline": {"score": 90.0},
            "strategy_validity": {"score": 90.0},
            "risk_engine": {"score": 90.0},
            "performance_stability": {"score": 90.0},
        },
    }
    (state_dir / "progress_history.json").write_text(json.dumps([history_entry]))

    async def low():
        return DimensionScore(name="test", score=30.0)

    for coll in scorer._collectors.values():
        coll.collect = low

    report = await scorer.score()
    for _name, trend in report.trends.items():
        assert trend.direction == "degrading"


@pytest.mark.asyncio
async def test_trend_stable(scorer, tmp_path):
    """Current ~= 6h avg -> 'stable'."""
    state_dir = tmp_path / "STATE"
    state_dir.mkdir(parents=True)
    past = datetime.now(timezone.utc) - timedelta(hours=3)
    history_entry = {
        "overall_score": 60.0,
        "generated_at": past.isoformat(),
        "dimensions": {
            "system_health": {"score": 60.0},
            "execution_pipeline": {"score": 60.0},
            "strategy_validity": {"score": 60.0},
            "risk_engine": {"score": 60.0},
            "performance_stability": {"score": 60.0},
        },
    }
    (state_dir / "progress_history.json").write_text(json.dumps([history_entry]))

    async def same():
        return DimensionScore(name="test", score=62.0)

    for coll in scorer._collectors.values():
        coll.collect = same

    report = await scorer.score()
    for _name, trend in report.trends.items():
        assert trend.direction == "stable"


@pytest.mark.asyncio
async def test_history_pruning(scorer, tmp_path):
    """Old entries are pruned from history."""
    state_dir = tmp_path / "STATE"
    state_dir.mkdir(parents=True)
    old_entry = {
        "overall_score": 50.0,
        "generated_at": (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
        "dimensions": {},
    }
    recent_entry = {
        "overall_score": 60.0,
        "generated_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        "dimensions": {},
    }
    (state_dir / "progress_history.json").write_text(json.dumps([old_entry, recent_entry]))

    async def stub():
        return DimensionScore(name="test", score=70.0)

    for coll in scorer._collectors.values():
        coll.collect = stub

    await scorer.score()

    history = json.loads((state_dir / "progress_history.json").read_text())
    # Old entry should be pruned (10 days > 7 day retention)
    # Should have recent_entry + new entry = 2
    assert len(history) == 2
    for h in history:
        ts_str = h["generated_at"].replace("Z", "+00:00")
        ts = datetime.fromisoformat(ts_str)
        # Strip tzinfo for comparison — entries may have tz or not
        ts_naive = ts.replace(tzinfo=None)
        assert ts_naive > datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=8)


# =====================================================================
# Integration tests
# =====================================================================


@pytest.mark.asyncio
async def test_full_score_cycle(tmp_path):
    """Full score -> persist -> reload -> compare cycle."""
    scorer = ProgressScorer(base_path=str(tmp_path))

    async def stub():
        return DimensionScore(name="test", score=65.0)

    for coll in scorer._collectors.values():
        coll.collect = stub

    report1 = await scorer.score()
    assert report1.overall_score == 65.0
    assert report1.overall_alert == AlertLevel.YELLOW

    # Score again
    report2 = await scorer.score()
    assert report2.overall_score == 65.0

    # History should have 2 entries
    history_path = tmp_path / "STATE" / "progress_history.json"
    history = json.loads(history_path.read_text())
    assert len(history) == 2


@pytest.mark.asyncio
async def test_score_serialization_roundtrip(tmp_path):
    """Report can be serialized and deserialized correctly."""
    scorer = ProgressScorer(base_path=str(tmp_path))

    async def stub():
        return DimensionScore(
            name="test",
            score=72.0,
            sub_metrics=[SubMetric(name="m1", value=80.0)],
            warnings=["test warning"],
        )

    for coll in scorer._collectors.values():
        coll.collect = stub

    report = await scorer.score()
    json_str = report.model_dump_json()
    restored = ProgressReport.model_validate_json(json_str)

    assert restored.overall_score == report.overall_score
    assert len(restored.dimensions) == len(report.dimensions)
    assert len(restored.warnings) == len(report.warnings)


@pytest.mark.asyncio
async def test_concurrent_score_safety(tmp_path):
    """Multiple concurrent score() calls don't corrupt state."""
    scorer = ProgressScorer(base_path=str(tmp_path))

    async def stub():
        return DimensionScore(name="test", score=50.0)

    for coll in scorer._collectors.values():
        coll.collect = stub

    # Run 3 score calls concurrently
    results = await asyncio.gather(
        scorer.score(),
        scorer.score(),
        scorer.score(),
    )

    assert all(r.overall_score == 50.0 for r in results)

    history_path = tmp_path / "STATE" / "progress_history.json"
    history = json.loads(history_path.read_text())
    # With asyncio lock, all 3 entries must be persisted (no lost writes)
    assert len(history) == 3


# =====================================================================
# H5: Corrupted history recovery tests
# =====================================================================


def test_corrupted_history_returns_empty(tmp_path):
    """Write garbage to history file, verify _load_history returns []."""
    scorer = ProgressScorer(base_path=str(tmp_path))
    state_dir = tmp_path / "STATE"
    state_dir.mkdir(parents=True)
    (state_dir / "progress_history.json").write_text("NOT VALID JSON {{{")

    result = scorer._load_history()
    assert result == []


@pytest.mark.asyncio
async def test_score_recovers_from_corrupted_history(tmp_path):
    """Corrupt history, run score(), verify it produces valid report and new history."""
    scorer = ProgressScorer(base_path=str(tmp_path))
    state_dir = tmp_path / "STATE"
    state_dir.mkdir(parents=True)
    (state_dir / "progress_history.json").write_text("CORRUPT DATA!!!")

    async def stub():
        return DimensionScore(name="test", score=55.0)

    for coll in scorer._collectors.values():
        coll.collect = stub

    report = await scorer.score()
    assert report.overall_score == 55.0
    assert report.overall_alert == AlertLevel.YELLOW

    # History file should be valid JSON now with exactly 1 entry
    history = json.loads((state_dir / "progress_history.json").read_text())
    assert len(history) == 1
    assert history[0]["overall_score"] == 55.0


def test_truncated_json_history(tmp_path):
    """Write partial JSON, verify _load_history returns [] (recovery)."""
    scorer = ProgressScorer(base_path=str(tmp_path))
    state_dir = tmp_path / "STATE"
    state_dir.mkdir(parents=True)
    # Valid JSON start but truncated
    (state_dir / "progress_history.json").write_text('[{"overall_score": 50.0, "gen')

    result = scorer._load_history()
    assert result == []
