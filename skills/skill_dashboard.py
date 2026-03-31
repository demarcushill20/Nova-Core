"""Skill evolution observability dashboard.

Aggregates data from all skill subsystems to provide:
- Skill health overview
- Evolution history and lineage visualization
- Token savings metrics
- Queue and circuit breaker status
- Telegram-ready formatted reports

Phase 10 of Self-Evolving Skills.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from skills.evolution_audit import EvolutionAudit
from skills.evolution_queue import EvolutionQueue
from skills.execution_analysis import SkillHealth
from skills.skill_cache import SkillCache
from skills.version_store import SkillVersionStore

logger = logging.getLogger(__name__)


class SkillDashboard:
    """Aggregated dashboard for skill evolution observability.

    Read-only module — queries existing data, never modifies state.
    All subsystem dependencies are optional; missing subsystems produce
    empty/default results rather than errors.
    """

    def __init__(
        self,
        version_store: SkillVersionStore | None = None,
        skill_cache: SkillCache | None = None,
        evolution_queue: EvolutionQueue | None = None,
        evolution_audit: EvolutionAudit | None = None,
    ):
        self.version_store = version_store
        self.skill_cache = skill_cache
        self.evolution_queue = evolution_queue
        self.evolution_audit = evolution_audit

    # -----------------------------------------------------------------
    # Health summary
    # -----------------------------------------------------------------

    def get_health_summary(self) -> dict:
        """Get aggregated health summary across all skills.

        Returns:
            {
                "total_skills": int,
                "active_skills": int,
                "healthy_skills": int,
                "unhealthy_skills": int,
                "frozen_skills": list[str],
                "skills": list[dict],
                "generated_at": str
            }
        """
        result: dict = {
            "total_skills": 0,
            "active_skills": 0,
            "healthy_skills": 0,
            "unhealthy_skills": 0,
            "frozen_skills": [],
            "skills": [],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

        if not self.version_store:
            return result

        # Get all skills (active + inactive)
        all_skills = self.version_store.list_skills(active_only=False)
        active_skills = [s for s in all_skills if s.is_active]
        result["total_skills"] = len(all_skills)
        result["active_skills"] = len(active_skills)

        # get_skill_health returns list[SkillHealth] (Pydantic models)
        # Use default min_selections=5 to avoid flagging freshly registered
        # skills with zero executions as unhealthy.
        health_data: list[SkillHealth] = self.version_store.get_skill_health()

        healthy_count = 0
        unhealthy_count = 0
        skill_dicts: list[dict] = []

        for h in health_data:
            d = h.model_dump()
            skill_dicts.append(d)
            if h.is_healthy:
                healthy_count += 1
            else:
                unhealthy_count += 1

        result["healthy_skills"] = healthy_count
        result["unhealthy_skills"] = unhealthy_count
        result["skills"] = skill_dicts

        # Frozen skills
        if self.evolution_queue:
            result["frozen_skills"] = self.evolution_queue.get_frozen_skills()

        return result

    # -----------------------------------------------------------------
    # Evolution summary
    # -----------------------------------------------------------------

    def get_evolution_summary(self) -> dict:
        """Get evolution activity summary.

        Returns:
            {
                "total_evolutions": int,
                "by_type": {"FIX": int, "DERIVED": int, "CAPTURED": int},
                "success_rate": float,
                "recent": list[dict],
                "queue_status": dict,
            }
        """
        result: dict = {
            "total_evolutions": 0,
            "by_type": {"FIX": 0, "DERIVED": 0, "CAPTURED": 0},
            "success_rate": 0.0,
            "recent": [],
            "queue_status": {},
        }

        if self.evolution_audit:
            entries = self.evolution_audit.get_recent_entries(limit=100)
            result["total_evolutions"] = len(entries)

            successes = 0
            for entry in entries:
                etype = entry.evolution_type
                if etype in result["by_type"]:
                    result["by_type"][etype] += 1
                if entry.result == "success":
                    successes += 1

            if entries:
                result["success_rate"] = round(successes / len(entries), 4)

            # Last 10 entries as dicts for the "recent" field
            result["recent"] = [e.to_dict() for e in entries[-10:]]

        if self.evolution_queue:
            result["queue_status"] = self.evolution_queue.get_queue_status()

        return result

    # -----------------------------------------------------------------
    # Token savings
    # -----------------------------------------------------------------

    def get_token_savings(self) -> dict:
        """Get token savings from warm skill reuse.

        Returns:
            {
                "total_tokens_saved": int,
                "overall_savings_ratio": float,
                ... (other total_savings fields),
                "per_skill": {name: TokenStats.to_dict()}
            }
        """
        if not self.skill_cache:
            return {
                "total_tokens_saved": 0,
                "overall_savings_ratio": 0.0,
                "per_skill": {},
            }

        total = self.skill_cache.get_total_savings()
        per_skill: dict = {}
        for name, stats in self.skill_cache.get_all_stats().items():
            per_skill[name] = stats.to_dict()

        total["per_skill"] = per_skill
        return total

    # -----------------------------------------------------------------
    # Lineage tree
    # -----------------------------------------------------------------

    def get_lineage_tree(self, skill_name: str) -> dict:
        """Get the full lineage DAG for a skill as a tree structure.

        Returns:
            {
                "root": str,
                "nodes": [
                    {"skill_id": str, "name": str, "generation": int,
                     "origin": str, "is_active": bool, "version": str,
                     "stats": dict, "change_summary": str, "created_at": str}
                ],
                "edges": [{"from": str, "to": str}]
            }
        """
        empty: dict = {"root": "", "nodes": [], "edges": []}

        if not self.version_store:
            return empty

        # Get active version by name
        active = self.version_store.get_active_version(skill_name)
        if not active:
            return empty

        # Walk the lineage chain (returns list from current -> root)
        chain = self.version_store.get_lineage_chain(active.skill_id)

        nodes: list[dict] = []
        edges: list[dict] = []
        root = ""

        for sv in chain:
            node = {
                "skill_id": sv.skill_id,
                "name": sv.name,
                "generation": sv.lineage.generation,
                "origin": sv.lineage.origin.value,
                "is_active": sv.is_active,
                "version": sv.version,
                "stats": {
                    "selections": sv.stats.selections,
                    "executions": sv.stats.executions,
                    "completions": sv.stats.completions,
                    "failures": sv.stats.failures,
                    "fallbacks": sv.stats.fallbacks,
                },
                "change_summary": sv.lineage.change_summary or "",
                "created_at": sv.created_at,
            }
            nodes.append(node)

            # Build edges from parent_ids
            for parent_id in sv.lineage.parent_ids:
                edges.append({"from": parent_id, "to": sv.skill_id})

            # Root is generation 0
            if sv.lineage.generation == 0:
                root = sv.skill_id

        return {"root": root, "nodes": nodes, "edges": edges}

    # -----------------------------------------------------------------
    # Full report
    # -----------------------------------------------------------------

    def get_full_report(self) -> dict:
        """Get the complete dashboard report combining all sections."""
        return {
            "health": self.get_health_summary(),
            "evolution": self.get_evolution_summary(),
            "token_savings": self.get_token_savings(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    # -----------------------------------------------------------------
    # Telegram formatting
    # -----------------------------------------------------------------

    def format_telegram_health(self) -> str:
        """Format health summary for Telegram notification.

        Concise, one-alert-per-metric-type. Shows up to 3 unhealthy skills.
        """
        health = self.get_health_summary()

        lines = [
            "\U0001f4ca *Skill Health Report*",
            f"Active: {health['active_skills']}/{health['total_skills']}",
            f"Healthy: {health['healthy_skills']} | Unhealthy: {health['unhealthy_skills']}",
        ]

        if health["frozen_skills"]:
            lines.append(f"\U0001f9ca Frozen: {', '.join(health['frozen_skills'])}")

        # Top unhealthy skills (limit to 3)
        unhealthy = [s for s in health.get("skills", []) if not s.get("is_healthy", True)]
        for s in unhealthy[:3]:
            name = s.get("skill_name", "?")
            rate = s.get("completion_rate", 0)
            lines.append(f"  \u26a0\ufe0f {name}: completion={rate:.0%}")

        return "\n".join(lines)

    def format_telegram_evolution(self) -> str:
        """Format evolution summary for Telegram notification."""
        evo = self.get_evolution_summary()

        lines = [
            "\U0001f9ec *Evolution Report*",
            f"Total: {evo['total_evolutions']} (success: {evo['success_rate']:.0%})",
            (
                f"FIX: {evo['by_type']['FIX']} | "
                f"DERIVED: {evo['by_type']['DERIVED']} | "
                f"CAPTURED: {evo['by_type']['CAPTURED']}"
            ),
        ]

        queue = evo.get("queue_status", {})
        if queue:
            lines.append(f"Queue: {queue.get('queued', 0)} pending, {queue.get('active_evolutions', 0)} active")
            frozen = queue.get("frozen_skills", [])
            if frozen:
                lines.append(f"\U0001f9ca Frozen: {', '.join(frozen)}")

        return "\n".join(lines)

    def format_telegram_savings(self) -> str:
        """Format token savings for Telegram notification."""
        savings = self.get_token_savings()

        total_saved = savings.get("total_tokens_saved", 0)
        ratio = savings.get("overall_savings_ratio", 0)

        lines = [
            "\U0001f4b0 *Token Savings*",
            f"Total saved: {total_saved:,} tokens",
            f"Overall savings: {ratio:.1%}",
        ]

        # Top savers
        per_skill = savings.get("per_skill", {})
        top = sorted(
            per_skill.items(),
            key=lambda x: x[1].get("total_tokens_saved", 0),
            reverse=True,
        )[:3]
        for name, stats in top:
            saved = stats.get("total_tokens_saved", 0)
            if saved > 0:
                lines.append(f"  \u2728 {name}: {saved:,} tokens saved")

        return "\n".join(lines)
