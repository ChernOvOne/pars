"""Tests for the Selectel hoster integration (Keystone + Nova, HTTP mocked)."""

from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from wlfinder.hosters.selectel import SelectelConfig, SelectelHoster, _extract_ipv4

KEYSTONE = "https://cloud.api.selcloud.ru/identity/v3/auth/tokens"
COMPUTE = "https://ru-2.cloud.api.selcloud.ru/compute/v2.1"


@pytest.fixture(autouse=True)
def _creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SELECTEL_ACCOUNT_ID", "12345")
    monkeypatch.setenv("SELECTEL_SERVICE_USER", "svc-user")
    monkeypatch.setenv("SELECTEL_SERVICE_PASS", "svc-pass")
    monkeypatch.setenv("SELECTEL_PROJECT_ID", "proj-uuid")


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant(_: float) -> None:
        return None

    monkeypatch.setattr("wlfinder.hosters._http.asyncio.sleep", _instant)
    monkeypatch.setattr("wlfinder.hosters.selectel.asyncio.sleep", _instant)


@pytest.fixture
async def hoster() -> AsyncIterator[SelectelHoster]:
    cfg = SelectelConfig.model_validate(
        {
            "name": "selectel-spb",
            "type": "selectel",
            "region": "ru-2",
            "flavor_id": "flavor-uuid",
            "image_id": "image-uuid",
            "network_id": "net-uuid",
        }
    )
    async with httpx.AsyncClient() as client:
        yield SelectelHoster(cfg, client)


def _keystone_ok() -> httpx.Response:
    return httpx.Response(
        201,
        headers={"X-Subject-Token": "kt-1"},
        json={"token": {"expires_at": "2099-01-01T00:00:00.000000Z"}},
    )


def test_extract_ipv4_prefers_floating() -> None:
    server = {
        "addresses": {
            "private": [{"addr": "10.0.0.5", "version": 4, "OS-EXT-IPS:type": "fixed"}],
            "public": [{"addr": "203.0.113.9", "version": 4, "OS-EXT-IPS:type": "floating"}],
        }
    }
    assert _extract_ipv4(server) == "203.0.113.9"
    assert _extract_ipv4({"addresses": {}}) is None


@respx.mock
async def test_keystone_auth_and_create(hoster: SelectelHoster) -> None:
    respx.post(KEYSTONE).mock(return_value=_keystone_ok())
    respx.get(f"{COMPUTE}/os-keypairs").mock(
        return_value=httpx.Response(200, json={"keypairs": []})
    )
    respx.post(f"{COMPUTE}/os-keypairs").mock(
        return_value=httpx.Response(201, json={"keypair": {"name": "wlfinder"}})
    )
    create = respx.post(f"{COMPUTE}/servers").mock(
        return_value=httpx.Response(202, json={"server": {"id": "s-1"}})
    )
    respx.get(f"{COMPUTE}/servers/s-1").mock(
        side_effect=[
            httpx.Response(200, json={"server": {"id": "s-1", "addresses": {}}}),
            httpx.Response(
                200,
                json={
                    "server": {
                        "id": "s-1",
                        "addresses": {
                            "public": [
                                {
                                    "addr": "198.51.100.7",
                                    "version": 4,
                                    "OS-EXT-IPS:type": "floating",
                                }
                            ]
                        },
                    }
                },
            ),
        ]
    )

    server = await hoster.create(name="wlfinder-x", ssh_pub_key="ssh-ed25519 AAA t", user_data=None)

    assert server.server_id == "s-1"
    assert server.public_ipv4 == "198.51.100.7"
    assert create.calls.last.request.headers["X-Auth-Token"] == "kt-1"


@respx.mock
async def test_delete_is_idempotent_on_404(hoster: SelectelHoster) -> None:
    respx.post(KEYSTONE).mock(return_value=_keystone_ok())
    respx.delete(f"{COMPUTE}/servers/404").mock(return_value=httpx.Response(404))
    await hoster.delete("404")


@respx.mock
async def test_list_servers(hoster: SelectelHoster) -> None:
    respx.post(KEYSTONE).mock(return_value=_keystone_ok())
    respx.get(f"{COMPUTE}/servers/detail").mock(
        return_value=httpx.Response(
            200,
            json={
                "servers": [
                    {
                        "id": "s-1",
                        "name": "wlfinder-a",
                        "addresses": {
                            "public": [
                                {"addr": "1.2.3.4", "version": 4, "OS-EXT-IPS:type": "floating"}
                            ]
                        },
                    }
                ]
            },
        )
    )
    servers = await hoster.list_servers()
    assert servers[0].server_id == "s-1"
    assert servers[0].public_ipv4 == "1.2.3.4"
