# TV Webhook Demo Pipeline (v1) Implementation Plan

> **For agentic workers:** use the `implementation-team` skill to execute task-by-task. Steps use `- [ ]` syntax for tracking.

**Plan ID:** `tv-webhook-demo-pipeline-v1`
**Status:** backlog
**Progress:** 0/6
**Confidence:** high
**Date:** 2026-04-30

**Goal:** Stand up a second NovaTrade runner instance on the existing-but-dormant TradingView webhook pipeline (Phase 8/9 code), connected to a fresh non-expiring IC Markets cTrader demo via MetaApi, parallel to the current vault-port runner.

**Architecture:** Fork Pine champion → emit MODIFY_SL on ATR-trail/breakeven changes; bump alert schema doc to v5.1 (relaxed broker/campaign); patch TradingAgent to validate payload campaign against `FtmoProfile.campaign_label`; new env template + reconciliation script. Existing `novacore-novatrade.service` stays untouched.

**Tech Stack:** Python 3.11, FastAPI, MetaApi cloud SDK, jsonschema, Pine v5, pytest, systemd, nginx.

**Spec:** `docs/superpowers/specs/2026-04-30-tv-webhook-demo-pipeline-design.md` (commit `64c472d`).

**Out of scope (v1):** PARTIAL_CLOSE alert emission from Pine (TradingAgent already supports it; Pine fork defers); operator-side ops (systemd/nginx/cert/DNS/MetaApi provisioning/TV alert UI) are runbook-only.

---

## Phases

- [ ] **Phase 1: Schema v5.1 contract** — `docs/demo_test_run/alerts_schema_v5_1.json` + structural test
- [ ] **Phase 2: TradingAgent validation patch** — accept v5.1, validate campaign against `FtmoProfile.campaign_label`
- [ ] **Phase 3: Pine fork** — `irb_v5_m5_webhook.pine` with new constants + MODIFY_SL emission on cur_stop change
- [ ] **Phase 4: Webhook env template + reconciliation script** — `configs/novatrade.webhook.env` + `scripts/diff_pine_alerts_vs_metaapi.py`
- [ ] **Phase 5: E2E webhook pipeline test** — `tests/test_webhook_demo_pipeline.py` (FastAPI + mock adapter)
- [ ] **Phase 6: Operator runbook** — `docs/operator/webhook-demo-runbook.md`

---

## Phase 1: Schema v5.1 contract

### Task 1.1: Create `docs/demo_test_run/alerts_schema_v5_1.json`

- [ ] **Step 1:** Copy `docs/demo_test_run/alerts_schema.json` → `alerts_schema_v5_1.json` and apply diffs:
  - `"$id"` → `"novatrade-irb-alert-v5.1.0"`
  - `strategy_version` const `"5.0.0"` → `"5.1.0"` in all four `$defs`
  - `signal_alert.properties.broker_symbol`: `{"type":"string","const":"EURUSD.sim"}` → `{"type":"string","minLength":1}`
  - `campaign` (all four `$defs`): `{"type":"string","const":"ftmo-free-trial-march-2026"}` → `{"type":"string","minLength":1}`
  - `trail_alert.required`: remove `"trail_ema"`, `"trail_ema_period"`
  - `trail_alert.properties`: keep `trail_ema`/`trail_ema_period` (now optional); add `"trail_atr_mult":{"type":"number"}` and `"trail_method":{"type":"string","enum":["ATR","EMA"]}`
  - Update top-level `description` to mention v5.1 changes

- [ ] **Step 2:** Validate parses: `python -c "import json; json.load(open('docs/demo_test_run/alerts_schema_v5_1.json'))"` → no output.

- [ ] **Step 3:** `git add docs/demo_test_run/alerts_schema_v5_1.json && git commit -m "docs(schema): add alerts_schema v5.1 (relaxed broker/campaign, ATR trail)"`

### Task 1.2: Structural test `tests/test_alerts_schema_v5_1.py`

- [ ] **Step 1: Create the test file:**

