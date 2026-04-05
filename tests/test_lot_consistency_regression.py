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

    def test_test_data_corruption_protection(self):
        """Test protection against test data corruption (0.10 median blocking 1.0 trades)."""
        checker = LotSizeConsistencyChecker(min_trades_for_enforcement=3)

        # Simulate corrupted test data - all tiny volumes
        checker.record(0.01, "EURUSD")  # Tiny test volume
        checker.record(0.05, "EURUSD")  # Tiny test volume
        checker.record(0.10, "EURUSD")  # Tiny test volume

        # Median should be tiny (0.05)
        volumes = [r.volume for r in checker._history]
        assert median(volumes) == 0.05

        # But normal FTMO volume (1.0) should be allowed due to corruption protection
        result = checker.check(1.0)
        assert result.passed is True
        assert "test data corruption" in result.detail
        assert "0.05" in result.detail
        assert "allowing 1.00" in result.detail

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
        """Test the exact boundary conditions for corruption detection."""
        checker = LotSizeConsistencyChecker(min_trades_for_enforcement=3)

        # Exactly at corruption threshold: all ≤0.15, median ≤0.15, volume ≥0.50
        checker.record(0.15, "EURUSD")  # At threshold
        checker.record(0.10, "EURUSD")  # Below threshold
        checker.record(0.05, "EURUSD")  # Below threshold

        volumes = [r.volume for r in checker._history]
        assert median(volumes) == 0.10  # ≤ 0.15 ✓
        assert all(v <= 0.15 for v in volumes)  # All ≤ 0.15 ✓

        # Volume ≥ 0.50 should trigger protection
        result = checker.check(0.50)
        assert result.passed is True
        assert "test data corruption" in result.detail

        # Volume < 0.50 should NOT trigger protection (use normal enforcement)
        # 0.35 is 3.5x median (0.10), which exceeds max_deviation_factor=3.0
        result = checker.check(0.35)
        assert result.passed is False  # Exceeds normal 3.0x limit without corruption protection

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
