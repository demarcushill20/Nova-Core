"""Phase 10: Scheduler Orchestrator — Integration, Migration & Rollout.

Single entry point that ties all 9 prior scheduler phases together into
a unified pipeline:

  1. Work unit model & classification (P1)
  2. Multi-factor scoring & ranking (P2)
  3. Capacity-aware block packing (P3)
  4. Rolling-horizon replanning (P4)
  5. Calibration & after-action learning (P5)
  6. Uncertainty modeling & Monte Carlo simulation (P6)
  7. Dependency graph & unlock chains (P7)
  8. Interrupt handling & preemption (P8)
  9. Starvation prevention & maintenance guarantees (P9)

The orchestrator exposes:
  - SchedulerConfig: master config aggregating all sub-configs
  - SchedulerResult: full pipeline output with diagnostics
  - build_block_plan(): THE main function — runs the full pipeline
  - load_scheduler_config(): loads config from files with defaults
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from utils.scheduler.block import BlockPlan, validate_block_plan
from utils.scheduler.calibration import (
    correct_estimate,
    load_calibration,
)
from utils.scheduler.dependency_graph import (
    build_graph,
    generate_probe,
    get_critical_path,
    get_unlock_values,
    suggest_decomposition,
)
from utils.scheduler.interrupt_handler import InterruptPolicy
from utils.scheduler.packer import PackerConfig, pack_block
from utils.scheduler.replanner import ReplannerConfig
from utils.scheduler.scorer import ScoredTask, ScoringConfig, rank_tasks
from utils.scheduler.starvation import (
    AgingConfig,
    MaintenanceConfig,
    check_starvation,
    check_work_mode_balance,
    reserve_maintenance,
    update_deferment_ages,
)
from utils.scheduler.uncertainty import (
    PlanVariant,
    SimulationResult,
    format_confidence_report,
    generate_plan_variants,
    select_best_variant,
    simulate_block,
)
from utils.scheduler.work_unit import TaskClass, WorkUnit

log = logging.getLogger("nova.scheduler.orchestrator")


# ---------------------------------------------------------------------------
# SchedulerConfig
# ---------------------------------------------------------------------------


class SchedulerConfig(BaseModel):
    """Master configuration aggregating all scheduler sub-configs.

    The ``enabled`` flag is the master feature flag.  When False,
    build_block_plan() returns an empty result immediately.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True, description="Master feature flag")
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    packer: PackerConfig = Field(default_factory=PackerConfig)
    replanner: ReplannerConfig = Field(default_factory=ReplannerConfig)
    interrupt_policy: InterruptPolicy = Field(default_factory=InterruptPolicy)
    aging: AgingConfig = Field(default_factory=AgingConfig)
    maintenance: MaintenanceConfig = Field(default_factory=MaintenanceConfig)
    use_calibration: bool = Field(default=True, description="Apply calibration corrections")
    use_uncertainty: bool = Field(default=True, description="Run Monte Carlo simulation")
    uncertainty_simulations: int = Field(default=200, ge=1)
    variant_selection: str = Field(
        default="balanced",
        description="aggressive / balanced / conservative / auto",
    )


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

_BASE_DIR = Path(__file__).resolve().parents[2]


def load_scheduler_config(config_dir: Path | None = None) -> SchedulerConfig:
    """Load scheduler config from files, merging into SchedulerConfig.

    Sources:
      - config/scheduler_weights.yaml  -> scoring config
      - config/interrupt_policy.yaml   -> interrupt policy
      - STATE/config/feature_flags.json -> enabled flag + other flags

    Falls back to defaults for any missing file or key.
    """
    base = config_dir or _BASE_DIR

    kwargs: dict = {}

    # --- scheduler_weights.yaml ---
    weights_path = base / "config" / "scheduler_weights.yaml"
    if weights_path.exists():
        try:
            raw = yaml.safe_load(weights_path.read_text()) or {}
            # ScoringConfig is loaded by its own loader, but we can also
            # extract orchestrator-level keys if present
            orch = raw.get("orchestrator", {})
            if isinstance(orch, dict):
                if "use_calibration" in orch:
                    kwargs["use_calibration"] = bool(orch["use_calibration"])
                if "use_uncertainty" in orch:
                    kwargs["use_uncertainty"] = bool(orch["use_uncertainty"])
                if "uncertainty_simulations" in orch:
                    kwargs["uncertainty_simulations"] = int(orch["uncertainty_simulations"])
                if "variant_selection" in orch:
                    kwargs["variant_selection"] = str(orch["variant_selection"])
        except (yaml.YAMLError, OSError) as exc:
            log.warning("Failed to parse %s: %s", weights_path, exc)

    # --- interrupt_policy.yaml ---
    policy_path = base / "config" / "interrupt_policy.yaml"
    if policy_path.exists():
        try:
            raw = yaml.safe_load(policy_path.read_text()) or {}
            policy_data = raw.get("interrupt_policy", {})
            if isinstance(policy_data, dict):
                kwargs["interrupt_policy"] = InterruptPolicy(**policy_data)
        except (yaml.YAMLError, OSError, ValueError, TypeError) as exc:
            log.warning("Failed to parse %s: %s", policy_path, exc)

    # --- feature_flags.json ---
    flags_path = base / "STATE" / "config" / "feature_flags.json"
    if flags_path.exists():
        try:
            flags = json.loads(flags_path.read_text())
            sched_flags = flags.get("scheduler", {})
            if isinstance(sched_flags, dict) and "enabled" in sched_flags:
                kwargs["enabled"] = bool(sched_flags["enabled"])
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to parse %s: %s", flags_path, exc)

    return SchedulerConfig(**kwargs)


