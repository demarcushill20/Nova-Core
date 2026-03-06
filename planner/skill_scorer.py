"""Deterministic 4-component skill scorer for NovaCore planner.

Implements the roadmap formula:
  score = semantic_match + activation_rules + recency + success_rate

Component ranges:
  semantic_match:  0.0–0.4  (keyword overlap with description)
  activation_rules: 0.0–0.2  (activation keyword matches)
  recency:         0.0–0.2  (time since last use)
  success_rate:    0.0–0.2  (historical success ratio)

No embeddings — deterministic substring/keyword overlap only.
"""

from __future__ import annotations

import re

from planner.schemas import SkillScore, TaskIntent
from planner.skill_history import SkillHistoryStore

# ---------------------------------------------------------------------------
# Default skills catalog
#
# Each entry: name, description, keywords (activation conditions).
# ---------------------------------------------------------------------------

DEFAULT_SKILLS_CATALOG: list[dict] = [
    {
        "name": "log_triage",
        "description": "Diagnose and triage errors from log files and tracebacks",
        "keywords": [
            "diagnose", "logs", "failure", "error", "traceback",
            "crash", "exception", "debug", "investigate", "why did",
            "root cause", "stack trace", "stderr", "log file",
        ],
    },
    {
        "name": "code_improve",
        "description": "Fix bugs, apply patches, refactor and improve code quality",
        "keywords": [
            "fix", "patch", "change code", "refactor", "improve",
            "clean up", "code quality", "lint", "bug", "typo",
            "rename", "simplify", "optimize", "dead code",
        ],
    },
    {
        "name": "service_ops",
        "description": "Manage systemd services: restart, check status, monitor health",
        "keywords": [
            "restart", "service", "status", "systemctl", "daemon",
            "reload", "enable", "disable", "uptime", "health check",
            "process", "pid", "systemd",
        ],
    },
    {
        "name": "system_supervisor",
        "description": "Validate outputs, check contracts, audit compliance, review results",
        "keywords": [
            "validate", "verify", "contract", "check", "audit",
            "supervisor", "review", "inspect", "compliance",
        ],
    },
    {
        "name": "task_execution",
        "description": "Execute and manage task queue, dispatch workflows, process work items",
        "keywords": [
            "task", "execute", "run task", "process task", "queue",
            "dispatch", "workflow",
        ],
    },
    {
        "name": "file_ops",
        "description": "Create, read, write, edit, move, and rename files",
        "keywords": [
            "create file", "write file", "edit file", "move file",
            "rename file", "read file", "copy file",
        ],
    },
    {
        "name": "shell_ops",
        "description": "Run shell commands, execute scripts, manage terminal operations",
        "keywords": [
            "run command", "shell", "bash", "script", "pip install",
            "execute command", "terminal",
        ],
    },
    {
        "name": "web_research",
        "description": "Search the web, find information, research topics, look up documentation",
        "keywords": [
            "research", "search", "find information", "look up",
            "web search", "google", "what is", "how to",
        ],
    },
]


class SkillScorer:
    """Score skills against a task intent using the 4-component model."""

    def score_skill(
        self,
        intent: TaskIntent,
        skill_meta: dict,
        history_store: SkillHistoryStore,
    ) -> SkillScore:
        """Score a single skill against an intent.

        Returns a SkillScore with all four component scores and total.
        """
        goal_lower = intent.goal.lower()
        skill_name = skill_meta["name"]

        # 1. semantic_match (0.0–0.4): word overlap with description
        desc_words = set(re.findall(r"\w+", skill_meta.get("description", "").lower()))
        goal_words = set(re.findall(r"\w+", goal_lower))
        if desc_words:
            overlap = len(goal_words & desc_words) / len(desc_words)
            semantic_match = round(min(0.4, overlap * 0.4), 4)
        else:
            semantic_match = 0.0

        # 2. activation_rules (0.0–0.2): activation keyword substring matches
        keywords = skill_meta.get("keywords", [])
        matched_keywords: list[str] = []
        if keywords:
            matched_keywords = [kw for kw in keywords if kw.lower() in goal_lower]
            activation_rules = round(
                min(0.2, (len(matched_keywords) / len(keywords)) * 0.2), 4
            )
        else:
            activation_rules = 0.0

        # 3. recency (0.0–0.2): scaled from history recency score
        recency_raw = history_store.get_recency_score(skill_name)
        recency = round(recency_raw * 0.2, 4)

        # 4. success_rate (0.0–0.2): scaled from history success rate
        sr_raw = history_store.get_success_rate(skill_name)
        success_rate = round(sr_raw * 0.2, 4)

        total_score = round(
            semantic_match + activation_rules + recency + success_rate, 4
        )

        # Build reasons list
        reasons: list[str] = []
        if semantic_match > 0:
            reasons.append(f"semantic_match={semantic_match}")
        if activation_rules > 0:
            reasons.append(
                f"activation: {', '.join(matched_keywords)}"
            )
        if recency_raw != 0.5:
            reasons.append(f"recency={recency} (raw={recency_raw:.3f})")
        if sr_raw != 0.5:
            reasons.append(f"success_rate={success_rate} (raw={sr_raw:.3f})")

        return SkillScore(
            skill_name=skill_name,
            semantic_match=semantic_match,
            activation_rules=activation_rules,
            recency=recency,
            success_rate=success_rate,
            total_score=total_score,
            reasons=reasons,
        )

    def rank_skills(
        self,
        intent: TaskIntent,
        skills_catalog: list[dict],
        history_store: SkillHistoryStore,
    ) -> list[SkillScore]:
        """Score and rank all skills. Returns descending by total_score.

        Only includes skills with nonzero semantic_match or activation_rules.
        """
        scores: list[SkillScore] = []
        for meta in skills_catalog:
            score = self.score_skill(intent, meta, history_store)
            if score.semantic_match > 0 or score.activation_rules > 0:
                scores.append(score)
        scores.sort(key=lambda s: (-s.total_score, s.skill_name))
        return scores
