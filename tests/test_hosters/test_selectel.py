"""Tests for the Selectel hoster (Keystone + Neutron floating IPs, HTTP mocked)."""

from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from wlfinder.hosters.selectel import SelectelConfig, SelectelHoster

KEYSTONE = "https://cloud.api.selcloud.ru/identity/v3/auth/tokens"
NEUTRON = "https://ru-1.cloud.api.selcloud.ru/network/v2.0"
EXT_NET = "ext-net-uuid"


@pytest.fixture(autouse=True)
def _creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SELECTEL_ACCOUNT_ID", "599260")
    monkeypatch.setenv("SELECTEL_SERVICE_USER", "svc-user")
    monkeypatch.setenv("SELECTEL_SERVICE_PASS", "svc-pass")
    monkeypatch.setenv("SELECTEL_PROJECT_ID", "proj-uuid")


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant(_: float) -> None:
        return None

    monkeypatch.setattr("wlfinder.hosters._http.asyncio.sleep", _instant)


@pytest.fixture
async def hoster() -> AsyncIterator[SelectelHoster]:
    cfg = SelectelConfig.model_validate(
        {"name": "selectel-spb", "type": "selectel", "region": "ru-1"}
    )
    async with httpx.AsyncClient() as client:
        yield SelectelHoster(cfg, client)


def _keystone_ok() -> httpx.Response:
    return httpx.Response(
        201,
        headers={"X-Subject-Token": "kt-1"},
        json={"token": {"expires_at": "2099-01-01T00:00:00.000000Z"}},
    )


@respx.mock
async def test_keystone_auth_and_create(hoster: SelectelHoster) -> None:
    respx.post(KEYSTONE).mock(return_value=_keystone_ok())
    respx.get(f"{NEUTRON}/networks").mock(
        return_value=httpx.Response(
            200, json={"networks": [{"id": EXT_NET, "router:external": True}]}
        )
    )
    create = respx.post(f"{NEUTRON}/floatingips").mock(
        return_value=httpx.Response(
            201,
            json={"floatingip": {"id": "fip-1", "floating_ip_address": "198.51.100.7"}},
        )
    )

    server = await hoster.create(name="wlfinder-x", ssh_pub_key="ssh-ed25519 AAA t", user_data=None)

    assert server.server_id == "fip-1"
    assert server.public_ipv4 == "198.51.100.7"
    assert server.region == "ru-1"
    assert create.calls.last.request.headers["X-Auth-Token"] == "kt-1"
    # the external network was auto-discovered and sent in the body
    assert EXT_NET in create.calls.last.request.content.decode()


@respx.mock
async def test_create_without_address_releases_and_raises(hoster: SelectelHoster) -> None:
    respx.post(KEYSTONE).mock(return_value=_keystone_ok())
    respx.get(f"{NEUTRON}/networks").mock(
        return_value=httpx.Response(200, json={"networks": [{"id": EXT_NET}]})
    )
    respx.post(f"{NEUTRON}/floatingips").mock(
        return_value=httpx.Response(201, json={"floatingip": {"id": "fip-2"}})
    )
    release = respx.delete(f"{NEUTRON}/floatingips/fip-2").mock(
        return_value=httpx.Response(204)
    )

    from wlfinder.hosters.base import HosterError

    with pytest.raises(HosterError):
        await hoster.create(name="x", ssh_pub_key="k", user_data=None)
    assert release.called  # the address-less floating IP was cleaned up


@respx.mock
async def test_delete_is_idempotent_on_404(hoster: SelectelHoster) -> None:
    respx.post(KEYSTONE).mock(return_value=_keystone_ok())
    respx.delete(f"{NEUTRON}/floatingips/404").mock(return_value=httpx.Response(404))
    await hoster.delete("404")


