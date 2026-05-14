"""Tests for the Cloud.ru Evolution hoster integration (REST, HTTP mocked)."""

import json
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from wlfinder.hosters.cloudru import CloudRuConfig, CloudRuHoster

IAM = "https://iam.api.cloud.ru/api/v1/auth/token"
C = "https://compute.api.cloud.ru"


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
    cfg = CloudRuConfig.model_validate({"name": "cloudru-msk", "type": "cloudru"})
    async with httpx.AsyncClient() as client:
        yield CloudRuHoster(cfg, client)


def _token_ok() -> httpx.Response:
    return httpx.Response(200, json={"access_token": "at-1"})


@respx.mock
async def test_create_full_flow(hoster: CloudRuHoster) -> None:
    respx.post(IAM).mock(return_value=_token_ok())
    create = respx.post(f"{C}/api/v1.1/vms").mock(
        return_value=httpx.Response(202, json=[{"id": "vm-1", "state": "creating"}])
    )
    respx.get(f"{C}/api/v1/vms/vm-1").mock(
        side_effect=[
            httpx.Response(200, json={"id": "vm-1", "state": "creating", "interfaces": []}),
            httpx.Response(
                200,
                json={
                    "id": "vm-1",
                    "state": "running",
                    "interfaces": [{"id": "if-1", "ip_address": "10.0.0.5"}],
                },
            ),
        ]
    )
    respx.get(f"{C}/api/v1/availability-zones").mock(
        return_value=httpx.Response(200, json=[{"id": "zone-1", "name": "ru.AZ-1"}])
    )
    fip = respx.post(f"{C}/api/v1/floating-ips").mock(
        return_value=httpx.Response(201, json={"id": "fip-1", "ip_address": "203.0.113.50"})
    )

    server = await hoster.create(name="wlfinder-x", ssh_pub_key="ssh-ed25519 AAA t", user_data=None)

    assert server.server_id == "vm-1"
    assert server.public_ipv4 == "203.0.113.50"
    # create VM body is a *list* of one payload
    sent = json.loads(create.calls.last.request.content)
    assert isinstance(sent, list) and sent[0]["flavor_name"] == "lowcost10-1-1"
    assert sent[0]["image_metadata"]["public_key"] == "ssh-ed25519 AAA t"
    # floating IP was bound to the VM interface
    fip_body = json.loads(fip.calls.last.request.content)
    assert fip_body["interface_id"] == "if-1"
    assert fip_body["availability_zone_id"] == "zone-1"


@respx.mock
async def test_token_refreshed_on_401(hoster: CloudRuHoster) -> None:
    token = respx.post(IAM).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "at-1"}),
            httpx.Response(200, json={"access_token": "at-2"}),
        ]
    )
    respx.get(f"{C}/api/v1/flavors").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json=[])]
    )
    assert await hoster.health_check() is True
    assert token.call_count == 2  # initial + refresh


@respx.mock
async def test_delete_releases_floating_ip_first(hoster: CloudRuHoster) -> None:
    respx.post(IAM).mock(return_value=_token_ok())
    respx.get(f"{C}/api/v1/vms/vm-1").mock(
        return_value=httpx.Response(
            200, json={"id": "vm-1", "interfaces": [{"floating_ip": {"id": "fip-1"}}]}
        )
    )
    del_fip = respx.delete(f"{C}/api/v1/floating-ips/fip-1").mock(
        return_value=httpx.Response(204)
    )
    del_vm = respx.delete(f"{C}/api/v1/vms/vm-1").mock(return_value=httpx.Response(204))

    await hoster.delete("vm-1")

    assert del_fip.called
    assert del_vm.called


@respx.mock
async def test_delete_is_idempotent_on_404(hoster: CloudRuHoster) -> None:
    respx.post(IAM).mock(return_value=_token_ok())
    respx.get(f"{C}/api/v1/vms/404").mock(return_value=httpx.Response(404))
    respx.delete(f"{C}/api/v1/vms/404").mock(return_value=httpx.Response(404))
    await hoster.delete("404")  # must not raise


@respx.mock
async def test_list_servers(hoster: CloudRuHoster) -> None:
    respx.post(IAM).mock(return_value=_token_ok())
    respx.get(f"{C}/api/v1/vms").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "vm-1",
                        "name": "wlfinder-a",
                        "interfaces": [{"floating_ip": {"ip_address": "1.2.3.4"}}],
                    }
                ]
            },
        )
    )
    servers = await hoster.list_servers()
    assert servers[0].server_id == "vm-1"
    assert servers[0].public_ipv4 == "1.2.3.4"
