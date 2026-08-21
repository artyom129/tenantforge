from redis.asyncio import Redis

from app.config import get_settings

redis_client = Redis.from_url(
    get_settings().redis_url,
    decode_responses=True,
    health_check_interval=30,
)


def get_redis() -> Redis:
    return redis_client


async def close_redis() -> None:
    await redis_client.aclose()

