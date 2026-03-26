"""Abstract base for all metric collectors."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from novatrade.autonomy.schemas import DimensionScore


class BaseCollector(ABC):
    """Base class for all scoring-dimension collectors.

    Each collector gathers sub-metrics for a single dimension, normalises
    them to 0-100, and returns a ``DimensionScore``.
    """

    def __init__(self, base_path: str = "/home/nova/nova-core") -> None:
        self.base_path = base_path
        self.log = logging.getLogger(f"novatrade.autonomy.{self.__class__.__name__}")

    @abstractmethod
    async def collect(self) -> DimensionScore:
        """Collect metrics and return a scored dimension."""
        ...

    def _safe_score(self, value: float) -> float:
        """Clamp *value* between 0 and 100."""
        return max(0.0, min(100.0, value))
