"""Tests for the Timeweb Cloud hoster integration (HTTP mocked with respx).

wlfinder's Timeweb roulette runs on floating IPs: POST /floating-ips to
allocate, DELETE /floating-ips/{id} to release — no VPS involved.
"""

import json
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from wlfinder.hosters.base import HosterAuthError, HosterError, RateLimitError
from wlfinder.hosters.timeweb import TimewebConfig, TimewebHoster
from wlfinder.models import CreatedServer

API = "https://api.timeweb.cloud/api/v1"


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIMEWEB_TOKEN", "test-token-123")


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant(_: float) -> None:
        return None

    # retry backoff lives in _http; the request-pacing sleep lives in timeweb
    monkeypatch.setattr("wlfinder.hosters._http.asyncio.sleep", _instant)
    monkeypatch.setattr("wlfinder.hosters.timeweb.asyncio.sleep", _instant)


@pytest.fixture
def tw_cfg() -> TimewebConfig:
    return TimewebConfig(
        name="timeweb-msk", type="timeweb", token_env="TIMEWEB_TOKEN", availability_zone="msk-1"
    )


@pytest.fixture
async def hoster(tw_cfg: TimewebConfig) -> AsyncIterator[TimewebHoster]:
    async with httpx.AsyncClient() as client:
        yield TimewebHoster(tw_cfg, client)


@respx.mock
async def test_create_allocates_floating_ip(hoster: TimewebHoster) -> None:
    create = respx.post(f"{API}/floating-ips").mock(
        return_value=httpx.Response(
            201,
            json={
                "ip": {
                    "id": "fip-uuid-1",
                    "ip": "203.0.113.50",
                    "availability_zone": "msk-1",
                    "comment": "wlfinder-x",
                }
            },
        )
    )

    server = await hoster.create(name="wlfinder-x", ssh_pub_key="unused", user_data=None)

    assert server.server_id == "fip-uuid-1"
    assert server.public_ipv4 == "203.0.113.50"
    assert server.region == "msk-1"
    body = json.loads(create.calls.last.request.content)
    assert body["availability_zone"] == "msk-1"
    assert body["comment"] == "wlfinder-x"  # so destroy/list can find it


@respx.mock
async def test_create_releases_ip_with_no_address(hoster: TimewebHoster) -> None:
    respx.post(f"{API}/floating-ips").mock(
        return_value=httpx.Response(201, json={"ip": {"id": "fip-bad", "ip": None}})
    )
    released = respx.delete(f"{API}/floating-ips/fip-bad").mock(return_value=httpx.Response(204))

    with pytest.raises(HosterError):
        await hoster.create(name="x", ssh_pub_key="unused", user_data=None)

    assert released.called  # no leak


@respx.mock
async def test_delete_releases_floating_ip(hoster: TimewebHoster) -> None:
    route = respx.delete(f"{API}/floating-ips/fip-1").mock(return_value=httpx.Response(204))
    await hoster.delete("fip-1")
    assert route.called


@respx.mock
async def test_delete_is_idempotent_on_404(hoster: TimewebHoster) -> None:
    respx.delete(f"{API}/floating-ips/gone").mock(return_value=httpx.Response(404))
    await hoster.delete("gone")  # must not raise


@respx.mock
async def test_auth_error_raises(hoster: TimewebHoster) -> None:
    respx.get(f"{API}/account/status").mock(return_value=httpx.Response(401))
    with pytest.raises(HosterAuthError):
        await hoster.health_check()


@respx.mock
async def test_retry_on_403_then_success(hoster: TimewebHoster) -> None:
    # 403 from Timeweb is soft rate-limiting — it should be retried, not fatal
    respx.get(f"{API}/account/status").mock(
        side_effect=[httpx.Response(403), httpx.Response(200, json={"status": {}})]
    )
    assert await hoster.health_check() is True


@respx.mock
async def test_rate_limit_exhausted_raises(hoster: TimewebHoster) -> None:
    respx.get(f"{API}/account/status").mock(return_value=httpx.Response(429))
    with pytest.raises(RateLimitError):
        await hoster.health_check()


@respx.mock
async def test_health_check_ok(hoster: TimewebHoster) -> None:
    respx.get(f"{API}/account/status").mock(return_value=httpx.Response(200, json={"status": {}}))
    assert await hoster.health_check() is True


@respx.mock
async def test_promote_creates_server_and_binds_ip() -> None:
    cfg = TimewebConfig.model_validate(
        {
            "name": "timeweb-msk",
            "type": "timeweb",
            "token_env": "TIMEWEB_TOKEN",
            "availability_zone": "msk-1",
            "preset_id": 4799,
        }
    )
    async with httpx.AsyncClient() as client:
        h = TimewebHoster(cfg, client)
        respx.get(f"{API}/ssh-keys").mock(return_value=httpx.Response(200, json={"ssh_keys": []}))
        respx.post(f"{API}/ssh-keys").mock(
            return_value=httpx.Response(201, json={"ssh_key": {"id": 55}})
        )
        create_vm = respx.post(f"{API}/servers").mock(
            return_value=httpx.Response(201, json={"server": {"id": 7777}})
        )
        bind = respx.post(f"{API}/floating-ips/fip-1/bind").mock(
            return_value=httpx.Response(204)
        )
        fip = CreatedServer(
            hoster="timeweb-msk",
            server_id="fip-1",
            public_ipv4="203.0.113.50",
            region="msk-1",
            raw={"id": "fip-1", "ip": "203.0.113.50", "comment": "wlfinder-x"},
        )
        promoted = await h.promote(fip, "ssh-ed25519 AAA test")

    assert promoted.server_id == "7777"  # now the VM id
    assert promoted.public_ipv4 == "203.0.113.50"  # the floating IP, unchanged
    sent = json.loads(create_vm.calls.last.request.content)
    assert sent["preset_id"] == 4799
    assert sent["name"] == "wlfinder-x"  # taken from the floating IP's comment
    assert json.loads(bind.calls.last.request.content) == {
        "resource_id": "7777",
        "resource_type": "server",
    }


async def test_promote_without_preset_keeps_floating_ip(hoster: TimewebHoster) -> None:
    # tw_cfg has no preset_id -> promote is a no-op (the IP stays reserved)
    fip = CreatedServer(
        hoster="timeweb-msk", server_id="fip-1", public_ipv4="1.2.3.4", region="msk-1", raw={}
    )
    promoted = await hoster.promote(fip, "ssh-ed25519 AAA test")
    assert promoted is fip  # unchanged, no API calls made


@respx.mock
async def test_list_servers(hoster: TimewebHoster) -> None:
    respx.get(f"{API}/floating-ips").mock(
        return_value=httpx.Response(
            200,
            json={
                "ips": [
                    {
                        "id": "fip-1",
                        "ip": "1.2.3.4",
                        "comment": "wlfinder-a",
                        "availability_zone": "msk-1",
                    },
                    {
                        "id": "fip-2",
                        "ip": "5.6.7.8",
                        "comment": "manual",
                        "availability_zone": "spb-1",
                    },
                ]
            },
        )
    )
    servers = await hoster.list_servers()
    assert {s.server_id for s in servers} == {"fip-1", "fip-2"}
    wl = [s for s in servers if s.name.startswith("wlfinder-")]
    assert len(wl) == 1
    assert wl[0].public_ipv4 == "1.2.3.4"
    assert wl[0].region == "msk-1"
