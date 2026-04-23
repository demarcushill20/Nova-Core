"""Tests for utils.memory_provenance.

Covers wire-format contracts for the Fusion MCP provenance hooks
(Nova_AI_Fusion_Memory_MCP memory_service.py:1342-1422).
"""

from __future__ import annotations

import pytest

from utils.memory_provenance import (
    MAX_COMPACTION_GROUP,
    build_compaction_metadata,
    build_promotion_metadata,
)

# ---------------------------------------------------------------------------
# build_promotion_metadata
# ---------------------------------------------------------------------------


def test_build_promotion_metadata_happy_path():
    out = build_promotion_metadata("mem_123", "episodic", "semantic")
    assert out == {
        "_promoted_from": {
            "old_id": "mem_123",
            "from_layer": "episodic",
            "to_layer": "semantic",
        }
    }


@pytest.mark.parametrize(
    "old_id,from_layer,to_layer,expected_field",
    [
        ("", "episodic", "semantic", "old_id"),
        ("mem_123", "", "semantic", "from_layer"),
        ("mem_123", "episodic", "", "to_layer"),
    ],
)
def test_build_promotion_metadata_rejects_empty_strs(old_id, from_layer, to_layer, expected_field):
    with pytest.raises(ValueError) as exc:
        build_promotion_metadata(old_id, from_layer, to_layer)
    # Pin the field name in the message so callers can debug which arg was bad.
    assert expected_field in str(exc.value)


@pytest.mark.parametrize(
    "old_id,from_layer,to_layer",
    [
        (None, "episodic", "semantic"),
        ("mem_123", 42, "semantic"),
        ("mem_123", "episodic", ["semantic"]),
    ],
)
def test_build_promotion_metadata_rejects_non_str(old_id, from_layer, to_layer):
    with pytest.raises(ValueError):
        build_promotion_metadata(old_id, from_layer, to_layer)


# ---------------------------------------------------------------------------
# build_compaction_metadata
# ---------------------------------------------------------------------------


def test_build_compaction_metadata_happy_path():
    # Without optional fields
    out = build_compaction_metadata(["a", "b", "c"])
    assert out == {"_compacted_from": {"source_ids": ["a", "b", "c"]}}

    # With optional fields
    out2 = build_compaction_metadata(["a", "b"], algorithm="daily_summary", reason="daily roll-up")
    assert out2 == {
        "_compacted_from": {
            "source_ids": ["a", "b"],
            "algorithm": "daily_summary",
            "reason": "daily roll-up",
        }
    }


def test_build_compaction_metadata_dedupes():
    out = build_compaction_metadata(["a", "b", "a", "c", "b"])
    assert out["_compacted_from"]["source_ids"] == ["a", "b", "c"]


@pytest.mark.parametrize(
    "input_ids,expected",
    [
        # Duplicate at the front
        (["a", "a", "b"], ["a", "b"]),
        # Case-sensitive non-dup: Fusion MCP ids are case-sensitive
        (["A", "a"], ["A", "a"]),
        # Duplicate at the tail
        (["a", "b", "b"], ["a", "b"]),
        # All duplicates collapse to one
        (["x", "x", "x"], ["x"]),
    ],
)
def test_build_compaction_metadata_dedup_positions(input_ids, expected):
    out = build_compaction_metadata(input_ids)
    assert out["_compacted_from"]["source_ids"] == expected


def test_build_compaction_metadata_rejects_empty_list():
    with pytest.raises(ValueError):
        build_compaction_metadata([])


@pytest.mark.parametrize("bad_elem", [None, 42, ["nested"], {}])
def test_build_compaction_metadata_rejects_non_str_element(bad_elem):
    with pytest.raises(ValueError) as exc:
        build_compaction_metadata(["a", "b", bad_elem])
    # Pin the field-name in the message so callers can debug the offending index.
    assert "source_ids[2]" in str(exc.value)


