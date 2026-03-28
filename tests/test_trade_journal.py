"""Tests for trade journal logging (v87 P2.4)."""

import json

import pytest

from novatrade.monitor.trade_journal import (
    get_journal_stats,
    log_trade_close,
    log_trade_open,
    log_trade_reject,
)


@pytest.fixture(autouse=True)
def _isolate_journal(tmp_path, monkeypatch):
    """Redirect journal file to tmp."""
    journal_file = tmp_path / "trade_journal.jsonl"
    monkeypatch.setattr("novatrade.monitor.trade_journal.JOURNAL_DIR", tmp_path)
    monkeypatch.setattr("novatrade.monitor.trade_journal.JOURNAL_FILE", journal_file)


class TestTradeJournalWrite:
    def test_log_open(self, tmp_path):
        log_trade_open(
            position_id="pos_123",
            symbol="EURUSD",
            side="BUY",
            volume=0.01,
            entry_price=1.08500,
            stop_loss=1.08200,
        )
        journal_file = tmp_path / "trade_journal.jsonl"
        assert journal_file.exists()
        line = journal_file.read_text().strip()
        entry = json.loads(line)
        assert entry["event"] == "OPEN"
        assert entry["position_id"] == "pos_123"
        assert entry["symbol"] == "EURUSD"
        assert entry["side"] == "BUY"
        assert entry["volume"] == 0.01
        assert "logged_at" in entry

    def test_log_close(self, tmp_path):
        log_trade_close(
            position_id="pos_123",
            symbol="EURUSD",
            side="BUY",
            volume=0.01,
            pnl_usd=15.50,
            exit_reason="TIME_STOP",
        )
        journal_file = tmp_path / "trade_journal.jsonl"
        entry = json.loads(journal_file.read_text().strip())
        assert entry["event"] == "CLOSE"
        assert entry["pnl_usd"] == 15.50
        assert entry["exit_reason"] == "TIME_STOP"

    def test_log_reject(self, tmp_path):
        log_trade_reject(
            symbol="EURUSD",
            side="BUY",
            reason="SL distance too small",
            gate="sl_distance",
        )
        journal_file = tmp_path / "trade_journal.jsonl"
        entry = json.loads(journal_file.read_text().strip())
        assert entry["event"] == "REJECT"
        assert entry["gate"] == "sl_distance"

    def test_multiple_entries(self, tmp_path):
        log_trade_open(position_id="p1", symbol="EURUSD", side="BUY", volume=0.01, entry_price=1.08, stop_loss=1.07)
        log_trade_close(position_id="p1", symbol="EURUSD", side="BUY", volume=0.01, pnl_usd=10.0)
        log_trade_reject(symbol="GBPUSD", side="SELL", reason="cooldown")
        journal_file = tmp_path / "trade_journal.jsonl"
        lines = journal_file.read_text().strip().split("\n")
        assert len(lines) == 3


class TestJournalStats:
    def test_empty_stats(self):
        stats = get_journal_stats()
        assert stats == {"total": 0, "opens": 0, "closes": 0, "rejects": 0}

    def test_stats_count(self, tmp_path):
        log_trade_open(position_id="p1", symbol="EURUSD", side="BUY", volume=0.01, entry_price=1.08, stop_loss=1.07)
        log_trade_open(position_id="p2", symbol="EURUSD", side="SELL", volume=0.01, entry_price=1.09, stop_loss=1.10)
        log_trade_close(position_id="p1", symbol="EURUSD", side="BUY", volume=0.01, pnl_usd=10.0)
        log_trade_reject(symbol="EURUSD", side="BUY", reason="cooldown")
        stats = get_journal_stats()
        assert stats["total"] == 4
        assert stats["opens"] == 2
        assert stats["closes"] == 1
        assert stats["rejects"] == 1
