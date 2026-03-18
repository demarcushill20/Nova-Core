"""Tests for utils/error_catalog.py — Phase 6B Step 6.9 Error Catalog."""

import re

import pytest

from utils.error_catalog import (
    _CATALOG,
    ErrorEntry,
    catalog_classify_error,
    classify_from_catalog,
    get_catalog,
    get_retry_config,
    register_entry,
    reset_catalog,
)
from utils.self_healing import ErrorCategory

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_catalog():
    """Reset catalog before and after every test."""
    reset_catalog()
    yield
    reset_catalog()


# ---------------------------------------------------------------------------
# MCP errors
# ---------------------------------------------------------------------------


class TestMCPErrors:
    def test_mcp_connection_refused(self):
        entry = classify_from_catalog("MCP server connection refused on port 3000")
        assert entry is not None
        assert entry.code == "E-MCP-001"
        assert entry.category == "transient"
        assert entry.max_retries == 3

    def test_mcp_timeout(self):
        entry = classify_from_catalog("MCP request timeout after 30s")
        assert entry is not None
        assert entry.code == "E-MCP-002"
        assert entry.recommended_action == "retry"


# ---------------------------------------------------------------------------
# Claude API errors
# ---------------------------------------------------------------------------


class TestClaudeErrors:
    def test_claude_rate_limit(self):
        entry = classify_from_catalog("Anthropic API rate_limit exceeded (429)")
        assert entry is not None
        assert entry.code == "E-CLAUDE-001"
        assert entry.category == "rate_limit"
        assert entry.backoff_base == 60.0

    def test_claude_auth_error(self):
        entry = classify_from_catalog("Anthropic API authentication failed: 401 Unauthorized")
        assert entry is not None
        assert entry.code == "E-CLAUDE-002"
        assert entry.category == "auth"
        assert entry.max_retries == 0
        assert entry.recommended_action == "alert"


# ---------------------------------------------------------------------------
# Infrastructure errors (Redis, Pinecone, Neo4j)
# ---------------------------------------------------------------------------


class TestInfraErrors:
    def test_redis_connection_refused(self):
        entry = classify_from_catalog("redis.exceptions.ConnectionError: connection refused")
        assert entry is not None
        assert entry.code == "E-REDIS-001"
        assert entry.max_retries == 3

    def test_pinecone_timeout(self):
        entry = classify_from_catalog("pinecone.core.client.exceptions.PineconeTimeout: timeout")
        assert entry is not None
        assert entry.code == "E-PINECONE-001"
        assert entry.max_retries == 2

    def test_neo4j_connection_error(self):
        entry = classify_from_catalog("neo4j.exceptions.ServiceUnavailable: connection refused")
        assert entry is not None
        assert entry.code == "E-NEO4J-001"
        assert entry.max_retries == 3


# ---------------------------------------------------------------------------
# MetaApi errors
# ---------------------------------------------------------------------------


class TestMetaApiErrors:
    def test_metaapi_timeout(self):
        entry = classify_from_catalog("MetaApi connection timeout after 60s")
        assert entry is not None
        assert entry.code == "E-METAAPI-001"
        assert entry.backoff_base == 30.0

    def test_metaapi_service_unavailable(self):
        entry = classify_from_catalog("MetaApi service unavailable 503")
        assert entry is not None
        assert entry.code == "E-METAAPI-001"


# ---------------------------------------------------------------------------
# Task / file errors
# ---------------------------------------------------------------------------


class TestTaskErrors:
    def test_task_file_not_found(self):
        entry = classify_from_catalog("FileNotFoundError: [Errno 2] No such file: 'TASKS/foo.md'")
        assert entry is not None
        assert entry.code == "E-TASK-001"
        assert entry.category == "permanent"
        assert entry.recommended_action == "skip"
        assert entry.max_retries == 0


# ---------------------------------------------------------------------------
# Resource errors
# ---------------------------------------------------------------------------


class TestResourceErrors:
    def test_memory_error(self):
        entry = classify_from_catalog("MemoryError: unable to allocate 4GB array")
        assert entry is not None
        assert entry.code == "E-RESOURCE-001"
        assert entry.category == "resource"
        assert entry.recommended_action == "degrade"

    def test_resource_warning(self):
        entry = classify_from_catalog("ResourceWarning: unclosed connection")
        assert entry is not None
        assert entry.code == "E-RESOURCE-001"


