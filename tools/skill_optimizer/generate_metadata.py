#!/usr/bin/env python3
"""Generate metadata.json for all NovaCore skills.

Phase 0 of the autonomous skill improvement pipeline.
Scans .claude/skills/*/SKILL.md (prompt skills) and SKILLS/*/SKILL.md (execution skills),
extracts frontmatter, infers domains, applies curated neighbor mappings, and writes
a validated metadata.json into each skill directory.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]  # nova-core/
PROMPT_SKILLS_DIR = REPO_ROOT / ".claude" / "skills"
EXEC_SKILLS_DIR = REPO_ROOT / "SKILLS"
SCHEMA_PATH = REPO_ROOT / "schemas" / "skill_metadata.schema.json"

# ---------------------------------------------------------------------------
# Curated neighbor mapping (from the plan)
# ---------------------------------------------------------------------------
NEIGHBOR_MAP: dict[str, list[str]] = {
    "web-research": ["firecrawl-deep-research", "http-fetch", "research-to-action"],
    "firecrawl-deep-research": ["web-research", "firecrawl-site-crawl", "firecrawl-extract"],
    "firecrawl-site-crawl": ["firecrawl-deep-research", "firecrawl-extract"],
    "firecrawl-extract": ["firecrawl-site-crawl", "firecrawl-deep-research"],
    "http-fetch": ["web-research", "browser-automation"],
    "browser-automation": ["http-fetch", "firecrawl-deep-research"],
    "research-to-action": ["web-research", "http-fetch", "browser-automation"],
    "gmail-triage": ["google-gmail", "email-to-task", "daily-briefing"],
    "google-gmail": ["gmail-triage", "email-to-task"],
    "email-to-task": ["gmail-triage", "google-gmail"],
    "memory-recall": ["memory-unified-recall", "reading-obsidian-memory", "memory-store"],
    "memory-unified-recall": ["memory-recall", "reading-obsidian-memory"],
    "reading-obsidian-memory": ["memory-recall", "memory-unified-recall"],
    "memory-store": ["memory-recall", "capturing-workflow-learnings"],
    "memory-checkpoint": ["memory-checkpoint-to-diary", "memory-store"],
    "memory-checkpoint-to-diary": ["memory-checkpoint", "memory-store"],
    "memory-health": ["self-verification", "memory-recall"],
    "memory-promote-pattern": ["writing-agent-patterns", "memory-surface-adr-candidates"],
    "memory-surface-adr-candidates": ["memory-promote-pattern", "plan-tracker"],
    "writing-agent-patterns": ["capturing-workflow-learnings", "memory-promote-pattern"],
    "capturing-workflow-learnings": ["writing-agent-patterns", "memory-store"],
    "retrieving-task-patterns": ["reading-obsidian-memory", "task-execution"],
    "daily-briefing": ["weekly-digest", "gmail-triage", "google-calendar"],
    "weekly-digest": ["daily-briefing", "plan-tracker"],
    "google-calendar": ["meeting-prep", "daily-briefing"],
    "meeting-prep": ["google-calendar", "daily-briefing"],
    "google-docs": ["google-drive"],
    "google-drive": ["google-docs"],
    "git-ops": ["github-ops", "file-ops"],
    "github-ops": ["git-ops"],
    "file-ops": ["git-ops", "shell-ops"],
    "shell-ops": ["file-ops"],
    "context7-docs": ["claude-api"],
    "self-verification": ["auditing-obsidian-memory-safety"],
    "plan-tracker": ["task-execution"],
    "task-execution": ["plan-tracker", "retrieving-task-patterns"],
}

# ---------------------------------------------------------------------------
# Domain inference keywords
# ---------------------------------------------------------------------------
DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "web": ["web", "search", "scrape", "crawl", "fetch", "http", "url", "browser", "firecrawl", "tavily", "brave"],
    "memory": ["memory", "recall", "checkpoint", "fusion", "pinecone", "neo4j", "redis", "obsidian", "vault"],
    "email": ["email", "gmail", "inbox", "triage", "mail"],
    "calendar": ["calendar", "meeting", "schedule", "event"],
    "docs": ["docs", "document", "sheets", "spreadsheet", "google docs"],
    "storage": ["drive", "file", "upload", "download", "storage"],
    "git": ["git", "commit", "branch", "merge", "pull request", "pr", "github"],
    "shell": ["shell", "bash", "command", "terminal", "execute", "process"],
    "security": ["security", "vulnerability", "scan", "audit", "semgrep", "owasp", "cve"],
    "observability": ["observability", "trace", "langfuse", "cost", "token", "usage", "monitoring"],
    "automation": ["automation", "workflow", "n8n", "webhook", "cron", "schedule"],
    "planning": ["plan", "task", "tracker", "briefing", "digest", "summary"],
    "knowledge": ["pattern", "learning", "adr", "research", "knowledge"],
    "code": ["code", "improve", "refactor", "lint", "test", "repo"],
    "ai": ["claude", "llm", "api", "sdk", "prompt", "model"],
    "skills": ["skill", "eval", "benchmark", "optimize", "trigger"],
}

# High-risk skills that need strict eval profiles
HIGH_RISK_SKILLS = frozenset(
    {
        "task-execution",
        "self-verification",
        "auditing-obsidian-memory-safety",
        "semgrep-security",
    }
)

# Skills excluded from optimization
OPTIMIZATION_EXCLUDED = frozenset(
    {
        "skill-creator",  # meta-skill, do not self-optimize
    }
)


def parse_prompt_skill(skill_dir: Path) -> dict | None:
    """Parse a prompt skill's SKILL.md with YAML frontmatter."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None

    text = skill_md.read_text(encoding="utf-8")

    # Extract YAML frontmatter
    fm_match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not fm_match:
        return None

    try:
        frontmatter = yaml.safe_load(fm_match.group(1))
    except yaml.YAMLError:
        return None

    if not isinstance(frontmatter, dict):
        return None

    return {
        "frontmatter": frontmatter,
        "body": text[fm_match.end() :],
        "full_text": text,
    }


