"""Action->Metric Causal Model — tracks which actions produce which metric changes.

Records causal observations linking actions to their measured impact on
autonomy dimensions.  Over time this builds an empirical model of what
works and what doesn't, enabling the decision engine to make data-driven
choices instead of purely rule-based ones.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("novatrade.autonomy.causal_model")


@dataclass
class CausalRecord:
    """A single action->outcome observation."""

    action: str  # e.g. "restart novacore-novatrade"
    target_dimension: str  # e.g. "strategy_validity"
    pre_score: float  # score before action
    post_score: float  # score after action (next cycle)
    delta: float  # post - pre
    context: dict = field(default_factory=dict)  # market session, degradation tier, etc.
    effective: bool = False  # True if delta > threshold
    recorded_at: str = ""  # ISO timestamp
    decision_id: str = ""  # link to decision


class CausalModel:
    """Tracks action->metric causality from decision outcomes.

    Stores records in STATE/causal_records.json.  Provides:
    - record(): Add a new causal observation
    - rank_actions(): Rank actions by effectiveness for a dimension
    - avoid_list(): Actions that historically don't work in current context
    """

    def __init__(self, base_path: str = "/home/nova/nova-core") -> None:
        self._state_dir = Path(base_path) / "STATE"
        self._records_path = self._state_dir / "causal_records.json"
        self._max_records = 500  # keep last 500

    def record(self, rec: CausalRecord) -> None:
        """Append a causal record."""
        if not rec.recorded_at:
            rec.recorded_at = datetime.now(timezone.utc).isoformat()
        records = self._load()
        records.append(asdict(rec))
        records = records[-self._max_records :]
        self._save(records)

    def rank_actions(self, dimension: str, limit: int = 5) -> list[dict]:
        """Rank actions by average delta for a dimension.

        Returns list of {action, avg_delta, success_rate, count}
        sorted by success_rate descending.
        """
        records = self._load()
        by_action: dict[str, list[dict]] = {}
        for r in records:
            if r.get("target_dimension") == dimension:
                action = r.get("action", "unknown")
                by_action.setdefault(action, []).append(r)

        ranked = []
        for action, recs in by_action.items():
            deltas = [r.get("delta", 0) for r in recs]
            effective_count = sum(1 for r in recs if r.get("effective"))
            ranked.append(
                {
                    "action": action,
                    "avg_delta": sum(deltas) / len(deltas) if deltas else 0,
                    "success_rate": effective_count / len(recs) if recs else 0,
                    "count": len(recs),
                }
            )

        ranked.sort(key=lambda x: x["success_rate"], reverse=True)
        return ranked[:limit]

    def avoid_list(
        self,
        dimension: str,
        context: dict | None = None,
        min_samples: int = 3,
        max_success_rate: float = 0.2,
    ) -> list[str]:
        """Actions to avoid -- historically ineffective for this dimension.

        Only includes actions with at least min_samples observations
        and success rate below max_success_rate.
        """
        ranked = self.rank_actions(dimension, limit=100)
        return [r["action"] for r in ranked if r["count"] >= min_samples and r["success_rate"] <= max_success_rate]

    def get_records(self, dimension: str | None = None) -> list[dict]:
        """Return raw records, optionally filtered by dimension."""
        records = self._load()
        if dimension:
            return [r for r in records if r.get("target_dimension") == dimension]
        return records

    def _load(self) -> list[dict]:
        if not self._records_path.exists():
            return []
        try:
            data = json.loads(self._records_path.read_text())
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, records: list[dict]) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._state_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(records, f, indent=2, default=str)
            os.replace(tmp, str(self._records_path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