# ---------------------------------------------------------------------------
# JSON / LLM errors
# ---------------------------------------------------------------------------


class TestLLMErrors:
    def test_json_decode_llm_output(self):
        entry = classify_from_catalog("json.JSONDecodeError: Expecting value in LLM response output")
        assert entry is not None
        assert entry.code == "E-LLM-001"
        assert entry.category == "transient"
        assert entry.recommended_action == "retry"

    def test_json_decode_generic(self):
        entry = classify_from_catalog("JSONDecodeError: Expecting value: line 1 column 1")
        assert entry is not None
        assert entry.code in ("E-LLM-001", "E-LLM-002")
        assert entry.max_retries == 2


# ---------------------------------------------------------------------------
# Unknown / no match
# ---------------------------------------------------------------------------


class TestUnknownErrors:
    def test_unknown_error_returns_none(self):
        entry = classify_from_catalog("something completely unexpected happened 🤷")
        assert entry is None

    def test_unknown_error_via_catalog_classify(self):
        result = catalog_classify_error("something completely unexpected happened")
        # Falls through to self_healing.classify_error
        assert result is not None
        assert result.max_retries >= 0  # conservative but valid


# ---------------------------------------------------------------------------
# get_retry_config
# ---------------------------------------------------------------------------


class TestGetRetryConfig:
    def test_retry_config_for_transient(self):
        entry = classify_from_catalog("MCP server connection refused")
        config = get_retry_config(entry)
        assert config["max_retries"] == 3
        assert config["backoff_base"] == 5.0
        assert config["backoff_max"] >= 300.0

    def test_retry_config_for_permanent(self):
        entry = classify_from_catalog("FileNotFoundError: [Errno 2] 'TASKS/missing.md'")
        config = get_retry_config(entry)
        assert config["max_retries"] == 0
        assert config["backoff_base"] == 0.0

    def test_retry_config_for_none(self):
        config = get_retry_config(None)
        assert config["max_retries"] == 1
        assert config["backoff_base"] == 10.0
        assert config["backoff_max"] >= 10.0

    def test_retry_config_keys(self):
        config = get_retry_config(None)
        assert set(config.keys()) == {"max_retries", "backoff_base", "backoff_max"}


# ---------------------------------------------------------------------------
# Integration with self_healing.classify_error
# ---------------------------------------------------------------------------


class TestSelfHealingIntegration:
    def test_catalog_classify_maps_to_classified_error(self):
        result = catalog_classify_error("Anthropic API rate_limit exceeded (429)")
        assert result.category == ErrorCategory.RESOURCE  # rate_limit -> RESOURCE
        assert result.should_retry is True
        assert result.max_retries == 3
        assert "E-CLAUDE-001" in result.reason

    def test_catalog_classify_auth_maps_to_permanent(self):
        result = catalog_classify_error("Anthropic API authentication failed 401")
        assert result.category == ErrorCategory.PERMANENT
        assert result.should_retry is False
        assert result.max_retries == 0

    def test_catalog_classify_falls_through_to_self_healing(self):
        # A generic timeout with no catalog-specific match
        result = catalog_classify_error("timeout on some random thing")
        # self_healing catches "timeout" as TIMEOUT
        assert result.category == ErrorCategory.TIMEOUT
        assert result.should_retry is True

    def test_exception_object_works(self):
        exc = ConnectionRefusedError("MCP server connection refused")
        result = catalog_classify_error(exc)
        assert "E-MCP-001" in result.reason


# ---------------------------------------------------------------------------
# Catalog management
# ---------------------------------------------------------------------------


class TestCatalogManagement:
    def test_get_catalog_returns_copy(self):
        cat = get_catalog()
        assert len(cat) == len(_CATALOG)
        cat.pop()
        # Original unchanged
        assert len(get_catalog()) == len(_CATALOG)

    def test_register_entry_takes_priority(self):
        custom = ErrorEntry(
            code="E-CUSTOM-001",
            category="permanent",
            pattern=r"custom boom",
            recommended_action="alert",
            max_retries=0,
            backoff_base=0.0,
        )
        register_entry(custom)
        entry = classify_from_catalog("custom boom happened")
        assert entry is not None
        assert entry.code == "E-CUSTOM-001"

    def test_error_entry_pattern_compiles(self):
        for entry in _CATALOG:
            assert isinstance(entry._compiled, re.Pattern)
