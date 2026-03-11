"""Tests for utils.extract_task_class."""

import pytest

from utils.extract_task_class import KNOWN_TASK_CLASSES, extract_task_class

# --- Happy path ---

@pytest.mark.parametrize("cls", sorted(KNOWN_TASK_CLASSES))
def test_known_classes(cls: str) -> None:
    assert extract_task_class({"task_class": cls}) == cls


def test_unknown_string_still_returned() -> None:
    """Arbitrary strings are returned — caller decides validity."""
    assert extract_task_class({"task_class": "custom_class"}) == "custom_class"


def test_strips_whitespace() -> None:
    assert extract_task_class({"task_class": "  research  "}) == "research"


# --- Invalid shapes → None ---

@pytest.mark.parametrize("bad_record", [
    None,
    42,
    "not a dict",
    [],
    [{"task_class": "research"}],
    True,
])
def test_non_dict_returns_none(bad_record: object) -> None:
    assert extract_task_class(bad_record) is None


def test_missing_key() -> None:
    assert extract_task_class({"status": "completed"}) is None


@pytest.mark.parametrize("bad_val", [0, 123, True, False, [], {}, None])
def test_non_string_value_returns_none(bad_val: object) -> None:
    assert extract_task_class({"task_class": bad_val}) is None


def test_empty_string() -> None:
    assert extract_task_class({"task_class": ""}) is None


def test_whitespace_only() -> None:
    assert extract_task_class({"task_class": "   "}) is None


# --- Extra keys ignored ---

def test_extra_keys_ignored() -> None:
    record = {"task_class": "code_impl", "status": "completed", "extra": 99}
    assert extract_task_class(record) == "code_impl"
