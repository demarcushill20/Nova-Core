"""Prompt-building functions for the heartbeat module.

Extracted from heartbeat.py (Phase 4.5: Heartbeat Prompt Extraction).
All functions maintain identical signatures and behavior.

These functions use lazy imports of heartbeat module constants
(TASKS_DIR, OUTPUT_DIR, STATE_DIR, etc.) to avoid circular imports,
since heartbeat.py imports from this module.
"""

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

_CODEBASE_CACHE_FILE = Path("/home/nova/nova-core/STATE/codebase_snapshot.json")
_CODEBASE_CACHE_TTL = 6 * 3600  # 6 hours


def _gather_extended_state(checks: list) -> str:
    """Collect system state summary for the LLM heartbeat agent."""
    import heartbeat

    parts = []

    # Service status summary — prominent, unambiguous
    service_checks = [c for c in checks if c["name"].startswith("service:")]
    svc_pass = sum(1 for c in service_checks if c["ok"])
    svc_fail = len(service_checks) - svc_pass
    if svc_fail == 0:
        parts.append(f"## SERVICE STATUS: ALL {svc_pass} SERVICES RUNNING ✓")
        for c in service_checks:
            parts.append(f"  ✓ {c['name']}: {c['detail']}")
    else:
        parts.append(f"## SERVICE STATUS: {svc_fail} FAILED, {svc_pass} OK")
        for c in service_checks:
            mark = "✓" if c["ok"] else "✗ FAILED"
            parts.append(f"  {mark} {c['name']}: {c['detail']}")

    # Deterministic check results (non-service)
    parts.append("\n## Other Health Checks")
    for c in checks:
        if c["name"].startswith("service:"):
            continue  # already shown above
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

    # Autonomy progress snapshot (Phase 2.4)
    autonomy_file = heartbeat.STATE_DIR / "autonomy_snapshot.json"
    if autonomy_file.exists():
        try:
            snap = json.loads(autonomy_file.read_text())
            score = snap.get("overall_score", 0)
            alert = snap.get("alert_level", "unknown").upper()
            parts.append(f"\n## Autonomy Progress: {score}/100 ({alert})")
            for dim_name, dim_data in snap.get("dimensions", {}).items():
                d_score = dim_data.get("score", 0)
                d_trend = dim_data.get("trend", "stable")
                parts.append(f"  - {dim_name}: {d_score:.0f} ({d_trend})")
            dec = snap.get("decision", {})
            if dec:
                parts.append(f"  Last decision: {dec.get('mode', '?')} — {dec.get('reason', '')[:100]}")
            goal = snap.get("goal_tree", {})
            if goal:
                parts.append(f"  Goal tree: {goal.get('completed', 0)}/{goal.get('total', 0)} sub-goals complete")
                actionable = goal.get("actionable", [])
                if actionable:
                    parts.append(f"  Actionable: {', '.join(actionable[:3])}")
        except Exception:
            pass

    # Last heartbeat agent action (to avoid repeating)
    # Filter out false service-down claims to prevent feedback loops
    if heartbeat.HEARTBEAT_AGENT_LOG.exists():
        try:
            lines = heartbeat.HEARTBEAT_AGENT_LOG.read_text().strip().splitlines()
            if lines:
                last_line = lines[-1][:200]
                # Suppress false service-down claims from feedback loop
                _lower = last_line.lower()
                _svc_phrases = ["service", "dead", "down", "failed"]
                if sum(1 for p in _svc_phrases if p in _lower) >= 2 and svc_fail == 0:
                    parts.append("\n## Last Agent Action")
                    parts.append(
                        "  (previous action contained false service-down claim — suppressed to break feedback loop)"
                    )
                else:
                    parts.append("\n## Last Agent Action")
                    parts.append(f"  {last_line}")
        except Exception:
            pass

    return "\n".join(parts)


