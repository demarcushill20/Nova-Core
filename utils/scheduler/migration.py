"""Migration helpers for enriching existing task files with scheduler metadata.

Reads TASKS/*.md files, classifies them, and optionally writes enriched
frontmatter back.  Non-destructive by default (dry_run=True).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

from utils.scheduler.classifier import classify_task
from utils.scheduler.work_unit import WorkMode, WorkUnit

# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from task file content.

    Returns (frontmatter_dict, body_text).  If no valid frontmatter is
    found, returns ({}, full_content).
    """
    if not content.startswith("---"):
        return {}, content

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content

    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}, content

    # yaml.safe_load can return scalars (str, int, bool) or lists — not just dicts
    if not isinstance(fm, dict):
        return {}, content

    body = parts[2]
    return fm, body


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------


def enrich_task(frontmatter: dict, body: str, file_path: str = "") -> WorkUnit:
    """Given existing frontmatter and body, classify and create a WorkUnit.

    Combines frontmatter-based construction with heuristic classification
    to produce a fully-enriched WorkUnit.
    """
    # Start with a WorkUnit from frontmatter (identity, priority, work_mode)
    wu = WorkUnit.from_frontmatter(frontmatter, file_path=file_path)

    # Extract title from body if not in frontmatter
    if not wu.title or wu.title == wu.id:
        # Try first markdown heading
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                wu.title = stripped[2:].strip()
                break

    # Run heuristic classifier for duration estimates + refined work_mode
    classification = classify_task(
        title=wu.title,
        body=body,
        category=str(frontmatter.get("category", "")),
        estimated_effort=str(frontmatter.get("estimated_effort", "")),
    )

    # Apply classification results:
    # - Keyword match is the strongest signal — always wins.
    # - If no keyword matched and from_frontmatter produced a non-default mode
    #   (i.e. shift_block mapping), preserve it over the classifier's
    #   category-only / fallback mode.
    if classification["keyword_matched"] or wu.work_mode == WorkMode.ADMIN:
        wu.work_mode = classification["work_mode"]
    wu.task_class = classification["task_class"]
    wu.estimated_optimistic_min = classification["estimated_optimistic_min"]
    wu.estimated_expected_min = classification["estimated_expected_min"]
    wu.estimated_pessimistic_min = classification["estimated_pessimistic_min"]
    wu.confidence = classification["confidence"]

    return wu


