import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Membership, Organization, Subscription, User
from app.models.enums import Plan, Role
from tests.helpers import (
    auth_headers,
    create_organization,
    register_and_login,
)

pytestmark = pytest.mark.asyncio


async def add_membership(
    session: AsyncSession,
    *,
    email: str,
    organization_id: str,
    role: Role,
) -> Membership:
    user = await session.scalar(select(User).where(User.email == email))
    assert user is not None
    membership = Membership(
        user_id=user.id,
        organization_id=uuid.UUID(organization_id),
        role=role,
    )
    session.add(membership)
    await session.commit()
    return membership


async def test_create_organization_persists_tenant(client: AsyncClient, db_session: AsyncSession):
    tokens = await register_and_login(client)

    response = await client.post(
        "/organizations",
        headers=auth_headers(tokens["access_token"]),
        json={"name": "Acme Labs", "slug": "acme-labs"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Acme Labs"
    assert body["slug"] == "acme-labs"
    organization = await db_session.get(Organization, uuid.UUID(body["id"]))
    assert organization is not None


async def test_creator_gets_owner_membership_and_free_subscription(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    tokens = await register_and_login(client, email="founder@example.test")
    organization = await create_organization(client, tokens["access_token"])

    membership = await db_session.scalar(
        select(Membership).where(
            Membership.organization_id == uuid.UUID(organization["id"])
        )
    )
    subscription = await db_session.scalar(
        select(Subscription).where(
            Subscription.organization_id == uuid.UUID(organization["id"])
        )
    )

    assert membership is not None and membership.role == Role.OWNER
    assert subscription is not None and subscription.plan == Plan.FREE
    assert subscription.current_period_end > subscription.current_period_start


async def test_list_organizations_returns_only_current_users_tenants(
    client: AsyncClient,
) -> None:
    owner = await register_and_login(client, email="first@example.test")
    first = await create_organization(
        client,
        owner["access_token"],
        name="First Org",
        slug="first-org",
    )
    second = await create_organization(
        client,
        owner["access_token"],
        name="Second Org",
        slug="second-org",
    )
    outsider = await register_and_login(client, email="outsider@example.test")
    await create_organization(
        client,
        outsider["access_token"],
        name="Hidden Org",
        slug="hidden-org",
    )

    response = await client.get(
        "/organizations",
        headers=auth_headers(owner["access_token"]),
    )

    assert response.status_code == 200
    assert {item["id"] for item in response.json()} == {first["id"], second["id"]}


async def test_get_organization_returns_members_tenant(client: AsyncClient) -> None:
    tokens = await register_and_login(client)
    organization = await create_organization(client, tokens["access_token"])

    response = await client.get(
        f"/organizations/{organization['id']}",
        headers=auth_headers(tokens["access_token"]),
    )

    assert response.status_code == 200
    assert response.json() == organization


async def test_get_organization_hides_tenant_from_non_member(client: AsyncClient) -> None:
    owner = await register_and_login(client, email="tenant-owner@example.test")
    organization = await create_organization(client, owner["access_token"])
    outsider = await register_and_login(client, email="tenant-outsider@example.test")

    response = await client.get(
        f"/organizations/{organization['id']}",
        headers=auth_headers(outsider["access_token"]),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "organization_not_found"


async def test_owner_can_update_organization(client: AsyncClient) -> None:
    tokens = await register_and_login(client)
    organization = await create_organization(client, tokens["access_token"])

    response = await client.patch(
        f"/organizations/{organization['id']}",
        headers=auth_headers(tokens["access_token"]),
        json={"name": "Acme Platform", "slug": "acme-platform"},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Acme Platform"
    assert response.json()["slug"] == "acme-platform"


async def test_admin_can_update_organization(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    owner = await register_and_login(client, email="owner@example.test")
    organization = await create_organization(client, owner["access_token"])
    admin = await register_and_login(client, email="admin@example.test")
    await add_membership(
        db_session,
        email="admin@example.test",
        organization_id=organization["id"],
        role=Role.ADMIN,
    )

    response = await client.patch(
        f"/organizations/{organization['id']}",
        headers=auth_headers(admin["access_token"]),
        json={"name": "Admin Updated"},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Admin Updated"


async def test_member_cannot_update_organization(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    owner = await register_and_login(client, email="owner@example.test")
    organization = await create_organization(client, owner["access_token"])
    member = await register_and_login(client, email="member@example.test")
    await add_membership(
        db_session,
        email="member@example.test",
        organization_id=organization["id"],
        role=Role.MEMBER,
    )

    response = await client.patch(
        f"/organizations/{organization['id']}",
        headers=auth_headers(member["access_token"]),
        json={"name": "Forbidden Update"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_owner_can_delete_organization(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    tokens = await register_and_login(client)
    organization = await create_organization(client, tokens["access_token"])

    response = await client.delete(
        f"/organizations/{organization['id']}",
        headers=auth_headers(tokens["access_token"]),
    )

    assert response.status_code == 204
    assert response.content == b""
    db_session.expire_all()
    assert await db_session.get(Organization, uuid.UUID(organization["id"])) is None


async def test_admin_cannot_delete_organization(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    owner = await register_and_login(client, email="owner@example.test")
    organization = await create_organization(client, owner["access_token"])
    admin = await register_and_login(client, email="admin@example.test")
    await add_membership(
        db_session,
        email="admin@example.test",
        organization_id=organization["id"],
        role=Role.ADMIN,
    )

    response = await client.delete(
        f"/organizations/{organization['id']}",
        headers=auth_headers(admin["access_token"]),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_create_organization_rejects_duplicate_slug(client: AsyncClient) -> None:
    tokens = await register_and_login(client)
    await create_organization(client, tokens["access_token"])

    response = await client.post(
        "/organizations",
        headers=auth_headers(tokens["access_token"]),
        json={"name": "Another Acme", "slug": "acme-labs"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "slug_taken"


async def test_update_organization_rejects_slug_owned_by_another_tenant(
    client: AsyncClient,
) -> None:
    tokens = await register_and_login(client)
    await create_organization(
        client,
        tokens["access_token"],
        name="First",
        slug="first",
    )
    second = await create_organization(
        client,
        tokens["access_token"],
        name="Second",
        slug="second",
    )

    response = await client.patch(
        f"/organizations/{second['id']}",
        headers=auth_headers(tokens["access_token"]),
        json={"slug": "first"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "slug_taken"


async def test_create_organization_requires_authentication(client: AsyncClient) -> None:
    response = await client.post(
        "/organizations",
        json={"name": "Anonymous", "slug": "anonymous"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


async def test_create_organization_validates_slug(client: AsyncClient) -> None:
    tokens = await register_and_login(client)

    response = await client.post(
        "/organizations",
        headers=auth_headers(tokens["access_token"]),
        json={"name": "Invalid Slug", "slug": "Invalid Slug"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