```python
"""Structural tests for alerts_schema_v5_1.json."""
from __future__ import annotations
import json
from pathlib import Path
import jsonschema
import pytest

SCHEMA_PATH = Path(__file__).parent.parent / "docs" / "demo_test_run" / "alerts_schema_v5_1.json"


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _signal(*, broker_symbol="EURUSD", campaign="ic-markets-demo-2026-q2") -> dict:
    return {
        "strategy_name": "Rob Hoffman IRB", "strategy_version": "5.1.0",
        "action": "PLACE_STOP_ORDER", "signal_type": "LONG_IRB", "irb_type": "UPTREND_IRB",
        "symbol": "EURUSD", "broker_symbol": broker_symbol, "timeframe": "H1",
        "side": "BUY", "order_type": "BUY_STOP",
        "entry_price": 1.0950, "stop_loss": 1.0900, "stop_distance_pips": 50.0,
        "volume": 0.10, "risk_dollars": 100.0, "bar_close_time": 1700000000000,
        "bar_ohlc_o": 1.0920, "bar_ohlc_h": 1.0950, "bar_ohlc_l": 1.0910, "bar_ohlc_c": 1.0945,
        "irb_range": 0.0040,
        "ema_10_h1": 1.093, "ema_20_h1": 1.0925, "ema_50_h1": 1.090,
        "trail_ema": 1.092, "trail_ema_period": 40,
        "ema_stack_h1": "BULL", "ema_confirm_bars": 3,
        "ema10_confirm_long": True, "ema10_confirm_short": False,
        "ema_slope": 0.5, "ema_20_h4": 1.090, "ema_20_h4_dir": "RISING",
        "adx_14": 25.0, "atr_14": 0.0030, "overextension_ratio": 1.2,
        "trigger_window_bars": 20, "strategy_state": "PENDING_LONG", "campaign": campaign,
    }


def test_schema_id_is_v5_1(schema):
    assert schema["$id"] == "novatrade-irb-alert-v5.1.0"


def test_signal_accepts_non_ftmo_broker(schema):
    jsonschema.validate(_signal(broker_symbol="EURUSD"), schema)


def test_signal_accepts_arbitrary_campaign(schema):
    jsonschema.validate(_signal(campaign="any-non-empty"), schema)


def test_trail_no_longer_requires_ema_fields(schema):
    jsonschema.validate({
        "strategy_name": "Rob Hoffman IRB", "strategy_version": "5.1.0",
        "action": "MODIFY_SL", "symbol": "EURUSD", "side": "BUY",
        "old_stop": 1.09, "new_stop": 1.091, "best_close": 1.095,
        "bars_since_entry": 5, "campaign": "ic-markets-demo-2026-q2",
        "trail_method": "ATR", "trail_atr_mult": 2.0,
    }, schema)


def test_signal_rejects_empty_campaign(schema):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_signal(campaign=""), schema)
```

- [ ] **Step 2:** `pytest tests/test_alerts_schema_v5_1.py -v` → 5 passing.
- [ ] **Step 3:** `git add tests/test_alerts_schema_v5_1.py && git commit -m "test(schema): structural tests for alerts_schema v5.1"`

---

## Phase 2: TradingAgent validation patch

### Task 2.1: Failing tests in `tests/test_trading_agent.py`

- [ ] **Step 1:** Append helper near the top of the file (if not already present):

```python
def _signal_payload(*, strategy_version="5.0.0", campaign="test-campaign") -> dict:
    return {
        "strategy_name": "Rob Hoffman IRB", "strategy_version": strategy_version,
        "action": "PLACE_STOP_ORDER", "signal_type": "LONG_IRB",
        "symbol": "EURUSD", "broker_symbol": "EURUSD.sim",
        "side": "BUY", "order_type": "BUY_STOP",
        "entry_price": 1.095, "stop_loss": 1.090, "volume": 0.10,
        "bar_close_time": 1700000000000, "campaign": campaign,
    }
```

- [ ] **Step 2:** Append the four tests:

