"""Tests for trade journal logging (v87 P2.4) + rotation."""

import json

import pytest

from novatrade.monitor.trade_journal import (
    _count_lines,
    _rotate_if_needed,
    get_journal_stats,
    log_trade_close,
    log_trade_open,
    log_trade_reject,
    rotate_now,
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


class TestJournalRotation:
    def _write_n_lines(self, tmp_path, n):
        """Write n dummy JSONL lines to the journal."""
        journal_file = tmp_path / "trade_journal.jsonl"
        with open(journal_file, "w") as f:
            for i in range(n):
                f.write(json.dumps({"event": "REJECT", "i": i, "logged_at": f"2026-01-01T00:00:{i:02d}"}) + "\n")
        return journal_file

    def test_count_lines(self, tmp_path):
        jf = self._write_n_lines(tmp_path, 50)
        assert _count_lines(jf) == 50

    def test_count_lines_missing_file(self, tmp_path):
        assert _count_lines(tmp_path / "nonexistent.jsonl") == 0

    def test_rotate_if_needed_under_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr("novatrade.monitor.trade_journal.MAX_JOURNAL_LINES", 100)
        self._write_n_lines(tmp_path, 50)
        _rotate_if_needed()
        # No rotation should occur — still 50 lines
        assert _count_lines(tmp_path / "trade_journal.jsonl") == 50
        archives = list(tmp_path.glob("trade_journal.*.jsonl"))
        assert len(archives) == 0

    def test_rotate_if_needed_over_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr("novatrade.monitor.trade_journal.MAX_JOURNAL_LINES", 100)
        monkeypatch.setattr("novatrade.monitor.trade_journal.KEEP_AFTER_ROTATE", 20)
        self._write_n_lines(tmp_path, 150)
        _rotate_if_needed()
        # Active file should have KEEP_AFTER_ROTATE lines
        assert _count_lines(tmp_path / "trade_journal.jsonl") == 20
        # Archive should exist with all 150 original lines
        archives = list(tmp_path.glob("trade_journal.*.jsonl"))
        assert len(archives) == 1
        assert _count_lines(archives[0]) == 150

    def test_rotate_keeps_newest_entries(self, tmp_path, monkeypatch):
        monkeypatch.setattr("novatrade.monitor.trade_journal.MAX_JOURNAL_LINES", 100)
        monkeypatch.setattr("novatrade.monitor.trade_journal.KEEP_AFTER_ROTATE", 10)
        self._write_n_lines(tmp_path, 150)
        _rotate_if_needed()
        # The kept entries should be the last 10 (i=140..149)
        with open(tmp_path / "trade_journal.jsonl") as f:
            entries = [json.loads(line) for line in f]
        assert len(entries) == 10
        assert entries[0]["i"] == 140
        assert entries[-1]["i"] == 149

    def test_rotate_now_forces_rotation(self, tmp_path, monkeypatch):
        """rotate_now() should work even when under MAX_JOURNAL_LINES."""
        monkeypatch.setattr("novatrade.monitor.trade_journal.MAX_JOURNAL_LINES", 10_000)
        monkeypatch.setattr("novatrade.monitor.trade_journal.KEEP_AFTER_ROTATE", 5)
        self._write_n_lines(tmp_path, 50)
        result = rotate_now()
        assert result["archived_lines"] == 50
        assert result["kept_lines"] == 5
        assert result["archive_path"] is not None
        assert _count_lines(tmp_path / "trade_journal.jsonl") == 5

    def test_rotate_now_empty_journal(self, tmp_path):
        result = rotate_now()
        assert result["archived_lines"] == 0

    def test_auto_rotation_via_append(self, tmp_path, monkeypatch):
        """Rotation triggers automatically after _ROTATE_CHECK_INTERVAL appends."""
        monkeypatch.setattr("novatrade.monitor.trade_journal.MAX_JOURNAL_LINES", 50)
        monkeypatch.setattr("novatrade.monitor.trade_journal.KEEP_AFTER_ROTATE", 10)
        monkeypatch.setattr("novatrade.monitor.trade_journal._ROTATE_CHECK_INTERVAL", 5)
        monkeypatch.setattr("novatrade.monitor.trade_journal._append_counter", 0)
        # Pre-populate with 60 lines (over limit)
        self._write_n_lines(tmp_path, 60)
        # Now append 5 more to trigger the check interval
        for _ in range(5):
            log_trade_reject(symbol="EURUSD", side="BUY", reason="test")
        # Rotation should have fired — active file should be small
        line_count = _count_lines(tmp_path / "trade_journal.jsonl")
        assert line_count <= 15  # 10 kept + 5 new appends at most
