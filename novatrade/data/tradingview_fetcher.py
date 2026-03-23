"""TradingView data fetcher via the tv-cli.js Node.js bridge.

Provides historical OHLCV data, server-side indicator values, and
programmatic strategy backtesting through TradingView's infrastructure.

Requires:
    - Node.js v18+ with @mathieuc/tradingview installed
    - TV_SESSION and TV_SIGNATURE env vars (TradingView cookies)

Usage:
    fetcher = TradingViewFetcher()

    # Fetch candles
    candles = fetcher.fetch_candles("OANDA:EURUSD", "60", bars=5000)

    # Run backtest on a saved strategy
    report = fetcher.run_backtest(
        symbol="OANDA:EURUSD",
        timeframe="60",
        strategy_id="PUB;abc123",
        overrides={"Length": 14, "ATR_Mult": 2.0},
    )

    # Get indicator values
    ema_values = fetcher.fetch_indicator(
        symbol="OANDA:EURUSD",
        timeframe="60",
        indicator="STD;EMA",
        options={"Length": 50},
    )
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from novatrade.models import Candle

log = logging.getLogger("novatrade.data.tradingview_fetcher")

# Path to the Node.js CLI tool
_CLI_PATH = Path(__file__).resolve().parents[2] / "tools" / "tradingview-cli" / "tv-cli.js"

# Node binary — prefer nvm-managed version
_NODE_BIN = os.environ.get("NODE_BIN", "node")


# ---------------------------------------------------------------------------
# Data classes for backtest results
# ---------------------------------------------------------------------------


@dataclass
class TVTrade:
    """Single trade from a TradingView strategy backtest."""

    entry_name: str
    entry_type: str  # "long" or "short"
    entry_price: float
    entry_time: float  # unix timestamp
    exit_name: str
    exit_price: float
    exit_time: float
    quantity: float
    profit: dict | None = None  # {"rel": float, "abs": float}
    cumulative: dict | None = None
    runup: dict | None = None
    drawdown: dict | None = None


@dataclass
class TVPerformance:
    """Performance summary from a TradingView strategy backtest."""

    net_profit: float = 0.0
    net_profit_pct: float = 0.0
    total_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    avg_trade: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    max_drawdown: float = 0.0
    commission_paid: float = 0.0
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict | None) -> TVPerformance:
        if not d:
            return cls()
        # TV returns nested structure — flatten the "all" sub-report
        perf = d.get("all", d)
        return cls(
            net_profit=perf.get("netProfit", 0),
            net_profit_pct=perf.get("netProfitPercent", 0),
            total_trades=perf.get("totalTrades", 0),
            win_rate=perf.get("percentProfitable", 0),
            profit_factor=perf.get("profitFactor", 0),
            sharpe_ratio=perf.get("sharpeRatio", 0),
            sortino_ratio=perf.get("sortinoRatio", 0),
            avg_trade=perf.get("avgTrade", 0),
            avg_win=perf.get("avgWinTrade", 0),
            avg_loss=perf.get("avgLosTrade", 0),
            largest_win=perf.get("largestWinTrade", 0),
            largest_loss=perf.get("largestLosTrade", 0),
            max_drawdown=perf.get("maxDrawDown", 0),
            commission_paid=perf.get("commissionPaid", 0),
            raw=d,
        )


@dataclass
class TVBacktestReport:
    """Complete backtest report from TradingView."""

    symbol: str
    timeframe: str
    strategy_id: str
    inputs: dict
    trade_count: int
    trades: list[TVTrade]
    performance: TVPerformance
    equity_curve: list[dict] | None = None
    settings: dict | None = None

    @property
    def candles_as_novatrade(self) -> list[Candle]:
        """Convert trades to NovaTrade Candle format (not applicable here)."""
        raise NotImplementedError("Use fetch_candles() for candle data")


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------


class TradingViewFetcher:
    """Python wrapper around the tv-cli.js Node.js bridge."""

    def __init__(
        self,
        session: str | None = None,
        signature: str | None = None,
        node_bin: str | None = None,
        timeout: int = 120,
    ):
        self._session = session or os.environ.get("TV_SESSION", "")
        self._signature = signature or os.environ.get("TV_SIGNATURE", "")
        self._node = node_bin or _NODE_BIN
        self._timeout = timeout

        if not self._session:
            log.warning("TV_SESSION not set — TradingView API calls will fail")

    def _run_cli(self, command: str, **kwargs: Any) -> dict:
        """Execute tv-cli.js and return parsed JSON output."""
        cmd = [self._node, str(_CLI_PATH), command]
        for key, val in kwargs.items():
            if val is None:
                continue
            flag = f"--{key.replace('_', '-')}"
            if isinstance(val, dict):
                # Convert dict to repeated --option Key=Value args
                for k, v in val.items():
                    cmd.extend([flag, f"{k}={v}"])
            elif isinstance(val, bool):
                if val:
                    cmd.append(flag)
            else:
                cmd.extend([flag, str(val)])

        env = os.environ.copy()
        env["TV_SESSION"] = self._session
        env["TV_SIGNATURE"] = self._signature

        log.debug("Running: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                env=env,
                cwd=str(_CLI_PATH.parent),
            )
        except subprocess.TimeoutExpired as err:
            raise TimeoutError(f"tv-cli {command} timed out after {self._timeout}s") from err

        # Parse stdout as JSON
        stdout = result.stdout.strip()
        if not stdout:
            stderr = result.stderr.strip()
            raise RuntimeError(f"tv-cli {command} returned no output. Exit code: {result.returncode}. Stderr: {stderr}")

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as err:
            raise RuntimeError(f"tv-cli {command} returned invalid JSON: {stdout[:500]}") from err

        if not data.get("ok"):
            raise RuntimeError(f"tv-cli {command} failed: {data.get('error', 'unknown error')}")

        return data

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_candles(
        self,
        symbol: str = "OANDA:EURUSD",
        timeframe: str = "60",
        bars: int = 500,
    ) -> list[Candle]:
        """Fetch historical OHLCV candles from TradingView.

        Args:
            symbol: TradingView symbol (e.g., "OANDA:EURUSD", "BINANCE:BTCUSDT")
            timeframe: Timeframe in minutes ("1", "5", "15", "60", "240", "D", "W")
            bars: Number of bars to fetch (max ~5000)

        Returns:
            List of NovaTrade Candle objects, sorted by timestamp ascending.
        """
        data = self._run_cli(
            "fetch-candles",
            symbol=symbol,
            timeframe=timeframe,
            bars=bars,
        )

        # Map TradingView timeframe to NovaTrade timeframe string
        tf_map = {"1": "M1", "5": "M5", "15": "M15", "60": "H1", "240": "H4", "D": "D1", "W": "W1"}
        nt_tf = tf_map.get(timeframe, timeframe)

        # Strip exchange prefix for NovaTrade symbol (OANDA:EURUSD -> EURUSD)
        nt_symbol = symbol.split(":")[-1] if ":" in symbol else symbol

        candles = [
            Candle(
                timestamp=c["timestamp"],
                open=c["open"],
                high=c["high"],
                low=c["low"],
                close=c["close"],
                volume=c.get("volume", 0),
                symbol=nt_symbol,
                timeframe=nt_tf,
            )
            for c in data.get("candles", [])
        ]

        # Sort ascending by timestamp
        candles.sort(key=lambda c: c.timestamp)
        log.info(
            "Fetched %d candles for %s %s from TradingView",
            len(candles),
            symbol,
            timeframe,
        )
        return candles

    def fetch_indicator(
        self,
        symbol: str = "OANDA:EURUSD",
        timeframe: str = "60",
        indicator: str = "STD;EMA",
        options: dict | None = None,
        bars: int = 500,
    ) -> list[dict]:
        """Fetch server-side indicator values from TradingView.

        Args:
            symbol: TradingView symbol
            timeframe: Timeframe in minutes
            indicator: Indicator ID (e.g., "STD;EMA", "STD;ATR", "STD;Supertrend")
            options: Input overrides (e.g., {"Length": 50})
            bars: Number of bars

        Returns:
            List of dicts with timestamp + indicator plot values.
        """
        data = self._run_cli(
            "fetch-indicator",
            symbol=symbol,
            timeframe=timeframe,
            indicator=indicator,
            option=options,
            bars=bars,
        )
        return data.get("values", [])

    def run_backtest(
        self,
        symbol: str = "OANDA:EURUSD",
        timeframe: str = "60",
        strategy_id: str = "",
        overrides: dict | None = None,
        bars: int = 5000,
    ) -> TVBacktestReport:
        """Run a strategy backtest on TradingView and return the full report.

        The strategy must already be saved/published on TradingView. Pass its
        script ID (e.g., "PUB;abc123" for published, or the private script ID
        from your account).

        Args:
            symbol: TradingView symbol
            timeframe: Timeframe in minutes
            strategy_id: TradingView script ID (required)
            overrides: Input parameter overrides (e.g., {"Length": 14, "ATR_Mult": 2.0})
            bars: Number of historical bars for backtest

        Returns:
            TVBacktestReport with trades, performance, and equity curve.
        """
        if not strategy_id:
            raise ValueError("strategy_id is required (e.g., 'PUB;abc123')")

        data = self._run_cli(
            "run-backtest",
            symbol=symbol,
            timeframe=timeframe,
            strategy=strategy_id,
            option=overrides,
            bars=bars,
        )

        trades = [
            TVTrade(
                entry_name=t.get("entry_name", ""),
                entry_type=t.get("entry_type", ""),
                entry_price=t.get("entry_price", 0),
                entry_time=t.get("entry_time", 0),
                exit_name=t.get("exit_name", ""),
                exit_price=t.get("exit_price", 0),
                exit_time=t.get("exit_time", 0),
                quantity=t.get("quantity", 0),
                profit=t.get("profit"),
                cumulative=t.get("cumulative"),
                runup=t.get("runup"),
                drawdown=t.get("drawdown"),
            )
            for t in data.get("trades", [])
        ]

        return TVBacktestReport(
            symbol=data.get("symbol", symbol),
            timeframe=data.get("timeframe", timeframe),
            strategy_id=strategy_id,
            inputs=data.get("inputs", {}),
            trade_count=data.get("trade_count", 0),
            trades=trades,
            performance=TVPerformance.from_dict(data.get("performance")),
            equity_curve=data.get("equity_curve"),
            settings=data.get("settings"),
        )

    def list_inputs(self, strategy_id: str) -> dict:
        """List all input parameters for a strategy/indicator.

        Returns dict with 'inputs' (parameter names + current values),
        'description', and 'script_type'.
        """
        return self._run_cli("list-inputs", strategy=strategy_id)

    def search_symbol(self, query: str, symbol_type: str = "") -> list[dict]:
        """Search TradingView symbols.

        Args:
            query: Search string (e.g., "EURUSD", "Bitcoin")
            symbol_type: Filter by type ("forex", "crypto", "stock", etc.)

        Returns:
            List of matching symbols with exchange, description, etc.
        """
        data = self._run_cli("search-symbol", query=query, type=symbol_type)
        return data.get("results", [])

    def sweep_parameters(
        self,
        symbol: str,
        timeframe: str,
        strategy_id: str,
        param_grid: dict[str, list],
        bars: int = 5000,
    ) -> list[TVBacktestReport]:
        """Run parameter sweep — backtest strategy with every combination.

        Args:
            symbol: TradingView symbol
            timeframe: Timeframe in minutes
            strategy_id: TradingView script ID
            param_grid: Dict of parameter name -> list of values to try.
                        e.g., {"Length": [10, 14, 20], "ATR_Mult": [1.5, 2.0, 2.5]}
            bars: Number of bars for each backtest

        Returns:
            List of TVBacktestReport, one per parameter combination.
        """
        import itertools

        keys = list(param_grid.keys())
        values = list(param_grid.values())
        combos = list(itertools.product(*values))

        log.info(
            "Running %d backtest combinations for %s on %s %s",
            len(combos),
            strategy_id,
            symbol,
            timeframe,
        )

        results = []
        for i, combo in enumerate(combos, 1):
            overrides = dict(zip(keys, combo, strict=True))
            log.info("  [%d/%d] %s", i, len(combos), overrides)
            try:
                report = self.run_backtest(
                    symbol=symbol,
                    timeframe=timeframe,
                    strategy_id=strategy_id,
                    overrides=overrides,
                    bars=bars,
                )
                results.append(report)
            except Exception as e:
                log.error("  [%d/%d] Failed: %s", i, len(combos), e)

        log.info("Sweep complete: %d/%d succeeded", len(results), len(combos))
        return results