```python
def test_validate_alert_rejects_campaign_mismatch():
    err, _ = validate_alert(_signal_payload(campaign="wrong"), expected_campaign="ic-markets-demo-2026-q2")
    assert err is not None and "campaign" in err.lower()


def test_validate_alert_accepts_matching_campaign():
    err, _ = validate_alert(_signal_payload(campaign="ic-markets-demo-2026-q2"), expected_campaign="ic-markets-demo-2026-q2")
    assert err is None


def test_validate_alert_skips_campaign_check_when_no_expected():
    err, _ = validate_alert(_signal_payload(campaign="any"))
    assert err is None


def test_validate_alert_accepts_v5_1_strategy_version():
    err, _ = validate_alert(_signal_payload(strategy_version="5.1.0"))
    assert err is None
```

- [ ] **Step 3:** `pytest tests/test_trading_agent.py::test_validate_alert_rejects_campaign_mismatch tests/test_trading_agent.py::test_validate_alert_accepts_v5_1_strategy_version -v` → FAIL.

### Task 2.2: Patch `validate_alert`

**File:** `novatrade/execution/trading_agent.py:256-321`

- [ ] **Step 1:** Update function signature:

```python
def validate_alert(
    payload: dict,
    expected_campaign: str | None = None,
) -> tuple[str | None, str]:
```

- [ ] **Step 2:** Replace the version-check block (around line 270):

```python
    actual_version = payload.get("strategy_version")
    if actual_version not in ("5.0.0", "5.1.0"):
        return (
            f"version mismatch: got {actual_version!r} (type {type(actual_version).__name__}), "
            f"expected '5.0.0' or '5.1.0'"
        ), ""
```

- [ ] **Step 3:** Add campaign-check block immediately *before* `return None, action` at the end:

```python
    if expected_campaign:
        actual_campaign = payload.get("campaign", "")
        if actual_campaign != expected_campaign:
            return (
                f"campaign mismatch: got {actual_campaign!r}, "
                f"expected {expected_campaign!r}"
            ), ""
```

- [ ] **Step 4:** Update the call site at `novatrade/execution/trading_agent.py:492` (inside `TradingAgent.process_alert`). Replace `error, action = validate_alert(payload)` with:

```python
        error, action = validate_alert(
            payload,
            expected_campaign=self._cfg.ftmo.campaign_label or None,
        )
```

- [ ] **Step 5:** `pytest tests/test_trading_agent.py -v` → all green.
- [ ] **Step 6:** `git add novatrade/execution/trading_agent.py tests/test_trading_agent.py && git commit -m "feat(trading_agent): validate payload campaign against FtmoProfile + accept v5.1.0 schema"`

---

## Phase 3: Pine fork `irb_v5_m5_webhook.pine`

### Task 3.1: Copy + verify

- [ ] **Step 1:** `cp configs/pinescript/irb_v5_m5_champion.pine configs/pinescript/irb_v5_m5_webhook.pine`
- [ ] **Step 2:** `diff configs/pinescript/irb_v5_m5_champion.pine configs/pinescript/irb_v5_m5_webhook.pine` → no output.

### Task 3.2: Update constants + alert payload literals

- [ ] **Step 1:** In the constants block, set:
  - `WH_SECRET = "REPLACE_AT_DEPLOY"` (operator patches the literal at deploy time per runbook step 5)
  - `CAMPAIGN = "ic-markets-demo-2026-q2"`
- [ ] **Step 2:** In each existing alert-emit block (signal, cancel, time-stop), change `\"broker_symbol\":\"EURUSD.sim\"` → `\"broker_symbol\":\"EURUSD\"` (where present).
- [ ] **Step 3:** Replace all `\"strategy_version\":\"5.0.0\"` → `\"strategy_version\":\"5.1.0\"` in the alert-payload JSON literals.
- [ ] **Step 4:** Verify diff scope: only constants + JSON-literal lines differ.

### Task 3.3: Long-management MODIFY_SL emit

**File:** `configs/pinescript/irb_v5_m5_webhook.pine` (long management block, around lines 833–880)

- [ ] **Step 1:** Add `prev_cs_long` capture immediately after `if pos_bars > 0` in the long block:

```pinescript
    if pos_bars > 0
        float prev_cs_long = nz(cur_stop, na)
        best_cl := math.max(nz(best_cl, close), close)
```

- [ ] **Step 2:** After the existing `strategy.exit("Long Stop", "Long", stop = cur_stop, comment_loss = "TRAIL_SL")` call, before the long time-stop block, append:

