import asyncio
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.main import app
from app.models.audit import AuditLog
from app.models.identity import User
from app.models.tenancy import Invitation, Membership
from app.security.opaque_tokens import hash_token

PASSWORD = "correct-horse-battery-staple"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register(client: AsyncClient, email: str) -> tuple[str, str]:
    registration = await client.post(
        "/auth/register",
        json={"email": email, "password": PASSWORD},
    )
    assert registration.status_code == 201, registration.text
    login = await client.post(
        "/auth/login",
        json={"email": email, "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    return registration.json()["id"], login.json()["access_token"]


async def _create_organization(
    client: AsyncClient,
    token: str,
    *,
    name: str,
    slug: str,
) -> dict[str, Any]:
    response = await client.post(
        "/organizations",
        headers=_headers(token),
        json={"name": name, "slug": slug},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_project(
    client: AsyncClient,
    token: str,
    organization_id: str,
    *,
    name: str = "Victim Roadmap",
) -> dict[str, Any]:
    response = await client.post(
        f"/organizations/{organization_id}/projects",
        headers=_headers(token),
        json={"name": name, "description": "Private tenant plan"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_invitation(
    client: AsyncClient,
    owner_token: str,
    organization_id: str,
    *,
    email: str,
    role: str,
) -> dict[str, Any]:
    response = await client.post(
        f"/organizations/{organization_id}/invitations",
        headers=_headers(owner_token),
        json={"email": email, "role": role},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _add_member(
    client: AsyncClient,
    owner_token: str,
    organization_id: str,
    *,
    email: str,
    role: str,
) -> tuple[str, str, dict[str, Any]]:
    user_id, member_token = await _register(client, email)
    invitation = await _create_invitation(
        client,
        owner_token,
        organization_id,
        email=email,
        role=role,
    )
    response = await client.post(
        f"/invitations/{invitation['token']}/accept",
        headers=_headers(member_token),
    )
    assert response.status_code == 200, response.text
    return user_id, member_token, response.json()


ATTACK_CASES = (
    pytest.param("project_get", id="project-get"),
    pytest.param("project_patch", id="project-patch"),
    pytest.param("project_delete", id="project-delete"),
    pytest.param("audit_get", id="audit-get"),
    pytest.param("membership_patch", id="membership-patch"),
    pytest.param("membership_delete", id="membership-delete"),
    pytest.param("subscription_get", id="subscription-get"),
    pytest.param("usage_get", id="usage-get"),
)


@pytest.mark.parametrize("attack_case", ATTACK_CASES)
@pytest.mark.asyncio
async def test_user_a_cannot_access_or_mutate_org_b_resources(
    attack_case: str,
    client: AsyncClient,
) -> None:
    _, attacker_token = await _register(client, "attacker@example.com")
    await _create_organization(
        client,
        attacker_token,
        name="Attacker Organization",
        slug="attacker-org",
    )
    _, victim_token = await _register(client, "victim@example.com")
    victim_organization = await _create_organization(
        client,
        victim_token,
        name="Victim Organization",
        slug="victim-org",
    )
    organization_id = victim_organization["id"]
    victim_project = await _create_project(client, victim_token, organization_id)
    _, _, victim_member = await _add_member(
        client,
        victim_token,
        organization_id,
        email="victim-member@example.com",
        role="MEMBER",
    )

    project_url = f"/organizations/{organization_id}/projects/{victim_project['id']}"
    membership_url = (
        f"/organizations/{organization_id}/members/{victim_member['id']}"
    )
    requests: dict[str, tuple[str, str, dict[str, str] | None]] = {
        "project_get": ("GET", project_url, None),
        "project_patch": ("PATCH", project_url, {"name": "Compromised"}),
        "project_delete": ("DELETE", project_url, None),
        "audit_get": ("GET", f"/organizations/{organization_id}/audit-log", None),
        "membership_patch": ("PATCH", membership_url, {"role": "ADMIN"}),
        "membership_delete": ("DELETE", membership_url, None),
        "subscription_get": (
            "GET",
            f"/organizations/{organization_id}/subscription",
            None,
        ),
        "usage_get": ("GET", f"/organizations/{organization_id}/usage", None),
    }
    method, url, payload = requests[attack_case]
    request_kwargs = {} if payload is None else {"json": payload}

    attack = await client.request(
        method,
        url,
        headers=_headers(attacker_token),
        **request_kwargs,
    )

    assert attack.status_code == 404, attack.text
    assert attack.json()["error"]["code"] == "organization_not_found"

    project_after = await client.get(project_url, headers=_headers(victim_token))
    members_after = await client.get(
        f"/organizations/{organization_id}/members",
        headers=_headers(victim_token),
    )
    subscription_after = await client.get(
        f"/organizations/{organization_id}/subscription",
        headers=_headers(victim_token),
    )
    usage_after = await client.get(
        f"/organizations/{organization_id}/usage",
        headers=_headers(victim_token),
    )
    audit_after = await client.get(
        f"/organizations/{organization_id}/audit-log",
        headers=_headers(victim_token),
    )

    assert project_after.status_code == 200, project_after.text
    assert project_after.json()["name"] == "Victim Roadmap"
    assert members_after.status_code == 200, members_after.text
    stored_member = next(
        member for member in members_after.json() if member["id"] == victim_member["id"]
    )
    assert stored_member["role"] == "MEMBER"
    assert subscription_after.status_code == 200, subscription_after.text
    assert subscription_after.json()["organization_id"] == organization_id
    assert subscription_after.json()["plan"] == "FREE"
    assert usage_after.status_code == 200, usage_after.text
    assert usage_after.json()["used"] == 0
    assert audit_after.status_code == 200, audit_after.text
    assert audit_after.json()
    assert all(entry["organization_id"] == organization_id for entry in audit_after.json())


@pytest.mark.parametrize("role", ["MEMBER", "ADMIN"])
@pytest.mark.asyncio
async def test_member_and_admin_can_use_full_project_crud(
    role: str,
    client: AsyncClient,
) -> None:
    _, owner_token = await _register(client, "owner@example.com")
    organization = await _create_organization(
        client,
        owner_token,
        name=f"{role.title()} Project Organization",
        slug=f"{role.lower()}-project-org",
    )
    organization_id = organization["id"]
    actor_id, actor_token, _ = await _add_member(
        client,
        owner_token,
        organization_id,
        email=f"{role.lower()}@example.com",
        role=role,
    )

    created = await _create_project(
        client,
        actor_token,
        organization_id,
        name=f"{role.title()} Project",
    )
    project_url = f"/organizations/{organization_id}/projects/{created['id']}"
    listed = await client.get(
        f"/organizations/{organization_id}/projects",
        headers=_headers(actor_token),
    )
    fetched = await client.get(project_url, headers=_headers(actor_token))
    updated = await client.patch(
        project_url,
        headers=_headers(actor_token),
        json={"name": "Updated by member", "status": "ARCHIVED"},
    )
    deleted = await client.delete(project_url, headers=_headers(actor_token))
    missing = await client.get(project_url, headers=_headers(actor_token))

    assert created["created_by"] == actor_id
    assert listed.status_code == 200, listed.text
    assert [project["id"] for project in listed.json()] == [created["id"]]
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["id"] == created["id"]
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Updated by member"
    assert updated.json()["status"] == "ARCHIVED"
    assert deleted.status_code == 204, deleted.text
    assert missing.status_code == 404


@pytest.mark.skipif(
    os.environ.get("TEST_USE_EXTERNAL_SERVICES") != "1",
    reason="requires PostgreSQL row locking",
)
@pytest.mark.asyncio
async def test_concurrent_invitation_accept_has_one_winner(
    client: AsyncClient,
    session_factory,
) -> None:
    _, owner_token = await _register(client, "owner@example.com")
    organization = await _create_organization(
        client,
        owner_token,
        name="Concurrent Invitation Organization",
        slug="concurrent-invitation-org",
    )
    invitee_email = "invitee@example.com"
    _, invitee_token = await _register(client, invitee_email)
    invitation = await _create_invitation(
        client,
        owner_token,
        organization["id"],
        email=invitee_email,
        role="MEMBER",
    )
    endpoint = f"/invitations/{invitation['token']}/accept"
    organization_id = uuid.UUID(organization["id"])
    start = asyncio.Event()

    async def accept_once(request_client: AsyncClient):
        await start.wait()
        return await request_client.post(endpoint, headers=_headers(invitee_token))

    first_transport = ASGITransport(app=app, raise_app_exceptions=False)
    second_transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=first_transport, base_url="http://testserver") as first_client,
        AsyncClient(transport=second_transport, base_url="http://testserver") as second_client,
    ):
        first_task = asyncio.create_task(accept_once(first_client))
        second_task = asyncio.create_task(accept_once(second_client))
        start.set()
        first, second = await asyncio.gather(first_task, second_task)

    assert sorted((first.status_code, second.status_code)) == [200, 409]
    conflict = first if first.status_code == 409 else second
    assert conflict.json()["error"]["code"] in {"invitation_used", "already_member"}

    async with session_factory() as session:
        invitee = await session.scalar(select(User).where(User.email == invitee_email))
        stored_invitation = await session.scalar(
            select(Invitation).where(
                Invitation.token_hash == hash_token(invitation["token"])
            )
        )
        assert invitee is not None
        membership_count = await session.scalar(
            select(func.count())
            .select_from(Membership)
            .where(
                Membership.organization_id == organization_id,
                Membership.user_id == invitee.id,
            )
        )
        acceptance_audit_count = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.organization_id == organization_id,
                AuditLog.action == "invitation.accepted",
            )
        )

    assert membership_count == 1
    assert acceptance_audit_count == 1
    assert stored_invitation is not None
    assert stored_invitation.accepted_at is not None


@pytest.mark.skipif(
    os.environ.get("TEST_USE_EXTERNAL_SERVICES") != "1",
    reason="requires PostgreSQL row locking",
)
@pytest.mark.asyncio
async def test_concurrent_invitation_create_has_one_active_invitation(
    client: AsyncClient,
    session_factory,
) -> None:
    _, owner_token = await _register(client, "owner@example.com")
    organization = await _create_organization(
        client,
        owner_token,
        name="Concurrent Invitation Creation Organization",
        slug="concurrent-invitation-creation-org",
    )
    _, admin_token, _ = await _add_member(
        client,
        owner_token,
        organization["id"],
        email="admin@example.com",
        role="ADMIN",
    )
    invitee_email = "concurrent-invitee@example.com"
    endpoint = f"/organizations/{organization['id']}/invitations"
    start = asyncio.Event()

    async def invite_once(request_client: AsyncClient, actor_token: str):
        await start.wait()
        return await request_client.post(
            endpoint,
            headers=_headers(actor_token),
            json={"email": invitee_email, "role": "MEMBER"},
        )

    first_transport = ASGITransport(app=app, raise_app_exceptions=False)
    second_transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=first_transport, base_url="http://testserver") as first_client,
        AsyncClient(transport=second_transport, base_url="http://testserver") as second_client,
    ):
        first_task = asyncio.create_task(invite_once(first_client, owner_token))
        second_task = asyncio.create_task(invite_once(second_client, admin_token))
        start.set()
        first, second = await asyncio.gather(first_task, second_task)

    assert sorted((first.status_code, second.status_code)) == [201, 409]
    conflict = first if first.status_code == 409 else second
    assert conflict.json()["error"]["code"] == "invitation_exists"

    async with session_factory() as session:
        active_count = await session.scalar(
            select(func.count())
            .select_from(Invitation)
            .where(
                Invitation.organization_id == uuid.UUID(organization["id"]),
                func.lower(Invitation.email) == invitee_email,
                Invitation.accepted_at.is_(None),
                Invitation.expires_at > datetime.now(UTC),
            )
        )
    assert active_count == 1
