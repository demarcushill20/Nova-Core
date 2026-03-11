"""Tests for utils.workflow_status.is_terminal_status."""

import pytest

from utils.workflow_status import TERMINAL_STATUSES, is_terminal_status


@pytest.mark.parametrize("status", ["completed", "failed", "rejected", "halted"])
def test_terminal_statuses_return_true(status):
    assert is_terminal_status(status) is True


@pytest.mark.parametrize("status", ["created", "planning", "executing", "idle", "pending"])
def test_non_terminal_statuses_return_false(status):
    assert is_terminal_status(status) is False


def test_empty_string_is_not_terminal():
    assert is_terminal_status("") is False


def test_terminal_set_is_frozenset():
    assert isinstance(TERMINAL_STATUSES, frozenset)


def test_terminal_set_has_exactly_four_members():
    assert len(TERMINAL_STATUSES) == 4
