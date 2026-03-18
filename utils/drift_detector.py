"""Drift detection for NovaCore task execution.

Analyzes structured JSONL logs to detect behavioral drift:
- Declining task success rate
- Increasing average task duration
- Rising error rate
- Contract failure spikes

Computes metrics over sliding windows and compares against baselines.
Optionally pushes drift scores to Langfuse.

Usage:
    from utils.drift_detector import detect_drift, update_baseline
    report = detect_drift()
    if report["drift_detected"]:
        print(report["signals"])
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_LOGS_DIR = Path("LOGS")
_STATE_DIR = Path("STATE")
_JSONL_FILE = _LOGS_DIR / "structured.jsonl"
_BASELINE_FILE = _STATE_DIR / "drift_baseline.json"

# Drift thresholds
_SUCCESS_RATE_DROP = 0.15  # 15% drop from baseline triggers drift
_DURATION_SPIKE = 5.0  # 5x increase in avg duration triggers drift
# Note: prior value of 1.5x caused constant false positives because task
# durations are bimodal — quick file ops (~400ms) vs. agent tasks (30-300s).
# A single heavy session (reviews, multi-agent work) would spike 1000x.
# 5x catches genuine slowdowns while tolerating normal workload variation.
_ERROR_RATE_SPIKE = 2.0  # 2x increase in error rate triggers drift
_MIN_SAMPLES = 5  # need at least this many tasks to compute metrics


@dataclass
class DriftSignal:
    metric: str
    baseline: float
    current: float
    threshold: float
    severity: str  # "warning" or "critical"
    message: str


@dataclass
class DriftReport:
    drift_detected: bool = False
    signals: list[DriftSignal] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    window_tasks: int = 0
    baseline_tasks: int = 0
    computed_at: str = ""


def _load_events(hours: float = 24.0) -> list[dict]:
    """Load structured events from the last N hours."""
    if not _JSONL_FILE.exists():
        return []
    cutoff = time.time() - (hours * 3600)
    events = []
    try:
        for line in _JSONL_FILE.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
                ts_str = evt.get("ts", "")
                if ts_str:
                    ts = datetime.fromisoformat(ts_str).timestamp()
                    if ts >= cutoff:
                        events.append(evt)
            except (json.JSONDecodeError, ValueError):
                continue
    except OSError:
        pass
    return events


def _compute_metrics(events: list[dict]) -> dict:
    """Compute task-level metrics from structured events."""
    completed = [e for e in events if e.get("event") == "task.completed"]
    failed = [e for e in events if e.get("event") == "task.failed"]
    errors = [e for e in events if e.get("level") in ("error", "ERROR")]
    reflexions = [e for e in events if e.get("event") == "task.reflexion_start"]
    all_events = len(events)

    total_tasks = len(completed) + len(failed)
    success_rate = len(completed) / total_tasks if total_tasks > 0 else 1.0
    error_rate = len(errors) / all_events if all_events > 0 else 0.0

    durations = []
    for e in completed + failed:
        d = e.get("data", {})
        if isinstance(d, dict) and "duration_ms" in d:
            durations.append(d["duration_ms"])

    avg_duration = sum(durations) / len(durations) if durations else 0.0

    return {
        "total_tasks": total_tasks,
        "completed": len(completed),
        "failed": len(failed),
        "success_rate": success_rate,
        "error_rate": error_rate,
        "avg_duration_ms": avg_duration,
        "reflexion_count": len(reflexions),
        "total_events": all_events,
    }


def _load_baseline() -> dict | None:
    """Load the stored baseline metrics."""
    if not _BASELINE_FILE.exists():
        return None
    try:
        return json.loads(_BASELINE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def update_baseline(hours: float = 168.0) -> dict:
    """Recompute and store baseline metrics from the last N hours (default: 7 days).

    Call this periodically (e.g., weekly) or after known-good periods.
    """
    events = _load_events(hours)
    metrics = _compute_metrics(events)
    metrics["computed_at"] = datetime.now(timezone.utc).isoformat()
    metrics["window_hours"] = hours

    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    _BASELINE_FILE.write_text(json.dumps(metrics, indent=2))
    return metrics


def detect_drift(window_hours: float = 24.0) -> DriftReport:
    """Detect behavioral drift by comparing recent metrics against baseline.

    Args:
        window_hours: How far back to look for recent metrics (default: 24h).

    Returns:
        DriftReport with any detected signals.
    """
    report = DriftReport(computed_at=datetime.now(timezone.utc).isoformat())

    baseline = _load_baseline()
    events = _load_events(window_hours)
    current = _compute_metrics(events)
    report.metrics = current
    report.window_tasks = current["total_tasks"]

    if baseline is None:
        # No baseline yet — store current as baseline and return clean
        update_baseline(window_hours)
        return report

    report.baseline_tasks = baseline.get("total_tasks", 0)

    # Need minimum samples for meaningful comparison
    if current["total_tasks"] < _MIN_SAMPLES:
        return report

    # Check success rate drift
    bl_success = baseline.get("success_rate", 1.0)
    if bl_success > 0 and (bl_success - current["success_rate"]) > _SUCCESS_RATE_DROP:
        severity = "critical" if (bl_success - current["success_rate"]) > 0.3 else "warning"
        report.signals.append(
            DriftSignal(
                metric="success_rate",
                baseline=bl_success,
                current=current["success_rate"],
                threshold=_SUCCESS_RATE_DROP,
                severity=severity,
                message=f"Success rate dropped from {bl_success:.0%} to {current['success_rate']:.0%}",
            )
        )

    # Check duration drift
    bl_duration = baseline.get("avg_duration_ms", 0)
    if bl_duration > 0 and current["avg_duration_ms"] > bl_duration * _DURATION_SPIKE:
        ratio = current["avg_duration_ms"] / bl_duration
        severity = "critical" if ratio > 3.0 else "warning"
        report.signals.append(
            DriftSignal(
                metric="avg_duration_ms",
                baseline=bl_duration,
                current=current["avg_duration_ms"],
                threshold=_DURATION_SPIKE,
                severity=severity,
                message=(
                    f"Avg task duration spiked {ratio:.1f}x "
                    f"(baseline: {bl_duration:.0f}ms, current: {current['avg_duration_ms']:.0f}ms)"
                ),
            )
        )

    # Check error rate drift
    bl_errors = baseline.get("error_rate", 0)
    if bl_errors > 0 and current["error_rate"] > bl_errors * _ERROR_RATE_SPIKE:
        ratio = current["error_rate"] / bl_errors
        severity = "critical" if ratio > 5.0 else "warning"
        report.signals.append(
            DriftSignal(
                metric="error_rate",
                baseline=bl_errors,
                current=current["error_rate"],
                threshold=_ERROR_RATE_SPIKE,
                severity=severity,
                message=(
                    f"Error rate spiked {ratio:.1f}x (baseline: {bl_errors:.2%}, current: {current['error_rate']:.2%})"
                ),
            )
        )

    report.drift_detected = len(report.signals) > 0

    # Push drift scores to Langfuse if detected
    if report.drift_detected:
        _push_drift_to_langfuse(report)

    return report


def _push_drift_to_langfuse(report: DriftReport) -> None:
    """Best-effort push of drift signals to Langfuse as events."""
    try:
        from utils.langfuse_tracing import trace_event

        for signal in report.signals:
            trace_event(
                f"drift.{signal.metric}",
                component="drift_detector",
                level="warn" if signal.severity == "warning" else "error",
                baseline=signal.baseline,
                current=signal.current,
                message=signal.message,
            )
    except Exception:
        pass


def get_drift_summary() -> str:
    """Get a human-readable drift summary for dashboards or Telegram."""
    report = detect_drift()
    if not report.drift_detected:
        return (
            f"No drift detected. {report.window_tasks} tasks in window, "
            f"success rate: {report.metrics.get('success_rate', 0):.0%}"
        )

    lines = [f"DRIFT DETECTED ({len(report.signals)} signal(s)):"]
    for s in report.signals:
        icon = "!!" if s.severity == "critical" else "!"
        lines.append(f"  {icon} {s.message}")
    lines.append(f"  Window: {report.window_tasks} tasks | Baseline: {report.baseline_tasks} tasks")
    return "\n".join(lines)
