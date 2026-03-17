"""Runtime runner — Phase 8 main entrypoint.

Wires together:
  TradingView webhook → Trading Agent → Risk Engine → Adapter → Evidence
  + OpsMonitor loop → Reconciliation → Action execution → Alerts/Summary

This is the minimal operational shell for the IRB demo-run stack.
It does NOT launch live trading — it provides the infrastructure to
run controlled dry-run tests and prepares for final demo activation.

Usage::

    # Dry-run mode (default, safe)
    python -m novatrade.runtime.runner

    # With real adapter (requires MetaApi credentials)
    NOVATRADE_DRY_RUN=false python -m novatrade.runtime.runner

    # Custom port and interval
    NOVATRADE_PORT=8080 NOVATRADE_MONITOR_INTERVAL=30 python -m novatrade.runtime.runner
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import uvicorn

from novatrade.config import NovaTradeCfg
from novatrade.execution.trading_agent import TradingAgent
from novatrade.models import AccountState
from novatrade.monitor.ops_monitor import OpsMonitor
from novatrade.risk.risk_engine import RiskEngine
from novatrade.runtime.dry_run import DryRunAdapter
from novatrade.runtime.monitor_loop import MonitorLoop
from novatrade.runtime.webhook_server import WebhookState, create_app
from novatrade.validation.evidence import EvidenceRecorder

log = logging.getLogger("novatrade.runtime.runner")


def build_stack(
    cfg: NovaTradeCfg | None = None,
) -> tuple[WebhookState, MonitorLoop]:
    """Build the full NovaTrade runtime stack.

    Returns the webhook state (for the FastAPI app) and the monitor loop.
    In dry-run mode, uses the DryRunAdapter automatically.
    """
    if cfg is None:
        cfg = NovaTradeCfg.load()

    log.info(
        "building stack: mode=%s dry_run=%s symbols=%s",
        cfg.mode.value,
        cfg.dry_run,
        cfg.symbols,
    )

    # --- Adapter ---
    # Phase 8 always uses DryRunAdapter regardless of cfg.dry_run.
    # The DryRunAdapter itself provides the safety net — no real broker
    # operations occur.  We set cfg.dry_run=False so the pre-trade gate
    # allows orders through (the adapter intercepts them safely).
    is_dry_run = cfg.dry_run
    adapter = DryRunAdapter(inner=None)
    if is_dry_run:
        log.info("using DryRunAdapter (no real broker connection)")
    else:
        log.warning(
            "dry_run=False requested but Phase 8 enforces DryRunAdapter. "
            "Set NOVATRADE_DRY_RUN=true or wait for final launch phase."
        )
    # Allow orders through the risk gate — DryRunAdapter is the safety net
    cfg.dry_run = False

    # --- Evidence ---
    recorder = EvidenceRecorder(
        path=cfg.data_dir / "evidence.jsonl",
        campaign="irb-phase8-dry-run" if cfg.dry_run else "irb-demo-run",
    )

    # --- Risk Engine ---
    risk_engine = RiskEngine(cfg)
    # Initialize with simulated account for dry-run
    initial_account = AccountState(
        balance=100_000.0,
        equity=100_000.0,
        mode=cfg.mode,
    )
    risk_engine.initialize(initial_account)

    # --- Trading Agent ---
    agent = TradingAgent(
        cfg=cfg,
        adapter=adapter,
        risk_engine=risk_engine,
        recorder=recorder,
    )

    # --- OpsMonitor ---
    monitor = OpsMonitor(
        cfg=cfg,
        adapter=adapter,
        agent=agent,
        risk_engine=risk_engine,
        recorder=recorder,
    )

    # --- Monitor Loop ---
    interval = float(os.environ.get("NOVATRADE_MONITOR_INTERVAL", "60"))
    loop = MonitorLoop(
        monitor=monitor,
        interval_seconds=interval,
        recorder=recorder,
    )

    # --- Webhook State ---
    ws = WebhookState(
        agent=agent,
        risk_engine=risk_engine,
        monitor=monitor,
        recorder=recorder,
        dry_run=is_dry_run,  # operator-visible flag uses original setting
        webhook_secret=os.environ.get("NOVATRADE_WEBHOOK_SECRET", ""),
        started_at=time.time(),
    )

    return ws, loop


async def run_server(
    ws: WebhookState,
    loop: MonitorLoop,
    *,
    host: str = "0.0.0.0",  # noqa: S104
    port: int = 8877,
) -> None:
    """Run the webhook server and monitor loop concurrently."""
    app = create_app(ws)

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)

    log.info("starting webhook server on %s:%d (dry_run=%s)", host, port, ws.dry_run)

    # Run server and monitor loop concurrently
    loop_task = asyncio.create_task(loop.start())
    try:
        await server.serve()
    finally:
        loop.stop()
        await loop_task


def main() -> None:
    """CLI entrypoint."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    port = int(os.environ.get("NOVATRADE_PORT", "8877"))
    host = os.environ.get("NOVATRADE_HOST", "0.0.0.0")  # noqa: S104

    ws, loop = build_stack()
    asyncio.run(run_server(ws, loop, host=host, port=port))


if __name__ == "__main__":
    main()
