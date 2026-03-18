"""Quality scoring for NovaCore agent outputs.

Phase 7 Step 7.7 — LLM-as-Judge with deterministic fallback.

Provides rubric-based quality scoring for different output categories.
When LLM access is available, uses DeepEval GEval for semantic scoring.
Falls back to deterministic keyword/structural scoring otherwise.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

_log = logging.getLogger("nova.quality_scorer")

# ------------------------------------------------------------------
# Quality rubrics per output category
# ------------------------------------------------------------------

RUBRIC: dict[str, list[str]] = {
    "heartbeat": [
        "Contains system health metrics",
        "Lists actionable items",
        "Identifies degradation trends",
    ],
    "codegen": [
        "Code is syntactically valid Python",
        "Includes test coverage",
        "Follows existing code patterns",
    ],
    "research": [
        "Citations are present",
        "Findings are structured",
        "Recommendations are actionable",
    ],
    "plan": [
        "Phases are clearly defined",
        "Dependencies are identified",
        "Estimates are provided",
    ],
    "bugfix": [
        "Root cause is identified",
        "Fix is described",
        "Tests are included",
    ],
    "novatrade": [
        "Risk parameters are specified",
        "Entry/exit rules are clear",
        "FTMO compliance is addressed",
    ],
}

# ------------------------------------------------------------------
# Quality thresholds
# ------------------------------------------------------------------

THRESHOLDS: dict[str, dict[str, float]] = {
    "heartbeat": {"green": 0.8, "yellow": 0.6, "red": 0.0},
    "codegen": {"green": 0.85, "yellow": 0.7, "red": 0.0},
    "research": {"green": 0.75, "yellow": 0.6, "red": 0.0},
    "plan": {"green": 0.8, "yellow": 0.65, "red": 0.0},
    "bugfix": {"green": 0.8, "yellow": 0.65, "red": 0.0},
    "novatrade": {"green": 0.85, "yellow": 0.7, "red": 0.0},
}


# ------------------------------------------------------------------
# Data class for results
# ------------------------------------------------------------------


@dataclass
class QualityScore:
    """Result of quality scoring."""

    category: str
    score: float  # 0.0 to 1.0
    rating: str  # "green", "yellow", "red"
    criteria_met: list[str] = field(default_factory=list)
    criteria_missed: list[str] = field(default_factory=list)
    method: str = "deterministic"  # "deterministic" or "llm"


# ------------------------------------------------------------------
# Helpers — deterministic criterion checkers
# ------------------------------------------------------------------

# Patterns that indicate health metrics (numbers, percentages, status words)
_METRIC_PATTERNS = [
    re.compile(r"\d+(\.\d+)?%"),  # percentages
    re.compile(r"\b(cpu|memory|disk|ram|uptime|latency|load)\b", re.IGNORECASE),
    re.compile(r"\b\d+(\.\d+)?\s*(ms|MB|GB|s|sec)\b", re.IGNORECASE),
    re.compile(r"\b(healthy|degraded|critical|ok|error|warning)\b", re.IGNORECASE),
]

# Patterns for actionable items
_ACTION_PATTERNS = [
    re.compile(r"\b(TODO|FIXME|ACTION|NEXT|should|must|need to|fix|resolve|investigate)\b", re.IGNORECASE),
    re.compile(r"^\s*[-*]\s+", re.MULTILINE),  # bullet lists
    re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE),  # numbered lists
]

# Patterns for degradation trends
_TREND_PATTERNS = [
    re.compile(
        r"\b(trend\w*|increas\w*|decreas\w*|spike\w*|drop\w*|degrad\w*|drift\w*|declin\w*|rising|falling)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(↑|↓|→|▲|▼)", re.IGNORECASE),
    re.compile(r"\b(worse|better|stable|unstable|improv\w*|worsen\w*|upward|downward)\b", re.IGNORECASE),
]

# Patterns for test coverage
_TEST_PATTERNS = [
    re.compile(r"\b(test_|pytest|unittest|assert|mock|fixture)\b", re.IGNORECASE),
    re.compile(r"\bdef\s+test_\w+", re.IGNORECASE),
    re.compile(r"\b(coverage|tested|tests pass)\b", re.IGNORECASE),
]

# Patterns for code patterns (imports, classes, docstrings)
_CODE_PATTERN_PATTERNS = [
    re.compile(r"^\s*(import|from)\s+", re.MULTILINE),
    re.compile(r"^\s*class\s+\w+", re.MULTILINE),
    re.compile(r'""".*?"""', re.DOTALL),
    re.compile(r"^\s*def\s+\w+", re.MULTILINE),
]

# Patterns for citations / references
_CITATION_PATTERNS = [
    re.compile(r"https?://\S+"),
    re.compile(r"\[[\d]+\]"),  # [1], [2] style references
    re.compile(r"\b(source|reference|citation|according to|per|see)\b", re.IGNORECASE),
    re.compile(r"\([\w\s]+,\s*\d{4}\)"),  # (Author, 2024)
]

# Patterns for structured findings
_STRUCTURED_PATTERNS = [
    re.compile(r"^#+\s+", re.MULTILINE),  # markdown headings
    re.compile(r"^\s*[-*]\s+", re.MULTILINE),  # bullet lists
    re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE),  # numbered lists
    re.compile(r"\b(finding|result|conclusion|summary|key takeaway)\b", re.IGNORECASE),
]

# Patterns for actionable recommendations
_RECOMMENDATION_PATTERNS = [
    re.compile(r"\b(recommend|suggest|should|consider|propose|advise|next step)\b", re.IGNORECASE),
    re.compile(r"\b(action|implement|adopt|migrate|switch to|use)\b", re.IGNORECASE),
]

# Patterns for phases / milestones
_PHASE_PATTERNS = [
    re.compile(r"\b(phase|step|stage|milestone|sprint)\s*\d+", re.IGNORECASE),
    re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE),  # numbered lists
    re.compile(r"\b(first|second|third|then|next|finally|after)\b", re.IGNORECASE),
]

# Patterns for dependencies
_DEPENDENCY_PATTERNS = [
    re.compile(r"\b(depend|requires|prerequisite|blocks|blocked by|before|after)\b", re.IGNORECASE),
    re.compile(r"\b(upstream|downstream)\b", re.IGNORECASE),
    re.compile(r"→|->|=>", re.IGNORECASE),
]

# Patterns for estimates
_ESTIMATE_PATTERNS = [
    re.compile(r"\b\d+\s*(hour|day|week|sprint|month|min)\w*\b", re.IGNORECASE),
    re.compile(r"\b(estimate|ETA|timeline|deadline|target date|by)\b", re.IGNORECASE),
    re.compile(r"\b(small|medium|large|XL|S|M|L)\b"),  # T-shirt sizes
]

# Patterns for root cause
_ROOT_CAUSE_PATTERNS = [
    re.compile(r"\b(root cause|caused by|because|due to|origin|source of|reason)\b", re.IGNORECASE),
    re.compile(r"\b(bug|issue|problem|defect|regression|fault)\b", re.IGNORECASE),
]

# Patterns for fix description
_FIX_PATTERNS = [
    re.compile(r"\b(fix|patch|resolve|solution|workaround|corrected|changed|updated)\b", re.IGNORECASE),
    re.compile(r"\b(before|after)\b.*\b(change|fix|update)\b", re.IGNORECASE),
    re.compile(r"```", re.MULTILINE),  # code blocks showing the fix
]

# Patterns for risk parameters
_RISK_PATTERNS = [
    re.compile(r"\b(risk|stop.?loss|take.?profit|drawdown|max.?loss|position.?size|lot)\b", re.IGNORECASE),
    re.compile(r"\b\d+(\.\d+)?%\s*(risk|drawdown|loss)\b", re.IGNORECASE),
    re.compile(r"\b(R:R|risk.?reward|risk.?ratio)\b", re.IGNORECASE),
]

# Patterns for entry/exit rules
_ENTRY_EXIT_PATTERNS = [
    re.compile(r"\b(entry|exit|open|close|buy|sell|long|short|signal|trigger)\b", re.IGNORECASE),
    re.compile(r"\b(condition|rule|criteria|when|if)\b.*\b(entry|exit|buy|sell)\b", re.IGNORECASE),
    re.compile(r"\b(SMA|EMA|RSI|MACD|breakout|crossover|pullback)\b", re.IGNORECASE),
]

# Patterns for FTMO compliance
_FTMO_PATTERNS = [
    re.compile(r"\b(FTMO|prop.?firm|funded|challenge|verification)\b", re.IGNORECASE),
    re.compile(r"\b(daily.?loss|max.?drawdown|profit.?target|trading.?days)\b", re.IGNORECASE),
    re.compile(r"\b(compliance|rule|limit|constraint)\b", re.IGNORECASE),
]


def _match_any(text: str, patterns: list[re.Pattern]) -> bool:
    """Return True if any pattern matches in the text."""
    return any(p.search(text) for p in patterns)


# ------------------------------------------------------------------
# Per-criterion deterministic checkers
# ------------------------------------------------------------------

# Maps (category, criterion_text) -> checker function.
# Each checker takes (input_text, output_text) and returns bool.

_CRITERION_CHECKERS: dict[tuple[str, str], object] = {}


def _register(category: str, criterion: str):
    """Decorator to register a criterion checker."""

    def decorator(fn):
        _CRITERION_CHECKERS[(category, criterion)] = fn
        return fn

    return decorator


# --- heartbeat ---


@_register("heartbeat", "Contains system health metrics")
def _hb_metrics(_inp: str, out: str) -> bool:
    return sum(1 for p in _METRIC_PATTERNS if p.search(out)) >= 2


@_register("heartbeat", "Lists actionable items")
def _hb_actions(_inp: str, out: str) -> bool:
    return _match_any(out, _ACTION_PATTERNS)


@_register("heartbeat", "Identifies degradation trends")
def _hb_trends(_inp: str, out: str) -> bool:
    return _match_any(out, _TREND_PATTERNS)


# --- codegen ---


@_register("codegen", "Code is syntactically valid Python")
def _cg_syntax(_inp: str, out: str) -> bool:
    # Try to find Python code in the output (fenced blocks or raw)
    from utils.ast_validator import extract_python_blocks, validate_python

    blocks = extract_python_blocks(out)
    if blocks:
        return all(validate_python(b)[0] for b in blocks)
    # If no fenced blocks, try the whole output
    valid, _ = validate_python(out)
    return valid


@_register("codegen", "Includes test coverage")
def _cg_tests(_inp: str, out: str) -> bool:
    return _match_any(out, _TEST_PATTERNS)


@_register("codegen", "Follows existing code patterns")
def _cg_patterns(_inp: str, out: str) -> bool:
    return sum(1 for p in _CODE_PATTERN_PATTERNS if p.search(out)) >= 2


# --- research ---


@_register("research", "Citations are present")
def _res_citations(_inp: str, out: str) -> bool:
    return _match_any(out, _CITATION_PATTERNS)


@_register("research", "Findings are structured")
def _res_structured(_inp: str, out: str) -> bool:
    return sum(1 for p in _STRUCTURED_PATTERNS if p.search(out)) >= 2


@_register("research", "Recommendations are actionable")
def _res_recs(_inp: str, out: str) -> bool:
    return _match_any(out, _RECOMMENDATION_PATTERNS)


# --- plan ---


@_register("plan", "Phases are clearly defined")
def _plan_phases(_inp: str, out: str) -> bool:
    return _match_any(out, _PHASE_PATTERNS)


@_register("plan", "Dependencies are identified")
def _plan_deps(_inp: str, out: str) -> bool:
    return _match_any(out, _DEPENDENCY_PATTERNS)


@_register("plan", "Estimates are provided")
def _plan_estimates(_inp: str, out: str) -> bool:
    return _match_any(out, _ESTIMATE_PATTERNS)


# --- bugfix ---


@_register("bugfix", "Root cause is identified")
def _bug_root(_inp: str, out: str) -> bool:
    return _match_any(out, _ROOT_CAUSE_PATTERNS)


@_register("bugfix", "Fix is described")
def _bug_fix(_inp: str, out: str) -> bool:
    return _match_any(out, _FIX_PATTERNS)


@_register("bugfix", "Tests are included")
def _bug_tests(_inp: str, out: str) -> bool:
    return _match_any(out, _TEST_PATTERNS)


# --- novatrade ---


@_register("novatrade", "Risk parameters are specified")
def _nt_risk(_inp: str, out: str) -> bool:
    return _match_any(out, _RISK_PATTERNS)


@_register("novatrade", "Entry/exit rules are clear")
def _nt_entry_exit(_inp: str, out: str) -> bool:
    return _match_any(out, _ENTRY_EXIT_PATTERNS)


@_register("novatrade", "FTMO compliance is addressed")
def _nt_ftmo(_inp: str, out: str) -> bool:
    return _match_any(out, _FTMO_PATTERNS)


# ------------------------------------------------------------------
# Rating computation
# ------------------------------------------------------------------


def _compute_rating(score: float, category: str) -> str:
    """Map a numeric score to a color rating using category thresholds."""
    thresholds = THRESHOLDS.get(category, {"green": 0.8, "yellow": 0.6, "red": 0.0})
    if score >= thresholds["green"]:
        return "green"
    if score >= thresholds["yellow"]:
        return "yellow"
    return "red"


# ------------------------------------------------------------------
# Deterministic scoring (always available)
# ------------------------------------------------------------------


def score_output_deterministic(category: str, input_text: str, output_text: str) -> QualityScore:
    """Score output quality using keyword/structural matching.

    For each rubric criterion in the given category, applies a
    deterministic checker (regex/structural) to see if the output
    meets that criterion.

    Args:
        category: One of the RUBRIC keys (e.g. "heartbeat", "codegen").
        input_text: The input/prompt that produced the output.
        output_text: The agent output to evaluate.

    Returns:
        A QualityScore with method="deterministic".

    Raises:
        ValueError: If category is not in RUBRIC.
    """
    if category not in RUBRIC:
        raise ValueError(f"Unknown category {category!r}. Valid categories: {sorted(RUBRIC.keys())}")

    criteria = RUBRIC[category]

    # Empty output always scores zero
    if not output_text or not output_text.strip():
        return QualityScore(
            category=category,
            score=0.0,
            rating="red",
            criteria_met=[],
            criteria_missed=list(criteria),
            method="deterministic",
        )

    met: list[str] = []
    missed: list[str] = []

    for criterion in criteria:
        checker = _CRITERION_CHECKERS.get((category, criterion))
        if checker is not None:
            try:
                if checker(input_text, output_text):
                    met.append(criterion)
                else:
                    missed.append(criterion)
            except Exception:
                _log.warning(
                    "Criterion checker failed for %s/%s",
                    category,
                    criterion,
                    exc_info=True,
                )
                missed.append(criterion)
        else:
            # No specific checker — conservative: mark as missed
            _log.debug("No deterministic checker for %s/%s", category, criterion)
            missed.append(criterion)

    total = len(criteria)
    score = len(met) / total if total > 0 else 0.0

    # Clamp to [0.0, 1.0]
    score = max(0.0, min(1.0, score))

    rating = _compute_rating(score, category)

    return QualityScore(
        category=category,
        score=score,
        rating=rating,
        criteria_met=met,
        criteria_missed=missed,
        method="deterministic",
    )


# ------------------------------------------------------------------
# LLM scoring (optional, when OPENAI_API_KEY is set)
# ------------------------------------------------------------------


async def score_output_llm(category: str, input_text: str, output_text: str) -> QualityScore:
    """Score output quality using DeepEval GEval (LLM-as-Judge).

    Uses GEval with the rubric criteria as evaluation criteria.
    Requires OPENAI_API_KEY to be set in the environment.

    Args:
        category: One of the RUBRIC keys.
        input_text: The input/prompt that produced the output.
        output_text: The agent output to evaluate.

    Returns:
        A QualityScore with method="llm".

    Raises:
        ValueError: If category is not in RUBRIC.
        RuntimeError: If OPENAI_API_KEY is not set or DeepEval fails.
    """
    if category not in RUBRIC:
        raise ValueError(f"Unknown category {category!r}. Valid categories: {sorted(RUBRIC.keys())}")

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set. Cannot use LLM scoring.")

    # Empty output
    if not output_text or not output_text.strip():
        return QualityScore(
            category=category,
            score=0.0,
            rating="red",
            criteria_met=[],
            criteria_missed=list(RUBRIC[category]),
            method="llm",
        )

    # Lazy import to avoid loading DeepEval when not needed
    from deepeval.metrics import GEval
    from deepeval.test_case import LLMTestCase, LLMTestCaseParams

    criteria = RUBRIC[category]
    criteria_text = "\n".join(f"- {c}" for c in criteria)

    evaluation_criteria = (
        f"Evaluate the output for category '{category}' against these criteria:\n"
        f"{criteria_text}\n\n"
        f"Score each criterion as met or not met. "
        f"The overall score should reflect the proportion of criteria satisfied."
    )

    metric = GEval(
        name=f"quality_{category}",
        criteria=evaluation_criteria,
        evaluation_params=[
            LLMTestCaseParams.INPUT,
            LLMTestCaseParams.ACTUAL_OUTPUT,
        ],
        threshold=0.5,
    )

    test_case = LLMTestCase(
        input=input_text,
        actual_output=output_text,
    )

    await metric.a_measure(test_case)

    raw_score = metric.score if metric.score is not None else 0.0
    # Clamp to [0.0, 1.0]
    score = max(0.0, min(1.0, raw_score))

    # Derive criteria met/missed from score proportion
    n_criteria = len(criteria)
    n_met = round(score * n_criteria)
    n_met = max(0, min(n_criteria, n_met))

    # We cannot know exactly which criteria the LLM considered met,
    # so we list them in order, first n_met as met, rest as missed.
    met = criteria[:n_met]
    missed = criteria[n_met:]

    rating = _compute_rating(score, category)

    return QualityScore(
        category=category,
        score=score,
        rating=rating,
        criteria_met=met,
        criteria_missed=missed,
        method="llm",
    )


# ------------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------------


def score_output(
    category: str,
    input_text: str,
    output_text: str,
    use_llm: bool = False,
) -> QualityScore:
    """Score output quality. Falls back to deterministic if LLM unavailable.

    Args:
        category: One of the RUBRIC keys.
        input_text: The input/prompt that produced the output.
        output_text: The agent output to evaluate.
        use_llm: If True, attempt LLM-based scoring first.

    Returns:
        A QualityScore instance.
    """
    if use_llm:
        try:
            import asyncio

            # If we're already in an async context, create a new loop
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop is not None:
                # Already in async context — schedule coroutine
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as pool:
                    result = pool.submit(
                        asyncio.run,
                        score_output_llm(category, input_text, output_text),
                    ).result()
                return result
            else:
                return asyncio.run(score_output_llm(category, input_text, output_text))
        except Exception:
            _log.warning(
                "LLM scoring failed, falling back to deterministic",
                exc_info=True,
            )

    return score_output_deterministic(category, input_text, output_text)
