"""Tests for tools.adapters.system_service — parse_status_output + service_status.

These tests mock subprocess output so they run without systemd present.
"""

from unittest.mock import patch

from tools.adapters.system_service import parse_status_output, service_status


# --- Realistic systemctl status output samples --------------------------------

ACTIVE_OUTPUT = """\
● novacore-watcher.service - NovaCore Watcher
     Loaded: loaded (/etc/systemd/system/novacore-watcher.service; enabled; vendor preset: enabled)
     Active: active (running) since Mon 2026-03-02 09:54:48 UTC; 5h 12min ago
   Main PID: 184355 (python3)
      Tasks: 2 (limit: 4556)
     Memory: 42.3M
        CPU: 1min 23.456s
     CGroup: /system.slice/novacore-watcher.service
             └─184355 /home/nova/nova-core/.venv/bin/python3 watcher.py
Mar 02 14:50:01 nova python3[184355]: [watcher] Polling TASKS/ ...
Mar 02 14:51:01 nova python3[184355]: [watcher] No pending tasks.
"""

INACTIVE_OUTPUT = """\
● novacore-telegram.service - NovaCore Telegram Bot
     Loaded: loaded (/etc/systemd/system/novacore-telegram.service; enabled; vendor preset: enabled)
     Active: inactive (dead) since Mon 2026-03-02 12:00:00 UTC; 3h ago
   Main PID: 150200 (code=exited, status=0/SUCCESS)
Mar 02 12:00:00 nova systemd[1]: Stopped NovaCore Telegram Bot.
"""

FAILED_OUTPUT = """\
● novacore-notifier.service - NovaCore Telegram Notifier
     Loaded: loaded (/etc/systemd/system/novacore-notifier.service; enabled; vendor preset: enabled)
     Active: failed (Result: exit-code) since Mon 2026-03-02 11:30:00 UTC; 4h ago
    Process: 160000 ExecStart=/home/nova/nova-core/.venv/bin/python3 telegram_notifier.py (code=exited, status=1/FAILURE)
   Main PID: 160000 (code=exited, status=1/FAILURE)
Mar 02 11:30:00 nova python3[160000]: Traceback (most recent call last):
Mar 02 11:30:00 nova python3[160000]:   File "telegram_notifier.py", line 1, in <module>
Mar 02 11:30:00 nova python3[160000]: ImportError: No module named 'httpx'
"""

MINIMAL_OUTPUT = """\
● unknown.service
     Loaded: not-found (Reason: No such file or directory)
     Active: inactive (dead)
"""


# --- Tests for parse_status_output -------------------------------------------


def test_parse_active_service():
    result = parse_status_output(ACTIVE_OUTPUT)
    assert result["active_state"] == "active"
    assert result["sub_state"] == "running"
    assert result["main_pid"] == 184355
    assert "loaded" in result["loaded"]
    assert "active (running)" in result["active_summary"]
    assert result["raw_excerpt"]  # non-empty


def test_parse_inactive_service():
    result = parse_status_output(INACTIVE_OUTPUT)
    assert result["active_state"] == "inactive"
    assert result["sub_state"] == "dead"
    assert result["main_pid"] == 150200


def test_parse_failed_service():
    result = parse_status_output(FAILED_OUTPUT)
    assert result["active_state"] == "failed"
    assert result["sub_state"] == "Result: exit-code"
    assert "exit-code" in result["active_summary"]
    assert result["main_pid"] == 160000


def test_parse_minimal_output():
    result = parse_status_output(MINIMAL_OUTPUT)
    assert result["active_state"] == "inactive"
    assert result["sub_state"] == "dead"
    assert result["main_pid"] is None
    assert "not-found" in result["loaded"]


def test_parse_empty_output():
    result = parse_status_output("")
    assert result["active_state"] == ""
    assert result["sub_state"] == ""
    assert result["main_pid"] is None
    assert result["loaded"] == ""
    assert result["raw_excerpt"] == ""


# --- Tests for service_status (with mocked subprocess) -----------------------


@patch("tools.adapters.system_service.run_subprocess")
def test_service_status_active(mock_run):
    mock_run.return_value = {
        "exit_code": 0,
        "stdout": ACTIVE_OUTPUT,
        "stderr": "",
    }
    result = service_status("novacore-watcher")
    assert result["ok"] is True
    assert result["service"] == "novacore-watcher"
    assert result["active_state"] == "active"
    assert result["main_pid"] == 184355

    # Verify correct command was called
    call_args = mock_run.call_args
    assert call_args[0][0] == ["systemctl", "status", "novacore-watcher", "--no-pager", "-l"]


@patch("tools.adapters.system_service.run_subprocess")
def test_service_status_inactive_exit3(mock_run):
    mock_run.return_value = {
        "exit_code": 3,
        "stdout": INACTIVE_OUTPUT,
        "stderr": "",
    }
    result = service_status("novacore-telegram")
    assert result["ok"] is True  # exit 3 = inactive, not an error
    assert result["active_state"] == "inactive"


@patch("tools.adapters.system_service.run_subprocess")
def test_service_status_not_found(mock_run):
    mock_run.return_value = {
        "exit_code": 4,
        "stdout": MINIMAL_OUTPUT,
        "stderr": "Unit unknown.service could not be found.",
    }
    result = service_status("unknown")
    assert result["ok"] is False  # exit 4 = unit not found


def test_service_status_invalid_name():
    try:
        service_status("foo; rm -rf /")
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "Invalid service name" in str(e)


def test_service_status_empty_name():
    try:
        service_status("")
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "requires 'name'" in str(e)


# --- Run as script -----------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_parse_active_service,
        test_parse_inactive_service,
        test_parse_failed_service,
        test_parse_minimal_output,
        test_parse_empty_output,
        test_service_status_active,
        test_service_status_inactive_exit3,
        test_service_status_not_found,
        test_service_status_invalid_name,
        test_service_status_empty_name,
    ]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} tests passed")
