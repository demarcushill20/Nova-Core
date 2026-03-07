"""Phase 7.5 — Memory and Context Routing.

Provides:
  1. MemoryArtifact schema for structured workflow learnings
  2. Validated, bounded write path into MEMORY/agent_patterns/ and
     MEMORY/workflow_learnings/
  3. Workflow summary compaction (workflow state → reusable memory)
  4. Planner-facing retrieval hook for related prior patterns

Design constraints:
  - Writes are validated and fail-closed on missing fields.
  - Write count is bounded (max 2 per workflow, enforced by caller/engine).
  - Retrieval returns a bounded set of artifacts (max 5 by default).
  - All artifacts are JSON — inspectable, auditable, machine-readable.
  - No LLM calls — all logic is deterministic.
"""

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

BASE = Path(os.environ.get("NOVACORE_ROOT", "/home/nova/nova-core"))
MEMORY_DIR = BASE / "MEMORY"
AGENT_PATTERNS_DIR = MEMORY_DIR / "agent_patterns"
WORKFLOW_LEARNINGS_DIR = MEMORY_DIR / "workflow_learnings"

# Bounded retrieval default
MAX_RETRIEVAL_RESULTS = 5

# Max artifact size (bytes) to prevent unbounded writes
MAX_ARTIFACT_SIZE = 32_768  # 32 KB

# Required fields for a valid memory artifact
REQUIRED_FIELDS = {
    "artifact_id",
    "workflow_id",
    "task_summary",
    "task_class",
    "roles_involved",
    "key_decisions",
    "successful_patterns",
    "verification_outcome",
    "reusable_guidance",
    "created_at",
    "confidence",
}

VALID_CONFIDENCE = {"low", "medium", "high"}
VALID_TASK_CLASSES = {"research", "code_impl", "code_review", "system", "simple", "unknown"}
VALID_VERIFICATION_OUTCOMES = {"approved", "rejected", "incomplete", "partial", "not_verified"}

# Artifact ID pattern: mem_<workflow_id>_<timestamp>
ARTIFACT_ID_RE = re.compile(r"^mem_[a-zA-Z0-9_-]+_\d+$")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class MemoryArtifact:
    """Structured memory artifact from a completed workflow."""
    artifact_id: str
    workflow_id: str
    task_summary: str
    task_class: str
    roles_involved: list[str]
    key_decisions: list[str]
    successful_patterns: list[str]
    failure_patterns: list[str] = field(default_factory=list)
    verification_outcome: str = "not_verified"
    reusable_guidance: str = ""
    created_at: str = ""
    confidence: str = "medium"

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryArtifact":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_memory_artifact(data: dict) -> tuple[bool, list[str]]:
    """Validate a memory artifact dict. Returns (valid, errors).

    Fails closed: missing required fields or invalid values → rejected.
    """
    errors: list[str] = []

    # Check required fields
    for f in REQUIRED_FIELDS:
        if f not in data:
            errors.append(f"missing required field: {f}")
        elif data[f] is None or (isinstance(data[f], str) and not data[f].strip()):
            errors.append(f"empty required field: {f}")

    if errors:
        return False, errors

    # Type checks for list fields
    for list_field in ("roles_involved", "key_decisions", "successful_patterns"):
        if not isinstance(data.get(list_field), list):
            errors.append(f"{list_field} must be a list")

    if "failure_patterns" in data and not isinstance(data["failure_patterns"], list):
        errors.append("failure_patterns must be a list")

    # Enum checks
    if data.get("confidence") not in VALID_CONFIDENCE:
        errors.append(
            f"invalid confidence: {data.get('confidence')!r} "
            f"(must be one of {sorted(VALID_CONFIDENCE)})"
        )

    if data.get("task_class") not in VALID_TASK_CLASSES:
        errors.append(
            f"invalid task_class: {data.get('task_class')!r} "
            f"(must be one of {sorted(VALID_TASK_CLASSES)})"
        )

    if data.get("verification_outcome") not in VALID_VERIFICATION_OUTCOMES:
        errors.append(
            f"invalid verification_outcome: {data.get('verification_outcome')!r} "
            f"(must be one of {sorted(VALID_VERIFICATION_OUTCOMES)})"
        )

    # Artifact ID format
    aid = data.get("artifact_id", "")
    if not ARTIFACT_ID_RE.match(aid):
        errors.append(
            f"invalid artifact_id format: {aid!r} "
            f"(must match mem_<workflow_id>_<timestamp>)"
        )

    # Size bound check (serialized)
    try:
        serialized = json.dumps(data, default=str)
        if len(serialized.encode()) > MAX_ARTIFACT_SIZE:
            errors.append(
                f"artifact too large: {len(serialized.encode())} bytes "
                f"(max {MAX_ARTIFACT_SIZE})"
            )
    except (TypeError, ValueError) as e:
        errors.append(f"artifact not JSON-serializable: {e}")

    return (len(errors) == 0), errors


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------

