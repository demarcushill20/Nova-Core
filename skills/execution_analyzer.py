"""Execution analyzer for skill feedback loop.

Phase 2 of Self-Evolving Skills — analyzes skill execution outcomes
to produce structured feedback and update version store stats.

Tries LLM analysis first, falls back to deterministic scoring.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from skills.execution_analysis import (
    EvolutionSuggestion,
    ExecutionAnalysis,
    SkillJudgment,
)

logger = logging.getLogger(__name__)


class ExecutionAnalyzer:
    """Analyzes skill execution outcomes to produce structured feedback."""

    def __init__(self, version_store=None, use_cache: bool = True):
        self.version_store = version_store  # SkillVersionStore instance
        self.use_cache = use_cache

    def analyze(
        self,
        task_id: str,
        task_description: str,
        selected_skills: list[str],  # skill names
        execution_trace: str,  # worker log tail
        outcome: dict,  # {"success": bool, "exit_code": int, ...}
    ) -> ExecutionAnalysis:
        """Analyze execution and produce structured feedback.

        Tries LLM analysis first, falls back to deterministic scoring.
        """
        try:
            return self._llm_analyze(
                task_id, task_description, selected_skills, execution_trace, outcome
            )
        except Exception as e:
            logger.warning("LLM analysis failed, using deterministic fallback: %s", e)
            return self._deterministic_analyze(
                task_id, task_description, selected_skills, outcome
            )

    def _llm_analyze(
        self, task_id, task_description, selected_skills, execution_trace, outcome
    ) -> ExecutionAnalysis:
        """LLM-driven analysis with caching."""
        # Build prompts
        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(
            task_description, selected_skills, execution_trace, outcome
        )

        # C1 FIX: Prepend system prompt to user prompt so it is actually sent
        # to the LLM. The claude CLI --print mode uses a single prompt argument;
        # embedding the system prompt with a separator is the simplest approach.
        full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"

        import subprocess

        def call_fn():
            result = subprocess.run(
                ["claude", "--print", "-p", full_prompt, "--output-format", "json"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Claude call failed: {result.stderr[:500]}")
            try:
                data = json.loads(result.stdout)
                return data.get("result", result.stdout)
            except json.JSONDecodeError:
                return result.stdout

        # H2 FIX: When use_cache=False, call the LLM directly without caching
        if self.use_cache:
            from utils.llm_cache import cached_llm_call

            cache_result = cached_llm_call(
                call_fn=call_fn,
                model="claude-sonnet-4-20250514",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.0,
            )
            response_text = cache_result.get("response", "")
        else:
            response_text = call_fn() or ""

        # Parse with structured_output
        from utils.structured_output import parse_and_validate

        analysis = parse_and_validate(response_text, ExecutionAnalysis)

        # H4 FIX: Single fallback path — raise so analyze() catches and falls back.
        # Previously there was an inner fallback here that created a second
        # deterministic path, making error tracing harder.
        if analysis is None:
            raise ValueError("Structured extraction returned None — LLM response unparseable")

        # Ensure task_id is set
        analysis.task_id = task_id
        analysis.task_description = task_description
        analysis.analysis_timestamp = datetime.now(timezone.utc).isoformat()
        analysis.raw_response = response_text[:2000]

        return analysis

    def _deterministic_analyze(
        self, task_id, task_description, selected_skills, outcome
    ) -> ExecutionAnalysis:
        """Deterministic fallback when LLM is unavailable.

        H3 NOTE: Known limitation — the deterministic path has no per-skill
        signal. All skills in a single execution share the same success/failure
        outcome because without the LLM we cannot distinguish which skill
        contributed to success or caused a failure. The quality_score and
        completed flag are applied uniformly from the task-level outcome.
        """
        success = outcome.get("success", False)
        exit_code = outcome.get("exit_code", 1)

        judgments = []
        suggestions = []

        for skill_name in selected_skills:
            # Look up skill_id from version store
            skill_id = ""
            if self.version_store:
                sv = self.version_store.get_active_version(skill_name)
                if sv:
                    skill_id = sv.skill_id

            judgment = SkillJudgment(
                skill_name=skill_name,
                skill_id=skill_id,
                applied=True,  # assume applied if selected (conservative)
                completed=success,
                failure_reason="" if success else f"Task failed with exit_code={exit_code}",
                quality_score=0.8 if success else 0.2,
            )
            judgments.append(judgment)

            # Suggest FIX for failed skills
            if not success and skill_id:
                suggestions.append(
                    EvolutionSuggestion(
                        type="FIX",
                        target_skill_name=skill_name,
                        target_skill_id=skill_id,
                        direction=f"Skill failed during task execution (exit_code={exit_code}). Investigate and fix.",
                        priority=2,
                    )
                )

        return ExecutionAnalysis(
            task_id=task_id,
            task_description=task_description,
            skill_judgments=judgments,
            evolution_suggestions=suggestions,
            overall_quality=0.8 if success else 0.2,
            analysis_timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def _build_system_prompt(self) -> str:
        return (
            "You are an execution analyzer for an AI agent skill system. "
            "Given a task, the skills that were selected, the execution trace, "
            "and the outcome, produce a structured analysis.\n\n"
            "Output valid JSON matching this schema:\n"
            "{\n"
            '  "task_id": "string",\n'
            '  "skill_judgments": [\n'
            "    {\n"
            '      "skill_name": "string",\n'
            '      "applied": bool,\n'
            '      "completed": bool,\n'
            '      "failure_reason": "string (empty if completed)",\n'
            '      "quality_score": float (0.0-1.0),\n'
            '      "tool_issues": ["string"]\n'
            "    }\n"
            "  ],\n"
            '  "evolution_suggestions": [\n'
            "    {\n"
            '      "type": "FIX" | "DERIVED" | "CAPTURED",\n'
            '      "target_skill_name": "string",\n'
            '      "direction": "string describing what to change",\n'
            '      "priority": int (1-5, 1=highest)\n'
            "    }\n"
            "  ],\n"
            '  "overall_quality": float (0.0-1.0)\n'
            "}\n\n"
            "Rules:\n"
            "- applied=true means the skill was actually used during execution "
            "(not just selected)\n"
            "- completed=true means the skill-guided work finished successfully\n"
            "- quality_score reflects how well the skill performed "
            "(0=terrible, 1=perfect)\n"
            "- Suggest FIX when a skill consistently fails or has clear defects\n"
            "- Suggest DERIVED when a skill works but could be specialized "
            "for a pattern\n"
            "- Suggest CAPTURED when successful execution used a novel pattern "
            "not covered by any skill\n"
            "- priority 1 = urgent fix needed, 5 = minor enhancement"
        )

    def _build_user_prompt(
        self, task_description, selected_skills, execution_trace, outcome
    ) -> str:
        # M2 FIX: Take the LAST 4000 chars (tail) — the end of the trace
        # contains the most relevant information (final errors, exit status).
        trace_truncated = execution_trace[-4000:] if execution_trace else "(no trace)"
        skills_str = ", ".join(selected_skills) if selected_skills else "(none)"
        outcome_str = json.dumps(outcome, default=str)[:1000]

        return (
            f"Task: {task_description[:1000]}\n\n"
            f"Selected Skills: {skills_str}\n\n"
            f"Execution Trace (tail, last 4000 chars):\n{trace_truncated}\n\n"
            f"Outcome: {outcome_str}\n\n"
            "Analyze each skill's contribution and suggest evolution actions "
            "if appropriate."
        )

    def update_stats(self, analysis: ExecutionAnalysis) -> None:
        """Update SkillVersionStore counters from analysis results."""
        if not self.version_store:
            return

        for judgment in analysis.skill_judgments:
            if not judgment.skill_id:
                continue
            try:
                if judgment.applied:
                    self.version_store.increment_stat(judgment.skill_id, "executions")
                    if judgment.completed:
                        self.version_store.increment_stat(
                            judgment.skill_id, "completions"
                        )
                    else:
                        self.version_store.increment_stat(
                            judgment.skill_id, "failures"
                        )
                else:
                    self.version_store.increment_stat(judgment.skill_id, "fallbacks")
            except Exception as e:
                logger.warning(
                    "Failed to update stats for %s: %s", judgment.skill_id, e
                )
