"""CLI commands for strategy doctrine management.

Provides:
  - doctrine create: translate a concept into a structured StrategyDoctrine YAML
  - doctrine show: load and pretty-print a saved doctrine YAML
"""

from __future__ import annotations

from pathlib import Path

import typer

# ---------------------------------------------------------------------------
# create command
# ---------------------------------------------------------------------------


def doctrine_create(
    concept: str = typer.Option(
        ...,
        "--concept",
        "-C",
        help="Strategy description text (e.g. 'IRB reversal with EMA confirmation')",
    ),
    pair: str = typer.Option("EURUSD", "--pair", "-p", help="Currency pair"),
    timeframe: str = typer.Option("H1", "--timeframe", "-t", help="Primary timeframe"),
    output: Path = typer.Option(  # noqa: B008
        Path("doctrine.yaml"),
        "--output",
        "-o",
        help="Output YAML file path",
    ),
) -> None:
    """Translate a strategy concept into a StrategyDoctrine and save as YAML.

    Uses rule-based pattern matching for known families (IRB, breakout, mean
    reversion, trend following).  Unknown concepts produce a skeleton doctrine
    the operator can complete manually.
    """
    from novatrade.doctrine.bridge import validate_doctrine_config_compatibility
    from novatrade.doctrine.translator import concept_to_doctrine, detect_strategy_family

    # Detect family first for feedback
    family = detect_strategy_family(concept)
    typer.echo(f"Detected family: {family}")

    # Translate concept to doctrine
    typer.echo("Translating concept to doctrine...")
    doctrine = concept_to_doctrine(concept, pair=pair, timeframe=timeframe)

    # Validate config compatibility
    issues = validate_doctrine_config_compatibility(doctrine)
    if issues:
        typer.echo("")
        typer.echo("Compatibility warnings:", err=True)
        for issue in issues:
            typer.echo(f"  - {issue}", err=True)
        typer.echo("")

    # Save to YAML
    output_path = Path(output)
    doctrine.to_yaml(output_path)
    typer.echo(f"Saved: {output_path}")

    # Print summary
    _print_doctrine_summary(doctrine)


# ---------------------------------------------------------------------------
# show command
# ---------------------------------------------------------------------------


def doctrine_show(
    path: Path = typer.Argument(  # noqa: B008
        ...,
        help="Path to a doctrine YAML file",
    ),
) -> None:
    """Load and pretty-print a saved StrategyDoctrine YAML."""
    from novatrade.doctrine.schema import StrategyDoctrine

    yaml_path = Path(path)
    if not yaml_path.exists():
        typer.echo(f"Error: file not found: {yaml_path}", err=True)
        raise typer.Exit(code=1)

    try:
        doctrine = StrategyDoctrine.from_yaml(yaml_path)
    except Exception as exc:
        typer.echo(f"Error loading doctrine: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Doctrine: {yaml_path}")
    typer.echo(f"Hash:     {doctrine.content_hash()}")
    typer.echo("")
    _print_doctrine_summary(doctrine)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _print_doctrine_summary(doctrine) -> None:
    """Print a human-readable summary of a StrategyDoctrine."""
    typer.echo("")
    typer.echo("=" * 56)
    typer.echo("  Strategy Doctrine Summary")
    typer.echo("=" * 56)
    typer.echo(f"  Name:          {doctrine.name}")
    typer.echo(f"  Version:       {doctrine.version}")
    typer.echo(f"  Author:        {doctrine.author}")
    typer.echo(f"  Setup type:    {doctrine.setup_type}")
    typer.echo(f"  Entry signal:  {doctrine.entry_signal}")
    typer.echo(f"  Confirmations: {', '.join(doctrine.confirmations) or '(none)'}")
    typer.echo(f"  Exit method:   {doctrine.exit_method}")
    typer.echo(f"  Hash:          {doctrine.content_hash()}")
    typer.echo("")

    # Data
    typer.echo("  Data:")
    typer.echo(f"    Pair:       {doctrine.data.pair}")
    typer.echo(f"    Timeframes: {', '.join(doctrine.data.timeframes)}")
    typer.echo(f"    Min bars:   {doctrine.data.min_bars:,}")
    typer.echo("")

    # Mutable params
    if doctrine.mutable_params:
        typer.echo(f"  Mutable parameters ({len(doctrine.mutable_params)}):")
        for name, bound in sorted(doctrine.mutable_params.items()):
            typer.echo(
                f"    {name:<28s} default={bound.default:<8g} "
                f"[{bound.min}, {bound.max}]" + (f" step={bound.step}" if bound.step else "")
            )
        typer.echo("")

    # Immutable params
    if doctrine.immutable_params:
        typer.echo(f"  Immutable parameters ({len(doctrine.immutable_params)}):")
        for name, value in sorted(doctrine.immutable_params.items()):
            typer.echo(f"    {name:<28s} {value}")
        typer.echo("")

    # Filters
    if doctrine.filters_mandatory:
        typer.echo(f"  Mandatory filters: {', '.join(doctrine.filters_mandatory)}")
    if doctrine.filters_optional:
        typer.echo(f"  Optional filters:  {', '.join(doctrine.filters_optional)}")
    if doctrine.filters_forbidden:
        typer.echo(f"  Forbidden filters: {', '.join(doctrine.filters_forbidden)}")
    if doctrine.filters_mandatory or doctrine.filters_optional or doctrine.filters_forbidden:
        typer.echo("")

    # Execution
    typer.echo("  Execution:")
    typer.echo(f"    Spread:     {doctrine.execution.spread_pips} pips")
    typer.echo(f"    Slippage:   {doctrine.execution.slippage_pips} pips")
    typer.echo(f"    Commission: ${doctrine.execution.commission_per_lot}/lot")
    typer.echo(f"    Leverage:   {doctrine.execution.leverage}x")
    typer.echo(f"    Fill:       {doctrine.execution.fill_policy}")
    typer.echo("")

    # Validation criteria
    typer.echo("  Validation criteria:")
    typer.echo(f"    Min trades:      {doctrine.validation.min_trades}")
    typer.echo(f"    Min months:      {doctrine.validation.min_months}")
    typer.echo(f"    Max drawdown:    {doctrine.validation.max_drawdown_pct}%")
    typer.echo(f"    Min PF:          {doctrine.validation.min_profit_factor}")
    typer.echo(f"    Min Sharpe:      {doctrine.validation.min_sharpe}")
    typer.echo(f"    WF windows:      {doctrine.validation.walk_forward_windows}")
    typer.echo(f"    WFE min:         {doctrine.validation.walk_forward_efficiency_min}")
    typer.echo(f"    Max PBO:         {doctrine.validation.max_pbo}")
    typer.echo("")

    # Campaign
    typer.echo("  Campaign:")
    typer.echo(f"    Max experiments: {doctrine.campaign.max_experiments}")
    typer.echo(f"    Search:          {doctrine.campaign.search_strategy}")
    typer.echo(f"    Wall clock:      {doctrine.campaign.wall_clock_limit_hours}h")
    typer.echo(f"    Stagnation:      {doctrine.campaign.stagnation_limit}")

    # Invalidation
    if doctrine.invalidation_rules:
        typer.echo("")
        typer.echo("  Invalidation rules:")
        for rule in doctrine.invalidation_rules:
            typer.echo(f"    - {rule}")

    typer.echo("=" * 56)
