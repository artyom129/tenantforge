import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

USE_EXTERNAL_SERVICES = os.environ.get("TEST_USE_EXTERNAL_SERVICES") == "1"
os.environ.setdefault("APP_ENV", "test")
if not USE_EXTERNAL_SERVICES:
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"
    os.environ["REDIS_URL"] = "redis://localhost:6379/15"
os.environ.setdefault("JWT_SECRET", "tenantforge-test-jwt-secret-at-least-32-bytes")
os.environ.setdefault("BILLING_WEBHOOK_SECRET", "tenantforge-test-billing-secret")
os.environ.setdefault("USER_RATE_LIMIT_PER_MINUTE", "1000")
os.environ.setdefault("API_RATE_LIMIT_PER_MINUTE", "1000")

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

import app.models  # noqa: E402, F401
from app.database import Base, get_session  # noqa: E402
from app.infrastructure.redis import get_redis  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Membership, Organization, Subscription, User  # noqa: E402
from app.models.enums import Plan, Role, SubscriptionStatus  # noqa: E402
from app.security.passwords import hash_password  # noqa: E402
from tests.helpers import DEFAULT_PASSWORD  # noqa: E402

SessionFactory = async_sessionmaker[AsyncSession]
UserFactory = Callable[..., Awaitable[User]]
OrganizationFactory = Callable[..., Awaitable[tuple[Organization, Membership]]]


class InMemoryRedis(FakeRedis):
    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> int:
        if numkeys != 1 or len(keys_and_args) != 2:
            raise ValueError("The test Redis supports the rate-limit script contract")
        key, raw_ttl = keys_and_args
        count = int(await self.incr(str(key)))
        if count == 1:
            await self.expire(str(key), int(raw_ttl))
        return count


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[SessionFactory]:
    if USE_EXTERNAL_SERVICES:
        engine = create_async_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
        async with engine.begin() as connection:
            await connection.exec_driver_sql(_truncate_statement())
    else:
        engine = create_async_engine(
            "sqlite+aiosqlite://",
            poolclass=StaticPool,
        )
        async with engine.begin() as connection:
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory

    async with engine.begin() as connection:
        if USE_EXTERNAL_SERVICES:
            await connection.exec_driver_sql(_truncate_statement())
        else:
            await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()


def _truncate_statement() -> str:
    tables = ", ".join(f'"{table.name}"' for table in reversed(Base.metadata.sorted_tables))
    return f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"


@pytest_asyncio.fixture
async def db_session(session_factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[Redis]:
    redis = (
        Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
        if USE_EXTERNAL_SERVICES
        else InMemoryRedis(decode_responses=True)
    )
    await redis.flushdb()
    yield redis
    await redis.flushdb()
    await redis.aclose()


@pytest_asyncio.fixture
async def client(
    session_factory: SessionFactory,
    redis_client: Redis,
) -> AsyncIterator[AsyncClient]:
    async def test_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_session] = test_session
    app.dependency_overrides[get_redis] = lambda: redis_client
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def user_factory(db_session: AsyncSession) -> UserFactory:
    async def create_user(
        *,
        email: str | None = None,
        password: str = DEFAULT_PASSWORD,
        is_active: bool = True,
        is_verified: bool = False,
    ) -> User:
        user = User(
            email=email or f"user-{uuid.uuid4().hex}@example.test",
            password_hash=hash_password(password),
            is_active=is_active,
            is_verified=is_verified,
        )
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)
        return user

    return create_user


@pytest.fixture
def organization_factory(
    db_session: AsyncSession,
    user_factory: UserFactory,
) -> OrganizationFactory:
    async def create_organization(
        *,
        owner: User | None = None,
        name: str = "Factory Organization",
        slug: str | None = None,
        role: Role = Role.OWNER,
    ) -> tuple[Organization, Membership]:
        owner = owner or await user_factory()
        organization = Organization(
            name=name,
            slug=slug or f"org-{uuid.uuid4().hex}",
        )
        db_session.add(organization)
        await db_session.flush()
        membership = Membership(
            user_id=owner.id,
            organization_id=organization.id,
            role=role,
        )
        period_start = datetime.now(UTC)
        subscription = Subscription(
            organization_id=organization.id,
            plan=Plan.FREE,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=period_start,
            current_period_end=period_start + timedelta(days=30),
        )
        db_session.add_all((membership, subscription))
        await db_session.commit()
        await db_session.refresh(organization)
        await db_session.refresh(membership)
        return organization, membership

    return create_organization
