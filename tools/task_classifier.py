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

Stage B rollout:
  Only "research" class is eligible for the multi-agent orchestrator path.
  A deterministic mutation-signal check rejects tasks that contain
  code-change or shell-execution signals even if classified as research.

Stage C rollout (superset of Stage B):
  Adds "code_impl" and "code_review" for low-risk repo inspection/refactor
  tasks. A deterministic high-risk-signal check rejects tasks that involve
  deployment, secrets, destructive operations, or overly broad scope.
  Verifier approval is mandatory for all Stage C coding tasks.
  Research tasks continue under the same rules as Stage B.
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

# ---------------------------------------------------------------------------
# Stage B: mutation-signal denylist
# ---------------------------------------------------------------------------
# If any of these patterns match, the task contains mutation intent and
# must NOT enter the read-only research multi-agent path — even if the
# keyword classifier scored it as "research".
_MUTATION_SIGNALS: list[str] = [
    r"\bimplement\b", r"\bcreate\s+(?:a\s+)?(?:function|class|module|script|file)\b",
    r"\bwrite\s+(?:code|file|script)\b", r"\bmodify\b", r"\bchange\b",
    r"\bpatch\b", r"\bcommit\b", r"\bgit\s+push\b",
    r"\brefactor\b", r"\brewrite\b", r"\bfix\s+(?:the\s+)?(?:bug|error|issue|crash)\b",
    r"\bdeploy\b", r"\binstall\b", r"\bpip\s+install\b",
    r"\brm\s", r"\bdelete\b", r"\bremove\b",
    r"\bexecute\b", r"\brun\s+(?:command|script|shell)\b",
    r"\bshell\b", r"\bbash\b",
    r"\bsystemd\b", r"\bservice\b", r"\bcron\b",
    r"\bsudo\b", r"\bchmod\b", r"\bchown\b",
]

_MUTATION_COMPILED: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE) for p in _MUTATION_SIGNALS
]

# Stage B: allowed classes for multi-agent orchestrator path
STAGE_B_CLASSES = frozenset({"research"})

# ---------------------------------------------------------------------------
# Stage C: high-risk signal denylist for coding tasks
# ---------------------------------------------------------------------------
# Stage C adds code_impl and code_review to the multi-agent path, but only
# for low-risk tasks. Tasks matching any high-risk signal are rejected.
STAGE_C_CLASSES = frozenset({"code_impl", "code_review"})

_HIGH_RISK_SIGNALS: list[str] = [
    # Deployment & infrastructure mutation
    r"\bdeploy\b", r"\bproduction\b", r"\bstaging\b",
    r"\binfrastructure\b", r"\bsystemd\b", r"\bcron\b",
    # Secrets & credentials
    r"\bsecret[s]?\b", r"\bcredential[s]?\b", r"\bpassword[s]?\b",
    r"\bapi[\s_-]?key[s]?\b", r"\b\.env\s+file\b",
    # Destructive operations
    r"\bsudo\b", r"\brm\s+-rf\b",
    r"\bgit\s+push\s+--force\b", r"\bgit\s+reset\s+--hard\b",
    r"\bdrop\s+(?:table|database|collection)\b", r"\bdestructive\b",
    # Overly broad scope
    r"\brewrite\s+(?:everything|all|entire)\b", r"\bcross[\s-]?repo\b",
    r"\bmigrat(?:e|ion)\b",
    # Shell / external execution
    r"\brun\s+(?:command|script|shell)\b",
    r"\bexecute\s+(?:command|script|shell)\b",
    r"\bshell\b", r"\bbash\b",
    # Policy / agent config changes
    r"\bpolicy\s+(?:engine|config)\b",
    r"\bagent\s+(?:config|profile|registry)\b",
    # Package management
    r"\bpip\s+install\b", r"\bnpm\s+install\b", r"\bapt[\s-]get\s+install\b",
]

_HIGH_RISK_COMPILED: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE) for p in _HIGH_RISK_SIGNALS
]


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


def has_mutation_signals(task_text: str) -> tuple[bool, list[str]]:
    """Check if task text contains mutation/write intent signals.

    Returns (has_mutations, matched_signals).
    Used by Stage B to reject research-classified tasks that actually
    contain code-change or shell-execution intent.
    """
    matched = []
    for pattern in _MUTATION_COMPILED:
        m = pattern.search(task_text)
        if m:
            matched.append(m.group())
    return bool(matched), matched


