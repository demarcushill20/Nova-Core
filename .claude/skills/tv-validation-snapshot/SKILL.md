# TV Validation Snapshot

Operator-triggered TradingView backtest validation via browser automation.
Opens TradingView, extracts Strategy Tester metrics, and compares against
tv-cli or Python backtest results to detect environment drift.

**NOT autonomous** — requires operator to compile + apply Pine Script manually.

## When to use

- After modifying a Pine Script to verify it matches expected backtest results
- To validate tv-cli backtest results against the actual TradingView UI
- When trade count or metrics drift is suspected between environments
- When the user says "validate on TradingView", "check TV backtest", "compare backtests"

## Workflow

### Step 1: Get reference metrics

Run a tv-cli backtest to get reference metrics:

```python
from novatrade.data.tradingview_fetcher import TradingViewFetcher
from novatrade.data.tv_backtest_validator import metrics_from_tv_cli_report

fetcher = TradingViewFetcher()
report = fetcher.run_backtest(
    symbol="OANDA:EURUSD",
    timeframe="60",
    strategy_id="PUB;8545b63cbd4d4fd3b2102f367a0d0049",
)
reference = metrics_from_tv_cli_report(report)
```

Or construct reference metrics manually:

```python
from novatrade.data.tv_backtest_validator import TVSnapshotMetrics

reference = TVSnapshotMetrics(
    net_profit=1149.0,
    total_trades=148,
    win_rate=52.03,
    profit_factor=1.07,
    sharpe_ratio=0.28,
    max_drawdown=892.5,
)
```

### Step 2: Open TradingView in browser

Use Playwright MCP to navigate to the chart:

1. `mcp__playwright__browser_navigate` to `https://www.tradingview.com/chart/`
2. `mcp__playwright__browser_snapshot` to verify page loaded
3. Tell operator: "Please open the Pine Editor, paste your script, and compile. Let me know when Strategy Tester results are visible."

### Step 3: Extract metrics from Strategy Tester

Once operator confirms Strategy Tester is showing results:

1. `mcp__playwright__browser_snapshot` to capture the Strategy Tester tab
2. Parse metrics:

```python
from novatrade.data.tv_backtest_validator import extract_metrics_from_snapshot

snapshot_text = "<accessibility tree from browser_snapshot>"
source = extract_metrics_from_snapshot(snapshot_text)
```

Or use JavaScript extraction for more precise values:

```python
# Execute JS in the page to extract metrics
js_result = mcp__playwright__browser_evaluate(...)
source = extract_metrics_from_js_result(js_result)
```

### Step 4: Compare and report

```python
from novatrade.data.tv_backtest_validator import compare_backtests, DriftThresholds

report = compare_backtests(
    source=source,
    reference=reference,
    thresholds=DriftThresholds(
        trade_count_pct=5.0,
        net_profit_pct=10.0,
        win_rate_abs=2.0,
    ),
    source_label="tv_browser",
    reference_label="tv_cli",
)

# Save report
report.save("OUTPUT/tv_validation_report.json")

# Present results
if report.passed:
    print("PASS — all metrics within tolerance")
else:
    for d in report.failed_metrics:
        print(f"FAIL {d.metric}: {d.source_value} vs {d.reference_value} ({d.relative_diff_pct:.1f}% drift)")
```

## Allowed tools

- `mcp__playwright__browser_navigate`
- `mcp__playwright__browser_snapshot`
- `mcp__playwright__browser_evaluate`
- `mcp__playwright__browser_take_screenshot`
- `mcp__playwright__browser_wait_for`
- `mcp__playwright__browser_click`
- `mcp__playwright__browser_close`
- Python execution via Bash for validator code

## Safety rules

- Do NOT automate login or credential entry
- Do NOT automate Pine Script compilation (ToS risk)
- Do NOT run unattended — operator must be present
- Always close browser when done
- Save screenshots to OUTPUT/ for audit trail

## Output contract

```
| Metric         | TV Browser | Reference  | Drift   | Status |
|----------------|-----------|------------|---------|--------|
| total_trades   | 148       | 148        | 0.0%    | OK     |
| net_profit     | $1,149    | $1,100     | 4.5%    | WARN   |
| win_rate       | 52.0%     | 52.0%      | 0.0pp   | OK     |
| ...            | ...       | ...        | ...     | ...    |

Overall: PASS/WARN/FAIL
Report saved: OUTPUT/tv_validation_report.json
```
