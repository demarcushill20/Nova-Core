"""Testing-Mode Automation Loop — evidence-driven supervised autonomy.

Upgrades the heartbeat from a reflective loop into an evidence-driven
supervised autonomy loop that can safely test real automation reliability
via canary tasks, contract validation, anomaly detection, and
evidence-gated plan revision.

Stdlib-only, thread-safe.  Follows the pattern of self_healing.py and
max_plan_guard.py.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE = Path("/home/nova/nova-core")
STATE_DIR = BASE / "STATE" / "testing_mode"
OUTPUT_DIR = BASE / "OUTPUT"
CONFIG_FILE = STATE_DIR / "config.json"
EVIDENCE_FILE = STATE_DIR / "evidence.jsonl"
ANOMALY_FILE = STATE_DIR / "anomaly_history.jsonl"
CANARY_REGISTRY_FILE = STATE_DIR / "canary_registry.json"
LAST_CANARY_RUN = STATE_DIR / "last_canary_run.json"
LAST_CYCLE_SUMMARY = STATE_DIR / "last_cycle_summary.json"
LAST_DEEP_MAINTENANCE = STATE_DIR / "last_deep_maintenance.json"
CYCLE_SUMMARY_DIR = STATE_DIR / "cycle_summaries"

EVIDENCE_MAX_ENTRIES = 2000
ANOMALY_MAX_ENTRIES = 500
CANARY_TIMEOUT_DEFAULT = 120  # seconds
CANARY_COOLDOWN_MINUTES = 55  # ~hourly
CYCLE_SUMMARY_COOLDOWN_MINUTES = 170  # ~3 hours
DEEP_MAINTENANCE_COOLDOWN_MINUTES = 720  # ~12 hours

ACTIVE_HOURS_START = int(os.environ.get("HEARTBEAT_ACTIVE_START", "0"))
ACTIVE_HOURS_END = int(os.environ.get("HEARTBEAT_ACTIVE_END", "24"))

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TestingMode(IntEnum):
    """Testing-mode levels."""

    NORMAL = 0
    TESTING = 1
    DRY_RUN = 2


class CanaryVerdict(str, Enum):
    """Outcome of a canary task execution."""

    PASS = "pass"  # noqa: S105
    SOFT_FAIL = "soft_fail"
    HARD_FAIL = "hard_fail"


class AnomalySeverity(str, Enum):
    """Severity of a detected anomaly."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TestingModeConfig:
    """Persistent testing-mode configuration."""

    mode: int = TestingMode.NORMAL
    enabled_at: str = ""
    reason: str = ""
    canary_budget_limit: int = 5

    @property
    def dry_run(self) -> bool:
        return self.mode == TestingMode.DRY_RUN


@dataclass
class CanaryTask:
    """Definition of a canary task."""

    id: str
    name: str
    description: str
    task_content: str
    expected_contract_fields: list[str] = field(default_factory=lambda: ["status", "confidence"])
    timeout_secs: int = CANARY_TIMEOUT_DEFAULT
    safe: bool = True
    tags: list[str] = field(default_factory=list)


@dataclass
class CanaryResult:
    """Result of executing a canary task."""

    task_id: str
    cycle_id: str
    run_id: str
    verdict: str  # CanaryVerdict value
    reason: str
    confidence: float = 0.0
    output_path: str = ""
    contract_valid: bool = False
    duration_secs: float = 0.0
    timestamp: str = ""
    dry_run: bool = False
    error: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


@dataclass
class EvidenceEntry:
    """Single entry in the append-only evidence ledger."""

    cycle_id: str
    run_id: str
    timestamp: str
    entry_type: str  # "health_snapshot", "canary_result", "anomaly", "cycle_summary"
    data: dict = field(default_factory=dict)
    verdict: str = ""


@dataclass
class AnomalyReport:
    """A detected anomaly / pathology."""

    anomaly_type: str  # REPEATED_TASK, STUCK_RETRY, SILENT_FAILURE, EVIDENCE_GAP, TREND_REGRESSION
    severity: str  # AnomalySeverity value
    detail: str
    evidence_ids: list[str] = field(default_factory=list)
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


@dataclass
class CycleSummary:
    """Summary of a testing-mode cycle."""

    cycle_id: str
    timestamp: str
    canary_count: int = 0
    pass_count: int = 0
    soft_fail_count: int = 0
    hard_fail_count: int = 0
    anomalies: list[dict] = field(default_factory=list)
    health_snapshot: dict = field(default_factory=dict)
    trend: str = "stable"


