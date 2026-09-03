"""Redis-backed distributed rate limiting with a bounded local fallback.

Why this shape
--------------
Multiple API replicas share one limiter so that a client making 60 req/min
against *each* of three replicas still sees one 60 req/min budget. The window is
implemented in Redis with ``INCR`` + ``PEXPIRE`` inside a Lua script, which makes
the increment-and-expire atomic and removes the classic race where a process
crashes between the two commands and leaves a key without a TTL (a permanent
self-inflicted denial of service).

Redis is a *dependency of politeness*, not of availability: when Redis is
unreachable the limiter falls back to a bounded in-process window (documented as
such in every response header — no pretending) when
``RATE_LIMIT_FAIL_OPEN=true``, and rejects with 503 when it is false. The
read API itself never becomes unavailable merely because Redis is down.

Policy
------
Three technical tiers, all environment-configurable::

    anonymous      per client IP, before authentication
    authenticated  per API key (``read:*`` scopes)
    admin          per API key carrying the ``admin`` scope

No commercial pricing tiers exist yet, and none are invented here: a future
plan system maps a key to a policy, it does not change this algorithm.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Protocol

from app.config import Settings, get_settings
from app.logging import get_logger

logger = get_logger("tenderbase.rate_limit")

#: Atomic "increment then (first hit) set expiry, and read back the TTL" script.
#: KEYS[1] = bucket key. ARGV[1] = window in milliseconds.
_LUA_WINDOW = """
local hits = redis.call('INCR', KEYS[1])
if hits == 1 then
  redis.call('PEXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('PTTL', KEYS[1])
if ttl < 0 then
  redis.call('PEXPIRE', KEYS[1], ARGV[1])
  ttl = ARGV[1]
end
return {hits, ttl}
"""

WINDOW_SECONDS = 60


@dataclass(slots=True)
class RateDecision:
    """Outcome of one limiter check."""

    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int
    #: "redis" | "in-process" | "disabled" | "fail-closed"
    backend: str = "disabled"

    def headers(self) -> dict[str, str]:
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(self.remaining, 0)),
            "X-RateLimit-Reset": str(int(time.time()) + self.retry_after_seconds),
            "X-RateLimit-Policy": self.backend,
        }
        if not self.allowed:
            headers["Retry-After"] = str(max(self.retry_after_seconds, 1))
        return headers


class RateLimiter(Protocol):
    """Interface implemented by every backend."""

    name: str = "abstract"

    async def check(self, bucket: str, *, limit: int, burst: int) -> RateDecision: ...
    async def close(self) -> None: ...
    async def healthy(self) -> bool: ...


class InProcessWindowLimiter:
    """Fixed-window counter kept in this process only.

    Used for single-process deployments, tests, and as the Redis outage
    fallback. Bounded: entries beyond ``max_entries`` are evicted oldest-first
    so a flood of distinct client IPs cannot exhaust the API's memory.
    """

    name = "in-process"

    def __init__(self, *, max_entries: int = 10_000) -> None:
        self._counters: OrderedDict[str, tuple[int, float]] = OrderedDict()
        self.max_entries = max_entries

    def _bucket_for(self, bucket: str) -> tuple[int, float]:
        window = int(time.time() // WINDOW_SECONDS)
        entry = self._counters.get(bucket)
        if entry is None or entry[1] != window:
            if len(self._counters) >= self.max_entries:
                # Evict the oldest window (FIFO) rather than growing unbounded.
                self._counters.popitem(last=False)
            self._counters[bucket] = (0, window)
            return 0, window
        return entry

    async def check(self, bucket: str, *, limit: int, burst: int) -> RateDecision:
        hits, window = self._bucket_for(bucket)
        hits += 1
        self._counters[bucket] = (hits, window)
        allowed_until = limit + burst
        remaining = allowed_until - hits
        reset_in = int((window + 1) * WINDOW_SECONDS - time.time())
        return RateDecision(
            allowed=hits <= allowed_until,
            limit=allowed_until,
            remaining=remaining,
            retry_after_seconds=max(reset_in, 1),
            backend=self.name,
        )

    async def close(self) -> None:
        self._counters.clear()

    async def healthy(self) -> bool:
        return True


class RedisWindowLimiter:
    """Distributed fixed-window limiter executed as an atomic Lua script."""

    name = "redis"

    def __init__(self, client: Any, *, namespace: str = "tenderbase:ratelimit") -> None:
        self.client = client
        self.namespace = namespace
        self._script = (
            client.register_script(_LUA_WINDOW) if hasattr(client, "register_script") else None
        )

    @classmethod
    def from_url(cls, url: str, *, namespace: str = "tenderbase:ratelimit") -> RedisWindowLimiter:
        import redis.asyncio as aioredis

        client = aioredis.from_url(url, encoding="utf-8", decode_responses=True)
        return cls(client, namespace=namespace)

    async def check(self, bucket: str, *, limit: int, burst: int) -> RateDecision:
        window = int(time.time() // WINDOW_SECONDS)
        key = f"{self.namespace}:{window}:{bucket}"
        allowed_until = limit + burst
        hits, ttl_ms = await self._execute(key)
        remaining = allowed_until - int(hits)
        retry_after = max(int(int(ttl_ms) / 1000), 1) if int(ttl_ms) > 0 else WINDOW_SECONDS
        return RateDecision(
            allowed=int(hits) <= allowed_until,
            limit=allowed_until,
            remaining=remaining,
            retry_after_seconds=retry_after,
            backend=self.name,
        )

    async def _execute(self, key: str) -> tuple[int, int]:
        if self._script is not None:
            result = await self._script(keys=[key], args=[WINDOW_SECONDS * 1000])
        else:  # pragma: no cover - only for clients without script support
            hits = await self.client.incr(key)
            if hits == 1:
                await self.client.pexpire(key, WINDOW_SECONDS * 1000)
            ttl = await self.client.pttl(key)
            result = [hits, ttl]
        hits, ttl = result[0], result[1]
        # A Redis that drops our write (eviction under maxmemory) must not
        # silently disable limiting: fall back to a full window.
        if int(hits) <= 0:
            return 1, WINDOW_SECONDS * 1000
        return int(hits), int(ttl)

    async def close(self) -> None:
        try:
            await self.client.aclose()
        except AttributeError:  # pragma: no cover - redis-py < 5
            await self.client.close()
        except Exception as exc:  # noqa: BLE001 - shutdown is best-effort
            logger.warning("rate_limit.close_failed", error=str(exc))

    async def healthy(self) -> bool:
        try:
            return bool(await self.client.ping())
        except Exception:  # noqa: BLE001
            return False


class ResilientRateLimiter:
    """Redis first, local fallback when Redis misbehaves.

    The fallback is *deliberately visible*: decisions carry
    ``backend="in-process (redis unavailable)"`` so operators can tell from a
    response header that enforcement became per-replica rather than global.
    """

    name = "redis+fallback"

    def __init__(
        self,
        primary: RateLimiter | None,
        fallback: RateLimiter,
        *,
        fail_open: bool = True,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.fail_open = fail_open

    async def check(self, bucket: str, *, limit: int, burst: int) -> RateDecision:
        if self.primary is not None:
            try:
                return await self.primary.check(bucket, limit=limit, burst=burst)
            except Exception as exc:  # noqa: BLE001 - Redis outage is not fatal
                logger.error("rate_limit.redis_unavailable", error=str(exc))
                if not self.fail_open:
                    raise
        decision = await self.fallback.check(bucket, limit=limit, burst=burst)
        decision.backend = "in-process (redis unavailable)"
        return decision

    async def close(self) -> None:
        for limiter in (self.primary, self.fallback):
            if limiter is not None:
                try:
                    await limiter.close()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("rate_limit.close_failed", error=str(exc))

    async def healthy(self) -> bool:
        if self.primary is None:
            return False
        try:
            return await self.primary.healthy()
        except Exception:  # noqa: BLE001
            return False


def policy_for(bucket_class: str, settings: Settings | None = None) -> tuple[int, int]:
    """Return ``(sustained_per_minute, burst)`` for a tier name."""
    cfg = settings or get_settings()
    limits = {
        "anonymous": cfg.rate_limit_anonymous_per_minute,
        "authenticated": cfg.rate_limit_authenticated_per_minute,
        "admin": cfg.rate_limit_admin_per_minute,
    }
    return limits.get(bucket_class, cfg.rate_limit_anonymous_per_minute), cfg.rate_limit_burst


_limiter: ResilientRateLimiter | None = None


async def build_limiter(settings: Settings | None = None) -> ResilientRateLimiter:
    """Create the process-wide limiter, using Redis when it answers."""
    cfg = settings or get_settings()
    fallback = InProcessWindowLimiter(max_entries=cfg.rate_limit_fallback_max_entries)
    primary: RateLimiter | None = None
    try:
        candidate = RedisWindowLimiter.from_url(cfg.redis_url)
        if await candidate.healthy():
            primary = candidate
        else:
            await candidate.close()
            logger.warning("rate_limit.redis_unhealthy_using_local_fallback")
    except Exception as exc:  # noqa: BLE001 - any Redis surprise degrades, never aborts
        logger.warning("rate_limit.redis_connect_failed", error=str(exc))
    return ResilientRateLimiter(primary, fallback, fail_open=cfg.rate_limit_fail_open)


def get_limiter() -> ResilientRateLimiter | None:
    """The limiter installed by the application lifespan (``None`` if disabled)."""
    return _limiter


def install_limiter(limiter: ResilientRateLimiter | None) -> ResilientRateLimiter | None:
    global _limiter
    _limiter = limiter
    return limiter
