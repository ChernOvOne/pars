"""Tests for the Timeweb Cloud hoster integration (HTTP mocked with respx).

The real Timeweb create flow is: POST /servers -> POST /floating-ips ->
POST /floating-ips/{id}/bind.
"""

import base64
import json
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from wlfinder.hosters.base import BalanceError, HosterAuthError, HosterError, RateLimitError
from wlfinder.hosters.timeweb import TimewebConfig, TimewebHoster

API = "https://api.timeweb.cloud/api/v1"


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIMEWEB_TOKEN", "test-token-123")


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry backoff lives in _http — make it instant so tests stay fast."""

    async def _instant(_: float) -> None:
        return None

    monkeypatch.setattr("wlfinder.hosters._http.asyncio.sleep", _instant)


@pytest.fixture
def tw_cfg() -> TimewebConfig:
    return TimewebConfig(
        name="timeweb-msk",
        type="timeweb",
        token_env="TIMEWEB_TOKEN",
        preset_id=4799,
        os_id=99,
        region="ru-3",
        bandwidth=100,
    )


@pytest.fixture
async def hoster(tw_cfg: TimewebConfig) -> AsyncIterator[TimewebHoster]:
    async with httpx.AsyncClient() as client:
        yield TimewebHoster(tw_cfg, client)


def _server_resp(server_id: int = 777, az: str = "msk-1") -> httpx.Response:
    return httpx.Response(
        201,
        json={"server": {"id": server_id, "name": "wlfinder-x", "availability_zone": az}},
    )


@respx.mock
async def test_create_full_flow(hoster: TimewebHoster) -> None:
    respx.get(f"{API}/ssh-keys").mock(return_value=httpx.Response(200, json={"ssh_keys": []}))
    respx.post(f"{API}/ssh-keys").mock(
        return_value=httpx.Response(201, json={"ssh_key": {"id": 55}})
    )
    create = respx.post(f"{API}/servers").mock(return_value=_server_resp())
    fip = respx.post(f"{API}/floating-ips").mock(
        return_value=httpx.Response(
            201,
            json={
                "ip": {
                    "id": "fip-uuid-1",
                    "ip": "203.0.113.50",
                    "availability_zone": "msk-1",
                }
            },
        )
    )
    bind = respx.post(f"{API}/floating-ips/fip-uuid-1/bind").mock(
        return_value=httpx.Response(204)
    )

    server = await hoster.create(
        name="wlfinder-x", ssh_pub_key="ssh-ed25519 AAA test", user_data=None
    )

    assert server.server_id == "777"
    assert server.public_ipv4 == "203.0.113.50"
    assert server.region == "msk-1"  # the server's real AZ
    sent = json.loads(create.calls.last.request.content)
    assert sent["preset_id"] == 4799
    assert sent["os_id"] == 99
    assert sent["ssh_keys_ids"] == [55]
    # the floating IP is allocated in the server's AZ and bound to it
    assert json.loads(fip.calls.last.request.content)["availability_zone"] == "msk-1"
    assert json.loads(bind.calls.last.request.content) == {
        "resource_id": "777",
        "resource_type": "server",
    }


@respx.mock
async def test_create_reuses_ssh_key_and_encodes_cloud_init(hoster: TimewebHoster) -> None:
    respx.get(f"{API}/ssh-keys").mock(
        return_value=httpx.Response(
            200, json={"ssh_keys": [{"id": 9, "body": "ssh-ed25519 AAA test"}]}
        )
    )
    post_key = respx.post(f"{API}/ssh-keys")
    create = respx.post(f"{API}/servers").mock(return_value=_server_resp())
    respx.post(f"{API}/floating-ips").mock(
        return_value=httpx.Response(201, json={"ip": {"id": "fip-1", "ip": "198.51.100.1"}})
    )
    respx.post(f"{API}/floating-ips/fip-1/bind").mock(return_value=httpx.Response(204))

    server = await hoster.create(
        name="x", ssh_pub_key="ssh-ed25519 AAA test", user_data="#cloud-config\n"
    )

    assert server.public_ipv4 == "198.51.100.1"
    assert not post_key.called  # existing key reused
    sent = json.loads(create.calls.last.request.content)
    assert sent["ssh_keys_ids"] == [9]
    assert base64.b64decode(sent["cloud_init"]).decode() == "#cloud-config\n"


@respx.mock
async def test_create_cleans_up_server_on_later_failure(hoster: TimewebHoster) -> None:
    # server is created, but allocating the floating IP fails -> create() must
    # delete the server it just made instead of leaking it.
    respx.get(f"{API}/ssh-keys").mock(
        return_value=httpx.Response(200, json={"ssh_keys": [{"id": 9, "body": "k"}]})
    )
    respx.post(f"{API}/servers").mock(return_value=_server_resp(server_id=888))
    respx.post(f"{API}/floating-ips").mock(
        return_value=httpx.Response(400, json={"message": ["nope"]})
    )
    deleted = respx.delete(f"{API}/servers/888").mock(return_value=httpx.Response(204))

    with pytest.raises(HosterError):
        await hoster.create(name="x", ssh_pub_key="k", user_data=None)

    assert deleted.called  # no leak


@respx.mock
async def test_delete_releases_floating_ip(hoster: TimewebHoster) -> None:
    respx.get(f"{API}/servers/777").mock(
        return_value=httpx.Response(
            200,
            json={
                "server": {
                    "id": 777,
                    "networks": [
                        {
                            "type": "public",
                            "ips": [{"ip": "1.2.3.4", "id": "fip-1", "type": "ipv4"}],
                        }
                    ],
                }
            },
        )
    )
    del_fip = respx.delete(f"{API}/floating-ips/fip-1").mock(return_value=httpx.Response(204))
    del_srv = respx.delete(f"{API}/servers/777").mock(return_value=httpx.Response(204))

    await hoster.delete("777")

    assert del_fip.called
    assert del_srv.called


@respx.mock
async def test_delete_is_idempotent_on_404(hoster: TimewebHoster) -> None:
    respx.get(f"{API}/servers/404").mock(return_value=httpx.Response(404))
    respx.delete(f"{API}/servers/404").mock(return_value=httpx.Response(404))
    await hoster.delete("404")  # must not raise


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
        side_effect=[httpx.Response(429), httpx.Response(200, json={"status": {}})]
    )
    assert await hoster.health_check() is True


@respx.mock
async def test_rate_limit_exhausted_raises(hoster: TimewebHoster) -> None:
    respx.get(f"{API}/account/status").mock(return_value=httpx.Response(429))
    with pytest.raises(RateLimitError):
        await hoster.health_check()


@respx.mock
async def test_list_servers(hoster: TimewebHoster) -> None:
    respx.get(f"{API}/servers").mock(
        return_value=httpx.Response(
            200,
            json={
                "servers": [
                    {
                        "id": 1,
                        "name": "wlfinder-a",
                        "availability_zone": "msk-1",
                        "networks": [
                            {"type": "public", "ips": [{"type": "ipv4", "ip": "1.2.3.4"}]}
                        ],
                    },
                    {"id": 2, "name": "other", "networks": []},
                ]
            },
        )
    )
    servers = await hoster.list_servers()
    assert {s.server_id for s in servers} == {"1", "2"}
    wl = [s for s in servers if s.name.startswith("wlfinder-")]
    assert len(wl) == 1
    assert wl[0].public_ipv4 == "1.2.3.4"
    assert wl[0].region == "msk-1"


@respx.mock
async def test_estimate_cost_per_hour(hoster: TimewebHoster) -> None:
    respx.get(f"{API}/presets/servers").mock(
        return_value=httpx.Response(
            200, json={"server_presets": [{"id": 4799, "price": 720.0}, {"id": 1, "price": 9999}]}
        )
    )
    assert await hoster.estimate_cost_per_hour() == 1.0  # 720 ₽/month / 720 h
