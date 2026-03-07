"""Phase 7.7 — Production Hardening.

Feature-flagged rollout controls, rate limiting, archive/cleanup,
optional manual approval hooks, restart recovery, and policy denial
auditing for the multi-agent system.

All behavior is deterministic, bounded, and auditable.
Reuses existing STATE/, policy engine, and coordination primitives.

Stdlib only — no pip installs required.
"""

import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(os.environ.get("NOVACORE_ROOT", "/home/nova/nova-core"))


# ---------------------------------------------------------------------------
# Feature Flags — fail-closed access to rollout controls
# ---------------------------------------------------------------------------

class FeatureFlags:
    """Centralized feature flag access. Fail-closed on missing/corrupt data.

    Reads STATE/config/feature_flags.json.  If the file is missing,
    corrupt, or a key is absent, all multi-agent features default OFF.
    """

    def __init__(self, base: Path | None = None):
        self.base = base or BASE
        self._flags: dict | None = None

    def _load(self) -> dict:
        path = self.base / "STATE" / "config" / "feature_flags.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                return {}
            return data
        except (json.JSONDecodeError, OSError):
            return {}

    @property
    def flags(self) -> dict:
        if self._flags is None:
            self._flags = self._load()
        return self._flags

    def reload(self) -> None:
        self._flags = None

    # --- Master switch ---

    def is_multi_agent_enabled(self) -> bool:
        """Master switch for Phase 7 orchestrator. Fail-closed."""
        orch = self.flags.get("phase7_orchestrator", {})
        return orch.get("enabled", False) is True

    def orchestrator_config(self) -> dict:
        """Return the phase7_orchestrator config block (or empty dict)."""
        return self.flags.get("phase7_orchestrator", {})

    # --- Hardening flags ---

    def hardening_config(self) -> dict:
        """Return the phase7_hardening config block (or empty dict)."""
        return self.flags.get("phase7_hardening", {})

    def is_hardening_feature_enabled(self, feature: str) -> bool:
        """Check a specific hardening flag. Fail-closed (default False)."""
        return self.hardening_config().get(feature, False) is True

    def is_manual_approval_enabled(self) -> bool:
        return self.is_hardening_feature_enabled("manual_approval")

    def is_archive_enabled(self) -> bool:
        return self.is_hardening_feature_enabled("archive_cleanup")

    def is_rate_limiting_enabled(self) -> bool:
        return self.is_hardening_feature_enabled("rate_limiting")


# ---------------------------------------------------------------------------
# Rate Limiter — deterministic, counter-based, file-persisted
# ---------------------------------------------------------------------------

MAX_WORKFLOWS_PER_HOUR = 10
MAX_AGENT_SPAWNS_PER_HOUR = 20
MAX_TOOL_CALLS_PER_MINUTE = 60


@dataclass
class RateCheckResult:
    category: str
    allowed: bool
    count: int
    limit: int
    window_s: int
    remaining: int


class RateLimiter:
    """Deterministic rate limiter backed by STATE/rate_limits.json.

    Tracks event timestamps per category within a sliding window.
    All state is file-persisted — no hidden in-memory counters.
    """

    def __init__(self, base: Path | None = None):
        self.base = base or BASE
        self._state_path = self.base / "STATE" / "rate_limits.json"

    def _load_state(self) -> dict:
        if not self._state_path.exists():
            return {}
        try:
            data = json.loads(self._state_path.read_text())
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_state(self, state: dict) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.rename(self._state_path)

    def check_rate(self, category: str, limit: int,
                   window_s: int) -> RateCheckResult:
        """Check if an action is within its rate limit window."""
        now = time.time()
        state = self._load_state()
        events = state.get(category, {}).get("events", [])

        # Prune expired events
        cutoff = now - window_s
        events = [t for t in events if t > cutoff]

        count = len(events)
        allowed = count < limit
        return RateCheckResult(
            category=category,
            allowed=allowed,
            count=count,
            limit=limit,
            window_s=window_s,
            remaining=max(0, limit - count),
        )

    def record_event(self, category: str, window_s: int = 3600) -> None:
        """Record an event occurrence for rate limiting."""
        now = time.time()
        state = self._load_state()
        bucket = state.setdefault(category, {"events": []})

        cutoff = now - window_s
        bucket["events"] = [t for t in bucket["events"] if t > cutoff]
        bucket["events"].append(now)

        self._save_state(state)

    def check_workflow_launch(self) -> RateCheckResult:
        return self.check_rate("workflow_launch",
                               MAX_WORKFLOWS_PER_HOUR, 3600)

    def check_agent_spawn(self) -> RateCheckResult:
        return self.check_rate("agent_spawn",
                               MAX_AGENT_SPAWNS_PER_HOUR, 3600)


