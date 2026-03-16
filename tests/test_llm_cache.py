"""Tests for utils/llm_cache.py — Phase 4.2 LLM Response Caching."""

import time
from unittest import mock

import pytest

from utils.llm_cache import CacheStats, LLMCache, cached_llm_call

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeRedis:
    """Minimal in-memory Redis mock for unit testing."""

    def __init__(self):
        self._store: dict[str, bytes] = {}
        self._ttls: dict[str, float] = {}

    def ping(self):
        return True

    def get(self, key):
        if isinstance(key, str):
            key = key.encode()
        # Check TTL expiration
        if key in self._ttls and time.time() > self._ttls[key]:
            del self._store[key]
            del self._ttls[key]
            return None
        return self._store.get(key)

    def setex(self, key, ttl, value):
        if isinstance(key, str):
            key = key.encode()
        if isinstance(value, str):
            value = value.encode()
        self._store[key] = value
        self._ttls[key] = time.time() + ttl

    def delete(self, *keys):
        count = 0
        for k in keys:
            if isinstance(k, str):
                k = k.encode()
            if k in self._store:
                del self._store[k]
                if k in self._ttls:
                    del self._ttls[k]
                count += 1
        return count

    def scan(self, cursor=0, match=None, count=100):
        # Simple implementation: return all matching keys at once
        import fnmatch

        if isinstance(match, str):
            match_bytes = match.encode()
        else:
            match_bytes = match or b"*"
        result = []
        for k in list(self._store.keys()):
            # Check TTL
            if k in self._ttls and time.time() > self._ttls[k]:
                del self._store[k]
                del self._ttls[k]
                continue
            if fnmatch.fnmatch(k, match_bytes):
                result.append(k)
        return (0, result)  # cursor=0 means done

    def info(self, section=None):
        total_bytes = sum(len(v) for v in self._store.values())
        return {"used_memory": total_bytes}


def _make_cache(redis_client=None) -> LLMCache:
    """Create an LLMCache with a FakeRedis backend."""
    cache = LLMCache.__new__(LLMCache)
    cache._redis = redis_client or FakeRedis()
    cache._lock = __import__("threading").Lock()
    cache._local_stats = CacheStats()
    return cache


# ---------------------------------------------------------------------------
# Unit tests: cache_key
# ---------------------------------------------------------------------------


class TestCacheKey:
    def test_deterministic(self):
        k1 = LLMCache.cache_key("model-a", "sys", "user", 0.0)
        k2 = LLMCache.cache_key("model-a", "sys", "user", 0.0)
        assert k1 == k2

    def test_different_inputs_different_keys(self):
        k1 = LLMCache.cache_key("model-a", "sys", "hello", 0.0)
        k2 = LLMCache.cache_key("model-a", "sys", "world", 0.0)
        assert k1 != k2

    def test_temperature_matters(self):
        k1 = LLMCache.cache_key("model-a", "sys", "prompt", 0.0)
        k2 = LLMCache.cache_key("model-a", "sys", "prompt", 0.7)
        assert k1 != k2

    def test_prefix(self):
        key = LLMCache.cache_key("m", "s", "u", 0.0)
        assert key.startswith("novacore:llmcache:")

    def test_key_is_sha256_hex(self):
        key = LLMCache.cache_key("m", "s", "u", 0.0)
        hex_part = key.split(":")[-1]
        assert len(hex_part) == 64
        int(hex_part, 16)  # Should not raise


# ---------------------------------------------------------------------------
# Unit tests: get / set
# ---------------------------------------------------------------------------


