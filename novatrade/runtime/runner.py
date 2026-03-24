"""Runtime runner — Final Demo Launch Phase (Phase 9).

Wires together:
  TradingView webhook → Trading Agent → Risk Engine → Adapter → Evidence
  + OpsMonitor loop → Reconciliation → Action execution → Alerts/Summary

Supports three launch modes:
  - dry_run:      DryRunAdapter, safe simulated mode (default)
  - active_ready: MetaApiAdapter connected, launch gate pending operator confirmation
  - active_demo:  Full active FTMO demo routing after gate passes

Usage::

    # Dry-run mode (default, safe)
    python -m novatrade.runtime.runner

    # Active-ready mode (requires MetaApi credentials)
    NOVATRADE_LAUNCH_MODE=active_ready \\
    METAAPI_TOKEN=... METAAPI_ACCOUNT_ID=... \\
    NOVATRADE_WEBHOOK_SECRET=... \\
    python -m novatrade.runtime.runner

    # Active demo mode (requires all confirmations)
    NOVATRADE_LAUNCH_MODE=active_demo \\
    NOVATRADE_CONFIRM_PINE_COMPILED=true \\
    NOVATRADE_CONFIRM_TV_BACKTEST=true \\
    NOVATRADE_CONFIRM_WEBHOOK_URL=true \\
    NOVATRADE_CONFIRM_ACTIVE_DEMO=true \\
    python -m novatrade.runtime.runner
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

import uvicorn

from novatrade.adapter.base import MT5Adapter
from novatrade.backtest.environment import BacktestEnvironment
from novatrade.config import NovaTradeCfg
from novatrade.data.bar_aggregator import BarAggregator
from novatrade.data.price_feed import TickBatchPoller
from novatrade.execution.live_trading_agent import LiveTradingAgent
from novatrade.execution.trading_agent import TradingAgent
from novatrade.models import AccountState
from novatrade.monitor.feed_health import FeedHealthSupervisor
from novatrade.monitor.ops_monitor import OpsMonitor
from novatrade.risk.hard_risk_supervisor import HardLimits, HardRiskSupervisor
from novatrade.risk.risk_engine import RiskEngine
from novatrade.runtime.dry_run import DryRunAdapter
from novatrade.runtime.launch_gate import (
    LaunchMode,
    LaunchReadiness,
    ReadinessVerdict,
    evaluate_launch_gate,
    generate_readiness_report,
    record_launch_event,
    resolve_launch_mode,
    validate_startup,
)
from novatrade.runtime.live_loop import LiveLoop
from novatrade.runtime.monitor_loop import MonitorLoop
from novatrade.runtime.webhook_server import WebhookState, create_app
from novatrade.storage.state_store import StateStore
from novatrade.strategies.irb import IRBStrategy
from novatrade.strategy.live_engine import LiveStrategyEngine
from novatrade.validation.evidence import EvidenceRecorder

log = logging.getLogger("novatrade.runtime.runner")


# ---------------------------------------------------------------------------
# Adapter factory
# ---------------------------------------------------------------------------


def _create_adapter(cfg: NovaTradeCfg, mode: LaunchMode) -> MT5Adapter:
    """Create the appropriate adapter for the launch mode.

    - dry_run: always DryRunAdapter (safe)
    - active_ready / active_demo: MetaApiAdapter if credentials present
    """
    if mode == LaunchMode.DRY_RUN:
        log.info("using DryRunAdapter (dry_run mode)")
        return DryRunAdapter(inner=None)

    # Active modes require MetaApiAdapter
    meta_errors = cfg.metaapi.validate()
    if meta_errors:
        raise RuntimeError(
            f"Cannot create MetaApiAdapter — missing credentials: {'; '.join(meta_errors)}. "
            "Set METAAPI_TOKEN and METAAPI_ACCOUNT_ID, or use NOVATRADE_LAUNCH_MODE=dry_run."
        )

    from novatrade.adapter.metaapi_provider import MetaApiAdapter

    log.info(
        "using MetaApiAdapter (account=%s, region=%s)",
        cfg.metaapi.account_id[:8] + "...",
        cfg.metaapi.region,
    )
    return MetaApiAdapter(config=cfg.metaapi)


def _adapter_type_name(adapter: MT5Adapter) -> str:
    """Get the adapter class name for status display."""
    return type(adapter).__name__


# ---------------------------------------------------------------------------
# Stack builder
# ---------------------------------------------------------------------------


def build_stack(
    cfg: NovaTradeCfg | None = None,
    *,
    mode: LaunchMode | None = None,
) -> tuple[WebhookState, MonitorLoop, LaunchReadiness]:
    """Build the full NovaTrade runtime stack.

    Returns the webhook state, monitor loop, and launch readiness assessment.

    If *mode* is None, it is resolved from the environment.
    """
    if cfg is None:
        cfg = NovaTradeCfg.load()

    if mode is None:
        mode = resolve_launch_mode()

    log.info(
        "building stack: launch_mode=%s account_mode=%s symbols=%s",
        mode.value,
        cfg.mode.value,
        cfg.symbols,
    )

    # --- Startup validation ---
    validation = validate_startup(cfg, mode)
    if not validation.ok:
        log.error("startup validation FAILED: %s", validation.errors)
        if mode != LaunchMode.DRY_RUN:
            raise RuntimeError(f"Startup validation failed for {mode.value}: {'; '.join(validation.errors)}")
        # Dry-run can proceed with warnings
        log.warning("proceeding in dry_run despite validation warnings")

    for w in validation.warnings:
        log.warning("startup: %s", w)

    # --- Adapter ---
    adapter = _create_adapter(cfg, mode)
    is_dry_run = isinstance(adapter, DryRunAdapter)

    # For active adapter modes, cfg.dry_run must be False to allow orders
    # through the pre-trade gate. For dry-run, the DryRunAdapter is the
    # safety net so we also set cfg.dry_run=False.
    cfg.dry_run = False

    # --- Evidence ---
    campaign = {
        LaunchMode.DRY_RUN: "irb-dry-run",
        LaunchMode.ACTIVE_READY: "irb-active-ready",
        LaunchMode.ACTIVE_DEMO: "irb-active-demo",
    }[mode]
    recorder = EvidenceRecorder(
        path=cfg.data_dir / "evidence.jsonl",
        campaign=campaign,
    )

    # --- Risk Engine ---
    risk_engine = RiskEngine(cfg)
    initial_account = AccountState(
        balance=100_000.0,
        equity=100_000.0,
        mode=cfg.mode,
    )
    risk_engine.initialize(initial_account)

    # --- Hard Risk Supervisor ---
    supervisor = HardRiskSupervisor(
        limits=HardLimits(),
        state_dir=cfg.data_dir / "supervisor",
        kill_switch_dir=cfg.data_dir.parent / "STATE",
    )
    supervisor.initialize(initial_equity=initial_account.equity)

    # --- State Store (persistent FSM + idempotency) ---
    state_store = StateStore(cfg.data_dir / "state.db")

    # --- Trading Agent ---
    agent = TradingAgent(
        cfg=cfg,
        adapter=adapter,
        risk_engine=risk_engine,
        recorder=recorder,
        supervisor=supervisor,
        state_store=state_store,
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

    # --- Launch gate evaluation ---
    readiness = evaluate_launch_gate(
        cfg,
        mode,
        risk_engine_initialized=True,
        risk_engine_halted=risk_engine.halted,
        agent_initialized=True,
        monitor_initialized=True,
        adapter_connected=is_dry_run,  # DryRunAdapter is always "connected"
        adapter_type=_adapter_type_name(adapter),
    )

    # Record startup event
    record_launch_event(recorder, "STARTUP_VALIDATION", validation.to_dict())
    record_launch_event(recorder, "LAUNCH_GATE_EVALUATED", readiness.to_dict())

    # --- Webhook State ---
    ws = WebhookState(
        agent=agent,
        risk_engine=risk_engine,
        monitor=monitor,
        recorder=recorder,
        dry_run=is_dry_run,
        launch_mode=mode,
        adapter_type=_adapter_type_name(adapter),
        webhook_secret=os.environ.get("NOVATRADE_WEBHOOK_SECRET", ""),
        started_at=time.time(),
    )

    # --- Active demo gate check ---
    if mode == LaunchMode.ACTIVE_DEMO and readiness.verdict != ReadinessVerdict.READY_FOR_ACTIVE_DEMO:
        log.error(
            "ACTIVE_DEMO requested but launch gate says %s",
            readiness.verdict.value,
        )
        report = generate_readiness_report(readiness)
        log.error("\n%s", report)
        raise RuntimeError(
            f"Cannot enter active_demo mode — launch gate verdict: {readiness.verdict.value}. "
            f"Blockers: {readiness.blockers}"
        )

    return ws, loop, readiness


# ---------------------------------------------------------------------------
# Live stack builder (full-Python pipeline, no TradingView)
# ---------------------------------------------------------------------------


async def build_live_stack(
    cfg: NovaTradeCfg | None = None,
    *,
    poll_interval: float = 0.5,
    health_interval: float = 5.0,
    shadow: bool = False,
    dry_run: bool = False,
) -> LiveLoop:
    """Build the full live trading stack.

    Creates all components, fetches real account balance from broker,
    pre-seeds strategy engine with historical data, and returns a
    ready-to-run LiveLoop.

    Args:
        cfg: NovaTrade config. Loaded from env if None.
        poll_interval: Tick polling interval in seconds.
        health_interval: Feed health check interval.
        shadow: If True, use shadow mode (log only, no orders).
        dry_run: If True, use DryRunAdapter.
    """
    cfg = cfg or NovaTradeCfg.load()
    symbol = cfg.symbols[0]

    if len(cfg.symbols) > 1:
        log.warning(
            "build_live_stack: multiple symbols configured %s but only the first "
            "symbol (%s) will be used for the strategy engine. Additional symbols "
            "are polled for ticks but will NOT generate trading signals.",
            cfg.symbols,
            symbol,
        )

    # --- Adapter ---
    if dry_run or shadow:
        adapter: MT5Adapter = DryRunAdapter(inner=None)
        log.info("build_live_stack: using DryRunAdapter (dry_run=%s shadow=%s)", dry_run, shadow)
    else:
        meta_errors = cfg.metaapi.validate()
        if meta_errors:
            raise RuntimeError(
                f"Cannot create MetaApiAdapter — missing credentials: {'; '.join(meta_errors)}. "
                "Use --dry-run or --shadow, or set METAAPI_TOKEN and METAAPI_ACCOUNT_ID."
            )
        from novatrade.adapter.metaapi_provider import MetaApiAdapter

        adapter = MetaApiAdapter(config=cfg.metaapi)
        log.info("build_live_stack: using MetaApiAdapter")
        status = await adapter.connect()
        if not status.connected:
            raise RuntimeError(f"MetaApiAdapter connection failed: {status.message}")
        log.info("build_live_stack: adapter connected")

    # --- Account balance (CRITICAL: must succeed) ---
    try:
        account = await adapter.get_account()
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch account state — refusing to trade with unknown balance: {exc}") from exc
    log.info(
        "build_live_stack: account balance=%.2f equity=%.2f mode=%s",
        account.balance,
        account.equity,
        account.mode.value,
    )

    # --- Evidence ---
    recorder = EvidenceRecorder(
        path=cfg.data_dir / "live_evidence.jsonl",
        campaign="irb-live",
    )

    # --- Risk Engine ---
    risk_engine = RiskEngine(cfg)
    risk_engine.initialize(account)

    # --- Hard Risk Supervisor ---
    supervisor = HardRiskSupervisor(
        limits=HardLimits(),
        state_dir=cfg.data_dir / "supervisor",
        kill_switch_dir=cfg.data_dir.parent / "STATE",
    )
    supervisor.initialize(initial_equity=account.equity)

    # --- State Store (persistent FSM + idempotency) ---
    state_store = StateStore(cfg.data_dir / "live_state.db")

    # --- Trading Agent ---
    agent = TradingAgent(
        cfg=cfg,
        adapter=adapter,
        risk_engine=risk_engine,
        recorder=recorder,
        supervisor=supervisor,
        state_store=state_store,
    )

    # --- Strategy Engine ---
    env = BacktestEnvironment(symbol_display=symbol, initial_equity=account.balance)
    strategy = IRBStrategy(env)
    strategy_engine = LiveStrategyEngine(strategy, env)

    # --- Warmup: pre-seed historical candles ---
    try:
        h1_candles = await adapter.get_candles(symbol, "H1", 500)
        h4_candles = await adapter.get_candles(symbol, "H4", 200)
        strategy_engine.seed_history(h1_candles, h4_candles)
        log.info(
            "build_live_stack: seeded %d H1 + %d H4 candles",
            len(h1_candles),
            len(h4_candles),
        )
    except Exception:
        log.error(
            "build_live_stack: WARMUP FAILED — could not fetch historical candles. "
            "Strategy engine has NO historical context and must warm up entirely "
            "from live data. Signals will be unreliable until enough bars accumulate.",
            exc_info=True,
        )

    # --- Live Trading Agent ---
    live_agent = LiveTradingAgent(agent, strategy_engine, cfg, campaign="irb-live")

    # --- Tick Pipeline ---
    poller = TickBatchPoller(adapter, cfg.symbols, interval=poll_interval)

    # --- Bar Aggregator (ensure H4 is included) ---
    timeframes = list(cfg.timeframes)
    if "H4" not in timeframes:
        timeframes.append("H4")
    aggregator = BarAggregator(timeframes=timeframes)

    # --- Feed Health ---
    feed_supervisor = FeedHealthSupervisor()

    # --- LiveLoop ---
    live_loop = LiveLoop(
        poller=poller,
        aggregator=aggregator,
        supervisor=feed_supervisor,
        strategy_engine=strategy_engine,
        live_agent=live_agent,
        health_interval=health_interval,
    )

    log.info(
        "build_live_stack: ready — symbol=%s timeframes=%s poll=%.1fs health=%.1fs",
        symbol,
        timeframes,
        poll_interval,
        health_interval,
    )
    return live_loop


# ---------------------------------------------------------------------------
# Rollback to dry-run
# ---------------------------------------------------------------------------


def rollback_to_dry_run(
    ws: WebhookState,
    recorder: EvidenceRecorder | None = None,
) -> None:
    """Emergency rollback: switch the runtime to dry-run mode.

    This replaces the agent's adapter with DryRunAdapter, marks the
    webhook state as dry_run, and records the event.
    """
    dry_adapter = DryRunAdapter(inner=None)

    if ws.agent is not None:
        ws.agent._adapter = dry_adapter

    ws.dry_run = True
    ws.launch_mode = LaunchMode.DRY_RUN
    ws.adapter_type = "DryRunAdapter"

    log.warning("ROLLBACK: switched to DryRunAdapter (dry_run mode)")
    record_launch_event(
        recorder or ws.recorder,
        "ROLLBACK_TO_DRY_RUN",
        {"reason": "operator_triggered", "launch_mode": LaunchMode.DRY_RUN.value},
    )


# ---------------------------------------------------------------------------
# Server runner
# ---------------------------------------------------------------------------


async def run_server(
    ws: WebhookState,
    loop: MonitorLoop,
    *,
    host: str = "0.0.0.0",  # noqa: S104
    port: int = 8877,
) -> None:
    """Run the webhook server and monitor loop concurrently."""
    # Connect MetaApiAdapter if in active mode
    if ws.agent and not ws.dry_run:
        adapter = ws.agent._adapter
        if hasattr(adapter, "connect") and not getattr(adapter, "_connected", True):
            log.info("connecting MetaApiAdapter to broker...")
            try:
                status = await adapter.connect()
                if status.connected:
                    log.info("MetaApiAdapter connected successfully")
                else:
                    log.error(
                        "MetaApiAdapter connection failed: %s — rolling back to dry-run",
                        status.message,
                    )
                    rollback_to_dry_run(ws, ws.recorder)
            except Exception:
                log.exception("MetaApiAdapter connection failed — rolling back to dry-run")
                rollback_to_dry_run(ws, ws.recorder)

    app = create_app(ws)

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)

    log.info(
        "starting webhook server on %s:%d (mode=%s adapter=%s)",
        host,
        port,
        ws.launch_mode.value,
        ws.adapter_type,
    )

    # Run server and monitor loop concurrently
    loop_task = asyncio.create_task(loop.start())
    try:
        await server.serve()
    finally:
        loop.stop()
        await loop_task


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entrypoint."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    port = int(os.environ.get("NOVATRADE_PORT", "8877"))
    host = os.environ.get("NOVATRADE_HOST", "0.0.0.0")  # noqa: S104

    try:
        ws, loop, readiness = build_stack()
    except RuntimeError as exc:
        log.error("STARTUP FAILED: %s", exc)
        sys.exit(1)

    # Print readiness report
    report = generate_readiness_report(readiness)
    log.info("\n%s", report)

    asyncio.run(run_server(ws, loop, host=host, port=port))


if __name__ == "__main__":
    main()
