"""Prompt-building functions for the heartbeat module.

Extracted from heartbeat.py (Phase 4.5: Heartbeat Prompt Extraction).
All functions maintain identical signatures and behavior.

These functions use lazy imports of heartbeat module constants
(TASKS_DIR, OUTPUT_DIR, STATE_DIR, etc.) to avoid circular imports,
since heartbeat.py imports from this module.
"""

import json
import subprocess
from datetime import datetime, timezone


def _gather_extended_state(checks: list) -> str:
    """Collect system state summary for the LLM heartbeat agent."""
    import heartbeat

    parts = []

    # Deterministic check results
    parts.append("## Deterministic Health Checks")
    for c in checks:
        mark = "PASS" if c["ok"] else "FAIL"
        parts.append(f"  [{mark}] {c['name']}: {c['detail']}")

    # Pending tasks (with age)
    pending = [
        p
        for p in heartbeat.TASKS_DIR.glob("*.md")
        if not any(p.name.endswith(s) for s in (".inprogress", ".done", ".failed", ".cancelled"))
    ]
    if pending:
        parts.append(f"\n## Pending Tasks ({len(pending)})")
        now = datetime.now(timezone.utc)
        for p in sorted(pending, key=lambda x: x.stat().st_mtime):
            age_min = (now - datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)).total_seconds() / 60
            parts.append(f"  - {p.stem} ({round(age_min)}min old)")

    # Recent failed tasks (last 2 hours)
    failed = list(heartbeat.TASKS_DIR.glob("*.failed"))
    now = datetime.now(timezone.utc)
    recent_failed = []
    for f in failed:
        age_hr = (now - datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)).total_seconds() / 3600
        if age_hr < 2:
            recent_failed.append(f)
    if recent_failed:
        parts.append(f"\n## Recently Failed Tasks ({len(recent_failed)})")
        for f in recent_failed:
            parts.append(f"  - {f.stem}")

    # Recent outputs (last 4 hours)
    outputs = sorted(
        heartbeat.OUTPUT_DIR.glob("*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    recent_outputs = []
    for o in outputs[:10]:
        age_hr = (now - datetime.fromtimestamp(o.stat().st_mtime, tz=timezone.utc)).total_seconds() / 3600
        if age_hr < 4:
            recent_outputs.append((o, age_hr))
    if recent_outputs:
        parts.append(f"\n## Recent Outputs ({len(recent_outputs)} in last 4h)")
        for o, age in recent_outputs:
            parts.append(f"  - {o.stem} ({round(age, 1)}h ago)")

    # Goals
    goals_file = heartbeat.STATE_DIR / "goals.json"
    if goals_file.exists():
        try:
            data = json.loads(goals_file.read_text())
            # Handle both dict-wrapped and bare-array formats
            if isinstance(data, dict):
                goals_list = data.get("goals", [])
            elif isinstance(data, list):
                goals_list = data
            else:
                goals_list = []
            active = [g for g in goals_list if g.get("status") != "done"]
            if active:
                parts.append(f"\n## Active Goals ({len(active)})")
                for g in active:
                    parts.append(f"  - [{g.get('id', '?')}] {g.get('text', '?')}")
        except Exception:
            pass

    # Last heartbeat agent action (to avoid repeating)
    if heartbeat.HEARTBEAT_AGENT_LOG.exists():
        try:
            lines = heartbeat.HEARTBEAT_AGENT_LOG.read_text().strip().splitlines()
            if lines:
                parts.append("\n## Last Agent Action")
                parts.append(f"  {lines[-1][:200]}")
        except Exception:
            pass

    return "\n".join(parts)


def _scan_codebase() -> str:
    """Scan the nova-core codebase and return a structured snapshot."""
    import heartbeat

    parts = []

    # Recent git log (last 15 commits)
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-15", "--no-decorate"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(heartbeat.BASE),
        )
        if result.stdout.strip():
            parts.append("### Recent Commits (last 15)")
            parts.append(result.stdout.strip())
    except Exception:
        pass

    # File tree — top-level + key directories
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(heartbeat.BASE),
        )
        if result.stdout.strip():
            files = result.stdout.strip().splitlines()
            # Group by top-level directory
            dirs: dict[str, list[str]] = {}
            for f in files:
                top = f.split("/")[0] if "/" in f else "(root)"
                dirs.setdefault(top, []).append(f)
            parts.append(f"\n### File Tree ({len(files)} tracked files)")
            for d in sorted(dirs):
                parts.append(f"  {d}/ — {len(dirs[d])} files")
            # List Python files explicitly (these are the codebase)
            py_files = [f for f in files if f.endswith(".py")]
            parts.append(f"\n### Python Modules ({len(py_files)})")
            for f in py_files:
                parts.append(f"  {f}")
    except Exception:
        pass

    # Code stats — lines of Python
    try:
        total_lines = 0
        for py in heartbeat.BASE.rglob("*.py"):
            if ".venv" in str(py) or "__pycache__" in str(py):
                continue
            try:
                total_lines += len(py.read_text().splitlines())
            except Exception:
                pass
        parts.append("\n### Code Stats")
        parts.append(f"  Total Python lines: {total_lines:,}")
    except Exception:
        pass

    # Recent file changes (modified in last 24h)
    try:
        now = datetime.now(timezone.utc)
        recently_modified = []
        for py in heartbeat.BASE.rglob("*.py"):
            if ".venv" in str(py) or "__pycache__" in str(py):
                continue
            age_hr = (now - datetime.fromtimestamp(py.stat().st_mtime, tz=timezone.utc)).total_seconds() / 3600
            if age_hr < 24:
                recently_modified.append((py.relative_to(heartbeat.BASE), round(age_hr, 1)))
        if recently_modified:
            parts.append("\n### Recently Modified (last 24h)")
            for f, age in sorted(recently_modified, key=lambda x: x[1]):  # type: ignore[assignment]
                parts.append(f"  {f} ({age}h ago)")
    except Exception:
        pass

    # Uncommitted changes
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(heartbeat.BASE),
        )
        if result.stdout.strip():
            lines = result.stdout.strip().splitlines()
            parts.append(f"\n### Uncommitted Changes ({len(lines)})")
            for line in lines[:20]:
                parts.append(f"  {line}")
    except Exception:
        pass

    return "\n".join(parts) if parts else "(codebase scan failed)"