# ---------------------------------------------------------------------------
# Archive / Cleanup — bounded, explicit, auditable
# ---------------------------------------------------------------------------

ARCHIVE_AFTER_S = 86400      # archive after 24 hours
MAX_ARCHIVE_KEEP = 100       # keep last 100 archived items per category
STALE_TMP_THRESHOLD_S = 3600 # orphan .tmp files older than 1 hour


class ArchiveManager:
    """Deterministic archive/cleanup for completed workflow and agent state.

    Moves terminal-state artifacts to STATE/archive/ after ARCHIVE_AFTER_S
    and enforces MAX_ARCHIVE_KEEP to prevent unbounded growth.
    """

    def __init__(self, base: Path | None = None):
        self.base = base or BASE

    def _parse_timestamp(self, value, fallback_path: Path | None = None) -> float:
        """Parse a numeric or ISO timestamp. Returns epoch float."""
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(
                    value.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                pass
        if fallback_path and fallback_path.exists():
            return fallback_path.stat().st_mtime
        return 0.0

    def archive_completed_workflows(self) -> list[str]:
        """Move completed/failed/halted workflow state to archive."""
        wf_dir = self.base / "STATE" / "workflows"
        archive_dir = self.base / "STATE" / "archive" / "workflows"
        if not wf_dir.exists():
            return []

        now = time.time()
        archived = []

        for wf_file in sorted(wf_dir.glob("*.json")):
            try:
                data = json.loads(wf_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue

            status = data.get("status", "")
            if status not in ("completed", "failed", "halted"):
                continue

            ts = self._parse_timestamp(
                data.get("completed_at") or data.get("updated_at", 0),
                wf_file,
            )
            if now - ts < ARCHIVE_AFTER_S:
                continue

            archive_dir.mkdir(parents=True, exist_ok=True)
            wf_file.rename(archive_dir / wf_file.name)
            archived.append(wf_file.name)

        return archived

    def archive_agent_runtime(self) -> list[str]:
        """Move completed/failed agent runtime records to archive."""
        rt_dir = self.base / "STATE" / "agents" / "runtime"
        archive_dir = self.base / "STATE" / "archive" / "agents"
        if not rt_dir.exists():
            return []

        now = time.time()
        archived = []

        for rt_file in sorted(rt_dir.glob("*.json")):
            try:
                data = json.loads(rt_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue

            if data.get("status", "") not in ("completed", "failed"):
                continue

            ts = self._parse_timestamp(
                data.get("updated_at") or data.get("completed_at", 0),
                rt_file,
            )
            if now - ts < ARCHIVE_AFTER_S:
                continue

            archive_dir.mkdir(parents=True, exist_ok=True)
            rt_file.rename(archive_dir / rt_file.name)
            archived.append(rt_file.name)

        return archived

    def cleanup_expired_leases(self) -> list[str]:
        """Remove expired lease files."""
        leases_dir = self.base / "STATE" / "leases"
        if not leases_dir.exists():
            return []

        now = time.time()
        cleaned = []

        for lease_file in leases_dir.glob("*.json"):
            try:
                data = json.loads(lease_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue

            acquired = data.get("acquired_at", 0)
            renewed = data.get("renewed_at")
            ttl = data.get("ttl_s", 600)
            expires_at = (renewed or acquired) + ttl

            if now > expires_at:
                lease_file.unlink()
                cleaned.append(lease_file.name)

        return cleaned

    def cleanup_stale_tmp_files(self) -> list[str]:
        """Remove orphaned .tmp files from STATE/."""
        state_dir = self.base / "STATE"
        if not state_dir.exists():
            return []

        now = time.time()
        cleaned = []

        for tmp_file in state_dir.rglob("*.tmp"):
            try:
                age = now - tmp_file.stat().st_mtime
            except OSError:
                continue
            if age > STALE_TMP_THRESHOLD_S:
                tmp_file.unlink(missing_ok=True)
                cleaned.append(str(tmp_file.relative_to(self.base)))

        return cleaned

    def cleanup_old_approvals(self, max_age_s: int = 86400) -> list[str]:
        """Remove resolved/expired approval files older than max_age_s."""
        approvals_dir = self.base / "STATE" / "approvals"
        if not approvals_dir.exists():
            return []

        now = time.time()
        cleaned = []

        for f in approvals_dir.glob("*.json"):
            try:
                age = now - f.stat().st_mtime
            except OSError:
                continue
            if age > max_age_s:
                f.unlink(missing_ok=True)
                cleaned.append(f.name)

        return cleaned

    def enforce_archive_limits(self) -> int:
        """Trim archives to MAX_ARCHIVE_KEEP most recent. Returns removed count."""
        removed = 0
        for subdir in ("workflows", "agents"):
            archive_dir = self.base / "STATE" / "archive" / subdir
            if not archive_dir.exists():
                continue

            files = sorted(archive_dir.glob("*.json"),
                           key=lambda f: f.stat().st_mtime)
            excess = len(files) - MAX_ARCHIVE_KEEP
            if excess > 0:
                for f in files[:excess]:
                    f.unlink(missing_ok=True)
                    removed += 1
        return removed

    def run_cleanup(self) -> dict:
        """Execute all cleanup tasks. Returns summary dict."""
        result = {
            "archived_workflows": self.archive_completed_workflows(),
            "archived_agents": self.archive_agent_runtime(),
            "cleaned_leases": self.cleanup_expired_leases(),
            "cleaned_tmp": self.cleanup_stale_tmp_files(),
            "cleaned_approvals": self.cleanup_old_approvals(),
        }
        result["archive_trimmed"] = self.enforce_archive_limits()
        return result


# ---------------------------------------------------------------------------
# Manual Approval Hooks — optional, bounded, for high-risk actions only
# ---------------------------------------------------------------------------

HIGH_RISK_TOOLS = frozenset({
    "repo.git.commit",
    "system.service.restart",
})

APPROVAL_TIMEOUT_S = 300  # 5 minutes


class ApprovalGate:
    """Optional manual approval gate for high-risk actions.

    Only active when phase7_hardening.manual_approval flag is True.
    Writes pending approval requests to STATE/approvals/.
    External approver (Telegram admin, human) can approve/deny.
    Auto-denies on timeout.
    """

    def __init__(self, base: Path | None = None):
        self.base = base or BASE
        self._approvals_dir = self.base / "STATE" / "approvals"

    def is_approval_required(self, tool_name: str) -> bool:
        """Check if manual approval is required for this tool.

        Returns False if manual_approval flag is disabled (fail-closed).
        """
        ff = FeatureFlags(self.base)
        if not ff.is_manual_approval_enabled():
            return False
        return tool_name in HIGH_RISK_TOOLS

    def request_approval(self, action_id: str, tool_name: str,
                         agent_id: str, detail: str) -> Path:
        """Write a pending approval request. Returns the request path."""
        self._approvals_dir.mkdir(parents=True, exist_ok=True)
        request = {
            "action_id": action_id,
            "tool_name": tool_name,
            "agent_id": agent_id,
            "detail": detail,
            "status": "pending",
            "requested_at": time.time(),
            "timeout_at": time.time() + APPROVAL_TIMEOUT_S,
        }
        path = self._approvals_dir / f"{action_id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(request, indent=2))
        tmp.rename(path)
        return path

    def check_approval(self, action_id: str) -> tuple[bool, str]:
        """Check approval status. Returns (approved, reason)."""
        path = self._approvals_dir / f"{action_id}.json"
        if not path.exists():
            return False, "no approval request found"

        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return False, "corrupt approval file"

        status = data.get("status", "pending")

        if status == "approved":
            return True, "manually approved"
        if status == "denied":
            return False, data.get("reason", "manually denied")

        # Check timeout
        if time.time() > data.get("timeout_at", 0):
            data["status"] = "denied"
            data["reason"] = "approval_timeout"
            path.write_text(json.dumps(data, indent=2))
            return False, "approval timed out"

        return False, "pending approval"

    def approve(self, action_id: str, approver: str = "admin") -> bool:
        """Grant approval for a pending action."""
        path = self._approvals_dir / f"{action_id}.json"
        if not path.exists():
            return False

        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return False

        data["status"] = "approved"
        data["approved_by"] = approver
        data["approved_at"] = time.time()
        path.write_text(json.dumps(data, indent=2))
        return True

    def deny(self, action_id: str, reason: str = "denied",
             denier: str = "admin") -> bool:
        """Deny a pending action."""
        path = self._approvals_dir / f"{action_id}.json"
        if not path.exists():
            return False

        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return False

        data["status"] = "denied"
        data["denied_by"] = denier
        data["denied_at"] = time.time()
        data["reason"] = reason
        path.write_text(json.dumps(data, indent=2))
        return True


# ---------------------------------------------------------------------------
# Policy Denial Auditing
# ---------------------------------------------------------------------------

def audit_policy_denial(agent_id: str, tool_name: str, reason: str,
                        base: Path | None = None) -> None:
    """Append a policy denial record to STATE/policy_denials.jsonl.

    Provides a dedicated audit trail for denied tool calls, separate
    from the general tool_audit.jsonl.
    """
    base = base or BASE
    audit_path = base / "STATE" / "policy_denials.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "ts": time.time(),
        "agent_id": agent_id,
        "tool_name": tool_name,
        "allowed": False,
        "reason": reason,
    }
    with audit_path.open("a") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Restart Recovery — reconcile state from persisted artifacts
# ---------------------------------------------------------------------------

class RestartRecovery:
    """Reconcile workflow, lease, and task state after service restart.

    Uses existing persisted STATE/ artifacts to reconstruct recoverable
    execution state. All recovery actions are logged to LOGS/recovery.log.
    """

    def __init__(self, base: Path | None = None):
        self.base = base or BASE

    def reconcile(self) -> dict:
        """Run full restart recovery. Returns summary of actions taken."""
        actions: list[dict] = []

        actions.extend(self._cleanup_stale_pids())
        actions.extend(self._recover_stale_leases())
        actions.extend(self._reconcile_workflows())
        actions.extend(self._reconcile_inprogress_tasks())

        self._write_recovery_log(actions)

        return {
            "actions": actions,
            "total_actions": len(actions),
            "recovered_at": time.time(),
        }

    def _cleanup_stale_pids(self) -> list[dict]:
        """Remove PID files for dead processes."""
        running_dir = self.base / "STATE" / "running"
        if not running_dir.exists():
            return []

        actions = []
        for pid_file in running_dir.glob("*.pid"):
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)  # Check if process exists
            except ProcessLookupError:
                pid_file.unlink()
                actions.append({
                    "type": "stale_pid_removed",
                    "file": pid_file.name,
                    "detail": f"PID {pid} no longer running",
                })
            except (ValueError, PermissionError, OSError):
                pass

        return actions

    def _recover_stale_leases(self) -> list[dict]:
        """Remove expired lease files."""
        leases_dir = self.base / "STATE" / "leases"
        if not leases_dir.exists():
            return []

        now = time.time()
        actions = []

        for lease_file in leases_dir.glob("*.json"):
            if lease_file.name == "recovery.jsonl":
                continue
            try:
                data = json.loads(lease_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue

            acquired = data.get("acquired_at", 0)
            renewed = data.get("renewed_at")
            ttl = data.get("ttl_s", 600)
            expires_at = (renewed or acquired) + ttl

            if now > expires_at:
                lease_file.unlink()
                actions.append({
                    "type": "lease_recovered",
                    "file": lease_file.name,
                    "detail": f"Expired {now - expires_at:.0f}s ago",
                })

        return actions

    def _reconcile_workflows(self) -> list[dict]:
        """Reconcile in-flight workflows after restart.

        Stale workflows (no update in 2x SLA) are halted.
        Recoverable workflows have executing nodes reset to pending.
        """
        wf_dir = self.base / "STATE" / "workflows"
        if not wf_dir.exists():
            return []

        now = time.time()
        actions = []

        for wf_file in wf_dir.glob("*.json"):
            try:
                data = json.loads(wf_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue

            status = data.get("status", "")
            if status not in ("executing", "planning", "created"):
                continue

            # Determine last activity timestamp
            updated = data.get("updated_at") or data.get("created_at", 0)
            updated_ts = self._parse_ts(updated, wf_file)

            max_runtime = data.get("budget", {}).get("max_runtime_s", 1800)

            if now - updated_ts > max_runtime * 2:
                # Too stale — halt
                data["status"] = "halted"
                data["halt_reason"] = "restart_recovery_stale"
                data["recovered_at"] = now
                wf_file.write_text(json.dumps(data, indent=2))
                actions.append({
                    "type": "workflow_halted",
                    "file": wf_file.name,
                    "detail": "Stale beyond 2x SLA, halted on restart",
                })
            else:
                # Reset executing/claimed nodes to pending
                node_states = data.get("node_states", {})
                reset_count = 0
                failed_count = 0
                for _nid, ns in node_states.items():
                    if ns.get("status") in ("executing", "claimed"):
                        retries = ns.get("retry_count", 0)
                        max_retries = ns.get("max_retries", 1)
                        if retries < max_retries:
                            ns["status"] = "pending"
                            ns["retry_count"] = retries + 1
                            ns["assigned_agent"] = None
                            ns["claimed_at"] = None
                            ns["started_at"] = None
                            reset_count += 1
                        else:
                            ns["status"] = "failed"
                            ns["error"] = "max retries exceeded on restart"
                            ns["completed_at"] = now
                            failed_count += 1

                changed = reset_count + failed_count
                if changed > 0:
                    data["node_states"] = node_states
                    wf_file.write_text(json.dumps(data, indent=2))
                    detail_parts = []
                    if reset_count:
                        detail_parts.append(f"reset {reset_count}")
                    if failed_count:
                        detail_parts.append(f"failed {failed_count} (retries exhausted)")
                    actions.append({
                        "type": "workflow_nodes_reset",
                        "file": wf_file.name,
                        "detail": ", ".join(detail_parts),
                    })

        return actions

    def _reconcile_inprogress_tasks(self) -> list[dict]:
        """Requeue .inprogress tasks that have no running worker."""
        tasks_dir = self.base / "TASKS"
        if not tasks_dir.exists():
            return []

        running_dir = self.base / "STATE" / "running"
        actions = []

        for ip_file in tasks_dir.glob("*.inprogress"):
            stem = ip_file.name  # e.g. "0042_task.md.inprogress"
            if not stem.endswith(".md.inprogress"):
                continue

            task_stem = stem.replace(".md.inprogress", "")

            # Check if worker is still running
            worker_alive = False
            if running_dir.exists():
                pid_file = running_dir / f"{task_stem}.pid"
                if pid_file.exists():
                    try:
                        pid = int(pid_file.read_text().strip())
                        os.kill(pid, 0)
                        worker_alive = True
                    except (ProcessLookupError, ValueError,
                            PermissionError, OSError):
                        pass

            if not worker_alive:
                # Requeue: rename back to .md
                original_name = stem.replace(".inprogress", "")
                original = ip_file.parent / original_name
                ip_file.rename(original)
                actions.append({
                    "type": "task_requeued",
                    "file": stem,
                    "detail": "Requeued abandoned task (no worker running)",
                })

        return actions

    def _write_recovery_log(self, actions: list[dict]) -> None:
        """Append recovery actions to LOGS/recovery.log."""
        if not actions:
            return

        logs_dir = self.base / "LOGS"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = logs_dir / "recovery.log"

        now_iso = datetime.now(timezone.utc).isoformat()
        lines = [f"--- Recovery at {now_iso} ---"]
        for a in actions:
            lines.append(f"  [{a['type']}] {a.get('file', '')} — {a['detail']}")
        lines.append("")

        with log_path.open("a") as f:
            f.write("\n".join(lines) + "\n")

    @staticmethod
    def _parse_ts(value, fallback_path: Path | None = None) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(
                    value.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                pass
        if fallback_path and fallback_path.exists():
            return fallback_path.stat().st_mtime
        return 0.0


# ---------------------------------------------------------------------------
# Integration entry point — called from heartbeat.py
# ---------------------------------------------------------------------------

def run_production_hardening(base: Path | None = None) -> dict:
    """Run all production hardening maintenance tasks.

    Designed to be called from the heartbeat timer (every 30min).
    Non-fatal — wraps each subsystem in try/except.
    """
    base = base or BASE
    result: dict = {}

    ff = FeatureFlags(base)
    result["multi_agent_enabled"] = ff.is_multi_agent_enabled()

    # Archive/cleanup (only when enabled)
    if ff.is_archive_enabled():
        try:
            am = ArchiveManager(base)
            result["cleanup"] = am.run_cleanup()
        except Exception as e:
            result["cleanup_error"] = str(e)
    else:
        result["cleanup"] = "disabled"

    return result
