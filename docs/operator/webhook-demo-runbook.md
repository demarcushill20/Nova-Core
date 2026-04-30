# Webhook Demo Pipeline — Operator Runbook (v1)

Operational steps to bring the second NovaTrade runner instance online against a fresh IC Markets cTrader Demo via MetaApi. Existing `novacore-novatrade.service` (port 8877, vault-port track) stays untouched throughout.

## Prerequisites

- **Spec:** `docs/superpowers/specs/2026-04-30-tv-webhook-demo-pipeline-design.md`
- **Plan:** `docs/superpowers/plans/2026-04-30-tv-webhook-demo-pipeline-v1.md` (vault index: `10-plans/plan-tv-webhook-demo-pipeline-v1.md`)
- Code phases 1–5 merged into `main` (or running on the `tv-webhook-demo-pipeline-v1` branch).

## 1. Provision IC Markets cTrader Demo via MetaApi

1. Open <https://app.metaapi.cloud> and sign in.
2. **Add new MT5 account** → broker: **IC Markets Global**, type: **Demo**, server: pick the cTrader-compatible server (e.g. `ICMarkets-Demo01`).
3. Account size: **$100,000**.
4. Save. Copy the **account ID** (UUID) and the **API token** — needed in step 2.

## 2. Install production env file

Copy `configs/novatrade.webhook.env.example` → `/etc/novacore/novatrade-webhook.env` (mode 600, owner `nova`) and replace the three `fill-at-deploy` placeholders:

- `METAAPI_TOKEN` — from step 1
- `METAAPI_ACCOUNT_ID` — from step 1
- `NOVATRADE_WEBHOOK_SECRET` — generate one fresh:

```bash
sudo install -m 600 -o nova -g nova \
    /home/nova/nova-core/configs/novatrade.webhook.env.example \
    /etc/novacore/novatrade-webhook.env

openssl rand -hex 32   # copy this output to NOVATRADE_WEBHOOK_SECRET

sudo nano /etc/novacore/novatrade-webhook.env   # fill all three values
```

`FTMO_CAMPAIGN_LABEL` is already set to `ic-markets-demo-2026-q2` and **must match the `CAMPAIGN` literal in `configs/pinescript/irb_v5_m5_webhook.pine`**. Don't change one without the other.

## 3. Install systemd unit

Create `/etc/systemd/system/novacore-novatrade-webhook.service`:

```ini
[Unit]
Description=NovaTrade Webhook Pipeline (second instance, IC Markets demo)
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=nova
Group=nova
WorkingDirectory=/home/nova/nova-core
EnvironmentFile=/etc/novacore/novatrade-webhook.env
ExecStart=/usr/bin/python3 -m novatrade.runtime.runner
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable novacore-novatrade-webhook.service
# Don't start yet — finish DNS + nginx + cert in step 4 first.
```

## 4. Subdomain + nginx + Let's Encrypt cert

1. Add a DNS A record: `nova-webhook.duckdns.org` → VPS public IP (use the duckdns API or the dashboard).
2. Wait for propagation: `dig +short nova-webhook.duckdns.org` should return the VPS IP.
3. Install `/etc/nginx/sites-available/nova-webhook`:

```nginx
server {
    server_name nova-webhook.duckdns.org;

    location /webhook/  { proxy_pass http://127.0.0.1:8878; proxy_set_header Host $host; }
    location /health    { proxy_pass http://127.0.0.1:8878; proxy_set_header Host $host; }
    location /status    { proxy_pass http://127.0.0.1:8878; proxy_set_header Host $host; }
    location /readiness { proxy_pass http://127.0.0.1:8878; proxy_set_header Host $host; }
    location /control/  { proxy_pass http://127.0.0.1:8878; proxy_set_header Host $host; }

    listen 80;
}
```

4. Enable + cert:

```bash
sudo ln -s /etc/nginx/sites-available/nova-webhook /etc/nginx/sites-enabled/nova-webhook
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d nova-webhook.duckdns.org
```

## 5. Patch the Pine fork's `WH_SECRET`

The committed `configs/pinescript/irb_v5_m5_webhook.pine` has `WH_SECRET = "REPLACE_AT_DEPLOY"`. **Do NOT commit a real secret to the repo.** Either:

- Edit the literal directly in the TradingView Pine editor before saving the alert (recommended), or
- Locally edit the file with the real secret and **do not commit** the change (use a private working copy or stash).

The Pine secret MUST match `NOVATRADE_WEBHOOK_SECRET` in `/etc/novacore/novatrade-webhook.env` exactly.

## 6. Start the service + verify

```bash
sudo systemctl start novacore-novatrade-webhook.service
sudo systemctl status novacore-novatrade-webhook.service
journalctl -u novacore-novatrade-webhook.service -f
```

Verify the public endpoint:

```bash
curl https://nova-webhook.duckdns.org/health
# Expect: {"status":"ok",...,"adapter_connected":true,...}

curl https://nova-webhook.duckdns.org/status | jq '.runtime_mode, .adapter_type, .webhook'
# Expect runtime_mode="active_ready", adapter_type="MetaApiAdapter", webhook counters present
```

## 7. Configure TradingView alert

1. In TradingView, paste `configs/pinescript/irb_v5_m5_webhook.pine` into the Pine editor (with the real secret patched into `WH_SECRET` per step 5).
2. **Add to chart** on EURUSD M5. Confirm there are no red compile errors in the editor.
3. **Create Alert** → Condition: **Any alert() function call**.
4. **Notifications → Webhook URL**: `https://nova-webhook.duckdns.org/webhook/alert`.
5. **Message:** leave blank — Pine emits the JSON body directly.
6. Save the alert. (TV Pro plan supports the volume we expect — see spec D7.)

## 8. Smoke verification

1. Wait for the next bar close on EURUSD M5 that produces an IRB signal.
2. In `journalctl -u novacore-novatrade-webhook.service`, look for:
   - `WEBHOOK_RECEIVED` event with the alert payload
   - `WEBHOOK_ROUTED` event with `success:true` (or a `rejected_reason` if a guard vetoed)
3. Confirm the order appears on the IC Markets demo via metaapi.cloud's account view or the cTrader UI.

**Per `CLAUDE.md` "NovaTrade Live System Safety": never invoke supervisor with synthetic data on a live system.** Smoke verification uses real Pine bar-close events only.

## 9. Daily reconciliation cron

```bash
crontab -e
```

Add:

```
0 7 * * * cd /home/nova/nova-core && python3 scripts/diff_pine_alerts_vs_metaapi.py \
  --account-id $(grep METAAPI_ACCOUNT_ID /etc/novacore/novatrade-webhook.env | cut -d= -f2) \
  --since $(date -d "yesterday" +%F) \
  > OUTPUT/novatrade/reconciliation/$(date +%F).json
```

The script classifies each divergence as:

- `alert_without_deal` — Pine alert recorded but no MetaApi deal (likely missed delivery)
- `deal_without_alert` — MetaApi deal exists but no matching alert (manual trade?)
- `parameter_mismatch` — alert+deal pair found, but volume/price differs
- `expected_v1_divergence` — Pine PARTIAL_TP fill with no PARTIAL_CLOSE alert (intentional v1 deferral; not a real mismatch)

## 10. Rollback

To stop the second instance without affecting the existing runner:

```bash
sudo systemctl stop novacore-novatrade-webhook.service
sudo systemctl disable novacore-novatrade-webhook.service
```

The original `novacore-novatrade.service` (port 8877, vault-port track) is untouched throughout. If you want to also disable the public endpoint:

```bash
sudo rm /etc/nginx/sites-enabled/nova-webhook
sudo systemctl reload nginx
```
