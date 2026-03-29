"""Migrate legacy skill_usage_history.json into the SkillVersionStore.

Reads STATE/skill_usage_history.json and creates SkillVersion records for each
skill entry.  Migration is idempotent — existing skill_ids are skipped.

Mapping:
    runs         -> executions
    successes    -> completions
    failures     -> failures
    selections   -> 0 (no legacy data)
    fallbacks    -> 0 (no legacy data)

Legacy fields avg_duration_ms, last_used_ts, total_retries are preserved
in change_summary as a JSON string.

Usage:
    python scripts/migrate_skill_history.py
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path

# Ensure project root is on sys.path
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from skills.skill_record import (  # noqa: E402
    ExecutionStats,
    Origin,
    SkillLineage,
    SkillVersion,
    generate_skill_id,
)
from skills.version_store import SkillVersionStore  # noqa: E402

log = logging.getLogger("scripts.migrate_skill_history")

DEFAULT_HISTORY_PATH = Path("STATE/skill_usage_history.json")
DEFAULT_DB_PATH = Path("STATE/skill_versions.db")


def load_history(path: Path) -> dict:
    """Load the legacy skill usage history JSON.

    Returns empty dict on missing/corrupt file.
    """
    if not path.exists():
        log.warning("History file not found: %s", path)
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return {}
        data = json.loads(text)
        if not isinstance(data, dict):
            log.warning("History file is not a dict: %s", path)
            return {}
        return data
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to load history file %s: %s", path, exc)
        return {}


def migrate(
    history_path: Path = DEFAULT_HISTORY_PATH,
    db_path: Path = DEFAULT_DB_PATH,
) -> int:
    """Run the migration. Returns the number of skills migrated."""
    history = load_history(history_path)
    if not history:
        log.info("No skills to migrate (empty or missing history)")
        return 0

    store = SkillVersionStore(db_path=db_path)
    migrated = 0

    for name, stats in history.items():
        skill_id = generate_skill_id(name, Origin.IMPORTED, generation=0)

        # Idempotent: skip if already exists
        existing = store.get_skill(skill_id)
        if existing is not None:
            log.debug("Skipping already-migrated skill: %s (%s)", name, skill_id)
            continue

        # Preserve legacy-only fields in change_summary
        legacy_fields = {
            "avg_duration_ms": stats.get("avg_duration_ms", 0),
            "last_used_ts": stats.get("last_used_ts"),
            "total_retries": stats.get("total_retries", 0),
        }

        sv = SkillVersion(
            skill_id=skill_id,
            name=name,
            description="Migrated from skill_usage_history.json",
            path="",
            is_active=True,
            version="1.0.0",
            lineage=SkillLineage(
                origin=Origin.IMPORTED,
                generation=0,
                parent_ids=[],
                change_summary=json.dumps(legacy_fields),
            ),
            stats=ExecutionStats(
                selections=0,
                executions=stats.get("runs", 0),
                completions=stats.get("successes", 0),
                failures=stats.get("failures", 0),
                fallbacks=0,
            ),
            created_by="migration",
        )

        try:
            store.register_skill(sv)
            migrated += 1
            log.info("Migrated skill: %s -> %s", name, skill_id)
        except sqlite3.IntegrityError:
            # Another idempotency guard (race condition / concurrent migration)
            log.debug("Skill already exists (integrity): %s", skill_id)
        except Exception:
            log.exception("Failed to migrate skill: %s", name)

    store.close()
    log.info("Migration complete: %d skills migrated", migrated)
    return migrated


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    count = migrate()
    print(f"Migrated {count} skills")
