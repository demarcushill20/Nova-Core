"""Regression tests for lot consistency checker fixes.

Tests for Phase 2 fixes:
1. Empty history handling - skip enforcement when no history
2. Test data corruption protection - allow normal FTMO lots when history has tiny test volumes
"""

import time
from statistics import median

from novatrade.risk.ftmo_compliance import LotRecord, LotSizeConsistencyChecker


class TestLotConsistencyRegression:
    """Regression tests for lot consistency checker Phase 2 fixes."""

    def test_empty_recent_history_skips_enforcement(self):
        """Test that empty recent history skips enforcement (main bug from plan)."""
        checker = LotSizeConsistencyChecker(min_trades_for_enforcement=1)

        # Add old historical data (>30 days old)
        old_time = time.time() - (35 * 24 * 3600)  # 35 days ago
        checker._history.append(LotRecord(old_time, 0.10, "EURUSD"))
        checker._history.append(LotRecord(old_time, 0.15, "EURUSD"))

        # Should skip enforcement due to no recent data
        result = checker.check(1.0)
        assert result.passed is True
        assert "recent trades" in result.detail
        assert "enforcement" in result.detail

    def test_empty_total_history_skips_enforcement(self):
        """Test that completely empty history skips enforcement."""
        checker = LotSizeConsistencyChecker()

        # No history at all
        assert len(checker._history) == 0

        # Should skip enforcement due to insufficient history
        result = checker.check(1.0)
        assert result.passed is True
        assert "trades in history" in result.detail
        assert "need" in result.detail

    def test_mixed_tiny_volumes_rejected(self):
        """Non-identical tiny volumes should still enforce lot consistency (not corruption)."""
        checker = LotSizeConsistencyChecker(min_trades_for_enforcement=3)

        # Mixed tiny volumes — these are NOT all identical, so NOT corruption
        checker.record(0.01, "EURUSD")
        checker.record(0.05, "EURUSD")
        checker.record(0.10, "EURUSD")

        volumes = [r.volume for r in checker._history]
        assert median(volumes) == 0.05

        # Normal enforcement: 1.0 is 20x median, exceeds 3x → reject
        result = checker.check(1.0)
        assert result.passed is False
        assert "exceeds" in result.detail

    def test_identical_full_window_triggers_corruption_bypass(self):
        """Full window of identical tiny volumes triggers corruption bypass."""
        # window_size=5 to keep the test compact
        checker = LotSizeConsistencyChecker(min_trades_for_enforcement=3, window_size=5)
        for _ in range(5):
            checker.record(0.10, "EURUSD")

        # All 5 identical values fill the window → corruption detected
        result = checker.check(1.0)
        assert result.passed is True
        assert "test data corruption" in result.detail
        assert "identical" in result.detail

    def test_normal_lot_enforcement_still_works(self):
        """Test that normal lot size enforcement still works for legitimate data."""
        checker = LotSizeConsistencyChecker(min_trades_for_enforcement=3, max_deviation_factor=3.0)

        # Add legitimate FTMO-sized trading history
        checker.record(1.0, "EURUSD")
        checker.record(1.2, "EURUSD")
        checker.record(0.8, "EURUSD")

        # Normal volume should pass
        result = checker.check(1.0)
        assert result.passed is True
        assert "ratio=" in result.detail
        assert "1.00" in result.detail

        # Excessive volume should fail (median ~1.0, so 5.0 is 5x > 3.0 limit)
        result = checker.check(5.0)
        assert result.passed is False
        assert "exceeds" in result.detail

    def test_mixed_history_corruption_edge_case(self):
        """Test edge case with mix of normal and corrupted data."""
        checker = LotSizeConsistencyChecker(min_trades_for_enforcement=3)

        # Add mix: mostly tiny (corrupted) + one normal
        checker.record(0.01, "EURUSD")  # Tiny
        checker.record(0.05, "EURUSD")  # Tiny
        checker.record(1.0, "EURUSD")  # Normal

        # Median will be 0.05, but not ALL volumes are ≤0.15
        volumes = [r.volume for r in checker._history]
        assert median(volumes) == 0.05
        assert not all(v <= 0.15 for v in volumes)  # 1.0 breaks the pattern

        # Should NOT trigger corruption protection (because not all are tiny)
        # So 1.0 volume should be rejected as ~20x the median
        result = checker.check(1.0)
        assert result.passed is False
        assert "exceeds" in result.detail

    def test_boundary_corruption_threshold(self):
        """Non-identical tiny volumes don't trigger corruption bypass."""
        checker = LotSizeConsistencyChecker(min_trades_for_enforcement=3)

        # Mixed values: all ≤0.15 but NOT identical → no corruption bypass
        checker.record(0.15, "EURUSD")
        checker.record(0.10, "EURUSD")
        checker.record(0.05, "EURUSD")

        volumes = [r.volume for r in checker._history]
        assert median(volumes) == 0.10
        assert all(v <= 0.15 for v in volumes)

        # 0.50 is 5x median — exceeds 3x limit, should be REJECTED
        result = checker.check(0.50)
        assert result.passed is False
        assert "exceeds" in result.detail

        # 0.25 is 2.5x median — within 3x limit, should PASS
        result = checker.check(0.25)
        assert result.passed is True

    def test_zero_median_protection(self):
        """Test protection against zero median calculations."""
        checker = LotSizeConsistencyChecker(min_trades_for_enforcement=3)

        # All zero volumes
        checker.record(0.0, "EURUSD")
        checker.record(0.0, "EURUSD")
        checker.record(0.0, "EURUSD")

        # Should skip enforcement due to zero median
        result = checker.check(1.0)
        assert result.passed is True
        assert "median volume is 0" in result.detail

    def test_empty_recent_volumes_protection(self):
        """Test the new empty recent_volumes protection."""
        checker = LotSizeConsistencyChecker(min_trades_for_enforcement=1)

        # This shouldn't happen in normal flow, but test the defensive code
        # We can't easily trigger this without mocking, so this is more of a code coverage test
        # The actual protection is in the code: if not recent_volumes: return passed=True

        # Add history that would pass the initial length check
        checker.record(1.0, "EURUSD")

        # Normal case should work
        result = checker.check(1.0)
        assert result.passed is True
        # This should pass due to normal logic, not the empty protection
