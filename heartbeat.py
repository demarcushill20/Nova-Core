#!/usr/bin/env python3
"""NovaCore heartbeat — proactive health monitoring.

Runs as a systemd oneshot (triggered by novacore-heartbeat.timer every 30min).
Checks service health, disk, task queue, and worker liveness.
Writes HEARTBEAT.md, alerts via Telegram on failure, optionally injects repair tasks.

Stdlib only — no pip installs required.
"""

import json
import os
import re
import signal
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Phase 6B.13: Graceful shutdown flag for heartbeat oneshot
# ---------------------------------------------------------------------------
_heartbeat_shutdown_requested = False


def _heartbeat_signal_handler(signum, _frame):
    """Signal handler for heartbeat — set flag, no I/O."""
    global _heartbeat_shutdown_requested
    _heartbeat_shutdown_requested = True


signal.signal(signal.SIGTERM, _heartbeat_signal_handler)
signal.signal(signal.SIGINT, _heartbeat_signal_handler)

try:
    from utils.sd_notify import notify_status as _sd_notify_status
except ImportError:
    _sd_notify_status = None  # type: ignore[assignment,misc]

try:
    from utils.structured_log import slog
    from utils.trace_context import TraceContext
except ImportError:
    slog = None  # type: ignore[assignment,misc]
    TraceContext = None  # type: ignore[assignment,misc]

try:
    from utils.dual_memory_health import CrossSystemHealthChecker
except ImportError:
    CrossSystemHealthChecker = None  # type: ignore[assignment,misc]

try:
    from utils.max_plan_guard import record_invocation as _mpg_record
    from utils.max_plan_guard import should_allow_task as _mpg_allow
except ImportError:
    _mpg_record = None  # type: ignore[assignment,misc]
    _mpg_allow = None  # type: ignore[assignment,misc]

try:
    from utils.self_healing import (
        DegradationTier as _DegradationTier,
    )
    from utils.self_healing import (
        get_degradation_tier as _sh_get_tier,
    )
    from utils.self_healing import (
        record_error as _sh_record_error,
    )
    from utils.self_healing import (
        record_memory_snapshot as _sh_record_mem,
    )
    from utils.self_healing import (
        set_degradation_tier as _sh_set_tier,
    )
except ImportError:
    _DegradationTier = None  # type: ignore[assignment,misc]
    _sh_get_tier = None  # type: ignore[assignment,misc]
    _sh_record_error = None  # type: ignore[assignment,misc]
    _sh_record_mem = None  # type: ignore[assignment,misc]
    _sh_set_tier = None  # type: ignore[assignment,misc]

try:
    from agents.budget_enforcer import budget as _budget_enforcer
except ImportError:
    _budget_enforcer = None  # type: ignore[assignment,misc]

try:
    from utils.cruise_mode import update_cruise_state as _update_cruise_state
except ImportError:
    _update_cruise_state = None  # type: ignore[assignment,misc]

try:
    from utils.scheduler.calibration import (
        compute_calibration as _compute_calibration,
    )
    from utils.scheduler.calibration import (
        generate_calibration_report as _generate_calibration_report,
    )
    from utils.scheduler.execution_log import read_execution_log as _read_execution_log
    from utils.scheduler.interrupt_handler import (
        InterruptEvent as _InterruptEvent,
    )
    from utils.scheduler.interrupt_handler import (
        classify_interrupt as _classify_interrupt,
    )
    from utils.scheduler.interrupt_handler import (
        load_interrupt_state as _load_interrupt_state,
    )
    from utils.scheduler.interrupt_handler import (
        save_interrupt_state as _save_interrupt_state,
    )
    from utils.scheduler.orchestrator import load_scheduler_config as _load_sched_config

    _HAS_SCHEDULER = True
except ImportError:
    _HAS_SCHEDULER = False

try:
    import asyncio as _asyncio

    from novatrade.autonomy import (
        ActionMode as _ActionMode,
    )
    from novatrade.autonomy import (
        ContextAssembler as _ContextAssembler,
    )
    from novatrade.autonomy import (
        DecisionEngine as _DecisionEngine,
    )
    from novatrade.autonomy import (
        DirectActionExecutor as _DirectActionExecutor,
    )
    from novatrade.autonomy import (
        GoalDecomposer as _GoalDecomposer,
    )
    from novatrade.autonomy import (
        OutcomeAnalyzer as _OutcomeAnalyzer,
    )
    from novatrade.autonomy import (
        ProgressScorer as _ProgressScorer,
    )
    from novatrade.autonomy import (
        ReasoningEngine as _ReasoningEngine,
    )
    from novatrade.autonomy import (
        TaskSpecGenerator as _TaskSpecGenerator,
    )
    from novatrade.autonomy import (
        build_novatrade_tree as _build_novatrade_tree,
    )
except ImportError:
    _asyncio = None  # type: ignore[assignment,misc]
    _ActionMode = None  # type: ignore[assignment,misc]
    _ContextAssembler = None  # type: ignore[assignment,misc]
    _DecisionEngine = None  # type: ignore[assignment,misc]
    _DirectActionExecutor = None  # type: ignore[assignment,misc]
    _GoalDecomposer = None  # type: ignore[assignment,misc]
    _ProgressScorer = None  # type: ignore[assignment,misc]
    _TaskSpecGenerator = None  # type: ignore[assignment,misc]
    _build_novatrade_tree = None  # type: ignore[assignment,misc]
    _OutcomeAnalyzer = None  # type: ignore[assignment,misc]
    _ReasoningEngine = None  # type: ignore[assignment,misc]

from prompts.heartbeat_prompts import (  # noqa: E402
    _build_planning_prompt,
    _build_research_prompt,
    _gather_extended_state,
)


@dataclass
class HeartbeatSnapshot:
    """Type-safe container for a single heartbeat metrics sample."""

    ts: str  # ISO 8601 UTC timestamp
    epoch: int  # Unix epoch seconds
    status: str  # "healthy" | "unhealthy"
    total_checks: int
    passed: int
    failed: int
    failed_names: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    checks: dict = field(default_factory=dict)  # check_name -> {ok, detail}


# --- Configuration -----------------------------------------------------------

BASE = Path("/home/nova/nova-core")
HEARTBEAT_FILE = BASE / "HEARTBEAT.md"
STATE_DIR = BASE / "STATE"
TASKS_DIR = BASE / "TASKS"
OUTPUT_DIR = BASE / "OUTPUT"
LOGS_DIR = BASE / "LOGS"

DISK_WARN_PERCENT = 85
ORPHAN_INPROGRESS_MINUTES = 15
MAX_PENDING_TASKS = 10
PENDING_ALERT_AGE_HOURS = 6  # only count tasks pending longer than this as problematic
BACKUP_DIR = Path("/home/nova/backups")
BACKUP_MAX_AGE_HOURS = 26  # alert if no backup in ~1 day
LOG_SIZE_WARN_MB = 50
STATE_DIR_WARN_FILES = 50000
GW_TOKEN_FILE = Path.home() / ".config" / "nova-core" / "google" / "token.json"

SERVICES = [
    "novacore-watcher",
    "novacore-telegram",
    "novacore-telegram-notifier",
]

# Telegram cooldown settings (seconds)
TELEGRAM_COOLDOWN_DEFAULT = 1800  # 30 minutes for general agent alerts
TELEGRAM_COOLDOWN_COST = 14400  # 4 hours for cost alerts
TELEGRAM_COOLDOWN_FILE = STATE_DIR / "telegram_cooldown.json"


# --- Health checks -----------------------------------------------------------


