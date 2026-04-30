"""E2E test for the TV webhook -> TradingAgent demo pipeline.

Spins up the FastAPI app from `webhook_server.create_app()` with a mocked
MT5 adapter, POSTs Pine-shaped alert payloads, and asserts:
  - 200 with ok=true on a valid signal alert
  - 403 on a bad webhook secret
  - 200 with ok=false on a campaign mismatch (rejected_reason set)
  - place_order is invoked exactly once for a valid signal
  - Idempotency: a duplicate POST does not double-execute
  - WEBHOOK_RECEIVED evidence carries the enriched payload fields
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from novatrade.config import FtmoProfile, NovaTradeCfg, RiskConfig
from novatrade.execution.trading_agent import TradingAgent
from novatrade.models import AccountMode, AccountState, OrderResult, OrderStatus, SymbolPrice
from novatrade.risk.risk_engine import RiskEngine
from novatrade.runtime.webhook_server import WebhookState, create_app
from novatrade.validation.evidence import EvidenceRecorder

WH_SECRET_VAL = "test-wh-2026"
CAMPAIGN = "ic-markets-demo-2026-q2"


def _signal(*, campaign: str, secret: str) -> dict:
    return {
        "webhook_secret": secret,
        "strategy_name": "Rob Hoffman IRB",
        "strategy_version": "5.1.0",
        "action": "PLACE_STOP_ORDER",
        "signal_type": "LONG_IRB",
        "irb_type": "UPTREND_IRB",
        "symbol": "EURUSD",
        "broker_symbol": "EURUSD",
        "timeframe": "M5",
        "side": "BUY",
        "order_type": "BUY_STOP",
        "entry_price": 1.0950,
        "stop_loss": 1.0900,
        "stop_distance_pips": 50.0,
        "volume": 0.10,
        "risk_dollars": 100.0,
        "bar_close_time": 1700000000000,
        "campaign": campaign,
    }


@pytest.fixture
def mock_adapter() -> MagicMock:
    """Mock MT5Adapter mirroring the test_trading_agent.py shape."""
    adapter = MagicMock()
    adapter._connected = True
    adapter.get_account = AsyncMock(
        return_value=AccountState(balance=100_000.0, equity=100_000.0, mode=AccountMode.DEMO)
    )
    adapter.get_positions = AsyncMock(return_value=[])
    adapter.place_order = AsyncMock(return_value=OrderResult(ok=True, order_id="ORD-001", status=OrderStatus.PENDING))
    adapter.modify_order = AsyncMock(return_value=OrderResult(ok=True, order_id="POS-001", status=OrderStatus.FILLED))
    adapter.cancel_order = AsyncMock(
        return_value=OrderResult(ok=True, order_id="ORD-001", status=OrderStatus.CANCELLED)
    )
    adapter.close_position = AsyncMock(return_value=OrderResult(ok=True, order_id="POS-001", status=OrderStatus.FILLED))
    adapter.get_symbol_price = AsyncMock(
        return_value=SymbolPrice(symbol="EURUSD", bid=1.0950, ask=1.0951, spread=0.0001)
    )
    return adapter


@pytest.fixture
def cfg() -> NovaTradeCfg:
    return NovaTradeCfg(
        mode=AccountMode.DEMO,
        ftmo=FtmoProfile(
            enabled=True,
            symbol_map={"EURUSD": "EURUSD"},
            campaign_label=CAMPAIGN,
        ),
        risk=RiskConfig(max_positions=1),
    )


@pytest.fixture
def state(cfg: NovaTradeCfg, mock_adapter: MagicMock, tmp_path) -> WebhookState:
    risk_engine = RiskEngine(cfg)
    risk_engine.initialize(AccountState(balance=100_000.0, equity=100_000.0, mode=AccountMode.DEMO))
    # EvidenceRecorder stamps its own campaign label onto every record (overrides
    # any campaign in the data dict). For reconciliation traceability we keep
    # the recorder's campaign aligned with the runner's FTMO_CAMPAIGN_LABEL.
    recorder = EvidenceRecorder(path=tmp_path / "evidence.jsonl", campaign=CAMPAIGN)
    agent = TradingAgent(cfg, mock_adapter, risk_engine, recorder)
    return WebhookState(
        agent=agent,
        risk_engine=risk_engine,
        recorder=recorder,
        webhook_secret=WH_SECRET_VAL,
    )


@pytest.fixture
def client(state: WebhookState) -> TestClient:
    return TestClient(create_app(state))


def test_health_endpoint_ok(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert "status" in body
    assert "adapter_connected" in body


def test_bad_secret_403(client: TestClient) -> None:
    r = client.post("/webhook/alert", json=_signal(campaign=CAMPAIGN, secret="wrong"))
    assert r.status_code == 403


def test_campaign_mismatch_rejected(client: TestClient, mock_adapter: MagicMock) -> None:
    r = client.post("/webhook/alert", json=_signal(campaign="other-campaign", secret=WH_SECRET_VAL))
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "campaign" in (body.get("rejected_reason") or "").lower()
    # adapter must not have been called for a rejected payload
    assert mock_adapter.place_order.await_count == 0


def test_valid_signal_places_order(client: TestClient, mock_adapter: MagicMock) -> None:
    r = client.post("/webhook/alert", json=_signal(campaign=CAMPAIGN, secret=WH_SECRET_VAL))
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert mock_adapter.place_order.await_count == 1


def test_idempotent_duplicate_alert_does_not_double_place(client: TestClient, mock_adapter: MagicMock) -> None:
    payload = _signal(campaign=CAMPAIGN, secret=WH_SECRET_VAL)
    client.post("/webhook/alert", json=payload)
    client.post("/webhook/alert", json=payload)
    assert mock_adapter.place_order.await_count == 1


def test_webhook_received_evidence_carries_enriched_fields(client: TestClient, state: WebhookState, tmp_path) -> None:
    """Phase 4 enriched WEBHOOK_RECEIVED with side/entry_price/volume/etc."""
    payload = _signal(campaign=CAMPAIGN, secret=WH_SECRET_VAL)
    client.post("/webhook/alert", json=payload)

    import json

    evidence_path = tmp_path / "evidence.jsonl"
    assert evidence_path.exists(), "EvidenceRecorder did not write the JSONL trail"
    received_records = []
    for line in evidence_path.read_text().splitlines():
        rec = json.loads(line)
        if rec.get("data", {}).get("event") == "WEBHOOK_RECEIVED":
            received_records.append(rec["data"])
    assert received_records, "no WEBHOOK_RECEIVED events recorded"
    rec = received_records[0]
    assert rec["side"] == "BUY"
    assert rec["entry_price"] == 1.0950
    assert rec["volume"] == 0.10
    assert rec["bar_close_time"] == 1700000000000
    assert rec["campaign"] == CAMPAIGN
    assert rec["strategy_version"] == "5.1.0"
    assert "webhook_secret" not in rec, "webhook_secret must not be in evidence"
