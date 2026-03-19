"""CLI commands for AutoResearch campaign management.

Provides the glue between the CLI layer and the campaign engine:
  - start_campaign: load config/data, run campaign, print report
  - format_campaign_report: human-readable summary of CampaignResult
"""

from __future__ import annotations

import threading
from pathlib import Path

import typer

from novatrade.cli.config_schema import StrategyConfig
from novatrade.optimization.campaign_engine import (
    CampaignConfig,
    CampaignResult,
    run_campaign,
)
from novatrade.storage.experiment_db import ExperimentDB

# ---------------------------------------------------------------------------
# Campaign execution
# ---------------------------------------------------------------------------


def start_campaign(
    config_path: Path,
    campaign_config: CampaignConfig,
    h1_candles: list,
    h4_candles: list,
    dataset_hash: str = "",
    doctrine_hash: str = "",
    campaign_dir: Path | None = None,
    db: ExperimentDB | None = None,
    stop_event: threading.Event | None = None,
) -> CampaignResult:
    """Load config, set up artifacts, and run a campaign.

    Args:
        config_path: Path to the base strategy YAML config.
        campaign_config: Campaign parameters.
        h1_candles: H1 candle data.
        h4_candles: H4 candle data.
        dataset_hash: Data fingerprint.
        doctrine_hash: Doctrine file hash.
        campaign_dir: Optional directory for artifacts/checkpoints.
        db: Optional experiment database.
        stop_event: Optional stop signal.

    Returns:
        CampaignResult from the campaign loop.
    """
    # Load base config
    if not config_path.exists():
        typer.echo(f"Error: config not found: {config_path}", err=True)
        raise typer.Exit(code=1)

    try:
        base_config = StrategyConfig.from_yaml(config_path)
    except Exception as exc:
        typer.echo(f"Error loading config: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Config: {base_config.name} v{base_config.version} [{base_config.content_hash()[:8]}]")
    typer.echo(f"Campaign: {campaign_config.campaign_id}")
    typer.echo(f"Data: {len(h1_candles):,} H1 + {len(h4_candles):,} H4 bars")

    # Set up campaign directory
    if campaign_dir is None:
        campaign_dir = Path("/home/nova/nova-core/data/campaigns") / campaign_config.campaign_id
    campaign_dir.mkdir(parents=True, exist_ok=True)

    # Run campaign
    result = run_campaign(
        base_config=base_config,
        h1_candles=h1_candles,
        h4_candles=h4_candles,
        campaign_config=campaign_config,
        dataset_hash=dataset_hash,
        doctrine_hash=doctrine_hash,
        campaign_dir=campaign_dir,
        db=db,
        stop_event=stop_event,
    )

    # Print report
    typer.echo(format_campaign_report(result))

    return result


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def format_campaign_report(result: CampaignResult) -> str:
    """Render a CampaignResult as a human-readable summary."""
    lines: list[str] = [
        "",
        "=" * 64,
        "  AutoResearch Campaign Report",
        "=" * 64,
        "",
        f"  Campaign ID:       {result.campaign_id}",
        f"  Total experiments: {result.total_experiments}",
        f"  Elapsed:           {result.elapsed_seconds:.1f}s ({result.elapsed_seconds / 60:.1f} min)",
        f"  End reason:        {result.end_reason}",
        "",
    ]

    # Status breakdown
    lines.append("  Status breakdown:")
    for status, count in sorted(result.status_counts.items()):
        lines.append(f"    {status:<12s} {count:>5d}")
    lines.append("")

    # Best result
    if result.best_experiment_id:
        lines.append(f"  Best experiment:   {result.best_experiment_id}")
        lines.append(f"  Best scout score:  {result.best_scout_score:.4f}")
        lines.append("")

        if result.best_config:
            lines.append("  Best config parameters:")
            for k, v in sorted(result.best_config.items()):
                if isinstance(v, float):
                    lines.append(f"    {k:<28s} {v:.4f}")
                else:
                    lines.append(f"    {k:<28s} {v}")
            lines.append("")
    else:
        lines.append("  No ranked experiments found.")
        lines.append("")

    # Improvement timeline
    if result.improvements:
        lines.append(f"  Improvements ({len(result.improvements)}):")
        for imp in result.improvements:
            lines.append(
                f"    #{imp['experiment_index']:<5d} score={imp['scout_score']:.4f}  id={imp['experiment_id']}"
            )
        lines.append("")

    # Checkpoints
    if result.checkpoints:
        lines.append(f"  Checkpoints: {len(result.checkpoints)} saved")
        lines.append("")

    lines.append("=" * 64)
    return "\n".join(lines)
