"""Canonical skill discovery for the autonomous skill improvement pipeline.

Discovers all skills across both skill systems (.claude/skills/ prompt skills
and SKILLS/ execution skills), classifies them, and produces a machine-readable
registry. This module is the single source of truth for "what skills exist"
used by all downstream pipeline phases.

Design decisions:
- Reuses tools/skills.py's frontmatter parser for prompt skills
- Deterministic output (sorted, no randomness)
- Classification is explicit, not inferred at runtime
- Frozen skills are clearly marked with reasons
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent.parent
PROMPT_SKILLS_DIR = BASE_DIR / ".claude" / "skills"
EXEC_SKILLS_DIR = BASE_DIR / "SKILLS"
REGISTRY_PATH = BASE_DIR / "configs" / "skill_registry.yaml"
METADATA_SCHEMA_PATH = BASE_DIR / "schemas" / "skill_metadata.schema.json"

# Skills that must never be auto-optimized.
# Reasons are documented per-skill for audit trail.
FROZEN_SKILLS: dict[str, str] = {
    "skill-creator": "meta-skill — optimizing itself is recursive and unsafe",
    "task-execution": "core execution pipeline — mutation risks task processing integrity",
    "self-verification": "safety-critical — validates all other skill outputs",
    "auditing-obsidian-memory-safety": "governance-critical — enforces memory write safety",
    "semgrep-security": "security-critical — scans for vulnerabilities",
}

# Risk classification rules.
# Skills not listed here default to "medium".
RISK_OVERRIDES: dict[str, str] = {
    # Critical — frozen, never auto-optimize
    "skill-creator": "critical",
    "task-execution": "critical",
    "self-verification": "critical",
    "auditing-obsidian-memory-safety": "critical",
    "semgrep-security": "critical",
    # High — extra-strict thresholds if ever enabled
    "git-ops": "high",
    "shell-ops": "high",
    "file-ops": "high",
    "memory-store": "high",
    "memory-checkpoint": "high",
    # Low — safe to optimize aggressively
    "context7-docs": "low",
    "daily-briefing": "low",
    "weekly-digest": "low",
    "meeting-prep": "low",
    "plan-tracker": "low",
    "sequential-thinking": "low",
    "langfuse-observability": "low",
}

# Domain tag assignments for neighbor detection.
DOMAIN_TAGS: dict[str, list[str]] = {
    "memory-checkpoint": ["memory"],
    "memory-checkpoint-to-diary": ["memory", "obsidian"],
    "memory-health": ["memory"],
    "memory-promote-pattern": ["memory", "obsidian"],
    "memory-recall": ["memory"],
    "memory-store": ["memory"],
    "memory-surface-adr-candidates": ["memory", "obsidian"],
    "memory-unified-recall": ["memory"],
    "google-calendar": ["google-workspace", "scheduling"],
    "google-docs": ["google-workspace", "documents"],
    "google-drive": ["google-workspace", "files"],
    "google-gmail": ["google-workspace", "email"],
    "gmail-triage": ["google-workspace", "email"],
    "email-to-task": ["google-workspace", "email", "tasks"],
    "daily-briefing": ["google-workspace", "overview"],
    "meeting-prep": ["google-workspace", "scheduling"],
    "weekly-digest": ["google-workspace", "overview"],
    "web-research": ["research", "web"],
    "firecrawl-deep-research": ["research", "web"],
    "firecrawl-extract": ["research", "web", "extraction"],
    "firecrawl-site-crawl": ["research", "web"],
    "http-fetch": ["research", "web"],
    "browser-automation": ["research", "web", "automation"],
    "research-to-action": ["research", "web"],
    "context7-docs": ["research", "documentation"],
    "reading-obsidian-memory": ["obsidian", "knowledge"],
    "writing-agent-patterns": ["obsidian", "knowledge"],
    "capturing-workflow-learnings": ["obsidian", "knowledge"],
    "retrieving-task-patterns": ["obsidian", "knowledge"],
    "auditing-obsidian-memory-safety": ["obsidian", "governance"],
    "git-ops": ["devops", "git"],
    "github-ops": ["devops", "git", "github"],
    "shell-ops": ["devops", "system"],
    "file-ops": ["devops", "files"],
    "semgrep-security": ["devops", "security"],
    "langfuse-observability": ["devops", "observability"],
    "task-execution": ["tasks", "execution"],
    "plan-tracker": ["tasks", "planning"],
    "sequential-thinking": ["reasoning"],
    "self-verification": ["governance", "verification"],
    "skill-creator": ["meta"],
    "n8n-workflows": ["automation"],
}

# Manually curated neighbor pairs (symmetric).
# These are skills that are likely to compete for the same queries.
MANUAL_NEIGHBORS: dict[str, list[str]] = {
    "web-research": ["firecrawl-deep-research", "http-fetch", "research-to-action", "context7-docs"],
    "firecrawl-deep-research": ["web-research", "firecrawl-site-crawl", "research-to-action"],
    "firecrawl-extract": ["firecrawl-site-crawl", "http-fetch"],
    "firecrawl-site-crawl": ["firecrawl-deep-research", "firecrawl-extract"],
    "http-fetch": ["web-research", "firecrawl-extract", "browser-automation"],
    "browser-automation": ["http-fetch", "firecrawl-site-crawl"],
    "research-to-action": ["web-research", "firecrawl-deep-research"],
    "memory-recall": ["memory-unified-recall", "reading-obsidian-memory"],
    "memory-unified-recall": ["memory-recall", "reading-obsidian-memory"],
    "memory-store": ["memory-checkpoint", "capturing-workflow-learnings"],
    "memory-checkpoint": ["memory-store", "memory-checkpoint-to-diary"],
    "reading-obsidian-memory": ["memory-recall", "memory-unified-recall", "retrieving-task-patterns"],
    "retrieving-task-patterns": ["reading-obsidian-memory", "memory-recall"],
    "writing-agent-patterns": ["capturing-workflow-learnings", "memory-promote-pattern"],
    "capturing-workflow-learnings": ["writing-agent-patterns", "memory-store"],
    "memory-promote-pattern": ["writing-agent-patterns", "memory-surface-adr-candidates"],
    "memory-surface-adr-candidates": ["memory-promote-pattern"],
    "google-gmail": ["gmail-triage", "email-to-task"],
    "gmail-triage": ["google-gmail", "daily-briefing"],
    "email-to-task": ["google-gmail", "gmail-triage"],
    "daily-briefing": ["weekly-digest", "gmail-triage", "meeting-prep"],
    "weekly-digest": ["daily-briefing"],
    "meeting-prep": ["google-calendar", "daily-briefing"],
    "google-calendar": ["meeting-prep"],
    "google-docs": ["google-drive"],
    "google-drive": ["google-docs"],
    "git-ops": ["github-ops", "shell-ops"],
    "github-ops": ["git-ops"],
    "shell-ops": ["git-ops", "file-ops"],
    "file-ops": ["shell-ops"],
}


@dataclass
class SkillRecord:
    """Canonical record for a discovered skill."""

    name: str
    skill_type: str  # "prompt" or "execution"
    description: str
    path: str  # absolute path to skill directory
    risk_level: str  # "low", "medium", "high", "critical"
    optimization_enabled: bool
    frozen_reason: str | None = None
    domains: list[str] = field(default_factory=list)
    neighbors: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    version: str = "0.1.0"


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Extract YAML frontmatter from SKILL.md content.

    Reuses the parsing logic from tools/skills.py but returns a flat dict.
    """
    if not text.startswith("---"):
        return {}

    end = text.find("\n---", 3)
    if end == -1:
        return {}

    fm_block = text[3:end].strip()
    meta: dict[str, Any] = {}
    current_key = ""
    current_list: list[str] | None = None

    for line in fm_block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(line.lstrip())

        if stripped.startswith("- ") and current_list is not None:
            current_list.append(stripped[2:].strip().strip('"').strip("'"))
            continue

        if stripped.endswith(":") and ":" not in stripped[:-1] and indent == 0:
            current_key = stripped[:-1].strip()
            current_list = None
            continue

        if ":" in stripped:
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")

            full_key = f"{current_key}.{key}" if current_key and indent > 0 else key

            if not val:
                current_list = []
                meta[full_key] = current_list
                continue

            if val.startswith("[") and val.endswith("]"):
                items = [v.strip().strip('"').strip("'") for v in val[1:-1].split(",") if v.strip()]
                meta[full_key] = items
            else:
                meta[full_key] = val
            if indent == 0:
                current_key = ""
            current_list = None

    return meta


