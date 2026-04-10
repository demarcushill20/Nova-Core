---
name: autonomy-preflight-validation
description: Cross-check autonomy metrics against raw data sources before generating repair tasks to prevent false-alarm task creation.
activation:
  keywords:
    - preflight
    - false alarm
    - metric validation
    - strategy validity
    - trade score
    - autonomy scoring
    - repair task validation
    - pre-flight check
tool_doctrine:
  primary:
    - Read (trade_log.json, MetaApi position data, scoring outputs)
    - Bash (query MetaApi API, inspect raw trade data)
    - Grep (search for metric computation logic)
  secondary:
    - memory-recall (check if this metric has triggered false alarms before)
    - memory-store (record confirmed false alarms for future pattern detection)
output_contract:
  required:
    - validation_verdict (CONFIRMED | FALSE_ALARM | INCONCLUSIVE)
    - raw_data_summary (what the raw source actually shows)
    - computed_metric_summary (what the scoring system computed)
    - discrepancy_details (if any mismatch found)
    - recommended_action (proceed with task | suppress task | escalate)
---

# Autonomy Pre-Flight Validation

Validates autonomy-generated repair tasks against raw data sources before allowing task creation or worker spin-up. Prevents false-alarm CRITICAL tasks from wasting cycles.

## Problem This Solves

The autonomy decision engine computes composite scores (e.g. `trade_score`, `strategy_validity`) from derived metrics. When a score drops below threshold, it generates CRITICAL priority repair tasks. However, the computed metric can diverge from ground truth due to stale caches, missing data windows, or scoring formula edge cases. This skill acts as a circuit breaker between "metric says bad" and "spin up a repair task."

## When To Invoke

- Before any autonomy-generated repair task with priority >= HIGH is created or executed
- When a scoring dimension drops sharply (>30 points) in a single cycle
- When the decision engine flags CRITICAL on a dimension that was GREEN in the previous cycle
- Manually, when a repair task seems suspicious or unnecessary

## Step-by-Step Procedure

### Step 1: Identify the triggering metric

Extract from the repair task or decision engine output:
- Which scoring dimension triggered (e.g. `strategy_validity`, `trade_score`)
- The computed value and the threshold it violated
- The timestamp of the computation

### Step 2: Locate the raw data source

Map the metric to its ground-truth source:

| Metric | Raw Source |
|---|---|
| `trade_score` | `novatrade/data/trade_log.json`, MetaApi position history |
| `strategy_validity` | Live strategy config, backtest results, recent trade outcomes |
| `system_health` | systemd service status, process list, port checks |
| `performance_stability` | Equity curve data, drawdown calculations from actual P&L |
| `learning_growth` | Memory store counts, pattern library entries |

### Step 3: Query the raw source directly

- Read the raw data file or query the live API
- Compute the metric independently from the raw data
- Compare against what the scoring system reported
- Check for common divergence causes:
  - **Stale data**: scoring read a cached value, raw source has newer data
  - **Empty window**: no trades in the scoring window ≠ bad trades
  - **Initialization artifact**: score defaults to 0 or low value before first data point
  - **Time zone mismatch**: CEST/UTC offset causing wrong trade window

### Step 4: Render verdict

- **CONFIRMED**: Raw data agrees the metric is genuinely degraded → allow the repair task
- **FALSE_ALARM**: Raw data contradicts the computed metric → suppress the task, log the discrepancy
- **INCONCLUSIVE**: Cannot definitively confirm or deny → escalate to operator via Telegram

### Step 5: Record outcome

- If FALSE_ALARM: store in memory with the specific divergence cause so the scoring formula can be hardened later
- If CONFIRMED: proceed normally, no memory entry needed
- If INCONCLUSIVE: flag for operator review, do not auto-generate repair task

## Expected Inputs

- Repair task specification (task ID, triggering metric, computed score, threshold)
- Access to raw data sources (trade_log.json, MetaApi API, service status)

## Expected Outputs

```
validation_verdict: FALSE_ALARM
raw_data_summary: "trade_log.json shows 3 profitable trades in last 24h, equity +1.2%"
computed_metric_summary: "trade_score=20/100 triggered by strategy_validity scorer"
discrepancy_details: "Scorer used empty 4h window (no trades between 02:00-06:00 UTC) as signal of failure. Actual trading window is 08:00-16:00 CEST."
recommended_action: "Suppress repair task 0644. Update scorer to respect trading hours."
```

## Error Handling

- If raw data source is unreachable (MetaApi down, file missing): verdict = INCONCLUSIVE, escalate
- If scoring code cannot be located for inspection: note in output, rely on raw data comparison only
- If multiple metrics triggered simultaneously: validate each independently — one false alarm doesn't mean all are false
- Never suppress a task without documenting the specific evidence that contradicts the metric