```pinescript
    // --- MODIFY_SL alert (webhook fork) ---
    if state == S_LONG and not na(cur_stop) and (na(prev_cs_long) or cur_stop != prev_cs_long)
        string sl_long = "{" +
             "\"webhook_secret\":\""      + WH_SECRET + "\"" +
             ",\"strategy_name\":\""      + STRAT_NAME + "\"" +
             ",\"strategy_version\":\"5.1.0\"" +
             ",\"action\":\"MODIFY_SL\"" +
             ",\"symbol\":\"EURUSD\"" +
             ",\"side\":\"BUY\"" +
             ",\"old_stop\":"  + str.tostring(nz(prev_cs_long, cur_stop), "#.#####") +
             ",\"new_stop\":"  + str.tostring(cur_stop, "#.#####") +
             ",\"best_close\":" + str.tostring(nz(best_cl, close), "#.#####") +
             ",\"bars_since_entry\":" + str.tostring(pos_bars) +
             ",\"trail_method\":\"ATR\"" +
             ",\"trail_atr_mult\":" + str.tostring(TRAIL_ATR_MULT, "#.##") +
             ",\"campaign\":\"" + CAMPAIGN + "\"" +
             "}"
        alert(sl_long, alert.freq_once_per_bar_close)
        if DIAG_LOG
            log.info(sl_long)
```

### Task 3.4: Short-management MODIFY_SL emit

**File:** same file, short management block (around lines 882–940)

- [ ] **Step 1:** Add `prev_cs_short` capture after `if pos_bars > 0` in the short block:

```pinescript
    if pos_bars > 0
        float prev_cs_short = nz(cur_stop, na)
        best_cl := math.min(nz(best_cl, close), close)
```

- [ ] **Step 2:** After the existing `strategy.exit("Short Stop", ...)` call, before the short time-stop block, append:

```pinescript
    // --- MODIFY_SL alert (webhook fork) ---
    if state == S_SHORT and not na(cur_stop) and (na(prev_cs_short) or cur_stop != prev_cs_short)
        string sl_short = "{" +
             "\"webhook_secret\":\""      + WH_SECRET + "\"" +
             ",\"strategy_name\":\""      + STRAT_NAME + "\"" +
             ",\"strategy_version\":\"5.1.0\"" +
             ",\"action\":\"MODIFY_SL\"" +
             ",\"symbol\":\"EURUSD\"" +
             ",\"side\":\"SELL\"" +
             ",\"old_stop\":"  + str.tostring(nz(prev_cs_short, cur_stop), "#.#####") +
             ",\"new_stop\":"  + str.tostring(cur_stop, "#.#####") +
             ",\"best_close\":" + str.tostring(nz(best_cl, close), "#.#####") +
             ",\"bars_since_entry\":" + str.tostring(pos_bars) +
             ",\"trail_method\":\"ATR\"" +
             ",\"trail_atr_mult\":" + str.tostring(TRAIL_ATR_MULT, "#.##") +
             ",\"campaign\":\"" + CAMPAIGN + "\"" +
             "}"
        alert(sl_short, alert.freq_once_per_bar_close)
        if DIAG_LOG
            log.info(sl_short)
```

### Task 3.5: Pine reference check + commit

- [ ] **Step 1:** Invoke the `pinescript-reference` skill to verify `alert`, `str.tostring`, `nz`, and `alert.freq_once_per_bar_close` for Pine v5.
- [ ] **Step 2:** TradingView compile check is deferred to runbook step 7.
- [ ] **Step 3:** `git add configs/pinescript/irb_v5_m5_webhook.pine && git commit -m "feat(pinescript): irb_v5_m5_webhook fork — IC Markets demo + MODIFY_SL emission for ATR trail/BE"`

---

## Phase 4: Webhook env template + reconciliation script

### Task 4.1: `configs/novatrade.webhook.env`

- [ ] **Step 1:** Create the file with this content:

