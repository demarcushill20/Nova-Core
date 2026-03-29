"""Tests for skills.skill_record, skills.version_store, and migration script.

~40+ tests covering:
- SkillVersion dataclass creation, serialisation, defaults
- SkillVersionStore CRUD, evolution, rollback, lineage, snapshots, stats
- Migration from legacy skill_usage_history.json
- Skill identity generation and sidecar files
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from skills.skill_record import (
    ExecutionStats,
    Origin,
    SkillLineage,
    SkillVersion,
    generate_skill_id,
)
from skills.version_store import (
    SkillVersionStore,
    read_sidecar,
    snapshot_skill_dir,
    write_sidecar,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_skill(
    name: str = "test-skill",
    origin: Origin = Origin.IMPORTED,
    generation: int = 0,
    parent_ids: list[str] | None = None,
    is_active: bool = True,
    **kwargs,
) -> SkillVersion:
    """Create a SkillVersion with sensible defaults for testing."""
    skill_id = kwargs.pop(
        "skill_id", generate_skill_id(name, origin, generation)
    )
    return SkillVersion(
        skill_id=skill_id,
        name=name,
        description=kwargs.pop("description", f"Test skill: {name}"),
        path=kwargs.pop("path", f"SKILLS/{name}"),
        is_active=is_active,
        version=kwargs.pop("version", "1.0.0"),
        lineage=SkillLineage(
            origin=origin,
            generation=generation,
            parent_ids=parent_ids or [],
            change_summary=kwargs.pop("change_summary", None),
        ),
        stats=ExecutionStats(
            selections=kwargs.pop("selections", 0),
            executions=kwargs.pop("executions", 0),
            completions=kwargs.pop("completions", 0),
            failures=kwargs.pop("failures", 0),
            fallbacks=kwargs.pop("fallbacks", 0),
        ),
        created_by=kwargs.pop("created_by", "test"),
    )


@pytest.fixture
def store(tmp_path: Path) -> SkillVersionStore:
    """Return a SkillVersionStore backed by a temp database."""
    return SkillVersionStore(db_path=tmp_path / "test_skills.db")


# ===========================================================================
# TestSkillVersion — dataclass creation, serialisation, defaults
# ===========================================================================


class TestSkillVersion:
    """Tests for the SkillVersion dataclass."""

    def test_create_with_defaults(self):
        sv = SkillVersion(
            skill_id="s1",
            name="my-skill",
            description="A skill",
            path="SKILLS/my-skill",
            lineage=SkillLineage(origin=Origin.IMPORTED),
        )
        assert sv.skill_id == "s1"
        assert sv.is_active is True
        assert sv.version == "1.0.0"
        assert sv.stats.executions == 0
        assert sv.stats.failures == 0
        assert sv.created_by == "system"

    def test_origin_enum_values(self):
        assert Origin.IMPORTED.value == "imported"
        assert Origin.FIXED.value == "fixed"
        assert Origin.DERIVED.value == "derived"
        assert Origin.CAPTURED.value == "captured"

    def test_to_dict_roundtrip(self):
        sv = _make_skill(
            name="roundtrip",
            executions=5,
            completions=3,
            failures=2,
            change_summary="test change",
        )
        d = sv.to_dict()
        assert d["skill_id"] == sv.skill_id
        assert d["name"] == "roundtrip"
        assert d["stats"]["executions"] == 5
        assert d["lineage"]["origin"] == "imported"
        assert d["lineage"]["change_summary"] == "test change"

    def test_from_dict_roundtrip(self):
        original = _make_skill(
            name="from-dict",
            origin=Origin.DERIVED,
            generation=2,
            parent_ids=["p1", "p2"],
            executions=10,
        )
        d = original.to_dict()
        restored = SkillVersion.from_dict(d)
        assert restored.skill_id == original.skill_id
        assert restored.name == "from-dict"
        assert restored.lineage.origin == Origin.DERIVED
        assert restored.lineage.generation == 2
        assert restored.lineage.parent_ids == ["p1", "p2"]
        assert restored.stats.executions == 10

    def test_from_dict_missing_optional_fields(self):
        minimal = {
            "skill_id": "min1",
            "name": "minimal",
        }
        sv = SkillVersion.from_dict(minimal)
        assert sv.skill_id == "min1"
        assert sv.description == ""
        assert sv.is_active is True
        assert sv.lineage.origin == Origin.IMPORTED
        assert sv.stats.selections == 0

    def test_to_dict_omits_content_snapshot(self):
        sv = _make_skill()
        sv.lineage.content_snapshot = b"some bytes"
        d = sv.to_dict()
        assert "content_snapshot" not in d["lineage"]

    def test_execution_stats_defaults(self):
        stats = ExecutionStats()
        assert stats.selections == 0
        assert stats.executions == 0
        assert stats.completions == 0
        assert stats.failures == 0
        assert stats.fallbacks == 0

    def test_skill_lineage_defaults(self):
        lineage = SkillLineage(origin=Origin.CAPTURED)
        assert lineage.generation == 0
        assert lineage.parent_ids == []
        assert lineage.content_snapshot is None
        assert lineage.content_diff is None
        assert lineage.change_summary is None


# ===========================================================================
# TestSkillVersionStore — CRUD, evolution, rollback, lineage, snapshots
# ===========================================================================


class TestSkillVersionStore:
    """Tests for SkillVersionStore SQLite operations."""

    def test_register_and_get(self, store: SkillVersionStore):
        sv = _make_skill(name="reg-test")
        store.register_skill(sv)
        loaded = store.get_skill(sv.skill_id)
        assert loaded is not None
        assert loaded.name == "reg-test"
        assert loaded.lineage.origin == Origin.IMPORTED

    def test_get_nonexistent_returns_none(self, store: SkillVersionStore):
        assert store.get_skill("nonexistent") is None

    def test_get_active_version(self, store: SkillVersionStore):
        sv = _make_skill(name="active-test")
        store.register_skill(sv)
        loaded = store.get_active_version("active-test")
        assert loaded is not None
        assert loaded.skill_id == sv.skill_id

    def test_get_active_version_ignores_inactive(
        self, store: SkillVersionStore
    ):
        sv = _make_skill(name="inactive-test", is_active=False)
        store.register_skill(sv)
        assert store.get_active_version("inactive-test") is None

    def test_list_skills_active_only(self, store: SkillVersionStore):
        active = _make_skill(
            name="list-a", skill_id="list_a_1", is_active=True
        )
        inactive = _make_skill(
            name="list-b", skill_id="list_b_1", is_active=False
        )
        store.register_skill(active)
        store.register_skill(inactive)
        result = store.list_skills(active_only=True)
        names = [s.name for s in result]
        assert "list-a" in names
        assert "list-b" not in names

    def test_list_skills_all(self, store: SkillVersionStore):
        active = _make_skill(
            name="list-all-a", skill_id="all_a_1", is_active=True
        )
        inactive = _make_skill(
            name="list-all-b", skill_id="all_b_1", is_active=False
        )
        store.register_skill(active)
        store.register_skill(inactive)
        result = store.list_skills(active_only=False)
        names = [s.name for s in result]
        assert "list-all-a" in names
        assert "list-all-b" in names

    def test_evolve_skill_atomicity(self, store: SkillVersionStore):
        parent = _make_skill(name="evolver", skill_id="parent_1")
        store.register_skill(parent)
        child = _make_skill(
            name="evolver",
            skill_id="child_1",
            origin=Origin.DERIVED,
            generation=1,
            parent_ids=["parent_1"],
            version="2.0.0",
        )
        store.evolve_skill("parent_1", child, deactivate_parent=True)
        # Parent should be deactivated
        p = store.get_skill("parent_1")
        assert p is not None
        assert p.is_active is False
        # Child should be active
        c = store.get_skill("child_1")
        assert c is not None
        assert c.is_active is True
        assert c.version == "2.0.0"

    def test_evolve_skill_keeps_parent_active(
        self, store: SkillVersionStore
    ):
        parent = _make_skill(name="keeper", skill_id="keep_parent")
        store.register_skill(parent)
        child = _make_skill(
            name="keeper",
            skill_id="keep_child",
            parent_ids=["keep_parent"],
        )
        store.evolve_skill(
            "keep_parent", child, deactivate_parent=False
        )
        p = store.get_skill("keep_parent")
        assert p is not None
        assert p.is_active is True

    def test_increment_stat_executions(self, store: SkillVersionStore):
        sv = _make_skill(name="stat-inc", skill_id="stat_1")
        store.register_skill(sv)
        store.increment_stat("stat_1", "executions")
        store.increment_stat("stat_1", "executions")
        store.increment_stat("stat_1", "executions")
        loaded = store.get_skill("stat_1")
        assert loaded is not None
        assert loaded.stats.executions == 3

    def test_increment_stat_all_fields(self, store: SkillVersionStore):
        sv = _make_skill(name="all-stats", skill_id="allstat_1")
        store.register_skill(sv)
        for field in (
            "selections",
            "executions",
            "completions",
            "failures",
            "fallbacks",
        ):
            store.increment_stat("allstat_1", field)
        loaded = store.get_skill("allstat_1")
        assert loaded is not None
        assert loaded.stats.selections == 1
        assert loaded.stats.executions == 1
        assert loaded.stats.completions == 1
        assert loaded.stats.failures == 1
        assert loaded.stats.fallbacks == 1

    def test_increment_stat_invalid_field(self, store: SkillVersionStore):
        sv = _make_skill(name="bad-field", skill_id="bad_1")
        store.register_skill(sv)
        with pytest.raises(ValueError, match="Invalid stat field"):
            store.increment_stat("bad_1", "nonexistent")

    def test_get_lineage_chain_single(self, store: SkillVersionStore):
        sv = _make_skill(name="solo", skill_id="solo_1")
        store.register_skill(sv)
        chain = store.get_lineage_chain("solo_1")
        assert len(chain) == 1
        assert chain[0].skill_id == "solo_1"

    def test_get_lineage_chain_multi_generation(
        self, store: SkillVersionStore
    ):
        # Create a 3-generation chain: g0 -> g1 -> g2
        g0 = _make_skill(name="chain", skill_id="chain_g0")
        store.register_skill(g0)
        g1 = _make_skill(
            name="chain",
            skill_id="chain_g1",
            generation=1,
            parent_ids=["chain_g0"],
        )
        store.evolve_skill("chain_g0", g1)
        g2 = _make_skill(
            name="chain",
            skill_id="chain_g2",
            generation=2,
            parent_ids=["chain_g1"],
        )
        store.evolve_skill("chain_g1", g2)
        chain = store.get_lineage_chain("chain_g2")
        assert len(chain) == 3
        assert chain[0].skill_id == "chain_g2"
        assert chain[1].skill_id == "chain_g1"
        assert chain[2].skill_id == "chain_g0"

    def test_get_lineage_chain_nonexistent(self, store: SkillVersionStore):
        chain = store.get_lineage_chain("nonexistent")
        assert chain == []

    def test_rollback_deactivates_child_reactivates_parent(
        self, store: SkillVersionStore
    ):
        parent = _make_skill(name="rollback", skill_id="rb_parent")
        store.register_skill(parent)
        child = _make_skill(
            name="rollback",
            skill_id="rb_child",
            generation=1,
            parent_ids=["rb_parent"],
        )
        store.evolve_skill("rb_parent", child, deactivate_parent=True)
        # Verify parent is inactive
        assert store.get_skill("rb_parent").is_active is False
        # Rollback
        restored = store.rollback_skill("rb_child")
        assert restored is not None
        assert restored.skill_id == "rb_parent"
        assert restored.is_active is True
        # Child should be deactivated
        c = store.get_skill("rb_child")
        assert c.is_active is False

    def test_rollback_no_parent_returns_none(
        self, store: SkillVersionStore
    ):
        sv = _make_skill(name="no-parent", skill_id="np_1")
        store.register_skill(sv)
        result = store.rollback_skill("np_1")
        assert result is None

    def test_save_and_get_snapshot(self, store: SkillVersionStore):
        sv = _make_skill(name="snap", skill_id="snap_1")
        store.register_skill(sv)
        content = {
            "SKILL.md": "# My Skill\nDoes stuff.",
            "metadata.json": '{"name": "snap"}',
        }
        store.save_snapshot("snap_1", content)
        loaded = store.get_snapshot("snap_1")
        assert loaded is not None
        assert loaded["SKILL.md"] == "# My Skill\nDoes stuff."
        assert loaded["metadata.json"] == '{"name": "snap"}'

    def test_get_snapshot_nonexistent(self, store: SkillVersionStore):
        assert store.get_snapshot("nonexistent") is None

    def test_snapshot_is_compressed(self, tmp_path: Path):
        """Verify the stored blob is actually gzip-compressed."""
        db_path = tmp_path / "compress_test.db"
        store = SkillVersionStore(db_path=db_path)
        sv = _make_skill(name="compress", skill_id="comp_1")
        store.register_skill(sv)
        content = {"SKILL.md": "hello world " * 100}
        store.save_snapshot("comp_1", content)
        # Read raw blob from DB
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT content FROM skill_snapshots WHERE skill_id = ?",
            ("comp_1",),
        ).fetchone()
        conn.close()
        raw_blob = row[0]
        # Verify it's valid gzip
        decompressed = gzip.decompress(raw_blob)
        assert json.loads(decompressed) == content

    def test_register_with_parent_ids(self, store: SkillVersionStore):
        """Verify parent_ids are persisted in lineage_parents table."""
        parent1 = _make_skill(name="p1", skill_id="pid_1")
        parent2 = _make_skill(name="p2", skill_id="pid_2")
        store.register_skill(parent1)
        store.register_skill(parent2)
        child = _make_skill(
            name="child",
            skill_id="child_multi",
            parent_ids=["pid_1", "pid_2"],
        )
        store.register_skill(child)
        loaded = store.get_skill("child_multi")
        assert loaded is not None
        assert set(loaded.lineage.parent_ids) == {"pid_1", "pid_2"}

    def test_close_is_safe(self, store: SkillVersionStore):
        """close() should be callable without error."""
        store.close()
        store.close()  # Double-close should also be safe

    def test_db_created_in_subdirectory(self, tmp_path: Path):
        """DB creation should auto-create parent directories."""
        db_path = tmp_path / "deep" / "nested" / "dir" / "skills.db"
        s = SkillVersionStore(db_path=db_path)
        sv = _make_skill(name="deep-test")
        s.register_skill(sv)
        assert db_path.exists()
        loaded = s.get_skill(sv.skill_id)
        assert loaded is not None


# ===========================================================================
# TestMigration — legacy JSON to SQLite
# ===========================================================================


class TestMigration:
    """Tests for scripts/migrate_skill_history.py."""

    def test_migrate_from_sample_json(self, tmp_path: Path):
        from scripts.migrate_skill_history import migrate

        history = {
            "file-ops": {
                "runs": 100,
                "successes": 90,
                "failures": 10,
                "avg_duration_ms": 350000,
                "last_used_ts": "2026-03-29T12:00:00Z",
                "total_retries": 20,
            },
            "web-research": {
                "runs": 50,
                "successes": 40,
                "failures": 10,
                "avg_duration_ms": 500000,
                "last_used_ts": "2026-03-29T11:00:00Z",
                "total_retries": 15,
            },
        }
        history_path = tmp_path / "history.json"
        history_path.write_text(json.dumps(history))
        db_path = tmp_path / "skills.db"
        count = migrate(history_path=history_path, db_path=db_path)
        assert count == 2
        store = SkillVersionStore(db_path=db_path)
        # Verify file-ops
        sid = generate_skill_id("file-ops", Origin.IMPORTED)
        sv = store.get_skill(sid)
        assert sv is not None
        assert sv.name == "file-ops"
        assert sv.stats.executions == 100
        assert sv.stats.completions == 90
        assert sv.stats.failures == 10
        assert sv.stats.selections == 0
        assert sv.stats.fallbacks == 0
        assert sv.lineage.origin == Origin.IMPORTED
        assert sv.lineage.generation == 0
        # Verify legacy fields in change_summary
        summary = json.loads(sv.lineage.change_summary)
        assert summary["avg_duration_ms"] == 350000
        assert summary["last_used_ts"] == "2026-03-29T12:00:00Z"
        assert summary["total_retries"] == 20

    def test_idempotent_re_migration(self, tmp_path: Path):
        from scripts.migrate_skill_history import migrate

        history = {
            "shell-ops": {
                "runs": 10,
                "successes": 10,
                "failures": 0,
                "avg_duration_ms": 200000,
                "last_used_ts": "2026-03-29T10:00:00Z",
                "total_retries": 0,
            }
        }
        history_path = tmp_path / "history.json"
        history_path.write_text(json.dumps(history))
        db_path = tmp_path / "skills.db"
        # First migration
        count1 = migrate(history_path=history_path, db_path=db_path)
        assert count1 == 1
        # Second migration — should skip
        count2 = migrate(history_path=history_path, db_path=db_path)
        assert count2 == 0

    def test_empty_history(self, tmp_path: Path):
        from scripts.migrate_skill_history import migrate

        history_path = tmp_path / "history.json"
        history_path.write_text("{}")
        db_path = tmp_path / "skills.db"
        count = migrate(history_path=history_path, db_path=db_path)
        assert count == 0

    def test_missing_history_file(self, tmp_path: Path):
        from scripts.migrate_skill_history import migrate

        history_path = tmp_path / "nonexistent.json"
        db_path = tmp_path / "skills.db"
        count = migrate(history_path=history_path, db_path=db_path)
        assert count == 0

    def test_corrupt_history_file(self, tmp_path: Path):
        from scripts.migrate_skill_history import migrate

        history_path = tmp_path / "history.json"
        history_path.write_text("{broken json!!!")
        db_path = tmp_path / "skills.db"
        count = migrate(history_path=history_path, db_path=db_path)
        assert count == 0

    def test_migration_sets_created_by(self, tmp_path: Path):
        from scripts.migrate_skill_history import migrate

        history = {
            "git-ops": {
                "runs": 5,
                "successes": 5,
                "failures": 0,
                "avg_duration_ms": 300000,
                "last_used_ts": "2026-03-29T08:00:00Z",
                "total_retries": 0,
            }
        }
        history_path = tmp_path / "history.json"
        history_path.write_text(json.dumps(history))
        db_path = tmp_path / "skills.db"
        migrate(history_path=history_path, db_path=db_path)
        store = SkillVersionStore(db_path=db_path)
        sid = generate_skill_id("git-ops", Origin.IMPORTED)
        sv = store.get_skill(sid)
        assert sv is not None
        assert sv.created_by == "migration"


# ===========================================================================
# TestSkillIdentity — generate_skill_id, sidecar files
# ===========================================================================


class TestSkillIdentity:
    """Tests for skill ID generation and sidecar files."""

    def test_generate_imported_id_format(self):
        sid = generate_skill_id("file-ops", Origin.IMPORTED)
        h = hashlib.sha256("file-ops".encode()).hexdigest()[:8]
        assert sid == f"file-ops__imp_{h}"

    def test_generate_derived_id_format(self):
        sid = generate_skill_id("file-ops", Origin.DERIVED, generation=2)
        h = hashlib.sha256("file-ops2".encode()).hexdigest()[:8]
        assert sid == f"file-ops__v_2_{h}"

    def test_generate_fixed_id_format(self):
        sid = generate_skill_id("shell-ops", Origin.FIXED, generation=1)
        h = hashlib.sha256("shell-ops1".encode()).hexdigest()[:8]
        assert sid == f"shell-ops__v_1_{h}"

    def test_generate_captured_id_format(self):
        sid = generate_skill_id("new-skill", Origin.CAPTURED, generation=0)
        h = hashlib.sha256("new-skill0".encode()).hexdigest()[:8]
        assert sid == f"new-skill__v_0_{h}"

    def test_imported_id_deterministic(self):
        sid1 = generate_skill_id("test", Origin.IMPORTED)
        sid2 = generate_skill_id("test", Origin.IMPORTED)
        assert sid1 == sid2

    def test_different_names_different_ids(self):
        sid1 = generate_skill_id("alpha", Origin.IMPORTED)
        sid2 = generate_skill_id("beta", Origin.IMPORTED)
        assert sid1 != sid2

    def test_different_generations_different_ids(self):
        sid1 = generate_skill_id("skill", Origin.DERIVED, generation=1)
        sid2 = generate_skill_id("skill", Origin.DERIVED, generation=2)
        assert sid1 != sid2

    def test_write_sidecar_creates_file(self, tmp_path: Path):
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        write_sidecar(str(skill_dir), "skill_id_123")
        sidecar = skill_dir / ".skill_id"
        assert sidecar.exists()
        assert sidecar.read_text().strip() == "skill_id_123"

    def test_read_sidecar_returns_content(self, tmp_path: Path):
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / ".skill_id").write_text("abc_def\n")
        result = read_sidecar(str(skill_dir))
        assert result == "abc_def"

    def test_read_sidecar_missing_returns_none(self, tmp_path: Path):
        assert read_sidecar(str(tmp_path)) is None

    def test_write_sidecar_overwrite_protection(self, tmp_path: Path):
        """Should not overwrite if content is the same."""
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        sidecar = skill_dir / ".skill_id"
        write_sidecar(str(skill_dir), "same_id")
        mtime1 = sidecar.stat().st_mtime_ns
        write_sidecar(str(skill_dir), "same_id")
        mtime2 = sidecar.stat().st_mtime_ns
        assert mtime1 == mtime2  # File should not have been rewritten

    def test_write_sidecar_updates_on_change(self, tmp_path: Path):
        """Should overwrite when content differs."""
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        write_sidecar(str(skill_dir), "old_id")
        write_sidecar(str(skill_dir), "new_id")
        result = read_sidecar(str(skill_dir))
        assert result == "new_id"

    def test_snapshot_skill_dir(self, tmp_path: Path):
        skill_dir = tmp_path / "test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Skill\nDescription here.")
        (skill_dir / "metadata.json").write_text('{"name": "test"}')
        result = snapshot_skill_dir(str(skill_dir))
        assert "SKILL.md" in result
        assert "metadata.json" in result
        assert result["SKILL.md"] == "# Skill\nDescription here."

    def test_snapshot_skill_dir_partial(self, tmp_path: Path):
        """Only existing files should be included."""
        skill_dir = tmp_path / "partial-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Only SKILL.md")
        result = snapshot_skill_dir(str(skill_dir))
        assert "SKILL.md" in result
        assert "metadata.json" not in result

    def test_snapshot_skill_dir_empty(self, tmp_path: Path):
        """Empty directory should return empty dict."""
        skill_dir = tmp_path / "empty-skill"
        skill_dir.mkdir()
        result = snapshot_skill_dir(str(skill_dir))
        assert result == {}


# ===========================================================================
# TestBugfixes — regression tests for C1, H1-H4, M1-M2
# ===========================================================================


class TestBugfixes:
    """Regression tests for issues found during code review."""

    def test_cte_depth_limit_prevents_infinite_loop(
        self, store: SkillVersionStore, tmp_path: Path
    ):
        """C1: Lineage chain with depth > _MAX_LINEAGE_DEPTH stops gracefully."""
        # Use a small depth limit for testability
        store._MAX_LINEAGE_DEPTH = 5

        # Build a chain of 8 generations (exceeds limit of 5)
        prev_id = "depth_g0"
        g0 = _make_skill(name="depth", skill_id="depth_g0")
        store.register_skill(g0)
        for i in range(1, 8):
            sid = f"depth_g{i}"
            sv = _make_skill(
                name="depth",
                skill_id=sid,
                origin=Origin.DERIVED,
                generation=i,
                parent_ids=[prev_id],
            )
            store.evolve_skill(prev_id, sv, deactivate_parent=True)
            prev_id = sid

        # Walk lineage from the deepest node — should be capped
        chain = store.get_lineage_chain("depth_g7")
        # The chain should stop at depth limit (6 entries: depth 0..5)
        assert len(chain) <= 6
        assert chain[0].skill_id == "depth_g7"

    def test_evolve_skill_nonexistent_parent_raises(
        self, store: SkillVersionStore
    ):
        """H4: evolve_skill raises ValueError when parent doesn't exist."""
        child = _make_skill(
            name="orphan",
            skill_id="orphan_child",
            parent_ids=["nonexistent_parent"],
        )
        with pytest.raises(ValueError, match="does not exist"):
            store.evolve_skill("nonexistent_parent", child)

    def test_duplicate_register_raises(self, store: SkillVersionStore):
        """Registering the same skill_id twice should raise IntegrityError."""
        sv = _make_skill(name="dupe", skill_id="dupe_1")
        store.register_skill(sv)
        with pytest.raises(Exception):
            store.register_skill(sv)

    def test_get_active_version_returns_latest_generation(
        self, store: SkillVersionStore
    ):
        """H2: When multiple active versions exist, latest generation wins."""
        # Insert two versions of the same name, both active
        v1 = _make_skill(
            name="multi-active",
            skill_id="ma_g0",
            generation=0,
            is_active=True,
        )
        v2 = _make_skill(
            name="multi-active",
            skill_id="ma_g3",
            generation=3,
            is_active=True,
        )
        store.register_skill(v1)
        store.register_skill(v2)
        active = store.get_active_version("multi-active")
        assert active is not None
        assert active.skill_id == "ma_g3"
        assert active.lineage.generation == 3

    def test_content_diff_persisted(self, store: SkillVersionStore):
        """H3: content_diff should survive a register/get roundtrip."""
        sv = _make_skill(name="diff-test", skill_id="diff_1")
        sv.lineage.content_diff = "--- a/SKILL.md\n+++ b/SKILL.md\n@@ added line"
        store.register_skill(sv)
        loaded = store.get_skill("diff_1")
        assert loaded is not None
        assert loaded.lineage.content_diff == sv.lineage.content_diff

    def test_content_diff_none_by_default(self, store: SkillVersionStore):
        """H3: content_diff defaults to None when not set."""
        sv = _make_skill(name="no-diff", skill_id="nodiff_1")
        store.register_skill(sv)
        loaded = store.get_skill("nodiff_1")
        assert loaded is not None
        assert loaded.lineage.content_diff is None

    def test_foreign_keys_enforced(self, store: SkillVersionStore):
        """H4/M1: Foreign key constraints are actually enforced."""
        # Try to insert a lineage row referencing a non-existent parent
        conn = store._get_conn()
        try:
            # First register a child skill
            sv = _make_skill(name="fk-test", skill_id="fk_child")
            store.register_skill(sv)
            # Try to insert a lineage row with a bogus parent_id
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO skill_lineage_parents (skill_id, parent_id) "
                    "VALUES (?, ?)",
                    ("fk_child", "bogus_parent"),
                )
                conn.commit()
        finally:
            conn.close()

    def test_name_active_index_exists(self, store: SkillVersionStore):
        """M2: The (name, is_active) index should exist."""
        conn = store._get_conn()
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name='idx_skill_versions_name_active'"
            ).fetchall()
            assert len(rows) == 1
        finally:
            conn.close()