def parse_execution_skill(skill_dir: Path) -> dict | None:
    """Parse an execution skill's SKILL.md (Markdown with embedded YAML)."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None

    text = skill_md.read_text(encoding="utf-8")

    # Try to extract embedded YAML from ```yaml blocks
    yaml_match = re.search(r"```yaml\s*\n(.*?)```", text, re.DOTALL)
    frontmatter: dict[str, Any] = {}
    if yaml_match:
        try:
            frontmatter = yaml.safe_load(yaml_match.group(1)) or {}
        except yaml.YAMLError:
            pass

    return {
        "frontmatter": frontmatter,
        "body": text,
        "full_text": text,
    }


def infer_domains(name: str, description: str) -> list[str]:
    """Infer domain tags from skill name and description (not full body)."""
    # Only use name + description to avoid false positives from body text
    text_lower = f"{name} {description}".lower()
    domains: list[str] = []

    for domain, keywords in DOMAIN_KEYWORDS.items():
        for kw in keywords:
            # Use word-boundary matching to avoid substring false positives
            # e.g. "pr" should not match "prior" or "reproducible"
            if re.search(rf"\b{re.escape(kw)}\b", text_lower):
                domains.append(domain)
                break

    return domains if domains else ["general"]


def infer_risk_level(name: str) -> str:
    """Determine risk level based on skill name."""
    if name in HIGH_RISK_SKILLS:
        return "high"
    if name in {"git-ops", "github-ops", "shell-ops"}:
        return "medium"
    return "low"


def build_metadata(
    name: str,
    parsed: dict,
    skill_type: str,
) -> dict:
    """Build a metadata.json dict from parsed skill data."""
    fm = parsed["frontmatter"]

    # Extract description — truncate to 200 chars
    description = fm.get("description", "")
    if isinstance(description, str):
        description = description.strip()
    else:
        description = str(description).strip()
    if len(description) > 200:
        description = description[:197] + "..."

    # Version — from frontmatter or default
    version = fm.get("version", "1.0.0")
    if not isinstance(version, str):
        version = str(version)
    if not re.match(r"^\d+\.\d+\.\d+$", version):
        version = "1.0.0"

    domains = infer_domains(name, description)
    neighbors = NEIGHBOR_MAP.get(name, [])
    risk_level = infer_risk_level(name)

    # Optimization eligibility
    optimization_enabled = skill_type == "prompt" and name not in OPTIMIZATION_EXCLUDED

    # Eval profile
    if name in HIGH_RISK_SKILLS:
        eval_profile = "strict"
    elif risk_level == "medium":
        eval_profile = "standard"
    else:
        eval_profile = "standard"

    return {
        "name": name,
        "version": version,
        "description": description,
        "skill_type": skill_type,
        "domains": domains,
        "risk_level": risk_level,
        "optimization_enabled": optimization_enabled,
        "neighbors": neighbors,
        "eval_profile": eval_profile,
        "owner": "nova",
        "created": "2026-03-15",
    }


def validate_metadata(meta: dict, schema: dict) -> list[str]:
    """Lightweight schema validation (no jsonschema dependency required)."""
    errors: list[str] = []

    # Check required fields
    for field in schema.get("required", []):
        if field not in meta:
            errors.append(f"missing required field: {field}")

    props = schema.get("properties", {})

    # Validate name pattern
    if "name" in meta and "pattern" in props.get("name", {}) and not re.match(props["name"]["pattern"], meta["name"]):
        errors.append(f"name '{meta['name']}' doesn't match pattern")

    # Validate version pattern
    if (
        "version" in meta
        and "pattern" in props.get("version", {})
        and not re.match(props["version"]["pattern"], meta["version"])
    ):
        errors.append(f"version '{meta['version']}' doesn't match pattern")

    # Validate description length
    if "description" in meta:
        max_len = props.get("description", {}).get("maxLength", 200)
        if len(meta["description"]) > max_len:
            errors.append(f"description exceeds {max_len} chars")

    # Validate enums
    for field in ["skill_type", "risk_level", "eval_profile"]:
        if field in meta and "enum" in props.get(field, {}) and meta[field] not in props[field]["enum"]:
            errors.append(f"{field} '{meta[field]}' not in {props[field]['enum']}")

    # Validate domains is non-empty array
    if "domains" in meta and (not isinstance(meta["domains"], list) or len(meta["domains"]) < 1):
        errors.append("domains must be a non-empty array")

    # Validate optimization_enabled is bool
    if "optimization_enabled" in meta and not isinstance(meta["optimization_enabled"], bool):
        errors.append("optimization_enabled must be boolean")

    return errors


def main() -> int:
    """Scan all skills, generate metadata.json, validate, and report."""
    # Load schema
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    stats: dict[str, Any] = {
        "prompt_skills": 0,
        "execution_skills": 0,
        "generated": 0,
        "validated": 0,
        "failed": [],
        "neighbor_coverage": 0,
    }

    all_metadata: list[dict] = []

    # --- Process prompt skills ---
    if PROMPT_SKILLS_DIR.exists():
        for skill_dir in sorted(PROMPT_SKILLS_DIR.iterdir()):
            if not skill_dir.is_dir():
                continue
            name = skill_dir.name
            parsed = parse_prompt_skill(skill_dir)
            if parsed is None:
                stats["failed"].append(f"{name} (parse error)")
                continue

            stats["prompt_skills"] += 1
            meta = build_metadata(name, parsed, "prompt")

            # Validate
            errors = validate_metadata(meta, schema)
            if errors:
                stats["failed"].append(f"{name}: {'; '.join(errors)}")
                continue

            # Write metadata.json
            out_path = skill_dir / "metadata.json"
            out_path.write_text(
                json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            stats["generated"] += 1
            stats["validated"] += 1
            all_metadata.append(meta)

            if meta.get("neighbors"):
                stats["neighbor_coverage"] += 1

    # --- Process execution skills ---
    if EXEC_SKILLS_DIR.exists():
        for skill_dir in sorted(EXEC_SKILLS_DIR.iterdir()):
            if not skill_dir.is_dir():
                continue
            name = skill_dir.name
            parsed = parse_execution_skill(skill_dir)
            if parsed is None:
                stats["failed"].append(f"{name} (parse error)")
                continue

            stats["execution_skills"] += 1
            meta = build_metadata(name, parsed, "execution")

            # Validate
            errors = validate_metadata(meta, schema)
            if errors:
                stats["failed"].append(f"{name}: {'; '.join(errors)}")
                continue

            # Write metadata.json
            out_path = skill_dir / "metadata.json"
            out_path.write_text(
                json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            stats["generated"] += 1
            stats["validated"] += 1
            all_metadata.append(meta)

            if meta.get("neighbors"):
                stats["neighbor_coverage"] += 1

    # --- Report ---
    total = stats["prompt_skills"] + stats["execution_skills"]
    print(f"\n{'=' * 60}")
    print("Skill Metadata Generator — Phase 0 Results")
    print(f"{'=' * 60}")
    print(f"Prompt skills found:    {stats['prompt_skills']}")
    print(f"Execution skills found: {stats['execution_skills']}")
    print(f"Total skills:           {total}")
    print(f"Metadata generated:     {stats['generated']}")
    print(f"Validation passed:      {stats['validated']}")
    print(
        f"Neighbor coverage:      {stats['neighbor_coverage']}/{total} "
        f"({100 * stats['neighbor_coverage'] // max(total, 1)}%)"
    )

    if stats["failed"]:
        print(f"\nFailed ({len(stats['failed'])}):")
        for f in stats["failed"]:
            print(f"  - {f}")

    print(f"{'=' * 60}")

    # Write summary as JSON for programmatic consumption
    summary_path = REPO_ROOT / "WORK" / "skill_metadata_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(stats, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"\nSummary written to: {summary_path}")

    return 0 if not stats["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