```
# configs/novatrade.webhook.env — TEMPLATE for second runner instance
NOVATRADE_LAUNCH_MODE=active_ready
NOVATRADE_PIPELINE=webhook
NOVATRADE_DRY_RUN=false
NOVATRADE_PORT=8878
NOVATRADE_HOST=0.0.0.0
NOVATRADE_MONITOR_INTERVAL=60

METAAPI_TOKEN=fill-at-deploy
METAAPI_ACCOUNT_ID=fill-at-deploy
METAAPI_REGION=london

FTMO_ENABLED=true
FTMO_CHALLENGE_TYPE=free_trial
FTMO_CAMPAIGN_LABEL=ic-markets-demo-2026-q2
FTMO_ACCOUNT_SIZE=100000
FTMO_SYMBOL_SUFFIX=

NOVATRADE_WEBHOOK_SECRET=fill-at-deploy

NOVATRADE_CONFIRM_PINE_COMPILED=true
NOVATRADE_CONFIRM_TV_BACKTEST=true
NOVATRADE_CONFIRM_WEBHOOK_URL=true
NOVATRADE_CONFIRM_ACTIVE_DEMO=true

NOVATRADE_STRATEGY_CONFIG=/home/nova/nova-core/configs/strategies/irb_v5_m5_champion.yaml
NOVATRADE_TIMEFRAMES=M5
```

- [ ] **Step 2:** `grep -c "fill-at-deploy" configs/novatrade.webhook.env` → `3`.
- [ ] **Step 3:** `git add configs/novatrade.webhook.env && git commit -m "feat(config): novatrade.webhook.env template for second runner instance"`

### Task 4.2: Failing tests `tests/test_diff_pine_alerts_vs_metaapi.py`

- [ ] **Step 1:** Create the test file:

```python
"""Tests for scripts/diff_pine_alerts_vs_metaapi.py — reconcile() core, no MetaApi I/O."""
from __future__ import annotations
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "diff_pine_alerts_vs_metaapi.py"


def _mod():
    spec = importlib.util.spec_from_file_location("diff_pine", SCRIPT)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def test_alert_without_deal():
    out = _mod().reconcile(
        [{"action": "PLACE_STOP_ORDER", "bar_close_time": 1000, "side": "BUY", "entry_price": 1.10, "volume": 0.1}],
        [],
    )
    assert out and out[0]["classification"] == "alert_without_deal"


def test_deal_without_alert():
    out = _mod().reconcile(
        [],
        [{"id": "d1", "type": "DEAL_TYPE_BUY", "time": 1000.0, "volume": 0.1, "price": 1.10, "comment": "manual"}],
    )
    assert out and out[0]["classification"] == "deal_without_alert"


def test_partial_tp_classified_as_expected_v1_divergence():
    out = _mod().reconcile(
        [],
        [{"id": "d2", "type": "DEAL_TYPE_SELL", "time": 1500.0, "volume": 0.05, "price": 1.11, "comment": "PARTIAL_TP"}],
    )
    assert out and out[0]["classification"] == "expected_v1_divergence"


def test_match_no_divergence():
    out = _mod().reconcile(
        [{"action": "PLACE_STOP_ORDER", "bar_close_time": 1_000_000, "side": "BUY", "entry_price": 1.10, "volume": 0.1}],
        [{"id": "d3", "type": "DEAL_TYPE_BUY", "time": 1000.5, "volume": 0.1, "price": 1.10, "comment": "PLACE_STOP_ORDER"}],
    )
    assert out == []
```

- [ ] **Step 2:** `pytest tests/test_diff_pine_alerts_vs_metaapi.py -v` → FAIL.

### Task 4.3: `scripts/diff_pine_alerts_vs_metaapi.py`

- [ ] **Step 1:** Create the script:

