---
name: pipeline-contract-validator
description: Validate file-contract alignment between NovaTrade state-file producers and autonomy collector consumers to prevent silent health-score degradation.
activation:
  keywords:
    - pipeline health
    - silent degradation
    - file contract
    - collector regression
    - missing state file
    - confidence drop
    - score regression
    - feed health fallback
    - pipeline contract
    - producer consumer alignment
tool_doctrine:
  primary:
    - Read: inspect collector source to extract expected file paths
    - Glob: locate actual state files written by NovaTrade runtime
    - Grep: trace file-write call sites in producers and file-read sites in consumers
    - Bash: run quick stat/mtime checks on STATE/ files and execute validation script
  secondary:
    - Agent(Explore): deep search when producer/consumer mapping is ambiguous
    - memory-recall: check prior pipeline regressions and known contract mismatches
output_contract:
  required:
    - contract_report: JSON object mapping each expected file to its producer status (written/missing/stale)
    - gap_list: list of files consumed but never produced, with collector line references
    - recommendation: actionable fix for each gap (add writer, add fallback, remove expectation)
    - confidence_impact: estimated confidence/score impact of each gap
  optional:
    - repair_diff: proposed code changes if auto-fix is requested
---

# Pipeline Contract Validator

Preventive validation skill that detects **silent degradation** in autonomy pipeline health checks caused by mismatches between state-file producers (NovaTrade runtime writers) and consumers (autonomy collector readers).

## Problem Statement

Autonomy collectors (e.g., `PipelineCollector`) expect specific state files to exist at known paths. When those files are never written by the runtime, collectors silently fall back to generic low-confidence scoring — making the system *appear* degraded when it is actually healthy. This is a **contract mismatch**, not a real health issue.

## When to Invoke

- After any change to `novatrade/autonomy/collectors/` or `novatrade/runtime/`
- When autonomy scores drop unexpectedly without a corresponding system change
- When confidence values fall to 0.1–0.3 despite healthy operational metrics
- As a periodic preventive check (e.g., after NovaTrade service changes)
- When a new collector or state-file writer is added

## Step-by-Step Procedure

### Step 1: Extract Consumer File Expectations

Read each collector in `novatrade/autonomy/collectors/` and extract every file path it attempts to open, along with the scoring weight and fallback behavior.

```
Target files:
  novatrade/autonomy/collectors/pipeline.py
  novatrade/autonomy/collectors/risk_engine.py
  novatrade/autonomy/collectors/*.py
```

For each file reference, record:
- **path**: the expected file path (e.g., `STATE/novatrade/connection_status.json`)
- **weight**: points assigned to that file's check
- **fallback**: what happens if the file is missing (generic mtime? zero score? skip?)
- **confidence_role**: whether the file affects confidence calculation

### Step 2: Extract Producer File Writes

Search the NovaTrade runtime for all state-file write operations:

```
Target directories:
  novatrade/runtime/
  novatrade/monitor/
  novatrade/execution/
  novatrade/data/
```

Search patterns:
- `write_text`, `json.dump`, `os.replace` into STATE/ paths
- `_persist_*` methods
- Any `Path(...)` construction targeting STATE/novatrade/

For each write site, record:
- **path**: the file written
- **frequency**: how often it is written (per-tick, periodic, on-event)
- **producer**: the module and function that writes it

### Step 3: Build the Contract Matrix

Cross-reference consumers and producers into a matrix:

| Expected File | Consumer | Weight | Producer | Status |
|---|---|---|---|---|
| connection_status.json | PipelineCollector | 40 pts | — | MISSING |
| live_metrics.json | PipelineCollector (fallback) | 50 pts | live_loop._persist_signal_metrics | OK |

Mark each entry as:
- **OK**: producer writes the file, consumer reads it
- **MISSING**: consumer expects it, no producer writes it
- **STALE**: producer writes it, but too infrequently for the consumer's freshness threshold
- **ORPHAN**: producer writes it, no consumer reads it (low priority, informational)

### Step 4: Assess Impact

For each MISSING or STALE entry, calculate:
- **Score impact**: how many points are lost when this file is absent
- **Confidence impact**: does the confidence calculation reference this file?
- **Fallback quality**: is there a meaningful fallback, or does it degrade to generic mtime?

Flag entries where:
- Confidence drops below 0.5 due to missing files
- Score drops more than 10 points due to missing files
- Fallback is generic mtime-based (silent degradation)

### Step 5: Generate Recommendations

For each gap, recommend one of:
1. **Add producer**: write the missing file from the appropriate runtime component
2. **Add explicit fallback**: make the collector use available operational files (live_metrics, signal_stats) with appropriate scoring
3. **Update confidence calculation**: ensure confidence reflects actual data availability, not just primary file presence
4. **Remove expectation**: if the file concept is obsolete, remove the check from the collector

### Step 6: Verify Current State (Live Check)

Run a quick live validation:

```bash
# Check which expected files actually exist
for f in connection_status.json feed_health.json last_order_attempt.json live_metrics.json signal_stats.json; do
  path="STATE/novatrade/$f"
  if [ -f "$path" ]; then
    age=$(($(date +%s) - $(stat -c %Y "$path")))
    echo "OK: $path (age: ${age}s)"
  else
    echo "MISSING: $path"
  fi
done
```

Compare live state against the contract matrix to confirm findings.

## Expected Inputs

- Access to `novatrade/autonomy/collectors/` source files
- Access to `novatrade/runtime/`, `novatrade/monitor/` source files
- Read access to `STATE/novatrade/` directory

## Expected Outputs

1. **Contract Report** (JSON): full producer-consumer mapping with status for each file
2. **Gap List**: files consumed but never produced, with source line references
3. **Recommendations**: specific fix for each gap, prioritized by score/confidence impact
4. **Confidence Impact Assessment**: estimated score and confidence values with vs. without each gap

## Error Handling

- If `STATE/novatrade/` does not exist: report as infrastructure issue, not contract gap
- If a collector file has changed since last validation: re-extract expectations (do not use cached results)
- If NovaTrade service is stopped: file staleness is expected — note this in the report but do not flag as a contract violation
- If multiple collectors read the same file: track all consumers in the matrix

## Known Contract Gaps (as of 2026-03-31)

These were identified and fixed in task 0659:
- `connection_status.json` — consumed by PipelineCollector, never produced → fixed with explicit live_metrics fallback
- `feed_health.json` — consumed by PipelineCollector, never produced → fixed with signal_stats fallback
- `last_order_attempt.json` — consumed by PipelineCollector, never produced → fixed with fallback chain

The fix pattern: add fallback scoring using files that ARE produced (live_metrics.json, signal_stats.json) rather than waiting for files that may never exist.
