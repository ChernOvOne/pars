"""Tests for the CLO.ru hoster integration (HTTP mocked with respx)."""

from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from wlfinder.hosters.base import HosterAuthError
from wlfinder.hosters.clo import CloConfig, CloHoster

API = "https://api.clo.ru/v1"


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLO_TOKEN", "clo-token")


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant(_: float) -> None:
        return None

    monkeypatch.setattr("wlfinder.hosters._http.asyncio.sleep", _instant)
    monkeypatch.setattr("wlfinder.hosters.clo.asyncio.sleep", _instant)


@pytest.fixture
async def hoster() -> AsyncIterator[CloHoster]:
    cfg = CloConfig.model_validate(
        {"name": "clo-msk", "type": "clo", "token_env": "CLO_TOKEN",
         "flavor": "small", "image": "ubuntu-24"}
    )
    async with httpx.AsyncClient() as client:
        yield CloHoster(cfg, client)


@respx.mock
async def test_create_immediate_ip(hoster: CloHoster) -> None:
    respx.post(f"{API}/instances").mock(
        return_value=httpx.Response(201, json={"instance": {"id": "i-1", "public_ip": "1.2.3.4"}})
    )
    server = await hoster.create(name="wlfinder-x", ssh_pub_key="ssh-ed25519 AAA t", user_data=None)
    assert server.server_id == "i-1"
    assert server.public_ipv4 == "1.2.3.4"
    assert server.region == "msk"


@respx.mock
async def test_create_polls_for_ip(hoster: CloHoster) -> None:
    respx.post(f"{API}/instances").mock(
        return_value=httpx.Response(202, json={"instance": {"id": "i-2"}})
    )
    respx.get(f"{API}/instances/i-2").mock(
        side_effect=[
            httpx.Response(200, json={"instance": {"id": "i-2"}}),
            httpx.Response(200, json={"instance": {"id": "i-2", "ip_address": "9.9.9.9"}}),
        ]
    )
    server = await hoster.create(name="x", ssh_pub_key="k", user_data=None)
    assert server.public_ipv4 == "9.9.9.9"


@respx.mock
async def test_delete_is_idempotent_on_404(hoster: CloHoster) -> None:
    respx.delete(f"{API}/instances/404").mock(return_value=httpx.Response(404))
    await hoster.delete("404")


@respx.mock
async def test_list_servers(hoster: CloHoster) -> None:
    respx.get(f"{API}/instances").mock(
        return_value=httpx.Response(
            200,
            json={
                "instances": [
                    {"id": "i-1", "name": "wlfinder-a", "public_ip": "1.1.1.1"},
                    {"id": "i-2", "name": "prod", "public_ip": "2.2.2.2"},
                ]
            },
        )
    )
    servers = await hoster.list_servers()
    assert {s.server_id for s in servers} == {"i-1", "i-2"}


@respx.mock
async def test_health_check_and_auth_error(hoster: CloHoster) -> None:
    respx.get(f"{API}/instances").mock(return_value=httpx.Response(200, json={"instances": []}))
    assert await hoster.health_check() is True
    respx.get(f"{API}/instances").mock(return_value=httpx.Response(403))
    with pytest.raises(HosterAuthError):
        await hoster.health_check()
