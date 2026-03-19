"""Sacred spread model — defines spread assumptions per pair and session.

THIS MODULE IS SACRED. The AutoResearch agent must NOT modify these defaults.
Any changes to spread parameters invalidate all prior backtest results
and require full re-evaluation. Version hash tracks configuration identity.

Spread model governs:
- Average spread assumption (used by default)
- Per-session spread overrides (London, NY, Asian, Overlap)
- Session-aware spread toggle (Phase 7 enables granular session spreads)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

VERSION: str = "1.0.0"


@dataclass(frozen=True)
class SpreadModelConfig:
    """Immutable spread model configuration.

    Default: EURUSD with 1.0 pip average spread (OANDA demo typical).
    Session-specific spreads are defined but disabled by default —
    use_session_spreads=True enables Phase 7 session-aware costing.
    """

    pair: str = "EURUSD"
    """Trading pair this spread model applies to."""

    avg_spread_pips: float = 1.0
    """Average spread in pips, used when session-aware is off."""

    london_spread_pips: float = 0.8
    """Spread during London session (tightest liquidity)."""

    ny_spread_pips: float = 0.9
    """Spread during New York session."""

    asian_spread_pips: float = 1.5
    """Spread during Asian session (widest for EUR pairs)."""

    overlap_spread_pips: float = 0.7
    """Spread during London/NY overlap (peak liquidity)."""

    use_session_spreads: bool = False
    """Use session-specific spreads instead of avg. Phase 7 enables this."""

    def version_hash(self) -> str:
        """SHA-256 hash of all config fields + VERSION for identity tracking."""
        payload = asdict(self)
        payload["__version__"] = VERSION
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


DEFAULT_SPREAD_MODEL = SpreadModelConfig()
