"""Skill-aware caching layer for token efficiency.

Extends the existing LLM cache with skill-specific caching:
- Longer TTL for skill-guided responses (72h vs 24h)
- Skill execution templates for warm reuse
- Progressive disclosure (headers-only for selection, full body after selection)
- Token savings tracking per skill
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Token efficiency constants
SKILL_CACHE_TTL = 259200  # 72 hours for skill-specific cache entries
DEFAULT_CACHE_TTL = 86400  # 24 hours for general entries


@dataclass
class TokenStats:
    """Token usage statistics for a skill."""

    skill_name: str
    cold_tokens: int = 0  # tokens used on first execution (no cache)
    warm_tokens: int = 0  # tokens used on cached reuse
    cold_count: int = 0  # number of cold (uncached) executions
    warm_count: int = 0  # number of warm (cached) executions

    @property
    def savings_ratio(self) -> float:
        """Token savings from warm reuse: 1 - (warm_avg / cold_avg)."""
        if self.cold_count == 0 or self.warm_count == 0:
            return 0.0
        cold_avg = self.cold_tokens / self.cold_count
        warm_avg = self.warm_tokens / self.warm_count
        if cold_avg == 0:
            return 0.0
        return max(0.0, 1.0 - (warm_avg / cold_avg))

    @property
    def total_tokens_saved(self) -> int:
        """Estimated total tokens saved by warm reuse."""
        if self.cold_count == 0 or self.warm_count == 0:
            return 0
        cold_avg = self.cold_tokens / self.cold_count
        # Tokens that would have been used if all warm calls were cold
        return int(self.warm_count * cold_avg) - self.warm_tokens

    def to_dict(self) -> dict:
        """Serialize stats to a dictionary."""
        return {
            "skill_name": self.skill_name,
            "cold_tokens": self.cold_tokens,
            "warm_tokens": self.warm_tokens,
            "cold_count": self.cold_count,
            "warm_count": self.warm_count,
            "savings_ratio": round(self.savings_ratio, 4),
            "total_tokens_saved": self.total_tokens_saved,
        }


class SkillCache:
    """Skill-aware caching for token efficiency.

    Uses Redis (via the existing LLM cache infrastructure) with longer TTL
    for skill-specific entries. Also provides progressive disclosure
    and execution template reuse.
    """

    def __init__(self, stats_path: str = "STATE/skill_token_stats.json"):
        self._stats: dict[str, TokenStats] = {}
        self._stats_path = Path(stats_path)
        self._load_stats()

    def _load_stats(self) -> None:
        """Load token stats from persistent storage."""
        try:
            if self._stats_path.exists():
                data = json.loads(self._stats_path.read_text())
                for name, d in data.items():
                    self._stats[name] = TokenStats(
                        skill_name=name,
                        cold_tokens=d.get("cold_tokens", 0),
                        warm_tokens=d.get("warm_tokens", 0),
                        cold_count=d.get("cold_count", 0),
                        warm_count=d.get("warm_count", 0),
                    )
        except Exception as e:
            logger.warning("Failed to load skill token stats: %s", e)

    def _save_stats(self) -> None:
        """Persist token stats to disk."""
        try:
            self._stats_path.parent.mkdir(parents=True, exist_ok=True)
            data = {name: stats.to_dict() for name, stats in self._stats.items()}
            self._stats_path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning("Failed to save skill token stats: %s", e)

    def record_usage(self, skill_name: str, tokens_used: int, was_cached: bool) -> None:
        """Record token usage for a skill execution."""
        if skill_name not in self._stats:
            self._stats[skill_name] = TokenStats(skill_name=skill_name)

        stats = self._stats[skill_name]
        if was_cached:
            stats.warm_tokens += tokens_used
            stats.warm_count += 1
        else:
            stats.cold_tokens += tokens_used
            stats.cold_count += 1

        self._save_stats()

    def get_stats(self, skill_name: str) -> TokenStats | None:
        """Get token stats for a specific skill."""
        return self._stats.get(skill_name)

    def get_all_stats(self) -> dict[str, TokenStats]:
        """Get all token stats."""
        return dict(self._stats)

    def get_total_savings(self) -> dict:
        """Get aggregate token savings across all skills."""
        total_cold = sum(s.cold_tokens for s in self._stats.values())
        total_warm = sum(s.warm_tokens for s in self._stats.values())
        total_cold_count = sum(s.cold_count for s in self._stats.values())
        total_warm_count = sum(s.warm_count for s in self._stats.values())
        total_saved = sum(s.total_tokens_saved for s in self._stats.values())

        return {
            "total_cold_tokens": total_cold,
            "total_warm_tokens": total_warm,
            "total_cold_executions": total_cold_count,
            "total_warm_executions": total_warm_count,
            "total_tokens_saved": total_saved,
            "overall_savings_ratio": round(
                1.0 - (total_warm / max(total_cold, 1)) if total_cold > 0 else 0.0,
                4,
            ),
        }

    @staticmethod
    def make_skill_cache_key(skill_id: str, task_pattern: str, model: str = "") -> str:
        """Generate a cache key for skill-specific responses."""
        raw = f"skill:{skill_id}:{task_pattern}:{model}"
        return f"skill_cache:{hashlib.sha256(raw.encode()).hexdigest()}"

    @staticmethod
    def progressive_disclosure_header(
        skill_name: str,
        description: str,
        quality_score: float = 0.0,
        completion_rate: float = 0.0,
    ) -> str:
        """Generate a compact header for skill selection (not full body).

        Shows only name + description + quality stats during selection.
        Full SKILL.md body is loaded only AFTER selection.
        """
        return f"[{skill_name}] {description} (quality={quality_score:.2f}, completion={completion_rate:.2f})"

    @staticmethod
    def build_execution_template(
        skill_name: str,
        task_description: str,
        execution_summary: str,
    ) -> dict:
        """Build an execution template from a successful run.

        Templates capture the essence of a successful execution for reuse.
        Stored in Fusion Memory or local cache for warm retrieval.
        """
        return {
            "skill_name": skill_name,
            "task_pattern": task_description[:500],
            "execution_template": execution_summary[:2000],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "template_key": hashlib.sha256(f"{skill_name}:{task_description[:500]}".encode()).hexdigest()[:16],
        }
