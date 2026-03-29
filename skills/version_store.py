"""SQLite-backed skill version store with lineage tracking and rollback.

Provides durable storage for:
- Skill version records (identity, metadata, stats)
- Parent-child lineage relationships (DAG)
- Compressed content snapshots for rollback
- Execution analyses for skill health tracking (Phase 2)

Uses connection-per-call pattern for async safety — each public method opens,
executes, commits, and closes its own connection.  WAL mode + busy_timeout
provide crash safety and concurrent-read performance.

Follows the same SQLite conventions as novatrade/storage/state_store.py.
"""

from __future__ import annotations

import gzip
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from skills.execution_analysis import ExecutionAnalysis, SkillHealth
from skills.skill_record import (
    ExecutionStats,
    Origin,
    SkillLineage,
    SkillVersion,
)

log = logging.getLogger("skills.version_store")

_SCHEMA_VERSION = 2

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS skill_versions (
    skill_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    path TEXT,
    is_active INTEGER DEFAULT 1,
    version TEXT DEFAULT '1.0.0',
    origin TEXT NOT NULL,
    generation INTEGER DEFAULT 0,
    change_summary TEXT,
    content_diff TEXT,
    selections INTEGER DEFAULT 0,
    executions INTEGER DEFAULT 0,
    completions INTEGER DEFAULT 0,
    failures INTEGER DEFAULT 0,
    fallbacks INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    created_by TEXT DEFAULT 'system'
);

CREATE INDEX IF NOT EXISTS idx_skill_versions_name_active
    ON skill_versions(name, is_active);

CREATE TABLE IF NOT EXISTS skill_lineage_parents (
    skill_id TEXT NOT NULL,
    parent_id TEXT NOT NULL,
    FOREIGN KEY (skill_id) REFERENCES skill_versions(skill_id),
    FOREIGN KEY (parent_id) REFERENCES skill_versions(skill_id),
    PRIMARY KEY (skill_id, parent_id)
);

CREATE TABLE IF NOT EXISTS skill_snapshots (
    skill_id TEXT PRIMARY KEY,
    content BLOB NOT NULL,
    snapshot_at TEXT NOT NULL,
    FOREIGN KEY (skill_id) REFERENCES skill_versions(skill_id)
);