def _scan_codebase() -> str:
    """Scan the nova-core codebase and return a structured snapshot.

    Caches results in STATE/codebase_snapshot.json with 6h TTL.
    """
    # Check cache first
    if _CODEBASE_CACHE_FILE.exists():
        try:
            cache = json.loads(_CODEBASE_CACHE_FILE.read_text())
            cached_at = cache.get("cached_at", 0)
            if (time.time() - cached_at) < _CODEBASE_CACHE_TTL:
                return cache.get("snapshot", "(cached scan empty)")
        except Exception:
            pass

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

    snapshot = "\n".join(parts) if parts else "(codebase scan failed)"

    # Cache the result
    try:
        _CODEBASE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CODEBASE_CACHE_FILE.write_text(
            json.dumps(
                {
                    "cached_at": time.time(),
                    "snapshot": snapshot,
                }
            )
        )
    except Exception:
        pass

    return snapshot


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

    ctx_line = (
        "CONTEXT: Query memory (get_last_checkpoint, "
        'query_memory "research topics"), '
        'scan vault (vault_list "40-research"), review goals.'
    )
    cats = (
        "Categories: MCP tools, agent architecture, "
        "production hardening, memory/RAG, code quality, "
        "security, monitoring, scheduling, meta-learning, "
        "cost optimization."
    )
    mem_meta = (
        f'{{"category":"research","project":"nova-core",'
        f'"topic":"<topic>","date":"{date_str}",'
        f'"confidence":"high","source":"heartbeat"}}'
    )
    vault_fm = (
        f'{{type:"research-summary", '
        f'research_id:"rs-<slug>-{date_str}", '
        f'title:"<title>", topic:"<topic>", '
        f'date_researched:"{date_str}", '
        f"sources_count:<N>, "
        f'confidence:"high", source:"nova-core-memory", '
        f'tags:["#type/research","<topic-tag>",'
        f'"heartbeat-research"]}}'
    )
    return f"""\
You are Nova. RESEARCH CYCLE — {ts}

{ctx_line}

Recent outputs (don't repeat): {recent_str}
Active goals: {goals_str}
Codebase: {len(codebase_snapshot)} chars snapshot — run git ls-files if needed.

TASK: Pick ONE unresearched topic aligned with goals. {cats}

RESEARCH: 3-5 searches (tavily_search/tavily_research),
fetch 2-3 pages, cross-reference. Prefer 2025-2026 sources.

WRITE: /home/nova/nova-core/OUTPUT/hb_research_{stamp}.md
Format: # Title, ## Executive Summary, ## Key Findings,
## Recommendations, ## Sources, ## CONTRACT

SAVE TO MEMORY: upsert_memory(
  content=<500-1000 char summary>,
  id="research_{date_str}_<slug>",
  metadata={mem_meta})

SAVE TO VAULT: vault_write(
  path="40-research/<slug>-{date_str}.md",
  frontmatter={vault_fm},
  body=<full content>)

VERIFY: OUTPUT file exists, upsert_memory success,
vault_write success. Retry once on failure.

## CONTRACT
summary: <one-line>
files_changed: OUTPUT/hb_research_{stamp}.md
verification: OUTPUT exists, memory saved, vault saved
confidence: high"""


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

    plan_ctx = (
        "CONTEXT: Query memory (get_last_checkpoint, "
        'query_memory "implementation plan", '
        '"recent research", "enhancement plan"). '
        'Scan vault (vault_search "implementation plan", '
        '"enhancement", vault_list "40-research").'
    )
    plan_decide = (
        "DECIDE: CREATE new plan if none exists/all complete/"
        "new direction. REVISE existing plan if new research "
        "available/priorities shifted."
    )
    plan_mem_meta = (
        f'{{"category":"decision","project":"nova-core",'
        f'"topic":"enhancement_plan",'
        f'"date":"{date_str}","confidence":"high",'
        f'"source":"heartbeat","plan_version":"<N>"}}'
    )
    plan_vault_fm = (
        f'{{type:"implementation-plan", '
        f'plan_id:"plan-nova-core-{stamp}", '
        f'title:"Nova-Core Enhancement Plan", '
        f'date_created:"{date_str}", confidence:"high", '
        f'source:"nova-core-memory", '
        f'tags:["#type/plan","planning",'
        f'"heartbeat-planning"]}}'
    )
    return f"""\
You are Nova. PLANNING CYCLE — {ts}

{plan_ctx}

Recent outputs: {recent_str}
Active goals: {goals_str}
Codebase: {len(codebase_snapshot)} chars snapshot — run git ls-files if needed.

{plan_decide}

BUILD PLAN:
# Nova-Core Enhancement Plan v<N> — {date_str}
## Vision (1 paragraph)
## Current State (what's built, working, missing)
## Phase-by-Phase: Goal, Prerequisites, Steps, Complexity,
   Success Criteria
## Research Gaps
## Quick Wins (<30 min each)

WRITE: /home/nova/nova-core/OUTPUT/hb_plan_{stamp}.md
(include ## CONTRACT at end)

SAVE TO MEMORY: upsert_memory(
  content=<plan summary 500-1000 chars>,
  id="plan_{date_str}_<slug>",
  metadata={plan_mem_meta})

SAVE TO VAULT: vault_write(
  path="00-inbox/plan-nova-core-{stamp}.md",
  frontmatter={plan_vault_fm},
  body=<full plan>)

VERIFY: OUTPUT file exists, memory saved, vault saved.
Retry once on failure.

## CONTRACT
summary: <created or revised plan>
files_changed: OUTPUT/hb_plan_{stamp}.md
verification: OUTPUT exists, memory saved, vault saved
confidence: high"""
