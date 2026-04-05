"""Comprehensive coverage tests for utils.llm_cache module.

Tests all untested paths, error conditions, and edge cases identified in coverage analysis.
"""

import threading
import time
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

from utils.llm_cache import (
    _DEFAULT_TTL,
    _FUZZY_ENABLED,
    _FUZZY_THRESHOLD,
    _KEY_PREFIX,
    _REDIS_DB,
    _STATS_KEY,
    CacheStats,
    LLMCache,
    cached_llm_call,
)


class TestCacheStats:
    """Test CacheStats dataclass and methods."""

    def test_cache_stats_default_values(self):
        """Test CacheStats default initialization."""
        stats = CacheStats()
        assert stats.hits == 0
        assert stats.misses == 0
        assert stats.writes == 0
        assert stats.bypasses == 0
        assert stats.fuzzy_hits == 0
        assert stats.fuzzy_unavailable == 0
        assert stats.total_entries == 0
        assert stats.memory_bytes == 0

    def test_cache_stats_hit_rate_zero_total(self):
        """Test hit_rate calculation with zero total requests."""
        stats = CacheStats()
        assert stats.hit_rate == 0.0

    def test_cache_stats_hit_rate_calculation(self):
        """Test hit_rate calculation with hits and misses."""
        stats = CacheStats(hits=3, misses=7)
        assert stats.hit_rate == 0.3

    def test_cache_stats_hit_rate_all_hits(self):
        """Test hit_rate calculation with all hits."""
        stats = CacheStats(hits=10, misses=0)
        assert stats.hit_rate == 1.0

    def test_cache_stats_as_dict(self):
        """Test as_dict method."""
        stats = CacheStats(
            hits=5,
            misses=3,
            writes=8,
            bypasses=2,
            fuzzy_hits=1,
            fuzzy_unavailable=0,
            total_entries=8,
            memory_bytes=1024,
        )
        result = stats.as_dict()

        expected = {
            "hits": 5,
            "misses": 3,
            "writes": 8,
            "bypasses": 2,
            "fuzzy_hits": 1,
            "fuzzy_unavailable": 0,
            "total_entries": 8,
            "memory_bytes": 1024,
            "hit_rate": 0.625,
        }
        assert result == expected

    def test_cache_stats_hit_rate_rounding(self):
        """Test hit_rate rounding in as_dict."""
        stats = CacheStats(hits=1, misses=3)  # hit_rate = 0.25
        result = stats.as_dict()
        assert result["hit_rate"] == 0.25

        stats = CacheStats(hits=1, misses=2)  # hit_rate = 0.33333...
        result = stats.as_dict()
        assert result["hit_rate"] == 0.3333


