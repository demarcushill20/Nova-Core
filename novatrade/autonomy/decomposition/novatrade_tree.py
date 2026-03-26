"""Pre-built NovaTrade goal decomposition tree.

Hardcoded decomposition of "Get NovaTrade 100% operational" into
~17 sub-goals across the 5 scoring dimensions, with dependency edges.
"""

from __future__ import annotations

from novatrade.autonomy.decomposition.models import GoalDecomposition, SubGoal

GOAL_ID = "novatrade_100pct_operational"


def build_novatrade_tree() -> GoalDecomposition:
    """Build the canonical NovaTrade decomposition tree."""

    sub_goals = [
        # ── SYSTEM_HEALTH ──────────────────────────────────────
        SubGoal(
            sub_goal_id="sg_services_running",
            parent_goal_id=GOAL_ID,
            dimension="system_health",
            title="All core services active",
            description=(
                "novacore-watcher, novacore-telegram, novacore-telegram-notifier, novacore-novatrade all running."
            ),
            priority=1,
        ),
        SubGoal(
            sub_goal_id="sg_no_orphan_tasks",
            parent_goal_id=GOAL_ID,
            dimension="system_health",
            title="Zero stale in-progress tasks",
            description="No .inprogress files older than 4 hours in TASKS/.",
            priority=3,
        ),
        SubGoal(
            sub_goal_id="sg_memory_reachable",
            parent_goal_id=GOAL_ID,
            dimension="system_health",
            title="Memory systems reachable",
            description="Pinecone, Neo4j, and Redis all healthy via check_health.",
            priority=2,
        ),
        # ── EXECUTION_PIPELINE ─────────────────────────────────
        SubGoal(
            sub_goal_id="sg_webhook_live",
            parent_goal_id=GOAL_ID,
            dimension="execution_pipeline",
            title="Webhook endpoint live",
            description="HTTP GET to localhost:8877/health returns 200.",
            priority=1,
        ),
        SubGoal(
            sub_goal_id="sg_adapter_connected",
            parent_goal_id=GOAL_ID,
            dimension="execution_pipeline",
            title="MetaApi adapter connected",
            description="MetaApi adapter is connected to the broker account.",
            priority=1,
        ),
        SubGoal(
            sub_goal_id="sg_feed_healthy",
            parent_goal_id=GOAL_ID,
            dimension="execution_pipeline",
            title="Price feed healthy",
            description="FeedHealthSupervisor reports non-stale status for all symbols.",
            dependencies=["sg_adapter_connected"],
            priority=1,
        ),
        SubGoal(
            sub_goal_id="sg_state_store_valid",
            parent_goal_id=GOAL_ID,
            dimension="execution_pipeline",
            title="State store valid",
            description="SQLite DB accessible, schema correct, read/write working.",
            priority=2,
        ),
        SubGoal(
            sub_goal_id="sg_order_flow_tested",
            parent_goal_id=GOAL_ID,
            dimension="execution_pipeline",
            title="Order flow end-to-end verified",
            description="Dry-run order placement through full pipeline succeeds.",
            dependencies=["sg_adapter_connected", "sg_feed_healthy"],
            priority=2,
        ),
        # ── STRATEGY_VALIDITY ──────────────────────────────────
        SubGoal(
            sub_goal_id="sg_strategy_loaded",
            parent_goal_id=GOAL_ID,
            dimension="strategy_validity",
            title="IRB strategy loaded",
            description="IRB config loaded, indicators computing without NaN.",
            priority=1,
        ),
        SubGoal(
            sub_goal_id="sg_signals_generating",
            parent_goal_id=GOAL_ID,
            dimension="strategy_validity",
            title="Strategy producing signals",
            description="LiveStrategyEngine producing signals within last 24h.",
            dependencies=["sg_strategy_loaded", "sg_feed_healthy"],
            priority=1,
        ),
        SubGoal(
            sub_goal_id="sg_backtest_alignment",
            parent_goal_id=GOAL_ID,
            dimension="strategy_validity",
            title="Backtest-live alignment",
            description="Backtest Sharpe vs live estimate delta < 30%.",
            dependencies=["sg_signals_generating"],
            priority=3,
        ),
        # ── RISK_ENGINE_INTEGRITY ──────────────────────────────
        SubGoal(
            sub_goal_id="sg_risk_not_halted",
            parent_goal_id=GOAL_ID,
            dimension="risk_engine",
            title="Risk engine not halted",
            description="RiskEngine is not in HALT state.",
            priority=1,
        ),
        SubGoal(
            sub_goal_id="sg_drawdown_safe",
            parent_goal_id=GOAL_ID,
            dimension="risk_engine",
            title="Drawdown within FTMO limits",
            description="Current drawdown < 80% of FTMO daily limit.",
            priority=1,
        ),
        SubGoal(
            sub_goal_id="sg_pre_trade_functional",
            parent_goal_id=GOAL_ID,
            dimension="risk_engine",
            title="Pre-trade gate functional",
            description="PreTradeGate returns ALLOW for valid synthetic signals.",
            dependencies=["sg_risk_not_halted"],
            priority=2,
        ),
        # ── PERFORMANCE_STABILITY ──────────────────────────────
        SubGoal(
            sub_goal_id="sg_sufficient_data",
            parent_goal_id=GOAL_ID,
            dimension="performance_stability",
            title="Sufficient trade data",
            description="30+ completed trades for statistical validity.",
            dependencies=["sg_signals_generating"],
            priority=3,
        ),
        SubGoal(
            sub_goal_id="sg_profit_factor_ok",
            parent_goal_id=GOAL_ID,
            dimension="performance_stability",
            title="Profit factor healthy",
            description="Profit factor > 1.2 in recent trading window.",
            dependencies=["sg_sufficient_data"],
            priority=2,
        ),
        SubGoal(
            sub_goal_id="sg_drawdown_controlled",
            parent_goal_id=GOAL_ID,
            dimension="performance_stability",
            title="Max drawdown controlled",
            description="Max drawdown < FTMO limit (10% of account).",
            dependencies=["sg_sufficient_data"],
            priority=2,
        ),
    ]

    # Build DAG edges from dependencies
    dag_edges: list[tuple[str, str]] = []
    for sg in sub_goals:
        for dep in sg.dependencies:
            dag_edges.append((dep, sg.sub_goal_id))

    return GoalDecomposition(
        goal_id=GOAL_ID,
        goal_description="Get NovaTrade 100% operational across all 5 dimensions.",
        sub_goals=sub_goals,
        dag_edges=dag_edges,
    )
