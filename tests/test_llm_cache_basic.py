"""Basic tests for utils/llm_cache.py - critical LLM caching with 0% coverage."""

import json
from unittest.mock import Mock, patch

import pytest

# Import the LLM cache module
from utils.llm_cache import LLMCache, cached_llm_call


class TestLLMCache:
    """Test LLMCache functionality."""

    @pytest.fixture
    def mock_redis(self):
        """Create a mock Redis client."""
        mock_redis = Mock()
        mock_redis.ping.return_value = True
        mock_redis.get.return_value = None
        mock_redis.setex.return_value = True
        return mock_redis

    @pytest.fixture
    def llm_cache(self, mock_redis):
        """Create LLMCache instance with mocked Redis."""
        with patch("redis.Redis", return_value=mock_redis):
            cache = LLMCache()
            cache._redis = mock_redis
            return cache

    def test_cache_key_generation(self, llm_cache):
        """Test cache key generation is deterministic."""
        key1 = llm_cache.cache_key("model1", "system", "user", 0.0)
        key2 = llm_cache.cache_key("model1", "system", "user", 0.0)
        assert key1 == key2
        assert "novacore" in key1

    def test_cache_key_different_inputs(self, llm_cache):
        """Test different inputs produce different cache keys."""
        key1 = llm_cache.cache_key("model1", "system", "user1", 0.0)
        key2 = llm_cache.cache_key("model1", "system", "user2", 0.0)
        assert key1 != key2

    def test_get_cache_miss(self, llm_cache, mock_redis):
        """Test cache miss returns None."""
        mock_redis.get.return_value = None
        result = llm_cache.get("test_key")
        assert result is None

    def test_get_cache_hit(self, llm_cache, mock_redis):
        """Test cache hit returns parsed JSON."""
        cached_data = {"response": "test response", "tokens": 100}
        mock_redis.get.return_value = json.dumps(cached_data)

        result = llm_cache.get("test_key")
        assert result == cached_data

    def test_set_cache(self, llm_cache, mock_redis):
        """Test setting cache data."""
        data = {"response": "test response", "tokens": 100}
        llm_cache.set("test_key", data)

        # Should call setex with TTL (86400 seconds = 24 hours)
        mock_redis.setex.assert_called_once()
        args = mock_redis.setex.call_args[0]
        assert args[0] == "test_key"
        assert args[1] == 86400  # 24 hour TTL
        cached_data = json.loads(args[2])
        assert cached_data["response"] == data["response"]
        assert cached_data["tokens"] == data["tokens"]
        # May include additional metadata like timestamp, ttl

    def test_redis_connection_failure(self, mock_redis):
        """Test handling of Redis connection failure."""
        mock_redis.ping.side_effect = Exception("Connection failed")

        with patch("redis.Redis", return_value=mock_redis):
            cache = LLMCache()
            # Should gracefully handle connection failure without crashing
            # Cache operations should not raise exceptions
            result = cache.get("test_key")
            assert result is None  # Should return None gracefully

    def test_cache_key_with_special_characters(self, llm_cache):
        """Test cache key generation with special characters."""
        key = llm_cache.cache_key("model", "system: hello\nworld", "user: test\r\ndata", 0.5)
        assert key is not None
        assert "novacore" in key


class TestCachedLLMCall:
    """Test cached_llm_call wrapper function."""

    @patch("utils.llm_cache.llm_cache")
    def test_cache_hit(self, mock_cache):
        """Test cached LLM call with cache hit."""
        # Mock cache hit
        cached_response = {"response": "cached result", "tokens_used": 50}
        mock_cache.get.return_value = cached_response

        call_fn = Mock()  # Should not be called on cache hit

        result = cached_llm_call(
            call_fn=call_fn, model="test-model", system_prompt="system", user_prompt="user", temperature=0.0
        )

        assert result["response"] == cached_response["response"]
        assert result["tokens_used"] == cached_response["tokens_used"]
        call_fn.assert_not_called()

    @patch("utils.llm_cache.llm_cache")
    def test_cache_miss(self, mock_cache):
        """Test cached LLM call with cache miss."""
        # Mock cache miss (both exact and fuzzy)
        mock_cache.get.return_value = None
        mock_cache.get_fuzzy.return_value = None

        # Mock LLM response
        llm_response = {"response": "new result", "tokens_used": 75}
        call_fn = Mock(return_value=llm_response)

        result = cached_llm_call(
            call_fn=call_fn, model="test-model", system_prompt="system", user_prompt="user", temperature=0.0
        )

        # The function should return the response with additional metadata
        assert result["response"] == llm_response["response"]
        assert result["tokens_used"] == llm_response["tokens_used"]
        assert result["model"] == "test-model"
        assert not result["cached"]
        assert result["source"] == "api"
        call_fn.assert_called_once()
        mock_cache.set.assert_called_once()

    @patch("utils.llm_cache.llm_cache")
    def test_exception_handling(self, mock_cache):
        """Test exception handling in cached LLM call."""
        # Mock cache miss (both exact and fuzzy)
        mock_cache.get.return_value = None
        mock_cache.get_fuzzy.return_value = None

        # Mock LLM call exception
        call_fn = Mock(side_effect=Exception("LLM API error"))

        with pytest.raises(Exception, match="LLM API error"):
            cached_llm_call(
                call_fn=call_fn, model="test-model", system_prompt="system", user_prompt="user", temperature=0.0
            )

        # Should not cache on exception
        mock_cache.set.assert_not_called()
