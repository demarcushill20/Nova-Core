"""Reflexion Wrapper — self-critique and learning after each heartbeat cycle.

After each heartbeat cycle, perform a brief self-critique step:
"Did my decisions match conditions? What would I do differently?"
Store insights via upsert_memory for future learning.
"""

import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("novatrade.utils.reflexion")


class ReflexionEngine:
    """Self-critique engine for heartbeat cycle decisions."""

    def __init__(self):
        self.enabled = True
        self._session_insights: list[dict[str, Any]] = []

    def reflect_on_cycle(
        self,
        autonomy_snapshot: dict[str, Any] | None = None,
        cruise_active: bool = False,
        cycle_duration_ms: float = 0.0,
        failed_checks: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Perform self-critique on the completed heartbeat cycle.

        Args:
            autonomy_snapshot: The autonomy analysis results
            cruise_active: Whether CRUISE mode was active
            cycle_duration_ms: How long the cycle took
            failed_checks: List of failed health check names

        Returns:
            Insight dict if generated, None if skipped
        """
        if not self.enabled:
            return None

        try:
            insight = self._generate_insight(
                autonomy_snapshot=autonomy_snapshot,
                cruise_active=cruise_active,
                cycle_duration_ms=cycle_duration_ms,
                failed_checks=failed_checks or [],
            )

            if insight:
                self._session_insights.append(insight)
                log.debug(f"Generated reflexion insight: {insight['type']}")

            return insight

        except Exception as e:
            log.error(f"Reflexion failed: {e}")
            return None

    def _generate_insight(
        self,
        autonomy_snapshot: dict[str, Any] | None,
        cruise_active: bool,
        cycle_duration_ms: float,
        failed_checks: list[str],
    ) -> dict[str, Any] | None:
        """Generate a specific insight based on cycle conditions."""

        timestamp = datetime.now(timezone.utc).isoformat()

        # Insight 1: Cruise mode effectiveness
        if cruise_active:
            return {
                "type": "cruise_efficiency",
                "timestamp": timestamp,
                "observation": "CRUISE mode active - system stable, expensive cycles skipped",
                "effectiveness": "high",
                "lesson": "Stable periods allow cost-efficient monitoring without degrading system health",
                "metadata": {"cycle_duration_ms": cycle_duration_ms, "failed_checks_count": len(failed_checks)},
            }

        # Insight 2: Performance analysis
        if cycle_duration_ms > 30000:  # > 30 seconds
            return {
                "type": "performance_concern",
                "timestamp": timestamp,
                "observation": f"Long heartbeat cycle: {cycle_duration_ms:.0f}ms",
                "effectiveness": "low",
                "lesson": "Cycles >30s indicate bottlenecks - investigate LLM call latency or agent spawning delays",
                "metadata": {"cycle_duration_ms": cycle_duration_ms, "threshold_exceeded": True},
            }

        # Insight 3: Health degradation pattern
        if failed_checks and len(failed_checks) >= 3:
            return {
                "type": "health_degradation",
                "timestamp": timestamp,
                "observation": f"Multiple health check failures: {', '.join(failed_checks[:3])}",
                "effectiveness": "low",
                "lesson": f"Cascading failures detected - prioritize fixing {failed_checks[0]} first",
                "metadata": {"failed_checks": failed_checks, "failure_count": len(failed_checks)},
            }

        # Insight 4: Decision quality analysis
        if autonomy_snapshot:
            decision = autonomy_snapshot.get("decision", {})
            overall_score = autonomy_snapshot.get("overall_score", 0)
            decision_mode = decision.get("mode", "unknown")

            # High score but still taking action - might be over-acting
            if overall_score >= 80 and decision_mode in ["research", "plan", "execute"]:
                return {
                    "type": "decision_calibration",
                    "timestamp": timestamp,
                    "observation": f"High score ({overall_score}) but active decision ({decision_mode})",
                    "effectiveness": "medium",
                    "lesson": (
                        "High-score active decisions suggest threshold tuning"
                        " needed - consider raising action thresholds"
                    ),
                    "metadata": {
                        "score": overall_score,
                        "decision_mode": decision_mode,
                        "decision_reason": decision.get("reason", "")[:100],  # truncate
                    },
                }

            # Low score but monitoring - might be under-acting
            if overall_score <= 60 and decision_mode == "monitor":
                return {
                    "type": "under_action_risk",
                    "timestamp": timestamp,
                    "observation": f"Low score ({overall_score}) but passive decision (monitor)",
                    "effectiveness": "low",
                    "lesson": (
                        "Low scores with monitoring suggest missed action"
                        " opportunities - investigate thresholds and cooldowns"
                    ),
                    "metadata": {"score": overall_score, "decision_mode": decision_mode},
                }

        # Insight 5: System stability
        if not failed_checks and cycle_duration_ms < 15000:
            return {
                "type": "optimal_operation",
                "timestamp": timestamp,
                "observation": "Fast cycle with no failures",
                "effectiveness": "high",
                "lesson": "Optimal operating conditions - maintain current configuration and patterns",
                "metadata": {"cycle_duration_ms": cycle_duration_ms, "health_status": "all_green"},
            }

        # No specific insight generated
        return None

    def get_session_summary(self) -> dict[str, Any]:
        """Get a summary of insights from this session."""
        if not self._session_insights:
            return {"insight_count": 0, "patterns": []}

        insight_types = [i["type"] for i in self._session_insights]
        type_counts: dict[str, int] = {}
        for itype in insight_types:
            type_counts[itype] = type_counts.get(itype, 0) + 1

        effectiveness_distribution: dict[str, int] = {}
        for insight in self._session_insights:
            eff = insight.get("effectiveness", "unknown")
            effectiveness_distribution[eff] = effectiveness_distribution.get(eff, 0) + 1

        return {
            "insight_count": len(self._session_insights),
            "patterns": type_counts,
            "effectiveness_distribution": effectiveness_distribution,
            "latest_insight": self._session_insights[-1] if self._session_insights else None,
        }

    def format_insight_for_memory(self, insight: dict[str, Any]) -> dict[str, Any]:
        """Format an insight for storage in Fusion Memory."""
        return {
            "content": f"Reflexion: {insight['observation']} → {insight['lesson']}",
            "metadata": {
                "category": "reflexion",
                "project": "nova-core",
                "insight_type": insight["type"],
                "effectiveness": insight["effectiveness"],
                "timestamp": insight["timestamp"],
                **insight.get("metadata", {}),
            },
        }


# Global instance
reflexion_engine = ReflexionEngine()
