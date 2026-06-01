# IRB Baseline Bridge

TradingView → MetaApi MT5 webhook bridge for the Rob Hoffman IRB v5 Relaxed
Reliable Build (5min Champion) baseline on EURUSD.

Independent of NovaTrade's live runtime. No HardRiskSupervisor. Dedicated
demo account.

## Components

| File | Purpose |
|---|---|
| `irb_bridge.py` | Flask webhook + MetaApi reconciler |
| `.env` | Credentials (gitignored) |
| `.env.example` | Template |
| `requirements.txt` | Python deps |
| `irb-bridge.service` | systemd unit |
| `../../configs/pinescript/irb_v5_baseline_webhook.pine` | TradingView strategy + alert payload |

## Service ops

```bash
sudo systemctl status irb-bridge
sudo systemctl restart irb-bridge
sudo journalctl -u irb-bridge -f
```

Local health check:

```bash
curl http://127.0.0.1:8081/irb/health
```

Public health check:

```bash
curl https://nova-link.duckdns.org/irb/health
```

## TradingView setup

1. Open EURUSD, switch to **5m**.
2. Pine editor → New → paste `configs/pinescript/irb_v5_baseline_webhook.pine`
   → Save → Add to chart.
3. Right-click chart → **Add alert**.
4. Condition: **Rob Hoffman IRB v5 - Relaxed Reliable Build**,
   `alert() function calls only`.
5. Expiration: **Open-ended** (TradingView Pro+ required for >1mo alerts).
6. Notifications tab → enable **Webhook URL**:
   `https://nova-link.duckdns.org/irb/webhook`
7. Message: `{{strategy.order.alert_message}}` (single placeholder — the
   Pine script builds the full JSON via `alert_message=` on each order).

That's it. One alert covers entries, partial exits, runner stops, and time
stops.

## Webhook contract

```json
POST /irb/webhook
{
  "secret": "...",
  "position_size": "<signed-net-position>",
  "comment": "entry_long | exit_long_tp1 | exit_long_runner | time_stop_long | ...",
  "action": "{{strategy.order.action}}",
  "entry": "<fill price>",   // entry alerts only
  "stop":  "<initial stop>"  // entry alerts only
}
```

`comment` drives the state machine: only alerts whose comment starts with
`entry` set the reference position and the risk-based full lot. Exit alerts
(`exit_*`, `time_stop_*`) scale that full lot by the surviving fraction, and
the fraction is **monotonically non-increasing** within a trade — so an
out-of-order or duplicate exit alert can never re-open or grow a position. A
full-close alert (`position_size == 0`) always flattens.

## Risk-based sizing

Each entry is sized to risk `IRB_RISK_PCT` of **live account equity** (read
from MetaApi at entry), given the stop distance from the Pine alert:

```
lot = equity * IRB_RISK_PCT/100 / (IRB_CONTRACT_SIZE * |entry - stop|)
```

then snapped to `IRB_LOT_STEP` and clamped to `[IRB_MIN_LOT, IRB_MAX_LOT]`.
On a ~$100k demo at IRB's ~2.8-pip median stop, 1% risk ≈ 35 lots. The
`IRB_MAX_LOT` cap (default 50) only binds on stops tighter than ~2 pips, where
it safely *under*-sizes (risk < 1%).

**Fallbacks (safe-by-design):**
- If equity can't be read (MetaApi hiccup) or `entry`/`stop` are missing, the
  trade sizes at `IRB_BASE_LOT` (0.10) and a **WARNING** is logged — the trade
  is taken at a small fixed size rather than blocked, but it is *not*
  risk-based that round.
- On startup the bridge **adopts** any pre-existing broker position into its
  state (handles a restart mid-trade): subsequent partials HOLD (entry
  reference is unknown) and the eventual full-close alert flattens. Logged at
  WARNING.

State (`bridge_state.json`) schema: `{side, entry_units, full_lot, last_fraction}`.
Delete it to force a clean start (do this when changing the contract).

## Config

| Env var | Default | Purpose |
|---|---|---|
| `IRB_WEBHOOK_SECRET` | — | Shared secret with the Pine script |
| `METAAPI_TOKEN` | — | MetaApi JWT |
| `METAAPI_ACCOUNT_ID` | — | Demo account UUID |
| `METAAPI_REGION` | `london` | MetaApi region |
| `IRB_SYMBOL` | `EURUSD` | Broker symbol |
| `IRB_RISK_PCT` | `1.0` | % of equity risked per trade |
| `IRB_CONTRACT_SIZE` | `100000` | Units per lot (EURUSD standard) |
| `IRB_MIN_LOT` | `0.01` | Minimum broker lot |
| `IRB_MAX_LOT` | `50.0` | Safety cap on lot size |
| `IRB_LOT_STEP` | `0.01` | Broker lot increment |
| `IRB_BASE_LOT` | `0.10` | Fallback lot when equity/stop unavailable |
| `IRB_DRY_EQUITY` | `100000` | Equity used in `IRB_DRY_RUN` |
| `IRB_BIND_HOST` | `127.0.0.1` | Flask bind (nginx proxies) |
| `IRB_BIND_PORT` | `8081` | Flask port |
| `IRB_DRY_RUN` | `false` | If true, log reconciles but don't trade |
