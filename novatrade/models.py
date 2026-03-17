"""Provider-neutral domain models for NovaTrade.

These models form the execution contract between NovaTrade's core logic and
any broker adapter.  They never import vendor SDKs — adapters translate
vendor objects into these types at the boundary.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class OrderStatus(Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class AccountMode(Enum):
    """Operating mode per White Paper I §3.3."""

    DEMO = "DEMO"
    CHALLENGE = "CHALLENGE"
    FUNDED_PRESERVATION = "FUNDED_PRESERVATION"
    FUNDED_SCALING = "FUNDED_SCALING"


class RiskVerdict(Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    HALT = "HALT"  # deny AND halt all future trading until operator resumes


class RiskAction(Enum):
    """Portfolio-level risk action — signals what the caller should do."""

    NONE = "NONE"
    HALT_TRADING = "HALT_TRADING"  # halt all new order placement
    SUSPEND_STRATEGY = "SUSPEND_STRATEGY"  # pause this strategy only
    FLATTEN_POSITIONS = "FLATTEN_POSITIONS"  # close all open positions
    CANCEL_PENDING = "CANCEL_PENDING"  # cancel all pending orders


class HealthState(Enum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"


# ---------------------------------------------------------------------------
# Order models
# ---------------------------------------------------------------------------


@dataclass
class OrderRequest:
    """Intent to place an order — submitted to the adapter."""

    symbol: str
    side: OrderSide
    order_type: OrderType
    volume: float
    price: float | None = None  # required for LIMIT/STOP
    stop_loss: float | None = None
    take_profit: float | None = None
    comment: str = ""
    idempotency_key: str = ""  # duplicate-prevention token
    strategy_id: str = ""
    strategy_version: str = ""

    def __post_init__(self) -> None:
        if self.volume <= 0:
            raise ValueError(f"volume must be positive, got {self.volume}")
        if self.order_type in (OrderType.LIMIT, OrderType.STOP) and self.price is None:
            raise ValueError(f"{self.order_type.value} orders require a price")


@dataclass
class OrderResult:
    """Outcome returned by the adapter after an order attempt."""

    ok: bool
    order_id: str = ""
    status: OrderStatus = OrderStatus.PENDING
    fill_price: float | None = None
    filled_volume: float | None = None
    error: str = ""
    broker_raw: dict | None = None  # opaque vendor payload for logging
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Position / Account
# ---------------------------------------------------------------------------


@dataclass
class Position:
    """A single open position."""

    position_id: str
    symbol: str
    side: OrderSide
    volume: float
    open_price: float
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    stop_loss: float | None = None
    take_profit: float | None = None
    open_time: float = 0.0
    strategy_id: str = ""
    comment: str = ""


@dataclass
class AccountState:
    """Snapshot of the trading account — adapter-provided."""

    balance: float
    equity: float
    margin: float = 0.0
    free_margin: float = 0.0
    currency: str = "USD"
    leverage: int = 100
    mode: AccountMode = AccountMode.DEMO
    open_positions: int = 0
    server: str = ""
    broker: str = ""
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@dataclass
class HealthStatus:
    """Adapter / connection health report."""

    state: HealthState
    connected: bool = False
    latency_ms: float | None = None
    message: str = ""
    last_heartbeat: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------


@dataclass
class RiskCheckResult:
    """Outcome of a single pre-trade check."""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class RiskDecision:
    """Pre-trade risk gate verdict — used by the risk layer."""

    verdict: RiskVerdict
    checks: list[RiskCheckResult] = field(default_factory=list)
    reason: str = ""
    rule: str = ""  # which rule triggered first denial
    policy_layer: int = -1  # which layer produced the verdict (-1 = N/A)
    actions: list[RiskAction] = field(default_factory=list)
    request: OrderRequest | None = None
    timestamp: float = field(default_factory=time.time)

    @property
    def denied(self) -> bool:
        return self.verdict in (RiskVerdict.DENY, RiskVerdict.HALT)

    @property
    def halted(self) -> bool:
        return self.verdict == RiskVerdict.HALT

    @property
    def failed_checks(self) -> list[RiskCheckResult]:
        return [c for c in self.checks if not c.passed]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


class ExecutionOutcome(Enum):
    """What happened during an execution attempt."""

    DENIED = "DENIED"  # gate rejected — adapter never called
    FILLED = "FILLED"  # adapter placed and order filled
    ADAPTER_ERROR = "ADAPTER_ERROR"  # adapter was called but returned failure
    EXCEPTION = "EXCEPTION"  # unexpected error during execution


@dataclass
class ExecutionResult:
    """End-to-end outcome of a single order execution attempt."""

    outcome: ExecutionOutcome
    risk_decision: RiskDecision | None = None
    order_result: OrderResult | None = None
    error: str = ""
    elapsed_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return self.outcome == ExecutionOutcome.FILLED

    @property
    def denied(self) -> bool:
        return self.outcome == ExecutionOutcome.DENIED


# ---------------------------------------------------------------------------
# Monitoring
# ---------------------------------------------------------------------------


@dataclass
class HealthSnapshot:
    """Point-in-time health and account summary for operator review."""

    adapter_health: HealthStatus
    account: AccountState | None = None
    position_count: int = 0
    total_unrealized_pnl: float = 0.0
    error: str = ""
    timestamp: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return self.adapter_health.state != HealthState.DOWN and not self.error


class MismatchType(Enum):
    """What kind of position mismatch was found."""

    MISSING = "MISSING"  # expected locally but not at broker
    UNEXPECTED = "UNEXPECTED"  # at broker but not expected locally
    VOLUME = "VOLUME"  # same position, different volume
    SIDE = "SIDE"  # same position, different side
    STOP_LOSS = "STOP_LOSS"  # SL differs
    TAKE_PROFIT = "TAKE_PROFIT"  # TP differs


@dataclass
class PositionMismatch:
    """A single discrepancy between expected and actual position state."""

    mismatch_type: MismatchType
    symbol: str
    detail: str
    expected_value: str = ""
    actual_value: str = ""
    position_id: str = ""


@dataclass
class ReconciliationResult:
    """Outcome of comparing expected vs actual broker state."""

    ok: bool
    mismatches: list[PositionMismatch] = field(default_factory=list)
    expected_count: int = 0
    actual_count: int = 0
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Evidence / validation
# ---------------------------------------------------------------------------


class EvidenceType(Enum):
    """What kind of event is being recorded."""

    EXECUTION = "EXECUTION"
    HEALTH_SNAPSHOT = "HEALTH_SNAPSHOT"
    RECONCILIATION = "RECONCILIATION"
    RISK_DECISION = "RISK_DECISION"
    RISK_HALT = "RISK_HALT"
    RISK_WARNING = "RISK_WARNING"
    ADAPTER_ERROR = "ADAPTER_ERROR"


@dataclass
class EvidenceRecord:
    """Single audit event captured during a validation session."""

    event_type: EvidenceType
    data: dict = field(default_factory=dict)
    error: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type.value,
            "data": self.data,
            "error": self.error,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict) -> EvidenceRecord:
        return cls(
            event_type=EvidenceType(d["event_type"]),
            data=d.get("data", {}),
            error=d.get("error", ""),
            timestamp=d.get("timestamp", 0.0),
        )


@dataclass
class ValidationReport:
    """Summary of evidence collected across validation sessions."""

    total_records: int = 0
    execution_count: int = 0
    filled_count: int = 0
    denied_count: int = 0
    adapter_error_count: int = 0
    exception_count: int = 0
    health_snapshots: int = 0
    health_ok_count: int = 0
    reconciliation_count: int = 0
    reconciliation_ok_count: int = 0
    mismatch_count: int = 0
    recent_failures: list[dict] = field(default_factory=list)
    time_range_start: float = 0.0
    time_range_end: float = 0.0
    timestamp: float = field(default_factory=time.time)

    @property
    def stable(self) -> bool:
        """Conservative stability check for MVP demo validation."""
        if self.total_records == 0:
            return False
        if self.adapter_error_count > 0 or self.exception_count > 0:
            return False
        return self.mismatch_count <= 0


class MvpVerdict(Enum):
    """MVP go/no-go verdict."""

    READY = "READY"  # stable enough for continued demo validation
    NEEDS_REMEDIATION = "NEEDS_REMEDIATION"  # issues found, fixable before proceeding
    NOT_READY = "NOT_READY"  # fundamental problems, cannot proceed
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # too few records to judge


@dataclass
class MvpDecision:
    """MVP validation decision package."""

    verdict: MvpVerdict
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    next_action: str = ""
    timestamp: float = field(default_factory=time.time)


class RemediationSeverity(Enum):
    """How urgent a remediation item is."""

    BLOCKER = "BLOCKER"  # must fix before proceeding
    WARNING = "WARNING"  # acceptable but should be watched
    INFO = "INFO"  # informational, no action required


@dataclass
class RemediationItem:
    """A single actionable remediation step."""

    category: str
    description: str
    action: str
    severity: RemediationSeverity


@dataclass
class ReadinessPackage:
    """Final MVP readiness and remediation package for operator review."""

    verdict: MvpVerdict
    ready: bool
    items: list[RemediationItem] = field(default_factory=list)
    do_not_proceed: list[str] = field(default_factory=list)
    evidence_gaps: list[str] = field(default_factory=list)
    next_step: str = ""
    timestamp: float = field(default_factory=time.time)

    @property
    def blocker_count(self) -> int:
        return sum(1 for i in self.items if i.severity == RemediationSeverity.BLOCKER)

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.items if i.severity == RemediationSeverity.WARNING)


class AuditSeverity(Enum):
    """Migration-readiness audit finding severity."""

    READY = "READY"  # no change needed
    MINOR_REFACTOR = "MINOR_REFACTOR"  # small cleanup recommended
    BLOCKING_COUPLING = "BLOCKING_COUPLING"  # must fix before provider swap


@dataclass
class AuditFinding:
    """A single migration-readiness finding."""

    location: str
    description: str
    severity: AuditSeverity
    recommendation: str
    defer_ok: bool = True  # can this fix be deferred to swap time?


@dataclass
class MigrationAuditResult:
    """Migration-readiness audit outcome."""

    swap_ready: bool
    findings: list[AuditFinding] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    @property
    def blocking_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == AuditSeverity.BLOCKING_COUPLING)

    @property
    def minor_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == AuditSeverity.MINOR_REFACTOR)

    @property
    def ready_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == AuditSeverity.READY)


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


@dataclass
class SymbolPrice:
    """Current bid/ask for a symbol."""

    symbol: str
    bid: float
    ask: float
    spread: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.spread:
            self.spread = round(self.ask - self.bid, 6)


@dataclass
class Candle:
    """Single OHLCV candle."""

    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    symbol: str = ""
    timeframe: str = ""
