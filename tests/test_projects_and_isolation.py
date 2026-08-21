from httpx import AsyncClient

PASSWORD = "correct-horse-battery-staple"


async def _register(client: AsyncClient, email: str = "owner@example.com") -> str:
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


async def _project(
    client: AsyncClient,
    token: str,
    organization_id: str,
    name: str = "Roadmap",
) -> dict:
    response = await client.post(
        f"/organizations/{organization_id}/projects",
        headers=_headers(token),
        json={"name": name, "description": "Quarterly roadmap"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_project(client: AsyncClient) -> None:
    token = await _register(client)
    organization = await _organization(client, token, "acme")

    project = await _project(client, token, organization["id"])

    assert project["organization_id"] == organization["id"]
    assert project["name"] == "Roadmap"
    assert project["description"] == "Quarterly roadmap"
    assert project["status"] == "ACTIVE"


async def test_list_projects(client: AsyncClient) -> None:
    token = await _register(client)
    organization = await _organization(client, token, "acme")
    first = await _project(client, token, organization["id"], "First")
    second = await _project(client, token, organization["id"], "Second")

    response = await client.get(
        f"/organizations/{organization['id']}/projects",
        headers=_headers(token),
    )

    assert response.status_code == 200
    assert {project["id"] for project in response.json()} == {first["id"], second["id"]}


async def test_get_project(client: AsyncClient) -> None:
    token = await _register(client)
    organization = await _organization(client, token, "acme")
    project = await _project(client, token, organization["id"])

    response = await client.get(
        f"/organizations/{organization['id']}/projects/{project['id']}",
        headers=_headers(token),
    )

    assert response.status_code == 200
    assert response.json() == project


async def test_update_project_fields(client: AsyncClient) -> None:
    token = await _register(client)
    organization = await _organization(client, token, "acme")
    project = await _project(client, token, organization["id"])

    response = await client.patch(
        f"/organizations/{organization['id']}/projects/{project['id']}",
        headers=_headers(token),
        json={"name": "Delivery plan", "description": None},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Delivery plan"
    assert response.json()["description"] is None


async def test_archive_project(client: AsyncClient) -> None:
    token = await _register(client)
    organization = await _organization(client, token, "acme")
    project = await _project(client, token, organization["id"])

    response = await client.patch(
        f"/organizations/{organization['id']}/projects/{project['id']}",
        headers=_headers(token),
        json={"status": "ARCHIVED"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ARCHIVED"


async def test_delete_project(client: AsyncClient) -> None:
    token = await _register(client)
    organization = await _organization(client, token, "acme")
    project = await _project(client, token, organization["id"])
    endpoint = f"/organizations/{organization['id']}/projects/{project['id']}"

    deleted = await client.delete(endpoint, headers=_headers(token))
    missing = await client.get(endpoint, headers=_headers(token))

    assert deleted.status_code == 204
    assert missing.status_code == 404


async def test_project_get_is_tenant_scoped(client: AsyncClient) -> None:
    token = await _register(client)
    first_org = await _organization(client, token, "first-org")
    second_org = await _organization(client, token, "second-org")
    project = await _project(client, token, first_org["id"])

    response = await client.get(
        f"/organizations/{second_org['id']}/projects/{project['id']}",
        headers=_headers(token),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "project_not_found"


async def test_project_patch_is_tenant_scoped(client: AsyncClient) -> None:
    token = await _register(client)
    first_org = await _organization(client, token, "first-org")
    second_org = await _organization(client, token, "second-org")
    project = await _project(client, token, first_org["id"])

    response = await client.patch(
        f"/organizations/{second_org['id']}/projects/{project['id']}",
        headers=_headers(token),
        json={"name": "Leaked update"},
    )

    assert response.status_code == 404
    original = await client.get(
        f"/organizations/{first_org['id']}/projects/{project['id']}",
        headers=_headers(token),
    )
    assert original.json()["name"] == "Roadmap"


async def test_project_delete_is_tenant_scoped(client: AsyncClient) -> None:
    token = await _register(client)
    first_org = await _organization(client, token, "first-org")
    second_org = await _organization(client, token, "second-org")
    project = await _project(client, token, first_org["id"])

    response = await client.delete(
        f"/organizations/{second_org['id']}/projects/{project['id']}",
        headers=_headers(token),
    )

    assert response.status_code == 404
    original = await client.get(
        f"/organizations/{first_org['id']}/projects/{project['id']}",
        headers=_headers(token),
    )
    assert original.status_code == 200


async def test_project_lists_do_not_mix_tenants(client: AsyncClient) -> None:
    token = await _register(client)
    first_org = await _organization(client, token, "first-org")
    second_org = await _organization(client, token, "second-org")
    first_project = await _project(client, token, first_org["id"], "First tenant")
    second_project = await _project(client, token, second_org["id"], "Second tenant")

    first_list = await client.get(
        f"/organizations/{first_org['id']}/projects",
        headers=_headers(token),
    )
    second_list = await client.get(
        f"/organizations/{second_org['id']}/projects",
        headers=_headers(token),
    )

    assert [project["id"] for project in first_list.json()] == [first_project["id"]]
    assert [project["id"] for project in second_list.json()] == [second_project["id"]]
