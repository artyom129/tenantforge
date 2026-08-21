import math
import time
from dataclasses import dataclass

from redis.asyncio import Redis

from app.core.errors import AppError

_INCREMENT_WINDOW = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int


class FixedWindowRateLimiter:
    def __init__(self, redis: Redis, *, prefix: str = "tenantforge:rate-limit") -> None:
        self.redis = redis
        self.prefix = prefix

    async def check(
        self,
        identifier: str,
        limit: int,
        *,
        window_seconds: int = 60,
        now: float | None = None,
    ) -> RateLimitResult:
        if limit < 1 or window_seconds < 1:
            raise ValueError("limit and window_seconds must be positive")

        current_time = time.time() if now is None else now
        window = math.floor(current_time / window_seconds)
        redis_key = f"{self.prefix}:{identifier}:{window}"
        count = int(
            await self.redis.eval(
                _INCREMENT_WINDOW,
                1,
                redis_key,
                window_seconds + 1,
            )
        )
        retry_after = max(1, math.ceil((window + 1) * window_seconds - current_time))
        return RateLimitResult(
            allowed=count <= limit,
            limit=limit,
            remaining=max(0, limit - count),
            retry_after=retry_after,
        )

    async def enforce(
        self,
        identifier: str,
        limit: int,
        *,
        window_seconds: int = 60,
        now: float | None = None,
    ) -> RateLimitResult:
        result = await self.check(
            identifier,
            limit,
            window_seconds=window_seconds,
            now=now,
        )
        if not result.allowed:
            raise AppError(
                429,
                "rate_limit_exceeded",
                "Too many requests",
                {"Retry-After": str(result.retry_after)},
            )
        return result


async def enforce_rate_limit(
    redis: Redis,
    identifier: str,
    limit: int,
    *,
    window_seconds: int = 60,
    now: float | None = None,
) -> RateLimitResult:
    return await FixedWindowRateLimiter(redis).enforce(
        identifier,
        limit,
        window_seconds=window_seconds,
        now=now,
    )

