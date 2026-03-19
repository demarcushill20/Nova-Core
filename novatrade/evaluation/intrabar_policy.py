"""Sacred intrabar execution policy — resolves same-bar ambiguities (P0.4).

THIS MODULE IS SACRED. The AutoResearch agent must NOT modify these defaults.
Any changes to intrabar policy invalidate all prior backtest results
and require full re-evaluation. Version hash tracks configuration identity.

Intrabar policy governs:
- Same-bar SL/TP ambiguity resolution (conservative: stop first)
- Same-bar entry+exit allowance
- Gap-through fill pricing (open price vs stop price)
- Weekend gap handling for held positions
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import Enum

VERSION: str = "1.0.0"


class AmbiguityResolution(Enum):
    """How to resolve same-bar SL/TP ambiguity."""

    STOP_FIRST = "stop_first"
    """Conservative: stop loss wins when both SL and TP hit same bar."""

    TARGET_FIRST = "target_first"
    """Optimistic: take profit wins when both SL and TP hit same bar."""

    PROPORTIONAL = "proportional"
    """Statistical: allocate proportionally based on distance ratios."""


@dataclass(frozen=True)
class IntrabarPolicyConfig:
    """Immutable intrabar execution policy configuration.

    Conservative defaults for maximum backtest realism.
    STOP_FIRST ensures we never overestimate performance from
    same-bar ambiguous outcomes.
    """

    same_bar_sl_tp: AmbiguityResolution = AmbiguityResolution.STOP_FIRST
    """How to resolve when both SL and TP are hit on the same bar."""

    same_bar_entry_exit: bool = True
    """Allow entry and exit on the same bar (if stop-order triggered by H/L)."""

    gap_fill_policy: str = "open_price"
    """Fill at open price on gap-through, not the original stop price."""

    weekend_gap_policy: str = "monday_open"
    """Use Monday open for weekend-held positions that gap through stops."""

    def version_hash(self) -> str:
        """SHA-256 hash of all config fields + VERSION for identity tracking."""
        payload = asdict(self)
        # Enum needs manual serialization for JSON
        payload["same_bar_sl_tp"] = self.same_bar_sl_tp.value
        payload["__version__"] = VERSION
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


DEFAULT_INTRABAR_POLICY = IntrabarPolicyConfig()
