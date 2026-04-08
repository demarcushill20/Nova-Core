"""Tests for novatrade.monitor.post_trade_verifier — post-trade strategy replay."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from novatrade.backtest.environment import BacktestEnvironment
from novatrade.models import Candle
from novatrade.monitor.post_trade_verifier import (
    PostTradeVerifier,
    VerificationResult,
    _pip_size,
    _price_delta_pips,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_candles(n: int, base_price: float = 1.1000, symbol: str = "EURUSD") -> list[Candle]:
    """Generate n synthetic H1 candles with a gentle uptrend."""
    candles = []
    t0 = 1_700_000_000.0  # arbitrary start timestamp
    for i in range(n):
        price = base_price + i * 0.0002  # gentle uptrend for EMA slope
        candles.append(
            Candle(
                timestamp=t0 + i * 3600,
                open=price,
                high=price + 0.0010,
                low=price - 0.0010,
                close=price + 0.0001,
                volume=100.0,
                symbol=symbol,
                timeframe="H1",
            )
        )
    return candles


def _make_env(**overrides: object) -> BacktestEnvironment:
    """Create a BacktestEnvironment with test-friendly defaults."""
    kwargs = dict(
        warmup_bars=5,
        ema_period=10,
        atr_period=5,
        adx_period=5,
        trend_slope_threshold=0.01,
        adx_threshold=5.0,
        irb_threshold=0.3,
        session_filter="all",
    )
    kwargs.update(overrides)
    return BacktestEnvironment(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pip helpers
# ---------------------------------------------------------------------------


def test_pip_size_eurusd():
    assert _pip_size("EURUSD") == 0.0001


def test_pip_size_jpy():
    assert _pip_size("USDJPY") == 0.01


def test_price_delta_pips():
    delta = _price_delta_pips(1.1050, 1.1040, "EURUSD")
    assert abs(delta - 10.0) < 0.01


# ---------------------------------------------------------------------------
# Verifier core
# ---------------------------------------------------------------------------


class TestPostTradeVerifier:
    """Test the PostTradeVerifier replay and scoring logic."""

    def test_no_signal_insufficient_candles(self, tmp_path: Path):
        """Replay with too few candles → NO_SIGNAL."""
        env = _make_env(warmup_bars=50)
        verifier = PostTradeVerifier(env, log_path=tmp_path / "verify.jsonl")

        result = verifier.verify_trade(
            position_id="POS-1",
            symbol="EURUSD",
            side="LONG",
            entry_price=1.1050,
            stop_loss=1.1000,
            entry_bar_timestamp=1_700_000_000.0,
            h1_candles=_make_candles(10),  # < 50 warmup
            h4_candles=[],
        )

        assert result.verdict == "NO_SIGNAL"
        assert "Insufficient candles" in result.detail

    def test_no_signal_no_entry_found(self, tmp_path: Path):
        """Replay finds no entry signal within search window → NO_SIGNAL."""
        env = _make_env()
        verifier = PostTradeVerifier(env, log_path=tmp_path / "verify.jsonl")

        # Use a timestamp that doesn't match any candle
        result = verifier.verify_trade(
            position_id="POS-2",
            symbol="EURUSD",
            side="LONG",
            entry_price=1.1050,
            stop_loss=1.1000,
            entry_bar_timestamp=9_999_999_999.0,  # way off
            h1_candles=_make_candles(100),
            h4_candles=[],
        )

        assert result.verdict == "NO_SIGNAL"

    @patch("novatrade.monitor.post_trade_verifier.IRBStrategy")
    def test_perfect_match(self, mock_strategy_cls, tmp_path: Path):
        """Replay finds exact same signal → MATCH."""
        from novatrade.strategies.base import EntrySignal

        env = _make_env()
        mock_strategy = mock_strategy_cls.return_value
        mock_strategy.compute_indicators.return_value = {"ema": [], "atr": [], "adx": []}

        # Return a matching signal on the target bar
        mock_strategy.check_entry.return_value = EntrySignal(
            bar_index=50,
            side="LONG",
            entry_price=1.1050,
            stop_loss=1.1000,
        )

        verifier = PostTradeVerifier(env, log_path=tmp_path / "verify.jsonl")
        candles = _make_candles(100)
        target_ts = candles[50].timestamp

        result = verifier.verify_trade(
            position_id="POS-3",
            symbol="EURUSD",
            side="LONG",
            entry_price=1.1050,
            stop_loss=1.1000,
            entry_bar_timestamp=target_ts,
            h1_candles=candles,
            h4_candles=[],
        )

        assert result.verdict == "MATCH"
        assert result.match_score == 1.0
        assert result.direction_match is True
        assert result.replay_found_signal is True
        assert result.replay_side == "LONG"

    @patch("novatrade.monitor.post_trade_verifier.IRBStrategy")
    def test_direction_mismatch(self, mock_strategy_cls, tmp_path: Path):
        """Replay finds opposite side signal → MISMATCH."""
        from novatrade.strategies.base import EntrySignal

        env = _make_env()
        mock_strategy = mock_strategy_cls.return_value
        mock_strategy.compute_indicators.return_value = {"ema": [], "atr": [], "adx": []}

        mock_strategy.check_entry.return_value = EntrySignal(
            bar_index=50,
            side="SHORT",  # opposite of actual LONG
            entry_price=1.1050,
            stop_loss=1.1100,
        )

        verifier = PostTradeVerifier(env, log_path=tmp_path / "verify.jsonl")
        candles = _make_candles(100)
        target_ts = candles[50].timestamp

        result = verifier.verify_trade(
            position_id="POS-4",
            symbol="EURUSD",
            side="LONG",
            entry_price=1.1050,
            stop_loss=1.1000,
            entry_bar_timestamp=target_ts,
            h1_candles=candles,
            h4_candles=[],
        )

        assert result.verdict == "MISMATCH"
        assert result.direction_match is False
        assert result.match_score < 0.5

    @patch("novatrade.monitor.post_trade_verifier.IRBStrategy")
    def test_price_within_tolerance(self, mock_strategy_cls, tmp_path: Path):
        """Small price delta within tolerance → still MATCH."""
        from novatrade.strategies.base import EntrySignal

        env = _make_env()
        mock_strategy = mock_strategy_cls.return_value
        mock_strategy.compute_indicators.return_value = {"ema": [], "atr": [], "adx": []}

        # 1 pip difference on entry, 1.5 pip on SL
        mock_strategy.check_entry.return_value = EntrySignal(
            bar_index=50,
            side="LONG",
            entry_price=1.1051,  # 1 pip off
            stop_loss=1.09985,  # 1.5 pips off
        )

        verifier = PostTradeVerifier(
            env,
            entry_price_tolerance_pips=2.0,
            stop_loss_tolerance_pips=3.0,
            log_path=tmp_path / "verify.jsonl",
        )
        candles = _make_candles(100)
        target_ts = candles[50].timestamp

        result = verifier.verify_trade(
            position_id="POS-5",
            symbol="EURUSD",
            side="LONG",
            entry_price=1.1050,
            stop_loss=1.1000,
            entry_bar_timestamp=target_ts,
            h1_candles=candles,
            h4_candles=[],
        )

        assert result.verdict == "MATCH"
        assert result.match_score == 1.0

    @patch("novatrade.monitor.post_trade_verifier.IRBStrategy")
    def test_price_outside_tolerance(self, mock_strategy_cls, tmp_path: Path):
        """Large price delta → PARTIAL_MATCH (direction correct but prices off)."""
        from novatrade.strategies.base import EntrySignal

        env = _make_env()
        mock_strategy = mock_strategy_cls.return_value
        mock_strategy.compute_indicators.return_value = {"ema": [], "atr": [], "adx": []}

        # 10 pips off on entry, 15 pips off on SL (both exceed tolerance)
        mock_strategy.check_entry.return_value = EntrySignal(
            bar_index=50,
            side="LONG",
            entry_price=1.1060,  # 10 pips off
            stop_loss=1.0985,  # 15 pips off
        )

        verifier = PostTradeVerifier(
            env,
            entry_price_tolerance_pips=2.0,
            stop_loss_tolerance_pips=3.0,
            log_path=tmp_path / "verify.jsonl",
        )
        candles = _make_candles(100)
        target_ts = candles[50].timestamp

        result = verifier.verify_trade(
            position_id="POS-6",
            symbol="EURUSD",
            side="LONG",
            entry_price=1.1050,
            stop_loss=1.1000,
            entry_bar_timestamp=target_ts,
            h1_candles=candles,
            h4_candles=[],
        )

        assert result.verdict == "PARTIAL_MATCH"
        assert result.match_score == 0.5  # direction OK (0.5), prices both fail (0.0)

    @patch("novatrade.monitor.post_trade_verifier.IRBStrategy")
    def test_rolling_window_alert(self, mock_strategy_cls, tmp_path: Path):
        """Mismatch rate exceeding threshold triggers alert."""
        from novatrade.strategies.base import EntrySignal

        env = _make_env()
        mock_strategy = mock_strategy_cls.return_value
        mock_strategy.compute_indicators.return_value = {"ema": [], "atr": [], "adx": []}

        # Always return opposite side signal
        mock_strategy.check_entry.return_value = EntrySignal(
            bar_index=50,
            side="SHORT",
            entry_price=1.1050,
            stop_loss=1.1100,
        )

        verifier = PostTradeVerifier(
            env,
            mismatch_alert_threshold=0.20,
            rolling_window_size=5,
            log_path=tmp_path / "verify.jsonl",
        )
        candles = _make_candles(100)
        target_ts = candles[50].timestamp

        # Generate 5 mismatches (all LONG trades but replay finds SHORT)
        for i in range(5):
            verifier.verify_trade(
                position_id=f"POS-ALERT-{i}",
                symbol="EURUSD",
                side="LONG",
                entry_price=1.1050,
                stop_loss=1.1000,
                entry_bar_timestamp=target_ts,
                h1_candles=candles,
                h4_candles=[],
            )

        assert verifier.should_alert() is True
        summary = verifier.get_summary()
        assert summary.mismatch_rate > 0.20
        assert summary.alert_triggered is True

    @patch("novatrade.monitor.post_trade_verifier.IRBStrategy")
    def test_verification_log_written(self, mock_strategy_cls, tmp_path: Path):
        """JSONL verification log gets written."""
        from novatrade.strategies.base import EntrySignal

        env = _make_env()
        mock_strategy = mock_strategy_cls.return_value
        mock_strategy.compute_indicators.return_value = {"ema": [], "atr": [], "adx": []}
        mock_strategy.check_entry.return_value = EntrySignal(
            bar_index=50,
            side="LONG",
            entry_price=1.1050,
            stop_loss=1.1000,
        )

        log_path = tmp_path / "verify.jsonl"
        verifier = PostTradeVerifier(env, log_path=log_path)
        candles = _make_candles(100)
        target_ts = candles[50].timestamp

        verifier.verify_trade(
            position_id="POS-LOG",
            symbol="EURUSD",
            side="LONG",
            entry_price=1.1050,
            stop_loss=1.1000,
            entry_bar_timestamp=target_ts,
            h1_candles=candles,
            h4_candles=[],
        )

        assert log_path.exists()
        lines = log_path.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["position_id"] == "POS-LOG"
        assert entry["verdict"] == "MATCH"

    @patch("novatrade.monitor.post_trade_verifier.IRBStrategy")
    def test_journal_verify_event(self, mock_strategy_cls, tmp_path: Path):
        """VERIFY event is written to trade journal."""
        from novatrade.strategies.base import EntrySignal

        env = _make_env()
        mock_strategy = mock_strategy_cls.return_value
        mock_strategy.compute_indicators.return_value = {"ema": [], "atr": [], "adx": []}
        mock_strategy.check_entry.return_value = EntrySignal(
            bar_index=50,
            side="LONG",
            entry_price=1.1050,
            stop_loss=1.1000,
        )

        verifier = PostTradeVerifier(env, log_path=tmp_path / "verify.jsonl")
        candles = _make_candles(100)
        target_ts = candles[50].timestamp

        with patch("novatrade.monitor.trade_journal.log_trade_verify") as mock_journal:
            verifier.verify_trade(
                position_id="POS-JOURNAL",
                symbol="EURUSD",
                side="LONG",
                entry_price=1.1050,
                stop_loss=1.1000,
                entry_bar_timestamp=target_ts,
                h1_candles=candles,
                h4_candles=[],
            )

            mock_journal.assert_called_once()
            call_kwargs = mock_journal.call_args[1]
            assert call_kwargs["position_id"] == "POS-JOURNAL"
            assert call_kwargs["verdict"] == "MATCH"
            assert call_kwargs["match_score"] == 1.0


class TestVerificationSummary:
    """Test summary and alert logic."""

    def test_empty_summary(self):
        env = _make_env()
        verifier = PostTradeVerifier(env)
        summary = verifier.get_summary()
        assert summary.total_verified == 0
        assert summary.alert_triggered is False

    def test_no_alert_below_threshold(self, tmp_path: Path):
        """No alert when mismatch rate is below threshold."""
        env = _make_env()
        verifier = PostTradeVerifier(
            env,
            mismatch_alert_threshold=0.50,
            rolling_window_size=10,
            log_path=tmp_path / "verify.jsonl",
        )
        # Manually populate with a mix of results
        verifier._results.append(
            VerificationResult(
                position_id="X",
                symbol="EURUSD",
                actual_side="LONG",
                actual_entry_price=1.1,
                actual_stop_loss=1.09,
                actual_entry_bar_ts=0,
                replay_found_signal=True,
                verdict="MATCH",
                match_score=1.0,
            )
        )
        assert verifier.should_alert() is False
