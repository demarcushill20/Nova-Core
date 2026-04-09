"""Symbol-aware pip helpers — shared across risk modules.

Provides pip value (price increment per pip) and pip USD value
(dollar value per pip per standard lot) for supported FX symbols.

Static lookup tables are used for known symbols.  Unknown symbols
fall back to pattern-matching heuristics (JPY → 0.01, XAU → 0.1,
else → 0.0001).  For exotic or dynamically-priced instruments,
extend the maps or replace with a broker-side query.
"""

from __future__ import annotations

# Pip value = smallest price increment that counts as 1 pip.
# 5-digit forex: 0.0001 for most pairs, 0.01 for JPY crosses.
_PIP_VALUES: dict[str, float] = {
    "EURUSD": 0.0001,
    "GBPUSD": 0.0001,
    "USDCHF": 0.0001,
    "AUDUSD": 0.0001,
    "NZDUSD": 0.0001,
    "USDCAD": 0.0001,
    "EURGBP": 0.0001,
    "EURJPY": 0.01,
    "GBPJPY": 0.01,
    "USDJPY": 0.01,
    "AUDJPY": 0.01,
    "CADJPY": 0.01,
    "NZDJPY": 0.01,
    "CHFJPY": 0.01,
    "EURAUD": 0.0001,
    "EURNZD": 0.0001,
    "EURCHF": 0.0001,
    "GBPAUD": 0.0001,
    "GBPNZD": 0.0001,
    "GBPCHF": 0.0001,
    "AUDNZD": 0.0001,
    "AUDCAD": 0.0001,
    "XAUUSD": 0.1,  # Gold: 1 pip = $0.10
}

# USD value per pip per standard lot (100,000 units of base currency).
# For USD-denominated pairs (EURUSD, GBPUSD, etc.) this is ~$10.
# JPY crosses use a conservative ~$7 estimate to err on the side of
# OVER-estimating risk (which is safer for position sizing).
_PIP_USD_VALUES: dict[str, float] = {
    "EURUSD": 10.0,
    "GBPUSD": 10.0,
    "AUDUSD": 10.0,
    "NZDUSD": 10.0,
    "USDCHF": 10.0,  # approximate (varies with USDCHF rate)
    "USDCAD": 10.0,  # approximate
    "EURGBP": 10.0,  # approximate
    "EURJPY": 7.0,   # conservative approximation (varies with USDJPY rate)
    "GBPJPY": 7.0,
    "USDJPY": 7.0,   # conservative approximation
    "AUDJPY": 7.0,
    "CADJPY": 7.0,
    "NZDJPY": 7.0,
    "CHFJPY": 7.0,
    "EURAUD": 10.0,
    "EURNZD": 10.0,
    "EURCHF": 10.0,
    "GBPAUD": 10.0,
    "GBPNZD": 10.0,
    "GBPCHF": 10.0,
    "AUDNZD": 10.0,
    "AUDCAD": 10.0,
    "XAUUSD": 10.0,  # Gold: ~$10/pip for 1 standard lot (100 oz)
}


def _clean_symbol(symbol: str) -> str:
    """Strip broker suffixes (.ftmo, .sim, etc.) and normalise."""
    return symbol.split(".")[0].upper()


def pip_value(symbol: str) -> float:
    """Return the pip increment for *symbol*.

    Uses the static lookup table first, then falls back to heuristics:
    JPY pairs → 0.01, XAU/Gold → 0.1, else → 0.0001.

    >>> pip_value("EURUSD")
    0.0001
    >>> pip_value("USDJPY")
    0.01
    """
    clean = _clean_symbol(symbol)
    if clean in _PIP_VALUES:
        return _PIP_VALUES[clean]
    # Heuristic fallback for unknown symbols
    if "JPY" in clean:
        return 0.01
    if "XAU" in clean or "GOLD" in clean:
        return 0.1
    return 0.0001


def pip_usd_value(symbol: str) -> float:
    """Return the approximate USD value per pip per standard lot for *symbol*.

    Uses the static lookup table first, then falls back to heuristics.
    Defaults are conservative and err on the side of OVER-estimating risk
    (which is safer for position sizing).

    >>> pip_usd_value("EURUSD")
    10.0
    """
    clean = _clean_symbol(symbol)
    if clean in _PIP_USD_VALUES:
        return _PIP_USD_VALUES[clean]
    # Heuristic fallback for unknown symbols
    if "JPY" in clean:
        return 7.0  # Conservative approximation
    if "XAU" in clean or "GOLD" in clean:
        return 10.0
    return 10.0  # Standard forex with USD quote
