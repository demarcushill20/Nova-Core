"""Report generation and TSV export for backtest results.

Provides two main functions:
  - format_run_report: Human-readable markdown report from a BacktestRunResult
  - export_campaign_tsv: Thin wrapper around ExperimentDB.export_tsv
"""

from __future__ import annotations

from pathlib import Path

from novatrade.backtest.metrics import format_metrics_report
from novatrade.cli.commands.run import BacktestRunResult
from novatrade.storage.experiment_db import ExperimentDB

# ---------------------------------------------------------------------------
# Human-readable report
# ---------------------------------------------------------------------------


def format_run_report(result: BacktestRunResult) -> str:
    """Render a BacktestRunResult as a human-readable markdown report.

    Sections:
      1. Header with experiment ID and status
      2. Gate results table (pass/fail per gate)
      3. Scout score (if computed)
      4. Key metrics summary (if available)

    Args:
        result: The BacktestRunResult from execute_backtest.

    Returns:
        Multi-line markdown string.
    """
    lines: list[str] = []

    # --- Header ---
    lines.append(f"# Backtest Report: `{result.experiment_id}`")
    lines.append("")
    lines.append(f"**Status:** {result.status}")
    if result.scout_score is not None:
        lines.append(f"**Scout Score:** {result.scout_score:.4f}")
    else:
        lines.append("**Scout Score:** N/A")
    lines.append("")

    # --- Notes ---
    if result.notes:
        lines.append(f"> {result.notes}")
        lines.append("")

    # --- Gate results ---
    lines.append("## Gate Results")
    lines.append("")
    if result.gate_results.results:
        lines.append("| Gate | Pass | Reason |")
        lines.append("|------|------|--------|")
        for r in result.gate_results.results:
            icon = "PASS" if r.passed else "FAIL"
            lines.append(f"| {r.gate} | {icon} | {r.reason} |")
    else:
        lines.append("_No gates evaluated._")
    lines.append("")

    # --- Metrics summary ---
    if result.metrics is not None:
        m = result.metrics
        lines.append("## Key Metrics")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append(f"| Completed trades | {m.total_completed_trades} |")
        lines.append(f"| Win rate | {m.win_rate:.1%} |")
        lines.append(f"| Profit factor | {m.profit_factor:.2f} |")
        lines.append(f"| Expectancy (R) | {m.expectancy_r:.2f}R |")
        lines.append(f"| Net result (pips) | {m.net_result_pips:.1f} |")
        lines.append(f"| Max drawdown (%) | {m.max_drawdown_pct:.2f}% |")
        lines.append(f"| Max consecutive losses | {m.max_consecutive_losses} |")
        lines.append(f"| Avg hold (bars) | {m.avg_hold_bars:.1f} |")
        lines.append(f"| Exposure (%) | {m.exposure_pct:.1f}% |")
        lines.append(
            f"| SL / Trail / Time exits | {m.stop_loss_exits} / {m.trailing_stop_exits} / {m.time_stop_exits} |"
        )
        lines.append("")

        # Full metrics report from metrics module
        lines.append("## Full Metrics")
        lines.append("")
        lines.append(format_metrics_report(m))
        lines.append("")
    else:
        lines.append("_No metrics available (backtest did not complete or produced no trades)._")
        lines.append("")

    # --- Trade count from backtest result ---
    if result.backtest_result is not None:
        bt = result.backtest_result
        lines.append("## Engine Summary")
        lines.append("")
        lines.append(f"- Total bars processed: {bt.total_bars:,}")
        lines.append(f"- Final equity: ${bt.final_equity:,.2f}")
        lines.append(f"- Trades completed: {len(bt.trades)}")
        lines.append(f"- Signals generated: {len(bt.signals)}")
        lines.append(f"- Pending orders placed: {len(bt.pending_orders)}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# TSV export
# ---------------------------------------------------------------------------


def export_campaign_tsv(
    db: ExperimentDB,
    campaign_id: str,
    output_path: Path,
) -> int:
    """Export all experiments in a campaign as a TSV file.

    Thin wrapper around ExperimentDB.export_tsv with path validation.

    Args:
        db: Open ExperimentDB connection.
        campaign_id: Campaign ID to export.
        output_path: Destination file path (.tsv).

    Returns:
        Number of rows exported.
    """
    output_path = Path(output_path)
    return db.export_tsv(campaign_id, output_path)