CREATE TABLE IF NOT EXISTS execution_analyses (
    analysis_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT DEFAULT '',
    quality_score REAL DEFAULT 0.5,
    tool_issues TEXT DEFAULT '[]',
    evolution_type TEXT,
    evolution_direction TEXT,
    evolution_priority INTEGER,
    analyzed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_analyses_skill ON execution_analyses(skill_id);
CREATE INDEX IF NOT EXISTS idx_analyses_task ON execution_analyses(task_id);
"""

_VALID_STAT_FIELDS = frozenset(
    {"selections", "executions", "completions", "failures", "fallbacks"}
)


class SkillVersionStore:
    """SQLite-backed persistence for skill versions and lineage.

    All methods are synchronous and safe to call from async contexts
    (each call uses its own short-lived connection).

    Args:
        db_path: Path to the SQLite database file. Created if it doesn't exist.
    """

    # Maximum recursion depth for lineage CTE queries.
    _MAX_LINEAGE_DEPTH: int = 100

    # M5: Health threshold constants
    _COMPLETION_RATE_FLOOR: float = 0.4
    _FALLBACK_RATE_CEIL: float = 0.5

    def __init__(self, db_path: str | Path = "STATE/skill_versions.db") -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Create a new connection with standard pragmas."""
        conn = sqlite3.connect(str(self._db_path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create tables, run migrations, and set schema version."""
        conn = self._get_conn()
        try:
            # C2 FIX: Check existing schema version before applying tables
            current_version = conn.execute("PRAGMA user_version").fetchone()[0]
            self._migrate_schema(conn, current_version)
            conn.executescript(_CREATE_TABLES)
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            conn.commit()
            log.info("skill_version_store: initialized at %s (schema v%d)", self._db_path, _SCHEMA_VERSION)
        except Exception:
            log.exception("skill_version_store: failed to initialize database")
        finally:
            conn.close()

    def _migrate_schema(self, conn: sqlite3.Connection, from_version: int) -> None:
        """Run schema migrations for existing databases.

        Migration path:
        - v0/v1 -> v2: execution_analyses table (created via IF NOT EXISTS
          in _CREATE_TABLES, but we log the migration for observability).

        All table creation uses IF NOT EXISTS so migrations are idempotent.
        """
        if from_version < 2:
            if from_version > 0:
                log.info(
                    "skill_version_store: migrating schema v%d -> v%d",
                    from_version,
                    _SCHEMA_VERSION,
                )
            # Tables are created idempotently by _CREATE_TABLES.
            # Future migrations that ALTER existing tables go here.

    # -----------------------------------------------------------------
    # Row <-> SkillVersion mapping
    # -----------------------------------------------------------------

    def _row_to_skill(
        self, row: sqlite3.Row, parent_ids: list[str] | None = None
    ) -> SkillVersion:
        """Convert a database row to a SkillVersion instance."""
        pids = parent_ids if parent_ids is not None else []
        return SkillVersion(
            skill_id=row["skill_id"],
            name=row["name"],
            description=row["description"] or "",
            path=row["path"] or "",
            is_active=bool(row["is_active"]),
            version=row["version"] or "1.0.0",
            lineage=SkillLineage(
                origin=Origin(row["origin"]),
                generation=row["generation"] or 0,
                parent_ids=pids,
                content_diff=row["content_diff"],
                change_summary=row["change_summary"],
            ),
            stats=ExecutionStats(
                selections=row["selections"] or 0,
                executions=row["executions"] or 0,
                completions=row["completions"] or 0,
                failures=row["failures"] or 0,
                fallbacks=row["fallbacks"] or 0,
            ),
            created_at=row["created_at"],
            created_by=row["created_by"] or "system",
        )

    def _load_parent_ids(
        self, conn: sqlite3.Connection, skill_id: str
    ) -> list[str]:
        """Load parent IDs for a skill from the lineage table."""
        rows = conn.execute(
            "SELECT parent_id FROM skill_lineage_parents WHERE skill_id = ?",
            (skill_id,),
        ).fetchall()
        return [r["parent_id"] for r in rows]

    # -----------------------------------------------------------------
    # CRUD operations
    # -----------------------------------------------------------------

    def register_skill(self, sv: SkillVersion) -> None:
        """Insert a new skill version record with its lineage parents."""
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO skill_versions
                   (skill_id, name, description, path, is_active, version,
                    origin, generation, change_summary, content_diff,
                    selections, executions, completions, failures, fallbacks,
                    created_at, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sv.skill_id,
                    sv.name,
                    sv.description,
                    sv.path,
                    int(sv.is_active),
                    sv.version,
                    sv.lineage.origin.value,
                    sv.lineage.generation,
                    sv.lineage.change_summary,
                    sv.lineage.content_diff,
                    sv.stats.selections,
                    sv.stats.executions,
                    sv.stats.completions,
                    sv.stats.failures,
                    sv.stats.fallbacks,
                    sv.created_at,
                    sv.created_by,
                ),
            )
            for parent_id in sv.lineage.parent_ids:
                conn.execute(
                    "INSERT INTO skill_lineage_parents (skill_id, parent_id) VALUES (?, ?)",
                    (sv.skill_id, parent_id),
                )
            conn.commit()
        except Exception:
            log.exception(
                "skill_version_store: failed to register skill %s",
                sv.skill_id,
            )
            raise
        finally:
            conn.close()

    def get_skill(self, skill_id: str) -> SkillVersion | None:
        """Load a skill version by its ID. Returns None if not found."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM skill_versions WHERE skill_id = ?",
                (skill_id,),
            ).fetchone()
            if row is None:
                return None
            parent_ids = self._load_parent_ids(conn, skill_id)
            return self._row_to_skill(row, parent_ids)
        except Exception:
            log.exception(
                "skill_version_store: failed to get skill %s", skill_id
            )
            return None
        finally:
            conn.close()

    def get_active_version(self, name: str) -> SkillVersion | None:
        """Load the active version of a skill by name.

        When multiple active versions exist, returns the one with the highest
        generation number (latest evolution). Returns None if not found.
        """
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM skill_versions WHERE name = ? AND is_active = 1 "
                "ORDER BY generation DESC LIMIT 1",
                (name,),
            ).fetchone()
            if row is None:
                return None
            parent_ids = self._load_parent_ids(conn, row["skill_id"])
            return self._row_to_skill(row, parent_ids)
        except Exception:
            log.exception(
                "skill_version_store: failed to get active version for %s",
                name,
            )
            return None
        finally:
            conn.close()

    def list_skills(self, active_only: bool = True) -> list[SkillVersion]:
        """List all skill versions, optionally filtered to active only."""
        conn = self._get_conn()
        try:
            if active_only:
                rows = conn.execute(
                    "SELECT * FROM skill_versions WHERE is_active = 1"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM skill_versions"
                ).fetchall()
            results = []
            for row in rows:
                parent_ids = self._load_parent_ids(conn, row["skill_id"])
                results.append(self._row_to_skill(row, parent_ids))
            return results
        except Exception:
            log.exception("skill_version_store: failed to list skills")
            return []
        finally:
            conn.close()

    # -----------------------------------------------------------------
    # Evolution and rollback
    # -----------------------------------------------------------------

    def evolve_skill(
        self,
        parent_id: str,
        new_version: SkillVersion,
        deactivate_parent: bool = True,
    ) -> None:
        """Atomically insert a new skill version and optionally deactivate parent.

        Runs in a single transaction so either both succeed or neither does.

        Raises:
            ValueError: If ``parent_id`` does not exist in the store.
        """
        conn = self._get_conn()
        try:
            # Validate parent exists before starting transaction
            parent_row = conn.execute(
                "SELECT skill_id FROM skill_versions WHERE skill_id = ?",
                (parent_id,),
            ).fetchone()
            if parent_row is None:
                raise ValueError(
                    f"Parent skill '{parent_id}' does not exist — "
                    "cannot evolve from a non-existent parent"
                )
            conn.execute("BEGIN")
            # Insert the new version
            conn.execute(
                """INSERT INTO skill_versions
                   (skill_id, name, description, path, is_active, version,
                    origin, generation, change_summary, content_diff,
                    selections, executions, completions, failures, fallbacks,
                    created_at, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_version.skill_id,
                    new_version.name,
                    new_version.description,
                    new_version.path,
                    int(new_version.is_active),
                    new_version.version,
                    new_version.lineage.origin.value,
                    new_version.lineage.generation,
                    new_version.lineage.change_summary,
                    new_version.lineage.content_diff,
                    new_version.stats.selections,
                    new_version.stats.executions,
                    new_version.stats.completions,
                    new_version.stats.failures,
                    new_version.stats.fallbacks,
                    new_version.created_at,
                    new_version.created_by,
                ),
            )
            # Insert lineage parents
            for pid in new_version.lineage.parent_ids:
                conn.execute(
                    "INSERT INTO skill_lineage_parents (skill_id, parent_id) VALUES (?, ?)",
                    (new_version.skill_id, pid),
                )
            # Deactivate parent if requested
            if deactivate_parent:
                conn.execute(
                    "UPDATE skill_versions SET is_active = 0 WHERE skill_id = ?",
                    (parent_id,),
                )
            conn.commit()
        except Exception:
            log.exception(
                "skill_version_store: failed to evolve skill %s -> %s",
                parent_id,
                new_version.skill_id,
            )
            raise
        finally:
            conn.close()

    def rollback_skill(self, skill_id: str) -> SkillVersion | None:
        """Rollback to parent: deactivate current version, reactivate parent.

        Returns the reactivated parent SkillVersion, or None if no parent exists.
        """
        conn = self._get_conn()
        try:
            # Find the parent(s) of this skill
            parent_rows = conn.execute(
                "SELECT parent_id FROM skill_lineage_parents WHERE skill_id = ?",
                (skill_id,),
            ).fetchall()
            if not parent_rows:
                return None
            # Use the first parent for rollback
            parent_id = parent_rows[0]["parent_id"]
            conn.execute("BEGIN")
            # Deactivate current
            conn.execute(
                "UPDATE skill_versions SET is_active = 0 WHERE skill_id = ?",
                (skill_id,),
            )
            # Reactivate parent
            conn.execute(
                "UPDATE skill_versions SET is_active = 1 WHERE skill_id = ?",
                (parent_id,),
            )
            conn.commit()
            # Load and return parent
            parent_row = conn.execute(
                "SELECT * FROM skill_versions WHERE skill_id = ?",
                (parent_id,),
            ).fetchone()
            if parent_row is None:
                return None
            parent_pids = self._load_parent_ids(conn, parent_id)
            return self._row_to_skill(parent_row, parent_pids)
        except Exception:
            log.exception(
                "skill_version_store: failed to rollback skill %s", skill_id
            )
            return None
        finally:
            conn.close()

    # -----------------------------------------------------------------
    # Stats
    # -----------------------------------------------------------------

    def increment_stat(self, skill_id: str, field: str) -> None:
        """Atomically increment a stat field by 1.

        Valid fields: selections, executions, completions, failures, fallbacks.
        Raises ValueError for invalid field names.
        """
        if field not in _VALID_STAT_FIELDS:
            raise ValueError(
                f"Invalid stat field '{field}'. Must be one of: {sorted(_VALID_STAT_FIELDS)}"
            )
        conn = self._get_conn()
        try:
            conn.execute(
                f"UPDATE skill_versions SET {field} = {field} + 1 WHERE skill_id = ?",  # noqa: S608
                (skill_id,),
            )
            conn.commit()
        except Exception:
            log.exception(
                "skill_version_store: failed to increment %s for %s",
                field,
                skill_id,
            )
            raise
        finally:
            conn.close()

    # -----------------------------------------------------------------
    # Execution analysis storage
    # -----------------------------------------------------------------

    def store_analysis(self, analysis: ExecutionAnalysis) -> None:
        """Store execution analysis results for skill health tracking.

        For each SkillJudgment in the analysis, inserts a row into
        execution_analyses. All matching EvolutionSuggestions for the same
        skill are serialized as a JSON array in the evolution_type column
        (H1 FIX — previously only the first suggestion was stored).

        Uses INSERT OR IGNORE to make repeated calls idempotent when the
        same analysis_id is generated (UUID4 guarantees uniqueness in
        practice).

        The entire batch is wrapped in a transaction (M4 FIX) so either
        all judgments are stored or none are.
        """
        conn = self._get_conn()
        try:
            # Build a lookup of evolution suggestions keyed by skill_name
            suggestions_by_skill: dict[str, list] = {}
            for suggestion in analysis.evolution_suggestions:
                suggestions_by_skill.setdefault(
                    suggestion.target_skill_name, []
                ).append(suggestion)

            analyzed_at = analysis.analysis_timestamp or datetime.now(
                timezone.utc
            ).isoformat()

            # M4 FIX: Wrap multi-insert in explicit transaction
            conn.execute("BEGIN")

            for judgment in analysis.skill_judgments:
                analysis_id = str(uuid.uuid4())

                # H1 FIX: Serialize ALL matching suggestions as JSON array,
                # not just the first one. Backwards-compatible: callers
                # reading evolution_type can still check for a single value,
                # and get_analyses_for_skill returns the full list.
                matching_suggestions = suggestions_by_skill.get(
                    judgment.skill_name, []
                )
                if matching_suggestions:
                    evolution_data = json.dumps([
                        {"type": s.type, "direction": s.direction, "priority": s.priority}
                        for s in matching_suggestions
                    ])
                    # Store first suggestion in legacy columns for simple queries
                    evolution_type = matching_suggestions[0].type
                    evolution_direction = json.dumps([
                        {"type": s.type, "direction": s.direction, "priority": s.priority}
                        for s in matching_suggestions
                    ]) if len(matching_suggestions) > 1 else matching_suggestions[0].direction
                    evolution_priority = matching_suggestions[0].priority
                else:
                    evolution_type = None
                    evolution_direction = None
                    evolution_priority = None

                conn.execute(
                    """INSERT OR IGNORE INTO execution_analyses
                       (analysis_id, task_id, skill_id, skill_name,
                        applied, completed, failure_reason, quality_score,
                        tool_issues, evolution_type, evolution_direction,
                        evolution_priority, analyzed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        analysis_id,
                        analysis.task_id,
                        judgment.skill_id or "",
                        judgment.skill_name,
                        int(judgment.applied),
                        int(judgment.completed),
                        judgment.failure_reason,
                        judgment.quality_score,
                        json.dumps(judgment.tool_issues),
                        evolution_type,
                        evolution_direction,
                        evolution_priority,
                        analyzed_at,
                    ),
                )
            conn.commit()
        except Exception:
            log.exception(
                "skill_version_store: failed to store analysis for task %s",
                analysis.task_id,
            )
            raise
        finally:
            conn.close()

    def get_analyses_for_skill(
        self, skill_id: str, limit: int = 20
    ) -> list[dict]:
        """Return recent execution analyses for a specific skill.

        Results are ordered by analyzed_at descending (most recent first).
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT * FROM execution_analyses
                   WHERE skill_id = ?
                   ORDER BY analyzed_at DESC
                   LIMIT ?""",
                (skill_id, limit),
            ).fetchall()
            results = []
            for row in rows:
                # H1: Parse evolution_direction — if it's a JSON array,
                # it contains all suggestions; otherwise it's a plain string.
                evo_direction = row["evolution_direction"]
                evolution_suggestions = []
                try:
                    parsed = json.loads(evo_direction) if evo_direction else None
                    if isinstance(parsed, list):
                        evolution_suggestions = parsed
                except (json.JSONDecodeError, TypeError):
                    pass

                results.append(
                    {
                        "analysis_id": row["analysis_id"],
                        "task_id": row["task_id"],
                        "skill_id": row["skill_id"],
                        "skill_name": row["skill_name"],
                        "applied": bool(row["applied"]),
                        "completed": bool(row["completed"]),
                        "failure_reason": row["failure_reason"] or "",
                        "quality_score": row["quality_score"],
                        "tool_issues": json.loads(row["tool_issues"] or "[]"),
                        "evolution_type": row["evolution_type"],
                        "evolution_direction": evo_direction if not evolution_suggestions else evolution_suggestions[0].get("direction", evo_direction),
                        "evolution_priority": row["evolution_priority"],
                        "evolution_suggestions": evolution_suggestions,
                        "analyzed_at": row["analyzed_at"],
                    }
                )
            return results
        except Exception:
            log.exception(
                "skill_version_store: failed to get analyses for skill %s",
                skill_id,
            )
            return []
        finally:
            conn.close()

    def get_skill_health(self, min_selections: int = 5) -> list[SkillHealth]:
        """Compute health metrics for all active skills.

        Only includes skills with at least ``min_selections`` selections
        to avoid noisy metrics on rarely-used skills.

        Flags a skill as unhealthy when:
        - completion_rate < 0.4 (fewer than 40% of executions complete)
        - fallback_rate > 0.5 (more than 50% of selections fall back)
        """
        conn = self._get_conn()
        try:
            # Get all active skills with sufficient selections
            rows = conn.execute(
                """SELECT skill_id, name, selections, executions,
                          completions, failures, fallbacks
                   FROM skill_versions
                   WHERE is_active = 1 AND selections >= ?""",
                (min_selections,),
            ).fetchall()

            results = []
            for row in rows:
                skill_id = row["skill_id"]
                selections = row["selections"] or 0
                executions = row["executions"] or 0
                completions = row["completions"] or 0
                failures = row["failures"] or 0
                fallbacks = row["fallbacks"] or 0

                # Compute rates safely (handle zero divisions)
                applied_rate = executions / selections if selections > 0 else 0.0
                completion_rate = completions / executions if executions > 0 else 0.0
                effective_rate = completions / selections if selections > 0 else 0.0
                fallback_rate = fallbacks / selections if selections > 0 else 0.0

                # Query avg quality score from execution_analyses
                avg_row = conn.execute(
                    """SELECT AVG(quality_score) as avg_q
                       FROM execution_analyses
                       WHERE skill_id = ?""",
                    (skill_id,),
                ).fetchone()
                avg_quality = avg_row["avg_q"] if avg_row and avg_row["avg_q"] is not None else 0.0

                # M5: Use class constants for health thresholds
                health_issues = []
                if completion_rate < self._COMPLETION_RATE_FLOOR:
                    health_issues.append(
                        f"Low completion rate: {completion_rate:.2f} (threshold: {self._COMPLETION_RATE_FLOOR:.2f})"
                    )
                if fallback_rate > self._FALLBACK_RATE_CEIL:
                    health_issues.append(
                        f"High fallback rate: {fallback_rate:.2f} (threshold: {self._FALLBACK_RATE_CEIL:.2f})"
                    )

                is_healthy = len(health_issues) == 0

                results.append(
                    SkillHealth(
                        skill_name=row["name"],
                        skill_id=skill_id,
                        selections=selections,
                        executions=executions,
                        completions=completions,
                        failures=failures,
                        fallbacks=fallbacks,
                        applied_rate=round(applied_rate, 4),
                        completion_rate=round(completion_rate, 4),
                        effective_rate=round(effective_rate, 4),
                        fallback_rate=round(fallback_rate, 4),
                        avg_quality_score=round(avg_quality, 4),
                        is_healthy=is_healthy,
                        health_issues=health_issues,
                    )
                )
            return results
        except Exception:
            log.exception("skill_version_store: failed to compute skill health")
            return []
        finally:
            conn.close()

    # -----------------------------------------------------------------
    # Lineage chain (recursive CTE)
    # -----------------------------------------------------------------

    def get_lineage_chain(self, skill_id: str) -> list[SkillVersion]:
        """Walk the parent chain using WITH RECURSIVE CTE.

        Returns a list starting with the given skill and walking back to the
        root ancestor.  Returns empty list if skill_id is not found.

        The recursion is capped at ``_MAX_LINEAGE_DEPTH`` generations to
        prevent infinite loops if cycles exist in the lineage graph.
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """
                WITH RECURSIVE chain(sid, depth) AS (
                    SELECT ?, 0
                    UNION ALL
                    SELECT lp.parent_id, chain.depth + 1
                    FROM skill_lineage_parents lp
                    JOIN chain ON lp.skill_id = chain.sid
                    WHERE chain.depth < ?
                )
                SELECT sv.*, chain.depth
                FROM chain
                JOIN skill_versions sv ON sv.skill_id = chain.sid
                ORDER BY chain.depth ASC
                """,
                (skill_id, self._MAX_LINEAGE_DEPTH),
            ).fetchall()
            if len(rows) >= self._MAX_LINEAGE_DEPTH:
                log.warning(
                    "skill_version_store: lineage chain for %s hit depth "
                    "limit (%d) — possible cycle in lineage graph",
                    skill_id,
                    self._MAX_LINEAGE_DEPTH,
                )
            results = []
            for row in rows:
                parent_ids = self._load_parent_ids(conn, row["skill_id"])
                results.append(self._row_to_skill(row, parent_ids))
            return results
        except Exception:
            log.exception(
                "skill_version_store: failed to get lineage chain for %s",
                skill_id,
            )
            return []
        finally:
            conn.close()

    # -----------------------------------------------------------------
    # Content snapshots
    # -----------------------------------------------------------------

    def save_snapshot(self, skill_id: str, content: dict[str, str]) -> None:
        """Save a gzip-compressed content snapshot for a skill.

        Args:
            skill_id: The skill version ID.
            content: Dict mapping filename to file content (e.g. SKILL.md text).
        """
        compressed = gzip.compress(
            json.dumps(content).encode("utf-8")
        )
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO skill_snapshots
                   (skill_id, content, snapshot_at)
                   VALUES (?, ?, ?)""",
                (skill_id, compressed, now),
            )
            conn.commit()
        except Exception:
            log.exception(
                "skill_version_store: failed to save snapshot for %s",
                skill_id,
            )
            raise
        finally:
            conn.close()

    def get_snapshot(self, skill_id: str) -> dict[str, str] | None:
        """Retrieve and decompress a content snapshot.

        Returns the dict mapping filename to content, or None if not found.
        """
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT content FROM skill_snapshots WHERE skill_id = ?",
                (skill_id,),
            ).fetchone()
            if row is None:
                return None
            decompressed = gzip.decompress(row["content"])
            return json.loads(decompressed.decode("utf-8"))
        except Exception:
            log.exception(
                "skill_version_store: failed to get snapshot for %s",
                skill_id,
            )
            return None
        finally:
            conn.close()

    # -----------------------------------------------------------------
    # Cleanup
    # -----------------------------------------------------------------

    def close(self) -> None:
        """No-op cleanup (connection-per-call pattern means no persistent conn)."""
        pass