def check_service(name: str) -> dict:
    """Check if a systemd service is active."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        active = result.stdout.strip() == "active"
        if active:
            info = subprocess.run(
                ["systemctl", "show", name, "--property=MainPID,ActiveEnterTimestamp"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            props = dict(line.split("=", 1) for line in info.stdout.strip().splitlines() if "=" in line)
            pid = props.get("MainPID", "?")
            since = props.get("ActiveEnterTimestamp", "?")
            detail = f"active (pid {pid}, since {since})"
        else:
            detail = f"NOT ACTIVE ({result.stdout.strip()})"
        return {"name": f"service:{name}", "ok": active, "detail": detail}
    except Exception as e:
        return {"name": f"service:{name}", "ok": False, "detail": f"check failed: {e}"}


def check_disk() -> dict:
    """Check disk usage on the partition containing ~/nova-core."""
    try:
        st = os.statvfs(str(BASE))
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used_pct = round((1 - free / total) * 100, 1)
        free_gb = round(free / (1024**3), 1)
        ok = used_pct < DISK_WARN_PERCENT
        return {"name": "disk", "ok": ok, "detail": f"{used_pct}% used ({free_gb}GB free)"}
    except Exception as e:
        return {"name": "disk", "ok": False, "detail": f"check failed: {e}"}


def check_claude_binary() -> dict:
    """Check that the Claude CLI binary is accessible."""
    claude_path = Path(os.environ.get("CLAUDE_BIN", "/home/nova/.local/bin/claude"))
    ok = claude_path.exists() and os.access(str(claude_path), os.X_OK)
    detail = "accessible" if ok else "NOT FOUND or not executable"
    return {"name": "claude_binary", "ok": ok, "detail": detail}


def _parse_scheduled_at(path: Path) -> datetime | None:
    """Extract ``scheduled_at`` from YAML frontmatter without a YAML dependency.

    Returns a timezone-aware (UTC) datetime if found, else None.
    """
    _SCHEDULED_RE = re.compile(r"^scheduled_at:\s*(.+)$", re.MULTILINE)
    try:
        # Read only the first 1 KB — frontmatter is always at the top
        text = path.read_text(encoding="utf-8", errors="replace")[:1024]
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("---", 3)
    if end < 0:
        return None
    fm_block = text[3:end]
    m = _SCHEDULED_RE.search(fm_block)
    if not m:
        return None
    raw = m.group(1).strip().strip("'\"")
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    # Treat naive datetimes as UTC (the watcher does the same)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def check_task_queue() -> dict:
    """Check for pending tasks and orphaned .inprogress files.

    Only tasks older than PENDING_ALERT_AGE_HOURS are counted as problematic
    to avoid false alarms from freshly generated shift/conversation tasks.
    Tasks with a future ``scheduled_at`` frontmatter field are excluded from
    the stale count — they are correctly waiting for their scheduled time.
    """
    if not TASKS_DIR.exists():
        return {"name": "task_queue", "ok": True, "detail": "no TASKS dir"}

    lifecycle_suffixes = (".inprogress", ".done", ".failed", ".cancelled")
    pending = [p for p in TASKS_DIR.glob("*.md") if not any(p.name.endswith(s) for s in lifecycle_suffixes)]

    # Only count stale tasks (older than PENDING_ALERT_AGE_HOURS) toward the alert threshold
    # Exclude tasks with a future scheduled_at — they are properly deferred, not stale
    now = datetime.now(timezone.utc)
    age_cutoff = now - timedelta(hours=PENDING_ALERT_AGE_HOURS)
    stale_pending = []
    deferred_count = 0
    for p in pending:
        # Check scheduled_at first — future-scheduled tasks are never stale
        sched = _parse_scheduled_at(p)
        if sched is not None and sched > now:
            deferred_count += 1
            continue
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
            if mtime < age_cutoff:
                stale_pending.append(p)
        except OSError:
            continue

    inprogress = list(TASKS_DIR.glob("*.inprogress"))
    orphaned = []
    for ip in inprogress:
        age_min = (now - datetime.fromtimestamp(ip.stat().st_mtime, tz=timezone.utc)).total_seconds() / 60
        if age_min > ORPHAN_INPROGRESS_MINUTES:
            orphaned.append(ip.name)

    # Auto-recover orphaned tasks inline — reset to .md for re-dispatch
    auto_recovered = []
    if orphaned:
        for orphan_name in list(orphaned):
            orphan_path = TASKS_DIR / orphan_name
            stem = orphan_name.replace(".md.inprogress", "")
            task_path = TASKS_DIR / f"{stem}.md"
            failed_path = TASKS_DIR / f"{stem}.md.failed"

            # Check retry eligibility
            can_retry = True
            try:
                from utils.task_checkpoint import MAX_TASK_RETRIES, increment_retry

                cp = increment_retry(stem)
                if cp is not None and cp.retry_count >= MAX_TASK_RETRIES:
                    can_retry = False
            except Exception:
                pass

            try:
                if can_retry:
                    orphan_path.rename(task_path)
                    auto_recovered.append(f"{stem}→retry")
                else:
                    orphan_path.rename(failed_path)
                    auto_recovered.append(f"{stem}→failed(max-retries)")
                orphaned.remove(orphan_name)
            except (OSError, FileNotFoundError):
                pass  # leave in orphaned list if rename fails

    ok = len(stale_pending) <= MAX_PENDING_TASKS and len(orphaned) == 0
    detail = (
        f"{len(pending)} pending ({len(stale_pending)} stale, {deferred_count} deferred), {len(inprogress)} in-progress"
    )
    if auto_recovered:
        detail += f", AUTO-RECOVERED: {', '.join(auto_recovered)}"
    if orphaned:
        detail += f", ORPHANED: {', '.join(orphaned)}"
    return {"name": "task_queue", "ok": ok, "detail": detail}


def check_last_output() -> dict:
    """Check recency of last OUTPUT file (informational, not critical)."""
    if not OUTPUT_DIR.exists():
        return {"name": "last_output", "ok": True, "detail": "no OUTPUT dir"}

    outputs = sorted(
        OUTPUT_DIR.glob("*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not outputs:
        return {"name": "last_output", "ok": True, "detail": "no outputs yet"}

    latest = outputs[0]
    age_min = (
        datetime.now(timezone.utc) - datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc)
    ).total_seconds() / 60
    detail = f"{latest.name} ({round(age_min)}min ago)"
    return {"name": "last_output", "ok": True, "detail": detail}


def check_stale_workers() -> dict:
    """Check for PID files in STATE/running/ pointing to dead processes."""
    running_dir = STATE_DIR / "running"
    if not running_dir.exists():
        return {"name": "stale_workers", "ok": True, "detail": "no running dir"}

    stale = []
    for pid_file in running_dir.glob("*.pid"):
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
        except ProcessLookupError:
            stale.append(f"{pid_file.stem} (pid {pid} dead)")
        except (ValueError, PermissionError):
            pass

    ok = len(stale) == 0
    detail = "all clean" if ok else f"{len(stale)} stale: {', '.join(stale)}"
    return {"name": "stale_workers", "ok": ok, "detail": detail}


def check_state_files() -> dict:
    """Validate structure of critical state files (e.g., goals.json)."""
    issues = []
    goals_file = STATE_DIR / "goals.json"
    if goals_file.exists():
        try:
            data = json.loads(goals_file.read_text())
            if isinstance(data, list):
                issues.append("goals.json is bare array (expected dict wrapper)")
            elif isinstance(data, dict):
                if "goals" not in data or "next_id" not in data:
                    issues.append("goals.json missing 'goals' or 'next_id' keys")
            else:
                issues.append("goals.json has unexpected type")
        except json.JSONDecodeError as e:
            issues.append(f"goals.json invalid JSON: {e}")
        except OSError as e:
            issues.append(f"goals.json read error: {e}")

    ok = len(issues) == 0
    detail = "all valid" if ok else "; ".join(issues)
    return {"name": "state_files", "ok": ok, "detail": detail}


def check_metrics() -> dict:
    """Check STATE/metrics.json for anomalous failure rates."""
    metrics_file = STATE_DIR / "metrics.json"
    if not metrics_file.exists():
        return {"name": "metrics", "ok": True, "detail": "no metrics file yet"}
    try:
        data = json.loads(metrics_file.read_text())
        if not isinstance(data, dict):
            return {"name": "metrics", "ok": True, "detail": "empty metrics"}
        cf = data.get("contract_failure", 0)
        cs = data.get("contract_success", 0)
        failures = cf.get("_total", 0) if isinstance(cf, dict) else cf
        successes = cs.get("_total", 0) if isinstance(cs, dict) else cs
        total = failures + successes
        if total == 0:
            return {"name": "metrics", "ok": True, "detail": "no executions recorded"}
        fail_rate = round(failures / total * 100, 1)
        ok = fail_rate < 50
        return {"name": "metrics", "ok": ok, "detail": f"{fail_rate}% failure rate ({failures}/{total})"}
    except Exception as e:
        return {"name": "metrics", "ok": False, "detail": f"parse error: {e}"}


def check_google_workspace() -> dict:
    """Check that the Google Workspace OAuth token is valid."""
    if not GW_TOKEN_FILE.exists():
        return {"name": "google_workspace", "ok": True, "detail": "not configured (no token)"}
    try:
        data = json.loads(GW_TOKEN_FILE.read_text())
        has_refresh = bool(data.get("refresh_token"))
        expiry = data.get("expiry", "")
        if expiry:
            # Token file stores expiry as ISO string
            exp_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if exp_dt < now and not has_refresh:
                return {"name": "google_workspace", "ok": False, "detail": "token expired, no refresh token"}
        detail = "token valid" if has_refresh else "token present (no refresh)"
        return {"name": "google_workspace", "ok": has_refresh, "detail": detail}
    except Exception as e:
        return {"name": "google_workspace", "ok": False, "detail": f"token check failed: {e}"}


def check_backup() -> dict:
    """Verify a recent backup exists in /home/nova/backups/."""
    if not BACKUP_DIR.exists():
        return {"name": "backup", "ok": False, "detail": f"backup dir missing: {BACKUP_DIR}"}

    tarballs = sorted(BACKUP_DIR.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not tarballs:
        return {"name": "backup", "ok": False, "detail": "no backups found"}

    latest = tarballs[0]
    age_hr = (
        datetime.now(timezone.utc) - datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc)
    ).total_seconds() / 3600
    size_mb = round(latest.stat().st_size / (1024**2), 1)
    ok = age_hr < BACKUP_MAX_AGE_HOURS
    detail = f"{latest.name} ({round(age_hr, 1)}h ago, {size_mb}MB)"
    if not ok:
        detail += " — STALE"
    return {"name": "backup", "ok": ok, "detail": detail}


def check_ruff() -> dict:
    """Run ruff on staged/tracked Python files and report violation count."""
    try:
        result = subprocess.run(
            ["ruff", "check", "--select", "E,F,W", "--statistics", "--quiet", str(BASE)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return {"name": "ruff", "ok": True, "detail": "0 violations"}
        lines = result.stdout.strip().splitlines()
        count = len(lines)
        detail = f"{count} violation(s)"
        if lines:
            detail += f" — top: {lines[0][:80]}"
        # Informational, not blocking — ok=True but report count
        return {"name": "ruff", "ok": True, "detail": detail}
    except FileNotFoundError:
        return {"name": "ruff", "ok": True, "detail": "ruff not installed"}
    except subprocess.TimeoutExpired:
        return {"name": "ruff", "ok": True, "detail": "timed out (30s)"}
    except Exception as e:
        return {"name": "ruff", "ok": True, "detail": f"check failed: {e}"}


def check_memory_systems() -> dict:
    """Check connectivity and cross-system health for Fusion Memory and Obsidian Vault."""
    issues = []

    # Basic connectivity checks first
    fusion_server = Path.home() / "Nova_AI_Fusion_Memory_MCP" / "mcp_server.py"
    if not fusion_server.exists():
        issues.append("Fusion Memory mcp_server.py missing")

    vault_dir = Path("/home/nova/nova-vault")
    if not vault_dir.exists():
        issues.append("Obsidian vault dir missing")
    else:
        meta_dir = vault_dir / "_meta"
        if not meta_dir.exists() or not list(meta_dir.glob("*.md")):
            issues.append("Obsidian vault _meta/ empty or missing")

    # Enhanced cross-system health monitoring (if available)
    if CrossSystemHealthChecker is not None and len(issues) == 0:
        try:
            checker = CrossSystemHealthChecker(max_checks_per_run=1)  # Light check for heartbeat
            # Only run a basic check to avoid expensive operations
            # This will detect major drift without full validation
            if hasattr(checker, "check_basic_connectivity"):
                basic_result = checker.check_basic_connectivity()
                if basic_result and basic_result.status != "ok":
                    issues.append(f"cross-system drift: {basic_result.check_name}")
        except Exception as e:
            # Don't fail the whole check if enhanced monitoring fails
            issues.append(f"enhanced check failed: {str(e)[:50]}...")

    ok = len(issues) == 0
    detail = "both systems healthy" if ok else "; ".join(issues)
    return {"name": "memory_systems", "ok": ok, "detail": detail}


def check_log_sizes() -> dict:
    """Check for oversized log files that need rotation."""
    large = []
    if LOGS_DIR.exists():
        for log_file in LOGS_DIR.iterdir():
            if log_file.is_file():
                size_mb = log_file.stat().st_size / (1024**2)
                if size_mb > LOG_SIZE_WARN_MB:
                    large.append(f"{log_file.name} ({round(size_mb, 1)}MB)")

    ok = len(large) == 0
    if ok:
        total_mb: float = 0
        if LOGS_DIR.exists():
            total_mb = sum(f.stat().st_size for f in LOGS_DIR.iterdir() if f.is_file()) / (1024**2)
        detail = f"all under {LOG_SIZE_WARN_MB}MB (total: {round(total_mb, 1)}MB)"
    else:
        detail = f"{len(large)} oversized: {', '.join(large)}"
    return {"name": "log_sizes", "ok": ok, "detail": detail}


def check_state_bloat() -> dict:
    """Check for STATE/ subdirectories with excessive file counts."""
    bloated = []
    if STATE_DIR.exists():
        for subdir in STATE_DIR.iterdir():
            if subdir.is_dir():
                try:
                    count = sum(1 for _ in subdir.iterdir())
                    if count > STATE_DIR_WARN_FILES:
                        bloated.append(f"{subdir.name}/ ({count} files)")
                except PermissionError:
                    pass

    ok = len(bloated) == 0
    detail = "all clean" if ok else f"bloated: {', '.join(bloated)}"
    return {"name": "state_bloat", "ok": ok, "detail": detail}


def check_pip_audit() -> dict:
    """Run pip-audit monthly. Only runs on the 1st and 15th of the month."""
    now = datetime.now(timezone.utc)
    if now.day not in (1, 15):
        return {"name": "pip_audit", "ok": True, "detail": f"skipped (runs on 1st/15th, today is {now.day}th)"}
    try:
        result = subprocess.run(
            ["pip-audit", "--format", "json", "--progress-spinner", "off"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout) if result.stdout.strip() else []
            if not data:
                return {"name": "pip_audit", "ok": True, "detail": "0 vulnerabilities"}
            vuln_count = len(data) if isinstance(data, list) else 0
            return {"name": "pip_audit", "ok": vuln_count == 0, "detail": f"{vuln_count} vulnerable package(s)"}
        else:
            # pip-audit returns non-zero when vulnerabilities found
            detail = result.stdout[:200] if result.stdout else result.stderr[:200]
            return {"name": "pip_audit", "ok": False, "detail": f"vulnerabilities found: {detail}"}
    except FileNotFoundError:
        return {"name": "pip_audit", "ok": True, "detail": "pip-audit not installed"}
    except subprocess.TimeoutExpired:
        return {"name": "pip_audit", "ok": True, "detail": "timed out (120s)"}
    except Exception as e:
        return {"name": "pip_audit", "ok": True, "detail": f"check failed: {e}"}


def check_llm_cache() -> dict:
    """Check LLM response cache health, hit rate, and memory usage."""
    try:
        from utils.llm_cache import llm_cache

        if not llm_cache.available:
            return {"name": "llm_cache", "ok": True, "detail": "Redis unavailable (cache inactive)"}

        stats = llm_cache.stats()
        memory_mb = stats.memory_bytes / (1024**2)
        issues = []

        # Alert: hit rate below 10% (only meaningful with enough traffic)
        total_lookups = stats.hits + stats.misses
        if total_lookups >= 20 and stats.hit_rate < 0.10:
            issues.append(f"low hit rate ({stats.hit_rate:.1%})")

        # Alert: cache memory above 500 MB
        if memory_mb > 500:
            issues.append(f"high memory ({memory_mb:.0f}MB)")

        if issues:
            detail = (
                f"{'; '.join(issues)} | "
                f"entries={stats.total_entries} "
                f"hits={stats.hits} misses={stats.misses} "
                f"rate={stats.hit_rate:.1%} mem={memory_mb:.1f}MB"
            )
            return {"name": "llm_cache", "ok": False, "detail": detail}

        detail = (
            f"entries={stats.total_entries} "
            f"hits={stats.hits} misses={stats.misses} "
            f"rate={stats.hit_rate:.1%} mem={memory_mb:.1f}MB"
        )
        return {"name": "llm_cache", "ok": True, "detail": detail}

    except Exception as e:
        return {"name": "llm_cache", "ok": True, "detail": f"check skipped: {e}"}


def check_cost_router() -> dict:
    """Check cost router health: budget utilization, alerts, and adaptive config."""
    try:
        from utils.cost_router import (
            check_budget_alerts,
            compute_heartbeat_config,
            get_cost_summary,
            get_rolling_average_cost,
            write_heartbeat_config,
        )

        cost = get_cost_summary()
        avg_daily = get_rolling_average_cost(days=7)
        alerts = check_budget_alerts()

        # Compute and persist adaptive heartbeat config
        hb_config = compute_heartbeat_config(tasks_dir=TASKS_DIR)
        write_heartbeat_config(hb_config)

        daily = cost.get("daily_cost_usd", 0.0)
        monthly = cost.get("monthly_cost_usd", 0.0)
        monthly_pct = cost.get("monthly_pct", 0.0)

        if alerts:
            alert_summary = "; ".join(a[:80] for a in alerts[:3])
            detail = (
                f"[WARN] {len(alerts)} alert(s): {alert_summary} | "
                f"daily=${daily:.2f} monthly=${monthly:.2f} ({monthly_pct:.1f}%) "
                f"7d_avg=${avg_daily:.2f}/day mode={hb_config.mode}"
            )
            return {"name": "cost_router", "ok": True, "detail": detail}

        detail = (
            f"daily=${daily:.2f} monthly=${monthly:.2f} ({monthly_pct:.1f}%) "
            f"7d_avg=${avg_daily:.2f}/day mode={hb_config.mode}"
        )
        return {"name": "cost_router", "ok": True, "detail": detail}

    except Exception as e:
        return {"name": "cost_router", "ok": True, "detail": f"check skipped: {e}"}


def check_novatrade_signals() -> dict:
    """Check NovaTrade signal generation rates and health status."""
    try:
        import json
        from pathlib import Path

        signal_stats_file = Path("/home/nova/nova-core/STATE/novatrade/signal_stats.json")

        if not signal_stats_file.exists():
            return {"name": "novatrade_signals", "ok": True, "detail": "no signal stats file (NovaTrade not running)"}

        with open(signal_stats_file) as f:
            stats = json.load(f)

        status = stats.get("status", "UNKNOWN")
        concern_level = stats.get("concern_level", "UNKNOWN")
        signals_1h = stats.get("signals_1h", 0)
        signals_4h = stats.get("signals_4h", 0)
        signals_24h = stats.get("signals_24h", 0)
        last_signal_at = stats.get("last_signal_at", "")

        regime = stats.get("regime", "")
        session = stats.get("session", "")

        # Convert timestamps for readability
        detail_parts = [
            f"status={status}",
            f"signals: 1h={signals_1h}, 4h={signals_4h}, 24h={signals_24h}",
        ]

        if regime or session:
            ctx = []
            if regime:
                ctx.append(f"regime={regime}")
            if session:
                ctx.append(f"session={session}")
            detail_parts.append(" ".join(ctx))

        if last_signal_at:
            try:
                from datetime import datetime, timezone

                last_dt = datetime.fromisoformat(last_signal_at.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                hours_ago = (now - last_dt).total_seconds() / 3600
                detail_parts.append(f"last: {hours_ago:.1f}h ago")
            except (ValueError, TypeError, KeyError):
                detail_parts.append(f"last: {last_signal_at[:19]}")

        detail = " | ".join(detail_parts)

        # Health assessment: RED/YELLOW = not ok, GREEN = ok
        # MARKET_CLOSED is always GREEN (expected during weekends)
        ok = concern_level == "GREEN"

        return {"name": "novatrade_signals", "ok": ok, "detail": detail}

    except Exception as e:
        return {"name": "novatrade_signals", "ok": True, "detail": f"check failed: {e}"}


# --- Output ------------------------------------------------------------------


def write_heartbeat(checks: list, autonomy_snapshot: dict | None = None) -> None:
    """Write HEARTBEAT.md with timestamped checklist and autonomy progress."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "# NovaCore Heartbeat",
        f"Last check: {now}",
        "",
    ]
    for c in checks:
        mark = "x" if c["ok"] else " "
        lines.append(f"- [{mark}] {c['name']}: {c['detail']}")

    all_ok = all(c["ok"] for c in checks)
    lines.append("")
    lines.append(f"Overall: {'HEALTHY' if all_ok else 'UNHEALTHY'}")

    # Autonomy progress section (Phase 2.4)
    if autonomy_snapshot:
        score = autonomy_snapshot.get("overall_score", 0)
        alert = autonomy_snapshot.get("alert_level", "unknown").upper()
        lines.append("")
        lines.append(f"## Progress Score: {score}/100 ({alert})")
        dims = autonomy_snapshot.get("dimensions", {})
        for dim_name, dim_data in dims.items():
            d_score = dim_data.get("score", 0)
            d_trend = dim_data.get("trend", "stable")
            lines.append(f"- {dim_name}: {d_score:.0f} ({d_trend})")

        dec = autonomy_snapshot.get("decision", {})
        if dec:
            lines.append(f"\nDecision: **{dec.get('mode', '?')}** — {dec.get('reason', '')[:120]}")

        goal = autonomy_snapshot.get("goal_tree", {})
        if goal:
            lines.append(f"Goal tree: {goal.get('completed', 0)}/{goal.get('total', 0)} sub-goals complete")

    lines.append("")

    HEARTBEAT_FILE.write_text("\n".join(lines) + "\n")


