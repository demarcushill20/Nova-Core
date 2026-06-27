"""Tests for the chronological train/validation split helper (task-1093).

Pure-function coverage: ratio correctness, embargo gap, boundary/validation
errors, and order preservation. No network — the crypto fetcher is exercised
separately/manually since it hits Binance's public API.
"""

from __future__ import annotations

import pytest

from novatrade.data.splits import split_by_ratio


def test_default_60_40_split():
    series = list(range(100))
    train, val = split_by_ratio(series, train_frac=0.60)
    assert train == list(range(60))
    assert val == list(range(60, 100))
    assert len(train) + len(val) == 100


def test_split_preserves_order_and_disjoint():
    series = list(range(50))
    train, val = split_by_ratio(series, train_frac=0.7)
    assert train == sorted(train)
    assert val == sorted(val)
    assert set(train).isdisjoint(set(val))


def test_embargo_drops_from_both_sides():
    series = list(range(100))
    train, val = split_by_ratio(series, train_frac=0.60, embargo=5)
    # cut=60; train loses last 5 (55), val loses first 5 (drops 60..64).
    assert train == list(range(55))
    assert val == list(range(65, 100))
    # 10 bars total are embargoed out.
    assert len(train) + len(val) == 90


def test_empty_series():
    assert split_by_ratio([], 0.6) == ([], [])


def test_rounding_to_nearest_bar():
    series = list(range(10))
    train, val = split_by_ratio(series, train_frac=0.55)  # 5.5 -> round -> 6
    assert len(train) == 6
    assert len(val) == 4


@pytest.mark.parametrize("bad", [0.0, 1.0, -0.1, 1.5])
def test_invalid_train_frac(bad):
    with pytest.raises(ValueError):
        split_by_ratio(list(range(10)), train_frac=bad)


def test_negative_embargo_rejected():
    with pytest.raises(ValueError):
        split_by_ratio(list(range(10)), train_frac=0.6, embargo=-1)
