#!/usr/bin/env bash
# NovaTrade parity check — chained runner.
# Pulls live FTMO trades, runs the IRB v5 backtest for the same window,
# and emits a parity report. Prints the three output paths so the caller
# (or the calling skill) can pick them up for interpretation.
#
# Usage:
#   run_parity_week.sh --start 2026-04-20 --end 2026-04-27 --days 7
#
# Defaults:
#   --tolerance-min 15
#   --warmup-days 2
#   --env-file OUTPUT/novatrade.env.updated
#
# Exit codes:
#   0  all three steps succeeded
#   1  bad args
#   2  fetch step failed
#   3  backtest step failed
#   4  parity step failed

set -euo pipefail

START=""
END=""
DAYS=""
TOLERANCE=15
WARMUP=2
ENVFILE="OUTPUT/novatrade.env.updated"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --start) START="$2"; shift 2 ;;
        --end) END="$2"; shift 2 ;;
        --days) DAYS="$2"; shift 2 ;;
        --tolerance-min) TOLERANCE="$2"; shift 2 ;;
        --warmup-days) WARMUP="$2"; shift 2 ;;
        --env-file) ENVFILE="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "$START" || -z "$END" || -z "$DAYS" ]]; then
    echo "required: --start YYYY-MM-DD --end YYYY-MM-DD --days N" >&2
    exit 1
fi

# Run from repo root so relative script paths and env file resolve.
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)"
cd "$REPO_ROOT"

echo "[parity] window: $START → $END  (live --days $DAYS, tolerance ±${TOLERANCE}min)"

echo "[parity] step 1/3: fetching live FTMO trades…"
if ! python3 scripts/fetch_ftmo_deal_history.py --days "$DAYS" --env-file "$ENVFILE" > /tmp/parity_fetch.log 2>&1; then
    echo "[parity] FETCH FAILED — see /tmp/parity_fetch.log" >&2
    tail -20 /tmp/parity_fetch.log >&2
    exit 2
fi
LIVE_CSV="$(ls -t OUTPUT/parity/live_trades_*.csv | head -1)"
echo "[parity] live trades: $LIVE_CSV"

# Select the backtest engine to match what the live runtime is actually
# running. NOVATRADE_STRATEGY_LOGIC=vault_native => VaultLiveEngine (vault core);
# diffing that against the nova IRBBacktester (which carries the documented
# sign-flipping bug) would flag false "strategy drift" on every trade.
# Resolution order: explicit env var > $ENVFILE > canonical live unit env >
# default vault_native (the current live runtime, 2026-05). Override by exporting
# NOVATRADE_STRATEGY_LOGIC=nova if comparing against the legacy engine.
STRATEGY_LOGIC="${NOVATRADE_STRATEGY_LOGIC:-}"
if [[ -z "$STRATEGY_LOGIC" ]]; then
    STRATEGY_LOGIC="$(grep -hE '^NOVATRADE_STRATEGY_LOGIC=' "$ENVFILE" /etc/novacore/novatrade.env 2>/dev/null | tail -1 | cut -d= -f2 | tr -d ' ')"
fi
STRATEGY_LOGIC="${STRATEGY_LOGIC:-vault_native}"
if [[ "$STRATEGY_LOGIC" == "vault_native" ]]; then
    BT_SCRIPT="scripts/backtest_window_vault_parity.py"
    echo "[parity] strategy logic = vault_native → using vault-native backtest ($BT_SCRIPT)"
else
    BT_SCRIPT="scripts/backtest_window_for_parity.py"
    echo "[parity] strategy logic = ${STRATEGY_LOGIC:-nova} → using nova backtest ($BT_SCRIPT)"
fi

echo "[parity] step 2/3: running backtest for the same window…"
if ! python3 "$BT_SCRIPT" \
        --start "$START" --end "$END" \
        --warmup-days "$WARMUP" \
        --env-file "$ENVFILE" > /tmp/parity_bt.log 2>&1; then
    echo "[parity] BACKTEST FAILED — see /tmp/parity_bt.log" >&2
    tail -20 /tmp/parity_bt.log >&2
    exit 3
fi
BT_CSV="$(ls -t OUTPUT/parity/backtest_trades_*.csv | head -1)"
echo "[parity] backtest trades: $BT_CSV"

echo "[parity] step 3/3: generating parity report…"
if ! python3 scripts/parity_report.py \
        --live "$LIVE_CSV" \
        --backtest "$BT_CSV" \
        --window-start "$START" \
        --window-end "$END" \
        --tolerance-min "$TOLERANCE" 2>&1 | tee /tmp/parity_report.log; then
    echo "[parity] PARITY REPORT FAILED — see /tmp/parity_report.log" >&2
    exit 4
fi
PARITY_CSV="$(ls -t OUTPUT/parity/parity_report_*.csv | head -1)"

echo ""
echo "[parity] DONE"
echo "  live     : $LIVE_CSV"
echo "  backtest : $BT_CSV"
echo "  report   : $PARITY_CSV"
