"""Fixed-window rate limiter.

Two interchangeable backends:

* ``MemoryLimiter`` — per-process, used in tests and single-replica dev.
* ``RedisLimiter`` — shared across replicas in production, atomic via Lua so a
  burst cannot slip between the INCR and the EXPIRE.

Keys are namespaced per scope (``auth:<ip>``, ``api:<user_id>``,
``meta:<account_id>``) so one noisy tenant cannot exhaust another's budget.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.core.config import settings
from app.core.exceptions import RateLimitExceeded

_REDIS_LUA = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('PEXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('PTTL', KEYS[1])
return {current, ttl}
"""


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_after_seconds: float

    @property
    def headers(self) -> dict[str, str]:
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(self.remaining, 0)),
            "X-RateLimit-Reset": str(int(time.time()) + max(int(self.reset_after_seconds), 1)),
            **({"Retry-After": str(max(int(self.reset_after_seconds), 1))} if not self.allowed else {}),
        }


class BaseLimiter:
    async def hit(self, key: str, *, limit: int, window_seconds: int = 60) -> RateLimitResult:
        raise NotImplementedError

    async def reset(self, key: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class MemoryLimiter(BaseLimiter):
    def __init__(self) -> None:
        self._buckets: dict[str, tuple[int, float]] = {}

    async def hit(self, key: str, *, limit: int, window_seconds: int = 60) -> RateLimitResult:
        now = time.monotonic()
        count, window_start = self._buckets.get(key, (0, now))
        if now - window_start >= window_seconds:
            count, window_start = 0, now
        count += 1
        self._buckets[key] = (count, window_start)
        remaining = max(limit - count, 0)
        reset_after = max(window_seconds - (now - window_start), 0)
        return RateLimitResult(
            allowed=count <= limit,
            limit=limit,
            remaining=remaining,
            reset_after_seconds=reset_after,
        )

    async def reset(self, key: str) -> None:
        self._buckets.pop(key, None)


class RedisLimiter(BaseLimiter):
    def __init__(self, redis_client: object) -> None:
        self._redis = redis_client
        self._script = None

    async def hit(self, key: str, *, limit: int, window_seconds: int = 60) -> RateLimitResult:
        import redis.asyncio as aioredis  # local import: optional dependency

        redis = self._redis
        assert isinstance(redis, aioredis.Redis)
        full_key = f"rl:{key}"
        if self._script is None:
            self._script = redis.register_script(_REDIS_LUA)
        current, ttl = await self._script(
            keys=[full_key], args=[window_seconds * 1000]
        )
        current = int(current)
        ttl_ms = int(ttl) if ttl and ttl > 0 else window_seconds * 1000
        return RateLimitResult(
            allowed=current <= limit,
            limit=limit,
            remaining=max(limit - current, 0),
            reset_after_seconds=ttl_ms / 1000,
        )

    async def reset(self, key: str) -> None:  # pragma: no cover - thin wrapper
        await self._redis.delete(f"rl:{key}")  # type: ignore[attr-defined]


_limiter: BaseLimiter | None = None


def get_limiter() -> BaseLimiter:
    global _limiter
    if _limiter is None:
        _limiter = MemoryLimiter() if settings.RATE_LIMIT_STORAGE == "memory" else _build_redis_limiter()
    return _limiter


def set_limiter(limiter: BaseLimiter) -> None:
    """Override the limiter (tests / custom backends)."""
    global _limiter
    _limiter = limiter


def reset_limiter() -> None:
    global _limiter
    _limiter = None


def _build_redis_limiter() -> BaseLimiter:  # pragma: no cover - needs redis
    import redis.asyncio as aioredis

    return RedisLimiter(aioredis.from_url(settings.REDIS_URL, decode_responses=True))


async def enforce_rate_limit(
    scope: str,
    identifier: str,
    *,
    limit: int,
    window_seconds: int = 60,
    error_detail: str | None = None,
) -> RateLimitResult:
    """Apply a limit and raise :class:`RateLimitExceeded` when exceeded."""
    if not settings.RATE_LIMIT_ENABLED:
        return RateLimitResult(True, limit, limit, window_seconds)
    result = await get_limiter().hit(f"{scope}:{identifier}", limit=limit, window_seconds=window_seconds)
    if not result.allowed:
        raise RateLimitExceeded(
            error_detail or "Too many requests. Please slow down.",
            headers=result.headers,
            context={"scope": scope, "limit": limit},
        )
    return result
