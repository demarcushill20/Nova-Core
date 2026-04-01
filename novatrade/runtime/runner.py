"""Runtime runner — NovaTrade service entrypoint.

Supports two pipeline modes controlled by ``NOVATRADE_PIPELINE``:

  - **webhook** (default): TradingView webhook → TradingAgent → Risk → Adapter
  - **live**: Full-Python pipeline — tick polling → bar aggregation →
    strategy evaluation → signal queue → order execution (no TradingView)

Launch modes (``NOVATRADE_LAUNCH_MODE``):
  - dry_run:      DryRunAdapter, safe simulated mode (default)
  - active_ready: MetaApiAdapter connected, launch gate pending operator confirmation
  - active_demo:  Full active FTMO demo routing after gate passes

Usage::

    # Webhook pipeline (default, backward-compatible)
    python -m novatrade.runtime.runner

    # Live pipeline with strategy config
    NOVATRADE_PIPELINE=live \\
    NOVATRADE_STRATEGY_CONFIG=configs/strategies/irb_v2_seed9999.yaml \\
    NOVATRADE_LAUNCH_MODE=active_ready \\
    python -m novatrade.runtime.runner
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import uvicorn

from novatrade.adapter.base import MT5Adapter
from novatrade.backtest.environment import BacktestEnvironment
from novatrade.config import NovaTradeCfg
from novatrade.data.bar_aggregator import BarAggregator
from novatrade.data.price_feed import TickBatchPoller
from novatrade.execution.live_trading_agent import LiveTradingAgent
from novatrade.execution.trading_agent import TradingAgent
from novatrade.models import AccountState, OrderSide
from novatrade.monitor.feed_health import FeedHealthConfig, FeedHealthSupervisor
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
from novatrade.strategy.live_engine import LiveConfig, LiveStrategyEngine
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

    # --- Persist strategy config for autonomy collector ---
    _persist_strategy_config(cfg, cfg.symbols[0] if cfg.symbols else "EURUSD", "H1", "H4", "webhook")

    return ws, loop, readiness


# ---------------------------------------------------------------------------
# Strategy config persistence (for autonomy collector)
# ---------------------------------------------------------------------------


def _persist_strategy_config(
    cfg: NovaTradeCfg,
    symbol: str,
    primary_tf: str,
    higher_tf: str,
    pipeline: str,
) -> None:
    """Write strategy_config.json so the autonomy collector can verify pipeline health."""
    import json as _json

    # data_dir is typically "OUTPUT/novatrade" (relative to project root).
    # Navigate up 2 levels to project root, then into STATE/novatrade.
    state_dir = Path(cfg.data_dir).parent.parent / "STATE" / "novatrade"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        config_path = state_dir / "strategy_config.json"
        config_data = {
            "strategy": "IRB",
            "symbol": symbol,
            "primary_timeframe": primary_tf,
            "higher_timeframe": higher_tf,
            "pipeline": pipeline,
            "started_at": time.time(),
            "symbols": cfg.symbols,
        }
        config_path.write_text(_json.dumps(config_data, indent=2))
        log.info("persisted strategy_config.json to %s", config_path)
    except OSError:
        log.debug("Failed to persist strategy_config.json", exc_info=True)


# ---------------------------------------------------------------------------
# Live stack builder (full-Python pipeline, no TradingView)
# ---------------------------------------------------------------------------


async def build_live_stack(
    cfg: NovaTradeCfg | None = None,
    *,
    poll_interval: float = 30.0,
    health_interval: float = 5.0,
    shadow: bool = False,
    dry_run: bool = False,
    strategy_config_path: str | None = None,
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
        strategy_config_path: Path to strategy YAML config. If None,
            reads from ``NOVATRADE_STRATEGY_CONFIG`` env var. If neither
            is set, uses default BacktestEnvironment parameters.
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

    # --- Strategy Engine (with optional YAML config) ---
    env_kwargs: dict[str, Any] = {
        "symbol_display": symbol,
        "initial_equity": account.balance,
    }
    config_path = strategy_config_path or os.environ.get("NOVATRADE_STRATEGY_CONFIG")
    if config_path:
        from novatrade.cli.config_schema import StrategyConfig

        sc = StrategyConfig.from_yaml(Path(config_path))
        env_kwargs.update(sc.to_environment_kwargs())
        log.info(
            "build_live_stack: loaded strategy config '%s' v%s (%s)",
            sc.name,
            sc.version,
            sc.description,
        )
    else:
        log.warning(
            "build_live_stack: no strategy config — using default parameters. "
            "Set NOVATRADE_STRATEGY_CONFIG to a YAML path to load optimised params."
        )

    # --- Resolve timeframes from strategy config or defaults ---
    primary_tf = "H1"
    higher_tf = "H4"
    if config_path:
        primary_tf = sc.primary_timeframe
        higher_tf = sc.higher_timeframe
        log.info(
            "build_live_stack: timeframes from config: primary=%s, higher=%s",
            primary_tf,
            higher_tf,
        )

    live_config = LiveConfig(
        symbol=symbol,
        primary_timeframe=primary_tf,
        higher_timeframe=higher_tf,
    )

    env = BacktestEnvironment(**env_kwargs)
    strategy = IRBStrategy(env)
    strategy_engine = LiveStrategyEngine(strategy, env, config=live_config)

    # --- Broker symbol resolution ---
    # OANDA and some FTMO MT5 brokers use suffixed symbol names (e.g. "EURUSD.sim").
    # Resolve display symbols → broker symbols for all data-layer calls.
    broker_symbol = cfg.ftmo.resolve_symbol(symbol)
    broker_map = {s: cfg.ftmo.resolve_symbol(s) for s in cfg.symbols}
    if broker_symbol != symbol:
        log.info(
            "build_live_stack: symbol mapping: %s → %s (suffix=%r)",
            symbol,
            broker_symbol,
            cfg.ftmo.symbol_suffix,
        )

    # --- Warmup: pre-seed historical candles ---
    try:
        primary_candles = await adapter.get_candles(broker_symbol, primary_tf, 500)
        higher_candles = await adapter.get_candles(broker_symbol, higher_tf, 200)
        strategy_engine.seed_history(primary_candles, higher_candles)
        log.info(
            "build_live_stack: seeded %d %s + %d %s candles",
            len(primary_candles),
            primary_tf,
            len(higher_candles),
            higher_tf,
        )
    except Exception:
        log.error(
            "build_live_stack: WARMUP FAILED — could not fetch historical candles. "
            "Strategy engine has NO historical context and must warm up entirely "
            "from live data. Signals will be unreliable until enough bars accumulate.",
            exc_info=True,
        )

    # --- Subscribe to market data (prime the price feed) ---
    broker_symbols = list(broker_map.values())
    try:
        await adapter.subscribe_to_market_data(broker_symbols)
        log.info("build_live_stack: subscribed to market data for %s", broker_symbols)
    except Exception:
        log.warning(
            "build_live_stack: market data subscription failed — poller will "
            "attempt on first poll, but initial ticks may be delayed",
            exc_info=True,
        )

    # --- Startup reconciliation: sync agent state with broker reality ---
    # After restart, agent restores persisted state from StateStore (may be
    # LONG/SHORT/PENDING from a previous session).  Must reconcile against
    # broker to prevent stale-state lockout where agent thinks it has a
    # position but the broker closed it (SL hit, manual close, etc.).
    try:
        positions = await adapter.get_positions()
        if positions:
            pos = positions[0]  # single-position strategy
            broker_side = "LONG" if getattr(pos, "type", "BUY") in ("BUY", "buy", "POSITION_TYPE_BUY") else "SHORT"
            order_side = OrderSide.BUY if broker_side == "LONG" else OrderSide.SELL
            agent.recover_position(
                position_id=pos.position_id,
                side=order_side,
                symbol=symbol,
                volume=pos.volume,
                fill_price=pos.open_price,
                stop_loss=getattr(pos, "stop_loss", 0.0) or 0.0,
            )
            strategy_engine.recover_position_state(
                side=broker_side,
                entry_price=pos.open_price,
                stop_loss=getattr(pos, "stop_loss", 0.0) or 0.0,
                volume=pos.volume,
            )
            log.info(
                "build_live_stack: RECONCILED — adopted broker position %s %s %.2f lots at %.5f",
                pos.position_id,
                broker_side,
                pos.volume,
                pos.open_price,
            )
        else:
            # Broker has NO positions — force agent to FLAT if it restored
            # to a non-FLAT state from persistence (stale state from a prior
            # session where the position was closed broker-side).
            from novatrade.execution.trading_agent import AgentState

            if agent.state != AgentState.FLAT:
                old_state = agent.state
                agent.force_flat(reason="startup_reconcile_no_broker_position")
                strategy_engine.cancel_pending()
                log.warning(
                    "build_live_stack: RECONCILED — agent was %s (persisted) but broker "
                    "has no positions. Forced FLAT to prevent stale-state lockout.",
                    old_state.value,
                )
            else:
                log.info("build_live_stack: reconciliation OK — agent FLAT, broker FLAT")
    except Exception:
        # Cannot reach broker — force FLAT as a safety measure.
        # A stale LONG/SHORT state with no ability to verify broker reality
        # is worse than starting FLAT and missing a position adoption (the
        # health monitor will attempt periodic reconciliation later).
        from novatrade.execution.trading_agent import AgentState

        if agent.state != AgentState.FLAT:
            old_state = agent.state
            agent.force_flat(reason="startup_reconcile_broker_unreachable")
            strategy_engine.cancel_pending()
            log.warning(
                "build_live_stack: startup reconciliation FAILED and agent was %s. "
                "Forced FLAT to prevent stale-state lockout. "
                "Health monitor will attempt reconciliation when broker is reachable.",
                old_state.value,
            )
        else:
            log.warning(
                "build_live_stack: startup reconciliation failed — agent already FLAT. "
                "Health monitor will attempt periodic reconciliation.",
                exc_info=True,
            )

    # --- Live Trading Agent ---
    live_agent = LiveTradingAgent(agent, strategy_engine, cfg, campaign="irb-live")

    # --- Tick Pipeline ---
    poller = TickBatchPoller(adapter, cfg.symbols, interval=poll_interval, broker_map=broker_map)

    # --- Bar Aggregator (ensure higher timeframe is included) ---
    timeframes = list(cfg.timeframes)
    if higher_tf not in timeframes:
        timeframes.append(higher_tf)
    aggregator = BarAggregator(timeframes=timeframes)

    # --- Feed Health ---
    feed_supervisor = FeedHealthSupervisor(FeedHealthConfig(poll_interval=poll_interval))

    # --- LiveLoop ---
    live_loop = LiveLoop(
        poller=poller,
        aggregator=aggregator,
        supervisor=feed_supervisor,
        strategy_engine=strategy_engine,
        live_agent=live_agent,
        health_interval=health_interval,
        state_store=state_store,
        adapter=adapter,
        hard_risk_supervisor=supervisor,
    )

    # --- Persist strategy config for autonomy collector ---
    _persist_strategy_config(cfg, symbol, primary_tf, higher_tf, "live")

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
# Live pipeline runner
# ---------------------------------------------------------------------------


async def run_live(
    live_loop: LiveLoop,
    *,
    host: str = "0.0.0.0",  # noqa: S104
    port: int = 8877,
) -> None:
    """Run the live trading loop with a health/status HTTP server.

    Starts both the LiveLoop (tick pipeline + strategy + execution) and a
    lightweight FastAPI server exposing /health, /status, and /readiness
    endpoints.  Webhook alerts are still accepted as a fallback.
    """
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    started_at = time.time()

    async def health(_request: Any) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok" if live_loop.running else "starting",
                "pipeline": "live",
                "uptime_seconds": round(time.time() - started_at, 1),
            }
        )

    async def status(_request: Any) -> JSONResponse:
        snapshot = live_loop.snapshot()
        snapshot["pipeline"] = "live"
        snapshot["uptime_seconds"] = round(time.time() - started_at, 1)
        return JSONResponse(snapshot)

    app = Starlette(
        routes=[
            Route("/health", health),
            Route("/status", status),
        ]
    )

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)

    log.info(
        "starting LIVE pipeline on %s:%d",
        host,
        port,
    )

    live_task = asyncio.create_task(live_loop.run())
    try:
        await server.serve()
    finally:
        live_loop.stop()
        await live_task


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entrypoint.

    Pipeline selection via ``NOVATRADE_PIPELINE``:
      - ``webhook`` (default): TradingView webhook-driven pipeline
      - ``live``: Full-Python tick → strategy → execution pipeline
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    port = int(os.environ.get("NOVATRADE_PORT", "8877"))
    host = os.environ.get("NOVATRADE_HOST", "0.0.0.0")  # noqa: S104
    pipeline = os.environ.get("NOVATRADE_PIPELINE", "webhook").lower()

    if pipeline == "live":
        log.info("selected pipeline: LIVE (full-Python)")

        async def _start_live() -> None:
            try:
                live_loop = await build_live_stack()
            except RuntimeError as exc:
                log.error("LIVE STARTUP FAILED: %s", exc)
                sys.exit(1)
            await run_live(live_loop, host=host, port=port)

        asyncio.run(_start_live())
    else:
        if pipeline != "webhook":
            log.warning("unknown pipeline %r — falling back to webhook", pipeline)
        log.info("selected pipeline: WEBHOOK")
        try:
            ws, loop, readiness = build_stack()
        except RuntimeError as exc:
            log.error("STARTUP FAILED: %s", exc)
            sys.exit(1)

        report = generate_readiness_report(readiness)
        log.info("\n%s", report)

        asyncio.run(run_server(ws, loop, host=host, port=port))


if __name__ == "__main__":
    main()
