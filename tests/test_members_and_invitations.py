import logging
import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select

from app.api.deps import get_invitation_email_sender
from app.infrastructure.queue import DEFAULT_QUEUE, Job
from app.main import app
from app.models.audit import AuditLog
from app.models.enums import Role
from app.models.tenancy import Invitation
from app.security.opaque_tokens import hash_token
from app.services.email import InvitationDeliveryError

PASSWORD = "correct-horse-battery-staple"


async def _register(client: AsyncClient, email: str) -> str:
    response = await client.post(
        "/auth/register",
        json={"email": email, "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    response = await client.post(
        "/auth/login",
        json={"email": email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _organization(client: AsyncClient, token: str, slug: str = "acme") -> dict:
    response = await client.post(
        "/organizations",
        headers=_headers(token),
        json={"name": slug.replace("-", " ").title(), "slug": slug},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _invite(
    client: AsyncClient,
    actor_token: str,
    organization_id: str,
    email: str,
    role: Role = Role.MEMBER,
) -> dict:
    response = await client.post(
        f"/organizations/{organization_id}/invitations",
        headers=_headers(actor_token),
        json={"email": email, "role": role.value},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _add_member(
    client: AsyncClient,
    owner_token: str,
    organization_id: str,
    email: str,
    role: Role = Role.MEMBER,
) -> tuple[str, dict]:
    member_token = await _register(client, email)
    invitation = await _invite(client, owner_token, organization_id, email, role)
    response = await client.post(
        f"/invitations/{invitation['token']}/accept",
        headers=_headers(member_token),
    )
    assert response.status_code == 200, response.text
    return member_token, response.json()


async def test_organization_creator_is_owner(client: AsyncClient) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token)

    response = await client.get(
        f"/organizations/{organization['id']}/members",
        headers=_headers(owner_token),
    )

    assert response.status_code == 200
    assert [member["role"] for member in response.json()] == ["OWNER"]


async def test_member_can_list_members(client: AsyncClient) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token)
    member_token, _ = await _add_member(
        client, owner_token, organization["id"], "member@example.com"
    )

    response = await client.get(
        f"/organizations/{organization['id']}/members",
        headers=_headers(member_token),
    )

    assert response.status_code == 200
    assert {member["email"] for member in response.json()} == {
        "owner@example.com",
        "member@example.com",
    }


async def test_owner_can_promote_member_to_admin(
    client: AsyncClient,
    session_factory,
) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token)
    _, membership = await _add_member(
        client, owner_token, organization["id"], "member@example.com"
    )

    response = await client.patch(
        f"/organizations/{organization['id']}/members/{membership['id']}",
        headers=_headers(owner_token),
        json={"role": "ADMIN"},
    )

    assert response.status_code == 200
    assert response.json()["role"] == "ADMIN"
    async with session_factory() as session:
        actions = set(
            await session.scalars(
                select(AuditLog.action).where(
                    AuditLog.organization_id == uuid.UUID(organization["id"])
                )
            )
        )
    assert "member.role_changed" in actions


async def test_member_cannot_promote_self(client: AsyncClient) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token)
    member_token, membership = await _add_member(
        client, owner_token, organization["id"], "member@example.com"
    )

    response = await client.patch(
        f"/organizations/{organization['id']}/members/{membership['id']}",
        headers=_headers(member_token),
        json={"role": "OWNER"},
    )

    assert response.status_code == 403


async def test_admin_cannot_promote_self_to_owner(client: AsyncClient) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token)
    admin_token, membership = await _add_member(
        client,
        owner_token,
        organization["id"],
        "admin@example.com",
        Role.ADMIN,
    )

    response = await client.patch(
        f"/organizations/{organization['id']}/members/{membership['id']}",
        headers=_headers(admin_token),
        json={"role": "OWNER"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "membership_change_forbidden"


async def test_admin_cannot_demote_owner(client: AsyncClient) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token)
    admin_token, _ = await _add_member(
        client,
        owner_token,
        organization["id"],
        "admin@example.com",
        Role.ADMIN,
    )
    members = (
        await client.get(
            f"/organizations/{organization['id']}/members",
            headers=_headers(owner_token),
        )
    ).json()
    owner_membership = next(member for member in members if member["role"] == "OWNER")

    response = await client.patch(
        f"/organizations/{organization['id']}/members/{owner_membership['id']}",
        headers=_headers(admin_token),
        json={"role": "MEMBER"},
    )

    assert response.status_code == 403


async def test_admin_cannot_remove_owner(client: AsyncClient) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token)
    admin_token, _ = await _add_member(
        client,
        owner_token,
        organization["id"],
        "admin@example.com",
        Role.ADMIN,
    )
    members = (
        await client.get(
            f"/organizations/{organization['id']}/members",
            headers=_headers(owner_token),
        )
    ).json()
    owner_membership = next(member for member in members if member["role"] == "OWNER")

    response = await client.delete(
        f"/organizations/{organization['id']}/members/{owner_membership['id']}",
        headers=_headers(admin_token),
    )

    assert response.status_code == 403


