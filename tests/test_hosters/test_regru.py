"""Tests for the REG.ru CloudVPS hoster integration (HTTP mocked with respx)."""

import json
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from wlfinder.hosters.base import HosterAuthError, RateLimitError
from wlfinder.hosters.regru import RegruConfig, RegruHoster

API = "https://api.cloudvps.reg.ru/v1"


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGRU_TOKEN", "regru-test-token")


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant(_: float) -> None:
        return None

    monkeypatch.setattr("wlfinder.hosters._http.asyncio.sleep", _instant)
    monkeypatch.setattr("wlfinder.hosters.regru.asyncio.sleep", _instant)


def _cfg(**extra: object) -> RegruConfig:
    base: dict[str, object] = {
        "name": "regru-msk",
        "type": "regru",
        "token_env": "REGRU_TOKEN",
        "size": "cloud-1",
        "image": "ubuntu-22-04-amd64",
        "region_slug": "msk1",
    }
    base.update(extra)
    return RegruConfig.model_validate(base)


@pytest.fixture
async def hoster() -> AsyncIterator[RegruHoster]:
    async with httpx.AsyncClient() as client:
        yield RegruHoster(_cfg(), client)


@respx.mock
async def test_create_reuses_existing_key_and_returns_immediate_ip(
    hoster: RegruHoster,
) -> None:
    respx.get(f"{API}/account/keys").mock(
        return_value=httpx.Response(
            200,
            json={"ssh_keys": [{"fingerprint": "aa:bb:cc", "public_key": "ssh-ed25519 AAA test"}]},
        )
    )
    post_key = respx.post(f"{API}/account/keys")
    create = respx.post(f"{API}/reglets").mock(
        return_value=httpx.Response(
            201,
            json={
                "reglet": {
                    "id": 4242,
                    "ip": "203.0.113.9",
                    "ipv6": "2001:db8::9",
                    "status": "active",
                }
            },
        )
    )

    server = await hoster.create(
        name="wlfinder-x", ssh_pub_key="ssh-ed25519 AAA test", user_data=None
    )

    assert server.server_id == "4242"
    assert server.public_ipv4 == "203.0.113.9"
    assert server.public_ipv6 == "2001:db8::9"
    assert server.region == "msk1"
    assert not post_key.called  # existing key reused
    sent = json.loads(create.calls.last.request.content)
    assert sent["size"] == "cloud-1"
    assert sent["image"] == "ubuntu-22-04-amd64"
    assert sent["ssh_keys"] == ["aa:bb:cc"]


@respx.mock
async def test_create_uploads_key_and_polls_for_ip(hoster: RegruHoster) -> None:
    respx.get(f"{API}/account/keys").mock(
        return_value=httpx.Response(200, json={"ssh_keys": []})
    )
    respx.post(f"{API}/account/keys").mock(
        return_value=httpx.Response(201, json={"ssh_key": {"fingerprint": "de:ad:be:ef"}})
    )
    create = respx.post(f"{API}/reglets").mock(
        return_value=httpx.Response(202, json={"reglet": {"id": 1, "status": "new"}})
    )
    respx.get(f"{API}/reglets/1").mock(
        side_effect=[
            httpx.Response(200, json={"reglet": {"id": 1, "status": "new"}}),
            httpx.Response(
                200, json={"reglet": {"id": 1, "ip": "198.51.100.7", "status": "active"}}
            ),
        ]
    )

    server = await hoster.create(name="x", ssh_pub_key="ssh-ed25519 AAA new", user_data=None)

    assert server.public_ipv4 == "198.51.100.7"
    assert server.public_ipv6 is None
    sent = json.loads(create.calls.last.request.content)
    assert sent["ssh_keys"] == ["de:ad:be:ef"]


@respx.mock
async def test_create_uses_config_fingerprints_without_api_call() -> None:
    async with httpx.AsyncClient() as client:
        h = RegruHoster(_cfg(ssh_key_fingerprints=["pre:set:fp"]), client)
        keys_route = respx.get(f"{API}/account/keys")
        respx.post(f"{API}/reglets").mock(
            return_value=httpx.Response(
                201, json={"reglet": {"id": 5, "ip": "192.0.2.5", "status": "active"}}
            )
        )
        server = await h.create(name="x", ssh_pub_key="ignored", user_data=None)

    assert server.public_ipv4 == "192.0.2.5"
    assert not keys_route.called  # config fingerprints used as-is


@respx.mock
async def test_delete_is_idempotent_on_404(hoster: RegruHoster) -> None:
    respx.delete(f"{API}/reglets/404").mock(return_value=httpx.Response(404))
    await hoster.delete("404")  # must not raise


@respx.mock
async def test_delete_ok(hoster: RegruHoster) -> None:
    route = respx.delete(f"{API}/reglets/7").mock(return_value=httpx.Response(204))
    await hoster.delete("7")
    assert route.called


@respx.mock
async def test_auth_error_raises(hoster: RegruHoster) -> None:
    respx.get(f"{API}/account/keys").mock(return_value=httpx.Response(403))
    with pytest.raises(HosterAuthError):
        await hoster.health_check()


@respx.mock
async def test_rate_limit_exhausted_raises(hoster: RegruHoster) -> None:
    respx.get(f"{API}/account/keys").mock(return_value=httpx.Response(429))
    with pytest.raises(RateLimitError):
        await hoster.health_check()


@respx.mock
async def test_health_check_ok(hoster: RegruHoster) -> None:
    respx.get(f"{API}/account/keys").mock(
        return_value=httpx.Response(200, json={"ssh_keys": []})
    )
    assert await hoster.health_check() is True


async def test_get_balance_is_none(hoster: RegruHoster) -> None:
    # REG.ru CloudVPS has no balance endpoint — must report None, not crash.
    assert await hoster.get_balance() is None


@respx.mock
async def test_list_servers(hoster: RegruHoster) -> None:
    respx.get(f"{API}/reglets").mock(
        return_value=httpx.Response(
            200,
            json={
                "reglets": [
                    {"id": 10, "name": "wlfinder-x", "ip": "5.6.7.8"},
                    {"id": 11, "name": "prod-db", "ip": "9.9.9.9"},
                ]
            },
        )
    )
    servers = await hoster.list_servers()
    assert {s.server_id for s in servers} == {"10", "11"}
    wl = [s for s in servers if s.name.startswith("wlfinder-")]
    assert len(wl) == 1
    assert wl[0].public_ipv4 == "5.6.7.8"
    assert wl[0].region == "msk1"
