"""Chronological train/validation split helpers.

The task-1093 source describes the classic "reserve ~60% for training, ~40%
for validation" block split. The walk-forward harness already does richer
train/val/test folds, but a plain ratio split is a useful, leakage-safe
primitive for quick crypto probes — so this is a small additive complement,
not a replacement.

Pure functions only: no I/O, no engine coupling. Works on any
chronologically-ordered sequence (``list[Candle]``, list of dicts, etc.).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")


def split_by_ratio(
    series: Sequence[T],
    train_frac: float = 0.60,
    embargo: int = 0,
) -> tuple[list[T], list[T]]:
    """Split a time-ordered ``series`` into ``(train, validation)`` blocks.

    The split is purely chronological: the first ``train_frac`` of the rows go
    to train, the remainder to validation. ``embargo`` drops that many rows at
    the boundary from BOTH sides of the cut to prevent look-ahead leakage from
    overlapping indicators/labels straddling the join.

    Args:
        series: time-ordered sequence (caller guarantees ascending order).
        train_frac: fraction in (0, 1) assigned to the train block.
        embargo: bars to drop on each side of the cut (>= 0).

    Returns:
        ``(train, validation)`` as two lists.

    Raises:
        ValueError: if ``train_frac`` is out of (0, 1) or ``embargo`` < 0.
    """
    if not 0.0 < train_frac < 1.0:
        raise ValueError(f"train_frac must be in (0, 1), got {train_frac}")
    if embargo < 0:
        raise ValueError(f"embargo must be >= 0, got {embargo}")

    n = len(series)
    if n == 0:
        return [], []

    cut = round(n * train_frac)
    train = list(series[: max(0, cut - embargo)])
    val = list(series[cut + embargo :])
    return train, val