async def test_owner_can_remove_member(
    client: AsyncClient,
    session_factory,
) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token)
    _, membership = await _add_member(
        client, owner_token, organization["id"], "member@example.com"
    )

    response = await client.delete(
        f"/organizations/{organization['id']}/members/{membership['id']}",
        headers=_headers(owner_token),
    )

    assert response.status_code == 204
    members = await client.get(
        f"/organizations/{organization['id']}/members",
        headers=_headers(owner_token),
    )
    assert {member["email"] for member in members.json()} == {"owner@example.com"}
    async with session_factory() as session:
        actions = set(
            await session.scalars(
                select(AuditLog.action).where(
                    AuditLog.organization_id == uuid.UUID(organization["id"])
                )
            )
        )
    assert "member.removed" in actions


async def test_last_owner_cannot_be_demoted(client: AsyncClient) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token)
    members = await client.get(
        f"/organizations/{organization['id']}/members",
        headers=_headers(owner_token),
    )
    owner_membership = members.json()[0]

    response = await client.patch(
        f"/organizations/{organization['id']}/members/{owner_membership['id']}",
        headers=_headers(owner_token),
        json={"role": "MEMBER"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "last_owner"


async def test_last_owner_cannot_be_removed(client: AsyncClient) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token)
    members = await client.get(
        f"/organizations/{organization['id']}/members",
        headers=_headers(owner_token),
    )
    owner_membership = members.json()[0]

    response = await client.delete(
        f"/organizations/{organization['id']}/members/{owner_membership['id']}",
        headers=_headers(owner_token),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "last_owner"


async def test_owner_can_invite_admin(client: AsyncClient) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token)

    invitation = await _invite(
        client,
        owner_token,
        organization["id"],
        "admin@example.com",
        Role.ADMIN,
    )

    assert invitation["role"] == "ADMIN"
    assert invitation["token"]


async def test_invitation_creation_enqueues_email_job(
    client: AsyncClient,
    redis_client,
) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token)

    invitation = await _invite(
        client, owner_token, organization["id"], "invitee@example.com"
    )
    raw_job = await redis_client.lindex(DEFAULT_QUEUE, 0)
    assert raw_job is not None
    job = Job.from_json(raw_job)

    assert job.type == "send_invitation_email"
    assert job.payload == {
        "email": "invitee@example.com",
        "organization_id": organization["id"],
        "invitation_id": invitation["id"],
    }
    assert invitation["token"] not in raw_job


async def test_invitation_email_adapter_can_be_replaced(client: AsyncClient) -> None:
    delivered = {}

    class CapturingEmailSender:
        async def send_invitation(self, **invitation) -> None:
            delivered.update(invitation)

    app.dependency_overrides[get_invitation_email_sender] = CapturingEmailSender
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token)

    invitation = await _invite(
        client,
        owner_token,
        organization["id"],
        "invitee@example.com",
    )

    assert delivered["token"] == invitation["token"]
    assert str(delivered["invitation_id"]) == invitation["id"]


