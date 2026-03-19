# NovaTrade AutoResearch Doctrine v2.0

## Changelog
- v2.0: Applied operator redlines — risk_fraction exclusion, gated evaluation
  ladder, bounded campaigns, structural templates, MCP thin wrapper, TSV-as-export.
- v1.0: Initial doctrine.

## Search Levels

### Level 1: Signal Parameters Only
- Any parameter within PARAMETER_BOUNDS (see config_schema.py)
- **EXCLUDED** from Level 1:
  - `risk_fraction` — separate sizing pass, not signal optimization
  - Anything that changes accounting/execution (commission, slippage, spread, fill model)
  - Anything that changes data alignment (warmup offset, start index, alignment mode)

### Level 2: Rule Toggles (Constrained DSL)
- Enable/disable **registered** filters only (see search_levels.REGISTERED_FILTERS)
- Choose from **named threshold presets** (conservative, moderate, aggressive)
- Select **registered exit-rule variants** (trailing_tight, trailing_standard, trailing_wide, time_only)
- Toggle **registered session filters** (london, newyork, overlap, asian)
- Toggle **registered regime switches** (trending_only, low_volatility_skip)
- **No freeform parameter changes** at Level 2 — only registered toggles

### Level 3: Structural Changes via Templates
- Agent chooses from **registered indicator modules** and **registered composition patterns**
- NOT "agent can add new indicators freely"
- Instead: "agent chooses from registered indicator modules and registered composition patterns"
- Reduces search entropy, keeps auditability high
- Available compositions: irb_trend_adx, irb_trend_mtf, irb_minimal, irb_full

## What CANNOT Change (Doctrine-Locked)
- Evaluation layer (metrics.py, fill model, spread, slippage, commission, session calendar)
- risk_fraction (fixed at 0.01 — separate sizing concern)
- Data alignment or preprocessing
- initial_equity
- Sacred model hashes (tracked by SacredBundle)
- Intrabar policy

## Evaluation: Gated Ladder (NOT Single Scalar)

### Stage A — Validity Gates (Binary Pass/Fail)
- Minimum trade count: >= 50 trades
- Minimum active months: >= 6
- No catastrophic drawdown: < 50%
- No P&L concentration: top-3 trades <= 40% of profit
- No lookahead: enforced by engine construction

### Stage B — Multi-Metric Ranking Dashboard
Survivors get **ranked on a dashboard**, NOT optimised against a single number.
Metrics displayed side-by-side:
- Sharpe ratio, Sortino ratio
- Profit factor, Calmar ratio
- Max drawdown %, Recovery factor
- Expectancy R, Win rate, Win/loss ratio
- Trade frequency (trades/month)
- Regime performance breakdown (when available)

A ranking_score exists for SORTING convenience only.

### Stage C — Promotion Ladder
Champions pass the full validation gauntlet:
1. Walk-forward OOS validation (multi-window)
2. Holdout evaluation
3. Parameter perturbation stability (±5%, ±10%)
4. Cost stress test (2x spread + slippage)
5. Promotion score computation

## Campaign Doctrine

### Bounded Campaigns (NOT "Never Stop")
- Run bounded campaigns until budget exhausted or manually stopped
- Fixed budget before research: max 200 experiments, 6 hours wall clock
- Stagnation limit: 10 experiments after last improvement
- Max consecutive crashes: 5
- Checkpoint every 25 experiments

### Campaign Loop (P5.2 Enhancements)
On every new best candidate:
1. Realism validation (Stage B gates)
2. Robustness validation (walk-forward if data permits)
3. Automatic full recheck (perturbation + stress)
4. Parameter perturbation stability
5. Cost-stress testing
6. Pass-rate threshold across walk-forward windows
7. Embargo/purge around IS/OOS splits (5-bar default)

## MCP Architecture: CLI-First, MCP-Second

MCP is a thin wrapper over stable CLI commands ONLY:
- `run` — single backtest
- `campaign_start` — launch bounded campaign
- `campaign_status` — current campaign state
- `campaign_stop` — graceful stop
- `walkforward` — OOS validation
- `compare` — side-by-side experiment comparison
- `leaderboard` — ranked dashboard export
- `fetch_data` — data retrieval

Agents may NOT directly mutate:
- Storage internals (SQLite, artifact dirs)
- Raw artifacts (trade logs, equity curves)
- Results registries (experiment DB records)

## Results Storage

- **Canonical store**: ExperimentDB (SQLite) + JSON artifacts
- **TSV is an EXPORT**, not the canonical store
- Artifact-rich storage includes: equity curve, drawdown curve, trade-by-trade
  log, regime labels (when available), parameter set, data version hash,
  fill-model version, strategy code hash

## Success Criteria

1. A single run is **reproducible from frozen artifacts** (ReproducibilityManifest)
2. Backtest engine completes **under target runtime** on cached data
3. Strategy passes **all three gating stages** (A → B → C)
4. Promotion pipeline confirms **OOS robustness**

## Simplicity Criterion
- At equal fitness: fewer parameters wins
- Marginal improvement + added complexity = reject
