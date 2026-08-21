from typing import Any

from httpx import AsyncClient

DEFAULT_PASSWORD = "correct-horse-battery-staple"


def auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


async def register_and_login(
    client: AsyncClient,
    *,
    email: str = "owner@example.test",
    password: str = DEFAULT_PASSWORD,
) -> dict[str, Any]:
    registration = await client.post(
        "/auth/register",
        json={"email": email, "password": password},
    )
    assert registration.status_code == 201, registration.text

    login = await client.post(
        "/auth/login",
        json={"email": email, "password": password},
    )
    assert login.status_code == 200, login.text
    return login.json()


async def create_organization(
    client: AsyncClient,
    access_token: str,
    *,
    name: str = "Acme Labs",
    slug: str = "acme-labs",
) -> dict[str, Any]:
    response = await client.post(
        "/organizations",
        headers=auth_headers(access_token),
        json={"name": name, "slug": slug},
    )
    assert response.status_code == 201, response.text
    return response.json()
