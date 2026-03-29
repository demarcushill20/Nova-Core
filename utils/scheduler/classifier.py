"""Heuristic task classifier for the scheduler.

Maps task metadata (category, effort level, title keywords) to WorkMode,
TaskClass, and three-point duration estimates.  Pure heuristics — no LLM
calls.  Phase 2+ will layer on learning-based refinement.
"""

from __future__ import annotations

import re

from utils.scheduler.work_unit import TaskClass, WorkMode

# ---------------------------------------------------------------------------
# Category -> WorkMode mapping
# ---------------------------------------------------------------------------

_CATEGORY_MODE_MAP: dict[str, WorkMode] = {
    "research": WorkMode.RESEARCH,
    "plan": WorkMode.ANALYSIS,
    "planning": WorkMode.ANALYSIS,
    "analyze": WorkMode.ANALYSIS,
    "analysis": WorkMode.ANALYSIS,
    "execute": WorkMode.DEEP_IMPLEMENTATION,
    "implementation": WorkMode.DEEP_IMPLEMENTATION,
    "repair": WorkMode.DEBUGGING,
    "fix": WorkMode.DEBUGGING,
    "debug": WorkMode.DEBUGGING,
    "debugging": WorkMode.DEBUGGING,
    "validate": WorkMode.REVIEW,
    "review": WorkMode.REVIEW,
    "test": WorkMode.REVIEW,
    "testing": WorkMode.REVIEW,
    "monitor": WorkMode.MONITORING,
    "monitoring": WorkMode.MONITORING,
    "deploy": WorkMode.DEPLOYMENT,
    "deployment": WorkMode.DEPLOYMENT,
    "communicate": WorkMode.COMMUNICATION,
    "communication": WorkMode.COMMUNICATION,
    "admin": WorkMode.ADMIN,
}

# ---------------------------------------------------------------------------
# Effort -> TaskClass + duration defaults
# ---------------------------------------------------------------------------

_EFFORT_DEFAULTS: dict[str, dict] = {
    "light": {
        "task_class": TaskClass.ATOMIC,
        "estimated_optimistic_min": 5.0,
        "estimated_expected_min": 12.0,
        "estimated_pessimistic_min": 20.0,
    },
    "medium": {
        "task_class": TaskClass.BOUNDED,
        "estimated_optimistic_min": 20.0,
        "estimated_expected_min": 45.0,
        "estimated_pessimistic_min": 90.0,
    },
    "heavy": {
        "task_class": TaskClass.EPIC,
        "estimated_optimistic_min": 60.0,
        "estimated_expected_min": 120.0,
        "estimated_pessimistic_min": 240.0,
    },
}

_DEFAULT_EFFORT: dict = {
    "task_class": TaskClass.BOUNDED,
    "estimated_optimistic_min": 15.0,
    "estimated_expected_min": 30.0,
    "estimated_pessimistic_min": 60.0,
}

# ---------------------------------------------------------------------------
# Keyword patterns for title-based refinement
# ---------------------------------------------------------------------------

_KEYWORD_MODE_PATTERNS: list[tuple[re.Pattern[str], WorkMode]] = [
    (re.compile(r"\b(?:fix|bug|broken|error|crash|fault)\b", re.IGNORECASE), WorkMode.DEBUGGING),
    (re.compile(r"\b(?:research|investigate|explore|study)\b", re.IGNORECASE), WorkMode.RESEARCH),
    (re.compile(r"\b(?:deploy|release|rollout|ship|publish)\b", re.IGNORECASE), WorkMode.DEPLOYMENT),
    (re.compile(r"\b(?:monitor|health|status|watch|alert)\b", re.IGNORECASE), WorkMode.MONITORING),
    (re.compile(r"\b(?:plan|assess|evaluate|prioritize|design|architect|analyze)\b", re.IGNORECASE), WorkMode.ANALYSIS),
    (re.compile(r"\b(?:review|audit|check|inspect|validate)\b", re.IGNORECASE), WorkMode.REVIEW),
    (re.compile(r"\b(?:send|notify|report|communicate|email|message)\b", re.IGNORECASE), WorkMode.COMMUNICATION),
    (re.compile(r"\b(?:cleanup|hygiene|archive|prune|tidy)\b", re.IGNORECASE), WorkMode.ADMIN),
    (re.compile(r"\b(?:implement|build|create|add|develop|code)\b", re.IGNORECASE), WorkMode.DEEP_IMPLEMENTATION),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_task(
    title: str,
    body: str = "",
    category: str = "",
    estimated_effort: str = "",
) -> dict:
    """Classify a task using pure heuristics.

    Returns a dict with:
        work_mode: WorkMode
        task_class: TaskClass
        estimated_optimistic_min: float
        estimated_expected_min: float
        estimated_pessimistic_min: float
        confidence: float
        epic_candidate: bool  (True if expected >= 90 min)

    Parameters
    ----------
    title : str
        Task title (used for keyword matching).
    body : str
        Task body/description (used for confidence scoring).
    category : str
        Existing category field (e.g. "research", "execute").
    estimated_effort : str
        Existing effort field (e.g. "light", "medium", "heavy").
    """
    # --- Step 1: Determine work_mode from category ---
    cat = category.strip().lower()
    category_mode = _CATEGORY_MODE_MAP.get(cat)
    work_mode = category_mode if category_mode is not None else WorkMode.ADMIN

    # --- Step 2: Keyword refinement from title ---
    # Keywords override category-based mode when title is more specific
    keyword_matched = False
    if title:
        for pattern, kw_mode in _KEYWORD_MODE_PATTERNS:
            if pattern.search(title):
                work_mode = kw_mode
                keyword_matched = True
                break  # First match wins

    # --- Step 3: Duration estimates from effort level ---
    effort = estimated_effort.strip().lower()
    effort_info = _EFFORT_DEFAULTS.get(effort, _DEFAULT_EFFORT)

    task_class: TaskClass = effort_info["task_class"]
    opt = effort_info["estimated_optimistic_min"]
    exp = effort_info["estimated_expected_min"]
    pess = effort_info["estimated_pessimistic_min"]

    # Flag EPIC candidates (expected >= 90 min)
    epic_candidate = exp >= 90.0

    # --- Step 4: Confidence based on input completeness ---
    has_title = bool(title.strip())
    has_body = bool(body.strip())
    has_category = bool(cat)
    has_effort = bool(effort)

    if has_title and has_body and has_category and has_effort:
        confidence = 0.7
    elif (has_title and (has_body or has_effort)) or (has_title and has_category):
        confidence = 0.5
    elif has_title:
        confidence = 0.3
    else:
        confidence = 0.2

    return {
        "work_mode": work_mode,
        "task_class": task_class,
        "estimated_optimistic_min": opt,
        "estimated_expected_min": exp,
        "estimated_pessimistic_min": pess,
        "confidence": confidence,
        "epic_candidate": epic_candidate,
        "keyword_matched": keyword_matched,
    }
