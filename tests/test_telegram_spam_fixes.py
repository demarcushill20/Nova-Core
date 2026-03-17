"""Tests for Telegram spam prevention fixes.

Covers:
  1. Cooldown/dedupe gate suppresses repeated identical messages
  2. Materially different messages pass through cooldown
  3. Cost alerts get longer cooldown (4 hours)
  4. Grounding filter suppresses false service-down alerts
  5. Grounding filter allows genuine service-down alerts
  6. _handle_agent_actions integrates cooldown + grounding
"""

import json

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_cooldown_file(tmp_path, monkeypatch):
    """Point TELEGRAM_COOLDOWN_FILE to tmp_path."""
    import heartbeat

    cooldown_file = tmp_path / "telegram_cooldown.json"
    monkeypatch.setattr(heartbeat, "TELEGRAM_COOLDOWN_FILE", cooldown_file)
    return cooldown_file


# ---------------------------------------------------------------------------
# 1. _normalize_fingerprint stability
# ---------------------------------------------------------------------------


class TestNormalizeFingerprint:
    def test_same_message_same_fingerprint(self):
        from heartbeat import _normalize_fingerprint

        fp1 = _normalize_fingerprint("Cost alert: daily spend $22.50 exceeds $20 threshold")
        fp2 = _normalize_fingerprint("Cost alert: daily spend $22.50 exceeds $20 threshold")
        assert fp1 == fp2

    def test_different_dollar_cents_same_fingerprint(self):
        """$22.50 and $22.99 should fingerprint the same (same integer part)."""
        from heartbeat import _normalize_fingerprint

        fp1 = _normalize_fingerprint("Cost alert: daily spend $22.50 exceeds $20 threshold")
        fp2 = _normalize_fingerprint("Cost alert: daily spend $22.99 exceeds $20 threshold")
        assert fp1 == fp2

    def test_different_dollar_integer_different_fingerprint(self):
        """$22 and $35 should fingerprint differently."""
        from heartbeat import _normalize_fingerprint

        fp1 = _normalize_fingerprint("Cost alert: daily spend $22.50 exceeds threshold")
        fp2 = _normalize_fingerprint("Cost alert: daily spend $35.50 exceeds threshold")
        assert fp1 != fp2

    def test_timestamp_stripped(self):
        """Different timestamps should produce same fingerprint."""
        from heartbeat import _normalize_fingerprint

        fp1 = _normalize_fingerprint("Alert at 08:41 UTC — services healthy")
        fp2 = _normalize_fingerprint("Alert at 14:22 UTC — services healthy")
        assert fp1 == fp2

    def test_materially_different_messages(self):
        from heartbeat import _normalize_fingerprint

        fp1 = _normalize_fingerprint("3 services are DOWN")
        fp2 = _normalize_fingerprint("Queue is empty, seeding research task")
        assert fp1 != fp2


# ---------------------------------------------------------------------------
# 2. _telegram_cooldown_gate
# ---------------------------------------------------------------------------


class TestTelegramCooldownGate:
    def test_first_message_allowed(self, tmp_path, monkeypatch):
        _patch_cooldown_file(tmp_path, monkeypatch)
        from heartbeat import _telegram_cooldown_gate

        assert _telegram_cooldown_gate("Hello world", cooldown_secs=1800) is True

    def test_repeated_message_suppressed(self, tmp_path, monkeypatch):
        _patch_cooldown_file(tmp_path, monkeypatch)
        from heartbeat import _telegram_cooldown_gate

        assert _telegram_cooldown_gate("Same alert text", cooldown_secs=1800) is True
        assert _telegram_cooldown_gate("Same alert text", cooldown_secs=1800) is False

    def test_different_message_allowed(self, tmp_path, monkeypatch):
        _patch_cooldown_file(tmp_path, monkeypatch)
        from heartbeat import _telegram_cooldown_gate

        assert _telegram_cooldown_gate("Alert A", cooldown_secs=1800) is True
        assert _telegram_cooldown_gate("Alert B", cooldown_secs=1800) is True

    def test_expired_cooldown_allows_resend(self, tmp_path, monkeypatch):
        cooldown_file = _patch_cooldown_file(tmp_path, monkeypatch)
        from heartbeat import _normalize_fingerprint, _telegram_cooldown_gate

        # First send
        assert _telegram_cooldown_gate("Expired test", cooldown_secs=1800) is True

        # Manually backdate the cooldown entry
        fp = _normalize_fingerprint("Expired test")
        data = json.loads(cooldown_file.read_text())
        data[fp] = data[fp] - 2000  # 2000 seconds ago, exceeds 1800s cooldown
        cooldown_file.write_text(json.dumps(data))

        # Should now be allowed
        assert _telegram_cooldown_gate("Expired test", cooldown_secs=1800) is True

    def test_cost_alert_gets_longer_cooldown(self, tmp_path, monkeypatch):
        """Cost-related messages should auto-detect 4-hour cooldown."""
        _patch_cooldown_file(tmp_path, monkeypatch)
        from heartbeat import _telegram_cooldown_gate

        # First send allowed
        assert _telegram_cooldown_gate("Cost alert: daily spend exceeds budget") is True
        # Second send suppressed (auto-detected as cost alert)
        assert _telegram_cooldown_gate("Cost alert: daily spend exceeds budget") is False

    def test_persists_across_calls(self, tmp_path, monkeypatch):
        """Cooldown state persists to disk."""
        cooldown_file = _patch_cooldown_file(tmp_path, monkeypatch)
        from heartbeat import _telegram_cooldown_gate

        _telegram_cooldown_gate("Persist test", cooldown_secs=1800)
        assert cooldown_file.exists()
        data = json.loads(cooldown_file.read_text())
        assert len(data) == 1