class TestGetSet:
    def test_miss_returns_none(self):
        cache = _make_cache()
        result = cache.get("novacore:llmcache:nonexistent")
        assert result is None

    def test_set_then_get(self):
        cache = _make_cache()
        key = cache.cache_key("model", "sys", "prompt", 0.0)
        cache.set(key, {"response": "hello", "tokens_used": 10})
        result = cache.get(key)
        assert result is not None
        assert result["response"] == "hello"
        assert result["tokens_used"] == 10
        assert "timestamp" in result
        assert "ttl" in result

    def test_set_with_custom_ttl(self):
        cache = _make_cache()
        key = cache.cache_key("model", "sys", "prompt", 0.0)
        cache.set(key, {"response": "hi"}, ttl=3600)
        result = cache.get(key)
        assert result["ttl"] == 3600

    def test_stats_after_hit_miss(self):
        cache = _make_cache()
        key = cache.cache_key("model", "sys", "prompt", 0.0)

        cache.get(key)  # miss
        assert cache._local_stats.misses == 1

        cache.set(key, {"response": "ok"})
        assert cache._local_stats.writes == 1

        cache.get(key)  # hit
        assert cache._local_stats.hits == 1

    def test_get_without_redis(self):
        cache = LLMCache.__new__(LLMCache)
        cache._redis = None
        cache._lock = __import__("threading").Lock()
        cache._local_stats = CacheStats()
        result = cache.get("anything")
        assert result is None


# ---------------------------------------------------------------------------
# Unit tests: invalidate
# ---------------------------------------------------------------------------


class TestInvalidate:
    def test_invalidate_all(self):
        cache = _make_cache()
        for i in range(5):
            key = cache.cache_key("model", "sys", f"prompt_{i}", 0.0)
            cache.set(key, {"response": f"resp_{i}"})

        deleted = cache.invalidate()
        assert deleted == 5

    def test_invalidate_pattern(self):
        fake = FakeRedis()
        cache = _make_cache(fake)

        # Set entries with known keys
        k1 = cache.cache_key("model", "sys", "alpha", 0.0)
        k2 = cache.cache_key("model", "sys", "beta", 0.0)
        cache.set(k1, {"response": "a"})
        cache.set(k2, {"response": "b"})

        # Invalidate all (pattern=*)
        deleted = cache.invalidate("*")
        assert deleted == 2

    def test_invalidate_no_redis(self):
        cache = LLMCache.__new__(LLMCache)
        cache._redis = None
        cache._lock = __import__("threading").Lock()
        cache._local_stats = CacheStats()
        assert cache.invalidate() == 0


# ---------------------------------------------------------------------------
# Unit tests: stats
# ---------------------------------------------------------------------------


class TestStats:
    def test_initial_stats(self):
        cache = _make_cache()
        s = cache.stats()
        assert s.hits == 0
        assert s.misses == 0
        assert s.writes == 0
        assert s.hit_rate == 0.0

    def test_hit_rate_calculation(self):
        s = CacheStats(hits=3, misses=7)
        assert s.hit_rate == pytest.approx(0.3)

    def test_hit_rate_zero_division(self):
        s = CacheStats(hits=0, misses=0)
        assert s.hit_rate == 0.0

    def test_as_dict(self):
        s = CacheStats(hits=5, misses=5, writes=3)
        d = s.as_dict()
        assert d["hits"] == 5
        assert d["hit_rate"] == 0.5
        assert "memory_bytes" in d

    def test_stats_counts_entries(self):
        cache = _make_cache()
        for i in range(3):
            key = cache.cache_key("m", "s", f"p{i}", 0.0)
            cache.set(key, {"response": f"r{i}"})
        s = cache.stats()
        assert s.total_entries == 3


# ---------------------------------------------------------------------------
# TTL expiration
# ---------------------------------------------------------------------------


class TestTTLExpiration:
    def test_expired_entry_returns_none(self):
        cache = _make_cache()
        key = cache.cache_key("model", "sys", "prompt", 0.0)
        # Set with very short TTL
        cache.set(key, {"response": "ephemeral"}, ttl=1)
        # Manually expire by adjusting FakeRedis TTL
        fake = cache._redis
        for k in fake._ttls:
            fake._ttls[k] = time.time() - 1  # already expired
        result = cache.get(key)
        assert result is None


