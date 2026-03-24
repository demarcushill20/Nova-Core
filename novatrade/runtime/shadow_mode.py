"""Shadow Mode — Phase 6.1 of NovaTrade Full-Python Implementation.

Runs the full-Python LiveLoop and the TradingView WebhookServer in parallel,
capturing signals from both pipelines. Generates hourly comparison reports
to validate signal parity before cutting over from TradingView.

Architecture:
    - LiveLoop runs in shadow mode (DryRunAdapter, no real orders)
    - WebhookServer continues to receive real TradingView alerts
    - Both emit signal records to JSONL log files
    - ShadowComparator matches signals and generates comparison reports
    - Target: >=95% match rate over 7 days before cutover
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from novatrade.runtime.live_loop import LiveLoop
from novatrade.runtime.webhook_server import WebhookState
from novatrade.strategy.live_engine import LiveSignal

log = logging.getLogger("novatrade.runtime.shadow_mode")


# ---------------------------------------------------------------------------
# Signal record
# ---------------------------------------------------------------------------


@dataclass
class ShadowSignalRecord:
    """A normalized signal record from either pipeline."""

    source: Literal["live", "tv"]
    timestamp: float  # unix epoch when signal was captured
    symbol: str
    side: str  # BUY/SELL or LONG/SHORT
    action: str  # ENTRY/EXIT/MODIFY_SL/CANCEL_PENDING/PENDING_FILL
    price: float
    bar_time: float  # bar close timestamp for matching
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        """Serialize to a single JSON line."""
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> ShadowSignalRecord:
        """Deserialize from a JSON line."""
        d = json.loads(line)
        return cls(**d)

    @classmethod
    def from_live_signal(cls, signal: LiveSignal) -> ShadowSignalRecord:
        """Create a record from a LiveSignal (full-Python pipeline)."""
        price = signal.entry_price or signal.exit_price or signal.new_stop or 0.0
        action = signal.signal_type.value
        return cls(
            source="live",
            timestamp=time.time(),
            symbol=signal.symbol,
            side=signal.side,
            action=action,
            price=price,
            bar_time=signal.metadata.get("bar_close_time", signal.timestamp) if signal.metadata else signal.timestamp,
            metadata=dict(signal.metadata) if signal.metadata else {},
        )

    @classmethod
    def from_tv_alert(cls, payload: dict[str, Any]) -> ShadowSignalRecord:
        """Create a record from a TradingView webhook payload."""
        return cls(
            source="tv",
            timestamp=time.time(),
            symbol=payload.get("symbol", "UNKNOWN"),
            side=payload.get("side", payload.get("action", "UNKNOWN")),
            action=payload.get("signal_type", payload.get("action", "UNKNOWN")),
            price=float(payload.get("entry_price", payload.get("exit_price", 0.0))),
            bar_time=float(payload.get("bar_close_time", 0.0)),
            metadata={
                k: v
                for k, v in payload.items()
                if k not in ("symbol", "side", "action", "signal_type", "entry_price", "exit_price", "bar_close_time")
            },
        )


# ---------------------------------------------------------------------------
# Signal logger
# ---------------------------------------------------------------------------


class ShadowSignalLogger:
    """Append-only JSONL logger for shadow signals."""

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = log_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._live_path = self._log_dir / "shadow_live_signals.jsonl"
        self._tv_path = self._log_dir / "shadow_tv_signals.jsonl"

    @property
    def live_path(self) -> Path:
        """Path to the live signals JSONL file."""
        return self._live_path

    @property
    def tv_path(self) -> Path:
        """Path to the TV signals JSONL file."""
        return self._tv_path

    def log_live_signal(self, record: ShadowSignalRecord) -> None:
        """Append a live-pipeline signal record to the JSONL log."""
        with open(self._live_path, "a") as f:
            f.write(record.to_json() + "\n")

    def log_tv_signal(self, record: ShadowSignalRecord) -> None:
        """Append a TV-pipeline signal record to the JSONL log."""
        with open(self._tv_path, "a") as f:
            f.write(record.to_json() + "\n")

    def read_live_signals(self, since: float | None = None) -> list[ShadowSignalRecord]:
        """Read live signal records from the JSONL file.

        Args:
            since: If provided, only return signals with timestamp >= since.
        """
        return self._read_signals(self._live_path, since=since)

    def read_tv_signals(self, since: float | None = None) -> list[ShadowSignalRecord]:
        """Read TV signal records from the JSONL file.

        Args:
            since: If provided, only return signals with timestamp >= since.
        """
        return self._read_signals(self._tv_path, since=since)

    @staticmethod
    def _read_signals(path: Path, *, since: float | None = None) -> list[ShadowSignalRecord]:
        """Read signal records from a JSONL file.

        Args:
            since: If provided, only return signals with timestamp >= since.
        """
        if not path.exists():
            return []
        records: list[ShadowSignalRecord] = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        record = ShadowSignalRecord.from_json(line)
                        if since is not None and record.timestamp < since:
                            continue
                        records.append(record)
                    except (json.JSONDecodeError, TypeError, KeyError):
                        log.warning("skipping malformed JSONL line in %s", path)
        return records


# ---------------------------------------------------------------------------
# Comparison report
# ---------------------------------------------------------------------------


@dataclass
class ShadowReport:
    """Result of comparing live vs TV signals over a window."""

    matched: list[tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    missed_by_live: list[dict[str, Any]] = field(default_factory=list)
    extra_by_live: list[dict[str, Any]] = field(default_factory=list)
    timing_deltas: list[float] = field(default_factory=list)
    match_rate: float = 0.0
    report_time: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the report to a plain dict."""
        return {
            "matched_count": len(self.matched),
            "missed_by_live_count": len(self.missed_by_live),
            "extra_by_live_count": len(self.extra_by_live),
            "match_rate": round(self.match_rate, 4),
            "timing_deltas": self.timing_deltas,
            "avg_timing_delta": (
                round(sum(self.timing_deltas) / len(self.timing_deltas), 2) if self.timing_deltas else 0.0
            ),
            "report_time": self.report_time,
            "matched": self.matched,
            "missed_by_live": self.missed_by_live,
            "extra_by_live": self.extra_by_live,
        }

    def to_summary(self) -> str:
        """Human-readable summary of the comparison."""
        total_tv = len(self.matched) + len(self.missed_by_live)
        avg_delta = round(sum(self.timing_deltas) / len(self.timing_deltas), 2) if self.timing_deltas else 0.0
        lines = [
            f"=== Shadow Mode Report ({time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(self.report_time))}) ===",
            f"TV signals:        {total_tv}",
            f"Live signals:      {len(self.matched) + len(self.extra_by_live)}",
            f"Matched:           {len(self.matched)}",
            f"Missed by live:    {len(self.missed_by_live)}",
            f"Extra by live:     {len(self.extra_by_live)}",
            f"Match rate:        {self.match_rate:.1%}",
            f"Avg timing delta:  {avg_delta}s",
        ]
        if self.match_rate >= 0.95:
            lines.append("Status: PASS (>=95%)")
        else:
            lines.append("Status: FAIL (<95%)")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Comparator
