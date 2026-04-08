"""Symbol-aware pip helpers — shared across risk modules.

Provides pip value (price increment per pip) and pip USD value
(dollar value per pip per standard lot) for supported FX symbols.

These are static lookups. For exotic or dynamically-priced instruments,
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
    "XAUUSD": 0.01,  # Gold: 1 pip = $0.01
}

# USD value per pip per standard lot (100,000 units of base currency).
# For USD-denominated pairs (EURUSD, GBPUSD, etc.) this is ~$10.
# For JPY crosses the pip is 0.01, so per-lot value differs.
_PIP_USD_VALUES: dict[str, float] = {
    "EURUSD": 10.0,
    "GBPUSD": 10.0,
    "AUDUSD": 10.0,
    "NZDUSD": 10.0,
    "USDCHF": 10.0,  # approximate (varies with USDCHF rate)
    "USDCAD": 10.0,  # approximate
    "EURGBP": 10.0,  # approximate
    "EURJPY": 10.0,  # approximate (varies with USDJPY rate)
    "GBPJPY": 10.0,
    "USDJPY": 10.0,  # approximate
    "AUDJPY": 10.0,
    "CADJPY": 10.0,
    "NZDJPY": 10.0,
    "CHFJPY": 10.0,
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

_DEFAULT_PIP_VALUE = 0.0001
_DEFAULT_PIP_USD_VALUE = 10.0


def pip_value(symbol: str) -> float:
    """Return the pip increment for *symbol*.

    >>> pip_value("EURUSD")
    0.0001
    >>> pip_value("USDJPY")
    0.01
    """
    # Strip common broker suffixes (.ftmo, .sim, etc.)
    clean = symbol.split(".")[0].upper()
    return _PIP_VALUES.get(clean, _DEFAULT_PIP_VALUE)


def pip_usd_value(symbol: str) -> float:
    """Return the approximate USD value per pip per standard lot for *symbol*.

    >>> pip_usd_value("EURUSD")
    10.0
    """
    clean = symbol.split(".")[0].upper()
    return _PIP_USD_VALUES.get(clean, _DEFAULT_PIP_USD_VALUE)