def test_build_compaction_metadata_rejects_empty_str_element():
    with pytest.raises(ValueError) as exc:
        build_compaction_metadata(["a", "", "b"])
    assert "source_ids[1]" in str(exc.value)


def test_build_compaction_metadata_rejects_non_list_input():
    with pytest.raises(ValueError):
        build_compaction_metadata("not-a-list")  # type: ignore[arg-type]


def test_build_compaction_metadata_enforces_20_cap():
    # 20 unique ids -> ok
    ids_20 = [f"mem_{i}" for i in range(20)]
    out = build_compaction_metadata(ids_20)
    assert len(out["_compacted_from"]["source_ids"]) == 20

    # 21 unique ids -> ValueError
    ids_21 = [f"mem_{i}" for i in range(21)]
    with pytest.raises(ValueError):
        build_compaction_metadata(ids_21)


def test_build_compaction_metadata_dedup_then_cap():
    # 25 items, but only 15 unique -> ok (dedup applied before cap)
    ids = [f"mem_{i % 15}" for i in range(25)]
    out = build_compaction_metadata(ids)
    assert len(out["_compacted_from"]["source_ids"]) == 15


def test_build_compaction_metadata_drops_empty_optionals():
    # Empty strings omitted
    out = build_compaction_metadata(["a"], algorithm="", reason="")
    assert out == {"_compacted_from": {"source_ids": ["a"]}}

    # None omitted
    out2 = build_compaction_metadata(["a"], algorithm=None, reason=None)
    assert out2 == {"_compacted_from": {"source_ids": ["a"]}}

    # Non-str omitted
    out3 = build_compaction_metadata(
        ["a"],
        algorithm=123,
        reason=["r"],  # type: ignore[arg-type]
    )
    assert out3 == {"_compacted_from": {"source_ids": ["a"]}}


def test_build_compaction_metadata_accepts_tuple():
    out = build_compaction_metadata(("a", "b"))
    assert out["_compacted_from"]["source_ids"] == ["a", "b"]


def test_max_compaction_group_constant():
    assert MAX_COMPACTION_GROUP == 20


# ---------------------------------------------------------------------------
# Documentation test: wire-format must match Fusion MCP hook contract
# ---------------------------------------------------------------------------


def test_wire_format_matches_fusion_mcp_hook_contract():
    """Document that helper output matches the exact keys/types consumed by
    Fusion MCP memory_service.py:1342-1422.
    """
    promo = build_promotion_metadata("mem_old", "episodic", "semantic")
    assert "_promoted_from" in promo
    p = promo["_promoted_from"]
    # Contract: dict with {old_id, from_layer, to_layer} as non-empty strs
    assert set(p.keys()) == {"old_id", "from_layer", "to_layer"}
    for k in ("old_id", "from_layer", "to_layer"):
        assert isinstance(p[k], str) and p[k]

    comp = build_compaction_metadata(["mem_1", "mem_2"], algorithm="daily_summary", reason="roll-up")
    assert "_compacted_from" in comp
    c = comp["_compacted_from"]
    # Contract: source_ids is a non-empty list[str]; algorithm/reason optional strs
    assert isinstance(c["source_ids"], list)
    assert len(c["source_ids"]) >= 1
    assert all(isinstance(s, str) and s for s in c["source_ids"])
    assert len(c["source_ids"]) <= MAX_COMPACTION_GROUP
    assert isinstance(c["algorithm"], str) and c["algorithm"]
    assert isinstance(c["reason"], str) and c["reason"]

    # Simulate merging into an upsert_memory metadata payload
    metadata: dict = {
        "category": "daily_summary",
        "project": "nova-core",
    }
    metadata.update(promo)
    metadata.update(comp)
    # Leading-underscore keys coexist with user metadata; they're scrubbed
    # by Fusion MCP before Pinecone/Neo4j base-node storage.
    assert "_promoted_from" in metadata
    assert "_compacted_from" in metadata
    assert metadata["category"] == "daily_summary"
