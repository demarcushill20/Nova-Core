#!/usr/bin/env python3
"""NovaCore heartbeat — proactive health monitoring.

Runs as a systemd oneshot (triggered by novacore-heartbeat.timer every 30min).
Checks service health, disk, task queue, and worker liveness.
Writes HEARTBEAT.md, alerts via Telegram on failure, optionally injects repair tasks.

Stdlib only — no pip installs required.
"""

import json
import os
import signal
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
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
    _sd_notify_status = None  # type: ignore[assignment]

try:
    from utils.structured_log import slog
    from utils.trace_context import TraceContext
except ImportError:
    slog = None  # type: ignore[assignment]
    TraceContext = None  # type: ignore[assignment,misc]

try:
    from utils.max_plan_guard import record_invocation as _mpg_record
    from utils.max_plan_guard import should_allow_task as _mpg_allow
except ImportError:
    _mpg_record = None  # type: ignore[assignment]
    _mpg_allow = None  # type: ignore[assignment]

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
    _sh_get_tier = None  # type: ignore[assignment]
    _sh_record_error = None  # type: ignore[assignment]
    _sh_record_mem = None  # type: ignore[assignment]
    _sh_set_tier = None  # type: ignore[assignment]

try:
    from agents.budget_enforcer import budget as _budget_enforcer
except ImportError:
    _budget_enforcer = None  # type: ignore[assignment]

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


def check_task_queue() -> dict:
    """Check for pending tasks and orphaned .inprogress files."""
    if not TASKS_DIR.exists():
        return {"name": "task_queue", "ok": True, "detail": "no TASKS dir"}

    lifecycle_suffixes = (".inprogress", ".done", ".failed", ".cancelled")
    pending = [p for p in TASKS_DIR.glob("*.md") if not any(p.name.endswith(s) for s in lifecycle_suffixes)]

    inprogress = list(TASKS_DIR.glob("*.inprogress"))
    now = datetime.now(timezone.utc)
    orphaned = []
    for ip in inprogress:
        age_min = (now - datetime.fromtimestamp(ip.stat().st_mtime, tz=timezone.utc)).total_seconds() / 60
        if age_min > ORPHAN_INPROGRESS_MINUTES:
            orphaned.append(ip.name)

    ok = len(pending) <= MAX_PENDING_TASKS and len(orphaned) == 0
    detail = f"{len(pending)} pending, {len(inprogress)} in-progress"
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
    """Check connectivity to Fusion Memory and Obsidian Vault."""
    issues = []

    # Fusion Memory: check if the MCP server entry point exists
    fusion_server = Path.home() / "Nova_AI_Fusion_Memory_MCP" / "mcp_server.py"
    if not fusion_server.exists():
        issues.append("Fusion Memory mcp_server.py missing")

    # Obsidian Vault: check if vault directory exists and has content
    vault_dir = Path("/home/nova/nova-vault")
    if not vault_dir.exists():
        issues.append("Obsidian vault dir missing")
    else:
        meta_dir = vault_dir / "_meta"
        if not meta_dir.exists() or not list(meta_dir.glob("*.md")):
            issues.append("Obsidian vault _meta/ empty or missing")

    ok = len(issues) == 0
    detail = "both reachable" if ok else "; ".join(issues)
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


# --- Output ------------------------------------------------------------------


def write_heartbeat(checks: list) -> None:
    """Write HEARTBEAT.md with timestamped checklist."""
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
    """Strip volatile parts (timestamps, exact dollar amounts) to produce a stable fingerprint."""
    import hashlib
    import re

    # Remove timestamps like 12:34 UTC, 2026-03-17T08:41:37Z
    normalized = re.sub(r"\d{1,2}:\d{2}(?:\s*UTC)?", "", text)
    normalized = re.sub(r"\d{4}-\d{2}-\d{2}T[\d:]+Z?", "", normalized)
    # Collapse dollar amounts to just the integer part (so $22.50 and $22.51 match)
    normalized = re.sub(r"\$(\d+)\.\d+", r"$\1", normalized)
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
    return False


def send_telegram_alert(checks: list) -> None:
    """Send Telegram message listing failed checks. Only called when unhealthy."""
    failed = [c for c in checks if not c["ok"]]
    lines = ["⚠️ NovaCore Heartbeat — UNHEALTHY", ""]
    for c in failed:
        lines.append(f"❌ {c['name']}: {c['detail']}")
    _send_telegram("\n".join(lines))


def send_telegram_heartbeat(checks: list) -> None:
    """Send a compact heartbeat pulse to Telegram on every run."""
    all_ok = all(c["ok"] for c in checks)
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    fail_count = len([c for c in checks if not c["ok"]])

    if all_ok:
        text = f"💚 Heartbeat {now} — HEALTHY ({len(checks)}/{len(checks)} checks passed)"
    else:
        failed = [c for c in checks if not c["ok"]]
        lines = [f"🔴 Heartbeat {now} — UNHEALTHY ({fail_count} failed)"]
        for c in failed:
            lines.append(f"  ❌ {c['name']}: {c['detail']}")
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

    prompt = (
        f"You are Nova, running a periodic heartbeat check.\n\n"
        f"Current time: {now}\n\n"
        f"## Your Checklist\n{checklist}\n\n"
        f"## Current System State\n{system_state}\n\n"
        f"Review each checklist item against the system state. "
        f"Only flag things that genuinely need attention — no false alarms. "
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
                    full_msg = f"🤖 Nova Heartbeat Agent:\n{msg}"
                    if not _ground_service_alert(msg, checks):
                        continue
                    if _telegram_cooldown_gate(full_msg):
                        _send_telegram(full_msg)
                    else:
                        print(f"[heartbeat] Cooldown suppressed: {msg[:80]}")
            elif action_type == "task":
                title = action.get("title", "heartbeat_proactive")
                body = action.get("body", "")
                _inject_proactive_task(title, body)
    else:
        # No structured JSON — treat the whole response as a notification
        full_msg = f"🤖 Nova Heartbeat Agent:\n{response[:500]}"
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
        _send_telegram(f"{emoji} {label} Complete:\n{title}{sink_str}")

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
                alert_msg = f"🛡️ Self-Healing [{alert['severity'].upper()}]: {alert['title']}\n{alert['detail']}"
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
                alert_msg = f"⚡ Max-Plan Guard [{alert['severity'].upper()}]: {alert['title']}\n{alert['detail']}"
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

    write_heartbeat(checks)

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

    # Always send heartbeat pulse to Telegram
    send_telegram_heartbeat(checks)

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
        if _degradation_tier >= 3:
            print("[heartbeat-agent] Skipped (EMERGENCY degradation tier — no LLM calls)")
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
        _skip_research = _degradation_tier >= 1 or _heartbeat_shutdown_requested
        _skip_planning = _degradation_tier >= 2 or _heartbeat_shutdown_requested
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

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
