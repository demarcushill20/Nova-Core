# TIMBOT Webhook Bridge — Design & Deployment

Fully-automated (Path B) demo: TradingView strategy alerts drive an MT5 demo
account via MetaApi. The TIMBOT Pine script is **not modified** — TradingView
strategy alerts expose `{{strategy.order.*}}` placeholders that carry the order
data.

## Flow

```
TIMBOT strategy (TradingView, XAUUSD 15m)
   │  every order fill → alert fires, POSTs JSON
   ▼
nginx :443  (https://nova-link.duckdns.org/timbot/  →  127.0.0.1:8080)
   ▼
timbot_bridge.py   (Flask, 127.0.0.1:8080, /timbot/webhook)
   │  validate shared secret → map position_size → target lot
   ▼
MetaApi RPC  →  dedicated MT5 demo account
```

TradingView only allows webhooks on ports 80/443, so the bridge is fronted by
the VPS's existing nginx (shared infra; a dedicated `location /timbot/` block —
NovaTrade routes untouched). The bridge itself binds 127.0.0.1 only.

## Why position reconciliation (not order copying)

Each TradingView fill (entry, `2R partial`, `HTF runner`, `BE runner`, reversal)
sends the resulting `strategy.position_size`. The bridge drives the demo's net
position to match. This is **idempotent and self-correcting** — a missed or
duplicated webhook does not desync the account, because the next webhook still
carries the absolute target.

## Sizing translation

TradingView trades ~0.5 oz (5% of $100k equity); MT5 lots have a 0.01 minimum,
so exact replication is impossible. Instead:

- A fresh entry (`comment` = `L` or `S`) sets the **reference** = that full
  position size.
- Target lot = `BASE_LOT × (current position_size / reference)`, signed.
- So a 50% partial leaves `BASE_LOT/2`; a full exit → 0; a reversal → flip.

`BASE_LOT` (default 0.10) is configurable. The demo validates the strategy's
**timing, direction, and exit behaviour** faithfully; absolute $ differ from
the backtest by the fixed lot scaling — state this in the validation report.

## Security

- Every request must carry the shared `secret`; mismatches are rejected 401.
- `.env` holds all credentials and is gitignored. Never commit it.
- Bridge binds `127.0.0.1:8080` only — not publicly reachable. All external
  traffic arrives via nginx on :443 (TLS, Let's Encrypt cert), path `/timbot/`.
- Port 8080 is NOT open in the firewall (deliberately).

## Deployment (NovaCore VPS)

```bash
cd /home/nova/nova-core/clients/timbot/bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env          # then fill in real values
python3 -c "import secrets;print(secrets.token_urlsafe(32))"   # secret

# open the firewall port (needs sudo — run yourself):
#   sudo ufw allow 8080/tcp

# smoke test in DRY_RUN first (TIMBOT_DRY_RUN=true in .env):
.venv/bin/python timbot_bridge.py
#   curl localhost:8080/timbot/health

# run as a service:
sudo cp timbot-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now timbot-bridge
```

## TradingView alert

1. Create an alert on the **strategy** `TIMBOT OFFICIAL — Universal`.
2. Condition: order fills (first Condition dropdown = the strategy itself).
3. **Webhook URL:** `https://nova-link.duckdns.org/timbot/webhook`
4. **Message:** paste the contents of `tradingview_alert_message.json`, with
   `secret` replaced by the real `TIMBOT_WEBHOOK_SECRET`.

## Go-live checklist

- [ ] `.env` filled, secret generated
- [ ] MT5 **demo** account provisioned in MetaApi, deployed, account ID in `.env`
- [ ] `TIMBOT_SYMBOL` matches the demo broker's gold symbol exactly
- [ ] firewall: `8080/tcp` open
- [ ] DRY_RUN smoke test passed (`/timbot/health` + a test POST)
- [ ] DRY_RUN test POST drives a reconcile in the log
- [ ] `TIMBOT_DRY_RUN=false`, service restarted
- [ ] TradingView alert created, webhook URL + secret set
- [ ] first live fill mirrored correctly on the demo account
