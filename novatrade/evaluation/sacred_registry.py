"""Sacred model registry — single source of truth for all evaluation model versions.

Collects version hashes from every sacred module into one verified bundle.
The combined hash changes whenever ANY sacred model changes, making it
trivial to detect whether two experiments used identical evaluation assumptions.

Sacred models:
  - fill_model: Stop-order fill behavior, gap-through handling
  - spread_model: Spread assumptions per pair/session
  - slippage_model: Slippage assumptions (normal + stress)
  - commission_model: Commission structure (per-lot, per-side)
  - session_calendar: Trading session hours, weekend gaps
  - intrabar_policy: Same-bar SL/TP ambiguity resolution
  - metrics: Metric computation definitions and formulas
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass

from novatrade.backtest.metrics import metrics_version_hash
from novatrade.evaluation.commission_model import (
    DEFAULT_COMMISSION_MODEL,
    CommissionModelConfig,
)
from novatrade.evaluation.fill_model import (
    DEFAULT_FILL_MODEL,
    FillModelConfig,
)
from novatrade.evaluation.intrabar_policy import (
    DEFAULT_INTRABAR_POLICY,
    IntrabarPolicyConfig,
)
from novatrade.evaluation.session_calendar import (
    DEFAULT_SESSION_CALENDAR,
    SessionCalendarConfig,
)
from novatrade.evaluation.slippage_model import (
    DEFAULT_SLIPPAGE_MODEL,
    SlippageModelConfig,
)
from novatrade.evaluation.spread_model import (
    DEFAULT_SPREAD_MODEL,
    SpreadModelConfig,
)


@dataclass(frozen=True)
class SacredBundle:
    """Immutable snapshot of all sacred model version hashes.

    The combined_hash is a single SHA-256 digest of all individual hashes,
    providing a single comparison point: if combined_hash matches between
    two experiments, every evaluation assumption was identical.
    """

    fill_model_hash: str
    spread_model_hash: str
    slippage_model_hash: str
    commission_model_hash: str
    session_calendar_hash: str
    intrabar_policy_hash: str
    metrics_hash: str
    combined_hash: str

    def to_dict(self) -> dict:
        return asdict(self)


def collect_sacred_hashes(
    fill_model: FillModelConfig | None = None,
    spread_model: SpreadModelConfig | None = None,
    slippage_model: SlippageModelConfig | None = None,
    commission_model: CommissionModelConfig | None = None,
    session_calendar: SessionCalendarConfig | None = None,
    intrabar_policy: IntrabarPolicyConfig | None = None,
) -> SacredBundle:
    """Collect version hashes from all sacred models.

    Uses DEFAULT_* instances when no override is provided. This is the
    normal case — overrides exist only for testing or future multi-pair
    configurations where spread/commission differ.

    Returns a SacredBundle with individual hashes and a combined digest.
    """
    fm = (fill_model or DEFAULT_FILL_MODEL).version_hash()
    sp = (spread_model or DEFAULT_SPREAD_MODEL).version_hash()
    sl = (slippage_model or DEFAULT_SLIPPAGE_MODEL).version_hash()
    cm = (commission_model or DEFAULT_COMMISSION_MODEL).version_hash()
    sc = (session_calendar or DEFAULT_SESSION_CALENDAR).version_hash()
    ib = (intrabar_policy or DEFAULT_INTRABAR_POLICY).version_hash()
    mt = metrics_version_hash()

    # Deterministic combined hash from all components
    parts = f"{fm}{sp}{sl}{cm}{sc}{ib}{mt}"
    combined = hashlib.sha256(parts.encode()).hexdigest()[:16]

    return SacredBundle(
        fill_model_hash=fm,
        spread_model_hash=sp,
        slippage_model_hash=sl,
        commission_model_hash=cm,
        session_calendar_hash=sc,
        intrabar_policy_hash=ib,
        metrics_hash=mt,
        combined_hash=combined,
    )


def verify_sacred_unchanged(bundle: SacredBundle) -> list[str]:
    """Compare a saved bundle against current defaults.

    Returns a list of sacred models that have changed. An empty list
    means all evaluation assumptions are identical to when the bundle
    was created.
    """
    current = collect_sacred_hashes()
    diffs: list[str] = []

    checks = [
        ("fill_model", bundle.fill_model_hash, current.fill_model_hash),
        ("spread_model", bundle.spread_model_hash, current.spread_model_hash),
        ("slippage_model", bundle.slippage_model_hash, current.slippage_model_hash),
        ("commission_model", bundle.commission_model_hash, current.commission_model_hash),
        ("session_calendar", bundle.session_calendar_hash, current.session_calendar_hash),
        ("intrabar_policy", bundle.intrabar_policy_hash, current.intrabar_policy_hash),
        ("metrics", bundle.metrics_hash, current.metrics_hash),
    ]

    for name, saved, current_hash in checks:
        if saved != current_hash:
            diffs.append(f"{name}: {saved} -> {current_hash}")

    return diffs