# ---------------------------------------------------------------------------
# Fuzzy match guard behavior
# ---------------------------------------------------------------------------


class TestFuzzyMatchGuard:
    def test_fuzzy_disabled_by_default(self):
        cache = _make_cache()
        result = cache.get_fuzzy("model", "sys", "prompt", 0.0)
        assert result is None
        assert cache._local_stats.fuzzy_unavailable >= 1

    @mock.patch.dict("os.environ", {"NOVA_LLM_CACHE_FUZZY": "1"})
    def test_fuzzy_enabled_but_no_sentence_transformers(self):
        cache = _make_cache()
        with (
            mock.patch.dict("sys.modules", {"sentence_transformers": None}),
            mock.patch("builtins.__import__", side_effect=ImportError),
        ):
            result = cache.get_fuzzy("model", "sys", "prompt", 0.0)
        assert result is None


# ---------------------------------------------------------------------------
# cached_llm_call wrapper
# ---------------------------------------------------------------------------


class TestCachedLLMCall:
    def test_cache_miss_calls_fn(self):
        fake = FakeRedis()
        with mock.patch("utils.llm_cache.llm_cache", _make_cache(fake)):
            result = cached_llm_call(
                call_fn=lambda: "result_text",
                model="test-model",
                system_prompt="sys",
                user_prompt="hello",
                temperature=0.0,
            )
        assert result["response"] == "result_text"
        assert result["cached"] is False
        assert result["source"] == "api"

    def test_cache_hit_skips_fn(self):
        fake = FakeRedis()
        cache = _make_cache(fake)
        with mock.patch("utils.llm_cache.llm_cache", cache):
            # First call: miss
            result1 = cached_llm_call(
                call_fn=lambda: "first_response",
                model="test-model",
                system_prompt="sys",
                user_prompt="hello",
                temperature=0.0,
            )
            assert result1["cached"] is False

            # Second call: should hit cache
            call_count = 0

            def should_not_call():
                nonlocal call_count
                call_count += 1
                return "second_response"

            result2 = cached_llm_call(
                call_fn=should_not_call,
                model="test-model",
                system_prompt="sys",
                user_prompt="hello",
                temperature=0.0,
            )
            assert result2["cached"] is True
            assert result2["source"] == "exact"
            assert result2["response"] == "first_response"
            assert call_count == 0

    def test_cache_bypass(self):
        fake = FakeRedis()
        cache = _make_cache(fake)
        with mock.patch("utils.llm_cache.llm_cache", cache):
            # Populate cache
            cached_llm_call(
                call_fn=lambda: "original",
                model="m",
                system_prompt="s",
                user_prompt="u",
                temperature=0.0,
            )

            # Bypass should call fn even though cache has entry
            result = cached_llm_call(
                call_fn=lambda: "fresh",
                model="m",
                system_prompt="s",
                user_prompt="u",
                temperature=0.0,
                cache_bypass=True,
            )
            assert result["cached"] is False
            assert result["response"] == "fresh"

    def test_extract_response_callable(self):
        fake = FakeRedis()
        cache = _make_cache(fake)
        with mock.patch("utils.llm_cache.llm_cache", cache):
            result = cached_llm_call(
                call_fn=lambda: {"choices": [{"text": "extracted"}]},
                model="m",
                system_prompt="s",
                user_prompt="u",
                extract_response=lambda r: r["choices"][0]["text"],
                extract_tokens=lambda r: 42,
            )
            assert result["response"] == "extracted"
            assert result["tokens_used"] == 42

    def test_dict_response_extracts_response_key(self):
        fake = FakeRedis()
        cache = _make_cache(fake)
        with mock.patch("utils.llm_cache.llm_cache", cache):
            result = cached_llm_call(
                call_fn=lambda: {"response": "dict_resp", "tokens_used": 99},
                model="m",
                system_prompt="s",
                user_prompt="u",
            )
            assert result["response"] == "dict_resp"
            assert result["tokens_used"] == 99