# ---------------------------------------------------------------------------
# (A) Mode Management
# ---------------------------------------------------------------------------


def _ensure_dirs() -> None:
    """Create STATE/testing_mode/ tree if needed."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CYCLE_SUMMARY_DIR.mkdir(parents=True, exist_ok=True)


def get_testing_mode() -> TestingModeConfig:
    """Read current testing-mode configuration.  Returns NORMAL if unset."""
    if not CONFIG_FILE.exists():
        return TestingModeConfig()
    try:
        data = json.loads(CONFIG_FILE.read_text())
        return TestingModeConfig(
            mode=int(data.get("mode", TestingMode.NORMAL)),
            enabled_at=data.get("enabled_at", ""),
            reason=data.get("reason", ""),
            canary_budget_limit=int(data.get("canary_budget_limit", 5)),
        )
    except Exception:
        return TestingModeConfig()


def set_testing_mode(mode: int | TestingMode, reason: str = "") -> TestingModeConfig:
    """Persist a new testing-mode configuration."""
    _ensure_dirs()
    cfg = TestingModeConfig(
        mode=int(mode),
        enabled_at=datetime.now(timezone.utc).isoformat(),
        reason=reason,
    )
    with _lock:
        CONFIG_FILE.write_text(json.dumps(asdict(cfg), indent=2) + "\n")
    return cfg


def is_testing_active() -> bool:
    """Return True if mode is TESTING or DRY_RUN."""
    return get_testing_mode().mode != TestingMode.NORMAL


# ---------------------------------------------------------------------------
# (B) Health Check Extension
# ---------------------------------------------------------------------------


def health_snapshot_from_checks(
    checks: list[dict],
    cycle_id: str,
    run_id: str,
) -> EvidenceEntry:
    """Convert heartbeat health-check results into an evidence entry."""
    total = len(checks)
    passed = sum(1 for c in checks if c.get("ok"))
    failed_names = [c["name"] for c in checks if not c.get("ok")]
    return EvidenceEntry(
        cycle_id=cycle_id,
        run_id=run_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        entry_type="health_snapshot",
        data={
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "failed_names": failed_names,
            "health_pct": round(passed / total * 100, 1) if total else 100.0,
        },
        verdict="pass" if not failed_names else "soft_fail",
    )


# ---------------------------------------------------------------------------
# (C) Canary Task Selector
# ---------------------------------------------------------------------------


def _default_canary_registry() -> list[CanaryTask]:
    """Built-in safe canary tasks."""
    return [
        CanaryTask(
            id="canary_contract_check",
            name="Contract Check",
            description="Write minimal OUTPUT with valid CONTRACT block",
            task_content=(
                "Write a file to OUTPUT/canary_contract_check.md with a brief status "
                "summary and a valid ## CONTRACT block containing status, confidence, "
                "and summary fields."
            ),
            expected_contract_fields=["status", "confidence", "summary"],
            tags=["contract", "write"],
        ),
        CanaryTask(
            id="canary_echo_test",
            name="Echo Test",
            description="Read HEARTBEAT.md, write summary to OUTPUT",
            task_content=(
                "Read HEARTBEAT.md and write a 3-line summary of its health status "
                "to OUTPUT/canary_echo_test.md. Include a valid ## CONTRACT block."
            ),
            tags=["read", "write"],
        ),
        CanaryTask(
            id="canary_state_read",
            name="State Read",
            description="Read STATE/metrics.json, report task success rate",
            task_content=(
                "Read STATE/metrics.json (if it exists) and write a summary of the "
                "task success rate to OUTPUT/canary_state_read.md. "
                "Include a valid ## CONTRACT block."
            ),
            tags=["read", "metrics"],
        ),
        CanaryTask(
            id="canary_memory_query",
            name="Memory Query",
            description="Call query_memory, write result summary",
            task_content=(
                "Use the query_memory tool to search for 'heartbeat health' and "
                "write a brief summary of the results to OUTPUT/canary_memory_query.md. "
                "Include a valid ## CONTRACT block."
            ),
            tags=["memory", "query"],
        ),
        CanaryTask(
            id="canary_git_status",
            name="Git Status",
            description="Run git status, write summary",
            task_content=(
                "Run 'git status' in /home/nova/nova-core and write a summary of "
                "the repository state to OUTPUT/canary_git_status.md. "
                "Include a valid ## CONTRACT block."
            ),
            tags=["git", "read"],
        ),
        CanaryTask(
            id="canary_health_summary",
            name="Health Summary",
            description="Parse HEARTBEAT.md, summarize pass/fail",
            task_content=(
                "Read HEARTBEAT.md, count PASS and FAIL checks, and write a "
                "health summary to OUTPUT/canary_health_summary.md. "
                "Include a valid ## CONTRACT block."
            ),
            tags=["read", "health"],
        ),
    ]


def load_canary_registry() -> list[CanaryTask]:
    """Load canary task registry.  Falls back to defaults."""
    if CANARY_REGISTRY_FILE.exists():
        try:
            data = json.loads(CANARY_REGISTRY_FILE.read_text())
            if isinstance(data, list) and data:
                tasks = []
                for item in data:
                    if isinstance(item, dict) and "id" in item and "name" in item:
                        tasks.append(
                            CanaryTask(
                                id=item["id"],
                                name=item["name"],
                                description=item.get("description", ""),
                                task_content=item.get("task_content", ""),
                                expected_contract_fields=item.get(
                                    "expected_contract_fields",
                                    ["status", "confidence"],
                                ),
                                timeout_secs=item.get("timeout_secs", CANARY_TIMEOUT_DEFAULT),
                                safe=item.get("safe", True),
                                tags=item.get("tags", []),
                            )
                        )
                if tasks:
                    return tasks
        except Exception:  # noqa: S110
            pass
    return _default_canary_registry()


def select_canary_tasks(
    registry: list[CanaryTask],
    count: int = 2,
    recent_evidence: list[dict] | None = None,
) -> list[CanaryTask]:
    """Select canary tasks weighted to avoid recent repeats."""
    if not registry:
        return []

    count = min(count, len(registry))

    # Find recently-run task IDs from evidence
    recent_ids: set[str] = set()
    if recent_evidence:
        for entry in recent_evidence:
            data = entry.get("data", {})
            if entry.get("entry_type") == "canary_result":
                tid = data.get("task_id", "")
                if tid:
                    recent_ids.add(tid)

    # Weight: tasks not recently run get higher weight
    weights = []
    for task in registry:
        if not task.safe:
            weights.append(0.0)
        elif task.id in recent_ids:
            weights.append(0.3)  # lower weight for recent
        else:
            weights.append(1.0)

    # If all weights are zero, fall back to uniform for safe tasks only
    if sum(weights) == 0:
        weights = [1.0 if t.safe else 0.0 for t in registry]
        if sum(weights) == 0:
            return []  # no safe tasks available

    # Weighted selection without replacement
    selected: list[CanaryTask] = []
    available = list(range(len(registry)))
    avail_weights = list(weights)

    for _ in range(count):
        if not available:
            break
        total = sum(avail_weights)
        if total <= 0:
            break
        r = random.random() * total  # noqa: S311
        cumulative = 0.0
        chosen_idx = 0
        for i, w in enumerate(avail_weights):
            cumulative += w
            if r <= cumulative:
                chosen_idx = i
                break
        selected.append(registry[available[chosen_idx]])
        available.pop(chosen_idx)
        avail_weights.pop(chosen_idx)

    return selected


# ---------------------------------------------------------------------------
# (D) Canary Executor
# ---------------------------------------------------------------------------


def execute_canary(
    task: CanaryTask,
    cycle_id: str,
    run_id: str,
    dry_run: bool = False,
) -> CanaryResult:
    """Execute a canary task.  dry_run returns synthetic PASS."""
    start = time.monotonic()

    if dry_run:
        # Synthetic result — no subprocess
        output_path = str(OUTPUT_DIR / f"canary_{task.id}_dryrun.md")
        return CanaryResult(
            task_id=task.id,
            cycle_id=cycle_id,
            run_id=run_id,
            verdict=CanaryVerdict.PASS.value,
            reason="dry-run synthetic pass",
            confidence=1.0,
            output_path=output_path,
            contract_valid=True,
            duration_secs=round(time.monotonic() - start, 2),
            dry_run=True,
        )

    # Live execution — check active hours
    current_hour = datetime.now(timezone.utc).hour
    if not (ACTIVE_HOURS_START <= current_hour < ACTIVE_HOURS_END):
        return CanaryResult(
            task_id=task.id,
            cycle_id=cycle_id,
            run_id=run_id,
            verdict=CanaryVerdict.SOFT_FAIL.value,
            reason=f"outside active hours ({ACTIVE_HOURS_START}-{ACTIVE_HOURS_END} UTC)",
            duration_secs=round(time.monotonic() - start, 2),
        )

    # Check max-plan guard
    try:
        from utils.max_plan_guard import should_allow_task

        allowed, guard_reason = should_allow_task(caller="testing_mode_canary")
        if not allowed:
            return CanaryResult(
                task_id=task.id,
                cycle_id=cycle_id,
                run_id=run_id,
                verdict=CanaryVerdict.SOFT_FAIL.value,
                reason=f"max-plan guard blocked: {guard_reason}",
                duration_secs=round(time.monotonic() - start, 2),
            )
    except ImportError:
        pass

    # Run via Claude subprocess (same pattern as _run_heartbeat_agent)
    claude_bin = os.environ.get("CLAUDE_BIN", "/home/nova/.local/bin/claude")
    prompt = (
        f"You are running a canary test task.\n\n"
        f"Task: {task.name}\n"
        f"Description: {task.description}\n\n"
        f"Instructions:\n{task.task_content}\n\n"
        f"Write output to OUTPUT/canary_{task.id}.md\n"
        f"You MUST include a ## CONTRACT block with at minimum: "
        f"status, confidence fields.\n"
        f"Complete this task quickly and concisely."
    )

    try:
        subprocess.run(
            [claude_bin, "-p", prompt, "--output-format", "text"],
            capture_output=True,
            text=True,
            timeout=task.timeout_secs,
            cwd=str(BASE),
        )

        canary_path = OUTPUT_DIR / f"canary_{task.id}.md"
        output_text = ""
        if canary_path.exists():
            output_text = canary_path.read_text()

        # Validate contract
        verdict, reason, confidence = validate_canary_output(output_text, task)

        return CanaryResult(
            task_id=task.id,
            cycle_id=cycle_id,
            run_id=run_id,
            verdict=verdict,
            reason=reason,
            confidence=confidence,
            output_path=str(canary_path),
            contract_valid=(verdict == CanaryVerdict.PASS.value),
            duration_secs=round(time.monotonic() - start, 2),
        )

    except subprocess.TimeoutExpired:
        return CanaryResult(
            task_id=task.id,
            cycle_id=cycle_id,
            run_id=run_id,
            verdict=CanaryVerdict.HARD_FAIL.value,
            reason=f"timeout after {task.timeout_secs}s",
            duration_secs=round(time.monotonic() - start, 2),
            error="timeout",
        )
    except Exception as e:
        return CanaryResult(
            task_id=task.id,
            cycle_id=cycle_id,
            run_id=run_id,
            verdict=CanaryVerdict.HARD_FAIL.value,
            reason=f"subprocess error: {e}",
            duration_secs=round(time.monotonic() - start, 2),
            error=str(e),
        )


# ---------------------------------------------------------------------------
# (E) Validator / Contract Checker
# ---------------------------------------------------------------------------


def validate_canary_output(
    output: str,
    task: CanaryTask,
) -> tuple[str, str, float]:
    """Validate canary output against expected contract.

    Returns (verdict, reason, confidence).
    """
    if not output or not output.strip():
        return CanaryVerdict.HARD_FAIL.value, "empty output", 0.0

    # Try to use tools.contracts.validate_contract for parsing
    contract: dict = {}
    try:
        from tools.contracts import validate_contract

        result = validate_contract(output)
        contract = result.get("contract", {})
    except ImportError:
        pass

    # If no contract dict extracted, fallback to header check
    if not contract:
        if "## CONTRACT" in output:
            # Parse manually: look for key-value pairs after ## CONTRACT
            in_contract = False
            for line in output.splitlines():
                if line.strip().startswith("## CONTRACT"):
                    in_contract = True
                    continue
                if in_contract and line.strip().startswith("- "):
                    kv = line.strip().lstrip("- ").split(":", 1)
                    if len(kv) == 2:
                        contract[kv[0].strip()] = kv[1].strip()
                elif in_contract and line.strip().startswith("#"):
                    break
        if not contract:
            return CanaryVerdict.SOFT_FAIL.value, "no contract fields found", 0.2

    # Check expected fields
    missing = [f for f in task.expected_contract_fields if f not in contract]
    if missing:
        return (
            CanaryVerdict.SOFT_FAIL.value,
            f"missing expected fields: {missing}",
            0.5,
        )

    # Extract confidence
    conf = 0.8
    raw_conf = contract.get("confidence", "")
    if isinstance(raw_conf, (int, float)):
        conf = float(raw_conf)
    elif isinstance(raw_conf, str):
        conf_map = {"high": 0.9, "medium": 0.7, "low": 0.4}
        conf = conf_map.get(raw_conf.lower(), 0.7)

    return CanaryVerdict.PASS.value, "contract valid", conf


# ---------------------------------------------------------------------------
# (F) Evidence Ledger Writer
# ---------------------------------------------------------------------------


def write_evidence(entry: EvidenceEntry) -> None:
    """Append an evidence entry to the JSONL ledger.  Thread-safe."""
    _ensure_dirs()
    with _lock:
        with open(EVIDENCE_FILE, "a") as f:
            f.write(json.dumps(asdict(entry)) + "\n")
        # Prune if oversized
        _prune_jsonl(EVIDENCE_FILE, EVIDENCE_MAX_ENTRIES)


def read_evidence(max_age_hours: float = 24.0) -> list[dict]:
    """Read evidence entries within the age window."""
    if not EVIDENCE_FILE.exists():
        return []

    cutoff = datetime.now(timezone.utc).timestamp() - (max_age_hours * 3600)
    entries: list[dict] = []

    try:
        with open(EVIDENCE_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts = entry.get("timestamp", "")
                    if ts:
                        entry_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if entry_time.timestamp() >= cutoff:
                            entries.append(entry)
                    else:
                        entries.append(entry)
                except (json.JSONDecodeError, ValueError):
                    continue
    except Exception:  # noqa: S110
        pass

    return entries


def _prune_jsonl(path: Path, max_entries: int) -> None:
    """Keep only the last *max_entries* lines in a JSONL file."""
    try:
        lines = path.read_text().strip().splitlines()
        if len(lines) > max_entries:
            keep = lines[-max_entries:]
            path.write_text("\n".join(keep) + "\n")
    except Exception:  # noqa: S110
        pass


# ---------------------------------------------------------------------------
# (G) Anomaly / Pathology Detector
# ---------------------------------------------------------------------------


def detect_anomalies(evidence: list[dict]) -> list[AnomalyReport]:
    """Detect anomalies from recent evidence entries."""
    anomalies: list[AnomalyReport] = []

    canary_results = [e for e in evidence if e.get("entry_type") == "canary_result"]

    if not canary_results:
        return anomalies

    # --- REPEATED_TASK: same task ID appearing > 3 times in window ---
    task_counts: dict[str, int] = {}
    for cr in canary_results:
        tid = cr.get("data", {}).get("task_id", "")
        if tid:
            task_counts[tid] = task_counts.get(tid, 0) + 1
    for tid, cnt in task_counts.items():
        if cnt > 3:
            anomalies.append(
                AnomalyReport(
                    anomaly_type="REPEATED_TASK",
                    severity=AnomalySeverity.WARNING.value,
                    detail=f"task {tid} ran {cnt} times in evidence window",
                    evidence_ids=[tid],
                )
            )

    # --- STUCK_RETRY: 3+ consecutive HARD_FAIL for same task ---
    task_streaks: dict[str, int] = {}
    for cr in canary_results:
        tid = cr.get("data", {}).get("task_id", "")
        verdict = cr.get("data", {}).get("verdict", cr.get("verdict", ""))
        if verdict == CanaryVerdict.HARD_FAIL.value:
            task_streaks[tid] = task_streaks.get(tid, 0) + 1
        else:
            task_streaks[tid] = 0
    for tid, streak in task_streaks.items():
        if streak >= 3:
            anomalies.append(
                AnomalyReport(
                    anomaly_type="STUCK_RETRY",
                    severity=AnomalySeverity.CRITICAL.value,
                    detail=f"task {tid} has {streak} consecutive HARD_FAILs",
                    evidence_ids=[tid],
                )
            )

    # --- SILENT_FAILURE: canary returned PASS but confidence < 0.3 ---
    for cr in canary_results:
        data = cr.get("data", {})
        verdict = data.get("verdict", cr.get("verdict", ""))
        conf = data.get("confidence", 1.0)
        if verdict == CanaryVerdict.PASS.value and conf < 0.3:
            tid = data.get("task_id", "unknown")
            anomalies.append(
                AnomalyReport(
                    anomaly_type="SILENT_FAILURE",
                    severity=AnomalySeverity.WARNING.value,
                    detail=f"task {tid} passed but confidence={conf:.2f}",
                    evidence_ids=[tid],
                )
            )

    # --- EVIDENCE_GAP: health snapshots > 2h apart ---
    snapshots = [e for e in evidence if e.get("entry_type") == "health_snapshot"]
    if len(snapshots) >= 2:
        sorted_snaps = sorted(snapshots, key=lambda e: e.get("timestamp", ""))
        for i in range(1, len(sorted_snaps)):
            try:
                t1 = datetime.fromisoformat(
                    sorted_snaps[i - 1]["timestamp"].replace("Z", "+00:00"),
                )
                t2 = datetime.fromisoformat(
                    sorted_snaps[i]["timestamp"].replace("Z", "+00:00"),
                )
                gap_hours = (t2 - t1).total_seconds() / 3600
                if gap_hours > 2.0:
                    anomalies.append(
                        AnomalyReport(
                            anomaly_type="EVIDENCE_GAP",
                            severity=AnomalySeverity.INFO.value,
                            detail=f"health snapshot gap of {gap_hours:.1f}h detected",
                        )
                    )
            except (ValueError, KeyError):
                continue

    # --- TREND_REGRESSION: pass rate dropping over last N canaries ---
    if len(canary_results) >= 6:
        mid = len(canary_results) // 2
        first_half = canary_results[:mid]
        second_half = canary_results[mid:]

        def _pass_rate(entries: list[dict]) -> float:
            if not entries:
                return 0.0
            passes = sum(
                1 for e in entries if e.get("data", {}).get("verdict", e.get("verdict", "")) == CanaryVerdict.PASS.value
            )
            return passes / len(entries)

        rate_early = _pass_rate(first_half)
        rate_late = _pass_rate(second_half)

        if rate_early > 0 and rate_late < rate_early * 0.6:
            anomalies.append(
                AnomalyReport(
                    anomaly_type="TREND_REGRESSION",
                    severity=AnomalySeverity.CRITICAL.value,
                    detail=(
                        f"pass rate dropped from {rate_early:.0%} to {rate_late:.0%} "
                        f"across {len(canary_results)} canary runs"
                    ),
                )
            )

    return anomalies


def _write_anomaly_history(anomalies: list[AnomalyReport]) -> None:
    """Append anomalies to the anomaly history JSONL."""
    if not anomalies:
        return
    _ensure_dirs()
    with _lock:
        with open(ANOMALY_FILE, "a") as f:
            for a in anomalies:
                f.write(json.dumps(asdict(a)) + "\n")
        _prune_jsonl(ANOMALY_FILE, ANOMALY_MAX_ENTRIES)


# ---------------------------------------------------------------------------
# (H) Plan Revision Gate
# ---------------------------------------------------------------------------


def should_allow_plan_revision(evidence: list[dict]) -> tuple[bool, str]:
    """Decide whether plan revision is justified by evidence.

    Requires:
    - At least 3 canary runs in evidence
    - Pass rate >= 60%
    - No active HARD_FAIL streak (3+ in a row at the tail)

    Returns (allowed, reason).
    """
    canary_results = [e for e in evidence if e.get("entry_type") == "canary_result"]

    if len(canary_results) < 3:
        return False, f"insufficient evidence: {len(canary_results)}/3 canary runs"

    # Pass rate
    passes = sum(
        1 for e in canary_results if e.get("data", {}).get("verdict", e.get("verdict", "")) == CanaryVerdict.PASS.value
    )
    pass_rate = passes / len(canary_results)
    if pass_rate < 0.6:
        return False, f"pass rate too low: {pass_rate:.0%} (need ≥60%)"

    # Check tail for HARD_FAIL streak
    tail = canary_results[-3:]
    all_hard_fail = all(
        e.get("data", {}).get("verdict", e.get("verdict", "")) == CanaryVerdict.HARD_FAIL.value for e in tail
    )
    if all_hard_fail:
        return False, "active HARD_FAIL streak in last 3 canary runs"

    return True, f"revision allowed: {pass_rate:.0%} pass rate, {len(canary_results)} runs"


# ---------------------------------------------------------------------------
# (I) Deep Maintenance Scheduler
# ---------------------------------------------------------------------------


def run_deep_maintenance(dry_run: bool = False) -> dict:
    """Run deep maintenance: stale cleanup, evidence compaction, registry health.

    Returns a report dict.
    """
    report: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "stale_cleaned": 0,
        "evidence_compacted": False,
        "registry_ok": True,
    }

    # 1. Stale canary output cleanup (> 7 days old)
    stale_cutoff = time.time() - (7 * 86400)
    stale_files: list[str] = []
    for f in OUTPUT_DIR.glob("canary_*.md"):
        try:
            if f.stat().st_mtime < stale_cutoff:
                stale_files.append(f.name)
                if not dry_run:
                    f.unlink()
        except OSError:
            continue
    report["stale_cleaned"] = len(stale_files)
    report["stale_files"] = stale_files[:20]

    # 2. Evidence compaction
    if EVIDENCE_FILE.exists():
        try:
            lines = EVIDENCE_FILE.read_text().strip().splitlines()
            if len(lines) > EVIDENCE_MAX_ENTRIES:
                if not dry_run:
                    _prune_jsonl(EVIDENCE_FILE, EVIDENCE_MAX_ENTRIES)
                report["evidence_compacted"] = True
                report["evidence_lines_before"] = len(lines)
        except Exception:  # noqa: S110
            pass

    # 3. Registry health check
    registry = load_canary_registry()
    report["registry_count"] = len(registry)
    report["registry_ok"] = len(registry) > 0

    return report


# ---------------------------------------------------------------------------
# Cooldown Helpers
# ---------------------------------------------------------------------------


def _check_cooldown(cooldown_file: Path, cooldown_minutes: int) -> bool:
    """Return True if cooldown has elapsed (ok to run)."""
    if not cooldown_file.exists():
        return True
    try:
        data = json.loads(cooldown_file.read_text())
        last_run = data.get("timestamp", "")
        if not last_run:
            return True
        last_time = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - last_time).total_seconds() / 60
        return elapsed >= cooldown_minutes
    except Exception:
        return True


def _update_cooldown(cooldown_file: Path, extra: dict | None = None) -> None:
    """Write a cooldown timestamp file."""
    _ensure_dirs()
    data = {"timestamp": datetime.now(timezone.utc).isoformat()}
    if extra:
        data.update(extra)
    cooldown_file.write_text(json.dumps(data, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Orchestrator — top-level entry point
# ---------------------------------------------------------------------------


def run_testing_mode_cycle(checks: list[dict]) -> dict:
    """Main entry point called from heartbeat.py.

    Returns a check-dict compatible with the heartbeat checks list:
      {"name": "testing_mode", "ok": bool, "detail": str, ...}
    """
    cfg = get_testing_mode()

    # --- NORMAL mode: inactive, return immediately ---
    if cfg.mode == TestingMode.NORMAL:
        return {
            "name": "testing_mode",
            "ok": True,
            "detail": "inactive (mode=NORMAL)",
        }

    # --- Safety gates ---
    # Degradation tier check
    try:
        from utils.self_healing import should_allow_feature

        if not should_allow_feature("testing_mode"):
            return {
                "name": "testing_mode",
                "ok": True,
                "detail": "skipped: degradation tier blocks testing_mode",
            }
    except ImportError:
        pass

    cycle_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    run_id = uuid.uuid4().hex[:12]
    dry_run = cfg.dry_run
    anomalies_out: list[dict] = []

    # (B) Always write health snapshot as evidence
    health_entry = health_snapshot_from_checks(checks, cycle_id, run_id)
    write_evidence(health_entry)

    # (C+D) Canary execution — hourly cooldown
    canary_results: list[CanaryResult] = []
    if _check_cooldown(LAST_CANARY_RUN, CANARY_COOLDOWN_MINUTES):
        registry = load_canary_registry()
        recent = read_evidence(max_age_hours=6.0)
        selected = select_canary_tasks(registry, count=2, recent_evidence=recent)

        for task in selected:
            cr = execute_canary(task, cycle_id, run_id, dry_run=dry_run)
            canary_results.append(cr)

            # Write canary result as evidence
            write_evidence(
                EvidenceEntry(
                    cycle_id=cycle_id,
                    run_id=run_id,
                    timestamp=cr.timestamp,
                    entry_type="canary_result",
                    data=asdict(cr),
                    verdict=cr.verdict,
                )
            )

        _update_cooldown(
            LAST_CANARY_RUN,
            {
                "canary_count": len(canary_results),
                "dry_run": dry_run,
            },
        )
        print(f"[testing-mode] Executed {len(canary_results)} canaries (dry_run={dry_run})")

    # (G+H) Cycle summary + anomaly detection — 3-hour cooldown
    if _check_cooldown(LAST_CYCLE_SUMMARY, CYCLE_SUMMARY_COOLDOWN_MINUTES):
        evidence = read_evidence(max_age_hours=24.0)
        detected = detect_anomalies(evidence)
        anomalies_out = [asdict(a) for a in detected]
        _write_anomaly_history(detected)

        # Build cycle summary
        all_canaries = [e for e in evidence if e.get("entry_type") == "canary_result"]
        pass_count = sum(
            1
            for e in all_canaries
            if e.get("data", {}).get("verdict", e.get("verdict", "")) == CanaryVerdict.PASS.value
        )
        soft_count = sum(
            1
            for e in all_canaries
            if e.get("data", {}).get("verdict", e.get("verdict", "")) == CanaryVerdict.SOFT_FAIL.value
        )
        hard_count = sum(
            1
            for e in all_canaries
            if e.get("data", {}).get("verdict", e.get("verdict", "")) == CanaryVerdict.HARD_FAIL.value
        )

        summary = CycleSummary(
            cycle_id=cycle_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            canary_count=len(all_canaries),
            pass_count=pass_count,
            soft_fail_count=soft_count,
            hard_fail_count=hard_count,
            anomalies=anomalies_out,
            health_snapshot=health_entry.data,
            trend="stable" if not detected else "degraded",
        )

        # Write summary evidence
        write_evidence(
            EvidenceEntry(
                cycle_id=cycle_id,
                run_id=run_id,
                timestamp=summary.timestamp,
                entry_type="cycle_summary",
                data=asdict(summary),
            )
        )

        # Persist summary snapshot
        summary_file = CYCLE_SUMMARY_DIR / f"summary_{cycle_id}.json"
        summary_file.write_text(json.dumps(asdict(summary), indent=2) + "\n")

        _update_cooldown(
            LAST_CYCLE_SUMMARY,
            {
                "anomaly_count": len(detected),
                "canary_total": len(all_canaries),
                "pass_rate": round(pass_count / len(all_canaries) * 100, 1) if all_canaries else 0,
            },
        )

        if detected:
            print(f"[testing-mode] {len(detected)} anomalies detected")

    # (I) Deep maintenance — 12-hour cooldown
    if _check_cooldown(LAST_DEEP_MAINTENANCE, DEEP_MAINTENANCE_COOLDOWN_MINUTES):
        dm_report = run_deep_maintenance(dry_run=dry_run)
        _update_cooldown(
            LAST_DEEP_MAINTENANCE,
            {
                "stale_cleaned": dm_report["stale_cleaned"],
            },
        )
        if dm_report["stale_cleaned"]:
            print(f"[testing-mode] Deep maintenance: cleaned {dm_report['stale_cleaned']} stale files")

    # --- Build result ---
    has_hard_fail = any(cr.verdict == CanaryVerdict.HARD_FAIL.value for cr in canary_results)
    has_critical_anomaly = any(a.get("severity") == AnomalySeverity.CRITICAL.value for a in anomalies_out)

    mode_label = TestingMode(cfg.mode).name
    detail_parts = [f"mode={mode_label}"]
    if canary_results:
        verdicts = [cr.verdict for cr in canary_results]
        detail_parts.append(f"canaries={len(canary_results)} [{', '.join(verdicts)}]")
    if anomalies_out:
        detail_parts.append(f"anomalies={len(anomalies_out)}")

    return {
        "name": "testing_mode",
        "ok": not (has_hard_fail or has_critical_anomaly),
        "detail": "; ".join(detail_parts),
        "anomalies": anomalies_out,
        "canary_results": [asdict(cr) for cr in canary_results],
        "dry_run": dry_run,
    }
