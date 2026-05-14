"""Tests for the 1cloud.ru hoster integration (HTTP mocked with respx)."""

import json
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from wlfinder.hosters.base import HosterAuthError
from wlfinder.hosters.onecloud import OneCloudConfig, OneCloudHoster

API = "https://api.1cloud.ru"


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONECLOUD_TOKEN", "oc-token")


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant(_: float) -> None:
        return None

    monkeypatch.setattr("wlfinder.hosters._http.asyncio.sleep", _instant)
    monkeypatch.setattr("wlfinder.hosters.onecloud.asyncio.sleep", _instant)


@pytest.fixture
async def hoster() -> AsyncIterator[OneCloudHoster]:
    cfg = OneCloudConfig.model_validate(
        {"name": "onecloud-msk", "type": "1cloud", "token_env": "ONECLOUD_TOKEN", "image_id": 42}
    )
    async with httpx.AsyncClient() as client:
        yield OneCloudHoster(cfg, client)


@respx.mock
async def test_create_uploads_key_and_polls_for_ip(hoster: OneCloudHoster) -> None:
    respx.get(f"{API}/sshkey").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{API}/sshkey").mock(return_value=httpx.Response(201, json={"ID": 7}))
    create = respx.post(f"{API}/server").mock(
        return_value=httpx.Response(201, json={"ID": 100, "Name": "wlfinder-x"})
    )
    respx.get(f"{API}/server/100").mock(
        side_effect=[
            httpx.Response(200, json={"ID": 100, "State": "New"}),
            httpx.Response(200, json={"ID": 100, "IP": "203.0.113.7", "State": "Active"}),
        ]
    )

    server = await hoster.create(name="wlfinder-x", ssh_pub_key="ssh-ed25519 AAA t", user_data=None)

    assert server.server_id == "100"
    assert server.public_ipv4 == "203.0.113.7"
    sent = json.loads(create.calls.last.request.content)
    assert sent["ImageID"] == 42
    assert sent["SshKeys"] == [7]


@respx.mock
async def test_delete_is_idempotent_on_404(hoster: OneCloudHoster) -> None:
    respx.delete(f"{API}/server/404").mock(return_value=httpx.Response(404))
    await hoster.delete("404")


@respx.mock
async def test_auth_error_raises(hoster: OneCloudHoster) -> None:
    respx.get(f"{API}/account").mock(return_value=httpx.Response(401))
    with pytest.raises(HosterAuthError):
        await hoster.health_check()


@respx.mock
async def test_list_servers(hoster: OneCloudHoster) -> None:
    respx.get(f"{API}/server").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"ID": 1, "Name": "wlfinder-a", "IP": "1.2.3.4"},
                {"ID": 2, "Name": "other", "IP": "5.6.7.8"},
            ],
        )
    )
    servers = await hoster.list_servers()
    assert {s.server_id for s in servers} == {"1", "2"}
    assert next(s for s in servers if s.server_id == "1").public_ipv4 == "1.2.3.4"


@respx.mock
async def test_get_balance(hoster: OneCloudHoster) -> None:
    respx.get(f"{API}/account").mock(return_value=httpx.Response(200, json={"Balance": 123.45}))
    assert await hoster.get_balance() == 123.45
