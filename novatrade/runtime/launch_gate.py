"""Launch gate — Final Demo Launch Phase (Phase 9).

Implements the activation gate that determines whether the NovaTrade IRB
stack can enter active demo mode.  Evaluates:

  1. Configuration readiness (env vars, credentials, paths)
  2. Adapter readiness (MetaApi reachable if active mode)
  3. Risk governance readiness (engine initialized, not halted)
  4. Monitoring readiness (OpsMonitor, MonitorLoop wired)
  5. External confirmations (TradingView Pine, alert path)
  6. Unresolved blockers from earlier phases

Produces a LaunchReadiness verdict:
  - READY_FOR_ACTIVE_DEMO
  - CONDITIONALLY_READY
  - NOT_READY
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum

from novatrade.config import NovaTradeCfg
from novatrade.models import EvidenceRecord, EvidenceType
from novatrade.validation.evidence import EvidenceRecorder

log = logging.getLogger("novatrade.runtime.launch_gate")


# ---------------------------------------------------------------------------
# Launch mode
# ---------------------------------------------------------------------------


class LaunchMode(Enum):
    """Runtime launch mode — explicit, operator-controlled."""

    DRY_RUN = "dry_run"
    ACTIVE_READY = "active_ready"
    ACTIVE_DEMO = "active_demo"


# ---------------------------------------------------------------------------
# Readiness verdict
# ---------------------------------------------------------------------------


class ReadinessVerdict(Enum):
    READY_FOR_ACTIVE_DEMO = "READY_FOR_ACTIVE_DEMO"
    CONDITIONALLY_READY = "CONDITIONALLY_READY"
    NOT_READY = "NOT_READY"


class CheckCategory(Enum):
    CODE = "code"
    CONFIG = "config"
    ADAPTER = "adapter"
    RISK = "risk"
    MONITORING = "monitoring"
    EXTERNAL = "external"
    OPERATOR = "operator"


@dataclass
class GateCheck:
    """A single launch-gate check result."""

    name: str
    category: CheckCategory
    passed: bool
    detail: str = ""
    required_for_active: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "category": self.category.value,
            "passed": self.passed,
            "detail": self.detail,
            "required_for_active": self.required_for_active,
        }


@dataclass
class LaunchReadiness:
    """Full launch-readiness assessment."""

    verdict: ReadinessVerdict
    launch_mode: LaunchMode
    checks: list[GateCheck] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    operator_tasks: list[str] = field(default_factory=list)
    external_confirmations: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def failed_count(self) -> int:
        return sum(1 for c in self.checks if not c.passed)

    @property
    def total_count(self) -> int:
        return len(self.checks)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "launch_mode": self.launch_mode.value,
            "checks": [c.to_dict() for c in self.checks],
            "blockers": self.blockers,
            "warnings": self.warnings,
            "operator_tasks": self.operator_tasks,
            "external_confirmations": self.external_confirmations,
            "passed": self.passed_count,
            "failed": self.failed_count,
            "total": self.total_count,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------


@dataclass
class StartupValidation:
    """Result of startup configuration validation."""

    ok: bool
    mode: LaunchMode
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "mode": self.mode.value,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def resolve_launch_mode() -> LaunchMode:
    """Determine launch mode from environment.

    NOVATRADE_LAUNCH_MODE can be: dry_run, active_ready, active_demo.
    Falls back to dry_run/active_ready based on NOVATRADE_DRY_RUN.
    """
    explicit = os.environ.get("NOVATRADE_LAUNCH_MODE", "").lower().strip()
    if explicit in ("dry_run", "dry-run", "dryrun"):
        return LaunchMode.DRY_RUN
    if explicit in ("active_ready", "active-ready", "activeready"):
        return LaunchMode.ACTIVE_READY
    if explicit in ("active_demo", "active-demo", "activedemo"):
        return LaunchMode.ACTIVE_DEMO

    # Fallback: infer from NOVATRADE_DRY_RUN
    dry_run_raw = os.environ.get("NOVATRADE_DRY_RUN", "true").lower()
    if dry_run_raw in ("true", "1", "yes"):
        return LaunchMode.DRY_RUN
    return LaunchMode.ACTIVE_READY


def validate_startup(cfg: NovaTradeCfg, mode: LaunchMode) -> StartupValidation:
    """Validate configuration for the requested launch mode.

    - dry_run: minimal validation, works without credentials
    - active_ready: full validation, fails on missing credentials
    - active_demo: same as active_ready + operator confirmation required
    """
    errors: list[str] = []
    warnings: list[str] = []

    # --- Universal checks ---
    if not cfg.symbols:
        errors.append("No trading symbols configured")
    if not cfg.timeframes:
        errors.append("No timeframes configured")

    risk_errors = cfg.risk.validate()
    errors.extend(risk_errors)

    # Evidence/data path
    if not cfg.data_dir:
        errors.append("data_dir is not set")

    # --- Active mode checks ---
    if mode in (LaunchMode.ACTIVE_READY, LaunchMode.ACTIVE_DEMO):
        # MetaApi credentials
        meta_errors = cfg.metaapi.validate()
        if meta_errors:
            errors.extend(meta_errors)

        # Webhook secret
        secret = os.environ.get("NOVATRADE_WEBHOOK_SECRET", "")
        if not secret:
            errors.append("NOVATRADE_WEBHOOK_SECRET is not set — required for active mode")

        # FTMO profile (warning if not enabled)
        if not cfg.ftmo.enabled:
            warnings.append("FTMO profile not enabled — set FTMO_ENABLED=true for FTMO demo")

    # --- Dry-run specific ---
    if mode == LaunchMode.DRY_RUN and not cfg.metaapi.token:
        warnings.append("METAAPI_TOKEN not set — DryRunAdapter will be used (expected)")

    ok = len(errors) == 0
    return StartupValidation(ok=ok, mode=mode, errors=errors, warnings=warnings)


# ---------------------------------------------------------------------------
# Launch gate evaluation
# ---------------------------------------------------------------------------

# External confirmations tracked via env vars (operator sets these)
_CONFIRMATION_ENV_VARS = {
    "NOVATRADE_CONFIRM_PINE_COMPILED": "TradingView Pine script compiled without error (B-IRB-1)",
    "NOVATRADE_CONFIRM_TV_BACKTEST": "TradingView backtest verified — IRB signals fire on EURUSD H1 (B-IRB-2)",
    "NOVATRADE_CONFIRM_WEBHOOK_URL": "TradingView webhook URL configured and pointing to this server (B-P8-1)",
    "NOVATRADE_CONFIRM_ACTIVE_DEMO": "Operator acknowledges active demo mode start",
}


def evaluate_launch_gate(
    cfg: NovaTradeCfg,
    mode: LaunchMode,
    *,
    risk_engine_initialized: bool = False,
    risk_engine_halted: bool = False,
    agent_initialized: bool = False,
    monitor_initialized: bool = False,
    adapter_connected: bool = False,
    adapter_type: str = "unknown",
) -> LaunchReadiness:
    """Evaluate whether the stack can enter the requested launch mode.

    This is the activation gate. It checks everything needed before
    the system can transition to active_ready or active_demo.
    """
    checks: list[GateCheck] = []
    blockers: list[str] = []
    warnings: list[str] = []
    operator_tasks: list[str] = []
    external_confirmations: list[str] = []

    # --- 1. Code readiness ---
    checks.append(
        GateCheck(
            name="trading_agent_initialized",
            category=CheckCategory.CODE,
            passed=agent_initialized,
            detail="Trading Agent is wired and ready" if agent_initialized else "Trading Agent not initialized",
        )
    )
    checks.append(
        GateCheck(
            name="monitor_initialized",
            category=CheckCategory.MONITORING,
            passed=monitor_initialized,
            detail="OpsMonitor is wired and ready" if monitor_initialized else "OpsMonitor not initialized",
        )
    )

    # --- 2. Configuration readiness ---
    startup = validate_startup(cfg, mode)
    config_ok = startup.ok
    checks.append(
        GateCheck(
            name="config_valid",
            category=CheckCategory.CONFIG,
            passed=config_ok,
            detail="Configuration valid" if config_ok else f"Config errors: {'; '.join(startup.errors)}",
        )
    )
    if startup.warnings:
        warnings.extend(startup.warnings)

    # Webhook secret
    has_secret = bool(os.environ.get("NOVATRADE_WEBHOOK_SECRET", ""))
    if mode in (LaunchMode.ACTIVE_READY, LaunchMode.ACTIVE_DEMO):
        checks.append(
            GateCheck(
                name="webhook_secret_set",
                category=CheckCategory.CONFIG,
                passed=has_secret,
                detail="Webhook secret configured" if has_secret else "NOVATRADE_WEBHOOK_SECRET not set",
            )
        )
        if not has_secret:
            operator_tasks.append("Set NOVATRADE_WEBHOOK_SECRET environment variable")

    # --- 3. Adapter readiness ---
    is_active_adapter = adapter_type != "DryRunAdapter"
    if mode in (LaunchMode.ACTIVE_READY, LaunchMode.ACTIVE_DEMO):
        checks.append(
            GateCheck(
                name="active_adapter_selected",
                category=CheckCategory.ADAPTER,
                passed=is_active_adapter,
                detail=f"Adapter: {adapter_type}"
                if is_active_adapter
                else "Still using DryRunAdapter — need MetaApiAdapter for active mode",
            )
        )
        checks.append(
            GateCheck(
                name="adapter_connected",
                category=CheckCategory.ADAPTER,
                passed=adapter_connected,
                detail="Adapter connected to broker" if adapter_connected else "Adapter not connected",
            )
        )
        if not is_active_adapter:
            operator_tasks.append(
                "Set NOVATRADE_LAUNCH_MODE=active_ready and ensure MetaApi credentials are configured"
            )
    else:
        # Dry-run: DryRunAdapter is expected
        checks.append(
            GateCheck(
                name="dry_run_adapter",
                category=CheckCategory.ADAPTER,
                passed=True,
                detail=f"DryRunAdapter active (expected for {mode.value})",
                required_for_active=False,
            )
        )

    # --- 4. Risk governance readiness ---
    checks.append(
        GateCheck(
            name="risk_engine_initialized",
            category=CheckCategory.RISK,
            passed=risk_engine_initialized,
            detail="Risk engine initialized" if risk_engine_initialized else "Risk engine not initialized",
        )
    )
    checks.append(
        GateCheck(
            name="risk_engine_not_halted",
            category=CheckCategory.RISK,
            passed=not risk_engine_halted,
            detail="Risk engine running"
            if not risk_engine_halted
            else "Risk engine is HALTED — must clear before active demo",
        )
    )
    if risk_engine_halted:
        blockers.append("Risk engine is halted — clear halt before active demo launch")

    # --- 5. External confirmations ---
    for env_var, description in _CONFIRMATION_ENV_VARS.items():
        confirmed = os.environ.get(env_var, "").lower() in ("true", "1", "yes")
        is_active_only = True
        checks.append(
            GateCheck(
                name=env_var.lower(),
                category=CheckCategory.EXTERNAL,
                passed=confirmed,
                detail=f"CONFIRMED: {description}" if confirmed else f"PENDING: {description}",
                required_for_active=is_active_only,
            )
        )
        if not confirmed:
            external_confirmations.append(f"{env_var}: {description}")

    # --- 6. Unresolved blockers from earlier phases ---
    # These are known from Phase 8 open issues
    if mode == LaunchMode.ACTIVE_DEMO:
        # B-P8-1 is covered by NOVATRADE_CONFIRM_WEBHOOK_URL
        # B-P8-2 is covered by MetaApi credential check
        # B-IRB-1 is covered by NOVATRADE_CONFIRM_PINE_COMPILED
        # B-IRB-2 is covered by NOVATRADE_CONFIRM_TV_BACKTEST
        pass

    # --- Compute verdict ---
    required_failed = [c for c in checks if not c.passed and c.required_for_active]

    if mode == LaunchMode.DRY_RUN:
        # Dry-run only needs code + basic config — warnings are acceptable
        code_config_failed = [
            c for c in checks if not c.passed and c.category in (CheckCategory.CODE, CheckCategory.MONITORING)
        ]
        if code_config_failed:
            verdict = ReadinessVerdict.NOT_READY
            for c in code_config_failed:
                blockers.append(c.detail)
        else:
            verdict = ReadinessVerdict.READY_FOR_ACTIVE_DEMO

    elif mode == LaunchMode.ACTIVE_READY:
        if required_failed:
            # Check if only external confirmations are missing
            non_external_failures = [c for c in required_failed if c.category != CheckCategory.EXTERNAL]
            if non_external_failures:
                verdict = ReadinessVerdict.NOT_READY
                for c in non_external_failures:
                    blockers.append(c.detail)
            else:
                verdict = ReadinessVerdict.CONDITIONALLY_READY
        else:
            verdict = ReadinessVerdict.READY_FOR_ACTIVE_DEMO

    else:  # ACTIVE_DEMO
        if required_failed:
            verdict = ReadinessVerdict.NOT_READY
            for c in required_failed:
                blockers.append(c.detail)
        else:
            verdict = ReadinessVerdict.READY_FOR_ACTIVE_DEMO

    return LaunchReadiness(
        verdict=verdict,
        launch_mode=mode,
        checks=checks,
        blockers=blockers,
        warnings=warnings,
        operator_tasks=operator_tasks,
        external_confirmations=external_confirmations,
    )


# ---------------------------------------------------------------------------
# Evidence recording
# ---------------------------------------------------------------------------


def record_launch_event(
    recorder: EvidenceRecorder | None,
    event_name: str,
    data: dict,
) -> None:
    """Record a launch-gate event to the evidence trail."""
    if recorder is None:
        return
    recorder._append(
        EvidenceRecord(
            event_type=EvidenceType.MONITORING,
            data={"event": event_name, **data},
        )
    )


# ---------------------------------------------------------------------------
# Readiness report generation
# ---------------------------------------------------------------------------


def generate_readiness_report(readiness: LaunchReadiness) -> str:
    """Generate a human-readable readiness report."""
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("NOVATRADE LAUNCH READINESS ASSESSMENT")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"Verdict:     {readiness.verdict.value}")
    lines.append(f"Launch Mode: {readiness.launch_mode.value}")
    lines.append(f"Checks:      {readiness.passed_count}/{readiness.total_count} passed")
    lines.append("")

    if readiness.blockers:
        lines.append("BLOCKERS:")
        for b in readiness.blockers:
            lines.append(f"  [BLOCK] {b}")
        lines.append("")

    if readiness.warnings:
        lines.append("WARNINGS:")
        for w in readiness.warnings:
            lines.append(f"  [WARN]  {w}")
        lines.append("")

    if readiness.operator_tasks:
        lines.append("OPERATOR TASKS:")
        for t in readiness.operator_tasks:
            lines.append(f"  [ ]  {t}")
        lines.append("")

    if readiness.external_confirmations:
        lines.append("EXTERNAL CONFIRMATIONS PENDING:")
        for e in readiness.external_confirmations:
            lines.append(f"  [ ]  {e}")
        lines.append("")

    lines.append("DETAILED CHECKS:")
    for c in readiness.checks:
        status = "PASS" if c.passed else "FAIL"
        lines.append(f"  [{status}] {c.name} ({c.category.value}): {c.detail}")

    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)