class TestLLMCacheBasics:
    """Test basic LLMCache functionality."""

    @pytest.fixture
    def cache(self):
        """Create LLMCache with fake Redis."""
        fake_redis = fakeredis.FakeRedis(db=_REDIS_DB, decode_responses=True)
        return LLMCache(redis_client=fake_redis)

    def test_cache_key_generation(self, cache):
        """Test cache key generation."""
        key = cache.cache_key("model-1", "system prompt", "user prompt", 0.5)

        assert key.startswith(_KEY_PREFIX)
        # Key should be a hash after the prefix (not contain literal model name)
        hash_part = key[len(_KEY_PREFIX) :]
        assert len(hash_part) == 64  # SHA-256 hex digest length
        assert hash_part.isalnum()
        # Key should be deterministic
        key2 = cache.cache_key("model-1", "system prompt", "user prompt", 0.5)
        assert key == key2

    def test_cache_key_different_inputs(self, cache):
        """Test cache key generation with different inputs produces different keys."""
        key1 = cache.cache_key("model-1", "system", "user", 0.5)
        key2 = cache.cache_key("model-2", "system", "user", 0.5)
        key3 = cache.cache_key("model-1", "different", "user", 0.5)
        key4 = cache.cache_key("model-1", "system", "different", 0.5)
        key5 = cache.cache_key("model-1", "system", "user", 0.7)

        assert key1 != key2
        assert key1 != key3
        assert key1 != key4
        assert key1 != key5

    def test_cache_key_temperature_precision(self, cache):
        """Test cache key handles temperature precision correctly."""
        key1 = cache.cache_key("model", "sys", "user", 0.5)
        key2 = cache.cache_key("model", "sys", "user", 0.50)
        key3 = cache.cache_key("model", "sys", "user", 0.500000)

        assert key1 == key2 == key3

    def test_set_and_get_success(self, cache):
        """Test successful set and get operations."""
        key = cache.cache_key("model", "sys", "user", 0.0)
        data = {"response": "test response", "tokens": 100}

        cache.set(key, data)
        result = cache.get(key)

        # Cache may enrich with metadata (timestamp, ttl); check original keys
        assert result["response"] == data["response"]
        assert result["tokens"] == data["tokens"]

    def test_get_nonexistent_key(self, cache):
        """Test get with nonexistent key returns None."""
        key = cache.cache_key("model", "sys", "user", 0.0)
        result = cache.get(key)
        assert result is None

    def test_set_with_ttl(self, cache):
        """Test set operation with custom TTL."""
        key = cache.cache_key("model", "sys", "user", 0.0)
        data = {"response": "test"}

        cache.set(key, data, ttl=3600)
        result = cache.get(key)
        assert result["response"] == data["response"]

    def test_clear_cache(self, cache):
        """Test cache clearing via invalidate()."""
        key = cache.cache_key("model", "sys", "user", 0.0)
        cache.set(key, {"response": "test"})

        assert cache.get(key) is not None
        cache.invalidate()
        assert cache.get(key) is None

    def test_get_stats_empty_cache(self, cache):
        """Test stats() with empty cache."""
        result = cache.stats()
        assert isinstance(result, CacheStats)
        assert result.hits == 0
        assert result.misses == 0
        assert result.writes == 0


class TestLLMCacheStats:
    """Test LLMCache statistics tracking."""

    @pytest.fixture
    def cache(self):
        """Create LLMCache with fake Redis."""
        fake_redis = fakeredis.FakeRedis(db=_REDIS_DB, decode_responses=True)
        return LLMCache(redis_client=fake_redis)

    def test_stats_tracking_hit(self, cache):
        """Test stats tracking for cache hit."""
        key = cache.cache_key("model", "sys", "user", 0.0)
        cache.set(key, {"response": "test"})

        # This should be a hit
        cache.get(key)

        stats = cache.stats()
        assert stats.hits == 1
        assert stats.writes == 1

    def test_stats_tracking_miss(self, cache):
        """Test stats tracking for cache miss."""
        key = cache.cache_key("model", "sys", "user", 0.0)

        # This should be a miss
        cache.get(key)

        stats = cache.stats()
        assert stats.misses == 1

    def test_stats_increment_writes(self, cache):
        """Test stats tracking for write operations."""
        key1 = cache.cache_key("model", "sys", "user1", 0.0)
        key2 = cache.cache_key("model", "sys", "user2", 0.0)

        cache.set(key1, {"response": "test1"})
        cache.set(key2, {"response": "test2"})

        stats = cache.stats()
        assert stats.writes == 2

    def test_stats_bypass_increment(self, cache):
        """Test stats tracking for bypass operations."""
        cache._inc("bypasses")

        stats = cache.stats()
        assert stats.bypasses == 1

    def test_stats_persistence(self, cache):
        """Test that stats persist across operations."""
        key = cache.cache_key("model", "sys", "user", 0.0)

        # Multiple operations
        cache.get(key)  # miss
        cache.set(key, {"response": "test"})  # write
        cache.get(key)  # hit
        cache.get("nonexistent")  # miss

        stats = cache.stats()
        assert stats.hits == 1
        assert stats.misses == 2
        assert stats.writes == 1


