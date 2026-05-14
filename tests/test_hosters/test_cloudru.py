"""Tests for the Cloud.ru Evolution hoster integration (HTTP mocked)."""

from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from wlfinder.hosters.cloudru import CloudRuConfig, CloudRuHoster

TOKEN_URL = "https://iam.api.cloud.ru/api/v1/auth/system/openid/token"
API = "https://api.cloud.ru/compute/v1"


@pytest.fixture(autouse=True)
def _creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDRU_KEY_ID", "key-id")
    monkeypatch.setenv("CLOUDRU_KEY_SECRET", "key-secret")
    monkeypatch.setenv("CLOUDRU_PROJECT_ID", "proj-1")


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant(_: float) -> None:
        return None

    monkeypatch.setattr("wlfinder.hosters._http.asyncio.sleep", _instant)
    monkeypatch.setattr("wlfinder.hosters.cloudru.asyncio.sleep", _instant)


@pytest.fixture
async def hoster() -> AsyncIterator[CloudRuHoster]:
    cfg = CloudRuConfig.model_validate(
        {"name": "cloudru-msk", "type": "cloudru", "flavor": "small", "image": "ubuntu-24"}
    )
    async with httpx.AsyncClient() as client:
        yield CloudRuHoster(cfg, client)


@respx.mock
async def test_token_exchange_and_create(hoster: CloudRuHoster) -> None:
    token = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
    )
    create = respx.post(f"{API}/instances").mock(
        return_value=httpx.Response(201, json={"id": "i-1", "public_ip": "9.9.9.9"})
    )
    server = await hoster.create(name="wlfinder-x", ssh_pub_key="ssh-ed25519 AAA t", user_data=None)

    assert server.server_id == "i-1"
    assert server.public_ipv4 == "9.9.9.9"
    assert token.called
    req = create.calls.last.request
    assert req.headers["Authorization"] == "Bearer at-1"
    assert req.headers["X-Project-Id"] == "proj-1"


@respx.mock
async def test_token_refreshed_on_401(hoster: CloudRuHoster) -> None:
    token = respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "at-2", "expires_in": 3600}),
        ]
    )
    respx.get(f"{API}/instances").mock(
        side_effect=[
            httpx.Response(401),  # stale token
            httpx.Response(200, json={"instances": []}),
        ]
    )
    assert await hoster.health_check() is True
    assert token.call_count == 2  # initial + refresh


@respx.mock
async def test_delete_is_idempotent_on_404(hoster: CloudRuHoster) -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
    )
    respx.delete(f"{API}/instances/404").mock(return_value=httpx.Response(404))
    await hoster.delete("404")


@respx.mock
async def test_list_servers(hoster: CloudRuHoster) -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
    )
    respx.get(f"{API}/instances").mock(
        return_value=httpx.Response(
            200,
            json={"instances": [{"id": "i-1", "name": "wlfinder-a", "ip_address": "1.2.3.4"}]},
        )
    )
    servers = await hoster.list_servers()
    assert servers[0].server_id == "i-1"
    assert servers[0].public_ipv4 == "1.2.3.4"
