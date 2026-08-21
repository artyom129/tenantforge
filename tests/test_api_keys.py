import uuid

from httpx import AsyncClient
from sqlalchemy import select

from app.api.deps import get_api_key_principal
from app.core.errors import AuthenticationError
from app.models.resources import ApiKey
from app.security.opaque_tokens import hash_token

PASSWORD = "correct-horse-battery-staple"


async def _register(client: AsyncClient, email: str) -> str:
    created = await client.post(
        "/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert created.status_code == 201, created.text
    logged_in = await client.post(
        "/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert logged_in.status_code == 200, logged_in.text
    return logged_in.json()["access_token"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _organization(client: AsyncClient, token: str, slug: str) -> dict:
    response = await client.post(
        "/organizations",
        headers=_headers(token),
        json={"name": slug.title(), "slug": slug},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_key(client: AsyncClient, token: str, organization_id: str) -> dict:
    response = await client.post(
        f"/organizations/{organization_id}/api-keys",
        headers=_headers(token),
        json={"name": "CI key"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _add_member(
    client: AsyncClient,
    owner_token: str,
    organization_id: str,
) -> str:
    member_token = await _register(client, "member@example.com")
    invitation = await client.post(
        f"/organizations/{organization_id}/invitations",
        headers=_headers(owner_token),
        json={"email": "member@example.com", "role": "MEMBER"},
    )
    assert invitation.status_code == 201, invitation.text
    accepted = await client.post(
        f"/invitations/{invitation.json()['token']}/accept",
        headers=_headers(member_token),
    )
    assert accepted.status_code == 200, accepted.text
    return member_token


async def test_create_api_key_returns_plaintext_once(client: AsyncClient) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token, "acme")

    api_key = await _create_key(client, owner_token, organization["id"])

    assert api_key["key"].startswith("tf_live_")
    assert api_key["prefix"] == api_key["key"][:16]
    assert api_key["revoked_at"] is None


async def test_api_key_plaintext_is_not_stored(client: AsyncClient, session_factory) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token, "acme")
    created = await _create_key(client, owner_token, organization["id"])

    async with session_factory() as session:
        stored = await session.scalar(
            select(ApiKey).where(ApiKey.id == uuid.UUID(created["id"]))
        )

    assert stored is not None
    assert stored.key_hash == hash_token(created["key"])
    assert stored.key_hash != created["key"]
    assert created["key"] not in repr(stored.__dict__)


async def test_api_key_list_never_returns_secret_or_hash(client: AsyncClient) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token, "acme")
    created = await _create_key(client, owner_token, organization["id"])

    response = await client.get(
        f"/organizations/{organization['id']}/api-keys",
        headers=_headers(owner_token),
    )

    assert response.status_code == 200
    listed = response.json()[0]
    assert listed["prefix"] == created["prefix"]
    assert "key" not in listed
    assert "key_hash" not in listed
    assert created["key"] not in response.text


async def test_member_cannot_create_api_key(client: AsyncClient) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token, "acme")
    member_token = await _add_member(client, owner_token, organization["id"])

    response = await client.post(
        f"/organizations/{organization['id']}/api-keys",
        headers=_headers(member_token),
        json={"name": "forbidden"},
    )

    assert response.status_code == 403


async def test_member_cannot_list_api_keys(client: AsyncClient) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token, "acme")
    await _create_key(client, owner_token, organization["id"])
    member_token = await _add_member(client, owner_token, organization["id"])

    response = await client.get(
        f"/organizations/{organization['id']}/api-keys",
        headers=_headers(member_token),
    )

    assert response.status_code == 403


async def test_member_cannot_revoke_api_key(client: AsyncClient) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token, "acme")
    created = await _create_key(client, owner_token, organization["id"])
    member_token = await _add_member(client, owner_token, organization["id"])

    response = await client.delete(
        f"/organizations/{organization['id']}/api-keys/{created['id']}",
        headers=_headers(member_token),
    )

    assert response.status_code == 403


async def test_api_key_authentication_dependency_accepts_active_key(
    client: AsyncClient,
    session_factory,
    redis_client,
) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token, "acme")
    created = await _create_key(client, owner_token, organization["id"])

    async with session_factory() as session:
        principal = await get_api_key_principal(session, redis_client, created["key"])
        await session.commit()

    assert str(principal.key_id) == created["id"]
    assert str(principal.organization_id) == organization["id"]
    async with session_factory() as session:
        stored = await session.scalar(select(ApiKey).where(ApiKey.id == principal.key_id))
        assert stored is not None
        assert stored.last_used_at is not None


async def test_revoked_api_key_cannot_authenticate(
    client: AsyncClient,
    session_factory,
    redis_client,
) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token, "acme")
    created = await _create_key(client, owner_token, organization["id"])
    response = await client.delete(
        f"/organizations/{organization['id']}/api-keys/{created['id']}",
        headers=_headers(owner_token),
    )
    assert response.status_code == 204

    async with session_factory() as session:
        try:
            await get_api_key_principal(session, redis_client, created["key"])
        except AuthenticationError as exc:
            assert exc.code == "invalid_api_key"
        else:
            raise AssertionError("Revoked API key authenticated")


async def test_api_key_revocation_is_idempotent(client: AsyncClient) -> None:
    owner_token = await _register(client, "owner@example.com")
    organization = await _organization(client, owner_token, "acme")
    created = await _create_key(client, owner_token, organization["id"])
    endpoint = f"/organizations/{organization['id']}/api-keys/{created['id']}"

    first = await client.delete(endpoint, headers=_headers(owner_token))
    second = await client.delete(endpoint, headers=_headers(owner_token))

    assert first.status_code == 204
    assert second.status_code == 204


async def test_cross_tenant_owner_cannot_list_keys(client: AsyncClient) -> None:
    first_owner = await _register(client, "first@example.com")
    first_org = await _organization(client, first_owner, "first-org")
    await _create_key(client, first_owner, first_org["id"])
    second_owner = await _register(client, "second@example.com")
    await _organization(client, second_owner, "second-org")

    response = await client.get(
        f"/organizations/{first_org['id']}/api-keys",
        headers=_headers(second_owner),
    )

    assert response.status_code == 404


async def test_cross_tenant_revoke_cannot_disable_key(
    client: AsyncClient,
    session_factory,
    redis_client,
) -> None:
    first_owner = await _register(client, "first@example.com")
    first_org = await _organization(client, first_owner, "first-org")
    created = await _create_key(client, first_owner, first_org["id"])
    second_owner = await _register(client, "second@example.com")
    second_org = await _organization(client, second_owner, "second-org")

    response = await client.delete(
        f"/organizations/{second_org['id']}/api-keys/{created['id']}",
        headers=_headers(second_owner),
    )

    assert response.status_code == 404
    async with session_factory() as session:
        principal = await get_api_key_principal(session, redis_client, created["key"])
        assert str(principal.key_id) == created["id"]