def _build_research_prompt() -> str:
    """Build the comprehensive research cycle prompt."""
    import heartbeat

    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    date_str = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y%m%d-%H%M%S")

    # Gather recent OUTPUT file names for topic deduplication
    recent_topics = []
    if heartbeat.OUTPUT_DIR.exists():
        for f in sorted(heartbeat.OUTPUT_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
            recent_topics.append(f.stem)
    recent_str = "\n".join(f"  - {t}" for t in recent_topics) or "  (none)"

    # Gather active goals
    goals_str = "(no active goals)"
    goals_file = heartbeat.STATE_DIR / "goals.json"
    if goals_file.exists():
        try:
            data = json.loads(goals_file.read_text())
            goals_list = data.get("goals", []) if isinstance(data, dict) else data
            active = [g for g in goals_list if g.get("status") != "done"]
            if active:
                goals_str = "\n".join(f"  - [{g.get('id', '?')}] {g.get('text', '?')}" for g in active)
        except Exception:
            pass

    # Scan the codebase
    codebase_snapshot = _scan_codebase()

    return f"""\
You are Nova, an autonomous AI agent. This is your RESEARCH CYCLE.
Current time: {ts}

YOUR MISSION: Scan your codebase AND both memory systems for context, then
conduct deep research on a topic of your choice, create a research report,
and save it to BOTH of your memory systems. You have 10 minutes.

═══════════════════════════════════════════════════════════════
STEP 1: CONTEXT GATHERING — scan codebase + both memory systems
═══════════════════════════════════════════════════════════════

1a. Query Fusion Memory for prior research:
    - Call `get_last_checkpoint` to see session state
    - Call `query_memory` with query="research topics completed nova-core"
    - Call `query_memory` with query="knowledge gaps improvement areas"

1b. Scan Obsidian Vault for existing research:
    - Call `vault_list` on path "40-research" to see what exists
    - Call `vault_search` with query="research" to find research notes

1c. Codebase snapshot (your own code — look for gaps, patterns, opportunities):
{codebase_snapshot}

1d. Review recent outputs (DO NOT repeat these topics):
{recent_str}

1e. Active goals to align research with:
{goals_str}

═══════════════════════════════════════════════════════════════
STEP 2: CHOOSE A RESEARCH TOPIC
═══════════════════════════════════════════════════════════════

Pick ONE topic that:
- Has NOT been researched before (check memory + recent outputs above)
- Aligns with an active goal when possible
- Is informed by the codebase scan — what does nova-core need right now?
- Is actionable — results should improve nova-core
- Has enough depth for a substantive report

Topic categories (pick one, go deep):
- New MCP servers or tools for agent capabilities
- Autonomous agent architecture patterns (self-healing, reflection, planning)
- Production hardening for AI agent runtimes
- Memory/RAG innovations and knowledge graph techniques
- Code quality and testing automation for AI-generated code
- Security practices for autonomous AI systems
- Monitoring and observability for agent runtimes
- Task scheduling and workflow orchestration
- Self-improvement and meta-learning in AI agents
- Cost optimization for LLM-powered systems

═══════════════════════════════════════════════════════════════
STEP 3: DEEP WEB RESEARCH (use multiple tools)
═══════════════════════════════════════════════════════════════

- Run 3-5 search queries using `brave_web_search` and/or `tavily_search`
- Use `tavily_research` for at least one deep synthesis query
- Fetch 2-3 authoritative full pages using `fetch`
- Cross-reference findings across multiple sources
- Prefer sources from 2025-2026

═══════════════════════════════════════════════════════════════
STEP 4: WRITE OUTPUT REPORT
═══════════════════════════════════════════════════════════════

Create file: /home/nova/nova-core/OUTPUT/hb_research_{stamp}.md

Include:
- # Title with topic and date
- ## Executive Summary (2-3 paragraphs)
- ## Key Findings (numbered, detailed)
- ## Recommendations for Nova-Core (specific, actionable)
- ## Sources (URLs with brief descriptions)
- ## CONTRACT block (required — see below)

═══════════════════════════════════════════════════════════════
STEP 5: SAVE TO FUSION MEMORY (MANDATORY — do not skip)
═══════════════════════════════════════════════════════════════

Call `upsert_memory` with these exact parameters:
- content: Dense summary of findings (500-1000 chars)
- id: "research_{date_str}_<topic_slug>"
- metadata: {{
    "category": "research",
    "project": "nova-core",
    "topic": "<the research topic>",
    "date": "{date_str}",
    "confidence": "high",
    "source": "heartbeat"
  }}

═══════════════════════════════════════════════════════════════
STEP 6: SAVE TO OBSIDIAN VAULT (MANDATORY — do not skip)
═══════════════════════════════════════════════════════════════

Call `vault_write` with these exact parameters:
- path: "40-research/<topic-slug>-{date_str}.md"
- frontmatter (MUST match this schema exactly):
    type: "research-summary"
    research_id: "rs-<topic-slug>-{date_str}"
    title: "<Research Title>"
    topic: "<topic category>"
    date_researched: "{date_str}"
    sources_count: <integer — number of sources>
    confidence: "high"
    source: "nova-core-memory"
    tags:
      - "#type/research"
      - "<topic-tag>"
      - "heartbeat-research"
- body: Full research content (findings + recommendations + sources)

IMPORTANT: The frontmatter must include `source: "nova-core-memory"` and
tags must include "#type/research". Without these, vault_write will reject.

═══════════════════════════════════════════════════════════════
STEP 7: SELF-CHECK
═══════════════════════════════════════════════════════════════

Before exiting, verify:
1. OUTPUT file exists at /home/nova/nova-core/OUTPUT/hb_research_{stamp}.md
2. upsert_memory returned success
3. vault_write returned success
If any failed, retry ONCE.

## CONTRACT (at end of OUTPUT report)
summary: <one-line description>
files_changed: OUTPUT/hb_research_{stamp}.md
verification: OUTPUT exists, upsert_memory success, vault_write success
confidence: high

BEGIN NOW. Start with Step 1 — query your memory systems."""


def _build_planning_prompt() -> str:
    """Build the planning cycle prompt — create or revise implementation plans."""
    import heartbeat

    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    date_str = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y%m%d-%H%M%S")

    # Reuse the codebase scan
    codebase_snapshot = _scan_codebase()

    # Gather recent OUTPUT file names
    recent_outputs = []
    if heartbeat.OUTPUT_DIR.exists():
        for f in sorted(heartbeat.OUTPUT_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
            recent_outputs.append(f.stem)
    recent_str = "\n".join(f"  - {t}" for t in recent_outputs) or "  (none)"

    # Gather active goals
    goals_str = "(no active goals)"
    goals_file = heartbeat.STATE_DIR / "goals.json"
    if goals_file.exists():
        try:
            data = json.loads(goals_file.read_text())
            goals_list = data.get("goals", []) if isinstance(data, dict) else data
            active = [g for g in goals_list if g.get("status") != "done"]
            if active:
                goals_str = "\n".join(f"  - [{g.get('id', '?')}] {g.get('text', '?')}" for g in active)
        except Exception:
            pass

    return f"""\
You are Nova, an autonomous AI agent. This is your PLANNING CYCLE.
Current time: {ts}

YOUR MISSION: Scan your codebase AND both memory systems for full context,
then either CREATE a new phased implementation plan or REVISE an existing
plan based on new research findings. Save the plan to BOTH memory systems.
You have 10 minutes.

═══════════════════════════════════════════════════════════════
STEP 1: DEEP CONTEXT GATHERING
═══════════════════════════════════════════════════════════════

1a. Query Fusion Memory for existing plans and recent research:
    - Call `get_last_checkpoint` to see session state
    - Call `query_memory` with query="implementation plan nova-core phases"
    - Call `query_memory` with query="recent research findings discoveries"
    - Call `query_memory` with query="enhancement plan revision"

1b. Scan Obsidian Vault for existing plans and patterns:
    - Call `vault_search` with query="implementation plan"
    - Call `vault_search` with query="enhancement"
    - Call `vault_list` on path "40-research" to see recent research

1c. Codebase snapshot (your own code — understand what exists):
{codebase_snapshot}

1d. Recent outputs (research reports to build plans from):
{recent_str}

1e. Active goals:
{goals_str}

═══════════════════════════════════════════════════════════════
STEP 2: DECIDE — CREATE NEW or REVISE EXISTING
═══════════════════════════════════════════════════════════════

Based on your context scan, decide:

A) CREATE a new plan if:
   - No current plan exists, OR
   - The existing plan is fully completed, OR
   - New research has revealed a completely new direction

B) REVISE an existing plan if:
   - A current plan exists but new research has been done since last revision
   - Some phases are complete and need updating
   - Priorities have shifted based on new findings

When revising: read the existing plan fully, note what's done, what's
changed, and what new research suggests. Don't start from scratch.

═══════════════════════════════════════════════════════════════
STEP 3: BUILD THE PLAN
═══════════════════════════════════════════════════════════════

Your plan MUST follow this structure:

# Nova-Core Enhancement Plan v<N> — {date_str}

## Vision
One paragraph describing the overall direction.

## Current State
What's built, what's working, what's missing. Reference the codebase scan.

## Phase-by-Phase Implementation

### Phase <N>: <Name> (priority: high/medium/low)
**Goal:** What this phase achieves
**Prerequisites:** What must be done first
**Steps:**
1. Step with specific file paths and code changes
2. Step with specific commands or configurations
3. Step with verification criteria
**Estimated complexity:** small/medium/large
**Success criteria:** How to know it's done

(Repeat for each phase — aim for 3-7 phases)

## Research Gaps
Topics that need research before certain phases can start.

## Quick Wins
Small improvements (< 30 min each) that can be done immediately.

═══════════════════════════════════════════════════════════════
STEP 4: WRITE OUTPUT REPORT
═══════════════════════════════════════════════════════════════

Create file: /home/nova/nova-core/OUTPUT/hb_plan_{stamp}.md

Include the full plan plus a ## CONTRACT block at the end.

═══════════════════════════════════════════════════════════════
STEP 5: SAVE TO FUSION MEMORY (MANDATORY)
═══════════════════════════════════════════════════════════════

Call `upsert_memory` with:
- content: Plan summary with phase names and priorities (500-1000 chars)
- id: "plan_{date_str}_<plan_slug>"
- metadata: {{
    "category": "decision",
    "project": "nova-core",
    "topic": "enhancement_plan",
    "date": "{date_str}",
    "confidence": "high",
    "source": "heartbeat",
    "plan_version": "<version number>"
  }}

═══════════════════════════════════════════════════════════════
STEP 6: SAVE TO OBSIDIAN VAULT (MANDATORY)
═══════════════════════════════════════════════════════════════

Call `vault_write` with EXACTLY this frontmatter (do NOT change the type or source):
- path: "00-inbox/plan-nova-core-{stamp}.md"
- frontmatter:
    type: "implementation-plan"
    plan_id: "plan-nova-core-{stamp}"
    title: "Nova-Core Enhancement Plan"
    date_created: "{date_str}"
    confidence: "high"
    source: "nova-core-memory"
    tags:
      - "#type/plan"
      - "planning"
      - "heartbeat-planning"
- body: Full plan content

CRITICAL: You MUST use type: "implementation-plan" and source: "nova-core-memory".
Any other values will be rejected by schema validation.

═══════════════════════════════════════════════════════════════
STEP 7: SELF-CHECK
═══════════════════════════════════════════════════════════════

Verify:
1. OUTPUT file exists at /home/nova/nova-core/OUTPUT/hb_plan_{stamp}.md
2. upsert_memory returned success
3. vault_write returned success
If any failed, retry ONCE.

## CONTRACT (at end of OUTPUT report)
summary: <one-line: created or revised plan>
files_changed: OUTPUT/hb_plan_{stamp}.md
verification: OUTPUT exists, upsert_memory success, vault_write success
confidence: high

BEGIN NOW. Start with Step 1 — gather context from memory and codebase."""
