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
    "novacore-novatrade",
]


class SystemHealthCollector(BaseCollector):
    """Measures overall system health: services, orphaned tasks, uptime, errors."""

    # Class-level cache for last-known-good service status.
    # Survives transient "can't start new thread" / timeout failures.
    _last_good_svc: tuple[float, float] | None = None

    async def collect(self) -> DimensionScore:
        warnings: list[str] = []
        sub_metrics: list[SubMetric] = []

        # --- service_status ---
        try:
            svc_score, svc_raw = await self._check_services()
            # Cache successful result for fallback on transient failures
            SystemHealthCollector._last_good_svc = (svc_score, svc_raw)
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
            # Use last-known-good value instead of 0.0 on transient errors
            cached = SystemHealthCollector._last_good_svc
            if cached is not None:
                self.log.info("Using cached service_status: %s", cached[0])
                sub_metrics.append(
                    SubMetric(
                        name="service_status",
                        value=self._safe_score(cached[0]),
                        raw_value=cached[1],
                        description="Systemd service liveness (cached fallback)",
                    )
                )
            else:
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

        # Compute confidence based on data freshness
        confidence = self._compute_confidence(sub_metrics, warnings)

        return DimensionScore(
            name="System Health",
            score=round(avg, 1),
            confidence=confidence,
            sub_metrics=sub_metrics,
            warnings=warnings,
        )

    def _compute_confidence(self, sub_metrics: list[SubMetric], warnings: list[str]) -> float:
        """Compute confidence based on sub-metric collection success.

        System health data is mostly real-time (systemctl, file checks),
        so confidence is primarily about whether checks succeeded.
        """
        if not sub_metrics:
            return 0.1

        # Count how many sub-metrics have non-zero values (successful checks)
        successful = sum(1 for m in sub_metrics if m.value > 0)
        total = len(sub_metrics)

        if successful == total:
            return 1.0
        elif successful > total * 0.5:
            return 0.8
        elif successful > 0:
            return 0.6

        # All sub-metrics zero — likely collection failures
        if warnings:
            return 0.3
        return 0.1

    # ------------------------------------------------------------------
    # private helpers
    # ------------------------------------------------------------------

    async def _check_services(self) -> tuple[float, float]:
        """Return (score, active_count) for systemd services."""
        active = 0
        checked = 0  # tracks how many checks got a systemctl response
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
                checked += 1
                if stdout.decode().strip() == "active":
                    active += 1
            except (asyncio.TimeoutError, FileNotFoundError, OSError):
                # systemctl not available or timed out
                continue
            except Exception as svc_exc:
                # Catch threading/resource errors per-service instead of
                # letting them abort the entire check
                self.log.debug("service check for %s failed: %s", svc, svc_exc)
                continue

        # If no check completed (all threw exceptions), this is a transient
        # resource issue (e.g. Errno 11) — not actual service failures.
        # Use cached fallback to avoid false-positive score regression.
        if checked == 0:
            cached = SystemHealthCollector._last_good_svc
            if cached is not None:
                self.log.warning(
                    "All service checks failed (resource issue) — using cached score: %s",
                    cached[0],
                )
                return cached
            # No cache — bubble up so top-level handler treats as failure
            raise OSError("All service checks failed — no systemctl response")

        return active * 25.0, float(active)

    def _check_orphaned_tasks(self) -> tuple[float, float]:
        """Return (score, orphan_count)."""
        tasks_dir = Path(self.base_path) / "TASKS"
        if not tasks_dir.is_dir():
            return 100.0, 0.0

        # Lifecycle suffixes that are NOT orphans
        _SKIP_SUFFIXES = (".done", ".failed", ".inprogress", ".partial", ".cleaned")
        # Prefixes for scheduled shifts (not task orphans)
        _SKIP_PREFIXES = ("shift_", "shift_gen_")

        cutoff = time.time() - 2 * 3600  # 2 hours ago
        orphans = 0
        for p in tasks_dir.glob("*.md*"):
            name_lower = p.name.lower()
            # Skip known lifecycle states
            if any(name_lower.endswith(s) for s in _SKIP_SUFFIXES):
                continue
            # Skip scheduled shift files
            if any(name_lower.startswith(s) for s in _SKIP_PREFIXES):
                continue
            # Skip retry files (e.g. task__retry1.md) — active retries, not orphans
            if "__retry" in name_lower:
                continue
            try:
                if p.stat().st_mtime < cutoff:
                    orphans += 1
            except OSError:
                continue

        # 10 pts per orphan (was 20) — avoids zeroing the metric on transient
        # task backlogs while still signalling when cleanup is needed.
        score = max(0.0, 100.0 - orphans * 10.0)
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
        """Best-effort parse of heartbeat timestamp from text.

        Prioritises the 'Last check:' line, then falls back to the first
        ISO-like timestamp found (not the last — service 'since' dates are
        always later in the file and much older).
        """
        # Primary: look for the "Last check:" line specifically
        lc_match = re.search(r"Last check:\s*(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})", text)
        if lc_match:
            ts_str = lc_match.group(1).replace("T", " ")
            try:
                return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass

        # Fallback: first ISO timestamp in the file (not last!)
        pattern = r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
        matches = re.findall(pattern, text)
        if not matches:
            return None
        first = matches[0].replace("T", " ")
        try:
            return datetime.strptime(first, "%Y-%m-%d %H:%M:%S")
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
                        # Only match structured log-level ERROR tokens.
                        # Patterns: "[ERROR]", or a timestamp prefix
                        # followed by ERROR (e.g. "2026-03-26 12:00:00 ERROR").
                        # Skip prose/markdown/summary lines entirely.
                        stripped = line.lstrip()
                        if not stripped:
                            continue
                        # Skip obvious non-log lines (markdown, bullets, etc.)
                        if stripped[0] in ("*", "-", "#", "`", ">"):
                            continue
                        if re.match(r"\d+\.\s", stripped):
                            continue
                        # Strip backtick-quoted content before matching
                        # (avoids false positives from prose like `[ERROR]`)
                        clean = re.sub(r"`[^`]*`", "", line)
                        # Require structured log-level indicators:
                        # "[ERROR]" bracket token, or timestamp-prefixed ERROR
                        if "[ERROR]" in clean or re.match(
                            r"\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2}\S*\s+ERROR\b",
                            stripped,
                        ):
                            errors += 1
            except OSError:
                continue

        score = max(0.0, 100.0 - errors * 5.0)
        return score, float(errors)