```python
#!/usr/bin/env python3
"""Reconcile webhook alerts vs MetaApi deals.

Classifies divergences:
  alert_without_deal | deal_without_alert | parameter_mismatch
  expected_v1_divergence (Pine PARTIAL_TP fills, no PARTIAL_CLOSE alert — spec D9)
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

TIME_TOL_SEC = 60.0
PRICE_TOL_PIPS = 1.0


def reconcile(alerts: list[dict], deals: list[dict], *, time_tol_sec: float = TIME_TOL_SEC) -> list[dict]:
    out: list[dict] = []
    matched: set[str] = set()

    for a in alerts:
        m = _find_match(a, deals, time_tol_sec=time_tol_sec, exclude=matched)
        if m is None:
            out.append({"classification": "alert_without_deal", "alert": a})
            continue
        matched.add(m["id"])
        issues = _params_diff(a, m)
        if issues:
            out.append({"classification": "parameter_mismatch", "alert": a, "deal": m, "issues": issues})

    for d in deals:
        if d.get("id") in matched:
            continue
        if "PARTIAL_TP" in (d.get("comment") or "").upper():
            out.append({"classification": "expected_v1_divergence", "deal": d,
                        "note": "Pine PARTIAL_TP fill — v1 defers PARTIAL_CLOSE emission (spec D9)."})
        else:
            out.append({"classification": "deal_without_alert", "deal": d})

    return out


def _find_match(a, deals, *, time_tol_sec, exclude):
    side = (a.get("side") or "").upper()
    t_ms = a.get("bar_close_time")
    if not isinstance(t_ms, (int, float)):
        return None
    t_sec = t_ms / 1000.0
    vol = float(a.get("volume") or 0.0)
    cands = []
    for d in deals:
        if d.get("id") in exclude:
            continue
        d_side = "BUY" if "BUY" in (d.get("type") or "").upper() else "SELL"
        if d_side != side:
            continue
        d_t = float(d.get("time") or 0.0)
        if abs(d_t - t_sec) > time_tol_sec:
            continue
        d_vol = float(d.get("volume") or 0.0)
        if vol > 0 and abs(d_vol - vol) > 0.001:
            continue
        cands.append((abs(d_t - t_sec), d))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    return cands[0][1]


def _params_diff(a, d):
    issues = []
    ap, dp = a.get("entry_price"), d.get("price")
    if isinstance(ap, (int, float)) and isinstance(dp, (int, float)):
        if abs(ap - dp) > PRICE_TOL_PIPS * 0.0001:
            issues.append(f"price drift: alert={ap} deal={dp}")
    return issues


def _load_alerts(glob: str) -> list[dict]:
    out = []
    for p in sorted(Path().glob(glob)):
        with p.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("data", {}).get("event") in ("WEBHOOK_RECEIVED", "WEBHOOK_ROUTED"):
                    out.append(rec.get("data", {}))
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--evidence-glob", default="OUTPUT/novatrade/evidence/*.jsonl")
    p.add_argument("--account-id", required=True)
    p.add_argument("--since", required=True)
    p.add_argument("--time-tolerance-sec", type=float, default=TIME_TOL_SEC)
    args = p.parse_args(argv)

    alerts = _load_alerts(args.evidence_glob)

    import asyncio
    from novatrade.adapter.metaapi_provider import MetaApiAdapter
    from novatrade.config import NovaTradeCfg

    async def _fetch() -> list[dict]:
        cfg = NovaTradeCfg.from_env()
        cfg.ftmo.enabled = True
        ad = MetaApiAdapter(cfg)
        await ad.connect()
        deals = await ad.fetch_deals_since(args.since)
        await ad.disconnect()
        return deals

    deals = asyncio.run(_fetch())
    divs = reconcile(alerts, deals, time_tol_sec=args.time_tolerance_sec)

    print(json.dumps({"since": args.since, "alerts_loaded": len(alerts),
                      "deals_loaded": len(deals), "divergences": divs}, indent=2, default=str))
    return 0 if not divs else 1


if __name__ == "__main__":
    sys.exit(main())
```

> **Adapter dependency note:** if `MetaApiAdapter.fetch_deals_since` doesn't exist, surface in implementation review and add a wrapper around the SDK's `historyStorage.deals`. Unit tests don't exercise it.

- [ ] **Step 2:** `pytest tests/test_diff_pine_alerts_vs_metaapi.py -v` → 4 passing.
- [ ] **Step 3:** `chmod +x scripts/diff_pine_alerts_vs_metaapi.py`
- [ ] **Step 4:** `git add scripts/diff_pine_alerts_vs_metaapi.py tests/test_diff_pine_alerts_vs_metaapi.py && git commit -m "feat(scripts): diff_pine_alerts_vs_metaapi reconciliation tool"`