# ---------------------------------------------------------------------------
# SchedulerResult
# ---------------------------------------------------------------------------


class SchedulerResult(BaseModel):
    """Full output of the scheduler orchestration pipeline."""

    model_config = ConfigDict(extra="forbid")

    block_plan: BlockPlan = Field(default_factory=BlockPlan)
    simulation: SimulationResult | None = None
    variant_used: str = "balanced"
    calibration_applied: bool = False
    starvation_alerts: list[dict] = Field(default_factory=list)
    epic_warnings: list[str] = Field(default_factory=list)
    probe_tasks: list[dict] = Field(default_factory=list)
    critical_path: list[str] = Field(default_factory=list)
    work_mode_balance: dict = Field(default_factory=dict)
    total_tasks_scored: int = 0
    total_tasks_scheduled: int = 0
    total_tasks_deferred: int = 0
    confidence_summary: str = ""
    validation_warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def build_block_plan(  # noqa: C901
    tasks: list[WorkUnit],
    block_duration_min: float = 480.0,
    config: SchedulerConfig | None = None,
) -> SchedulerResult:
    """Run the full scheduler pipeline and return a SchedulerResult.

    This is THE main function.  Pipeline:
      1.  Load config (use defaults if None)
      2.  If not enabled, return empty result
      3.  Update deferment ages (P9)
      4.  Check starvation alerts (P9)
      5.  Check for EPIC tasks - generate decomposition suggestions (P7)
      6.  Generate probes for high-uncertainty tasks (P7)
      7.  Build dependency graph (P7)
      8.  Get critical path (P7)
      9.  Compute unlock values from graph and update tasks (P7)
      10. Apply calibration corrections if available (P5)
      11. Score and rank tasks (P2)
      12. If use_uncertainty: generate plan variants (P6) + select best
          Else: pack directly (P3)
      13. Check work mode balance (P9)
      14. Reserve maintenance slots (P9)
      15. Validate block plan (P3)
      16. Run simulation on final plan (P6)
      17. Build confidence summary string (P6)
      18. Return SchedulerResult with all collected data
    """
    # 1. Load config
    cfg = config or load_scheduler_config()

    # 2. Feature flag check
    if not cfg.enabled:
        return SchedulerResult()

    if not tasks:
        return SchedulerResult()

    # Collect diagnostics
    epic_warnings: list[str] = []
    probe_tasks: list[dict] = []

    # 3. Update deferment ages (P9)
    aged_tasks = update_deferment_ages(tasks)

    # 4. Check starvation alerts (P9)
    starvation_alerts = check_starvation(aged_tasks, cfg.aging)

    # 5. Check for EPIC tasks - generate decomposition suggestions (P7)
    for wu in aged_tasks:
        if wu.task_class == TaskClass.EPIC:
            suggestion = suggest_decomposition(wu)
            if suggestion is not None:
                task_id = wu.id or wu.title or "unknown"
                epic_warnings.append(
                    f"EPIC task '{task_id}' should be decomposed: "
                    f"{suggestion.decomposition_strategy} into "
                    f"{len(suggestion.suggested_subtasks)} subtasks"
                )

    # 6. Generate probes for high-uncertainty tasks (P7)
    for wu in aged_tasks:
        probe = generate_probe(wu)
        if probe is not None:
            probe_tasks.append(probe.model_dump())

    # 7. Build dependency graph (P7)
    graph = build_graph(aged_tasks)

    # 8. Get critical path (P7)
    critical_path = get_critical_path(graph)

    # 9. Compute unlock values from graph and update tasks (P7)
    unlock_vals = get_unlock_values(graph)
    enriched_tasks: list[WorkUnit] = []
    for wu in aged_tasks:
        if wu.id and wu.id in unlock_vals:
            new_uv = max(wu.unblock_value, unlock_vals[wu.id])
            if new_uv != wu.unblock_value:
                enriched_tasks.append(wu.model_copy(update={"unblock_value": new_uv}))
            else:
                enriched_tasks.append(wu)
        else:
            enriched_tasks.append(wu)

    # 10. Apply calibration corrections if available (P5)
    calibration_applied = False
    if cfg.use_calibration:
        try:
            table = load_calibration()
            if table.entries or table.global_bias != 1.0:
                calibrated_tasks: list[WorkUnit] = []
                for wu in enriched_tasks:
                    corrected_expected = correct_estimate(
                        wu.estimated_expected_min,
                        wu.task_class,
                        wu.work_mode,
                        table,
                    )
                    # Update optimistic/pessimistic proportionally
                    if wu.estimated_expected_min > 0:
                        ratio = corrected_expected / wu.estimated_expected_min
                    else:
                        ratio = 1.0
                    corrected_optimistic = wu.estimated_optimistic_min * ratio
                    corrected_pessimistic = wu.estimated_pessimistic_min * ratio

                    # Ensure ordering constraint holds
                    corrected_optimistic = min(corrected_optimistic, corrected_expected)
                    corrected_pessimistic = max(corrected_pessimistic, corrected_expected)

                    calibrated_tasks.append(
                        wu.model_copy(
                            update={
                                "estimated_expected_min": max(1.0, round(corrected_expected, 2)),
                                "estimated_optimistic_min": max(1.0, round(corrected_optimistic, 2)),
                                "estimated_pessimistic_min": max(1.0, round(corrected_pessimistic, 2)),
                            }
                        )
                    )
                enriched_tasks = calibrated_tasks
                calibration_applied = True
        except Exception as exc:
            log.warning("Calibration loading failed, using raw estimates: %s", exc)

    # 11. Score and rank tasks (P2)
    scored = rank_tasks(enriched_tasks, cfg.scoring)
    total_tasks_scored = len(scored)

    # 12. Plan generation (P6 uncertainty or P3 direct pack)
    variant_used = cfg.variant_selection
    block_plan: BlockPlan

    if cfg.use_uncertainty:
        variants = generate_plan_variants(
            scored,
            block_duration_min=block_duration_min,
            n_simulations=cfg.uncertainty_simulations,
            packer_config=cfg.packer,
        )

        if cfg.variant_selection == "auto":
            best = select_best_variant(variants)
            variant_used = best.variant.value
            block_plan = best.plan
        else:
            # Map string to PlanVariant enum
            variant_map = {
                "aggressive": PlanVariant.AGGRESSIVE,
                "balanced": PlanVariant.BALANCED,
                "conservative": PlanVariant.CONSERVATIVE,
            }
            target_variant = variant_map.get(cfg.variant_selection, PlanVariant.BALANCED)
            selected = None
            for v in variants:
                if v.variant == target_variant:
                    selected = v
                    break
            if selected is None:
                selected = variants[1] if len(variants) > 1 else variants[0]
            variant_used = selected.variant.value
            block_plan = selected.plan
    else:
        block_plan = pack_block(scored, block_duration_min=block_duration_min, config=cfg.packer)

    # 13. Check work mode balance (P9) — uses empty records for now
    # In production, execution records would be loaded from LOGS/
    balance = check_work_mode_balance([])
    work_mode_balance_dict = balance.model_dump()

    # 14. Reserve maintenance slots (P9)
    # Gather maintenance-eligible scored tasks from the deferred list
    maint_modes = set(cfg.maintenance.maintenance_modes)
    maintenance_scored: list[ScoredTask] = [
        st for st in block_plan.deferred_tasks if st.work_unit.work_mode.value in maint_modes
    ]
    if maintenance_scored:
        block_plan = reserve_maintenance(block_plan, maintenance_scored, cfg.maintenance)

    # 15. Validate block plan (P3)
    validation_warnings = validate_block_plan(block_plan)

    # Merge epic warnings from packer with our orchestrator warnings
    all_epic_warnings = epic_warnings + block_plan.epic_warnings

    # 16. Run simulation on final plan (P6)
    simulation = simulate_block(block_plan, n_simulations=cfg.uncertainty_simulations)

    # 17. Build confidence summary string (P6)
    confidence_summary = format_confidence_report(
        simulation,
        variant=PlanVariant(variant_used)
        if variant_used in ("aggressive", "balanced", "conservative")
        else PlanVariant.BALANCED,
    )

    # 18. Return SchedulerResult
    return SchedulerResult(
        block_plan=block_plan,
        simulation=simulation,
        variant_used=variant_used,
        calibration_applied=calibration_applied,
        starvation_alerts=starvation_alerts,
        epic_warnings=all_epic_warnings,
        probe_tasks=probe_tasks,
        critical_path=critical_path,
        work_mode_balance=work_mode_balance_dict,
        total_tasks_scored=total_tasks_scored,
        total_tasks_scheduled=len(block_plan.scheduled_slots),
        total_tasks_deferred=len(block_plan.deferred_tasks),
        confidence_summary=confidence_summary,
        validation_warnings=validation_warnings,
    )
