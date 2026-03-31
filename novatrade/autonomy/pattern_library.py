"""Pattern Library — stores learned patterns derived from causal data.

Patterns are derived from CausalModel records when:
- An action has been tried 3+ times for the same dimension
- Success rate exceeds 60% (positive pattern) or falls below 20% (anti-pattern)

This gives the autonomy loop a growing repertoire of known-good (and
known-bad) responses to specific system states.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from novatrade.autonomy.causal_model import CausalModel

log = logging.getLogger("novatrade.autonomy.pattern_library")


@dataclass
class LearnedPattern:
    """A single learned pattern extracted from causal data."""

    pattern_id: str
    description: str
    trigger_conditions: dict = field(default_factory=dict)  # when to apply
    recommended_action: str = ""
    success_rate: float = 0.0  # historical success rate
    sample_size: int = 0  # how many times observed
    last_applied: str = ""  # ISO timestamp
    created_at: str = ""
    is_anti_pattern: bool = False  # True = "don't do this"


# Thresholds for pattern derivation
_MIN_SAMPLES = 3
_POSITIVE_THRESHOLD = 0.6  # >= 60% success = good pattern
_ANTI_THRESHOLD = 0.2  # <= 20% success = anti-pattern


class PatternLibrary:
    """Stores and retrieves learned patterns from causal data.

    Methods:
    - derive_patterns(causal_model): Scan records and create/update patterns
    - match(dimension, context): Find applicable patterns for current situation
    - get_recommendation(dimension, context): Best action recommendation
    """

    def __init__(self, base_path: str = "/home/nova/nova-core") -> None:
        self._state_dir = Path(base_path) / "STATE"
        self._patterns_path = self._state_dir / "learned_patterns.json"

    def derive_patterns(self, causal_model: CausalModel) -> list[LearnedPattern]:
        """Scan causal records and derive/update patterns.

        Groups records by (action, dimension).  Creates a positive pattern
        if success_rate >= 60% with 3+ samples, or an anti-pattern if
        success_rate <= 20% with 3+ samples.

        Returns newly created or updated patterns.
        """
        records = causal_model.get_records()
        # Group by (action, dimension)
        groups: dict[tuple[str, str], list[dict]] = {}
        for r in records:
            key = (r.get("action", ""), r.get("target_dimension", ""))
            groups.setdefault(key, []).append(r)

        existing = self._load()
        existing_by_id = {p["pattern_id"]: p for p in existing}
        now = datetime.now(timezone.utc).isoformat()
        new_or_updated: list[LearnedPattern] = []

        for (action, dimension), recs in groups.items():
            if len(recs) < _MIN_SAMPLES or not action or not dimension:
                continue

            effective_count = sum(1 for r in recs if r.get("effective"))
            success_rate = effective_count / len(recs)
            deltas = [r.get("delta", 0) for r in recs]
            avg_delta = sum(deltas) / len(deltas) if deltas else 0

            is_anti = success_rate <= _ANTI_THRESHOLD
            is_positive = success_rate >= _POSITIVE_THRESHOLD

            if not is_anti and not is_positive:
                continue  # ambiguous — not enough signal

            pattern_id = f"{'anti_' if is_anti else ''}{action}_{dimension}"
            if is_positive:
                desc = (
                    f"Action '{action}' is effective for {dimension} "
                    f"({success_rate:.0%} success, avg delta {avg_delta:+.1f})"
                )
            else:
                desc = (
                    f"AVOID: Action '{action}' is ineffective for {dimension} "
                    f"({success_rate:.0%} success, avg delta {avg_delta:+.1f})"
                )

            pattern = LearnedPattern(
                pattern_id=pattern_id,
                description=desc,
                trigger_conditions={"dimension": dimension},
                recommended_action=action if is_positive else "",
                success_rate=success_rate,
                sample_size=len(recs),
                last_applied="",
                created_at=existing_by_id.get(pattern_id, {}).get("created_at", now),
                is_anti_pattern=is_anti,
            )

            existing_by_id[pattern_id] = asdict(pattern)
            new_or_updated.append(pattern)

        # Save all
        self._save(list(existing_by_id.values()))
        return new_or_updated

    def match(self, dimension: str, context: dict | None = None) -> list[LearnedPattern]:
        """Find patterns applicable to the given dimension."""
        patterns = self._load()
        results = []
        for p in patterns:
            triggers = p.get("trigger_conditions", {})
            if triggers.get("dimension") == dimension:
                results.append(
                    LearnedPattern(
                        pattern_id=p.get("pattern_id", ""),
                        description=p.get("description", ""),
                        trigger_conditions=triggers,
                        recommended_action=p.get("recommended_action", ""),
                        success_rate=p.get("success_rate", 0),
                        sample_size=p.get("sample_size", 0),
                        last_applied=p.get("last_applied", ""),
                        created_at=p.get("created_at", ""),
                        is_anti_pattern=p.get("is_anti_pattern", False),
                    )
                )
        return results

    def get_recommendation(self, dimension: str, context: dict | None = None) -> str | None:
        """Get the best action recommendation for a dimension.

        Returns the recommended_action from the highest success-rate
        positive pattern, or None if no good patterns exist.
        """
        matches = self.match(dimension, context)
        # Only consider positive patterns
        positive = [p for p in matches if not p.is_anti_pattern and p.success_rate > 0.5]
        if not positive:
            return None
        best = max(positive, key=lambda p: p.success_rate)
        return best.recommended_action

    def get_anti_patterns(self, dimension: str) -> list[LearnedPattern]:
        """Get anti-patterns (things to avoid) for a dimension."""
        matches = self.match(dimension)
        return [p for p in matches if p.is_anti_pattern]

    def _load(self) -> list[dict]:
        if not self._patterns_path.exists():
            return []
        try:
            data = json.loads(self._patterns_path.read_text())
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, patterns: list[dict]) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._state_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(patterns, f, indent=2, default=str)
            os.replace(tmp, str(self._patterns_path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
