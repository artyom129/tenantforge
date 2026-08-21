from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.infrastructure.redis import get_redis

router = APIRouter(prefix="/health", tags=["Health"])
Session = Annotated[AsyncSession, Depends(get_session, scope="function")]
RedisConnection = Annotated[Redis, Depends(get_redis)]


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(
    response: Response,
    session: Session,
    redis: RedisConnection,
) -> dict[str, str]:
    checks = {"postgres": "ok", "redis": "ok"}
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        checks["postgres"] = "unavailable"
        try:
            await session.rollback()
        except Exception:
            checks["postgres"] = "unavailable"
    try:
        await redis.ping()
    except Exception:
        checks["redis"] = "unavailable"

    is_ready = all(value == "ok" for value in checks.values())
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if is_ready else "unavailable", **checks}
