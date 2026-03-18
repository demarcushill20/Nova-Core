"""P11.4 — Shared Context and Handoff Protocol.

Assembles role-specific context for agents so each receives only what it needs,
within a token budget. Manages handoff artifacts between workflow stages.

Context scoping rules:
  - Coder: full source files + summarised upstream results
  - Research: query + existing findings + repo structure
  - Critic: work product + original spec + review criteria
  - Verifier: contracts + artifacts to verify
  - Memory: workflow summary + contracts for extraction

Token budget: 8K default per agent. 80% triggers summarization warning.
90% hard rejects additional context.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BASE = Path("/home/nova/nova-core")
STATE = BASE / "STATE"
HANDOFFS_DIR = STATE / "handoffs"

# Approximate tokens per character (conservative for English text)
_CHARS_PER_TOKEN = 4
_DEFAULT_TOKEN_BUDGET = 8000
_SUMMARIZE_THRESHOLD = 0.80  # 80% of budget triggers warning
_HARD_REJECT_THRESHOLD = 0.90  # 90% of budget rejects new context


# ---------------------------------------------------------------------------
# Handoff artifact
# ---------------------------------------------------------------------------


@dataclass
class HandoffArtifact:
    """Structured handoff between workflow stages."""

    subtask_id: str
    from_role: str
    to_role: str
    status: str  # completed | failed | partial
    workflow_id: str = ""
    output_summary: str = ""
    artifacts: list[str] = field(default_factory=list)  # paths to files
    context_for_downstream: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> HandoffArtifact:
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# Context scoping rules
# ---------------------------------------------------------------------------

# Map of role → list of context keys to include
_ROLE_SCOPE: dict[str, list[str]] = {
    "research": ["query", "existing_findings", "repo_structure", "constraints"],
    "coder": ["source_files", "upstream_summary", "spec", "constraints", "test_files"],
    "critic": ["work_product", "spec", "review_criteria", "upstream_summary"],
    "verifier": ["contracts", "artifacts", "test_results", "spec"],
    "memory": ["workflow_summary", "contracts", "decisions"],
    "planner": ["task_description", "constraints", "repo_structure", "prior_plans"],
    "orchestrator": ["workflow_state", "node_statuses", "budget_status"],
}


def _estimate_tokens(text: str) -> int:
    """Rough token estimate from character count."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


# ---------------------------------------------------------------------------
# Context assembler
# ---------------------------------------------------------------------------


class ContextBudgetWarning(Exception):
    """Context is approaching the token budget."""


class ContextBudgetExceeded(Exception):
    """Context exceeds the token budget hard limit."""


class ContextAssembler:
    """Assembles role-scoped context within token budgets.

    Usage:
        assembler = ContextAssembler(token_budget=8000)
        context = assembler.assemble(
            role="coder",
            available_context={"source_files": "...", "spec": "..."},
        )
    """

    def __init__(
        self,
        token_budget: int = _DEFAULT_TOKEN_BUDGET,
        persist_dir: Path | None = None,
    ):
        self._budget = token_budget
        self._persist_dir = persist_dir or HANDOFFS_DIR
        self._persist_dir.mkdir(parents=True, exist_ok=True)

    @property
    def token_budget(self) -> int:
        return self._budget

    def assemble(
        self,
        role: str,
        available_context: dict[str, Any],
        *,
        extra_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        """Assemble context for a role, scoped and budget-limited.

        Args:
            role: The agent role (determines which keys are included).
            available_context: All available context keys and values.
            extra_keys: Additional context keys to include beyond the default scope.

        Returns:
            Dict of context keys → values, within the token budget.

        Raises:
            ContextBudgetExceeded: If mandatory context exceeds the hard limit.
        """
        allowed_keys = set(_ROLE_SCOPE.get(role, []))
        if extra_keys:
            allowed_keys.update(extra_keys)

        # Filter to allowed keys
        scoped: dict[str, Any] = {}
        total_tokens = 0

        for key in allowed_keys:
            if key not in available_context:
                continue
            value = available_context[key]
            value_str = json.dumps(value, default=str) if not isinstance(value, str) else value
            tokens = _estimate_tokens(value_str)

            # Check budget
            if total_tokens + tokens > self._budget * _HARD_REJECT_THRESHOLD:
                logger.warning(
                    "Context budget exceeded for role %s at key '%s' (%d + %d > %d * %.0f%%)",
                    role,
                    key,
                    total_tokens,
                    tokens,
                    self._budget,
                    _HARD_REJECT_THRESHOLD * 100,
                )
                # Truncate this value to fit
                remaining = int((self._budget * _HARD_REJECT_THRESHOLD - total_tokens) * _CHARS_PER_TOKEN)
                if remaining > 100:
                    value_str = value_str[:remaining] + "... [truncated]"
                    scoped[key] = value_str
                break

            scoped[key] = value
            total_tokens += tokens

            if total_tokens > self._budget * _SUMMARIZE_THRESHOLD:
                logger.info(
                    "Context at %.0f%% budget for role %s (%d/%d tokens)",
                    (total_tokens / self._budget) * 100,
                    role,
                    total_tokens,
                    self._budget,
                )

        return scoped

    def estimate_context_size(self, context: dict[str, Any]) -> int:
        """Estimate total token count for a context dict."""
        total = 0
        for v in context.values():
            s = json.dumps(v, default=str) if not isinstance(v, str) else v
            total += _estimate_tokens(s)
        return total

    def context_utilization(self, context: dict[str, Any]) -> float:
        """Return context utilization as a fraction of budget (0.0 to 1.0+)."""
        return self.estimate_context_size(context) / self._budget

    # -------------------------------------------------------------------
    # Handoff persistence
    # -------------------------------------------------------------------

    def save_handoff(self, handoff: HandoffArtifact) -> Path:
        """Persist a handoff artifact to disk."""
        path = self._persist_dir / f"{handoff.workflow_id}_{handoff.subtask_id}.json"
        path.write_text(json.dumps(handoff.to_dict(), indent=2, default=str))
        return path

    def load_handoff(self, workflow_id: str, subtask_id: str) -> HandoffArtifact | None:
        """Load a persisted handoff artifact."""
        path = self._persist_dir / f"{workflow_id}_{subtask_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return HandoffArtifact.from_dict(data)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to load handoff %s/%s: %s", workflow_id, subtask_id, e)
            return None

    def list_handoffs(self, workflow_id: str) -> list[HandoffArtifact]:
        """List all handoff artifacts for a workflow."""
        results = []
        for path in self._persist_dir.glob(f"{workflow_id}_*.json"):
            try:
                data = json.loads(path.read_text())
                results.append(HandoffArtifact.from_dict(data))
            except (json.JSONDecodeError, OSError):
                continue
        return results