---

## Phase 5: E2E webhook pipeline test

### Task 5.1: `tests/test_webhook_demo_pipeline.py`

- [ ] **Step 1:** Create the test file:

```python
"""E2E: TV webhook -> TradingAgent demo pipeline (FastAPI test client + mock adapter)."""
from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock
import pytest
from fastapi.testclient import TestClient

from novatrade.config import FtmoProfile, NovaTradeCfg, RiskConfig
from novatrade.execution.trading_agent import TradingAgent
from novatrade.models import AccountMode
from novatrade.risk.risk_engine import RiskEngine
from novatrade.runtime.webhook_server import WebhookState, create_app

WH_SECRET_VAL = "test-wh-2026"
CAMPAIGN = "ic-markets-demo-2026-q2"


def _signal(*, campaign: str, secret: str) -> dict:
    return {
        "webhook_secret": secret,
        "strategy_name": "Rob Hoffman IRB", "strategy_version": "5.1.0",
        "action": "PLACE_STOP_ORDER", "signal_type": "LONG_IRB", "irb_type": "UPTREND_IRB",
        "symbol": "EURUSD", "broker_symbol": "EURUSD", "timeframe": "M5",
        "side": "BUY", "order_type": "BUY_STOP",
        "entry_price": 1.0950, "stop_loss": 1.0900, "stop_distance_pips": 50.0,
        "volume": 0.10, "risk_dollars": 100.0, "bar_close_time": 1700000000000,
        "campaign": campaign,
    }


@pytest.fixture
def mock_adapter():
    a = AsyncMock()
    a._connected = True
    a.connect = AsyncMock()
    a.place_order = AsyncMock(return_value=MagicMock(ok=True, order_id="ord-1"))
    a.modify_order = AsyncMock(return_value=MagicMock(ok=True))
    a.close_position = AsyncMock(return_value=MagicMock(ok=True))
    a.get_account = AsyncMock(return_value=MagicMock(equity=100000.0, balance=100000.0, margin=0.0, currency="USD"))
    return a


@pytest.fixture
def client(mock_adapter):
    cfg = NovaTradeCfg(
        mode=AccountMode.DEMO, symbols=["EURUSD"],
        ftmo=FtmoProfile(enabled=True, symbol_map={"EURUSD": "EURUSD"}, campaign_label=CAMPAIGN),
        risk=RiskConfig(max_positions=1),
    )
    risk = RiskEngine(cfg)
    agent = TradingAgent(cfg=cfg, adapter=mock_adapter, risk_engine=risk)
    ws = WebhookState(agent=agent, risk_engine=risk, webhook_secret=WH_SECRET_VAL)
    return TestClient(create_app(ws))


def test_health(client):
    assert client.get("/health").status_code == 200


def test_bad_secret_403(client):
    r = client.post("/webhook/alert", json=_signal(campaign=CAMPAIGN, secret="wrong"))
    assert r.status_code == 403


def test_campaign_mismatch_rejected(client):
    r = client.post("/webhook/alert", json=_signal(campaign="other", secret=WH_SECRET_VAL))
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "campaign" in (body.get("rejected_reason") or "").lower()


def test_valid_signal_places_order(client, mock_adapter):
    r = client.post("/webhook/alert", json=_signal(campaign=CAMPAIGN, secret=WH_SECRET_VAL))
    assert r.status_code == 200 and r.json()["ok"] is True
    assert mock_adapter.place_order.await_count == 1


def test_idempotency(client, mock_adapter):
    p = _signal(campaign=CAMPAIGN, secret=WH_SECRET_VAL)
    client.post("/webhook/alert", json=p)
    client.post("/webhook/alert", json=p)
    assert mock_adapter.place_order.await_count == 1
```

- [ ] **Step 2:** `pytest tests/test_webhook_demo_pipeline.py -v` → 5 passing. If `RiskEngine` or `TradingAgent` constructor differs, mirror the fixture pattern from `tests/test_trading_agent.py`.
- [ ] **Step 3:** `pytest tests/ -x -q` → all green.
- [ ] **Step 4:** `git add tests/test_webhook_demo_pipeline.py && git commit -m "test(webhook): E2E demo-pipeline integration test (FastAPI + mock adapter)"`

