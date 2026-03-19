"""Sacred commission model — defines commission assumptions for backtesting.

THIS MODULE IS SACRED. The AutoResearch agent must NOT modify these defaults.
Any changes to commission parameters invalidate all prior backtest results
and require full re-evaluation. Version hash tracks configuration identity.

Commission model governs:
- Per-lot commission in USD (OANDA: zero, embedded in spread)
- Per-side vs round-trip commission structure
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

VERSION: str = "1.0.0"


@dataclass(frozen=True)
class CommissionModelConfig:
    """Immutable commission model configuration.

    Default: zero commission (OANDA model where cost is embedded in spread).
    For ECN brokers, set commission_per_lot_usd and commission_per_side=True.
    """

    commission_per_lot_usd: float = 0.0
    """Commission per standard lot in USD. OANDA: 0 (embedded in spread)."""

    commission_per_side: bool = False
    """If True, commission is charged per side (entry + exit separately)."""

    def version_hash(self) -> str:
        """SHA-256 hash of all config fields + VERSION for identity tracking."""
        payload = asdict(self)
        payload["__version__"] = VERSION
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


DEFAULT_COMMISSION_MODEL = CommissionModelConfig()