# -----------------------------------------------------------------
# Sidecar file helpers
# -----------------------------------------------------------------


def write_sidecar(skill_dir: str, skill_id: str) -> None:
    """Write a .skill_id sidecar file to a skill directory.

    Only writes if the file doesn't exist or its content differs.
    """
    p = Path(skill_dir) / ".skill_id"
    if p.exists():
        existing = p.read_text(encoding="utf-8").strip()
        if existing == skill_id:
            return
    p.write_text(skill_id + "\n", encoding="utf-8")


def read_sidecar(skill_dir: str) -> str | None:
    """Read the .skill_id sidecar file from a skill directory.

    Returns the skill_id string (stripped), or None if file doesn't exist.
    """
    p = Path(skill_dir) / ".skill_id"
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8").strip()


def snapshot_skill_dir(skill_dir: str) -> dict[str, str]:
    """Read SKILL.md and metadata.json from a skill directory.

    Returns a dict mapping filename to file content.
    Skips files that don't exist.
    """
    result: dict[str, str] = {}
    d = Path(skill_dir)
    for filename in ("SKILL.md", "metadata.json"):
        filepath = d / filename
        if filepath.exists():
            result[filename] = filepath.read_text(encoding="utf-8")
    return result


def register_existing_skills(
    store: SkillVersionStore,
    skills: list,
) -> int:
    """Register existing on-disk skills in the version store (idempotent).

    For each ``Skill`` object (from ``tools.skills.load_skills()``), checks
    whether an active version already exists.  If not, creates a
    ``SkillVersion`` with ``origin=IMPORTED`` and ``generation=0``, takes
    an initial content snapshot, and writes a ``.skill_id`` sidecar file.

    Safe to call on every startup — already-registered skills are skipped.

    Args:
        store: The ``SkillVersionStore`` instance to register into.
        skills: List of ``Skill`` objects from ``load_skills()``.

    Returns:
        Count of newly registered skills (0 when fully caught-up).
    """
    from skills.skill_record import generate_skill_id

    registered = 0
    for skill in skills:
        try:
            # Skip if already registered
            existing = store.get_active_version(skill.name)
            if existing is not None:
                continue

            skill_id = generate_skill_id(skill.name, Origin.IMPORTED, generation=0)
            skill_dir = str(skill.path.parent) if skill.path and skill.path.parent != Path(".") else ""

            sv = SkillVersion(
                skill_id=skill_id,
                name=skill.name,
                description=skill.description,
                path=skill_dir,
                lineage=SkillLineage(
                    origin=Origin.IMPORTED,
                    generation=0,
                ),
                is_active=True,
                version=skill.version or "1.0.0",
                stats=ExecutionStats(),
            )
            store.register_skill(sv)

            # Capture initial content snapshot
            if skill_dir:
                content = snapshot_skill_dir(skill_dir)
                if content:
                    store.save_snapshot(skill_id, content)

            # Write sidecar so future runs can look up the skill_id
            if skill_dir:
                write_sidecar(skill_dir, skill_id)

            registered += 1
            log.info(
                "register_existing_skills: registered %s as %s",
                skill.name,
                skill_id,
            )
        except Exception:
            log.warning(
                "register_existing_skills: failed to register %s (skipping)",
                skill.name,
                exc_info=True,
            )
    return registered