def enrich_task_file(path: Path) -> WorkUnit | None:
    """Read a task file, parse frontmatter, enrich it, return WorkUnit.

    Non-destructive: reads only, does not modify the file.
    Returns None if the file cannot be read or parsed.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None

    fm, body = parse_frontmatter(content)
    return enrich_task(fm, body, file_path=str(path))


# ---------------------------------------------------------------------------
# Write-back
# ---------------------------------------------------------------------------

# Fields added by the scheduler (not part of original frontmatter)
_SCHEDULER_FIELDS = {
    "scheduler_work_mode",
    "scheduler_task_class",
    "scheduler_urgency",
    "scheduler_strategic_value",
    "scheduler_unblock_value",
    "scheduler_risk_reduction",
    "scheduler_est_optimistic_min",
    "scheduler_est_expected_min",
    "scheduler_est_pessimistic_min",
    "scheduler_confidence",
    "scheduler_success_probability",
    "scheduler_context_switch_cost_min",
    "scheduler_commitment_level",
    "scheduler_must_do_today",
    "scheduler_epic_candidate",
}


def _scheduler_fields_from_work_unit(wu: WorkUnit) -> dict:
    """Extract scheduler-specific fields as a flat dict for frontmatter."""
    return {
        "scheduler_work_mode": wu.work_mode.value,
        "scheduler_task_class": wu.task_class.value,
        "scheduler_urgency": round(wu.urgency, 2),
        "scheduler_strategic_value": round(wu.strategic_value, 2),
        "scheduler_unblock_value": round(wu.unblock_value, 2),
        "scheduler_risk_reduction": round(wu.risk_reduction, 2),
        "scheduler_est_optimistic_min": round(wu.estimated_optimistic_min, 1),
        "scheduler_est_expected_min": round(wu.estimated_expected_min, 1),
        "scheduler_est_pessimistic_min": round(wu.estimated_pessimistic_min, 1),
        "scheduler_confidence": round(wu.confidence, 2),
        "scheduler_success_probability": round(wu.success_probability, 2),
        "scheduler_context_switch_cost_min": round(wu.context_switch_cost_min, 1),
        "scheduler_commitment_level": wu.commitment_level.value,
        "scheduler_must_do_today": wu.must_do_today,
        "scheduler_epic_candidate": wu.estimated_expected_min >= 90.0,
    }


def write_enriched_frontmatter(
    path: Path,
    work_unit: WorkUnit,
    dry_run: bool = True,
) -> bool:
    """Write enriched scheduler fields back to a task file.

    - Preserves ALL existing frontmatter fields.
    - Adds scheduler fields under a ``# scheduler`` YAML comment block.
    - Uses atomic write (tempfile + os.replace).
    - dry_run=True by default for safety.

    Returns True if successful (or would succeed in dry_run mode).
    """
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return False

    fm, body = parse_frontmatter(content)

    # Merge: existing fields take precedence, scheduler fields are additive
    scheduler_data = _scheduler_fields_from_work_unit(work_unit)

    # Remove any old scheduler fields from fm (in case of re-enrichment)
    for key in list(fm.keys()):
        if key in _SCHEDULER_FIELDS:
            del fm[key]

    # Build new frontmatter YAML
    # First the original fields, then a comment separator, then scheduler fields
    scheduler_yaml = yaml.dump(scheduler_data, default_flow_style=False, sort_keys=False).strip()

    if fm:
        original_yaml = yaml.dump(fm, default_flow_style=False, sort_keys=False).strip()
        new_frontmatter = f"{original_yaml}\n# scheduler\n{scheduler_yaml}"
    else:
        new_frontmatter = f"# scheduler\n{scheduler_yaml}"

    # Ensure body has a leading newline for clean separation
    if body and not body.startswith("\n"):
        body = "\n" + body
    new_content = f"---\n{new_frontmatter}\n---{body}"

    if dry_run:
        return True

    # Atomic write
    try:
        dir_path = path.parent
        fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new_content)
            os.replace(tmp_path, str(path))
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError:
        return False

    return True


# ---------------------------------------------------------------------------
# Batch migration
# ---------------------------------------------------------------------------


def migrate_all_tasks(
    tasks_dir: Path,
    dry_run: bool = True,
) -> list[dict]:
    """Scan a directory for *.md task files, enrich each, return summary.

    Parameters
    ----------
    tasks_dir : Path
        Directory containing task markdown files.
    dry_run : bool
        If True, only classify — do not modify files.

    Returns
    -------
    list[dict]
        One entry per file with keys: file, title, work_mode, task_class,
        confidence, epic_candidate, enriched (bool), error (str or None).
    """
    results: list[dict] = []

    if not tasks_dir.is_dir():
        return results

    md_files = sorted(tasks_dir.glob("*.md"))

    for md_file in md_files:
        entry: dict = {
            "file": str(md_file),
            "title": "",
            "work_mode": "",
            "task_class": "",
            "confidence": 0.0,
            "epic_candidate": False,
            "enriched": False,
            "error": None,
        }

        try:
            wu = enrich_task_file(md_file)
            if wu is None:
                entry["error"] = "Could not parse file"
                results.append(entry)
                continue

            entry["title"] = wu.title
            entry["work_mode"] = wu.work_mode.value
            entry["task_class"] = wu.task_class.value
            entry["confidence"] = wu.confidence
            entry["epic_candidate"] = wu.estimated_expected_min >= 90.0

            if not dry_run:
                success = write_enriched_frontmatter(md_file, wu, dry_run=False)
                entry["enriched"] = success
            else:
                entry["enriched"] = False  # dry_run, not actually enriched

        except Exception as exc:
            entry["error"] = str(exc)

        results.append(entry)

    return results
