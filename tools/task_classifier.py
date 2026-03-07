"""Task Classifier — classify task text into task classes for routing.

Determines whether a task should be routed through the Phase 7
orchestrator or handled by the direct worker dispatch path.

Task classes:
  - research:     information gathering, web search, analysis
  - code_impl:    feature implementation, bug fix, refactoring
  - code_review:  review, audit, quality check
  - system:       infrastructure, config, deployment, self-improvement
  - simple:       quick query, format fix, trivial ops
  - unknown:      cannot classify — falls back to direct worker
"""

import json
import re
from pathlib import Path

# Task class definitions with keyword patterns (case-insensitive)
TASK_CLASSES: dict[str, list[str]] = {
    "research": [
        r"\bresearch\b", r"\binvestigat\w+\b", r"\banalyz\w+\b",
        r"\bexplor\w+\b", r"\bsurvey\b", r"\bcompare\b",
        r"\bsummariz\w+\b", r"\breview\s+(?:paper|article|doc)",
        r"\bfind\s+(?:out|information)\b", r"\bliterature\b",
        r"\bweb\s*search\b", r"\blook\s*up\b",
    ],
    "code_impl": [
        r"\bimplement\w*\b", r"\bcreate\s+(?:a\s+)?(?:function|class|module|script|file)\b",
        r"\bbuild\b", r"\badd\s+(?:a\s+)?(?:feature|endpoint|handler|method)\b",
        r"\bfix\s+(?:the\s+)?(?:bug|error|issue|crash)\b",
        r"\brefactor\b", r"\brewrite\b", r"\boptimize\b",
        r"\bmigrat\w+\b", r"\bupgrade\b", r"\bport\b",
        r"\bcoding\b", r"\bdevelop\b",
    ],
    "code_review": [
        r"\breview\s+(?:code|pr|pull|changes|diff)\b",
        r"\baudit\b", r"\bcode\s+quality\b",
        r"\blint\b", r"\bstatic\s+analysis\b",
        r"\bsecurity\s+(?:review|scan|check)\b",
        r"\bmaker[\s-]*checker\b",
    ],
    "system": [
        r"\bdeploy\b", r"\bconfigure\b", r"\binfrastructure\b",
        r"\bsystemd\b", r"\bservice\b", r"\bcron\b",
        r"\bself[\s-]*improv\w+\b", r"\bbootstrap\b",
        r"\bphase\s+\d+\b", r"\barchitect\w*\b",
        r"\bpipeline\b", r"\bci[/\s]*cd\b",
        r"\bpromote\b", r"\brollout\b", r"\bfeature[\s-]*flag\b",
    ],
    "simple": [
        r"\bformat\b", r"\brename\b", r"\btypo\b",
        r"\bupdate\s+(?:readme|docs?|comment)\b",
        r"\blist\s+(?:files|tasks|outputs)\b",
        r"\bstatus\b", r"\bcheck\b",
        r"\bquick\b", r"\btrivial\b",
    ],
}

# Compiled patterns (built once)
_COMPILED: dict[str, list[re.Pattern]] = {
    cls: [re.compile(p, re.IGNORECASE) for p in patterns]
    for cls, patterns in TASK_CLASSES.items()
}

# Classes that benefit from orchestrator coordination
ORCHESTRATOR_CLASSES = {"research", "code_impl", "code_review", "system"}

# Classes that should always use direct worker
DIRECT_CLASSES = {"simple", "unknown"}


def classify_task(task_text: str) -> tuple[str, float]:
    """Classify task text into a task class.

    Returns (task_class, confidence) where confidence is 0.0-1.0.
    Higher confidence = more keyword matches.
    """
    if not task_text.strip():
        return "unknown", 0.0

    scores: dict[str, int] = {}
    for cls, patterns in _COMPILED.items():
        score = sum(1 for p in patterns if p.search(task_text))
        if score > 0:
            scores[cls] = score

    if not scores:
        return "unknown", 0.0

    # Pick highest-scoring class
    best_class = max(scores, key=scores.get)
    best_score = scores[best_class]

    # Confidence: normalize by pattern count for that class
    max_possible = len(_COMPILED[best_class])
    confidence = min(1.0, best_score / max(max_possible * 0.3, 1))

    return best_class, round(confidence, 2)


def should_use_orchestrator(
    task_class: str,
    confidence: float,
    feature_flags: dict | None = None,
) -> bool:
    """Decide whether a task should be routed through the orchestrator.

    Args:
        task_class: The classified task class.
        confidence: Classification confidence (0.0-1.0).
        feature_flags: Optional feature flag config. If None, loads from disk.

    Returns:
        True if the orchestrator should handle this task.
    """
    flags = feature_flags or load_feature_flags()

    # Master switch
    if not flags.get("enabled", False):
        return False

    # Check if this class is in the supported set
    supported = flags.get("supported_classes", [])
    if task_class not in supported:
        return False

    # Confidence threshold
    min_confidence = flags.get("min_confidence", 0.3)
    if confidence < min_confidence:
        return False

    return True


def load_feature_flags() -> dict:
    """Load feature flags from STATE/config/feature_flags.json.

    Returns defaults (disabled) if file doesn't exist.
    """
    flags_path = Path("/home/nova/nova-core/STATE/config/feature_flags.json")
    if not flags_path.exists():
        return _default_flags()

    try:
        data = json.loads(flags_path.read_text(encoding="utf-8"))
        return data.get("phase7_orchestrator", _default_flags())
    except (json.JSONDecodeError, OSError):
        return _default_flags()


def _default_flags() -> dict:
    """Default feature flags — orchestrator disabled."""
    return {
        "enabled": False,
        "supported_classes": [],
        "min_confidence": 0.3,
        "fallback_to_worker": True,
        "audit_routing": True,
    }


def classify_and_route(task_text: str) -> dict:
    """Full classification + routing decision for a task.

    Returns a dict with classification details and routing decision.
    """
    task_class, confidence = classify_task(task_text)
    flags = load_feature_flags()
    use_orchestrator = should_use_orchestrator(task_class, confidence, flags)

    return {
        "task_class": task_class,
        "confidence": confidence,
        "use_orchestrator": use_orchestrator,
        "fallback_reason": None if use_orchestrator else _fallback_reason(
            task_class, confidence, flags
        ),
        "feature_flags": flags,
    }


def _fallback_reason(task_class: str, confidence: float, flags: dict) -> str:
    """Explain why the task was NOT routed to the orchestrator."""
    if not flags.get("enabled", False):
        return "orchestrator_disabled"
    if task_class not in flags.get("supported_classes", []):
        return f"class_{task_class}_not_supported"
    if confidence < flags.get("min_confidence", 0.3):
        return f"confidence_{confidence}_below_threshold"
    return "unknown"
