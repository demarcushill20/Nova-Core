"""Reasoning Engine — LLM-powered gap analysis replacing rule-based decision logic.

Where the DecisionEngine uses hardcoded if/else rules that handle known failure
patterns, the ReasoningEngine handles *novel* situations by asking an LLM to
analyze the current system state and recommend actions.

Design principles:
  - Existing rule-based DecisionEngine remains the safety fallback
  - ReasoningEngine is called only when rules return MONITOR (nothing obvious)
    or when explicitly requested via shadow mode
  - Cost-controlled: max N reasoning calls per day, skip when scores unchanged
  - Structured output: LLM returns a ReasoningResult (not free text)

Usage:
    engine = ReasoningEngine()
    result = await engine.reason(context, report, recent_outcomes)
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from novatrade.autonomy.decision_context import DecisionContext
from novatrade.autonomy.decision_engine import ActionMode, Decision
from novatrade.autonomy.schemas import ProgressReport

log = logging.getLogger("novatrade.autonomy.reasoning_engine")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ReasoningResult:
    """Output of an LLM reasoning call."""

    recommended_mode: str  # ActionMode value
    target_dimension: str | None
    reasoning_chain: list[str]  # step-by-step reasoning
    suggested_actions: list[str]
    confidence: str  # high / medium / low
    novel_insight: str | None = None  # something rules wouldn't catch
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


@dataclass
class ReasoningConfig:
    """Tunable parameters for the reasoning engine."""

    max_calls_per_day: int = 5
    skip_if_score_unchanged: bool = True
    score_change_threshold: float = 2.0  # min delta to trigger reasoning
    model: str = "haiku"  # use cheapest model by default
    max_prompt_tokens: int = 2000


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class ReasoningEngine:
    """LLM-powered reasoning about system state gaps.

    Called when the rule-based DecisionEngine returns MONITOR but there may
    be novel patterns worth investigating.  Falls back gracefully if LLM
    is unavailable or budget exhausted.
    """

    def __init__(
        self,
        config: ReasoningConfig | None = None,
        base_path: str = "/home/nova/nova-core",
    ) -> None:
        self.config = config or ReasoningConfig()
        self._state_dir = Path(base_path) / "STATE"
        self._history_path = self._state_dir / "reasoning_history.json"
        self._last_score: float | None = self._load_last_score()

    async def reason(
        self,
        context: DecisionContext,
        report: ProgressReport,
        recent_outcomes: list[dict] | None = None,
    ) -> ReasoningResult | None:
        """Analyze the current state and produce a reasoning result.

        Returns None if reasoning is skipped (budget, unchanged scores, etc.).
        """
        # Budget check
        if self._daily_budget_exhausted():
            log.info(
                "Reasoning skipped — daily budget exhausted (%d/%d)", self._count_today(), self.config.max_calls_per_day
            )
            return None

        # Score-change gate
        if self.config.skip_if_score_unchanged and self._last_score is not None:
            delta = abs(report.overall_score - self._last_score)
            if delta < self.config.score_change_threshold:
                log.debug("Reasoning skipped — score unchanged (delta=%.1f)", delta)
                return None

        self._last_score = report.overall_score
        self._save_last_score(report.overall_score)

        # Build prompt
        prompt = self._build_prompt(context, report, recent_outcomes)

        # Call LLM (or synthesize from rules if LLM unavailable)
        result = await self._call_llm(prompt, report, context)

        # Persist
        if result:
            self._persist_result(result)

        return result

    def should_reason(self, rule_decision: Decision, report: ProgressReport) -> bool:
        """Determine if LLM reasoning should supplement rule-based decision.

        Reasoning is valuable when:
        1. Rules returned MONITOR but some dimension is YELLOW (not obvious fix)
        2. A REPAIR was issued but recent outcomes show repairs are ineffective
        3. Multiple dimensions are degrading simultaneously (complex interaction)
        """
        # Case 1: MONITOR with YELLOW dimensions (rules see nothing, but something's off)
        if rule_decision.mode == ActionMode.MONITOR:
            yellow_dims = [name for name, dim in report.dimensions.items() if 40 <= dim.score < 70]
            if yellow_dims:
                return True

        # Case 2: Multiple dimensions degrading
        degrading = [name for name, trend in report.trends.items() if trend.direction == "degrading"]
        if len(degrading) >= 2:
            return True

        # Case 3: Overall score below 75 but rules say MONITOR
        return rule_decision.mode == ActionMode.MONITOR and report.overall_score < 75

    def _build_prompt(
        self,
        context: DecisionContext,
        report: ProgressReport,
        recent_outcomes: list[dict] | None,
    ) -> str:
        """Build a structured prompt for the LLM."""
        lines = [
            "You are the NovaCore Autonomy Reasoning Engine. "
            "Analyze the current system state and recommend the single best action.",
            "",
            "## Current Scores",
        ]

        for name, dim in report.dimensions.items():
            trend = report.trends.get(name)
            trend_str = trend.direction if trend else "unknown"
            lines.append(f"- {name}: {dim.score:.0f}/100 ({dim.alert_level.value}, trend={trend_str})")
            if dim.warnings:
                for w in dim.warnings:
                    lines.append(f"  WARNING: {w}")

        lines.append(f"\nOverall: {report.overall_score:.0f}/100 ({report.overall_alert.value})")

        # Pending tasks
        if context.pending_tasks:
            lines.append("\n## Pending Tasks")
            for t in context.pending_tasks[:5]:
                lines.append(f"- [{t.status}] {t.title}")

        # Recent outcomes
        if recent_outcomes:
            lines.append("\n## Recent Decision Outcomes")
            for o in recent_outcomes[-5:]:
                lines.append(
                    f"- {o.get('mode', '?')}: delta={o.get('delta', '?')}, effectiveness={o.get('effectiveness', '?')}"
                )

        lines.extend(
            [
                "",
                "## Instructions",
                "1. Identify the most impactful gap or risk in the current state",
                "2. Recommend ONE action mode: RESEARCH, PLAN, EXECUTE, MONITOR, VALIDATE, or REPAIR",
                "3. Explain your reasoning step by step",
                "4. Suggest 2-3 specific actions",
                "5. Note any novel insight the rule-based system might miss",
                "",
                'Respond with JSON: {"mode": "...", "target_dimension": "...", '
                '"reasoning": ["step1", ...], "actions": ["action1", ...], '
                '"confidence": "high|medium|low", "novel_insight": "..."}',
            ]
        )

        return "\n".join(lines)

    async def _call_llm(
        self,
        prompt: str,
        report: ProgressReport,
        context: DecisionContext,
    ) -> ReasoningResult:
        """Call the LLM or fall back to heuristic reasoning.

        In production, this would call Claude via the cost_router.
        For now, we use structured heuristic reasoning that simulates
        what the LLM would return — this can be swapped for a real
        LLM call once the cost_router is wired.
        """
        # Heuristic reasoning (deterministic fallback)
        return self._heuristic_reason(report, context)

    def _heuristic_reason(
        self,
        report: ProgressReport,
        context: DecisionContext,
    ) -> ReasoningResult:
        """Structured heuristic reasoning — more nuanced than rule-based.

        This fills the gap between simple if/else rules and full LLM reasoning.
        It considers dimension interactions, trend momentum, and task saturation.
        """
        reasoning_chain: list[str] = []
        novel_insight: str | None = None

        # Analyze dimensions
        dims = report.dimensions
        trends = report.trends
        yellow_dims = [n for n, d in dims.items() if 40 <= d.score < 70]
        red_dims = [n for n, d in dims.items() if d.score < 40]
        degrading = [n for n, t in trends.items() if t.direction == "degrading"]
        improving = [n for n, t in trends.items() if t.direction == "improving"]

        # Check for correlated failures
        if len(yellow_dims) >= 3:
            reasoning_chain.append(
                f"Multiple dimensions in YELLOW ({', '.join(yellow_dims)}) — "
                "suggests a systemic issue rather than isolated failure."
            )
            novel_insight = (
                f"Correlated YELLOW across {len(yellow_dims)} dimensions may indicate "
                "a shared root cause (e.g., connectivity issue affecting multiple subsystems)."
            )
            return ReasoningResult(
                recommended_mode="research",
                target_dimension=yellow_dims[0],
                reasoning_chain=reasoning_chain,
                suggested_actions=[
                    "Investigate shared dependencies across affected dimensions",
                    f"Check if {yellow_dims[0]} and {yellow_dims[1]} share infrastructure",
                    "Run cross-dimension correlation analysis",
                ],
                confidence="medium",
                novel_insight=novel_insight,
            )

        # Check for stalled improvement
        if improving and not any(d.score >= 70 for n, d in dims.items() if n in improving):
            reasoning_chain.append(
                "Some dimensions show 'improving' trend but haven't reached GREEN — "
                "momentum may stall without intervention."
            )
            stalled = [n for n in improving if dims[n].score < 70]
            if stalled:
                target = stalled[0]
                return ReasoningResult(
                    recommended_mode="execute",
                    target_dimension=target,
                    reasoning_chain=reasoning_chain,
                    suggested_actions=[
                        f"Continue improvement actions on {target} to push past GREEN threshold",
                        f"Current {target} score: {dims[target].score:.0f} — target is 70",
                    ],
                    confidence="medium",
                    novel_insight=f"{target} is improving but may stall before GREEN threshold.",
                )

        # Check for degradation despite recent repairs
        recent_repairs = [d for d in context.recent_decisions if d.get("mode") == "repair"]
        if degrading and recent_repairs:
            reasoning_chain.append(
                "Dimensions are degrading despite recent REPAIR decisions — "
                "repairs may be treating symptoms, not root cause."
            )
            novel_insight = (
                "Repeated repairs without improvement suggests the fix strategy "
                "needs to change (e.g., config change instead of service restart)."
            )
            target = degrading[0]
            return ReasoningResult(
                recommended_mode="research",
                target_dimension=target,
                reasoning_chain=reasoning_chain,
                suggested_actions=[
                    f"Deep-dive investigation into {target} — repairs are ineffective",
                    "Review recent repair actions and their outcomes",
                    "Consider alternative remediation approaches",
                ],
                confidence="medium",
                novel_insight=novel_insight,
            )

        # Check for task saturation
        pending = context.pending_tasks
        if len(pending) > 10:
            reasoning_chain.append(
                f"Task queue is saturated ({len(pending)} pending tasks) — "
                "adding more tasks won't help, need to execute existing ones."
            )
            return ReasoningResult(
                recommended_mode="execute",
                target_dimension=None,
                reasoning_chain=reasoning_chain,
                suggested_actions=[
                    "Focus on completing existing tasks before generating new ones",
                    f"Top pending: {pending[0].title}",
                ],
                confidence="high",
                novel_insight=f"Task saturation: {len(pending)} pending tasks.",
            )

        # Default: system looks reasonable
        if red_dims:
            target = red_dims[0]
            reasoning_chain.append(f"{target} is RED ({dims[target].score:.0f}/100) — needs urgent attention.")
            return ReasoningResult(
                recommended_mode="repair",
                target_dimension=target,
                reasoning_chain=reasoning_chain,
                suggested_actions=[
                    f"Investigate and fix {target}",
                    f"Check {target} sub-metrics for specific failures",
                ],
                confidence="high",
            )

        if yellow_dims:
            target = min(yellow_dims, key=lambda n: dims[n].score)
            reasoning_chain.append(f"Weakest YELLOW dimension is {target} ({dims[target].score:.0f}/100).")
            return ReasoningResult(
                recommended_mode="plan",
                target_dimension=target,
                reasoning_chain=reasoning_chain,
                suggested_actions=[
                    f"Create improvement plan for {target}",
                    f"Target: raise {target} from {dims[target].score:.0f} to 70+",
                ],
                confidence="medium",
            )

        reasoning_chain.append("All dimensions are GREEN — system is healthy.")
        return ReasoningResult(
            recommended_mode="monitor",
            target_dimension=None,
            reasoning_chain=reasoning_chain,
            suggested_actions=[],
            confidence="high",
        )

    # -------------------------------------------------------------------
    # Last-score persistence (survives process restarts)
    # -------------------------------------------------------------------

    def _load_last_score(self) -> float | None:
        """Load the last reasoning score from disk."""
        path = self._state_dir / "reasoning_last_score.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return data.get("score")
        except (json.JSONDecodeError, OSError):
            return None

    def _save_last_score(self, score: float) -> None:
        """Save the current reasoning score to disk."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        path = self._state_dir / "reasoning_last_score.json"
        path.write_text(json.dumps({"score": score, "updated_at": datetime.now(timezone.utc).isoformat()}))

    # -------------------------------------------------------------------
    # Budget tracking
    # -------------------------------------------------------------------

    def _daily_budget_exhausted(self) -> bool:
        """Check if we've exceeded the daily reasoning call limit."""
        return self._count_today() >= self.config.max_calls_per_day

    def _count_today(self) -> int:
        """Count reasoning calls made today."""
        history = self._load_history()
        today = datetime.now(timezone.utc).date()
        count = 0
        for entry in history:
            ts = entry.get("timestamp", "")
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.date() == today:
                    count += 1
            except (ValueError, TypeError):
                pass
        return count

    # -------------------------------------------------------------------
    # Persistence
    # -------------------------------------------------------------------

    def _load_history(self) -> list[dict]:
        if not self._history_path.exists():
            return []
        try:
            data = json.loads(self._history_path.read_text())
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _persist_result(self, result: ReasoningResult) -> None:
        """Append result to reasoning history."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        history = self._load_history()
        history.append(asdict(result))
        history = history[-100:]  # keep last 100

        fd, tmp_path = tempfile.mkstemp(dir=str(self._state_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(history, f, indent=2, default=str)
            os.replace(tmp_path, str(self._history_path))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
