#!/usr/bin/env python3
"""Organize the Obsidian vault inbox — triage 00-inbox/ into proper folders.

Categorization rules:
  1. Heartbeat plan snapshots (plan-nova-core-YYYYMMDD-*) → _archive/heartbeat-plans/
     (keep latest 3 in 10-plans/)
  2. research-* → 40-research/
  3. Named plans (plan-*) → 10-plans/
  4. Daily summaries (YYYY-MM-DD-daily-summary, daily-summary-*) → 90-diary/
  5. Diary entries (diary-*) → 90-diary/
  6. ADR candidates (adr-candidate-*) → 10-adrs/
  7. Remaining notes → routed by keyword heuristics
"""

import re
import shutil
from pathlib import Path

VAULT = Path("/home/nova/nova-vault")
INBOX = VAULT / "00-inbox"
ARCHIVE = VAULT / "_archive" / "heartbeat-plans"

# Ensure target dirs exist
for d in [
    "10-plans",
    "10-adrs",
    "20-agent-patterns",
    "30-workflow-learnings",
    "40-research",
    "50-playbooks",
    "60-project",
    "70-debugging",
    "80-references",
    "90-diary",
    "_archive/heartbeat-plans",
]:
    (VAULT / d).mkdir(parents=True, exist_ok=True)

moved: dict[str, list[str]] = {
    "10-plans": [],
    "10-adrs": [],
    "40-research": [],
    "90-diary": [],
    "30-workflow-learnings": [],
    "70-debugging": [],
    "80-references": [],
    "_archive/heartbeat-plans": [],
    "skipped": [],
}

# ── Heartbeat plan snapshots ─────────────────────────────────────────
HB_PLAN_RE = re.compile(r"^plan-nova-core-(\d{8})-(\d{6})\.md$")
hb_plans = []
for f in sorted(INBOX.iterdir()):
    m = HB_PLAN_RE.match(f.name)
    if m:
        hb_plans.append(f)

# Sort by date+time (newest last), keep latest 3 in 10-plans, archive rest
hb_plans.sort(key=lambda p: p.name)
keep_latest = hb_plans[-3:] if len(hb_plans) >= 3 else hb_plans
archive_hb = [p for p in hb_plans if p not in keep_latest]

for f in keep_latest:
    dest = VAULT / "10-plans" / f.name
    if not dest.exists():
        shutil.move(str(f), str(dest))
        moved["10-plans"].append(f.name)
    else:
        moved["skipped"].append(f"DUP {f.name}")

for f in archive_hb:
    dest = ARCHIVE / f.name
    shutil.move(str(f), str(dest))
    moved["_archive/heartbeat-plans"].append(f.name)

# ── Categorize remaining inbox files ─────────────────────────────────
# Keyword-to-folder routing for misc notes
MISC_ROUTING = {
    "70-debugging": [
        "drift-detection-false-positive",
        "drift-detection-rate-limit",
    ],
    "80-references": [
        "novatrade-backtesting-readme",
        "novatrade-irb-strategy-reference",
    ],
    "30-workflow-learnings": [
        "budget-watchdog-implementation",
        "code-quality-ruff-scan",
        "ruff-linting-setup",
        "security-hardening-phase1-quickwins",
        "skill-metadata-schema-phase0",
        "test-coverage-analysis",
    ],
    "90-diary": [
        "autonomy-session-briefing",
        "operator-briefing",
    ],
    "10-plans": [
        "ceo-nova-telegram-implementation-plan",
        "nova-core-enhancement-plan",
        "novatrade-autoresearch-backtesting-plan",
        "novatrade-strategy-pipeline-plan",
        "p11-multi-agent-runtime-implementation",
    ],
    "40-research": [
        "agent-debugging-visualization-tools-research",
        "agent-self-improvement-meta-learning",
        "cli-tools-for-agents",
        "mcp-ecosystem-q1-2026-update",
        "mcp-ecosystem-survey",
        "mcp-ecosystem-synthesis",
        "mcp-server-ecosystem-survey",
        "multi-agent-coordination-protocols",
        "nova-core-capability-gaps",
        "openclaw-tooling-research",
        "python-testing-code-quality",
        "security-audit-2026",
        "structured-output-validation-research",
    ],
}


def route_file(name: str) -> str | None:
    """Return target folder for a file based on name patterns."""
    stem = name.removesuffix(".md")

    # Already handled heartbeat plans
    if HB_PLAN_RE.match(name):
        return None

    # Prefix-based routing
    if name.startswith("research-"):
        return "40-research"
    if name.startswith("adr-candidate-"):
        return "10-adrs"
    if name.startswith("diary-"):
        return "90-diary"
    if re.match(r"^\d{4}-\d{2}-\d{2}-daily-summary", name):
        return "90-diary"
    if name.startswith("daily-summary-"):
        return "90-diary"
    if name.startswith("plan-") and not HB_PLAN_RE.match(name):
        return "10-plans"

    # Keyword routing for misc
    for folder, keywords in MISC_ROUTING.items():
        for kw in keywords:
            if kw in stem:
                return folder

    return None


for f in sorted(INBOX.iterdir()):
    if not f.is_file() or f.name.startswith("."):
        continue
    # Skip already-handled heartbeat plans
    if HB_PLAN_RE.match(f.name):
        continue

    target_folder = route_file(f.name)
    if target_folder is None:
        moved["skipped"].append(f.name)
        continue

    dest = VAULT / target_folder / f.name
    if dest.exists():
        # Don't overwrite — skip duplicates
        moved["skipped"].append(f"DUP:{target_folder} {f.name}")
        continue

    shutil.move(str(f), str(dest))
    moved[target_folder].append(f.name)


# ── Report ────────────────────────────────────────────────────────────
total_moved = sum(len(v) for k, v in moved.items() if k != "skipped")
print(f"\n{'=' * 60}")
print("Vault Inbox Triage Complete")
print(f"{'=' * 60}")
for folder, files in sorted(moved.items()):
    if files:
        print(f"\n{folder}/ ({len(files)} files)")
        for fn in files[:5]:
            print(f"  → {fn}")
        if len(files) > 5:
            print(f"  ... and {len(files) - 5} more")

print(f"\n{'─' * 60}")
print(f"Total moved:   {total_moved}")
print(f"Skipped/dupes: {len(moved['skipped'])}")

# Count remaining inbox
remaining = list(INBOX.iterdir())
print(f"Remaining in inbox: {len(remaining)}")
if remaining:
    for r in sorted(remaining)[:10]:
        print(f"  • {r.name}")
    if len(remaining) > 10:
        print(f"  ... and {len(remaining) - 10} more")
