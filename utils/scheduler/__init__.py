"""Intelligent Dynamic Block Scheduler — data models, classification, scoring, and packing.

Phase 1: Work unit model, heuristic classifier, and migration helpers
for enriching existing TASKS/*.md files with scheduler metadata.

Phase 2: Task scoring engine — multi-factor objective function, dependency-
aware unlock value propagation, batch ranking with normalisation.

Phase 3: Block packer — capacity-aware scheduling with energy curves,
commitment-level budgets, context-switch accounting, and EPIC gating.

Phase 4: Rolling-horizon replanner — mid-block replanning triggered by
overruns, early completions, dependency failures, and buffer erosion.
Three-horizon model (strategic / tactical / execution) and lifecycle hooks.

Phase 5: Calibration & after-action learning — execution logging, bias
computation, pattern detection, and calibration persistence.

Phase 6: Uncertainty modeling & schedule confidence — Monte Carlo simulation,
plan variant generation, variant selection, and confidence reporting.

Phase 7: Dependency graph & unlock chains — sophisticated DAG with cycle
detection, critical path identification, fail-fast probes, epic decomposition.

Phase 8: Interrupt handling & preemption — classify health/operator/system
events into interrupt levels, preemption budgets, pause/resume, state persistence.

Phase 9: Starvation prevention & maintenance guarantees — aging-based priority
boosts, multi-tier starvation alerts, maintenance slot reservation, work mode
balance monitoring, and deferment age recalculation.
"""

from __future__ import annotations

from utils.scheduler.block import (
    BlockPlan,
    EnergyLevel,
    ScheduledSlot,
    validate_block_plan,
)
from utils.scheduler.calibration import (
    CalibrationEntry,
    CalibrationTable,
    compute_calibration,
    correct_estimate,
    detect_patterns,
    generate_calibration_report,
    get_bias_multiplier,
    load_calibration,
    save_calibration,
)
from utils.scheduler.classifier import classify_task
from utils.scheduler.dependency_graph import (
    DecompositionSuggestion,
    DependencyGraph,
    ProbeTask,
    build_graph,
    generate_probe,
    get_critical_path,
    get_unlock_values,
    is_on_critical_path,
    suggest_decomposition,
)
from utils.scheduler.execution_log import (
    ExecutionRecord,
    create_execution_record,
    log_execution,
    read_execution_log,
)
from utils.scheduler.interrupt_handler import (
    InterruptEvent,
    InterruptLevel,
    InterruptPolicy,
    InterruptState,
    can_resume_paused,
    classify_interrupt,
    get_resume_task_id,
    handle_interrupt,
    load_interrupt_state,
    save_interrupt_state,
)
from utils.scheduler.migration import (
    enrich_task,
    enrich_task_file,
    migrate_all_tasks,
    parse_frontmatter,
    write_enriched_frontmatter,
)
from utils.scheduler.orchestrator import (
    SchedulerConfig,
    SchedulerResult,
    build_block_plan,
    load_scheduler_config,
)
from utils.scheduler.packer import (
    PackerConfig,
    pack_block,
)
from utils.scheduler.replanner import (
    BlockState,
    ReplanDecision,
    ReplannerConfig,
    ReplanReason,
    check_replan_triggers,
    execute_replan,
    get_execution_horizon,
    get_strategic_horizon,
    get_tactical_horizon,
    load_block_state,
    on_task_complete,
    on_task_fail,
    on_task_progress,
    on_task_start,
    save_block_state,
)
from utils.scheduler.scorer import (
    ScoredTask,
    ScoringConfig,
    compute_unlock_values,
    load_scoring_config,
    rank_tasks,
    score_task,
)
from utils.scheduler.starvation import (
    AgingConfig,
    MaintenanceConfig,
    WorkModeBalance,
    check_starvation,
    check_work_mode_balance,
    compute_effective_priority,
    reserve_maintenance,
    update_deferment_ages,
)
from utils.scheduler.uncertainty import (
    PlanVariant,
    SimulationResult,
    VariantResult,
    format_confidence_report,
    generate_plan_variants,
    select_best_variant,
    simulate_block,
)
from utils.scheduler.work_unit import (
    TASK_CLASS_DURATION_RANGES,
    CommitmentLevel,
    TaskClass,
    WorkMode,
    WorkUnit,
)

__all__ = [
    "TASK_CLASS_DURATION_RANGES",
    "AgingConfig",
    "BlockPlan",
    "BlockState",
    "CalibrationEntry",
    "CalibrationTable",
    "CommitmentLevel",
    "DecompositionSuggestion",
    "DependencyGraph",
    "EnergyLevel",
    "ExecutionRecord",
    "InterruptEvent",
    "InterruptLevel",
    "InterruptPolicy",
    "InterruptState",
    "MaintenanceConfig",
    "PackerConfig",
    "PlanVariant",
    "ProbeTask",
    "ReplanDecision",
    "ReplanReason",
    "ReplannerConfig",
    "ScheduledSlot",
    "SchedulerConfig",
    "SchedulerResult",
    "ScoredTask",
    "ScoringConfig",
    "SimulationResult",
    "TaskClass",
    "VariantResult",
    "WorkMode",
    "WorkModeBalance",
    "WorkUnit",
    "build_block_plan",
    "build_graph",
    "can_resume_paused",
    "check_replan_triggers",
    "check_starvation",
    "check_work_mode_balance",
    "classify_interrupt",
    "classify_task",
    "compute_calibration",
    "compute_effective_priority",
    "compute_unlock_values",
    "correct_estimate",
    "create_execution_record",
    "detect_patterns",
    "enrich_task",
    "enrich_task_file",
    "execute_replan",
    "format_confidence_report",
    "generate_calibration_report",
    "generate_plan_variants",
    "generate_probe",
    "get_bias_multiplier",
    "get_critical_path",
    "get_execution_horizon",
    "get_resume_task_id",
    "get_strategic_horizon",
    "get_tactical_horizon",
    "get_unlock_values",
    "handle_interrupt",
    "is_on_critical_path",
    "load_block_state",
    "load_calibration",
    "load_interrupt_state",
    "load_scheduler_config",
    "load_scoring_config",
    "log_execution",
    "migrate_all_tasks",
    "on_task_complete",
    "on_task_fail",
    "on_task_progress",
    "on_task_start",
    "pack_block",
    "parse_frontmatter",
    "rank_tasks",
    "read_execution_log",
    "reserve_maintenance",
    "save_block_state",
    "save_calibration",
    "save_interrupt_state",
    "score_task",
    "select_best_variant",
    "simulate_block",
    "suggest_decomposition",
    "update_deferment_ages",
    "validate_block_plan",
    "write_enriched_frontmatter",
]