# ---------------------------------------------------------------------------
# Heartbeat check
# ---------------------------------------------------------------------------


class TestHeartbeatCheck:
    def test_check_llm_cache_healthy(self):
        from heartbeat import check_llm_cache

        mock_cache = mock.MagicMock()
        mock_cache.available = True
        mock_cache.stats.return_value = CacheStats(hits=50, misses=50, writes=100, total_entries=100, memory_bytes=1024)
        with mock.patch("utils.llm_cache.llm_cache", mock_cache):
            result = check_llm_cache()
        assert result["name"] == "llm_cache"
        assert result["ok"] is True
        assert "entries=100" in result["detail"]

    def test_check_llm_cache_low_hit_rate(self):
        from heartbeat import check_llm_cache

        mock_cache = mock.MagicMock()
        mock_cache.available = True
        mock_cache.stats.return_value = CacheStats(hits=1, misses=99, writes=100, total_entries=50, memory_bytes=1024)
        with mock.patch("utils.llm_cache.llm_cache", mock_cache):
            result = check_llm_cache()
        assert result["ok"] is False
        assert "low hit rate" in result["detail"]

    def test_check_llm_cache_high_memory(self):
        from heartbeat import check_llm_cache

        mock_cache = mock.MagicMock()
        mock_cache.available = True
        mock_cache.stats.return_value = CacheStats(
            hits=50,
            misses=50,
            writes=100,
            total_entries=50,
            memory_bytes=600 * 1024 * 1024,  # 600 MB
        )
        with mock.patch("utils.llm_cache.llm_cache", mock_cache):
            result = check_llm_cache()
        assert result["ok"] is False
        assert "high memory" in result["detail"]

    def test_check_llm_cache_redis_unavailable(self):
        from heartbeat import check_llm_cache

        mock_cache = mock.MagicMock()
        mock_cache.available = False
        with mock.patch("utils.llm_cache.llm_cache", mock_cache):
            result = check_llm_cache()
        assert result["ok"] is True
        assert "unavailable" in result["detail"]

    def test_check_llm_cache_exception_handled(self):
        from heartbeat import check_llm_cache

        with mock.patch("utils.llm_cache.llm_cache", side_effect=Exception("boom")):
            # The function catches exceptions internally
            result = check_llm_cache()
        assert result["ok"] is True
        assert "skipped" in result["detail"]


# ---------------------------------------------------------------------------
# available property
# ---------------------------------------------------------------------------


class TestAvailability:
    def test_available_with_redis(self):
        cache = _make_cache()
        assert cache.available is True

    def test_unavailable_without_redis(self):
        cache = LLMCache.__new__(LLMCache)
        cache._redis = None
        cache._lock = __import__("threading").Lock()
        cache._local_stats = CacheStats()
        assert cache.available is False


# ---------------------------------------------------------------------------
# Structured log emission
# ---------------------------------------------------------------------------


class TestLogEmission:
    def test_emit_calls_slog(self):
        cache = _make_cache()
        with (
            mock.patch("utils.llm_cache.slog", create=True) as mock_slog,
            mock.patch.dict("sys.modules", {"utils.structured_log": mock.MagicMock(slog=mock_slog)}),
        ):
            cache._emit("cache_hit", key="test")
            # Should not raise even if slog is unavailable


# ---------------------------------------------------------------------------
# traced_cached_llm_call
# ---------------------------------------------------------------------------


class TestTracedCachedLLMCall:
    def test_integration(self):
        from utils.langfuse_tracing import traced_cached_llm_call

        fake = FakeRedis()
        cache = _make_cache(fake)
        with mock.patch("utils.llm_cache.llm_cache", cache):
            result = traced_cached_llm_call(
                call_fn=lambda: "traced_result",
                model="test-model",
                system_prompt="sys",
                user_prompt="hello",
                trace_name="test_trace",
            )
            assert result["response"] == "traced_result"
            assert result["cached"] is False
