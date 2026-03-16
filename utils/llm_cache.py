"""Redis-backed LLM response cache for NovaCore.

Phase 4.2 — Semantic LLM Response Caching.

Provides exact-match caching with a guarded fuzzy-match path for future
enablement. All cache events are emitted via structured logging.

Redis db=4 is used exclusively for LLM cache (db=3 = circuit breakers).

Usage:
    from utils.llm_cache import llm_cache

    # Direct use
    key = llm_cache.cache_key("claude-sonnet-4-20250514", "system", "user", 0.0)
    cached = llm_cache.get(key)
    if cached:
        print(cached["response"])
    else:
        response = call_llm(...)
        llm_cache.set(key, {"response": response, "tokens_used": 150})

    # Wrapper use (preferred)
    from utils.llm_cache import cached_llm_call
    result = cached_llm_call(
        call_fn=lambda: openai_client.chat.completions.create(...),
        model="claude-sonnet-4-20250514",
        system_prompt="You are helpful.",
        user_prompt="What is 2+2?",
        temperature=0.0,
    )
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_REDIS_DB = 4
_KEY_PREFIX = "novacore:llmcache:"
_STATS_KEY = "novacore:llmcache:__stats__"
_DEFAULT_TTL = int(os.environ.get("NOVA_LLM_CACHE_TTL", 86400))  # 24h
_FUZZY_ENABLED = os.environ.get("NOVA_LLM_CACHE_FUZZY", "0") == "1"
_FUZZY_THRESHOLD = float(os.environ.get("NOVA_LLM_CACHE_FUZZY_THRESHOLD", "0.95"))


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class CacheStats:
    """Aggregated cache statistics."""

    hits: int = 0
    misses: int = 0
    writes: int = 0
    bypasses: int = 0
    fuzzy_hits: int = 0
    fuzzy_unavailable: int = 0
    total_entries: int = 0
    memory_bytes: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "bypasses": self.bypasses,
            "fuzzy_hits": self.fuzzy_hits,
            "fuzzy_unavailable": self.fuzzy_unavailable,
            "total_entries": self.total_entries,
            "memory_bytes": self.memory_bytes,
            "hit_rate": round(self.hit_rate, 4),
        }


# ---------------------------------------------------------------------------
# LLMCache
# ---------------------------------------------------------------------------


class LLMCache:
    """Redis-backed LLM response cache with exact-match and guarded fuzzy-match.

    Thread-safe. Gracefully degrades to no-op if Redis is unavailable.
    """

    def __init__(self, redis_client=None):
        self._redis = redis_client
        self._lock = threading.Lock()
        self._local_stats = CacheStats()

        if self._redis is None:
            try:
                import redis

                r = redis.Redis(
                    host="localhost",
                    port=6379,
                    db=_REDIS_DB,
                    socket_connect_timeout=1,
                    decode_responses=False,
                )
                r.ping()
                self._redis = r
            except Exception:
                self._redis = None

    @property
    def available(self) -> bool:
        """Whether the Redis backend is connected."""
        return self._redis is not None

    # -- Key generation -------------------------------------------------------

    @staticmethod
    def cache_key(
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
    ) -> str:
        """Generate a deterministic SHA-256 cache key."""
        raw = json.dumps(
            {
                "model": model,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "temperature": temperature,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(raw.encode()).hexdigest()
        return f"{_KEY_PREFIX}{digest}"

    # -- Core operations ------------------------------------------------------

    def get(self, key: str) -> dict[str, Any] | None:
        """Look up a cached response by exact key.

        Returns the cached entry dict or None on miss/error.
        """
        if self._redis is None:
            self._emit("cache_miss", key=key, reason="redis_unavailable")
            return None

        try:
            raw = self._redis.get(key)
            if raw is None:
                self._inc("misses")
                self._emit("cache_miss", key=key)
                return None

            entry = json.loads(raw)
            self._inc("hits")
            self._emit("cache_hit", key=key, model=entry.get("model", ""))
            return entry

        except Exception as exc:
            logger.warning("llm_cache.get failed: %s", exc)
            self._inc("misses")
            self._emit("cache_miss", key=key, reason=str(exc))
            return None

    def get_fuzzy(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
    ) -> dict[str, Any] | None:
        """Attempt fuzzy cache lookup via embedding similarity.

        Guarded: returns None and logs if fuzzy matching is disabled or
        the embedding model cannot be loaded. This keeps the hot path clean.
        """
        if not _FUZZY_ENABLED:
            self._inc("fuzzy_unavailable")
            self._emit(
                "fuzzy_cache_unavailable",
                reason="disabled_by_config",
            )
            return None

        try:
            import numpy as np
            from sentence_transformers import SentenceTransformer
        except ImportError:
            self._inc("fuzzy_unavailable")
            self._emit(
                "fuzzy_cache_unavailable",
                reason="sentence_transformers_not_installed",
            )
            return None

        if self._redis is None:
            self._inc("fuzzy_unavailable")
            self._emit("fuzzy_cache_unavailable", reason="redis_unavailable")
            return None

        try:
            # Load model (cached by sentence-transformers internally)
            st_model = SentenceTransformer("all-MiniLM-L6-v2")

            query_text = f"{system_prompt}\n{user_prompt}"
            query_vec = st_model.encode([query_text])[0]

            # Scan cache keys and find best match
            best_score = -1.0
            best_entry = None

            cursor = 0
            while True:
                cursor, keys = self._redis.scan(
                    cursor=cursor,
                    match=f"{_KEY_PREFIX}*",
                    count=100,
                )
                for k in keys:
                    if k == _STATS_KEY.encode():
                        continue
                    raw = self._redis.get(k)
                    if raw is None:
                        continue
                    entry = json.loads(raw)
                    stored_text = entry.get("_cache_text", "")
                    if not stored_text:
                        continue

                    stored_vec = st_model.encode([stored_text])[0]
                    score = float(
                        np.dot(query_vec, stored_vec) / (np.linalg.norm(query_vec) * np.linalg.norm(stored_vec) + 1e-9)
                    )
                    if score > best_score:
                        best_score = score
                        best_entry = entry

                if cursor == 0:
                    break

            if best_entry is not None and best_score >= _FUZZY_THRESHOLD:
                self._inc("fuzzy_hits")
                self._emit(
                    "fuzzy_cache_hit",
                    score=round(best_score, 4),
                    model=best_entry.get("model", ""),
                )
                return best_entry

            self._inc("misses")
            self._emit(
                "cache_miss",
                reason="fuzzy_no_match",
                best_score=round(best_score, 4) if best_score >= 0 else None,
            )
            return None

        except Exception as exc:
            logger.warning("llm_cache.get_fuzzy failed: %s", exc)
            self._inc("fuzzy_unavailable")
            self._emit("fuzzy_cache_unavailable", reason=str(exc))
            return None

    def set(
        self,
        key: str,
        response_data: dict[str, Any],
        ttl: int | None = None,
        cache_text: str = "",
    ) -> None:
        """Write a response to the cache.

        Args:
            key: Cache key from cache_key().
            response_data: Must include at least 'response'.
                           May include 'tokens_used', 'model', etc.
            ttl: Time-to-live in seconds (default: 24h).
            cache_text: Combined prompt text for fuzzy matching (optional).
        """
        if self._redis is None:
            return

        effective_ttl = ttl if ttl is not None else _DEFAULT_TTL

        entry = {
            **response_data,
            "timestamp": time.time(),
            "ttl": effective_ttl,
        }
        if cache_text and _FUZZY_ENABLED:
            entry["_cache_text"] = cache_text

        try:
            raw = json.dumps(entry, default=str, separators=(",", ":"))
            self._redis.setex(key, effective_ttl, raw)
            self._inc("writes")
            self._emit("cache_write", key=key, ttl=effective_ttl)
        except Exception as exc:
            logger.warning("llm_cache.set failed: %s", exc)

    def invalidate(self, pattern: str = "") -> int:
        """Delete cache entries matching a glob pattern.

        Args:
            pattern: Redis glob (e.g. '*'). If empty, clears ALL cache entries.

        Returns:
            Number of keys deleted.
        """
        if self._redis is None:
            return 0

        full_pattern = f"{_KEY_PREFIX}{pattern}" if pattern else f"{_KEY_PREFIX}*"
        deleted = 0

        try:
            cursor = 0
            while True:
                cursor, keys = self._redis.scan(cursor=cursor, match=full_pattern, count=200)
                if keys:
                    # Don't delete the stats key
                    cache_keys = [k for k in keys if k != _STATS_KEY.encode()]
                    if cache_keys:
                        deleted += self._redis.delete(*cache_keys)
                if cursor == 0:
                    break
        except Exception as exc:
            logger.warning("llm_cache.invalidate failed: %s", exc)

        return deleted

    def stats(self) -> CacheStats:
        """Return aggregated cache statistics."""
        s = CacheStats(
            hits=self._local_stats.hits,
            misses=self._local_stats.misses,
            writes=self._local_stats.writes,
            bypasses=self._local_stats.bypasses,
            fuzzy_hits=self._local_stats.fuzzy_hits,
            fuzzy_unavailable=self._local_stats.fuzzy_unavailable,
        )

        if self._redis is not None:
            try:
                # Count cache entries (exclude stats key)
                cursor = 0
                count = 0
                while True:
                    cursor, keys = self._redis.scan(cursor=cursor, match=f"{_KEY_PREFIX}*", count=500)
                    count += len(keys)
                    if cursor == 0:
                        break
                s.total_entries = count

                # Estimate memory usage
                info = self._redis.info("memory")
                s.memory_bytes = info.get("used_memory", 0)
            except Exception as exc:
                logger.debug("llm_cache.stats redis error: %s", exc)

        return s

    # -- Internal helpers -----------------------------------------------------

    def _inc(self, stat_name: str) -> None:
        """Thread-safe increment of a local stats counter."""
        with self._lock:
            current = getattr(self._local_stats, stat_name, 0)
            setattr(self._local_stats, stat_name, current + 1)

    def _emit(self, event_type: str, **data: Any) -> None:
        """Emit a structured log event (best-effort)."""
        try:
            from utils.structured_log import slog

            slog.event(f"llm_cache.{event_type}", **data)
        except Exception:  # noqa: S110
            pass  # structured logging is best-effort, never block cache ops


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

llm_cache = LLMCache()


# ---------------------------------------------------------------------------
# High-level wrapper
# ---------------------------------------------------------------------------


def cached_llm_call(
    call_fn: Callable[[], Any],
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.0,
    ttl: int | None = None,
    cache_bypass: bool = False,
    extract_response: Callable[[Any], str] | None = None,
    extract_tokens: Callable[[Any], int] | None = None,
) -> dict[str, Any]:
    """Execute an LLM call with automatic caching.

    Args:
        call_fn: Zero-arg callable that makes the actual LLM API call.
        model: Model identifier string.
        system_prompt: System prompt text.
        user_prompt: User prompt text.
        temperature: Sampling temperature.
        ttl: Cache TTL override in seconds.
        cache_bypass: If True, skip cache lookup and force a fresh call.
        extract_response: Optional fn to extract text from call_fn result.
        extract_tokens: Optional fn to extract token count from call_fn result.

    Returns:
        Dict with keys: response, tokens_used, model, cached, source.
    """
    key = llm_cache.cache_key(model, system_prompt, user_prompt, temperature)

    # --- Bypass mode ---
    if cache_bypass:
        llm_cache._inc("bypasses")
        llm_cache._emit("cache_bypass", key=key)
    else:
        # --- Exact match ---
        cached = llm_cache.get(key)
        if cached is not None:
            return {
                "response": cached.get("response", ""),
                "tokens_used": cached.get("tokens_used", 0),
                "model": cached.get("model", model),
                "cached": True,
                "source": "exact",
            }

        # --- Fuzzy match (guarded) ---
        fuzzy = llm_cache.get_fuzzy(model, system_prompt, user_prompt, temperature)
        if fuzzy is not None:
            return {
                "response": fuzzy.get("response", ""),
                "tokens_used": fuzzy.get("tokens_used", 0),
                "model": fuzzy.get("model", model),
                "cached": True,
                "source": "fuzzy",
            }

    # --- Cache miss: make the real call ---
    raw_result = call_fn()

    # Extract response text
    if extract_response is not None:
        response_text = extract_response(raw_result)
    elif isinstance(raw_result, str):
        response_text = raw_result
    elif isinstance(raw_result, dict):
        response_text = raw_result.get("response", str(raw_result))
    else:
        response_text = str(raw_result)

    # Extract token count
    if extract_tokens is not None:
        tokens_used = extract_tokens(raw_result)
    elif isinstance(raw_result, dict):
        tokens_used = raw_result.get("tokens_used", 0)
    else:
        tokens_used = 0

    # --- Write to cache ---
    cache_text = f"{system_prompt}\n{user_prompt}"
    llm_cache.set(
        key,
        {"response": response_text, "tokens_used": tokens_used, "model": model},
        ttl=ttl,
        cache_text=cache_text,
    )

    # --- Trace via Langfuse (best-effort) ---
    try:
        from utils.langfuse_tracing import trace_llm_call

        trace_llm_call(
            name="cached_llm_call",
            input_text=user_prompt,
            output_text=response_text,
            model=model,
            metadata={"cache": "miss", "tokens_used": tokens_used},
        )
    except Exception:  # noqa: S110
        pass  # Langfuse tracing is best-effort

    return {
        "response": response_text,
        "tokens_used": tokens_used,
        "model": model,
        "cached": False,
        "source": "api",
    }