# --- Metrics persistence -----------------------------------------------------

METRICS_JSONL = STATE_DIR / "heartbeat_metrics.jsonl"
METRICS_MAX_ENTRIES = 2000


def _append_metrics(snapshot: HeartbeatSnapshot) -> None:
    """Append a HeartbeatSnapshot to heartbeat_metrics.jsonl with 2000-entry rotation."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(snapshot)) + "\n"

    with open(METRICS_JSONL, "a") as f:
        f.write(line)

    # Rotate: if file exceeds max entries, keep the newest METRICS_MAX_ENTRIES
    try:
        lines = METRICS_JSONL.read_text().splitlines()
        if len(lines) > METRICS_MAX_ENTRIES:
            keep = lines[-METRICS_MAX_ENTRIES:]
            METRICS_JSONL.write_text("\n".join(keep) + "\n")
    except OSError:
        pass


def get_trend_summary(window: int = 100) -> dict:
    """Analyze the last ``window`` heartbeat snapshots for trends.

    Returns dict with:
      - availability_pct: % of snapshots that were healthy
      - total_snapshots: how many snapshots analyzed
      - failure_hotspots: list of (check_name, fail_count) sorted desc, top 5
      - recent_regressions: checks that failed in last 3 but passed in prior 10
    """
    if not METRICS_JSONL.exists():
        return {"availability_pct": 100.0, "total_snapshots": 0, "failure_hotspots": [], "recent_regressions": []}

    try:
        raw_lines = METRICS_JSONL.read_text().splitlines()
        entries = []
        for ln in raw_lines[-window:]:
            if ln.strip():
                entries.append(json.loads(ln))
    except (json.JSONDecodeError, OSError):
        return {"availability_pct": 100.0, "total_snapshots": 0, "failure_hotspots": [], "recent_regressions": []}

    if not entries:
        return {"availability_pct": 100.0, "total_snapshots": 0, "failure_hotspots": [], "recent_regressions": []}

    healthy_count = sum(1 for e in entries if e.get("status") == "healthy")
    availability = round(healthy_count / len(entries) * 100, 1)

    # Count failures per check name
    fail_counts: dict[str, int] = {}
    for e in entries:
        for name in e.get("failed_names", []):
            fail_counts[name] = fail_counts.get(name, 0) + 1
    hotspots = sorted(fail_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    # Regression detection: failed in last 3, passed in prior 10
    regressions: list[str] = []
    if len(entries) >= 4:
        recent = entries[-3:]
        prior = entries[-13:-3] if len(entries) >= 13 else entries[:-3]
        recent_fails: set[str] = set()
        for e in recent:
            recent_fails.update(e.get("failed_names", []))
        prior_fails: set[str] = set()
        for e in prior:
            prior_fails.update(e.get("failed_names", []))
        regressions = sorted(recent_fails - prior_fails)

    return {
        "availability_pct": availability,
        "total_snapshots": len(entries),
        "failure_hotspots": hotspots,
        "recent_regressions": regressions,
    }


def get_daily_delta() -> dict:
    """Compare today's heartbeat health with yesterday's.

    Groups metrics by calendar date (UTC) and returns:
      - today_score: average health score for today's snapshots
      - yesterday_score: average health score for yesterday's snapshots
      - delta: today - yesterday
      - today_samples: number of snapshots today
      - new_failures: checks that failed today but not yesterday
      - resolved_failures: checks that failed yesterday but not today
    """
    if not METRICS_JSONL.exists():
        return {
            "today_score": 100,
            "yesterday_score": 100,
            "delta": 0,
            "today_samples": 0,
            "new_failures": [],
            "resolved_failures": [],
        }

    try:
        lines = METRICS_JSONL.read_text().splitlines()
    except OSError:
        return {
            "today_score": 100,
            "yesterday_score": 100,
            "delta": 0,
            "today_samples": 0,
            "new_failures": [],
            "resolved_failures": [],
        }

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Bucket entries by date
    by_date: dict[str, list[dict]] = {}
    for ln in lines:
        if not ln.strip():
            continue
        try:
            entry = json.loads(ln)
        except json.JSONDecodeError:
            continue
        ts = entry.get("ts", "")
        date_key = ts[:10] if len(ts) >= 10 else ""
        if date_key:
            by_date.setdefault(date_key, []).append(entry)

    # Sort dates to find yesterday
    sorted_dates = sorted(by_date.keys())
    today_entries = by_date.get(today_str, [])

    # Find yesterday (most recent date before today)
    yesterday_str = ""
    for d in reversed(sorted_dates):
        if d < today_str:
            yesterday_str = d
            break

    yesterday_entries = by_date.get(yesterday_str, []) if yesterday_str else []

    def _avg_score(entries: list[dict]) -> int:
        if not entries:
            return 100
        scores = []
        for e in entries:
            total = e.get("total_checks", 22)
            passed = e.get("passed", total)
            scores.append(round(passed / total * 100) if total > 0 else 100)
        return round(sum(scores) / len(scores))

    def _all_failures(entries: list[dict]) -> set[str]:
        fails: set[str] = set()
        for e in entries:
            fails.update(e.get("failed_names", []))
        return fails

    today_score = _avg_score(today_entries)
    yesterday_score = _avg_score(yesterday_entries)
    today_fails = _all_failures(today_entries)
    yesterday_fails = _all_failures(yesterday_entries)

    return {
        "today_score": today_score,
        "yesterday_score": yesterday_score,
        "delta": today_score - yesterday_score,
        "today_samples": len(today_entries),
        "yesterday_date": yesterday_str,
        "new_failures": sorted(today_fails - yesterday_fails),
        "resolved_failures": sorted(yesterday_fails - today_fails),
    }


# --- Alerting ----------------------------------------------------------------


def _send_telegram(text: str) -> None:
    """Send a message to the configured Telegram chat."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("ALLOWED_CHAT_ID", "")
    if not token or not chat_id:
        print("WARN: TELEGRAM_BOT_TOKEN or ALLOWED_CHAT_ID not set, skipping alert")
        return

    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"WARN: Telegram send failed: {e}")