# ---------------------------------------------------------------------------


class ShadowComparator:
    """Match live signals against TV signals within a time tolerance window."""

    def __init__(self, tolerance_seconds: float = 300.0) -> None:
        self._tolerance = tolerance_seconds

    def compare(
        self,
        live_signals: list[ShadowSignalRecord],
        tv_signals: list[ShadowSignalRecord],
    ) -> ShadowReport:
        """Compare live and TV signal lists, returning a ShadowReport.

        Matching logic: signals match if same symbol + same normalized side +
        bar_time within tolerance. Each signal can match at most once.
        """
        matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
        timing_deltas: list[float] = []
        used_live: set[int] = set()

        # For each TV signal, find the best matching live signal
        unmatched_tv: list[ShadowSignalRecord] = []

        for tv_sig in tv_signals:
            best_idx: int | None = None
            best_delta = float("inf")

            for i, live_sig in enumerate(live_signals):
                if i in used_live:
                    continue

                if not self._signals_compatible(live_sig, tv_sig):
                    continue

                delta = abs(live_sig.bar_time - tv_sig.bar_time)
                if delta <= self._tolerance and delta < best_delta:
                    best_idx = i
                    best_delta = delta

            if best_idx is not None:
                used_live.add(best_idx)
                matched.append((asdict(live_signals[best_idx]), asdict(tv_sig)))
                # Timing delta: how much later/earlier did live fire vs TV
                timing_deltas.append(live_signals[best_idx].timestamp - tv_sig.timestamp)
            else:
                unmatched_tv.append(tv_sig)

        # Extra live signals: those not matched to any TV signal
        extra_live = [asdict(live_signals[i]) for i in range(len(live_signals)) if i not in used_live]

        # Match rate: fraction of TV signals that were matched
        total_tv = len(tv_signals)
        match_rate = len(matched) / total_tv if total_tv > 0 else 0.0

        return ShadowReport(
            matched=matched,
            missed_by_live=[asdict(s) for s in unmatched_tv],
            extra_by_live=extra_live,
            timing_deltas=timing_deltas,
            match_rate=match_rate,
            report_time=time.time(),
        )

    def _signals_compatible(self, live: ShadowSignalRecord, tv: ShadowSignalRecord) -> bool:
        """Check whether two signals refer to the same event."""
        if live.symbol != tv.symbol:
            return False
        # Normalize sides: LONG<->BUY, SHORT<->SELL
        if _normalize_side(live.side) != _normalize_side(tv.side):
            return False
        # Action must match: ENTRY != EXIT (prevents false-matching)
        return live.action.upper() == tv.action.upper()


