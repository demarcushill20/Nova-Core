"""Neighbor detection for the skill optimization pipeline.

Merges multiple neighbor sources:
1. Manual curation (from skill_discovery.MANUAL_NEIGHBORS)
2. Domain/tag heuristics (shared domain tags)
3. Metadata-declared neighbors (from metadata.json)
4. Historical co-trigger data (if available)
"""

from __future__ import annotations

import json
from pathlib import Path

from tools.skill_optimizer.skill_discovery import (
    BASE_DIR,
    MANUAL_NEIGHBORS,
    SkillRecord,
    compute_neighbors_by_domain,
    discover_all,
)


def get_metadata_neighbors(skill_path: str) -> list[str]:
    """Read neighbor declarations from a skill's metadata.json."""
    metadata_path = Path(skill_path) / "metadata.json"
    if not metadata_path.exists():
        return []
    try:
        data = json.loads(metadata_path.read_text())
        return data.get("neighbors", [])
    except (json.JSONDecodeError, OSError):
        return []


def get_overlap_pair_neighbors(skill_name: str) -> list[str]:
    """Read neighbors from global overlap_pairs.json."""
    overlap_path = BASE_DIR / "evals" / "global" / "overlap_pairs.json"
    if not overlap_path.exists():
        return []
    try:
        pairs = json.loads(overlap_path.read_text())
        neighbors = set()
        for pair in pairs:
            if pair.get("skill_a") == skill_name:
                neighbors.add(pair["skill_b"])
            elif pair.get("skill_b") == skill_name:
                neighbors.add(pair["skill_a"])
        return sorted(neighbors)
    except (json.JSONDecodeError, OSError):
        return []


def detect_neighbors(
    skill_name: str,
    records: list[SkillRecord] | None = None,
    include_manual: bool = True,
    include_domain: bool = True,
    include_metadata: bool = True,
    include_overlap_pairs: bool = True,
) -> dict[str, list[str]]:
    """Detect neighbors for a skill from all available sources.

    Returns a dict mapping source name to list of neighbor skill names.
    Also returns a 'merged' key with deduplicated union.
    """
    if records is None:
        records = discover_all()

    sources: dict[str, list[str]] = {}

    # 1. Manual curation
    if include_manual:
        sources["manual"] = MANUAL_NEIGHBORS.get(skill_name, [])

    # 2. Domain/tag heuristics
    if include_domain:
        domain_map = compute_neighbors_by_domain(records)
        sources["domain"] = domain_map.get(skill_name, [])

    # 3. Metadata-declared neighbors
    if include_metadata:
        record = next((r for r in records if r.name == skill_name), None)
        if record:
            sources["metadata"] = get_metadata_neighbors(record.path)

    # 4. Overlap pairs
    if include_overlap_pairs:
        sources["overlap_pairs"] = get_overlap_pair_neighbors(skill_name)

    # Merge all sources
    merged: set[str] = set()
    for source_neighbors in sources.values():
        merged.update(source_neighbors)
    merged.discard(skill_name)  # don't include self

    sources["merged"] = sorted(merged)

    return sources


def build_neighbor_map(records: list[SkillRecord] | None = None) -> dict[str, list[str]]:
    """Build the complete neighbor map for all skills.

    Returns {skill_name: [neighbor_names]}.
    """
    if records is None:
        records = discover_all()

    neighbor_map: dict[str, list[str]] = {}
    for record in records:
        result = detect_neighbors(record.name, records)
        neighbor_map[record.name] = result["merged"]

    return neighbor_map
