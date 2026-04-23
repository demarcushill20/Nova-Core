"""Provenance metadata helpers for Fusion Memory upsert_memory calls.

Consumer contract: Nova_AI_Fusion_Memory_MCP/app/services/memory_service.py:1342-1422.

The Fusion MCP `memory_service.perform_upsert()` has write-time hooks (behind
`ASSOC_PROVENANCE_WRITE_ENABLED`) that create PROMOTED_FROM and COMPACTED_FROM
graph edges when the incoming metadata dict carries the internal signal keys
`_promoted_from` / `_compacted_from`. This module emits those signals at the
source so the hooks fire in production.

Leading-underscore keys are scrubbed by Fusion MCP before Pinecone/Neo4j base-node
storage — they are transport-only and safe to include in any metadata payload.

Wire-format contracts (enforced here; silently skipped upstream if malformed):

- PROMOTED_FROM requires `metadata["_promoted_from"] = {"old_id", "from_layer",
  "to_layer"}`, all three non-empty strings.
- COMPACTED_FROM requires `metadata["_compacted_from"] = {"source_ids": [..]}`
  with a non-empty list of non-empty string ids, fan-out capped at 20 per the
  upstream `_MAX_COMPACTION_GROUP` contract at memory_service.py:1382. Optional
  `algorithm` and `reason` are included only when supplied as non-empty strings.

This module has zero runtime dependencies outside the Python stdlib.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

# Upstream fan-out cap. See Nova_AI_Fusion_Memory_MCP memory_service.py:1382.
MAX_COMPACTION_GROUP = 20


def _require_non_empty_str(value: Any, field: str) -> str:
    """Validate that `value` is a non-empty string; raise ValueError otherwise."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a str, got {type(value).__name__}")
    if not value:
        raise ValueError(f"{field} must be a non-empty str")
    return value


def build_promotion_metadata(old_id: str, from_layer: str, to_layer: str) -> dict[str, dict[str, str]]:
    """Return metadata fragment encoding a PROMOTED_FROM edge signal.

    All three arguments must be non-empty strings. The returned dict is suitable
    for merging into the `metadata` kwarg of an `upsert_memory` call and will
    cause the Fusion MCP hook to create a `PROMOTED_FROM` graph edge from the
    new memory back to `old_id`.

    Raises:
        ValueError: if any argument is not a non-empty str.
    """
    _require_non_empty_str(old_id, "old_id")
    _require_non_empty_str(from_layer, "from_layer")
    _require_non_empty_str(to_layer, "to_layer")
    return {
        "_promoted_from": {
            "old_id": old_id,
            "from_layer": from_layer,
            "to_layer": to_layer,
        }
    }


def build_compaction_metadata(
    source_ids: Sequence[str],
    *,
    algorithm: str | None = None,
    reason: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Return metadata fragment encoding a COMPACTED_FROM edge signal.

    `source_ids` is deduplicated preserving first-seen order. After dedup, the
    list must contain at least one id and at most `MAX_COMPACTION_GROUP` (20).

    Optional `algorithm` and `reason` are included in the output only when
    supplied as non-empty strings; otherwise they are omitted.

    Raises:
        ValueError: on non-list input, empty input, non-str elements, empty str
            elements, or more than `MAX_COMPACTION_GROUP` unique ids.
    """
    if not isinstance(source_ids, (list, tuple)):
        raise ValueError(f"source_ids must be a list or tuple, got {type(source_ids).__name__}")
    if len(source_ids) == 0:
        raise ValueError("source_ids must be non-empty")

    seen: set[str] = set()
    deduped: list[str] = []
    for idx, sid in enumerate(source_ids):
        if not isinstance(sid, str):
            raise ValueError(f"source_ids[{idx}] must be a str, got {type(sid).__name__}")
        if not sid:
            raise ValueError(f"source_ids[{idx}] must be a non-empty str")
        if sid in seen:
            continue
        seen.add(sid)
        deduped.append(sid)

    if len(deduped) > MAX_COMPACTION_GROUP:
        raise ValueError(
            f"source_ids exceeds MAX_COMPACTION_GROUP={MAX_COMPACTION_GROUP} after dedup (got {len(deduped)})"
        )

    payload: dict[str, Any] = {"source_ids": deduped}
    if isinstance(algorithm, str) and algorithm:
        payload["algorithm"] = algorithm
    if isinstance(reason, str) and reason:
        payload["reason"] = reason

    return {"_compacted_from": payload}