async def test_invitation_delivery_failure_returns_committed_token(
    client: AsyncClient,
    session_factory,
    caplog,
) -> None:
    class FailingEmailSender:
        async def send_invitation(self, **_invitation) -> None:
            raise InvitationDeliveryError("provider unavailable")

    app.dependency_overrides[get_invitation_email_sender] = FailingEmailSender
    caplog.set_level(logging.INFO)
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token)

    response = await client.post(
        f"/organizations/{organization['id']}/invitations",
        headers=_headers(owner_token),
        json={"email": "invitee@example.com", "role": "MEMBER"},
    )

    assert response.status_code == 201, response.text
    invitation = response.json()
    assert invitation["token"]
    async with session_factory() as session:
        stored = await session.scalar(
            select(Invitation).where(
                Invitation.token_hash == hash_token(invitation["token"])
            )
        )
    assert stored is not None
    messages = [record.getMessage() for record in caplog.records]
    assert any("invitation_email_delivery_failed" in message for message in messages)
    assert all(invitation["token"] not in message for message in messages)


async def test_admin_can_invite_member(client: AsyncClient) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token)
    admin_token, _ = await _add_member(
        client,
        owner_token,
        organization["id"],
        "admin@example.com",
        Role.ADMIN,
    )

    response = await client.post(
        f"/organizations/{organization['id']}/invitations",
        headers=_headers(admin_token),
        json={"email": "member@example.com", "role": "MEMBER"},
    )

    assert response.status_code == 201


async def test_admin_cannot_invite_admin(client: AsyncClient) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token)
    admin_token, _ = await _add_member(
        client,
        owner_token,
        organization["id"],
        "admin@example.com",
        Role.ADMIN,
    )

    response = await client.post(
        f"/organizations/{organization['id']}/invitations",
        headers=_headers(admin_token),
        json={"email": "second-admin@example.com", "role": "ADMIN"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "invitation_role_forbidden"


async def test_member_cannot_create_invitation(client: AsyncClient) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token)
    member_token, _ = await _add_member(
        client, owner_token, organization["id"], "member@example.com"
    )

    response = await client.post(
        f"/organizations/{organization['id']}/invitations",
        headers=_headers(member_token),
        json={"email": "another@example.com", "role": "MEMBER"},
    )

    assert response.status_code == 403


async def test_accept_invitation_creates_membership(client: AsyncClient) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token)
    invitee_token = await _register(client, "invitee@example.com")
    invitation = await _invite(
        client, owner_token, organization["id"], "invitee@example.com"
    )

    response = await client.post(
        f"/invitations/{invitation['token']}/accept",
        headers=_headers(invitee_token),
    )

    assert response.status_code == 200
    assert response.json()["organization_id"] == organization["id"]
    assert response.json()["role"] == "MEMBER"


async def test_invitation_email_must_match_account(client: AsyncClient) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token)
    wrong_user_token = await _register(client, "wrong@example.com")
    invitation = await _invite(
        client, owner_token, organization["id"], "invitee@example.com"
    )

    response = await client.post(
        f"/invitations/{invitation['token']}/accept",
        headers=_headers(wrong_user_token),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "invitation_email_mismatch"


async def test_expired_invitation_is_rejected(client: AsyncClient, session_factory) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token)
    invitee_token = await _register(client, "invitee@example.com")
    invitation = await _invite(
        client, owner_token, organization["id"], "invitee@example.com"
    )
    async with session_factory() as session:
        stored = await session.scalar(
            select(Invitation).where(
                Invitation.token_hash == hash_token(invitation["token"])
            )
        )
        assert stored is not None
        stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    response = await client.post(
        f"/invitations/{invitation['token']}/accept",
        headers=_headers(invitee_token),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invitation_expired"


async def test_invitation_cannot_be_reused(client: AsyncClient) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token)
    invitee_token = await _register(client, "invitee@example.com")
    invitation = await _invite(
        client, owner_token, organization["id"], "invitee@example.com"
    )
    first = await client.post(
        f"/invitations/{invitation['token']}/accept",
        headers=_headers(invitee_token),
    )

    second = await client.post(
        f"/invitations/{invitation['token']}/accept",
        headers=_headers(invitee_token),
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "invitation_used"


async def test_invitation_database_stores_hash_only(client: AsyncClient, session_factory) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token)
    invitation = await _invite(
        client, owner_token, organization["id"], "invitee@example.com"
    )

    async with session_factory() as session:
        stored = await session.scalar(
            select(Invitation).where(
                Invitation.token_hash == hash_token(invitation["token"])
            )
        )

    assert stored is not None
    assert stored.token_hash == hash_token(invitation["token"])
    assert stored.token_hash != invitation["token"]
    assert len(stored.token_hash) == 64