def discover_prompt_skills() -> list[SkillRecord]:
    """Discover all prompt skills from .claude/skills/."""
    records: list[SkillRecord] = []

    if not PROMPT_SKILLS_DIR.is_dir():
        return records

    for skill_dir in sorted(PROMPT_SKILLS_DIR.iterdir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            continue

        raw = skill_file.read_text(encoding="utf-8")
        meta = _parse_frontmatter(raw)

        name = meta.get("name", skill_dir.name)
        description = meta.get("description", "")

        # Extract activation keywords
        keywords_raw = meta.get("activation.keywords", [])
        if isinstance(keywords_raw, str):
            keywords_raw = [k.strip() for k in keywords_raw.split(",") if k.strip()]

        risk = RISK_OVERRIDES.get(name, "medium")
        frozen = name in FROZEN_SKILLS
        frozen_reason = FROZEN_SKILLS.get(name)
        domains = DOMAIN_TAGS.get(name, ["general"])
        neighbors = MANUAL_NEIGHBORS.get(name, [])

        records.append(
            SkillRecord(
                name=name,
                skill_type="prompt",
                description=description,
                path=str(skill_dir),
                risk_level=risk,
                optimization_enabled=not frozen,
                frozen_reason=frozen_reason,
                domains=domains,
                neighbors=neighbors,
                keywords=keywords_raw if isinstance(keywords_raw, list) else [],
            )
        )

    return records


def discover_execution_skills() -> list[SkillRecord]:
    """Discover all execution skills from SKILLS/."""
    records: list[SkillRecord] = []

    if not EXEC_SKILLS_DIR.is_dir():
        return records

    for skill_dir in sorted(EXEC_SKILLS_DIR.iterdir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            continue

        raw = skill_file.read_text(encoding="utf-8")
        meta = _parse_frontmatter(raw)

        name = meta.get("name", skill_dir.name)
        description = meta.get("description", "")
        version = meta.get("version", "0.1.0")

        # All execution skills are frozen in rollout 1
        records.append(
            SkillRecord(
                name=name,
                skill_type="execution",
                description=description,
                path=str(skill_dir),
                risk_level="high",
                optimization_enabled=False,
                frozen_reason="execution skills frozen in rollout 1",
                domains=DOMAIN_TAGS.get(name, ["execution"]),
                neighbors=MANUAL_NEIGHBORS.get(name, []),
                version=version,
            )
        )

    return records


def discover_all() -> list[SkillRecord]:
    """Discover all skills across both systems. Deterministic ordering."""
    prompt = discover_prompt_skills()
    execution = discover_execution_skills()
    return sorted(prompt + execution, key=lambda r: r.name)


def get_eligible_skills(records: list[SkillRecord] | None = None) -> list[SkillRecord]:
    """Return only skills eligible for optimization."""
    if records is None:
        records = discover_all()
    return [r for r in records if r.optimization_enabled]


def get_frozen_skills(records: list[SkillRecord] | None = None) -> list[SkillRecord]:
    """Return only frozen skills."""
    if records is None:
        records = discover_all()
    return [r for r in records if not r.optimization_enabled]


def compute_neighbors_by_domain(records: list[SkillRecord]) -> dict[str, list[str]]:
    """Compute neighbor relationships from shared domain tags.

    Two skills are domain-neighbors if they share at least one domain tag.
    Returns merged neighbors (manual + domain heuristic), deduped.
    """
    domain_index: dict[str, list[str]] = {}
    for r in records:
        for domain in r.domains:
            domain_index.setdefault(domain, []).append(r.name)

    neighbors: dict[str, list[str]] = {}
    for r in records:
        domain_peers: set[str] = set()
        for domain in r.domains:
            for peer in domain_index.get(domain, []):
                if peer != r.name:
                    domain_peers.add(peer)
        # Merge manual + domain
        manual = set(MANUAL_NEIGHBORS.get(r.name, []))
        merged = sorted(manual | domain_peers)
        neighbors[r.name] = merged

    return neighbors


def generate_registry(records: list[SkillRecord] | None = None) -> dict:
    """Generate the full registry as a dict (serializable to YAML)."""
    if records is None:
        records = discover_all()

    neighbor_map = compute_neighbors_by_domain(records)

    skills_list = []
    for r in records:
        entry: dict[str, Any] = {
            "name": r.name,
            "skill_type": r.skill_type,
            "description": r.description[:200],  # truncate for registry readability
            "path": r.path,
            "risk_level": r.risk_level,
            "optimization_enabled": r.optimization_enabled,
            "domains": r.domains,
            "neighbors": neighbor_map.get(r.name, r.neighbors),
        }
        if r.frozen_reason:
            entry["frozen_reason"] = r.frozen_reason
        skills_list.append(entry)

    # Summary stats
    total = len(records)
    prompt_count = sum(1 for r in records if r.skill_type == "prompt")
    exec_count = sum(1 for r in records if r.skill_type == "execution")
    eligible_count = sum(1 for r in records if r.optimization_enabled)
    frozen_count = total - eligible_count

    return {
        "version": "1.0.0",
        "generated_by": "tools/skill_optimizer/skill_discovery.py",
        "summary": {
            "total_skills": total,
            "prompt_skills": prompt_count,
            "execution_skills": exec_count,
            "eligible_for_optimization": eligible_count,
            "frozen": frozen_count,
        },
        "skills": skills_list,
    }


def write_registry(output_path: Path | None = None) -> Path:
    """Discover all skills and write the registry YAML."""
    path = output_path or REGISTRY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    registry = generate_registry()
    path.write_text(
        yaml.dump(registry, default_flow_style=False, sort_keys=False, width=120),
        encoding="utf-8",
    )
    return path


def generate_metadata(record: SkillRecord, neighbor_map: dict[str, list[str]]) -> dict:
    """Generate a metadata.json dict for a single skill."""
    return {
        "name": record.name,
        "version": record.version,
        "description": record.description[:200],
        "skill_type": record.skill_type,
        "domains": record.domains,
        "risk_level": record.risk_level,
        "optimization_enabled": record.optimization_enabled,
        "frozen_reason": record.frozen_reason,
        "neighbors": neighbor_map.get(record.name, record.neighbors),
        "eval_profile": "strict" if record.risk_level in ("high", "critical") else "standard",
        "owner": "nova",
        "created": "2026-03-15",
        "last_optimized": None,
    }


def _is_tool_generated(data: dict) -> bool:
    """Check if a metadata.json was generated by tooling (not hand-authored).

    Heuristic: if it has our standard fields and no extra hand-written fields,
    treat it as tool-generated and safe to update.
    """
    if data.get("generated_by") == "skill_discovery":
        return True
    # Files from prior auto-generation lack generated_by but match our schema
    standard_keys = {
        "name",
        "version",
        "description",
        "skill_type",
        "domains",
        "risk_level",
        "optimization_enabled",
        "neighbors",
        "eval_profile",
        "owner",
        "created",
        "last_optimized",
        "frozen_reason",
        "generated_by",
    }
    return set(data.keys()).issubset(standard_keys)


def write_all_metadata(records: list[SkillRecord] | None = None, dry_run: bool = False) -> list[Path]:
    """Write metadata.json for every discovered skill.

    Updates tool-generated metadata with corrected classifications.
    Never overwrites metadata with non-standard keys (assumed human-authored).
    """
    if records is None:
        records = discover_all()

    neighbor_map = compute_neighbors_by_domain(records)
    written: list[Path] = []

    for record in records:
        skill_dir = Path(record.path)
        metadata_path = skill_dir / "metadata.json"

        # Safety: don't overwrite human-authored metadata
        if metadata_path.exists():
            try:
                existing = json.loads(metadata_path.read_text())
                if not _is_tool_generated(existing):
                    continue  # Skip — human-authored
            except (json.JSONDecodeError, OSError):
                pass  # Overwrite broken files

        metadata = generate_metadata(record, neighbor_map)
        metadata["generated_by"] = "skill_discovery"

        if not dry_run:
            metadata_path.write_text(
                json.dumps(metadata, indent=2) + "\n",
                encoding="utf-8",
            )
        written.append(metadata_path)

    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    """CLI entry point for skill discovery."""
    import argparse

    parser = argparse.ArgumentParser(description="Discover and classify NovaCore skills")
    parser.add_argument("--registry", action="store_true", help="Write configs/skill_registry.yaml")
    parser.add_argument("--metadata", action="store_true", help="Write metadata.json per skill")
    parser.add_argument("--dry-run", action="store_true", help="Don't write files, just report")
    parser.add_argument("--json", action="store_true", help="Output registry as JSON to stdout")
    parser.add_argument("--eligible-only", action="store_true", help="Show only optimization-eligible skills")
    args = parser.parse_args()

    records = discover_all()

    if args.eligible_only:
        records = get_eligible_skills(records)

    if args.json:
        print(json.dumps(generate_registry(records), indent=2))
        return

    # Summary
    prompt_count = sum(1 for r in records if r.skill_type == "prompt")
    exec_count = sum(1 for r in records if r.skill_type == "execution")
    eligible = [r for r in records if r.optimization_enabled]
    frozen = [r for r in records if not r.optimization_enabled]

    print(f"Discovered {len(records)} skills ({prompt_count} prompt, {exec_count} execution)")
    print(f"  Eligible for optimization: {len(eligible)}")
    print(f"  Frozen: {len(frozen)}")

    if frozen:
        print("\nFrozen skills:")
        for r in frozen:
            print(f"  {r.name}: {r.frozen_reason}")

    print("\nRisk distribution:")
    for level in ["low", "medium", "high", "critical"]:
        count = sum(1 for r in records if r.risk_level == level)
        names = [r.name for r in records if r.risk_level == level]
        if names:
            print(f"  {level}: {count} — {', '.join(names[:5])}{'...' if len(names) > 5 else ''}")

    if args.registry:
        if args.dry_run:
            print("\n[DRY RUN] Would write registry to", REGISTRY_PATH)
        else:
            path = write_registry()
            print(f"\nRegistry written to: {path}")

    if args.metadata:
        written = write_all_metadata(records, dry_run=args.dry_run)
        if args.dry_run:
            print(f"\n[DRY RUN] Would write {len(written)} metadata.json files")
        else:
            print(f"\nWrote {len(written)} metadata.json files")

    return 0


if __name__ == "__main__":
    main()
