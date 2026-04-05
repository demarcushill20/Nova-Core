"""Simplified tests for utils/llm_cache.py to achieve basic coverage."""

import json
from unittest.mock import Mock, patch

# Import the LLM cache module
from utils.llm_cache import LLMCache, cached_llm_call


@patch("redis.Redis")
def test_llm_cache_creation(mock_redis_class):
    """Test LLMCache can be created."""
    mock_redis = Mock()
    mock_redis.ping.return_value = True
    mock_redis_class.return_value = mock_redis

    cache = LLMCache()
    assert cache is not None


@patch("redis.Redis")
def test_llm_cache_key_generation(mock_redis_class):
    """Test cache key generation works."""
    mock_redis = Mock()
    mock_redis.ping.return_value = True
    mock_redis_class.return_value = mock_redis

    cache = LLMCache()
    key1 = cache.cache_key("model", "system", "user", 0.0)
    key2 = cache.cache_key("model", "system", "user", 0.0)

    assert key1 == key2
    assert isinstance(key1, str)
    assert len(key1) > 0


@patch("redis.Redis")
def test_llm_cache_get_miss(mock_redis_class):
    """Test cache get with miss."""
    mock_redis = Mock()
    mock_redis.ping.return_value = True
    mock_redis.get.return_value = None
    mock_redis_class.return_value = mock_redis

    cache = LLMCache()
    cache._redis = mock_redis

    result = cache.get("test_key")
    assert result is None


@patch("redis.Redis")
def test_llm_cache_get_hit(mock_redis_class):
    """Test cache get with hit."""
    mock_redis = Mock()
    mock_redis.ping.return_value = True
    mock_data = {"response": "test", "tokens": 100}
    mock_redis.get.return_value = json.dumps(mock_data)
    mock_redis_class.return_value = mock_redis

    cache = LLMCache()
    cache._redis = mock_redis

    result = cache.get("test_key")
    assert isinstance(result, dict)


@patch("redis.Redis")
def test_llm_cache_set(mock_redis_class):
    """Test cache set operation."""
    mock_redis = Mock()
    mock_redis.ping.return_value = True
    mock_redis_class.return_value = mock_redis

    cache = LLMCache()
    cache._redis = mock_redis

    data = {"response": "test", "tokens": 100}
    cache.set("test_key", data)

    # Should have called setex
    mock_redis.setex.assert_called_once()


@patch("utils.llm_cache.llm_cache")
def test_cached_llm_call_basic(mock_cache):
    """Test cached LLM call basic functionality."""
    # Mock cache miss then successful call
    mock_cache.get.return_value = None
    mock_cache.get_fuzzy.return_value = None

    call_fn = Mock()
    call_fn.return_value = {"response": "test", "tokens_used": 50}

    result = cached_llm_call(
        call_fn=call_fn, model="test-model", system_prompt="system", user_prompt="user", temperature=0.0
    )

    assert isinstance(result, dict)
    assert "response" in result or call_fn.called
