"""System Health collector — services, tasks, logs, uptime."""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from novatrade.autonomy.collectors.base import BaseCollector
from novatrade.autonomy.schemas import DimensionScore, SubMetric

_SERVICES = [
    "novacore-watcher",
    "novacore-telegram",
    "novacore-telegram-notifier",
    "novatrade",
]


class SystemHealthCollector(BaseCollector):
    """Measures overall system health: services, orphaned tasks, uptime, errors."""

    async def collect(self) -> DimensionScore:
        warnings: list[str] = []
        sub_metrics: list[SubMetric] = []

        # --- service_status ---
        try:
            svc_score, svc_raw = await self._check_services()
            sub_metrics.append(
                SubMetric(
                    name="service_status",
                    value=self._safe_score(svc_score),
                    raw_value=svc_raw,
                    description="Systemd service liveness (25 pts each)",
                )
            )
        except Exception as exc:
            self.log.warning("service_status collection failed: %s", exc)
            warnings.append(f"service_status collection failed: {exc}")
            sub_metrics.append(SubMetric(name="service_status", value=0.0))

        # --- orphaned_tasks ---
        try:
            task_score, task_raw = self._check_orphaned_tasks()
            sub_metrics.append(
                SubMetric(
                    name="orphaned_tasks",
                    value=self._safe_score(task_score),
                    raw_value=task_raw,
                    description="Non-done TASKS/*.md older than 2h",
                )
            )
        except Exception as exc:
            self.log.warning("orphaned_tasks collection failed: %s", exc)
            warnings.append(f"orphaned_tasks collection failed: {exc}")
            sub_metrics.append(SubMetric(name="orphaned_tasks", value=0.0))

        # --- uptime_hours ---
        try:
            up_score, up_raw = self._check_uptime()
            sub_metrics.append(
                SubMetric(
                    name="uptime_hours",
                    value=self._safe_score(up_score),
                    raw_value=up_raw,
                    description="Continuous uptime derived from HEARTBEAT.md",
                )
            )
        except Exception as exc:
            self.log.warning("uptime_hours collection failed: %s", exc)
            warnings.append(f"uptime_hours collection failed: {exc}")
            sub_metrics.append(SubMetric(name="uptime_hours", value=0.0))

        # --- error_rate ---
        try:
            err_score, err_raw = self._check_error_rate()
            sub_metrics.append(
                SubMetric(
                    name="error_rate",
                    value=self._safe_score(err_score),
                    raw_value=err_raw,
                    description="ERROR lines in LOGS/ from last hour",
                )
            )
        except Exception as exc:
            self.log.warning("error_rate collection failed: %s", exc)
            warnings.append(f"error_rate collection failed: {exc}")
            sub_metrics.append(SubMetric(name="error_rate", value=0.0))

        # Dimension score = average of sub-metrics
        avg = sum(m.value for m in sub_metrics) / max(len(sub_metrics), 1)

        return DimensionScore(
            name="System Health",
            score=round(avg, 1),
            sub_metrics=sub_metrics,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # private helpers
    # ------------------------------------------------------------------

    async def _check_services(self) -> tuple[float, float]:
        """Return (score, active_count) for systemd services."""
        active = 0
        for svc in _SERVICES:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "systemctl",
                    "is-active",
                    svc,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                if stdout.decode().strip() == "active":
                    active += 1
            except (asyncio.TimeoutError, FileNotFoundError, OSError):
                # systemctl not available or timed out
                continue
        return active * 25.0, float(active)

    def _check_orphaned_tasks(self) -> tuple[float, float]:
        """Return (score, orphan_count)."""
        tasks_dir = Path(self.base_path) / "TASKS"
        if not tasks_dir.is_dir():
            return 100.0, 0.0

        cutoff = time.time() - 2 * 3600  # 2 hours ago
        orphans = 0
        for p in tasks_dir.glob("*.md"):
            name_lower = p.name.lower()
            if name_lower.endswith(".done") or name_lower.endswith(".failed"):
                continue
            try:
                if p.stat().st_mtime < cutoff:
                    orphans += 1
            except OSError:
                continue

        score = max(0.0, 100.0 - orphans * 20.0)
        return score, float(orphans)

    def _check_uptime(self) -> tuple[float, float]:
        """Return (score, hours) from HEARTBEAT.md last-update."""
        hb_path = Path(self.base_path) / "HEARTBEAT.md"
        if not hb_path.exists():
            return 0.0, 0.0

        try:
            text = hb_path.read_text(errors="replace")
        except OSError:
            return 0.0, 0.0

        # Try to parse a timestamp from the file (ISO format or epoch)
        last_ts = self._extract_timestamp(text)
        if last_ts is None:
            return 20.0, 0.0  # file exists but unparseable

        # Compare in naive UTC space since _extract_timestamp returns naive
        age_h = (datetime.now(timezone.utc).replace(tzinfo=None) - last_ts).total_seconds() / 3600.0
        # "uptime" = freshness of heartbeat
        if age_h < 1:
            score = 100.0
        elif age_h < 6:
            score = 80.0
        elif age_h < 12:
            score = 60.0
        elif age_h < 24:
            score = 40.0
        else:
            score = 20.0

        return score, round(age_h, 2)

    @staticmethod
    def _extract_timestamp(text: str) -> datetime | None:
        """Best-effort parse of the last ISO-like timestamp in text."""
        # Match ISO-8601 variants: 2026-03-25T12:34:56 or 2026-03-25 12:34:56
        pattern = r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
        matches = re.findall(pattern, text)
        if not matches:
            return None
        last = matches[-1].replace("T", " ")
        try:
            return datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    def _check_error_rate(self) -> tuple[float, float]:
        """Return (score, error_count) from LOGS/ last hour."""
        logs_dir = Path(self.base_path) / "LOGS"
        if not logs_dir.is_dir():
            return 100.0, 0.0

        cutoff = time.time() - 3600  # 1 hour ago
        errors = 0
        for log_file in logs_dir.iterdir():
            if not log_file.is_file():
                continue
            # Only scan recently modified files
            try:
                if log_file.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            try:
                # H4: read only the last 64KB to avoid reading multi-MB files
                tail_size = 65536
                file_size = log_file.stat().st_size
                with open(log_file, errors="replace") as fh:
                    if file_size > tail_size:
                        fh.seek(file_size - tail_size)
                        fh.readline()  # skip partial first line
                    for line in fh:
                        if "ERROR" in line:
                            errors += 1
            except OSError:
                continue

        score = max(0.0, 100.0 - errors * 5.0)
        return score, float(errors)
