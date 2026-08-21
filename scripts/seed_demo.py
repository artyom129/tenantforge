import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Membership, Organization, Project, Subscription, User
from app.models.enums import Plan, ProjectStatus, Role, SubscriptionStatus
from app.security.passwords import hash_password

DEMO_PASSWORD = "tenantforge-demo"
DEMO_USERS = (
    ("owner@tenantforge.local", Role.OWNER),
    ("admin@tenantforge.local", Role.ADMIN),
    ("member@tenantforge.local", Role.MEMBER),
)
DEMO_PROJECTS = (
    ("Public API", "Customer-facing API surface and integrations."),
    ("Usage Dashboard", "Monthly usage and quota reporting."),
    ("Billing Adapter", "Development billing workflow and webhook handling."),
)


async def get_or_create_user(session, email: str) -> User:
    user = await session.scalar(select(User).where(User.email == email))
    if user is not None:
        return user

    user = User(
        email=email,
        password_hash=hash_password(DEMO_PASSWORD),
        is_active=True,
        is_verified=True,
    )
    session.add(user)
    await session.flush()
    return user


async def seed() -> None:
    async with SessionLocal() as session, session.begin():
        users: dict[Role, User] = {}
        for email, role in DEMO_USERS:
            users[role] = await get_or_create_user(session, email)

        organization = await session.scalar(
            select(Organization).where(Organization.slug == "acme-labs")
        )
        if organization is None:
            organization = Organization(name="Acme Labs", slug="acme-labs")
            session.add(organization)
            await session.flush()

        for _, role in DEMO_USERS:
            membership = await session.scalar(
                select(Membership).where(
                    Membership.organization_id == organization.id,
                    Membership.user_id == users[role].id,
                )
            )
            if membership is None:
                session.add(
                    Membership(
                        organization_id=organization.id,
                        user_id=users[role].id,
                        role=role,
                    )
                )

        owner = users[Role.OWNER]
        for name, description in DEMO_PROJECTS:
            project = await session.scalar(
                select(Project).where(
                    Project.organization_id == organization.id,
                    Project.name == name,
                )
            )
            if project is None:
                session.add(
                    Project(
                        organization_id=organization.id,
                        name=name,
                        description=description,
                        status=ProjectStatus.ACTIVE,
                        created_by=owner.id,
                    )
                )

        subscription = await session.scalar(
            select(Subscription).where(Subscription.organization_id == organization.id)
        )
        if subscription is None:
            period_start = datetime.now(UTC)
            session.add(
                Subscription(
                    organization_id=organization.id,
                    plan=Plan.PRO,
                    status=SubscriptionStatus.ACTIVE,
                    current_period_start=period_start,
                    current_period_end=period_start + timedelta(days=30),
                )
            )
        else:
            subscription.plan = Plan.PRO
            subscription.status = SubscriptionStatus.ACTIVE

    print("Demo data is ready for Acme Labs (slug: acme-labs).")
    print("Development seed accounts: owner, admin, and member @tenantforge.local.")
    print("The shared development seed password is documented in README.md.")


if __name__ == "__main__":
    asyncio.run(seed())
