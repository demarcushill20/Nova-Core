"""NovaTrade MCP server — thin wrapper over CLI backtesting tools.

Uses FastMCP (stdio transport). Importable without mcp installed.
Run: python -m novatrade.mcp.server
"""

from __future__ import annotations

import sys

_FASTMCP_AVAILABLE = False
try:
    from mcp.server.fastmcp import FastMCP

    _FASTMCP_AVAILABLE = True
except ImportError:
    print("Warning: mcp package not installed. Install with: pip install mcp", file=sys.stderr)

from novatrade.mcp import resources, tools  # noqa: E402

# --- Build server (only if FastMCP is available) ---

server = None

if _FASTMCP_AVAILABLE:
    server = FastMCP("novatrade-bt", instructions="NovaTrade Backtesting & AutoResearch MCP Server")

    # -- Tools --

    @server.tool()
    def bt_run(config_yaml: str, snapshot_id: str = "", symbol: str = "EURUSD") -> str:
        """Run a single backtest with gated evaluation and return a formatted report."""
        return tools.bt_run(config_yaml, snapshot_id=snapshot_id, symbol=symbol)

    @server.tool()
    def bt_campaign_start(config_yaml: str, max_experiments: int = 200) -> str:
        """Start an AutoResearch campaign (Phase 5)."""
        return tools.bt_campaign_start(config_yaml, max_experiments)

    @server.tool()
    def bt_campaign_status() -> str:
        """Check status of the active AutoResearch campaign."""
        return tools.bt_campaign_status()

    @server.tool()
    def bt_campaign_stop() -> str:
        """Stop the active AutoResearch campaign."""
        return tools.bt_campaign_stop()

    @server.tool()
    def bt_walkforward(config_yaml: str, snapshot_id: str = "") -> str:
        """Run walk-forward analysis on a strategy config (Phase 3)."""
        return tools.bt_walkforward(config_yaml, snapshot_id=snapshot_id)

    @server.tool()
    def bt_compare(experiment_id_a: str, experiment_id_b: str, db_path: str = "") -> str:
        """Compare two experiments side-by-side on key metrics."""
        return tools.bt_compare(experiment_id_a, experiment_id_b, db_path=db_path)

    @server.tool()
    def bt_leaderboard(campaign_id: str = "", limit: int = 20, db_path: str = "") -> str:
        """Show the experiment leaderboard ranked by scout score."""
        return tools.bt_leaderboard(campaign_id=campaign_id, limit=limit, db_path=db_path)

    @server.tool()
    def bt_fetch_data(symbol: str = "EURUSD", days: int = 730) -> str:
        """Generate synthetic candle data and save to CSV."""
        return tools.bt_fetch_data(symbol=symbol, days=days)

    @server.tool()
    def bt_promote(experiment_id: str, db_path: str = "") -> str:
        """Promote an experiment to the active strategy config (Phase 5)."""
        return tools.bt_promote(experiment_id, db_path=db_path)

    # -- Resources --

    @server.resource("leaderboard://current")
    def resource_leaderboard() -> str:
        """Current experiment leaderboard."""
        return resources.leaderboard_current()

    @server.resource("campaign://status")
    def resource_campaign_status() -> str:
        """Active campaign status."""
        return resources.campaign_status()

    @server.resource("config://baseline")
    def resource_config_baseline() -> str:
        """Baseline strategy config YAML."""
        return resources.config_baseline()


def main() -> None:
    """Entry point for stdio transport."""
    if server is None:
        print("Error: mcp package not installed. Cannot start server.", file=sys.stderr)
        sys.exit(1)
    server.run()


if __name__ == "__main__":
    main()
