"""NovaTrade configuration — typed, validated, secrets from environment.

Secrets are loaded from the environment (expected to be injected via
/etc/novacore/novatrade.env or systemd EnvironmentFile).  No secrets are
hardcoded or stored in this file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from novatrade.models import AccountMode

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_ENV_FILE = Path("/etc/novacore/novatrade.env")
_DEFAULT_MAX_DAILY_DRAWDOWN_PCT = 5.0  # typical prop-firm limit
_DEFAULT_MAX_TOTAL_DRAWDOWN_PCT = 10.0
_DEFAULT_MAX_POSITIONS = 5
_DEFAULT_MAX_VOLUME_PER_TRADE = 1.0  # lots
_DEFAULT_SPREAD_CEILING_POINTS = 30.0


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class MetaApiConfig:
    """MetaApi-specific provider settings (Phase 2+)."""

    token: str = ""
    account_id: str = ""
    domain: str = "agiliumtrade.agiliumtrade.ai"
    region: str = "new-york"
    application: str = "NovaTrade"

    @classmethod
    def from_env(cls) -> MetaApiConfig:
        return cls(
            token=os.environ.get("METAAPI_TOKEN", ""),
            account_id=os.environ.get("METAAPI_ACCOUNT_ID", ""),
            domain=os.environ.get("METAAPI_DOMAIN", cls.domain),
            region=os.environ.get("METAAPI_REGION", cls.region),
            application=os.environ.get("METAAPI_APPLICATION", cls.application),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.token:
            errors.append("METAAPI_TOKEN is not set")
        if not self.account_id:
            errors.append("METAAPI_ACCOUNT_ID is not set")
        return errors


@dataclass
class RiskConfig:
    """Risk parameters — prop-firm-safe defaults."""

    max_daily_drawdown_pct: float = _DEFAULT_MAX_DAILY_DRAWDOWN_PCT
    max_total_drawdown_pct: float = _DEFAULT_MAX_TOTAL_DRAWDOWN_PCT
    max_positions: int = _DEFAULT_MAX_POSITIONS
    max_volume_per_trade: float = _DEFAULT_MAX_VOLUME_PER_TRADE
    min_volume_per_trade: float = 0.01
    spread_ceiling_points: float = _DEFAULT_SPREAD_CEILING_POINTS
    cooldown_seconds: int = 60
    max_trades_per_day: int = 20
    news_blackout_minutes: int = 15
    require_stop_loss: bool = True
    max_drawdown_equity_pct: float = _DEFAULT_MAX_DAILY_DRAWDOWN_PCT

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.max_daily_drawdown_pct <= 0:
            errors.append("max_daily_drawdown_pct must be positive")
        if self.max_total_drawdown_pct <= 0:
            errors.append("max_total_drawdown_pct must be positive")
        if self.max_positions < 1:
            errors.append("max_positions must be >= 1")
        if self.max_volume_per_trade <= 0:
            errors.append("max_volume_per_trade must be positive")
        if self.min_volume_per_trade <= 0:
            errors.append("min_volume_per_trade must be positive")
        if self.max_trades_per_day < 1:
            errors.append("max_trades_per_day must be >= 1")
        return errors


@dataclass
class NovaTradeCfg:
    """Top-level NovaTrade configuration."""

    mode: AccountMode = AccountMode.DEMO
    provider: str = "metaapi"  # future: could be "direct_mt5"
    symbols: list[str] = field(default_factory=lambda: ["EURUSD"])
    timeframes: list[str] = field(default_factory=lambda: ["H1"])
    metaapi: MetaApiConfig = field(default_factory=MetaApiConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    log_dir: Path = Path("LOGS/novatrade")
    data_dir: Path = Path("OUTPUT/novatrade")
    dry_run: bool = True  # safety default: no real orders

    @classmethod
    def load(cls, env_file: Path | None = None) -> NovaTradeCfg:
        """Build config from environment variables.

        If *env_file* exists, its lines are loaded into os.environ first
        (simple KEY=VALUE parsing, no shell expansion).
        """
        env_path = env_file or _DEFAULT_ENV_FILE
        if env_path.is_file():
            _load_env_file(env_path)

        mode_str = os.environ.get("NOVATRADE_MODE", "DEMO").upper()
        try:
            mode = AccountMode(mode_str)
        except ValueError:
            mode = AccountMode.DEMO

        symbols_raw = os.environ.get("NOVATRADE_SYMBOLS", "EURUSD")
        timeframes_raw = os.environ.get("NOVATRADE_TIMEFRAMES", "H1")

        dry_run_raw = os.environ.get("NOVATRADE_DRY_RUN", "true")

        return cls(
            mode=mode,
            provider=os.environ.get("NOVATRADE_PROVIDER", "metaapi"),
            symbols=[s.strip() for s in symbols_raw.split(",") if s.strip()],
            timeframes=[t.strip() for t in timeframes_raw.split(",") if t.strip()],
            metaapi=MetaApiConfig.from_env(),
            risk=RiskConfig(),
            dry_run=dry_run_raw.lower() in ("true", "1", "yes"),
        )

    def validate(self) -> list[str]:
        """Return a list of config errors (empty = valid)."""
        errors: list[str] = []
        if not self.symbols:
            errors.append("at least one symbol is required")
        if not self.timeframes:
            errors.append("at least one timeframe is required")
        if self.provider == "metaapi":
            errors.extend(self.metaapi.validate())
        errors.extend(self.risk.validate())
        return errors


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_env_file(path: Path) -> None:
    """Parse a simple KEY=VALUE env file into os.environ."""
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            os.environ.setdefault(key, value)