def _normalize_fingerprint(text: str) -> str:
    """Strip volatile parts (timestamps, dollar amounts, metrics) to produce a stable fingerprint.

    Alerts of the same *type* should match regardless of specific numbers.
    E.g. "High burn rate 4.75x baseline (13 calls/60m)" and
         "High burn rate 6.21x baseline (17 calls/60m)" → same fingerprint.
    """
    import hashlib
    import re

    # Remove timestamps like 12:34 UTC, 2026-03-17T08:41:37Z
    normalized = re.sub(r"\d{1,2}:\d{2}(?:\s*UTC)?", "", text)
    normalized = re.sub(r"\d{4}-\d{2}-\d{2}T[\d:]+Z?", "", normalized)
    # Collapse dollar amounts to just the integer part (so $22.50 and $22.51 match)
    normalized = re.sub(r"\$(\d+)\.\d+", r"$\1", normalized)
    # Strip all remaining digit sequences — metric values (burn rates, call counts,
    # thresholds) change every cycle but the alert *type* is what matters for dedup.
    normalized = re.sub(r"\d+", "", normalized)
    # Collapse whitespace
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _telegram_cooldown_gate(message: str, cooldown_secs: int | None = None) -> bool:
    """Check if a message should be sent based on cooldown. Returns True if allowed.

    Uses STATE/telegram_cooldown.json to persist fingerprint → timestamp mappings.
    Cost-related messages get a longer cooldown automatically.
    """
    if cooldown_secs is None:
        # Auto-detect: cost alerts get 4-hour cooldown, others 30 min
        lower = message.lower()
        if "cost" in lower or "budget" in lower or "spend" in lower:
            cooldown_secs = TELEGRAM_COOLDOWN_COST
        else:
            cooldown_secs = TELEGRAM_COOLDOWN_DEFAULT

    fingerprint = _normalize_fingerprint(message)
    now = datetime.now(timezone.utc).timestamp()

    # Load existing cooldowns
    cooldowns: dict = {}
    try:
        if TELEGRAM_COOLDOWN_FILE.exists():
            cooldowns = json.loads(TELEGRAM_COOLDOWN_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        cooldowns = {}

    # Check if this fingerprint is still in cooldown
    last_sent = cooldowns.get(fingerprint, 0)
    if now - last_sent < cooldown_secs:
        return False  # suppress

    # Record this send
    cooldowns[fingerprint] = now
    # Prune entries older than 24 hours to prevent file growth
    cutoff = now - 86400
    cooldowns = {k: v for k, v in cooldowns.items() if v > cutoff}
    try:
        TELEGRAM_COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TELEGRAM_COOLDOWN_FILE.write_text(json.dumps(cooldowns), encoding="utf-8")
    except OSError:
        pass  # non-fatal: cooldown state lost, message still sends

    return True  # allowed


def _is_self_referential_runaway(mpg_result: dict) -> bool:
    """Detect when max_plan_usage failure is caused by the heartbeat agent itself.

    When the heartbeat_agent is detected as the runaway caller, the heartbeat
    reporting UNHEALTHY and triggering the agent to act just makes things worse
    (more calls -> higher burn rate -> more UNHEALTHY). Break the loop by
    recognising this self-referential condition.
    """
    if mpg_result.get("ok", True):
        return False
    runaway_info = mpg_result.get("runaway", {})
    if not runaway_info.get("detected", False):
        return False
    reasons = runaway_info.get("reasons", [])
    return any("heartbeat_agent" in r for r in reasons)


def _ground_service_alert(message: str, checks: list | None) -> bool:
    """Validate service-down claims against actual structured check results.

    Returns True if the message should be sent (claim is grounded or non-service).
    Returns False if the message claims services are down but checks disagree.
    """
    if checks is None:
        return True  # no check data available, allow message

    lower = message.lower()
    service_down_phrases = ["service", "down", "dead", "stopped", "failed", "inactive"]
    # Only filter messages that appear to claim service problems
    if sum(1 for p in service_down_phrases if p in lower) < 2:
        return True  # not a service-down claim

    # Check actual service health from structured checks
    service_checks = [c for c in checks if c["name"].startswith("service:")]
    if not service_checks:
        return True  # no service check data, allow

    failed_services = [c for c in service_checks if not c["ok"]]
    if failed_services:
        return True  # services genuinely failed, allow the alert

    # LLM claims services are down but all service checks pass — suppress
    print(f"[heartbeat] SUPPRESSED false service-down alert (all {len(service_checks)} service checks pass)")

    # Overwrite the last log entry to break the feedback loop —
    # the LLM reads the last agent action and reinforces its hallucination
    try:
        log_path = LOGS_DIR / "heartbeat_agent.log"
        if log_path.exists():
            ts = datetime.now(timezone.utc).isoformat()
            log_path.write_text(f"[{ts}] HEARTBEAT_OK — false service-down alert suppressed\n")
    except Exception:
        pass

    return False


def send_telegram_alert(checks: list) -> None:
    """Send Telegram message listing failed checks. Only called when unhealthy."""
    failed = [c for c in checks if not c["ok"]]
    lines = ["Something needs attention:", ""]
    for c in failed:
        lines.append(f"- {c['name']}: {c['detail']}")
    _send_telegram("\n".join(lines))


def send_telegram_heartbeat(checks: list) -> None:
    """Send a compact heartbeat pulse to Telegram on every run."""
    all_ok = all(c["ok"] for c in checks)
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    fail_count = len([c for c in checks if not c["ok"]])

    if all_ok:
        text = f"All systems healthy at {now}."
    else:
        failed = [c for c in checks if not c["ok"]]
        lines = [f"Heads up — {fail_count} issue{'s' if fail_count != 1 else ''} at {now}:"]
        for c in failed:
            lines.append(f"- {c['name']}: {c['detail']}")
        text = "\n".join(lines)

    _send_telegram(text)


# --- Self-repair -------------------------------------------------------------


def inject_repair_task(checks: list) -> None:
    """For service failures, inject a self-repair task into TASKS/."""
    failed_services = [c for c in checks if c["name"].startswith("service:") and not c["ok"]]
    if not failed_services:
        return

    # Rate-limit: skip if a recent repair task is already in-progress
    existing = list(TASKS_DIR.glob("hb_*_self_repair.md*"))
    in_progress = [p for p in existing if p.name.endswith(".inprogress")]
    if in_progress:
        print(f"SKIP repair injection — already in-progress: {in_progress[0].name}")
        return

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    stem = f"hb_{ts}_self_repair"
    task_path = TASKS_DIR / f"{stem}.md"

    names = ", ".join(c["name"].replace("service:", "") for c in failed_services)
    content = (
        "# Heartbeat Self-Repair Task\n\n"
        "The following services were detected as unhealthy by the heartbeat:\n"
        f"{names}\n\n"
        "## Instructions\n"
        "1. Check `journalctl -u <service> -n 50` for each failed service.\n"
        "2. Attempt `sudo systemctl restart <service>` for each.\n"
        "3. Verify the restart succeeded with `systemctl is-active <service>`.\n"
        "4. Write results to OUTPUT.\n"
    )
    task_path.write_text(content)
    print(f"Injected repair task: {task_path}")


# --- LLM-driven proactive heartbeat ------------------------------------------

# Active hours (UTC) — only run LLM heartbeat during these hours
ACTIVE_HOURS_START = int(os.environ.get("HEARTBEAT_ACTIVE_START", "0"))
ACTIVE_HOURS_END = int(os.environ.get("HEARTBEAT_ACTIVE_END", "24"))

# Model for heartbeat reasoning
HEARTBEAT_MODEL = os.environ.get("HEARTBEAT_MODEL", "claude-opus-4-6")
HEARTBEAT_TIMEOUT = 90  # seconds

CHECKLIST_FILE = BASE / "HEARTBEAT_CHECKLIST.md"
HEARTBEAT_AGENT_LOG = LOGS_DIR / "heartbeat_agent.log"

# Autonomous research cycle configuration
RESEARCH_TIMEOUT = 14400  # 4 hours for deep research
RESEARCH_COOLDOWN_MINUTES = 55  # skip if ran less than 55 min ago
RESEARCH_COOLDOWN_FILE = STATE_DIR / "last_research_cycle.json"
RESEARCH_LOG = LOGS_DIR / "research_cycle.log"

# Planning cycle configuration — runs every 3rd active cycle
PLANNING_TIMEOUT = 14400  # 4 hours for planning
PLANNING_COOLDOWN_MINUTES = 170  # ~every 3 hours (timer fires every 30 min)
PLANNING_COOLDOWN_FILE = STATE_DIR / "last_planning_cycle.json"
PLANNING_LOG = LOGS_DIR / "planning_cycle.log"

# Heartbeat agent cooldown — prevent runaway loops (6-7+ calls per 30min window)
HEARTBEAT_AGENT_COOLDOWN_MINUTES = 10
HEARTBEAT_AGENT_COOLDOWN_FILE = STATE_DIR / "last_heartbeat_agent.json"

# Memory maintenance configuration — runs periodically
MEMORY_MAINTENANCE_COOLDOWN_MINUTES = 360  # every 6 hours
MEMORY_MAINTENANCE_COOLDOWN_FILE = STATE_DIR / "last_memory_maintenance.json"


def _heartbeat_agent_cooldown_ok() -> bool:
    """Check if enough time has passed since the last heartbeat agent run.

    Prevents runaway loops where the agent fires 6-7+ times in a single
    30-minute timer window.
    """
    if not HEARTBEAT_AGENT_COOLDOWN_FILE.exists():
        return True
    try:
        data = json.loads(HEARTBEAT_AGENT_COOLDOWN_FILE.read_text())
        last_run = data.get("last_run_utc", "")
        if not last_run:
            return True
        last_dt = datetime.fromisoformat(last_run)
        age_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
        return age_min >= HEARTBEAT_AGENT_COOLDOWN_MINUTES
    except Exception:
        return True


def _update_heartbeat_agent_cooldown(success: bool) -> None:
    """Record that the heartbeat agent ran."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "last_run_utc": datetime.now(timezone.utc).isoformat(),
        "success": success,
    }
    HEARTBEAT_AGENT_COOLDOWN_FILE.write_text(json.dumps(data, indent=2) + "\n")


def _run_heartbeat_agent(checks: list) -> None:
    """LLM-driven heartbeat: reads checklist + state, decides whether to act.

    Runs AFTER deterministic health checks. Only during active hours.
    Uses Haiku for cost efficiency.
    """
    current_hour = datetime.now(timezone.utc).hour
    if not (ACTIVE_HOURS_START <= current_hour < ACTIVE_HOURS_END):
        print(f"[heartbeat-agent] Outside active hours ({ACTIVE_HOURS_START}-{ACTIVE_HOURS_END} UTC), skipping")
        return

    if not CHECKLIST_FILE.exists():
        print("[heartbeat-agent] No HEARTBEAT_CHECKLIST.md, skipping")
        return

    # Cooldown gate: prevent runaway loops (the agent was firing 6-7+ times
    # per 30-min timer window, which is the very problem max_plan_guard is
    # designed to stop — but is_essential=True was bypassing it).
    if not _heartbeat_agent_cooldown_ok():
        print(f"[heartbeat-agent] Cooldown active — skipping (ran within last {HEARTBEAT_AGENT_COOLDOWN_MINUTES} min)")
        return

    checklist = CHECKLIST_FILE.read_text()
    system_state = _gather_extended_state(checks)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Count actual service failures for the prompt
    _svc_checks = [c for c in checks if c["name"].startswith("service:")]
    _svc_all_ok = all(c["ok"] for c in _svc_checks)
    _svc_status = (
        f"FACT: All {len(_svc_checks)} services are RUNNING (verified by systemctl). "
        f"Do NOT claim services are down — they are confirmed healthy."
        if _svc_all_ok
        else f"WARNING: {sum(1 for c in _svc_checks if not c['ok'])} service(s) are actually down."
    )

    prompt = (
        f"You are Nova, running a periodic heartbeat check.\n\n"
        f"Current time: {now}\n\n"
        f"## Your Checklist\n{checklist}\n\n"
        f"## Current System State\n{system_state}\n\n"
        f"## SERVICE HEALTH — GROUND TRUTH\n{_svc_status}\n\n"
        f"Review each checklist item against the system state. "
        f"Only flag things that genuinely need attention — no false alarms. "
        f"ABSOLUTE RULE: The SERVICE STATUS section at the top of the system state is "
        f"ground truth from systemctl. If it says ALL SERVICES RUNNING, you MUST NOT "
        f"report any service as down, dead, stopped, or failed. Any prior agent action "
        f"claiming services were down was a false alarm — ignore it completely. "
        f"If nothing needs attention, respond with exactly: HEARTBEAT_OK"
    )

    claude_bin = os.environ.get("CLAUDE_BIN", "/home/nova/.local/bin/claude")

    # Max-plan guard: check if heartbeat agent should run.
    # NOTE: is_essential=False so that PROTECTION and CRITICAL_LOCKDOWN modes
    # can actually block the heartbeat agent — it IS the runaway caller.
    if _mpg_allow is not None:
        allowed, reason = _mpg_allow("heartbeat_agent", is_essential=False)
        if not allowed:
            print(f"[heartbeat-agent] Blocked by max-plan guard: {reason}")
            return

    _hb_t0 = datetime.now(timezone.utc)
    # Record cooldown immediately so that crashes/timeouts still count
    # (prevents runaway retries from bypassing the cooldown gate).
    _update_heartbeat_agent_cooldown(success=True)

    try:
        child_env = os.environ.copy()
        child_env.pop("CLAUDECODE", None)
        child_env.pop("CLAUDE_CODE_ENTRYPOINT", None)

        result = subprocess.run(
            [claude_bin, "-p", "--model", HEARTBEAT_MODEL, "--dangerously-skip-permissions", prompt],
            capture_output=True,
            text=True,
            timeout=HEARTBEAT_TIMEOUT,
            cwd=str(BASE),
            env=child_env,
        )
        _hb_dur = (datetime.now(timezone.utc) - _hb_t0).total_seconds()
        response = result.stdout.strip()

        if _mpg_record is not None:
            _mpg_record(
                caller="heartbeat_agent",
                component="heartbeat._run_heartbeat_agent",
                model=HEARTBEAT_MODEL,
                success=result.returncode == 0,
                duration_secs=_hb_dur,
            )

        if not response:
            stderr_hint = result.stderr.strip()[:200] if result.stderr else ""
            _log_agent(
                f"EMPTY_RESPONSE — agent returned nothing"
                f" (exit={result.returncode}"
                f"{f', stderr={stderr_hint}' if stderr_hint else ''})"
            )
            return

        if "HEARTBEAT_OK" in response:
            _log_agent("HEARTBEAT_OK")
            print("[heartbeat-agent] All clear — HEARTBEAT_OK")
            return

        # Agent flagged something — parse and act
        _log_agent(f"ACTION: {response[:300]}")
        print(f"[heartbeat-agent] Action needed: {response[:200]}")
        _handle_agent_actions(response, checks=checks)

    except subprocess.TimeoutExpired:
        _log_agent("TIMEOUT")
        if _mpg_record is not None:
            _mpg_record(
                caller="heartbeat_agent",
                component="heartbeat._run_heartbeat_agent",
                model=HEARTBEAT_MODEL,
                success=False,
                duration_secs=(datetime.now(timezone.utc) - _hb_t0).total_seconds(),
            )
        print(f"[heartbeat-agent] Timed out after {HEARTBEAT_TIMEOUT}s")
    except FileNotFoundError:
        _log_agent(f"CLAUDE_NOT_FOUND: {claude_bin}")
        print(f"[heartbeat-agent] Claude binary not found: {claude_bin}")
    except Exception as e:
        _log_agent(f"ERROR: {e}")
        print(f"[heartbeat-agent] Error: {e}")


def _handle_agent_actions(response: str, checks: list | None = None) -> None:
    """Parse agent response and execute actions (notify or create task).

    Applies cooldown dedupe and grounding filter to prevent spam.
    """
    # Try to extract JSON actions from the response
    actions = _extract_json_actions(response)

    if actions:
        for action in actions:
            action_type = action.get("type", "")
            if action_type == "notify":
                msg = action.get("message", "")
                if msg:
                    full_msg = msg
                    if not _ground_service_alert(msg, checks):
                        continue
                    if _telegram_cooldown_gate(full_msg):
                        _send_telegram(full_msg)
                    else:
                        print(f"[heartbeat] Cooldown suppressed: {msg[:80]}")
            elif action_type == "task":
                title = action.get("title", "heartbeat_proactive")
                body = action.get("body", "")
                # Ground task injections too — suppress hallucinated service-down tasks
                task_text = f"{title} {body}"
                if not _ground_service_alert(task_text, checks):
                    print(f"[heartbeat] SUPPRESSED false service-down task: {title}")
                    continue
                _inject_proactive_task(title, body)
    else:
        # No structured JSON — treat the whole response as a notification
        full_msg = response[:500]
        if not _ground_service_alert(response, checks):
            return
        if _telegram_cooldown_gate(full_msg):
            _send_telegram(full_msg)
        else:
            print(f"[heartbeat] Cooldown suppressed: {response[:80]}")


def _extract_json_actions(text: str) -> list | None:
    """Extract JSON action list from Claude response text."""
    from utils.structured_output import _extract_json

    json_str = _extract_json(text)
    if json_str is None:
        return None
    try:
        data = json.loads(json_str)
        if isinstance(data, list) and all(isinstance(d, dict) for d in data):
            return data
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def _inject_proactive_task(title: str, body: str) -> None:
    """Create a proactive task file for the watcher to pick up."""
    # Rate-limit: max 4 pending proactive tasks (allows research pipeline to build up)
    existing = list(TASKS_DIR.glob("hb_proactive_*.md"))
    recent = [p for p in existing if not any(p.name.endswith(s) for s in (".done", ".failed", ".cancelled"))]
    if len(recent) >= 4:
        print("[heartbeat-agent] Rate limit: 4 proactive tasks already pending")
        return

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    import re

    slug = re.sub(r"[^a-zA-Z0-9]+", "_", title.strip()).strip("_").lower()[:50]
    stem = f"hb_proactive_{ts}_{slug}"
    task_path = TASKS_DIR / f"{stem}.md"
    content = f"# Heartbeat Proactive Task\n\n{body}" if body else f"# {title}"
    task_path.write_text(content)
    print(f"[heartbeat-agent] Injected proactive task: {task_path.name}")


def _log_agent(message: str) -> None:
    """Append to heartbeat agent log."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with open(HEARTBEAT_AGENT_LOG, "a") as f:
        f.write(f"[{ts}] {message}\n")


# --- Memory Maintenance Scheduler (Phase 8) -----------------------------------


def _memory_maintenance_cooldown_ok() -> bool:
    """Check if enough time has passed since the last memory maintenance run."""
    if not MEMORY_MAINTENANCE_COOLDOWN_FILE.exists():
        return True
    try:
        data = json.loads(MEMORY_MAINTENANCE_COOLDOWN_FILE.read_text())
        last_run = data.get("last_run_utc", "")
        if not last_run:
            return True
        last_dt = datetime.fromisoformat(last_run)
        age_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
        return age_min >= MEMORY_MAINTENANCE_COOLDOWN_MINUTES
    except Exception:
        return True


def _run_memory_maintenance() -> None:
    """Run periodic memory maintenance: governance sweeps, consolidation, compaction.

    Phase 8: Scheduled maintenance jobs. Runs every 6 hours via heartbeat timer.
    All operations are fail-open — maintenance failures never block heartbeat.

    Safety note: Governance and compaction run with dry_run=False (active mode).
    This is intentional — the module defaults are dry_run=True for API safety,
    but heartbeat explicitly opts into real maintenance. Actions taken:
    - Governance: marks stale loops (>14d), archives terminal loops (>30d)
    - Compaction: removes detected duplicates, cleans supersession chains
    """
    if not _memory_maintenance_cooldown_ok():
        print("[memory-maintenance] Cooldown not met, skipping")
        return

    print("[memory-maintenance] Starting scheduled maintenance (active mode, dry_run=False)...")
    results: dict[str, str] = {}

    # 1. Governance sweeps (retention, stale loop detection, protection enforcement)
    try:
        from agents.memory_governance import GovernanceEngine

        gov = GovernanceEngine()
        sweep = gov.run_sweep(dry_run=False)
        results["governance"] = f"ok ({sweep.items_acted_on} actions, {sweep.items_protected} protected)"
        print(f"[memory-maintenance] Governance: {sweep.items_acted_on} actions")
    except Exception as exc:
        results["governance"] = f"error: {exc}"
        print(f"[memory-maintenance] Governance failed: {exc}")

    # 2. Memory consolidation (window-based compression of working memory)
    try:
        from agents.memory_consolidator import WindowSpec, consolidate_window
        from agents.memory_router import WorkingMemoryAdapter

        adapter = WorkingMemoryAdapter()
        items = adapter.recall("", max_results=50)
        if items:
            spec = WindowSpec(
                window_type="working_memory",
                source_items=items,
            )
            consol = consolidate_window(spec)
            results["consolidation"] = (
                f"ok ({consol.dedupe_removed} deduped from {consol.input_count}, action={consol.action})"
            )
            print(f"[memory-maintenance] Consolidation: {consol.action}, {consol.dedupe_removed} deduped")
        else:
            results["consolidation"] = "ok (no items to consolidate)"
    except Exception as exc:
        results["consolidation"] = f"error: {exc}"
        print(f"[memory-maintenance] Consolidation failed: {exc}")

    # 3. Memory compaction (duplicate detection, supersession chain cleanup)
    try:
        from agents.memory_compactor import CompactionEngine

        compactor = CompactionEngine()
        compact = compactor.run_compaction(dry_run=False)
        results["compaction"] = f"ok ({compact.items_compacted} compacted, {compact.duplicates_found} dupes)"
        print(f"[memory-maintenance] Compaction: {compact.items_compacted} compacted")
    except Exception as exc:
        results["compaction"] = f"error: {exc}"
        print(f"[memory-maintenance] Compaction failed: {exc}")

    # Update cooldown
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_MAINTENANCE_COOLDOWN_FILE.write_text(
        json.dumps(
            {
                "last_run_utc": datetime.now(timezone.utc).isoformat(),
                "results": results,
            },
            indent=2,
        )
        + "\n"
    )

    print(f"[memory-maintenance] Complete: {results}")


# --- Autonomous Research Cycle -----------------------------------------------


def _research_cooldown_ok() -> bool:
    """Check if enough time has passed since the last research cycle."""
    if not RESEARCH_COOLDOWN_FILE.exists():
        return True
    try:
        data = json.loads(RESEARCH_COOLDOWN_FILE.read_text())
        last_run = data.get("last_run_utc", "")
        if not last_run:
            return True
        last_dt = datetime.fromisoformat(last_run)
        age_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
        return age_min >= RESEARCH_COOLDOWN_MINUTES
    except Exception:
        return True


def _update_research_cooldown(topic: str, success: bool) -> None:
    """Record that a research cycle ran."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "last_run_utc": datetime.now(timezone.utc).isoformat(),
        "topic": topic[:200],
        "success": success,
    }
    RESEARCH_COOLDOWN_FILE.write_text(json.dumps(data, indent=2) + "\n")


def _log_research(message: str) -> None:
    """Append to research cycle log."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with open(RESEARCH_LOG, "a") as f:
        f.write(f"[{ts}] {message}\n")


# --- Unified LLM Cycle Runner ------------------------------------------------


def _parse_cycle_outcome(response: str):
    """Parse a freeform cycle response into a structured CycleOutcome."""
    from utils.schemas.heartbeat import CycleOutcome, WriteStatus

    title = "unknown"
    for line in response.splitlines()[:30]:
        if line.startswith("#") and len(line) > 3:
            title = line.lstrip("# ").strip()[:200]
            break

    response_lower = response.lower()

    # Detect vault write status
    vault_status = WriteStatus.UNKNOWN
    if "vault_write" in response_lower:
        if any(kw in response_lower for kw in ("success", "accepted", "written")):
            vault_status = WriteStatus.SUCCESS
        elif any(kw in response_lower for kw in ("error", "rejected", "failed", "invalid")):
            vault_status = WriteStatus.FAILED

    # Detect memory write status
    memory_status = WriteStatus.UNKNOWN
    if "upsert_memory" in response_lower and any(kw in response_lower for kw in ("success", "stored")):
        memory_status = WriteStatus.SUCCESS

    return CycleOutcome(
        title=title,
        vault_write=vault_status,
        memory_write=memory_status,
    )


def _run_claude_cycle(
    *,
    cycle_name: str,
    prompt_builder,
    timeout: int,
    cooldown_check,
    cooldown_update,
    log_fn,
    emoji: str,
    label: str,
    mpg_caller: str,
    paused: bool = False,
) -> None:
    """Unified LLM cycle runner for research and planning.

    Parameterises the common pattern: active-hours gate -> cooldown check ->
    MPG gate -> subprocess.run(claude) -> response parsing -> Telegram notify.
    """
    tag = f"[{cycle_name}-cycle]"

    if paused:
        print(f"{tag} Paused, skipping")
        return

    current_hour = datetime.now(timezone.utc).hour
    if not (ACTIVE_HOURS_START <= current_hour < ACTIVE_HOURS_END):
        print(f"{tag} Outside active hours ({ACTIVE_HOURS_START}-{ACTIVE_HOURS_END} UTC), skipping")
        return

    if not cooldown_check():
        print(f"{tag} Cooldown active — skipping (ran recently)")
        return

    # Max-plan guard: check if cycle should run
    if _mpg_allow is not None:
        allowed, reason = _mpg_allow(mpg_caller)
        if not allowed:
            print(f"{tag} Blocked by max-plan guard: {reason}")
            return

    print(f"{tag} Starting autonomous {cycle_name} cycle...")
    prompt = prompt_builder()

    claude_bin = os.environ.get("CLAUDE_BIN", "/home/nova/.local/bin/claude")
    _cycle_t0 = datetime.now(timezone.utc)

    try:
        child_env = os.environ.copy()
        child_env.pop("CLAUDECODE", None)
        child_env.pop("CLAUDE_CODE_ENTRYPOINT", None)

        result = subprocess.run(
            [claude_bin, "-p", "--model", HEARTBEAT_MODEL, "--dangerously-skip-permissions", prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(BASE),
            env=child_env,
        )
        response = result.stdout.strip()
        _cycle_dur = (datetime.now(timezone.utc) - _cycle_t0).total_seconds()

        # Record completion in max-plan ledger (single entry per invocation)
        if _mpg_record is not None:
            _mpg_record(
                caller=mpg_caller,
                component=f"heartbeat._run_{cycle_name}_cycle",
                model=HEARTBEAT_MODEL,
                success=result.returncode == 0,
                duration_secs=_cycle_dur,
            )

        if not response:
            stderr_hint = result.stderr.strip()[:200] if result.stderr else ""
            log_fn(f"EMPTY_RESPONSE (exit={result.returncode}{f', stderr={stderr_hint}' if stderr_hint else ''})")
            cooldown_update("unknown", success=False)
            print(f"{tag} Empty response from Claude (exit={result.returncode})")
            return

        # Parse response into structured CycleOutcome
        outcome = _parse_cycle_outcome(response)
        from utils.schemas.heartbeat import WriteStatus

        title = outcome.title

        # Log persistence outcomes independently
        if outcome.vault_write == WriteStatus.SUCCESS:
            log_fn("VAULT_WRITE: success")
        elif outcome.vault_write == WriteStatus.FAILED:
            log_fn("VAULT_WRITE: FAILED — check vault audit log")
        else:
            log_fn("VAULT_WRITE: unknown (not detected in output)")

        if outcome.memory_write == WriteStatus.SUCCESS:
            log_fn("FUSION_MEMORY: success")
        else:
            log_fn("FUSION_MEMORY: unknown (not detected in output)")

        log_fn(f"COMPLETED: {title}")
        cooldown_update(title, success=True)
        print(f"{tag} Completed: {title}")

        # Build status summary for notification
        sinks = []
        if outcome.vault_write == WriteStatus.SUCCESS:
            sinks.append("vault \u2713")
        elif outcome.vault_write == WriteStatus.FAILED:
            sinks.append("vault \u2717")
        if outcome.memory_write == WriteStatus.SUCCESS:
            sinks.append("memory \u2713")
        sink_str = f" [{', '.join(sinks)}]" if sinks else ""
        _send_telegram(f"Finished {label.lower()}: {title}{sink_str}")

    except subprocess.TimeoutExpired:
        if _mpg_record is not None:
            _mpg_record(
                caller=mpg_caller,
                component=f"heartbeat._run_{cycle_name}_cycle",
                model=HEARTBEAT_MODEL,
                success=False,
                duration_secs=(datetime.now(timezone.utc) - _cycle_t0).total_seconds(),
            )
        log_fn(f"TIMEOUT after {timeout}s")
        cooldown_update("timeout", success=False)
        print(f"{tag} Timed out after {timeout}s")
    except FileNotFoundError:
        log_fn(f"CLAUDE_NOT_FOUND: {claude_bin}")
        print(f"{tag} Claude binary not found: {claude_bin}")
    except Exception as e:
        log_fn(f"ERROR: {e}")
        cooldown_update("error", success=False)
        print(f"{tag} Error: {e}")


def _run_research_cycle() -> None:
    """Autonomous research cycle (delegates to unified runner)."""
    _run_claude_cycle(
        cycle_name="research",
        prompt_builder=_build_research_prompt,
        timeout=RESEARCH_TIMEOUT,
        cooldown_check=_research_cooldown_ok,
        cooldown_update=_update_research_cooldown,
        log_fn=_log_research,
        emoji="\U0001f52c",
        label="Research Cycle",
        mpg_caller="research_cycle",
        paused=True,
    )


# --- Autonomous Planning Cycle -----------------------------------------------


def _planning_cooldown_ok() -> bool:
    """Check if enough time has passed since the last planning cycle."""
    if not PLANNING_COOLDOWN_FILE.exists():
        return True
    try:
        data = json.loads(PLANNING_COOLDOWN_FILE.read_text())
        last_run = data.get("last_run_utc", "")
        if not last_run:
            return True
        last_dt = datetime.fromisoformat(last_run)
        age_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
        return age_min >= PLANNING_COOLDOWN_MINUTES
    except Exception:
        return True


def _update_planning_cooldown(plan_title: str, success: bool) -> None:
    """Record that a planning cycle ran."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "last_run_utc": datetime.now(timezone.utc).isoformat(),
        "plan_title": plan_title[:200],
        "success": success,
    }
    PLANNING_COOLDOWN_FILE.write_text(json.dumps(data, indent=2) + "\n")


def _log_planning(message: str) -> None:
    """Append to planning cycle log."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with open(PLANNING_LOG, "a") as f:
        f.write(f"[{ts}] {message}\n")


def _run_planning_cycle() -> None:
    """Autonomous planning cycle (delegates to unified runner)."""
    _run_claude_cycle(
        cycle_name="planning",
        prompt_builder=_build_planning_prompt,
        timeout=PLANNING_TIMEOUT,
        cooldown_check=_planning_cooldown_ok,
        cooldown_update=_update_planning_cooldown,
        log_fn=_log_planning,
        emoji="\U0001f4cb",
        label="Planning Cycle",
        mpg_caller="planning_cycle",
    )


# --- Autonomy Cycle (Phase 2.4 + 3.4) ----------------------------------------

_NOVATRADE_GOAL_ID = "novatrade_100pct_operational"


def _has_pending_task_for_dimension(dimension: str | None, *, category: str | None = None) -> bool:
    """Check if TASKS/ already has a pending or recently-completed autonomy task targeting *dimension*.

    Prevents duplicate task storms by also considering tasks completed within
    the last ``_DEDUP_COOLDOWN_S`` seconds (default 2 hours).

    When *dimension* is None but *category* is provided (e.g. "validate"),
    dedup matches on category alone — prevents unbounded VALIDATE task spam.
    """
    _DEDUP_COOLDOWN_S = 7200  # 2 hours

    if not dimension and not category:
        return False
    import time as _time

    now = _time.time()
    dim_lower = dimension.replace("_", " ") if dimension else None

    for f in TASKS_DIR.glob("*.md*"):
        fname = f.name
        # Check if this is a completed/failed task within cooldown window
        is_terminal = any(fname.endswith(s) for s in (".done", ".failed", ".cancelled"))
        if is_terminal:
            try:
                mtime = f.stat().st_mtime
                if now - mtime > _DEDUP_COOLDOWN_S:
                    continue  # Too old — don't count
            except OSError:
                continue

        try:
            head = f.read_text()[:500]
            if "source: autonomy-decision-engine" not in head:
                continue
            # Match on dimension if provided
            if dim_lower and dim_lower in head.lower():
                return True
            # Match on category alone (e.g. "validate" with no dimension)
            if not dim_lower and category and f"category: {category}" in head:
                return True
        except OSError:
            continue
    return False


def _run_autonomy_cycle() -> dict | None:
    """Run the autonomy pipeline: score → decide → decompose → generate tasks.

    Uses a single asyncio.run() to orchestrate all async steps within one
    event loop, keeping asyncio.Lock semantics correct.

    Returns the progress snapshot dict for HEARTBEAT.md augmentation, or None
    if the autonomy package is unavailable.
    """
    if _ProgressScorer is None or _asyncio is None:
        return None

    async def _pipeline() -> dict:
        # Step 1: Score current system state
        scorer = _ProgressScorer()
        report = await scorer.score()
        print(f"[autonomy] Progress score: {report.overall_score}/100 ({report.overall_alert.value})")

        # Step 2: Assemble decision context
        assembler = _ContextAssembler()
        context = await assembler.assemble(report)

        # Step 3: Run decision engine
        engine = _DecisionEngine()
        decision = await engine.decide(context)
        print(f"[autonomy] Decision: {decision.mode.value} — {decision.reason[:100]}")

        # Step 3b: ReasoningEngine — LLM-powered gap analysis for novel situations
        # Called when rule-based engine returns MONITOR but something may be off,
        # or when multiple dimensions are degrading simultaneously.
        _reasoning_result = None
        if _ReasoningEngine is not None:
            try:
                reasoning = _ReasoningEngine()
                if reasoning.should_reason(decision, report):
                    _reasoning_result = await reasoning.reason(context, report)
                    if _reasoning_result is not None:
                        print(
                            f"[autonomy] Reasoning: {_reasoning_result.recommended_mode}"
                            f" (confidence={_reasoning_result.confidence})"
                        )
                        if _reasoning_result.novel_insight:
                            print(f"[autonomy]   Insight: {_reasoning_result.novel_insight[:120]}")
                        # If reasoning recommends a non-MONITOR action with high
                        # confidence, upgrade the decision so downstream steps act on it
                        if (
                            _reasoning_result.recommended_mode != "MONITOR"
                            and _reasoning_result.confidence in ("high", "medium")
                            and decision.mode == _ActionMode.MONITOR
                        ):
                            from novatrade.autonomy import Decision as _Decision

                            decision = _Decision(
                                mode=_ActionMode[_reasoning_result.recommended_mode],
                                reason=f"[reasoning] {_reasoning_result.reasoning_chain[0]}"
                                if _reasoning_result.reasoning_chain
                                else "[reasoning] LLM-identified gap",
                                target_dimension=_reasoning_result.target_dimension,
                            )
                            print(f"[autonomy] Decision upgraded by reasoning → {decision.mode.value}")
            except Exception as _re_exc:
                print(f"[autonomy] Reasoning engine failed (non-fatal): {_re_exc}")

        # Step 4: Bootstrap / update goal decomposition tree
        decomposer = _GoalDecomposer()
        tree = decomposer.load(_NOVATRADE_GOAL_ID)
        if tree is None:
            tree = _build_novatrade_tree()
            print("[autonomy] Bootstrapped NovaTrade goal tree (17 sub-goals)")
        tree = decomposer.update_progress(tree, report)
        decomposer.persist(tree)

        actionable = decomposer.get_actionable_subgoals(tree)
        completed = sum(1 for sg in tree.sub_goals if sg.status.value == "completed")
        print(f"[autonomy] Goal tree: {completed}/{len(tree.sub_goals)} complete, {len(actionable)} actionable")

        # Step 5: Direct action execution (inline, no task-file round-trip)
        # For critical decisions (REPAIR/EXECUTE), run diagnostics and
        # attempt safe remediation immediately, then alert via Telegram.
        _action_result = None
        if _DirectActionExecutor is not None and decision.mode != _ActionMode.MONITOR:
            try:
                executor = _DirectActionExecutor()
                _action_result = executor.execute(decision, report)
                _critical = [f for f in _action_result.findings if f.status == "critical"]
                if _critical:
                    print(f"[autonomy] Direct action: {_action_result.summary}")
                    for _f in _critical:
                        print(f"[autonomy]   CRITICAL: {_f.check} — {_f.detail[:80]}")
                if _action_result.actions_taken:
                    for _a in _action_result.actions_taken:
                        print(f"[autonomy]   ACTION: {_a[:100]}")
                if _action_result.escalated:
                    print("[autonomy]   ESCALATED — needs human attention")
            except Exception as _ae_exc:
                print(f"[autonomy] Direct action failed (non-fatal): {_ae_exc}")

        # Step 6: Generate task file for non-MONITOR decisions (for deeper follow-up)
        # Skip if a pending task already targets the same dimension (dedup)
        # For VALIDATE with no dimension, dedup on category to prevent spam
        if decision.mode != _ActionMode.MONITOR:
            _cat = decision.mode.value.lower()  # "validate", "repair", etc.
            if _has_pending_task_for_dimension(decision.target_dimension, category=_cat):
                print(f"[autonomy] Skipped task generation — pending task exists for {decision.target_dimension}")
            else:
                gen = _TaskSpecGenerator()
                spec = gen.from_decision(decision)
                task_path = gen.write_task_file(spec)
                if task_path:
                    print(f"[autonomy] Generated task: {task_path.name}")

        # Step 7: Build snapshot dict
        _snap: dict[str, object] = {
            "overall_score": report.overall_score,
            "alert_level": report.overall_alert.value,
            "dimensions": {
                name: {
                    "score": dim.score,
                    "trend": report.trends.get(name, None) and report.trends[name].direction,
                }
                for name, dim in report.dimensions.items()
            },
            "decision": {
                "mode": decision.mode.value,
                "reason": decision.reason,
                "target": decision.target_dimension,
            },
            "goal_tree": {
                "completed": completed,
                "total": len(tree.sub_goals),
                "actionable": [sg.title for sg in actionable[:5]],
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        # Include direct action results if any
        if _action_result is not None:
            _snap["direct_action"] = {
                "summary": _action_result.summary,
                "critical_findings": len([f for f in _action_result.findings if f.status == "critical"]),
                "actions_taken": _action_result.actions_taken,
                "alert_sent": _action_result.alert_sent,
                "escalated": _action_result.escalated,
            }
        # Include reasoning engine results if any
        if _reasoning_result is not None:
            _snap["reasoning"] = {
                "recommended_mode": _reasoning_result.recommended_mode,
                "target_dimension": _reasoning_result.target_dimension,
                "confidence": _reasoning_result.confidence,
                "novel_insight": _reasoning_result.novel_insight,
                "suggested_actions": _reasoning_result.suggested_actions[:3],
            }
        return _snap

    try:
        snapshot = _asyncio.run(_pipeline())

        # Atomic write of snapshot to STATE/
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        snap_path = STATE_DIR / "autonomy_snapshot.json"
        import tempfile as _snap_tf

        fd, tmp_path = _snap_tf.mkstemp(dir=str(STATE_DIR), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(snapshot, f, indent=2)
            os.replace(tmp_path, str(snap_path))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        return snapshot

    except Exception as exc:
        print(f"[autonomy] Cycle failed (non-fatal): {exc}")
        if _sh_record_error is not None:
            _sh_record_error("heartbeat.autonomy_cycle", exc)
        return None


# --- Scheduler health check --------------------------------------------------


def check_scheduler() -> dict:
    """Check that the dynamic scheduler is configured and operational."""
    if not _HAS_SCHEDULER:
        return {"name": "scheduler", "ok": True, "detail": "scheduler package not available (optional)"}
    try:
        config = _load_sched_config()
        status_parts = []
        status_parts.append(f"enabled={config.enabled}")
        status_parts.append(f"shadow={config.shadow_mode}")
        status_parts.append(f"variant={config.variant_selection}")

        # Check if execution log exists and has records
        exec_log = STATE_DIR / "scheduler" / "execution_log.jsonl"
        if exec_log.exists():
            try:
                records = _read_execution_log(exec_log)
                status_parts.append(f"exec_records={len(records)}")
                # Count real records (filter out synthetic/probe entries)
                real_records = [r for r in records if r.actual_duration_min >= 0.1]
                status_parts.append(f"real_records={len(real_records)}")
            except Exception:
                status_parts.append("exec_log=unreadable")
        else:
            status_parts.append("exec_log=none")

        # Check for today's BlockState
        from datetime import date as _date_type

        _today_block_id = f"shift_{_date_type.today():%Y%m%d}"
        _block_state_path = STATE_DIR / "scheduler" / f"{_today_block_id}_state.json"
        status_parts.append(f"block_state={'yes' if _block_state_path.exists() else 'no'}")

        return {"name": "scheduler", "ok": True, "detail": ", ".join(status_parts)}
    except Exception as e:
        return {"name": "scheduler", "ok": False, "detail": f"config load failed: {e}"}


# --- Main --------------------------------------------------------------------


def main() -> int:
    """Run all health checks, write HEARTBEAT.md, alert if unhealthy."""
    _t0 = time.monotonic()
    print(f"[heartbeat] Starting health check at {datetime.now(timezone.utc).isoformat()}")

    # Phase 6B.15: Informational sd_notify status for oneshot service
    if _sd_notify_status is not None:
        try:
            _sd_notify_status("Heartbeat cycle starting")
        except Exception:
            pass  # sd_notify is best-effort

    # Structured tracing for this heartbeat cycle
    hb_ctx = TraceContext.new("heartbeat") if TraceContext is not None else None
    if slog and hb_ctx:
        slog.event("heartbeat.started", hb_ctx)

    # --- Phase 1.2: Kill switch + dead man's switch ---
    try:
        from nova_kill_switch import MODE_RUN, MODE_STOPPED, check_kill_switch, heartbeat_alive

        heartbeat_alive()  # Refresh dead man's switch TTL (45 min)
        ks_mode = check_kill_switch()
        if ks_mode == MODE_STOPPED:
            print(f"[heartbeat] Kill switch ACTIVE (mode={ks_mode}) — skipping all work")
            return 0
        if ks_mode != MODE_RUN:
            print(f"[heartbeat] Kill switch mode={ks_mode} — running health checks only")
    except Exception as e:
        print(f"[heartbeat] Kill switch check failed (non-fatal): {e}")

    # --- Phase 6B: Degradation tier gating ---
    _degradation_tier = 0  # FULL by default
    if _sh_get_tier is not None:
        try:
            _deg_state = _sh_get_tier()
            _degradation_tier = int(_deg_state.tier)
            if _degradation_tier > 0:
                print(f"[heartbeat] Degradation tier: {_deg_state.tier.name} — {_deg_state.reason}")
        except Exception as e:
            print(f"[heartbeat] Degradation tier check failed (non-fatal): {e}")

    # --- Phase 6B: Budget enforcement at cycle start ---
    if _budget_enforcer is not None:
        try:
            _budget_ok, _budget_msg = _budget_enforcer.can_proceed(scope="daily")
            if not _budget_ok:
                print(f"[heartbeat] Budget exceeded: {_budget_msg} — downgrading to REDUCED")
                # Escalate to at least REDUCED if not already worse
                if _degradation_tier < 1:
                    _degradation_tier = 1  # REDUCED
                    # Persist so cycle runners calling get_degradation_tier() see the same value
                    if _sh_set_tier is not None and _DegradationTier is not None:
                        _sh_set_tier(_DegradationTier.REDUCED, reason=f"budget exceeded: {_budget_msg}")
            else:
                # Budget is OK - check if we need to clear a budget-triggered degradation
                if _sh_get_tier is not None and _sh_set_tier is not None and _DegradationTier is not None:
                    try:
                        current_state = _sh_get_tier()
                        if (current_state.tier == _DegradationTier.REDUCED and
                            current_state.reason and "budget exceeded" in current_state.reason.lower()):
                            print("[heartbeat] Budget now OK — clearing budget-triggered REDUCED tier")
                            _sh_set_tier(_DegradationTier.FULL, reason="budget constraints resolved")
                            _degradation_tier = 0  # Update local variable too
                    except Exception as clear_err:
                        print(f"[heartbeat] Failed to clear budget degradation (non-fatal): {clear_err}")
        except Exception as e:
            print(f"[heartbeat] Budget check failed (non-fatal): {e}")

    checks = []

    for svc in SERVICES:
        checks.append(check_service(svc))

    checks.append(check_disk())
    checks.append(check_claude_binary())
    checks.append(check_task_queue())
    checks.append(check_last_output())
    checks.append(check_stale_workers())
    checks.append(check_state_files())
    checks.append(check_metrics())
    checks.append(check_google_workspace())
    checks.append(check_backup())
    checks.append(check_ruff())
    checks.append(check_memory_systems())
    checks.append(check_log_sizes())
    checks.append(check_state_bloat())
    checks.append(check_pip_audit())
    checks.append(check_llm_cache())
    checks.append(check_cost_router())
    checks.append(check_scheduler())

    # --- Self-healing runtime (Phase 6A) ---
    try:
        from utils.self_healing import check_self_healing, touch_dead_man_switch

        touch_dead_man_switch(component="heartbeat")
        sh_result = check_self_healing()
        checks.append(sh_result)
        # Send Telegram alert for self-healing issues
        sh_alerts = sh_result.get("alerts", [])
        for alert in sh_alerts:
            if alert.get("severity") in ("warning", "critical"):
                alert_msg = f"Self-healing ({alert['severity']}): {alert['title']}\n{alert['detail']}"
                if _telegram_cooldown_gate(alert_msg):
                    _send_telegram(alert_msg)
    except Exception as e:
        checks.append({"name": "self_healing", "ok": True, "detail": f"check skipped: {e}"})

    # --- Max-plan usage monitoring ---
    try:
        from utils.max_plan_guard import check_max_plan_usage

        mpg_result = check_max_plan_usage()

        # Break self-referential runaway loop: when the heartbeat_agent itself
        # is detected as the runaway caller, marking UNHEALTHY triggers the
        # agent to act, which increases the burn rate, which keeps it UNHEALTHY.
        # Downgrade to ok=True with a warning so the loop can cool down.
        if _is_self_referential_runaway(mpg_result):
            mpg_result["ok"] = True
            mpg_result["detail"] = (
                f"self-referential runaway (heartbeat_agent) — "
                f"suppressed to break feedback loop, {mpg_result['detail']}"
            )
            print("[heartbeat] max_plan_usage: self-referential runaway detected, suppressing UNHEALTHY to break loop")

        checks.append(mpg_result)
        # Send Telegram alert for critical max-plan issues
        mpg_alerts = mpg_result.get("alerts", [])
        for alert in mpg_alerts:
            if alert.get("severity") in ("warning", "critical"):
                alert_msg = f"Budget alert ({alert['severity']}): {alert['title']}\n{alert['detail']}"
                if _telegram_cooldown_gate(alert_msg):
                    _send_telegram(alert_msg)
    except Exception as e:
        checks.append({"name": "max_plan_usage", "ok": True, "detail": f"check skipped: {e}"})

    # --- Drift detection (observability Phase 3) ---
    try:
        from utils.drift_detector import detect_drift

        drift = detect_drift(window_hours=24.0)
        if drift.drift_detected:
            signals_str = "; ".join(s.message for s in drift.signals[:3])
            checks.append(
                {
                    "name": "drift_detection",
                    "ok": False,
                    "detail": f"{len(drift.signals)} signal(s): {signals_str}",
                }
            )
        else:
            checks.append(
                {
                    "name": "drift_detection",
                    "ok": True,
                    "detail": f"no drift ({drift.window_tasks} tasks in window)",
                }
            )
    except Exception as e:
        checks.append({"name": "drift_detection", "ok": True, "detail": f"check skipped: {e}"})

    # --- Service staleness detection + auto-restart attempt ---
    try:
        from utils.service_staleness import check_all_staleness, restart_service

        for sr in check_all_staleness():
            if sr.is_stale:
                # Attempt inline restart (works only if not under NoNewPrivileges)
                rr = restart_service(sr.service)
                if rr.success:
                    checks.append(
                        {
                            "name": f"staleness:{sr.service}",
                            "ok": True,
                            "detail": f"was stale ({sr.commits_since_start} commits) — auto-restarted",
                        }
                    )
                    stale_msg = (
                        f"Auto-restarted {sr.service} — was running stale code "
                        f"({sr.commits_since_start} commit(s), {sr.stale_hours:.1f}h behind)."
                    )
                else:
                    # Restart failed (likely NoNewPrivileges) — alert only,
                    # the novacore-auto-deploy timer will handle it.
                    checks.append(
                        {
                            "name": f"staleness:{sr.service}",
                            "ok": False,
                            "detail": f"STALE ({sr.commits_since_start} commits, {sr.stale_hours:.1f}h) — "
                            f"inline restart failed, awaiting auto-deploy timer",
                        }
                    )
                    stale_msg = (
                        f"Service {sr.service} is running stale code — "
                        f"{sr.commits_since_start} commit(s) since start "
                        f"({sr.stale_hours:.1f}h ago). Auto-deploy timer will restart."
                    )
                if _telegram_cooldown_gate(stale_msg):
                    _send_telegram(stale_msg)
            else:
                checks.append(
                    {
                        "name": f"staleness:{sr.service}",
                        "ok": True,
                        "detail": sr.detail,
                    }
                )
    except Exception as e:
        checks.append({"name": "service_staleness", "ok": True, "detail": f"check skipped: {e}"})

    # --- NovaTrade signal monitoring ---
    try:
        signal_check = check_novatrade_signals()
        checks.append(signal_check)
    except Exception as e:
        checks.append({"name": "novatrade_signals", "ok": True, "detail": f"check skipped: {e}"})

    # --- Autonomy cycle (Phase 2.4 + 3.4): score → decide → decompose → tasks ---
    # Gated on degradation tier: skip at MINIMAL+ (tier >= 2), matching planning cycle policy
    _autonomy_snapshot = None
    _skip_autonomy = _heartbeat_shutdown_requested or _degradation_tier >= 2
    if _ProgressScorer is not None and not _skip_autonomy:
        _autonomy_snapshot = _run_autonomy_cycle()
    elif _skip_autonomy and _ProgressScorer is not None:
        _skip_reason = (
            "shutdown requested"
            if _heartbeat_shutdown_requested
            else f"degradation tier >= MINIMAL ({_degradation_tier})"
        )
        print(f"[autonomy] Skipped ({_skip_reason})")

    # --- CRUISE mode: reduce LLM cycle frequency during stable GREEN periods ---
    _cruise_active = False
    if _update_cruise_state is not None and _autonomy_snapshot is not None:
        try:
            _cruise_state = _update_cruise_state(
                score=_autonomy_snapshot.get("overall_score", 0.0),
                alert_level=_autonomy_snapshot.get("alert_level", ""),
                decision_mode=_autonomy_snapshot.get("decision", {}).get("mode", ""),
            )
            _cruise_active = _cruise_state.active
            if _cruise_active:
                print(
                    f"[cruise] ACTIVE — cycle {_cruise_state.cruise_cycles}/"
                    f"{_cruise_state.consecutive_stable} stable "
                    f"(skipping LLM agent, research, planning)"
                )
        except Exception as _cruise_exc:
            print(f"[cruise] State update failed (non-fatal): {_cruise_exc}")

    write_heartbeat(checks, autonomy_snapshot=_autonomy_snapshot)

    # Record metrics snapshot
    all_ok = all(c["ok"] for c in checks)
    fail_names = [c["name"] for c in checks if not c["ok"]]
    snapshot = HeartbeatSnapshot(
        ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        epoch=int(datetime.now(timezone.utc).timestamp()),
        status="healthy" if all_ok else "unhealthy",
        total_checks=len(checks),
        passed=len(checks) - len(fail_names),
        failed=len(fail_names),
        failed_names=fail_names,
        duration_ms=round((time.monotonic() - _t0) * 1000, 1),
        checks={c["name"]: {"ok": c["ok"], "detail": c["detail"]} for c in checks},
    )
    _append_metrics(snapshot)

    # --- Scheduler: create interrupt events for critical failures ---
    if _HAS_SCHEDULER and not all_ok:
        try:
            _sched_state_dir = STATE_DIR / "scheduler"
            _sched_state_dir.mkdir(parents=True, exist_ok=True)
            _int_state = _load_interrupt_state(_sched_state_dir / "interrupt_state.json")

            for _chk in checks:
                if not _chk["ok"]:
                    _level = _classify_interrupt(
                        f"health_check_failed:{_chk['name']}",
                        policy=None,  # use default policy
                    )
                    _evt = _InterruptEvent(
                        source="heartbeat",
                        event_type=f"health_check_failed:{_chk['name']}",
                        level=_level,
                        title=f"Health check failed: {_chk['name']}",
                        detail=_chk.get("detail", "")[:200],
                    )
                    _int_state.interrupt_log.append(_evt)

            _save_interrupt_state(_int_state, _sched_state_dir / "interrupt_state.json")
            print(f"[scheduler] Created {len(fail_names)} interrupt event(s) for failed checks")
        except Exception as _sched_exc:
            print(f"[scheduler] Interrupt event creation failed (non-fatal): {_sched_exc}")

    # --- Phase 7.6: multi-agent heartbeat ---
    try:
        from agents.observability import Severity, run_multiagent_heartbeat

        ma_report = run_multiagent_heartbeat()
        ma_ok = ma_report.overall == Severity.HEALTHY
        checks.append(
            {
                "name": "multi_agent_health",
                "ok": ma_ok,
                "detail": (
                    f"{ma_report.overall}: "
                    f"{len(ma_report.findings)} finding(s), "
                    f"{ma_report.metrics.active_workflows} active workflow(s)"
                ),
            }
        )
        if not ma_ok:
            all_ok = False
        print(f"[heartbeat] Multi-agent: {ma_report.overall} ({len(ma_report.findings)} findings)")
    except Exception as e:
        print(f"[heartbeat] Multi-agent check failed (non-fatal): {e}")
        checks.append(
            {
                "name": "multi_agent_health",
                "ok": True,
                "detail": f"check skipped: {e}",
            }
        )

    # --- Post-Heartbeat Autonomous Report → NovaVault diary ---
    # Runs after EVERY heartbeat to provide visibility into goal-driven decisions
    try:
        from scripts.autonomous_report import run_post_heartbeat_report

        if not _heartbeat_shutdown_requested:
            print("[autonomous-report] Generating post-heartbeat report")
            _report_rc = run_post_heartbeat_report(_autonomy_snapshot)
            if _report_rc == 0:
                print("[autonomous-report] Report written to NovaVault diary")
            else:
                print(f"[autonomous-report] Report generation failed (rc={_report_rc})")
    except Exception as e:
        print(f"[autonomous-report] Failed (non-fatal): {e}")

    # --- Scheduler: daily calibration report ---
    if _HAS_SCHEDULER and not _heartbeat_shutdown_requested:
        try:
            _sched_state_dir = STATE_DIR / "scheduler"
            _exec_log_path = STATE_DIR / "scheduler" / "execution_log.jsonl"
            _cal_report_marker = _sched_state_dir / "last_calibration_report.txt"

            # Run calibration once per day
            _should_calibrate = True
            if _cal_report_marker.exists():
                try:
                    _last_cal = datetime.fromisoformat(_cal_report_marker.read_text().strip())
                    _should_calibrate = (datetime.now(timezone.utc) - _last_cal).total_seconds() > 86400
                except (ValueError, OSError):
                    pass

            if _should_calibrate and _exec_log_path.exists():
                _records = _read_execution_log(_exec_log_path)
                # Filter out synthetic/probe records (real tasks take >0.1 min)
                _records = [r for r in _records if r.actual_duration_min >= 0.1]
                if len(_records) >= 5:
                    _cal_table = _compute_calibration(_records)
                    _cal_report = _generate_calibration_report(_cal_table, _records)
                    # Write report to OUTPUT/
                    _cal_stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                    _cal_path = OUTPUT_DIR / f"calibration_report__{_cal_stamp}.md"
                    _cal_path.write_text(_cal_report, encoding="utf-8")

                    # Update marker
                    _sched_state_dir.mkdir(parents=True, exist_ok=True)
                    _cal_report_marker.write_text(
                        datetime.now(timezone.utc).isoformat(),
                        encoding="utf-8",
                    )
                    print(f"[scheduler] Calibration report written: {_cal_path.name}")
        except Exception as _cal_exc:
            print(f"[scheduler] Calibration report failed (non-fatal): {_cal_exc}")

    # --- Phase 7.7: production hardening maintenance ---
    try:
        from agents.production_hardening import run_production_hardening

        ph_result = run_production_hardening()
        cleanup = ph_result.get("cleanup", {})
        if isinstance(cleanup, dict):
            archived = len(cleanup.get("archived_workflows", [])) + len(cleanup.get("archived_agents", []))
            cleaned = len(cleanup.get("cleaned_leases", [])) + len(cleanup.get("cleaned_tmp", []))
            if archived or cleaned:
                print(f"[heartbeat] Hardening: archived={archived} cleaned={cleaned}")
    except Exception as e:
        print(f"[heartbeat] Production hardening failed (non-fatal): {e}")

    # --- Skill Evolution: process queue + health scan ---
    try:
        from skills.evolution_audit import EvolutionAudit
        from skills.evolution_processor import EvolutionProcessor
        from skills.evolution_queue import EvolutionQueue
        from skills.skill_cache import SkillCache
        from skills.skill_dashboard import SkillDashboard
        from skills.skill_evolver import SkillEvolver
        from skills.version_store import SkillVersionStore

        _evo_store = SkillVersionStore()
        _evo_queue = EvolutionQueue()
        _evo_evolver = SkillEvolver(version_store=_evo_store)
        _evo_dashboard = SkillDashboard(
            version_store=_evo_store,
            skill_cache=SkillCache(),
            evolution_queue=_evo_queue,
            evolution_audit=EvolutionAudit(),
        )
        _evo_processor = EvolutionProcessor(
            version_store=_evo_store,
            evolution_queue=_evo_queue,
            skill_evolver=_evo_evolver,
            dashboard=_evo_dashboard,
            telegram_fn=_send_telegram,
        )

        # Process queued evolutions (up to 3 per heartbeat)
        _evo_results = _evo_processor.process_batch(max_items=3)

        # Run periodic health scan (detect improvement/capture candidates)
        _evo_scan = _evo_processor.run_health_scan()

        _evo_stats = _evo_processor.get_stats()
        _evo_detail = (
            f"processed={len(_evo_results)} "
            f"candidates={_evo_scan.get('candidates_found', 0)} "
            f"patterns={_evo_scan.get('patterns_found', 0)} "
            f"queue={_evo_stats.get('queue_size', 0)}"
        )
        checks.append({"name": "skill_evolution", "ok": True, "detail": _evo_detail})
        print(f"[skill-evolution] {_evo_detail}")
    except Exception as e:
        print(f"[skill-evolution] Failed (non-fatal): {e}")
        checks.append({"name": "skill_evolution", "ok": True, "detail": f"check skipped: {e}"})

    # Always send heartbeat pulse to Telegram
    send_telegram_heartbeat(checks)

    # --- Self-Heal: Route warnings to investigation queue ---
    try:
        from agents.self_heal_investigator import investigate_all_pending
        from utils.warning_router import (
            collect_from_budget,
            collect_from_drift,
            collect_from_error_summary,
            collect_from_heartbeat,
            collect_from_memory,
            collect_from_runaway,
        )

        # Collect warnings from all subsystems
        _warnings_queued = 0
        _warnings_queued += collect_from_heartbeat(checks)

        if _budget_enforcer is not None:
            _warnings_queued += collect_from_budget(_budget_enforcer.check_alerts())

        try:
            from utils.self_healing import get_error_summary, get_memory_snapshot

            _warnings_queued += collect_from_error_summary(get_error_summary(minutes=30))
            _mem_snap = get_memory_snapshot()
            _warnings_queued += collect_from_memory(
                {"rss_mb": _mem_snap.rss_mb, "warning": _mem_snap.warning, "critical": _mem_snap.critical}
            )
        except Exception:
            pass

        try:
            from dataclasses import asdict as _wr_asdict

            from utils.drift_detector import detect_drift as _wr_detect_drift
            from utils.max_plan_guard import detect_runaway as _wr_detect_runaway

            _wr_drift = _wr_detect_drift(window_hours=24.0)
            if _wr_drift.drift_detected:
                _warnings_queued += collect_from_drift(
                    {"drift_detected": True, "signals": [_wr_asdict(s) for s in _wr_drift.signals]}
                )

            _wr_runaway = _wr_detect_runaway()
            if _wr_runaway.detected:
                _warnings_queued += collect_from_runaway(_wr_asdict(_wr_runaway))
        except Exception:
            pass

        # Run investigation on all pending warnings
        if _warnings_queued > 0 or not all_ok:
            _inv_results = investigate_all_pending()
            _auto_fixed = sum(1 for r in _inv_results if r.action_taken == "auto_fixed" and r.fix_successful)
            _escalated = sum(1 for r in _inv_results if r.escalated)
            if _inv_results:
                print(
                    f"[self-heal] Investigated {len(_inv_results)} warnings: "
                    f"{_auto_fixed} auto-fixed, {_escalated} escalated"
                )
    except Exception as e:
        print(f"[self-heal] Warning investigation failed (non-fatal): {e}")

    fail_names = [c["name"] for c in checks if not c["ok"]]
    if all_ok:
        print("[heartbeat] All checks passed. HEALTHY.")
        if slog and hb_ctx:
            slog.event("heartbeat.healthy", hb_ctx, checks=len(checks), duration_ms=hb_ctx.elapsed_ms())
    else:
        print("[heartbeat] Some checks FAILED. Alerting...")
        inject_repair_task(checks)
        if slog and hb_ctx:
            slog.event(
                "heartbeat.unhealthy",
                hb_ctx,
                level="warn",
                checks=len(checks),
                failed=fail_names,
                duration_ms=hb_ctx.elapsed_ms(),
            )

    # --- Phase 3: Automatic memory trigger for heartbeat cycle ---
    try:
        from agents.memory_triggers import trigger_engine

        hb_summary = f"Heartbeat: {len(checks)} checks, {len(fail_names)} failed"
        if fail_names:
            hb_summary += f" ({', '.join(fail_names[:5])})"
        trigger_engine.fire(
            trigger_class="session_boundary",
            event_type="heartbeat_cycle",
            source="heartbeat",
            title=f"heartbeat_cycle: {'HEALTHY' if all_ok else 'UNHEALTHY'}"[:100],
            summary=hb_summary[:500],
            caller="heartbeat.main",
            ctx=hb_ctx,
            confidence="high" if all_ok else "medium",
            tags=["#heartbeat"] + ([f"#failed/{f}" for f in fail_names[:3]] if fail_names else []),
        )
    except Exception as exc:
        print(f"[heartbeat] Memory trigger failed (non-fatal): {exc}")

    # --- Phase 6B.13: Check shutdown flag before expensive operations ---
    if _heartbeat_shutdown_requested:
        print("[heartbeat] Shutdown requested — skipping LLM agent, maintenance, and autonomous cycles")
    else:
        # --- LLM-driven proactive heartbeat ---
        # Phase 6B: Skip LLM-driven agent in EMERGENCY (tier 3)
        # v87 P3: Skip in CRUISE mode (system stable, no action needed)
        if _degradation_tier >= 3:
            print("[heartbeat-agent] Skipped (EMERGENCY degradation tier — no LLM calls)")
        elif _cruise_active:
            print("[heartbeat-agent] Skipped (CRUISE mode — system stable)")
        else:
            try:
                _run_heartbeat_agent(checks)
            except Exception as e:
                print(f"[heartbeat-agent] Failed (non-fatal): {e}")
                if _sh_record_error is not None:
                    _sh_record_error("heartbeat.heartbeat_agent", e)

        # --- Memory maintenance scheduler (Phase 8) ---
        # Phase 6B: Skip memory maintenance in MINIMAL+ (tier >= 2)
        if _heartbeat_shutdown_requested:
            print("[memory-maintenance] Skipped (shutdown requested)")
        elif _degradation_tier >= 2:
            print("[memory-maintenance] Skipped (degradation tier >= MINIMAL)")
        else:
            try:
                _run_memory_maintenance()
            except Exception as e:
                print(f"[memory-maintenance] Failed (non-fatal): {e}")

        # --- Autonomous cycles: research (hourly) + planning (every 3 hours) ---
        # Phase 4.4: Gate expensive cycles on adaptive heartbeat config (budget-aware).
        # Phase 6B: Gate on degradation tier (research=optional, planning=optional).
        # v87 P3: Skip both in CRUISE mode (system stable, save LLM calls).
        _skip_research = _degradation_tier >= 1 or _heartbeat_shutdown_requested or _cruise_active
        _skip_planning = _degradation_tier >= 2 or _heartbeat_shutdown_requested or _cruise_active
        try:
            from utils.cost_router import read_heartbeat_config

            _hb_cfg = read_heartbeat_config()
            if _hb_cfg:
                _skip_research = _skip_research or _hb_cfg.skip_research
                _skip_planning = _skip_planning or _hb_cfg.skip_planning
                if _hb_cfg.skip_research or _hb_cfg.skip_planning:
                    print(
                        f"[cost-router] Adaptive config: mode={_hb_cfg.mode} "
                        f"skip_research={_hb_cfg.skip_research} skip_planning={_hb_cfg.skip_planning} "
                        f"reason={_hb_cfg.reason}"
                    )
        except Exception:
            pass  # cost router unavailable — run everything

        if _skip_research:
            _skip_reason = (
                "shutdown requested"
                if _heartbeat_shutdown_requested
                else "degradation tier >= REDUCED"
                if _degradation_tier >= 1
                else "CRUISE mode"
                if _cruise_active
                else "budget-constrained mode"
            )
            print(f"[research-cycle] Skipped ({_skip_reason})")
        else:
            try:
                _run_research_cycle()
            except Exception as e:
                print(f"[research-cycle] Failed (non-fatal): {e}")
                if _sh_record_error is not None:
                    _sh_record_error("heartbeat.research_cycle", e)

        if _skip_planning:
            _skip_reason = (
                "shutdown requested"
                if _heartbeat_shutdown_requested
                else "degradation tier >= MINIMAL"
                if _degradation_tier >= 2
                else "CRUISE mode"
                if _cruise_active
                else "budget-constrained mode"
            )
            print(f"[planning-cycle] Skipped ({_skip_reason})")
        else:
            try:
                _run_planning_cycle()
            except Exception as e:
                print(f"[planning-cycle] Failed (non-fatal): {e}")
                if _sh_record_error is not None:
                    _sh_record_error("heartbeat.planning_cycle", e)

    # --- Phase 6B: Record memory snapshot for RSS trend tracking ---
    if _sh_record_mem is not None:
        try:
            _sh_record_mem()
        except Exception as e:
            print(f"[heartbeat] Memory snapshot recording failed (non-fatal): {e}")

    # Append to heartbeat log
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fail_count = len([c for c in checks if not c["ok"]])
    log_line = (
        f"{datetime.now(timezone.utc).isoformat()} | {'HEALTHY' if all_ok else 'UNHEALTHY'} | {fail_count} failures\n"
    )
    with open(LOGS_DIR / "heartbeat.log", "a") as f:
        f.write(log_line)

    # Phase 6B.15: Informational sd_notify status for oneshot service
    if _sd_notify_status is not None:
        try:
            _elapsed = time.monotonic() - _t0
            _sd_notify_status(f"Heartbeat cycle complete ({'HEALTHY' if all_ok else 'UNHEALTHY'}, {_elapsed:.1f}s)")
        except Exception:
            pass  # sd_notify is best-effort

    # Always exit 0 — the heartbeat process itself succeeded even if checks found issues.
    # Exiting 1 causes systemd to show "failed" for this oneshot service, which
    # confuses the heartbeat LLM agent into hallucinating "services are dead".
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