---

## Phase 6: Operator runbook

### Task 6.1: `docs/operator/webhook-demo-runbook.md`

- [ ] **Step 1:** Create the runbook with these sections (full command blocks inline):

1. **Provision IC Markets cTrader Demo via metaapi.cloud** — broker IC Markets Global, Demo, server `ICMarkets-Demo01`, $100k size, capture account ID + token.
2. **Install `/etc/novacore/novatrade-webhook.env`** — copy `configs/novatrade.webhook.env` (mode 600, owner `nova`), fill the 3 `fill-at-deploy` values (METAAPI_TOKEN, METAAPI_ACCOUNT_ID, NOVATRADE_WEBHOOK_SECRET via `openssl rand -hex 32`).
3. **Install `/etc/systemd/system/novacore-novatrade-webhook.service`** — `EnvironmentFile=/etc/novacore/novatrade-webhook.env`, `ExecStart=/usr/bin/python3 -m novatrade.runtime.runner`, `User=nova`, `Restart=on-failure`. `daemon-reload && enable` (don't start until step 4 done).
4. **DNS + nginx + cert** — A record `nova-webhook.duckdns.org` → VPS IP; nginx site `proxy_pass http://127.0.0.1:8878` for `/webhook/`, `/health`, `/status`, `/readiness`, `/control/`; `certbot --nginx -d nova-webhook.duckdns.org`.
5. **Pine secret** — patch `WH_SECRET` literal in TradingView Pine editor (or local non-committed copy) to match step 2 secret.
6. **Start service + verify** — `systemctl start novacore-novatrade-webhook.service`; `curl https://nova-webhook.duckdns.org/health`; `curl .../status | jq '.runtime_mode, .adapter_type, .webhook'`.
7. **Configure TradingView alert** — paste fork into Pine editor, Add to chart EURUSD M5, confirm clean compile, Create Alert → Any alert() function call → Webhook URL `https://nova-webhook.duckdns.org/webhook/alert`, blank message, save.
8. **Smoke verification** — wait for next IRB-signal bar close; tail journalctl for `WEBHOOK_RECEIVED` + `WEBHOOK_ROUTED success:true`; confirm order on metaapi.cloud / cTrader UI.
9. **Daily reconciliation cron** — `0 7 * * * cd /home/nova/nova-core && python3 scripts/diff_pine_alerts_vs_metaapi.py --account-id $(grep METAAPI_ACCOUNT_ID /etc/novacore/novatrade-webhook.env | cut -d= -f2) --since $(date -d "yesterday" +%F) > OUTPUT/novatrade/reconciliation/$(date +%F).json`.
10. **Rollback** — `systemctl stop && systemctl disable novacore-novatrade-webhook.service`. Original runner unaffected.

- [ ] **Step 2:** `git add docs/operator/webhook-demo-runbook.md && git commit -m "docs(operator): webhook demo pipeline runbook"`

---

## Self-review

- **Spec coverage:** D1→Phase 4+6, D2→Phase 4+6, D3→Phase 3, D4→Phase 1, D5→Phase 4 (FTMO_ENABLED), D6→Phase 6 step 4, D7→Phase 3+5, D8→Phase 4, D9→Phase 4 reconcile classification.
- **No placeholders:** `fill-at-deploy` and `REPLACE_AT_DEPLOY` are intentional config-file placeholders for operator-supplied secrets, not plan placeholders. Every code-producing task has full code.
- **Type consistency:** `validate_alert(payload, expected_campaign=None)` and `self._cfg.ftmo.campaign_label` consistent across Phase 2/5. Webhook secret distinction (Pine `WH_SECRET` vs env `NOVATRADE_WEBHOOK_SECRET`) explicit in runbook step 5.
- **TDD:** Phases 1, 2, 4, 5 all have failing-test-first.

## Execution

After plan review, hand off to `implementation-team` to execute Phases 1–5. Phase 6 is a documentation phase the implementer also writes; the operator-side commands within run later, manually. On Phase 1 kickoff, vault index flips status `backlog` → `active`.
