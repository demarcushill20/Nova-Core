"""Sacred slippage model — defines slippage assumptions for backtesting.

THIS MODULE IS SACRED. The AutoResearch agent must NOT modify these defaults.
Any changes to slippage parameters invalidate all prior backtest results
and require full re-evaluation. Version hash tracks configuration identity.

Slippage model governs:
- Normal slippage assumption (applied to every fill)
- Stress slippage for cost stress testing scenarios
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

VERSION: str = "1.0.0"


@dataclass(frozen=True)
class SlippageModelConfig:
    """Immutable slippage model configuration.

    Default: 0.5 pips normal slippage, 1.5 pips stress scenario.
    Stress slippage is used in Stage C robustness testing to verify
    strategies survive adverse execution conditions.
    """

    slippage_pips: float = 0.5
    """Normal slippage in pips, applied to every fill."""

    stress_slippage_pips: float = 1.5
    """Elevated slippage for cost stress testing (Stage C)."""

    def version_hash(self) -> str:
        """SHA-256 hash of all config fields + VERSION for identity tracking."""
        payload = asdict(self)
        payload["__version__"] = VERSION
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


DEFAULT_SLIPPAGE_MODEL = SlippageModelConfig()
