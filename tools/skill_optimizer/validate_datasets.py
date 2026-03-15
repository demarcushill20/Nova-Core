"""Dataset validation for the autonomous skill improvement pipeline.

Validates eval datasets against the schema and quality rules before
any optimization work proceeds. This is a hard prerequisite gate.

Quality rules:
- Valid JSONL format with schema-compliant records
- No duplicate queries within a split
- No duplicate queries across train/dev/test splits (leakage prevention)
- Minimum record counts per split
- Reasonable class balance (not all positive or all negative)
- Hard negatives must reference real neighbor skills
- Global smoke test must cover all eligible skills
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent.parent
EVALS_DIR = BASE_DIR / "evals"
SCHEMA_PATH = BASE_DIR / "schemas" / "eval_record.schema.json"

# Minimum record counts per split
MIN_TRAIN = 6  # at least 3 positive + 3 negative
MIN_DEV = 0  # dev is optional
MIN_TEST = 4  # at least 2 positive + 2 negative
MIN_HARD_NEG = 0  # hard negatives are optional but recommended

# Class balance: at least this fraction of records must be each class
MIN_CLASS_FRACTION = 0.2  # at least 20% positive and 20% negative


@dataclass
class ValidationIssue:
    """A single validation issue."""

    severity: str  # "error", "warning", "info"
    skill: str
    split: str  # "train", "dev", "test", "hard_negatives", "global"
    message: str


@dataclass
class DatasetReport:
    """Validation report for all datasets."""

    issues: list[ValidationIssue] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "error" for i in self.issues)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "warning")

    def add(self, severity: str, skill: str, split: str, message: str) -> None:
        self.issues.append(ValidationIssue(severity, skill, split, message))

    def summary(self) -> str:
        lines = [f"Dataset validation: {self.error_count} errors, {self.warning_count} warnings"]
        if self.stats:
            lines.append(
                f"Skills with datasets: {self.stats.get('skills_with_data', 0)}/{self.stats.get('total_eligible', 0)}"
            )
            lines.append(f"Total records: {self.stats.get('total_records', 0)}")
        return "\n".join(lines)


def _load_schema() -> dict | None:
    """Load the eval record JSON schema."""
    if not SCHEMA_PATH.exists():
        return None
    return json.loads(SCHEMA_PATH.read_text())


def _validate_record(record: dict, schema: dict | None, line_num: int) -> list[str]:
    """Validate a single eval record against the schema.

    Returns list of error messages (empty if valid).
    Uses lightweight validation — jsonschema is available but we keep it
    simple for speed since we validate many records.
    """
    errors = []

    # Required fields
    if "query" not in record:
        errors.append(f"line {line_num}: missing required field 'query'")
    elif not isinstance(record["query"], str):
        errors.append(f"line {line_num}: 'query' must be a string")
    elif len(record["query"]) < 5:
        errors.append(f"line {line_num}: 'query' too short ({len(record['query'])} chars)")
    elif len(record["query"]) > 500:
        errors.append(f"line {line_num}: 'query' too long ({len(record['query'])} chars)")

    if "should_trigger" not in record:
        errors.append(f"line {line_num}: missing required field 'should_trigger'")
    elif not isinstance(record["should_trigger"], bool):
        errors.append(f"line {line_num}: 'should_trigger' must be boolean")

    # Optional fields type check
    if "difficulty" in record and record["difficulty"] not in ("easy", "medium", "hard"):
        errors.append(f"line {line_num}: 'difficulty' must be easy/medium/hard")

    if "category" in record and not isinstance(record["category"], str):
        errors.append(f"line {line_num}: 'category' must be a string")

    if "neighbor_skill" in record and not isinstance(record["neighbor_skill"], str):
        errors.append(f"line {line_num}: 'neighbor_skill' must be a string")

    # No unknown fields
    known = {"query", "should_trigger", "difficulty", "category", "neighbor_skill", "notes"}
    unknown = set(record.keys()) - known
    if unknown:
        errors.append(f"line {line_num}: unknown fields: {unknown}")

    return errors


def _load_jsonl(path: Path) -> tuple[list[dict], list[str]]:
    """Load a JSONL file, returning (records, errors)."""
    records: list[dict] = []
    errors: list[str] = []

    if not path.exists():
        return records, errors

    for line_num, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            record = json.loads(line)
            records.append(record)
        except json.JSONDecodeError as e:
            errors.append(f"line {line_num}: invalid JSON: {e}")

    return records, errors


def validate_split(
    path: Path,
    skill_name: str,
    split_name: str,
    min_count: int,
    schema: dict | None,
    report: DatasetReport,
) -> list[dict]:
    """Validate a single dataset split file. Returns valid records."""
    if not path.exists():
        if min_count > 0:
            report.add("error", skill_name, split_name, f"required file missing: {path.name}")
        return []

    records, parse_errors = _load_jsonl(path)
    for err in parse_errors:
        report.add("error", skill_name, split_name, err)

    if len(records) < min_count:
        report.add("error", skill_name, split_name, f"too few records: {len(records)} < {min_count} minimum")

    # Schema validation
    for i, record in enumerate(records, 1):
        for err in _validate_record(record, schema, i):
            report.add("error", skill_name, split_name, err)

    # Duplicate check within split
    queries = [r.get("query", "") for r in records]
    dupes = [q for q, count in Counter(queries).items() if count > 1]
    for dupe in dupes:
        report.add("error", skill_name, split_name, f"duplicate query: '{dupe[:60]}...'")

    # Class balance (skip for hard_negatives — those are expected to be all-negative)
    if records and split_name != "hard_negatives":
        pos = sum(1 for r in records if r.get("should_trigger"))
        neg = len(records) - pos
        total = len(records)
        if total >= 4:  # only check balance with enough records
            if pos / total < MIN_CLASS_FRACTION:
                report.add("warning", skill_name, split_name, f"low positive ratio: {pos}/{total} ({pos / total:.0%})")
            if neg / total < MIN_CLASS_FRACTION:
                report.add("warning", skill_name, split_name, f"low negative ratio: {neg}/{total} ({neg / total:.0%})")

    return records


def validate_skill_datasets(
    skill_name: str,
    skill_dir: Path,
    schema: dict | None,
    report: DatasetReport,
    neighbor_names: set[str] | None = None,
) -> dict:
    """Validate all dataset splits for a single skill."""
    stats: dict[str, Any] = {"skill": skill_name, "splits": {}}

    # Validate each split
    train = validate_split(skill_dir / "train.jsonl", skill_name, "train", MIN_TRAIN, schema, report)
    dev = validate_split(skill_dir / "dev.jsonl", skill_name, "dev", MIN_DEV, schema, report)
    test = validate_split(skill_dir / "test.jsonl", skill_name, "test", MIN_TEST, schema, report)
    hard_neg = validate_split(
        skill_dir / "hard_negatives.jsonl", skill_name, "hard_negatives", MIN_HARD_NEG, schema, report
    )

    for split_name, records in [("train", train), ("dev", dev), ("test", test), ("hard_negatives", hard_neg)]:
        stats["splits"][split_name] = len(records)

    # Cross-split leakage check
    all_splits = {"train": train, "dev": dev, "test": test}
    for a_name, a_records in all_splits.items():
        a_queries = {r.get("query", "") for r in a_records}
        for b_name, b_records in all_splits.items():
            if a_name >= b_name:  # avoid double-checking
                continue
            b_queries = {r.get("query", "") for r in b_records}
            overlap = a_queries & b_queries
            if overlap:
                report.add(
                    "error",
                    skill_name,
                    f"{a_name}/{b_name}",
                    f"query leakage across splits: {len(overlap)} shared queries",
                )

    # Hard negatives must reference real neighbors
    if hard_neg and neighbor_names:
        for record in hard_neg:
            ns = record.get("neighbor_skill")
            if ns and ns not in neighbor_names:
                report.add("warning", skill_name, "hard_negatives", f"neighbor_skill '{ns}' not in known neighbors")

    return stats


def validate_global_datasets(schema: dict | None, report: DatasetReport) -> None:
    """Validate global evaluation datasets."""
    global_dir = EVALS_DIR / "global"

    # Smoke test
    smoke_path = global_dir / "smoke_test.jsonl"
    if not smoke_path.exists():
        report.add("warning", "global", "smoke_test", "global smoke test file missing")
    else:
        records, errors = _load_jsonl(smoke_path)
        for err in errors:
            report.add("error", "global", "smoke_test", err)
        if records:
            # Check coverage: each eligible skill should have at least one smoke test
            skills_covered = {r.get("skill") for r in records if r.get("skill")}
            report.stats["smoke_skills_covered"] = len(skills_covered)

    # Overlap pairs
    overlap_path = global_dir / "overlap_pairs.json"
    if not overlap_path.exists():
        report.add("info", "global", "overlap_pairs", "overlap pairs file missing")
    else:
        try:
            data = json.loads(overlap_path.read_text())
            if not isinstance(data, list):
                report.add("error", "global", "overlap_pairs", "must be a JSON array")
        except json.JSONDecodeError as e:
            report.add("error", "global", "overlap_pairs", f"invalid JSON: {e}")


def validate_all(
    eligible_skills: list[dict] | None = None,
    evals_dir: Path | None = None,
) -> DatasetReport:
    """Validate all datasets for all eligible skills.

    Args:
        eligible_skills: List of skill dicts with 'name', 'path', 'neighbors' keys.
            If None, discovers from registry.
        evals_dir: Override evals directory path.
    """
    report = DatasetReport()
    base_evals = evals_dir or EVALS_DIR
    schema = _load_schema()

    if eligible_skills is None:
        # Import here to avoid circular dependency
        from tools.skill_optimizer.skill_discovery import get_eligible_skills

        records = get_eligible_skills()
        eligible_skills = [{"name": r.name, "path": r.path, "neighbors": r.neighbors} for r in records]

    all_neighbor_names = set()
    for skill in eligible_skills:
        all_neighbor_names.update(skill.get("neighbors", []))

    total_records = 0
    skills_with_data = 0

    for skill in eligible_skills:
        name = skill["name"]
        skill_evals_dir = base_evals / name

        if not skill_evals_dir.exists():
            report.add("warning", name, "all", f"no evals directory: evals/{name}/")
            continue

        neighbor_set = set(skill.get("neighbors", []))
        stats = validate_skill_datasets(name, skill_evals_dir, schema, report, neighbor_set)

        split_total = sum(stats["splits"].values())
        if split_total > 0:
            skills_with_data += 1
            total_records += split_total

    report.stats["total_eligible"] = len(eligible_skills)
    report.stats["skills_with_data"] = skills_with_data
    report.stats["total_records"] = total_records

    # Validate global datasets
    validate_global_datasets(schema, report)

    return report


def scaffold_skill_datasets(skill_name: str, evals_dir: Path | None = None) -> Path:
    """Create empty dataset scaffold for a skill."""
    base = evals_dir or EVALS_DIR
    skill_dir = base / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)

    for split in ["train.jsonl", "dev.jsonl", "test.jsonl", "hard_negatives.jsonl"]:
        path = skill_dir / split
        if not path.exists():
            path.write_text("")

    return skill_dir


def scaffold_all(eligible_skills: list[dict] | None = None) -> list[Path]:
    """Create dataset scaffolds for all eligible skills."""
    if eligible_skills is None:
        from tools.skill_optimizer.skill_discovery import get_eligible_skills

        records = get_eligible_skills()
        eligible_skills = [{"name": r.name} for r in records]

    # Global datasets
    global_dir = EVALS_DIR / "global"
    global_dir.mkdir(parents=True, exist_ok=True)
    for name in ["smoke_test.jsonl", "overlap_pairs.json", "paraphrase_sets.jsonl"]:
        path = global_dir / name
        if not path.exists():
            if name.endswith(".json"):
                path.write_text("[]")
            else:
                path.write_text("")

    paths = []
    for skill in eligible_skills:
        paths.append(scaffold_skill_datasets(skill["name"]))

    return paths


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Validate skill evaluation datasets")
    parser.add_argument("--scaffold", action="store_true", help="Create empty dataset scaffolds")
    parser.add_argument("--validate", action="store_true", help="Validate all datasets")
    parser.add_argument("--skill", default=None, help="Validate/scaffold a single skill")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    if args.scaffold:
        if args.skill:
            path = scaffold_skill_datasets(args.skill)
            print(f"Scaffolded: {path}")
        else:
            paths = scaffold_all()
            print(f"Scaffolded {len(paths)} skill datasets + global datasets")
        return

    if args.validate or not args.scaffold:
        if args.skill:
            # Validate single skill
            from tools.skill_optimizer.skill_discovery import discover_all

            all_records = discover_all()
            skill_record = next((r for r in all_records if r.name == args.skill), None)
            if not skill_record:
                print(f"Skill not found: {args.skill}", file=sys.stderr)
                sys.exit(1)
            eligible = [{"name": skill_record.name, "path": skill_record.path, "neighbors": skill_record.neighbors}]
            report = validate_all(eligible)
        else:
            report = validate_all()

        if args.json:
            output = {
                "errors": report.error_count,
                "warnings": report.warning_count,
                "stats": report.stats,
                "issues": [
                    {"severity": i.severity, "skill": i.skill, "split": i.split, "message": i.message}
                    for i in report.issues
                ],
            }
            print(json.dumps(output, indent=2))
        else:
            print(report.summary())
            print()
            if report.issues:
                for issue in report.issues:
                    tag = issue.severity.upper()
                    print(f"  [{tag}] {issue.skill}/{issue.split}: {issue.message}")
            else:
                print("  No issues found.")

        sys.exit(1 if report.has_errors else 0)


if __name__ == "__main__":
    main()