# ---------------------------------------------------------------------------
# 3. _ground_service_alert
# ---------------------------------------------------------------------------


class TestGroundServiceAlert:
    def _healthy_checks(self):
        return [
            {"name": "service:novacore-watcher", "ok": True, "detail": "active"},
            {"name": "service:novacore-telegram", "ok": True, "detail": "active"},
            {"name": "service:novacore-telegram-notifier", "ok": True, "detail": "active"},
            {"name": "disk", "ok": True, "detail": "45% used"},
        ]

    def _unhealthy_checks(self):
        checks = self._healthy_checks()
        checks[0] = {"name": "service:novacore-watcher", "ok": False, "detail": "inactive"}
        return checks

    def test_suppresses_false_service_down(self):
        from heartbeat import _ground_service_alert

        # LLM says services are down, but checks say healthy
        result = _ground_service_alert("3 services are DOWN and need restart", self._healthy_checks())
        assert result is False

    def test_allows_genuine_service_down(self):
        from heartbeat import _ground_service_alert

        # LLM says service is down, and it actually is
        result = _ground_service_alert("novacore-watcher service is down", self._unhealthy_checks())
        assert result is True

    def test_allows_non_service_messages(self):
        from heartbeat import _ground_service_alert

        # Message about something else entirely
        result = _ground_service_alert("Queue is empty, seeding research task", self._healthy_checks())
        assert result is True

    def test_allows_when_no_checks(self):
        from heartbeat import _ground_service_alert

        # No check data available — allow by default
        result = _ground_service_alert("services are down and dead", None)
        assert result is True

    def test_allows_cost_alert_with_healthy_services(self):
        from heartbeat import _ground_service_alert

        # Cost alerts should pass through even with all healthy services
        result = _ground_service_alert("Cost alert: daily spend $22.50 exceeds $20 threshold", self._healthy_checks())
        assert result is True

    def test_suppresses_dead_claim_with_all_healthy(self):
        from heartbeat import _ground_service_alert

        result = _ground_service_alert(
            "3 services showing DEAD status. Investigate and restart services.", self._healthy_checks()
        )
        assert result is False


# ---------------------------------------------------------------------------
# 4. _handle_agent_actions integration
# ---------------------------------------------------------------------------


class TestHandleAgentActionsSpamControl:
    def test_cooldown_suppresses_repeated_notify(self, tmp_path, monkeypatch):
        """Same agent notify action sent twice — second is suppressed."""
        _patch_cooldown_file(tmp_path, monkeypatch)
        import heartbeat

        sent = []
        monkeypatch.setattr(heartbeat, "_send_telegram", lambda msg: sent.append(msg))

        response = json.dumps([{"type": "notify", "message": "Test alert repeated"}])
        checks = [{"name": "disk", "ok": True, "detail": "ok"}]

        heartbeat._handle_agent_actions(response, checks=checks)
        heartbeat._handle_agent_actions(response, checks=checks)

        assert len(sent) == 1  # second call suppressed

    def test_different_notify_messages_both_sent(self, tmp_path, monkeypatch):
        """Materially different agent messages should both send."""
        _patch_cooldown_file(tmp_path, monkeypatch)
        import heartbeat

        sent = []
        monkeypatch.setattr(heartbeat, "_send_telegram", lambda msg: sent.append(msg))

        checks = [{"name": "disk", "ok": True, "detail": "ok"}]

        resp1 = json.dumps([{"type": "notify", "message": "Alert type A"}])
        resp2 = json.dumps([{"type": "notify", "message": "Alert type B"}])

        heartbeat._handle_agent_actions(resp1, checks=checks)
        heartbeat._handle_agent_actions(resp2, checks=checks)

        assert len(sent) == 2

    def test_false_service_down_suppressed_in_handler(self, tmp_path, monkeypatch):
        """Agent claims services down but checks are healthy — suppressed."""
        _patch_cooldown_file(tmp_path, monkeypatch)
        import heartbeat

        sent = []
        monkeypatch.setattr(heartbeat, "_send_telegram", lambda msg: sent.append(msg))

        healthy_checks = [
            {"name": "service:novacore-watcher", "ok": True, "detail": "active"},
            {"name": "service:novacore-telegram", "ok": True, "detail": "active"},
        ]

        response = json.dumps([{"type": "notify", "message": "3 services are down and dead. Restart immediately."}])
        heartbeat._handle_agent_actions(response, checks=healthy_checks)

        assert len(sent) == 0  # grounding filter blocks it

    def test_fallback_path_cooldown(self, tmp_path, monkeypatch):
        """Non-JSON agent response uses fallback path — also cooldown-gated."""
        _patch_cooldown_file(tmp_path, monkeypatch)
        import heartbeat

        sent = []
        monkeypatch.setattr(heartbeat, "_send_telegram", lambda msg: sent.append(msg))

        checks = [{"name": "disk", "ok": True, "detail": "ok"}]

        # Non-JSON response triggers fallback
        heartbeat._handle_agent_actions("plain text alert about something", checks=checks)
        heartbeat._handle_agent_actions("plain text alert about something", checks=checks)

        assert len(sent) == 1  # second suppressed by cooldown