class TestLLMCacheErrorHandling:
    """Test LLMCache error handling and edge cases."""

    def test_redis_connection_error_get(self):
        """Test Redis connection error during get operation."""
        mock_redis = MagicMock()
        mock_redis.get.side_effect = Exception("Redis connection failed")

        cache = LLMCache(redis_client=mock_redis)
        key = cache.cache_key("model", "sys", "user", 0.0)

        result = cache.get(key)
        assert result is None

    def test_redis_connection_error_set(self):
        """Test Redis connection error during set operation."""
        mock_redis = MagicMock()
        mock_redis.setex.side_effect = Exception("Redis connection failed")

        cache = LLMCache(redis_client=mock_redis)
        key = cache.cache_key("model", "sys", "user", 0.0)

        # Should not raise exception
        cache.set(key, {"response": "test"})

    def test_redis_connection_error_clear(self):
        """Test Redis connection error during clear operation."""
        mock_redis = MagicMock()
        mock_redis.delete.side_effect = Exception("Redis connection failed")

        cache = LLMCache(redis_client=mock_redis)

        # Should not raise exception
        cache.invalidate()

    def test_json_decode_error_get(self):
        """Test JSON decode error during get operation."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = "invalid json"

        cache = LLMCache(redis_client=mock_redis)
        key = cache.cache_key("model", "sys", "user", 0.0)

        result = cache.get(key)
        assert result is None

    def test_json_encode_error_set(self):
        """Test JSON encode error during set operation."""
        mock_redis = MagicMock()

        cache = LLMCache(redis_client=mock_redis)
        key = cache.cache_key("model", "sys", "user", 0.0)

        # Try to cache non-serializable data
        data = {"func": lambda x: x}  # Functions are not JSON serializable

        # Should not raise exception
        cache.set(key, data)

    def test_unicode_handling(self):
        """Test handling of Unicode characters."""
        fake_redis = fakeredis.FakeRedis(db=_REDIS_DB, decode_responses=True)
        cache = LLMCache(redis_client=fake_redis)

        key = cache.cache_key("model", "système", "utilisateur 🚀", 0.0)
        data = {"response": "réponse avec émojis 🎉", "tokens": 100}

        cache.set(key, data)
        result = cache.get(key)

        # Cache enriches with metadata (timestamp, ttl); check original keys
        assert result["response"] == data["response"]
        assert result["tokens"] == data["tokens"]


class TestCachedLLMCall:
    """Test cached_llm_call wrapper function."""

    @pytest.fixture
    def mock_cache(self):
        """Create mock cache for testing."""
        with patch("utils.llm_cache.llm_cache") as mock:
            yield mock

    def test_cached_llm_call_cache_hit(self, mock_cache):
        """Test cached_llm_call with cache hit."""
        cached_response = {"response": "cached result", "tokens": 50}
        mock_cache.get.return_value = cached_response
        mock_cache.cache_key.return_value = "test_key"

        call_fn = MagicMock(return_value="fresh result")

        result = cached_llm_call(
            call_fn=call_fn, model="test-model", system_prompt="system", user_prompt="user", temperature=0.0
        )

        assert result["response"] == "cached result"
        assert result["cached"] is True
        call_fn.assert_not_called()  # Should not call the function
        mock_cache.get.assert_called_once_with("test_key")

    def test_cached_llm_call_cache_miss(self, mock_cache):
        """Test cached_llm_call with cache miss."""
        mock_cache.get.return_value = None
        mock_cache.get_fuzzy.return_value = None
        mock_cache.cache_key.return_value = "test_key"

        call_fn = MagicMock(return_value="fresh result")

        result = cached_llm_call(
            call_fn=call_fn, model="test-model", system_prompt="system", user_prompt="user", temperature=0.0
        )

        assert result["response"] == "fresh result"
        assert result["cached"] is False
        call_fn.assert_called_once()
        mock_cache.set.assert_called_once()

    def test_cached_llm_call_with_tokens(self, mock_cache):
        """Test cached_llm_call with token extraction."""
        mock_cache.get.return_value = None
        mock_cache.get_fuzzy.return_value = None
        mock_cache.cache_key.return_value = "test_key"

        # Mock response with usage information
        mock_response = MagicMock()
        mock_response.usage.total_tokens = 150
        call_fn = MagicMock(return_value=mock_response)

        result = cached_llm_call(
            call_fn=call_fn,
            model="test-model",
            system_prompt="system",
            user_prompt="user",
            temperature=0.0,
            extract_tokens=lambda r: r.usage.total_tokens,
        )

        assert result["tokens_used"] == 150
        assert result["cached"] is False
        mock_cache.set.assert_called_once()

    def test_cached_llm_call_exception_handling(self, mock_cache):
        """Test cached_llm_call handles exceptions gracefully."""
        mock_cache.get.return_value = None
        mock_cache.get_fuzzy.return_value = None
        mock_cache.cache_key.return_value = "test_key"

        call_fn = MagicMock(side_effect=Exception("LLM API failed"))

        with pytest.raises(Exception, match="LLM API failed"):
            cached_llm_call(
                call_fn=call_fn, model="test-model", system_prompt="system", user_prompt="user", temperature=0.0
            )

        # Should not cache failed responses
        mock_cache.set.assert_not_called()

    def test_cached_llm_call_custom_ttl(self, mock_cache):
        """Test cached_llm_call with custom TTL."""
        mock_cache.get.return_value = None
        mock_cache.get_fuzzy.return_value = None
        mock_cache.cache_key.return_value = "test_key"

        call_fn = MagicMock(return_value="result")

        cached_llm_call(
            call_fn=call_fn, model="test-model", system_prompt="system", user_prompt="user", temperature=0.0, ttl=7200
        )

        mock_cache.set.assert_called_once()
        call_args = mock_cache.set.call_args
        assert call_args[1]["ttl"] == 7200

    def test_cached_llm_call_response_extraction(self, mock_cache):
        """Test cached_llm_call properly extracts response from cached data."""
        cached_data = {"response": "cached response", "tokens_used": 100, "timestamp": time.time()}
        mock_cache.get.return_value = cached_data
        mock_cache.cache_key.return_value = "test_key"

        call_fn = MagicMock()

        result = cached_llm_call(
            call_fn=call_fn, model="test-model", system_prompt="system", user_prompt="user", temperature=0.0
        )

        assert result["response"] == "cached response"
        assert result["cached"] is True
        call_fn.assert_not_called()


class TestLLMCacheConcurrency:
    """Test LLMCache thread safety and concurrency."""

    @pytest.fixture
    def cache(self):
        """Create LLMCache with fake Redis."""
        fake_redis = fakeredis.FakeRedis(db=_REDIS_DB, decode_responses=True)
        return LLMCache(redis_client=fake_redis)

    def test_concurrent_access(self, cache):
        """Test concurrent cache access doesn't cause issues."""
        results = []
        errors = []

        def worker(worker_id):
            try:
                for i in range(10):
                    key = cache.cache_key(f"model-{worker_id}", "sys", f"user-{i}", 0.0)
                    data = {"response": f"response-{worker_id}-{i}", "tokens": i}
                    cache.set(key, data)
                    retrieved = cache.get(key)
                    results.append(retrieved)
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(5):
            thread = threading.Thread(target=worker, args=(i,))
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        assert not errors, f"Concurrency errors occurred: {errors}"
        assert len(results) == 50  # 5 workers * 10 operations each

    def test_stats_thread_safety(self, cache):
        """Test that stats tracking is thread-safe."""

        def increment_stats():
            for _ in range(100):
                cache._inc("hits")
                cache._inc("misses")
                cache._inc("writes")

        threads = []
        for _ in range(5):
            thread = threading.Thread(target=increment_stats)
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        stats = cache.stats()
        # Each thread increments each counter 100 times
        assert stats.hits == 500
        assert stats.misses == 500
        assert stats.writes == 500


