---
name: autonomy-collector-diagnosis
description: Diagnose and fix autonomy collectors scoring below threshold by identifying config-vs-actual data confusion, missing-file-as-failure, and idle-market misscoring
activation:
  keywords:
    - collector scoring low
    - autonomy score below threshold
    - false breach
    - risk engine score
    - collector diagnosis
    - dimension score red
    - sub-metric broken
    - config misread
    - idle market penalty
    - collector fix
tool_doctrine:
  read_first: Always read the collector source AND its state files before proposing changes
  diff_small: Prefer targeted fixes over collector rewrites
  verify_with_test: Run existing collector tests after every fix to confirm no regression
output_contract:
  required:
    - root_cause_list: List of identified bugs with category (config-misread | missing-file-as-failure | idle-market-penalty | stale-data | other)
    - before_score: Score before fix (numeric 0-100)
    - after_score: Score after fix (numeric 0-100) or projected score if not yet runnable
    - diff_files: List of files modified
    - verification: Test results or manual verification output
---

# Autonomy Collector Diagnosis

Systematic diagnosis and repair of autonomy collectors that score below their expected threshold. Uses a proven 3-bug-class taxonomy to rapidly identify root causes.

## When to Invoke

- A dimension score drops below GREEN (< 70) without a real underlying issue
- A sub-metric returns 0 or a low score that contradicts actual system state
- A collector's raw_value doesn't match what the state files actually contain
- After deploying new state file formats that existing collectors haven't adapted to

## Step 1: Read the Collector and Its Data Sources

1. Read the collector source file under `novatrade/autonomy/collectors/`
2. Identify every state file, log file, or config file the collector reads
3. Read each data source to understand the actual on-disk content
4. Compare what the collector *thinks* a field means vs what it *actually* contains

## Step 2: Classify Bugs Using the 3-Bug Taxonomy

Check for each of these failure modes in order:

### Bug Class A: Config-vs-Actual Data Confusion
The collector reads a config/limit field (e.g., `daily_loss_pct = 5.0`) and treats it as an actual measured value (e.g., "5% daily loss — near breach!"). This is the most common and most damaging bug class.

**Diagnosis:** Find every field the collector reads from state/config files. For each field, verify: is this a *limit/threshold/config* value or an *actual/measured/current* value? If the collector treats a config value as a measurement, it will produce false alarms.

**Fix pattern:** Derive actual values from operational fields (e.g., `day_reference - current_equity`), not from config fields. Use config fields only as denominators in ratio calculations.

### Bug Class B: Missing-File-as-Failure
The collector scores 0 or very low when a state file doesn't exist, but file absence is actually a normal/healthy state (e.g., `halt_state.json` doesn't exist because the system has never halted).

**Diagnosis:** For every `if not path.exists(): return low_score` branch, ask: is this file's absence genuinely a problem, or is it the expected state? Check if there's a fallback source that can provide the same information.

**Fix pattern:** Implement a fallback chain:
1. Primary source (most authoritative file)
2. Fallback source (alternative state file with overlapping data)
3. Infrastructure check (does the relevant code/config exist?)
4. Safe default (score reflecting "unknown but not broken")

### Bug Class C: Idle-Market Penalty
The collector penalizes the absence of activity (no trades, no gate events, no signals) when the system is correctly idle. An idle-but-functional system should score 65-80, not 0-50.

**Diagnosis:** Find every "no data found" or "total == 0" branch. Ask: does zero activity mean the system is broken, or that the market is closed / no signals triggered?

**Fix pattern:** When no activity data exists, check whether the *infrastructure* is present (code files, config files, service running). Score tiers:
- Infrastructure present + recent activity → 80-100
- Infrastructure present + no recent activity (healthy idle) → 65-80
- Infrastructure partially present → 50-65
- No infrastructure at all → 0-50

## Step 3: Implement Fixes

For each identified bug:

1. Write the minimal fix (prefer adding fallback logic over restructuring)
2. Add a docstring update explaining the data semantics (what each field actually means)
3. Extract shared scoring logic into helper methods (e.g., `_score_from_usage()`) if the same pattern repeats across sub-metrics

## Step 4: Verify

1. Run the collector's test suite: `python -m pytest tests/test_<collector_name>.py -v`
2. If possible, run the collector against live state files and compare before/after scores
3. Confirm the score crossed the GREEN threshold (≥ 70)

## Step 5: Produce Output

Write a summary containing:
- Each bug found, classified by taxonomy (A/B/C)
- Before and after scores
- Files changed
- Any remaining risks or edge cases

## Error Handling

- If the collector has no tests, create minimal test cases covering each bug class before fixing
- If state files are missing entirely (no STATE/ dir), note this as a deployment issue, not a collector bug
- If the collector's BaseCollector interface has changed, check `base.py` and `schemas.py` first
- If the score improves but doesn't reach GREEN, document what additional state/data would be needed

## Example: RiskCollector Fix (Task 0663)

Three bugs identified and fixed, raising score from 58 → 76.2:

| Bug | Class | Field | Was | Fix |
|-----|-------|-------|-----|-----|
| `daily_loss_pct` treated as actual loss | A: Config Misread | `daily_loss_pct` | Config limit (5.0) read as 5% loss | Derive actual loss from `day_reference - peak_equity_today` |
| `halt_state.json` missing → score 50 | B: Missing-File | `halt_state.json` | Absence = uncertain | Fallback to `novatrade_risk_state.json`, then infrastructure check |
| No gate events → score 50 | C: Idle-Market | gate pass rate | Zero events = unknown | Check `pre_trade_gate.py` + `risk_policy.yaml` exist → score 75 |