def is_stageB_eligible(
    task_class: str,
    confidence: float,
    task_text: str,
    feature_flags: dict | None = None,
) -> tuple[bool, str]:
    """Deterministic Stage B eligibility check.

    Returns (eligible, reason) where reason explains the decision.

    Checks performed in order:
    1. Feature flag master switch
    2. Stage is "B" (explicit stage gate)
    3. Task class in Stage B allowed set (research only)
    4. Confidence above threshold
    5. No mutation signals in task text (read-only safety gate)

    Fails closed: any check failure → not eligible.
    """
    flags = feature_flags or load_feature_flags()

    # 1. Master switch
    if not flags.get("enabled", False):
        return False, "orchestrator_disabled"

    # 2. Stage gate
    stage = flags.get("stage", "")
    if stage != "B":
        return False, f"stage_{stage}_not_B"

    # 3. Class allowlist (from config, intersected with Stage B set)
    supported = set(flags.get("supported_classes", []))
    allowed = supported & STAGE_B_CLASSES
    if task_class not in allowed:
        return False, f"class_{task_class}_not_in_stageB"

    # 4. Confidence threshold
    min_confidence = flags.get("min_confidence", 0.5)
    if confidence < min_confidence:
        return False, f"confidence_{confidence}_below_{min_confidence}"

    # 5. Mutation signal check (deterministic safety gate)
    has_mut, signals = has_mutation_signals(task_text)
    if has_mut:
        return False, f"mutation_signals_detected:{','.join(signals[:3])}"

    return True, "stageB_research_eligible"


def has_high_risk_signals(task_text: str) -> tuple[bool, list[str]]:
    """Check if task text contains high-risk signals for Stage C.

    Returns (has_high_risk, matched_signals).
    Used by Stage C to reject coding tasks that involve dangerous
    operations like deployment, secret handling, or destructive commands.
    """
    matched = []
    for pattern in _HIGH_RISK_COMPILED:
        m = pattern.search(task_text)
        if m:
            matched.append(m.group())
    return bool(matched), matched


def is_stageC_eligible(
    task_class: str,
    confidence: float,
    task_text: str,
    feature_flags: dict | None = None,
) -> tuple[bool, str]:
    """Deterministic Stage C eligibility check.

    Returns (eligible, reason) where reason explains the decision.

    Stage C is a superset of Stage B:
    - Research tasks: same rules as Stage B (mutation denylist)
    - Coding tasks (code_impl, code_review): high-risk denylist + verifier

    Checks performed in order:
    1. Feature flag master switch
    2. Stage is "C" (explicit stage gate)
    3. Task class in Stage C coding set OR Stage B research set
    4. Confidence above threshold
    5. For coding: no high-risk signals
    6. For research: no mutation signals (same as Stage B)

    Fails closed: any check failure → not eligible.
    """
    flags = feature_flags or load_feature_flags()

    # 1. Master switch
    if not flags.get("enabled", False):
        return False, "orchestrator_disabled"

    # 2. Stage gate
    stage = flags.get("stage", "")
    if stage != "C":
        return False, f"stage_{stage}_not_C"

    # 3. Class check (intersect with config)
    supported = set(flags.get("supported_classes", []))
    is_coding = task_class in (supported & STAGE_C_CLASSES)
    is_research = task_class in (supported & STAGE_B_CLASSES)

    if not is_coding and not is_research:
        return False, f"class_{task_class}_not_in_stageC"

    # 4. Confidence threshold
    min_confidence = flags.get("min_confidence", 0.5)
    if confidence < min_confidence:
        return False, f"confidence_{confidence}_below_{min_confidence}"

    # 5/6. Risk signal check (different denylist for coding vs research)
    if is_coding:
        has_risk, signals = has_high_risk_signals(task_text)
        if has_risk:
            return False, f"high_risk_signals_detected:{','.join(signals[:3])}"
        return True, "stageC_coding_eligible"
    else:
        # Research: same mutation denylist as Stage B
        has_mut, signals = has_mutation_signals(task_text)
        if has_mut:
            return False, f"mutation_signals_detected:{','.join(signals[:3])}"
        return True, "stageC_research_eligible"


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
        "stage": "",
        "min_confidence": 0.5,
        "fallback_to_worker": True,
        "audit_routing": True,
    }


def classify_and_route(task_text: str) -> dict:
    """Full classification + routing decision for a task.

    Returns a dict with classification details and routing decision.
    Uses Stage B eligibility when stage is "B".
    """
    task_class, confidence = classify_task(task_text)
    flags = load_feature_flags()

    # Stage-specific routing
    stage = flags.get("stage", "")

    # Stage C: superset of Stage B (research + low-risk coding)
    if stage == "C":
        eligible, reason = is_stageC_eligible(
            task_class, confidence, task_text, flags
        )
        is_coding = task_class in STAGE_C_CLASSES
        return {
            "task_class": task_class,
            "confidence": confidence,
            "use_orchestrator": eligible,
            "stage": "C",
            "allowed_roles": flags.get("allowed_roles", ["research", "coding"]),
            "verifier_required": eligible and is_coding,
            "fallback_reason": None if eligible else reason,
            "feature_flags": flags,
        }

    # Stage B: research-only
    if stage == "B":
        eligible, reason = is_stageB_eligible(
            task_class, confidence, task_text, flags
        )
        return {
            "task_class": task_class,
            "confidence": confidence,
            "use_orchestrator": eligible,
            "stage": "B",
            "allowed_roles": flags.get("allowed_roles", ["research"]),
            "fallback_reason": None if eligible else reason,
            "feature_flags": flags,
        }

    # Default path (no stage marker)
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