def _atomic_write_json(path: Path, data: dict) -> None:
    """Atomic write: write to tmp + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str) + "\n")
    tmp.rename(path)


def write_memory_artifact(
    artifact: MemoryArtifact,
    target: str = "workflow_learnings",
    base: Path | None = None,
) -> Path:
    """Write a validated memory artifact to the appropriate MEMORY/ subdirectory.

    Args:
        artifact: The memory artifact to write.
        target: "workflow_learnings" or "agent_patterns".
        base: Override base directory (for testing).

    Returns:
        Path to the written artifact file.

    Raises:
        ValueError: If artifact fails validation or target is invalid.
    """
    data = artifact.to_dict()
    valid, errors = validate_memory_artifact(data)
    if not valid:
        raise ValueError(f"Memory artifact validation failed: {'; '.join(errors)}")

    if target not in ("workflow_learnings", "agent_patterns"):
        raise ValueError(f"Invalid target directory: {target!r}")

    root = base or MEMORY_DIR
    target_dir = root / target
    filename = f"{artifact.artifact_id}.json"
    path = target_dir / filename

    # Prevent overwrite of existing artifacts (append-only memory)
    if path.exists():
        raise ValueError(f"Artifact already exists: {path}")

    _atomic_write_json(path, data)
    return path


# ---------------------------------------------------------------------------
# Workflow compaction
# ---------------------------------------------------------------------------

def compact_workflow_summary(
    workflow_id: str,
    task_summary: str,
    task_class: str,
    delegations: list[dict],
    contracts: list[dict],
    metrics: dict,
    verification_outcome: str = "not_verified",
) -> MemoryArtifact:
    """Compact a completed workflow into a reusable MemoryArtifact.

    Extracts pattern-level learning from raw workflow state — not a
    transcript dump. Optimized for future planner usefulness.

    Args:
        workflow_id: The workflow identifier.
        task_summary: One-line description of the task.
        task_class: Classification (research, code_impl, etc.).
        delegations: List of delegation dicts from blackboard.
        contracts: List of child contract dicts from blackboard.
        metrics: Workflow metrics dict from blackboard.
        verification_outcome: Final verification result.

    Returns:
        A MemoryArtifact ready for validation and writing.
    """
    ts = int(time.time())
    artifact_id = f"mem_{workflow_id}_{ts}"

    # Extract roles involved
    roles = sorted({d.get("role", "unknown") for d in delegations})

    # Extract key decisions from contracts
    key_decisions = []
    for c in contracts:
        summary = c.get("summary", "")
        if summary:
            key_decisions.append(summary)
    # Cap at 10 decisions to keep compact
    key_decisions = key_decisions[:10]

    # Identify successful vs failed patterns
    successful_patterns = []
    failure_patterns = []
    for d in delegations:
        role = d.get("role", "unknown")
        status = d.get("status", "unknown")
        goal = d.get("goal", "")
        if status == "completed":
            successful_patterns.append(f"{role}: {goal}" if goal else f"{role}: completed")
        elif status == "failed":
            error = d.get("error", "unknown error")
            failure_patterns.append(f"{role}: {error}" if error else f"{role}: failed")
    # Cap lists
    successful_patterns = successful_patterns[:10]
    failure_patterns = failure_patterns[:5]

    # Build reusable guidance
    guidance_parts = []
    total = metrics.get("total_delegations", 0)
    completed = metrics.get("completed", 0)
    failed = metrics.get("failed", 0)
    if total > 0:
        ratio = completed / total
        if ratio == 1.0:
            guidance_parts.append("All subtasks completed successfully.")
        elif ratio >= 0.5:
            guidance_parts.append(
                f"{completed}/{total} subtasks completed; "
                f"review failure patterns before reuse."
            )
        else:
            guidance_parts.append(
                f"Low completion rate ({completed}/{total}); "
                f"consider restructuring approach."
            )

    latency = metrics.get("mean_subtask_latency_s")
    if latency is not None and latency > 120:
        guidance_parts.append(
            f"Mean subtask latency was {latency:.0f}s — consider parallelism."
        )

    if failure_patterns:
        guidance_parts.append(
            f"Common failure modes: {', '.join(fp.split(':')[0] for fp in failure_patterns[:3])}."
        )

    reusable_guidance = " ".join(guidance_parts) if guidance_parts else "No specific guidance."

    # Confidence based on verification and completion
    if verification_outcome == "approved" and failed == 0:
        confidence = "high"
    elif verification_outcome == "rejected":
        confidence = "low"
    else:
        confidence = "medium"

    return MemoryArtifact(
        artifact_id=artifact_id,
        workflow_id=workflow_id,
        task_summary=task_summary[:500],  # bound summary length
        task_class=task_class,
        roles_involved=roles,
        key_decisions=key_decisions,
        successful_patterns=successful_patterns,
        failure_patterns=failure_patterns,
        verification_outcome=verification_outcome,
        reusable_guidance=reusable_guidance,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Planner retrieval hook
# ---------------------------------------------------------------------------

def _load_artifacts(directory: Path) -> list[dict]:
    """Load all JSON artifacts from a directory."""
    if not directory.exists():
        return []
    artifacts = []
    for f in sorted(directory.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            if isinstance(data, dict):
                artifacts.append(data)
        except (json.JSONDecodeError, OSError):
            continue  # skip malformed files
    return artifacts


def _relevance_score(artifact: dict, task_class: str, keywords: list[str]) -> float:
    """Compute a simple relevance score for retrieval ranking.

    Scoring:
      - task_class exact match: +3.0
      - each keyword hit in task_summary or reusable_guidance: +1.0
      - high confidence: +1.0, medium: +0.5
      - recent artifact (< 7 days): +0.5

    Returns a float score. Higher is more relevant.
    """
    score = 0.0

    if artifact.get("task_class") == task_class:
        score += 3.0

    # Keyword matching in text fields
    searchable = " ".join([
        artifact.get("task_summary", ""),
        artifact.get("reusable_guidance", ""),
        " ".join(artifact.get("key_decisions", [])),
    ]).lower()

    for kw in keywords:
        if kw.lower() in searchable:
            score += 1.0

    # Confidence bonus
    conf = artifact.get("confidence", "medium")
    if conf == "high":
        score += 1.0
    elif conf == "medium":
        score += 0.5

    # Recency bonus
    created = artifact.get("created_at", "")
    try:
        created_ts = time.mktime(time.strptime(created, "%Y-%m-%dT%H:%M:%SZ"))
        age_days = (time.time() - created_ts) / 86400
        if age_days < 7:
            score += 0.5
    except (ValueError, OverflowError):
        pass

    return score


def retrieve_related_patterns(
    task_class: str,
    keywords: list[str],
    max_results: int = MAX_RETRIEVAL_RESULTS,
    base: Path | None = None,
) -> list[dict]:
    """Retrieve prior related memory artifacts for planner consumption.

    Returns a bounded, relevance-ranked list of prior artifacts. Results
    are advisory — the planner should not over-trust prior memory.

    Args:
        task_class: The task classification to match against.
        keywords: Keywords extracted from the current task description.
        max_results: Maximum number of results to return.
        base: Override MEMORY/ base directory (for testing).

    Returns:
        List of artifact dicts, ranked by relevance, capped at max_results.
        Each dict includes a "_relevance_score" field.
    """
    root = base or MEMORY_DIR
    max_results = min(max_results, MAX_RETRIEVAL_RESULTS)  # enforce hard cap

    # Gather from both subdirectories
    all_artifacts = (
        _load_artifacts(root / "agent_patterns")
        + _load_artifacts(root / "workflow_learnings")
    )

    if not all_artifacts:
        return []

    # Score and rank
    scored = []
    for art in all_artifacts:
        score = _relevance_score(art, task_class, keywords)
        if score > 0:
            entry = dict(art)
            entry["_relevance_score"] = round(score, 2)
            scored.append(entry)

    scored.sort(key=lambda x: x["_relevance_score"], reverse=True)
    return scored[:max_results]


def format_retrieval_for_planner(artifacts: list[dict]) -> str:
    """Format retrieved artifacts into a bounded planner-readable summary.

    Returns a compact markdown string suitable for injection into
    planner context. Capped in length to prevent context overflow.
    """
    if not artifacts:
        return "No prior related patterns found."

    lines = [f"## Prior Related Patterns ({len(artifacts)} found)\n"]
    for i, art in enumerate(artifacts, 1):
        lines.append(f"### Pattern {i}: {art.get('task_summary', 'N/A')}")
        lines.append(f"- **Class**: {art.get('task_class', 'N/A')}")
        lines.append(f"- **Outcome**: {art.get('verification_outcome', 'N/A')}")
        lines.append(f"- **Confidence**: {art.get('confidence', 'N/A')}")
        lines.append(f"- **Guidance**: {art.get('reusable_guidance', 'N/A')}")
        if art.get("failure_patterns"):
            lines.append(f"- **Failures**: {'; '.join(art['failure_patterns'][:3])}")
        lines.append(f"- **Relevance**: {art.get('_relevance_score', 0)}")
        lines.append("")

    lines.append("*Advisory only — verify against current repo state.*\n")

    result = "\n".join(lines)
    # Hard cap at 4KB to prevent context overflow
    if len(result) > 4096:
        result = result[:4090] + "\n...\n"
    return result


# ---------------------------------------------------------------------------
# Integration: capture memory from completed workflow
# ---------------------------------------------------------------------------

def capture_workflow_memory(
    workflow_id: str,
    task_summary: str,
    task_class: str,
    delegations: list[dict],
    contracts: list[dict],
    metrics: dict,
    verification_outcome: str = "not_verified",
    target: str = "workflow_learnings",
    base: Path | None = None,
) -> Path | None:
    """End-to-end: compact a workflow and write a validated memory artifact.

    This is the primary integration point — call after a workflow completes
    successfully (governed_synthesize or synthesize_workflow).

    Returns the path to the written artifact, or None if the workflow
    produced no meaningful learnings (e.g., no delegations).
    """
    if not delegations:
        return None

    artifact = compact_workflow_summary(
        workflow_id=workflow_id,
        task_summary=task_summary,
        task_class=task_class,
        delegations=delegations,
        contracts=contracts,
        metrics=metrics,
        verification_outcome=verification_outcome,
    )

    return write_memory_artifact(artifact, target=target, base=base)
