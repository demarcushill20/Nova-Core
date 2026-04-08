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
_OVERRIDE_ENV_FILE = Path("configs/novatrade.override.env")
_DEFAULT_MAX_DAILY_DRAWDOWN_PCT = 5.0  # typical prop-firm limit
_DEFAULT_MAX_TOTAL_DRAWDOWN_PCT = 10.0
_DEFAULT_MAX_POSITIONS = 1  # FTMO-safe: single position for IRB strategy
_DEFAULT_MAX_VOLUME_PER_TRADE = 5.0  # lots — sized for $100K FTMO account
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
    # Execution resilience (Phase: Execution Gaps)
    retry_max_attempts: int = 3  # max retries for broker operations
    circuit_breaker_threshold: int = 5  # failures before circuit opens
    circuit_breaker_reset_seconds: float = 60.0  # seconds before half-open
    reconnect_max_attempts: int = 3  # auto-reconnect attempts on disconnect
    reconnect_backoff_base: float = 2.0  # exponential backoff base (seconds)
    order_verify_timeout: float = 5.0  # seconds to poll for order verification
    order_verify_interval: float = 0.5  # poll interval for verification

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
    # Dynamic spread filter: deny if current spread > multiplier × rolling avg.
    # Catches liquidity dips where spread is technically below the ceiling but
    # abnormally wide relative to recent session conditions.  Set 0.0 to disable.
    spread_vs_avg_multiplier: float = 2.0
    cooldown_seconds: int = 60
    max_trades_per_day: int = 10  # FTMO-safe: quality over quantity
    # Extended from 15 for Apr 6-11: PCE/CPI high-vol releases
    news_blackout_minutes: int = 60
    require_stop_loss: bool = True
    max_drawdown_equity_pct: float = _DEFAULT_MAX_DAILY_DRAWDOWN_PCT
    # Phase 6: IRB-specific risk hardening
    check_forex_session: bool = True  # P4: Enable enhanced session-aware filtering
    irb_max_open_positions: int = 0  # IRB exposure limit (0 = disabled)
    # P4: Enhanced session filtering options
    session_overlap_only: bool = False  # Restrict to London-NY overlap only
    session_allow_asian: bool = True  # Allow Asian session trading
    session_minimum_quality: str = "acceptable"  # minimum|acceptable|good|premium
    # FTMO daily reset timezone (IANA name). FTMO resets at midnight Prague time.
    daily_reset_tz: str = "Europe/Prague"
    # Slippage control (Phase: Execution Gaps)
    max_slippage_pips: float = 3.0  # max acceptable slippage in pips (0 = disabled)
    # Anti-EA-detection: rollover dead zone (spreads widen, fills degrade)
    rollover_dead_zone_enabled: bool = True
    rollover_start_hour_utc: int = 21  # 21:00 UTC = daily FX rollover start (widened per microstructure research)
    rollover_end_hour_utc: int = 0  # 00:00 UTC = rollover window end (midnight next day, covers full volatility window)
    # Anti-EA-detection: entry timing jitter (randomizes order timing)
    entry_jitter_min_seconds: float = 1.0
    entry_jitter_max_seconds: float = 5.0
    entry_jitter_enabled: bool = True
    # Anti-EA-detection: lot-size micro-variation (prevents exact repetition)
    lot_micro_variation_enabled: bool = True
    lot_micro_variation_step: float = 0.01  # ±0.01 lot random offset
    # Anti-EA-detection: London Fix avoidance (benchmark FX rate setting)
    london_fix_avoidance_enabled: bool = True
    london_fix_start_hour_utc: int = 15
    london_fix_start_minute_utc: int = 45
    london_fix_end_hour_utc: int = 16
    london_fix_end_minute_utc: int = 15
    # Minimum SL distance in pips — prevents micro-stops that get stopped out
    # by spread noise.  Lowered from 10.0→3.0→2.0: IRB strategy generates
    # stops from candle geometry; ATR-adaptive SL now handles volatility
    # gating, so this is a safety net only.
    min_sl_distance_pips: float = 2.0
    # Spread cushion baked into SL at signal time (pips). Must match
    # BacktestEnvironment.sl_spread_buffer_pips for backtest/live parity.
    sl_spread_buffer_pips: float = 1.0
    # Max extra SL widening from live spread adjustment (pips). Caps the
    # dynamic spread adjustment to prevent runaway widening during spikes.
    sl_spread_max_extra_pips: float = 3.0

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
class FtmoProfile:
    """FTMO-specific profile for demo/free-trial validation campaigns.

    Provider-neutral where possible — stores broker metadata and optional
    symbol mapping overrides for FTMO MT5 accounts.
    """

    enabled: bool = False
    broker_label: str = "FTMO"
    challenge_type: str = "free_trial"  # free_trial | challenge | verification
    campaign_label: str = ""  # operator-chosen label for evidence tagging
    symbol_suffix: str = ""  # e.g. ".ftmo" if broker appends suffix
    symbol_map: dict[str, str] = field(default_factory=dict)  # display→broker overrides
    account_size: int = 0  # nominal account size (e.g. 10000, 25000, 100000)

    @classmethod
    def from_env(cls) -> FtmoProfile:
        enabled = os.environ.get("FTMO_ENABLED", "").lower() in ("true", "1", "yes")
        if not enabled:
            return cls(enabled=False)

        symbol_map: dict[str, str] = {}
        raw_map = os.environ.get("FTMO_SYMBOL_MAP", "")
        for pair in raw_map.split(","):
            pair = pair.strip()
            if ":" in pair:
                display, _, broker = pair.partition(":")
                symbol_map[display.strip()] = broker.strip()

        return cls(
            enabled=True,
            broker_label=os.environ.get("FTMO_BROKER_LABEL", "FTMO"),
            challenge_type=os.environ.get("FTMO_CHALLENGE_TYPE", "free_trial"),
            campaign_label=os.environ.get("FTMO_CAMPAIGN_LABEL", ""),
            symbol_suffix=os.environ.get("FTMO_SYMBOL_SUFFIX", ""),
            symbol_map=symbol_map,
            account_size=int(os.environ.get("FTMO_ACCOUNT_SIZE", "0") or "0"),
        )

    def resolve_symbol(self, display_symbol: str) -> str:
        """Map a display symbol to the broker symbol name.

        Priority: explicit map → suffix append → passthrough.
        """
        if display_symbol in self.symbol_map:
            return self.symbol_map[display_symbol]
        if self.symbol_suffix:
            return display_symbol + self.symbol_suffix
        return display_symbol


@dataclass
class NovaTradeCfg:
    """Top-level NovaTrade configuration."""

    mode: AccountMode = AccountMode.DEMO
    provider: str = "metaapi"  # future: could be "direct_mt5"
    symbols: list[str] = field(default_factory=lambda: ["EURUSD"])
    timeframes: list[str] = field(default_factory=lambda: ["H1"])
    metaapi: MetaApiConfig = field(default_factory=MetaApiConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    ftmo: FtmoProfile = field(default_factory=FtmoProfile)
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
        # Local override file (in working directory) — values here OVERRIDE
        # the main env file. Useful when /etc is read-only.
        if _OVERRIDE_ENV_FILE.is_file():
            _load_env_override(_OVERRIDE_ENV_FILE)

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
            risk=RiskConfig(
                max_volume_per_trade=float(
                    os.environ.get("NOVATRADE_MAX_VOLUME_PER_TRADE", str(_DEFAULT_MAX_VOLUME_PER_TRADE))
                ),
            ),
            ftmo=FtmoProfile.from_env(),
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


def _load_env_override(path: Path) -> None:
    """Parse override env file — values here OVERRIDE existing env vars."""
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
            os.environ[key] = value