def _normalize_side(side: str) -> str:
    """Normalize side to BUY/SELL for comparison."""
    s = side.upper()
    if s in ("LONG", "BUY"):
        return "BUY"
    if s in ("SHORT", "SELL"):
        return "SELL"
    return s


# ---------------------------------------------------------------------------
# Cutover decision gate (Phase 6.3)
# ---------------------------------------------------------------------------


@dataclass
class CutoverGate:
    """Formal criteria for switching from TradingView to full-Python.

    All conditions must be met before the operator approves cutover.
    """

    shadow_days: int = 0
    match_rate: float = 0.0
    matched_count: int = 0  # total matched signals across all reports
    min_signals: int = 10  # minimum matched signals before match_rate is meaningful
    missed_bar_closes: int = 0
    false_positive_blocks: int = 0
    failure_domains_observed: int = 0  # out of 8 total feed health domains
    operator_approved: bool = False

    @property
    def ready(self) -> bool:
        """Check if all cutover criteria are met."""
        return (
            self.shadow_days >= 7
            and self.matched_count >= self.min_signals
            and self.match_rate >= 0.95
            and self.missed_bar_closes == 0
            and self.false_positive_blocks == 0
            and self.failure_domains_observed >= 8
            and self.operator_approved
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize gate state to a dict."""
        return {
            "shadow_days": self.shadow_days,
            "match_rate": round(self.match_rate, 4),
            "matched_count": self.matched_count,
            "min_signals": self.min_signals,
            "missed_bar_closes": self.missed_bar_closes,
            "false_positive_blocks": self.false_positive_blocks,
            "failure_domains_observed": self.failure_domains_observed,
            "operator_approved": self.operator_approved,
            "ready": self.ready,
        }

    def summary(self) -> str:
        """Human-readable checklist of cutover criteria."""

        def _check(ok: bool) -> str:
            return "[x]" if ok else "[ ]"

        sd = self.shadow_days
        mc = self.matched_count
        ms = self.min_signals
        mr = self.match_rate
        mb = self.missed_bar_closes
        fp = self.false_positive_blocks
        fd = self.failure_domains_observed
        oa = self.operator_approved
        lines = [
            "=== Cutover Decision Gate ===",
            f"{_check(sd >= 7)} Shadow days >= 7 (current: {sd})",
            f"{_check(mc >= ms)} Matched signals >= {ms} (current: {mc})",
            f"{_check(mr >= 0.95)} Match rate >= 95% (current: {mr:.1%})",
            f"{_check(mb == 0)} Zero missed bar closes (current: {mb})",
            f"{_check(fp == 0)} Zero false-positive blocks (current: {fp})",
            f"{_check(fd >= 8)} All 8 failure domains observed ({fd}/8)",
            f"{_check(oa)} Operator approved (current: {oa})",
            "",
            f"Verdict: {'READY FOR CUTOVER' if self.ready else 'NOT READY'}",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Shadow mode runner
# ---------------------------------------------------------------------------


class ShadowModeRunner:
    """Run LiveLoop and WebhookServer in parallel, comparing signals.

    Works entirely through composition — does NOT modify LiveLoop or
    WebhookServer internals. Captures signals by wrapping the execute/
    process_alert methods on the injected objects.
    """

    def __init__(
        self,
        live_loop: LiveLoop,
        webhook_state: WebhookState,
        *,
        log_dir: Path,
        report_interval: float = 3600.0,
        match_rate_alert_threshold: float = 0.90,
    ) -> None:
        self._live_loop = live_loop
        self._webhook_state = webhook_state
        self._log_dir = log_dir
        self._report_interval = report_interval
        self._match_rate_alert_threshold = match_rate_alert_threshold

        self._signal_logger = ShadowSignalLogger(log_dir)
        self._comparator = ShadowComparator()
        self._latest_report: ShadowReport | None = None
        self._running = False
        self._stop_event: asyncio.Event | None = None
        self._last_report_time: float = time.time()

        # Install capture hooks (wrapping, not modifying originals)
        self._install_live_hook()
        self._install_tv_hook()

    def _install_live_hook(self) -> None:
        """Wrap LiveLoop's agent execute method to capture live signals."""
        agent = self._live_loop._live_agent
        original_execute = agent.execute

        async def _wrapped_execute(signal: LiveSignal) -> Any:
            try:
                record = ShadowSignalRecord.from_live_signal(signal)
                self._signal_logger.log_live_signal(record)
                log.debug("shadow: captured live signal %s %s %s", signal.symbol, signal.side, signal.signal_type.value)
            except Exception:
                log.exception("shadow: failed to log live signal")
            return await original_execute(signal)

        agent.execute = _wrapped_execute  # type: ignore[assignment]

    def _install_tv_hook(self) -> None:
        """Wrap WebhookState's agent process_alert to capture TV signals."""
        ws_agent = self._webhook_state.agent
        if ws_agent is None:
            log.warning("shadow: webhook agent is None, TV capture hook not installed")
            return

        original_process = ws_agent.process_alert

        async def _wrapped_process(payload: dict[str, Any]) -> Any:
            try:
                record = ShadowSignalRecord.from_tv_alert(payload)
                self._signal_logger.log_tv_signal(record)
                log.debug("shadow: captured TV signal %s %s", payload.get("symbol"), payload.get("action"))
            except Exception:
                log.exception("shadow: failed to log TV signal")
            return await original_process(payload)

        ws_agent.process_alert = _wrapped_process  # type: ignore[assignment]

    # -- Public API --------------------------------------------------------

    async def run(self) -> None:
        """Start the shadow mode runner.

        Runs the LiveLoop, and periodically generates comparison reports.
        The WebhookServer is assumed to be running externally (e.g., via
        uvicorn in the same process).
        """
        self._running = True
        self._stop_event = asyncio.Event()

        log.info(
            "shadow mode starting: log_dir=%s report_interval=%.0fs threshold=%.0f%%",
            self._log_dir,
            self._report_interval,
            self._match_rate_alert_threshold * 100,
        )

        try:
            await asyncio.gather(
                self._live_loop.run(),
                self._report_loop(),
            )
        finally:
            self._running = False
            log.info("shadow mode stopped")

    def stop(self) -> None:
        """Signal graceful shutdown."""
        self._running = False
        self._live_loop.stop()
        if self._stop_event is not None:
            self._stop_event.set()
        log.info("shadow mode stop requested")

    def get_latest_report(self) -> ShadowReport | None:
        """Return the most recent comparison report, or None."""
        return self._latest_report

    @property
    def signal_logger(self) -> ShadowSignalLogger:
        """Access the signal logger for external inspection."""
        return self._signal_logger

    # -- Report loop -------------------------------------------------------

    async def _report_loop(self) -> None:
        """Periodically compare signals and write reports."""
        report_dir = self._log_dir / "shadow_reports"
        report_dir.mkdir(parents=True, exist_ok=True)

        while self._running:
            stop_evt = self._stop_event
            if stop_evt is None:
                break

            try:
                await asyncio.wait_for(stop_evt.wait(), timeout=self._report_interval)
                # Event was set — exit
                break
            except asyncio.TimeoutError:
                # Normal timeout — generate report
                pass

            try:
                live_signals = self._signal_logger.read_live_signals(since=self._last_report_time)
                tv_signals = self._signal_logger.read_tv_signals(since=self._last_report_time)
                self._last_report_time = time.time()

                report = self._comparator.compare(live_signals, tv_signals)
                self._latest_report = report

                # Write report to file
                ts_str = time.strftime("%Y%m%d_%H%M%S", time.gmtime(report.report_time))
                report_path = report_dir / f"shadow_report_{ts_str}.json"
                report_path.write_text(json.dumps(report.to_dict(), indent=2))

                log.info(
                    "shadow report: match_rate=%.1f%% matched=%d missed=%d extra=%d -> %s",
                    report.match_rate * 100,
                    len(report.matched),
                    len(report.missed_by_live),
                    len(report.extra_by_live),
                    report_path,
                )

                if report.match_rate < self._match_rate_alert_threshold and (
                    len(report.matched) + len(report.missed_by_live) > 0
                ):
                    log.warning(
                        "SHADOW ALERT: match rate %.1f%% below threshold %.1f%%",
                        report.match_rate * 100,
                        self._match_rate_alert_threshold * 100,
                    )

            except Exception:
                log.exception("shadow report generation failed")

        log.info("shadow report loop stopped")


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


async def _main() -> None:
    """Async entrypoint for shadow mode."""
    from novatrade.runtime.runner import build_live_stack, build_stack

    log.info("shadow mode: building webhook stack...")
    ws, monitor_loop, readiness = build_stack()

    log.info("shadow mode: building live stack (shadow=True)...")
    live_loop = await build_live_stack(shadow=True)

    log_dir = Path("LOGS/shadow")
    runner = ShadowModeRunner(
        live_loop=live_loop,
        webhook_state=ws,
        log_dir=log_dir,
    )

    # Run webhook server + shadow mode runner concurrently
    import uvicorn

    from novatrade.runtime.webhook_server import create_app

    app = create_app(ws)
    config = uvicorn.Config(app, host="0.0.0.0", port=8877, log_level="info", access_log=False)  # noqa: S104
    server = uvicorn.Server(config)

    monitor_task = asyncio.create_task(monitor_loop.start())
    try:
        await asyncio.gather(
            runner.run(),
            server.serve(),
        )
    finally:
        runner.stop()
        monitor_loop.stop()
        await monitor_task


def main() -> None:
    """CLI entrypoint for shadow mode."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    asyncio.run(_main())


if __name__ == "__main__":
    main()