class TestLLMCacheConstants:
    """Test module constants and configuration."""

    def test_constants_exist(self):
        """Test that required constants are defined."""
        assert _REDIS_DB == 4
        assert _KEY_PREFIX == "novacore:llmcache:"
        assert _STATS_KEY == "novacore:llmcache:__stats__"
        assert isinstance(_DEFAULT_TTL, int)
        assert isinstance(_FUZZY_ENABLED, bool)
        assert isinstance(_FUZZY_THRESHOLD, float)

    @patch.dict("os.environ", {"NOVA_LLM_CACHE_TTL": "3600"})
    def test_env_ttl_override(self):
        """Test TTL can be overridden via environment."""
        # Need to reload the module to pick up env change
        import importlib

        import utils.llm_cache

        importlib.reload(utils.llm_cache)
        from utils.llm_cache import _DEFAULT_TTL

        assert _DEFAULT_TTL == 3600

    @patch.dict("os.environ", {"NOVA_LLM_CACHE_FUZZY": "1"})
    def test_env_fuzzy_enable(self):
        """Test fuzzy matching can be enabled via environment."""
        import importlib

        import utils.llm_cache

        importlib.reload(utils.llm_cache)
        from utils.llm_cache import _FUZZY_ENABLED

        assert _FUZZY_ENABLED is True
