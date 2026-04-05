"""Additional tests for LLMCache error paths and edge cases."""

import threading
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

from utils.llm_cache import LLMCache, cached_llm_call


class TestLLMCacheErrorPaths:
    """Test error handling and edge cases in LLMCache."""

    @pytest.fixture
    def isolated_cache(self):
        """Create completely isolated LLMCache instance."""
        fake_redis = fakeredis.FakeRedis(db=10, decode_responses=False)  # Different DB
        cache = LLMCache(redis_client=fake_redis)
        # Reset local stats to ensure isolation
        cache._local_stats.hits = 0
        cache._local_stats.misses = 0
        cache._local_stats.writes = 0
        cache._local_stats.bypasses = 0
        cache._local_stats.fuzzy_hits = 0
        cache._local_stats.fuzzy_unavailable = 0
        return cache

    def test_redis_connection_failure_in_get(self, isolated_cache):
        """Test get operation when Redis fails."""
        # Mock Redis to raise exception
        isolated_cache._redis.get = MagicMock(side_effect=Exception("Redis failed"))

        key = isolated_cache.cache_key("model", "sys", "user", 0.0)
        result = isolated_cache.get(key)

        assert result is None
        # Should increment misses on Redis failure
        stats = isolated_cache.stats()
        assert stats.misses == 1

    def test_redis_connection_failure_in_set(self, isolated_cache):
        """Test set operation when Redis fails."""
        # Mock Redis to raise exception
        isolated_cache._redis.setex = MagicMock(side_effect=Exception("Redis failed"))

        key = isolated_cache.cache_key("model", "sys", "user", 0.0)
        # Should not raise exception even if Redis fails
        isolated_cache.set(key, {"response": "test"})

        # Stats should not increment writes on failure
        stats = isolated_cache.stats()
        assert stats.writes == 0

    def test_json_decode_error_in_get(self, isolated_cache):
        """Test get operation with malformed JSON in cache."""
        key = isolated_cache.cache_key("model", "sys", "user", 0.0)

        # Store invalid JSON directly in Redis
        isolated_cache._redis.set(key, b"invalid json {")

        result = isolated_cache.get(key)
        assert result is None

        # Should count as miss due to JSON decode error
        stats = isolated_cache.stats()
        assert stats.misses == 1

    def test_stats_thread_safety(self, isolated_cache):
        """Test that stats operations are thread-safe."""
        key = isolated_cache.cache_key("model", "sys", "user", 0.0)

        def hit_cache():
            isolated_cache.set(key, {"response": "test"})
            for _ in range(10):
                isolated_cache.get(key)

        # Run multiple threads
        threads = [threading.Thread(target=hit_cache) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stats = isolated_cache.stats()
        # Should have consistent stats (3 writes, 30 hits)
        assert stats.writes == 3
        assert stats.hits == 30

    def test_cache_key_deterministic(self, isolated_cache):
        """Test that cache keys are deterministic."""
        key1 = isolated_cache.cache_key("model", "sys", "user", 0.5)
        key2 = isolated_cache.cache_key("model", "sys", "user", 0.5)
        assert key1 == key2

        # Different parameters should yield different keys
        key3 = isolated_cache.cache_key("model", "sys", "user", 0.6)
        assert key1 != key3

    def test_cached_llm_call_bypass_mode(self, isolated_cache):
        """Test cached_llm_call with cache bypass."""

        def mock_llm_call():
            return "fresh response"

        result = cached_llm_call(
            call_fn=mock_llm_call, model="test-model", system_prompt="sys", user_prompt="user", cache_bypass=True
        )

        assert result["response"] == "fresh response"
        assert result["cached"] is False
        assert result["source"] == "api"

    def test_cached_llm_call_with_extract_functions(self, isolated_cache):
        """Test cached_llm_call with custom extract functions."""

        def mock_llm_call():
            return {"content": "response text", "usage": {"total_tokens": 150}}

        def extract_response(result):
            return result["content"]

        def extract_tokens(result):
            return result["usage"]["total_tokens"]

        result = cached_llm_call(
            call_fn=mock_llm_call,
            model="test-model",
            system_prompt="sys",
            user_prompt="user",
            extract_response=extract_response,
            extract_tokens=extract_tokens,
        )

        assert result["response"] == "response text"
        assert result["tokens_used"] == 150
        assert result["cached"] is False

    def test_unavailable_redis_fallback(self):
        """Test LLMCache behavior when Redis is completely unavailable."""
        with patch("redis.Redis") as mock_redis_class:
            # Make Redis connection fail
            mock_redis_class.return_value.ping.side_effect = Exception("Connection failed")
            cache = LLMCache(redis_client=None)

        assert not cache.available

        key = cache.cache_key("model", "sys", "user", 0.0)

        # All operations should gracefully degrade
        result = cache.get(key)
        assert result is None

        # Set should not raise exception
        cache.set(key, {"response": "test"})

        # Stats should still work
        stats = cache.stats()
        assert stats.total_entries == 0
        assert stats.memory_bytes == 0

    def test_invalidate_with_pattern(self, isolated_cache):
        """Test cache invalidation with pattern matching."""
        # Set up some cache entries
        key1 = isolated_cache.cache_key("model1", "sys", "user", 0.0)
        key2 = isolated_cache.cache_key("model2", "sys", "user", 0.0)

        isolated_cache.set(key1, {"response": "test1"})
        isolated_cache.set(key2, {"response": "test2"})

        # Verify entries exist
        assert isolated_cache.get(key1) is not None
        assert isolated_cache.get(key2) is not None

        # Invalidate all
        deleted = isolated_cache.invalidate()
        assert deleted >= 2  # At least our 2 entries

        # Verify entries are gone
        assert isolated_cache.get(key1) is None
        assert isolated_cache.get(key2) is None

    def test_redis_info_error_in_stats(self, isolated_cache):
        """Test stats method when Redis info command fails."""
        # Mock Redis info to raise exception
        isolated_cache._redis.info = MagicMock(side_effect=Exception("Info failed"))

        # Should not raise exception
        stats = isolated_cache.stats()
        assert stats.memory_bytes == 0  # Default when info fails