@respx.mock
async def test_camp_mode_resolves_target_and_pins_subnet_id() -> None:
    """Camp-mode: create() тянет /floatingip_pools, находит subnet_id для
    целевой /24 и передаёт его в теле POST /floatingips."""
    cfg = SelectelConfig.model_validate(
        {
            "name": "camp",
            "type": "selectel",
            "region": "ru-1",
            "target_subnet_cidr": "46.182.24.0/24",
        }
    )
    async with httpx.AsyncClient() as client:
        h = SelectelHoster(cfg, client)
        respx.post(KEYSTONE).mock(return_value=_keystone_ok())
        respx.get(f"{NEUTRON}/networks").mock(
            return_value=httpx.Response(200, json={"networks": [{"id": EXT_NET}]})
        )
        respx.get(f"{NEUTRON}/floatingip_pools").mock(
            return_value=httpx.Response(
                200,
                json={
                    "floatingip_pools": [
                        {"cidr": "9.9.9.0/24", "subnet_id": "other-sid"},
                        {"cidr": "46.182.24.0/24", "subnet_id": "target-sid"},
                    ]
                },
            )
        )
        create = respx.post(f"{NEUTRON}/floatingips").mock(
            return_value=httpx.Response(
                201,
                json={"floatingip": {"id": "fip-9", "floating_ip_address": "46.182.24.42"}},
            )
        )

        server = await h.create(name="x", ssh_pub_key="k", user_data=None)

        assert server.public_ipv4 == "46.182.24.42"
        body = create.calls.last.request.content.decode()
        assert "target-sid" in body  # subnet_id прописан в теле запроса


@respx.mock
async def test_camp_mode_pool_exhausted_maps_to_hoster_error() -> None:
    """409 IpAddressGenerationFailure становится HosterError с маркером
    pool_exhausted — orchestrator посчитает soft-miss и пойдёт дальше."""
    cfg = SelectelConfig.model_validate(
        {
            "name": "camp",
            "type": "selectel",
            "region": "ru-1",
            "target_subnet_cidr": "46.182.24.0/24",
        }
    )
    async with httpx.AsyncClient() as client:
        h = SelectelHoster(cfg, client)
        respx.post(KEYSTONE).mock(return_value=_keystone_ok())
        respx.get(f"{NEUTRON}/networks").mock(
            return_value=httpx.Response(200, json={"networks": [{"id": EXT_NET}]})
        )
        respx.get(f"{NEUTRON}/floatingip_pools").mock(
            return_value=httpx.Response(
                200,
                json={
                    "floatingip_pools": [
                        {"cidr": "46.182.24.0/24", "subnet_id": "target-sid"}
                    ]
                },
            )
        )
        respx.post(f"{NEUTRON}/floatingips").mock(
            return_value=httpx.Response(
                409,
                json={
                    "NeutronError": {
                        "type": "IpAddressGenerationFailure",
                        "message": "No more IP addresses available.",
                    }
                },
            )
        )

        from wlfinder.hosters.base import HosterError

        with pytest.raises(HosterError) as ei:
            await h.create(name="x", ssh_pub_key="k", user_data=None)
        assert "pool_exhausted" in str(ei.value)


@respx.mock
async def test_camp_mode_target_not_in_pool_raises() -> None:
    """Если целевая /24 отсутствует в external pool региона — HosterError
    без попыток POST /floatingips."""
    cfg = SelectelConfig.model_validate(
        {
            "name": "camp",
            "type": "selectel",
            "region": "ru-1",
            "target_subnet_cidr": "5.188.115.0/24",
        }
    )
    async with httpx.AsyncClient() as client:
        h = SelectelHoster(cfg, client)
        respx.post(KEYSTONE).mock(return_value=_keystone_ok())
        respx.get(f"{NEUTRON}/networks").mock(
            return_value=httpx.Response(200, json={"networks": [{"id": EXT_NET}]})
        )
        respx.get(f"{NEUTRON}/floatingip_pools").mock(
            return_value=httpx.Response(
                200,
                json={
                    "floatingip_pools": [
                        {"cidr": "46.182.24.0/24", "subnet_id": "other-sid"}
                    ]
                },
            )
        )

        from wlfinder.hosters.base import HosterError

        with pytest.raises(HosterError, match="not present in the external pool"):
            await h.create(name="x", ssh_pub_key="k", user_data=None)


@respx.mock
async def test_list_servers(hoster: SelectelHoster) -> None:
    respx.post(KEYSTONE).mock(return_value=_keystone_ok())
    respx.get(f"{NEUTRON}/floatingips").mock(
        return_value=httpx.Response(
            200,
            json={"floatingips": [{"id": "fip-1", "floating_ip_address": "1.2.3.4"}]},
        )
    )
    servers = await hoster.list_servers()
    assert servers[0].server_id == "fip-1"
    assert servers[0].public_ipv4 == "1.2.3.4"
