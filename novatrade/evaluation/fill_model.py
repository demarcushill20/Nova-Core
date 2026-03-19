"""Sacred fill model — defines how orders are filled in the simulation.

THIS MODULE IS SACRED. The AutoResearch agent must NOT modify these defaults.
Any changes to fill model parameters invalidate all prior backtest results
and require full re-evaluation. Version hash tracks configuration identity.

Fill model governs:
- Stop-order fill behavior (touch vs close)
- Same-bar ambiguity resolution (SL vs TP precedence)
- Gap-through handling (fill at open vs stop price)
- High/Low check for intrabar stop triggers
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

VERSION: str = "1.0.0"


@dataclass(frozen=True)
class FillModelConfig:
    """Immutable fill model configuration.

    Conservative defaults: when both SL and TP are hit on the same bar,
    the stop loss wins (pessimistic assumption). Stop orders fill when
    High/Low crosses the stop price. Gap-through fills at bar open.
    """

    stop_first_on_ambiguity: bool = True
    """If both SL and TP hit on the same bar, stop loss wins (conservative)."""

    fill_on_touch: bool = True
    """Stop orders fill when bar High/Low crosses the stop price."""

    gap_fill_at_open: bool = True
    """If bar opens past the stop level, fill at open price (not stop price)."""

    use_high_low_check: bool = True
    """Use bar High/Low to determine if a stop order was triggered."""

    def version_hash(self) -> str:
        """SHA-256 hash of all config fields + VERSION for identity tracking."""
        payload = asdict(self)
        payload["__version__"] = VERSION
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


DEFAULT_FILL_MODEL = FillModelConfig()
