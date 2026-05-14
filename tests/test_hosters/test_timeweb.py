"""Tests for the Timeweb Cloud hoster integration (HTTP mocked with respx)."""

import base64
import json
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from wlfinder.hosters.base import BalanceError, HosterAuthError, RateLimitError
from wlfinder.hosters.timeweb import TimewebConfig, TimewebHoster

API = "https://api.timeweb.cloud/api/v1"


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIMEWEB_TOKEN", "test-token-123")


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make retry/poll backoff instant so tests stay fast."""

    async def _instant(_: float) -> None:
        return None

    monkeypatch.setattr("wlfinder.hosters.timeweb.asyncio.sleep", _instant)


@pytest.fixture
def tw_cfg() -> TimewebConfig:
    return TimewebConfig(
        name="timeweb-spb",
        type="timeweb",
        token_env="TIMEWEB_TOKEN",
        preset_id=4795,
        os_id=99,
        region="ru-1",
        bandwidth=100,
    )


@pytest.fixture
async def hoster(tw_cfg: TimewebConfig) -> AsyncIterator[TimewebHoster]:
    async with httpx.AsyncClient() as client:
        yield TimewebHoster(tw_cfg, client)


@respx.mock
async def test_create_polls_until_ip_appears(hoster: TimewebHoster) -> None:
    respx.get(f"{API}/ssh-keys").mock(return_value=httpx.Response(200, json={"ssh_keys": []}))
    respx.post(f"{API}/ssh-keys").mock(
        return_value=httpx.Response(201, json={"ssh_key": {"id": 55}})
    )
    create = respx.post(f"{API}/servers").mock(
        return_value=httpx.Response(201, json={"server": {"id": 777, "networks": []}})
    )
    respx.get(f"{API}/servers/777").mock(
        side_effect=[
            httpx.Response(200, json={"server": {"id": 777, "networks": []}}),
            httpx.Response(
                200,
                json={
                    "server": {
                        "id": 777,
                        "networks": [
                            {
                                "type": "public",
                                "ips": [
                                    {"type": "ipv4", "ip": "203.0.113.50"},
                                    {"type": "ipv6", "ip": "2001:db8::1"},
                                ],
                            }
                        ],
                    }
                },
            ),
        ]
    )

    server = await hoster.create(
        name="wlfinder-x", ssh_pub_key="ssh-ed25519 AAA test", user_data=None
    )

    assert server.server_id == "777"
    assert server.public_ipv4 == "203.0.113.50"
    assert server.public_ipv6 == "2001:db8::1"
    assert server.region == "ru-1"
    sent = json.loads(create.calls.last.request.content)
    assert sent["preset_id"] == 4795
    assert sent["os_id"] == 99
    assert sent["ssh_keys_ids"] == [55]


@respx.mock
async def test_create_reuses_existing_ssh_key_and_encodes_cloud_init(
    hoster: TimewebHoster,
) -> None:
    respx.get(f"{API}/ssh-keys").mock(
        return_value=httpx.Response(
            200, json={"ssh_keys": [{"id": 9, "body": "ssh-ed25519 AAA test"}]}
        )
    )
    post_key = respx.post(f"{API}/ssh-keys")
    create = respx.post(f"{API}/servers").mock(
        return_value=httpx.Response(
            201,
            json={
                "server": {
                    "id": 1,
                    "networks": [
                        {"type": "public", "ips": [{"type": "ipv4", "ip": "198.51.100.1"}]}
                    ],
                }
            },
        )
    )

    server = await hoster.create(
        name="x", ssh_pub_key="ssh-ed25519 AAA test", user_data="#cloud-config\n"
    )

    assert server.public_ipv4 == "198.51.100.1"
    assert not post_key.called  # existing key reused, nothing uploaded
    sent = json.loads(create.calls.last.request.content)
    assert sent["ssh_keys_ids"] == [9]
    assert base64.b64decode(sent["cloud_init"]).decode() == "#cloud-config\n"


@respx.mock
async def test_delete_is_idempotent_on_404(hoster: TimewebHoster) -> None:
    respx.delete(f"{API}/servers/404").mock(return_value=httpx.Response(404))
    await hoster.delete("404")  # must not raise


@respx.mock
async def test_delete_ok(hoster: TimewebHoster) -> None:
    route = respx.delete(f"{API}/servers/12").mock(return_value=httpx.Response(204))
    await hoster.delete("12")
    assert route.called


@respx.mock
async def test_auth_error_raises(hoster: TimewebHoster) -> None:
    respx.get(f"{API}/account/status").mock(return_value=httpx.Response(401))
    with pytest.raises(HosterAuthError):
        await hoster.health_check()


@respx.mock
async def test_balance_error_on_402(hoster: TimewebHoster) -> None:
    respx.get(f"{API}/ssh-keys").mock(
        return_value=httpx.Response(200, json={"ssh_keys": [{"id": 9, "body": "k"}]})
    )
    respx.post(f"{API}/servers").mock(return_value=httpx.Response(402))
    with pytest.raises(BalanceError):
        await hoster.create(name="x", ssh_pub_key="k", user_data=None)


@respx.mock
async def test_retry_on_429_then_success(hoster: TimewebHoster) -> None:
    respx.get(f"{API}/account/status").mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json={"status": {"balance": 150.0}}),
        ]
    )
    assert await hoster.health_check() is True


@respx.mock
async def test_rate_limit_exhausted_raises(hoster: TimewebHoster) -> None:
    respx.get(f"{API}/account/status").mock(return_value=httpx.Response(429))
    with pytest.raises(RateLimitError):
        await hoster.health_check()


@respx.mock
async def test_get_balance(hoster: TimewebHoster) -> None:
    respx.get(f"{API}/account/status").mock(
        return_value=httpx.Response(200, json={"status": {"balance": 42.5}})
    )
    assert await hoster.get_balance() == 42.5


@respx.mock
async def test_estimate_cost_per_hour(hoster: TimewebHoster) -> None:
    respx.get(f"{API}/presets/servers").mock(
        return_value=httpx.Response(
            200,
            json={"server_presets": [{"id": 4795, "price": 720.0}, {"id": 1, "price": 9999}]},
        )
    )
    # 720 ₽/month / 720 h == 1.0 ₽/h
    assert await hoster.estimate_cost_per_hour() == 1.0
