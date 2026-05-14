"""Tests for the Cloud.ru hoster integration (floating-IP roulette, HTTP mocked)."""

import json
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from wlfinder.hosters.base import HosterError
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
    cfg = CloudRuConfig.model_validate(
        {"name": "cloudru", "type": "cloudru", "availability_zone": "ru.AZ-1"}
    )
    async with httpx.AsyncClient() as client:
        yield CloudRuHoster(cfg, client)


def _token_ok() -> httpx.Response:
    return httpx.Response(200, json={"access_token": "at-1"})


@respx.mock
async def test_create_allocates_floating_ip(hoster: CloudRuHoster) -> None:
    respx.post(IAM).mock(return_value=_token_ok())
    create = respx.post(f"{C}/api/v1/floating-ips").mock(
        return_value=httpx.Response(
            201, json={"id": "fip-1", "ip_address": "176.109.109.49", "state": "creating"}
        )
    )
    # the floating IP is "creating" first, then settles to "available"
    respx.get(f"{C}/api/v1/floating-ips/fip-1").mock(
        side_effect=[
            httpx.Response(
                200,
                json={"id": "fip-1", "ip_address": "176.109.109.49", "state": "creating"},
            ),
            httpx.Response(
                200,
                json={
                    "id": "fip-1",
                    "ip_address": "176.109.109.49",
                    "state": "available",
                    "availability_zone": {"name": "ru.AZ-1"},
                },
            ),
        ]
    )

    server = await hoster.create(name="wlfinder-x", ssh_pub_key="unused", user_data=None)

    assert server.server_id == "fip-1"
    assert server.public_ipv4 == "176.109.109.49"
    body = json.loads(create.calls.last.request.content)
    assert body["availability_zone_name"] == "ru.AZ-1"
    assert body["name"] == "wlfinder-x"


@respx.mock
async def test_create_releases_floating_ip_with_no_address(hoster: CloudRuHoster) -> None:
    respx.post(IAM).mock(return_value=_token_ok())
    respx.post(f"{C}/api/v1/floating-ips").mock(
        return_value=httpx.Response(201, json={"id": "fip-bad", "state": "creating"})
    )
    respx.get(f"{C}/api/v1/floating-ips/fip-bad").mock(
        return_value=httpx.Response(
            200, json={"id": "fip-bad", "state": "available", "ip_address": None}
        )
    )
    released = respx.delete(f"{C}/api/v1/floating-ips/fip-bad").mock(
        return_value=httpx.Response(204)
    )

    with pytest.raises(HosterError):
        await hoster.create(name="x", ssh_pub_key="unused", user_data=None)

    assert released.called  # no leak


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
async def test_delete_releases_floating_ip(hoster: CloudRuHoster) -> None:
    respx.post(IAM).mock(return_value=_token_ok())
    route = respx.delete(f"{C}/api/v1/floating-ips/fip-1").mock(return_value=httpx.Response(204))
    await hoster.delete("fip-1")
    assert route.called


@respx.mock
async def test_delete_retries_through_creating_422(hoster: CloudRuHoster) -> None:
    respx.post(IAM).mock(return_value=_token_ok())
    route = respx.delete(f"{C}/api/v1/floating-ips/fip-1").mock(
        side_effect=[
            httpx.Response(422, json=[{"message": "still creating"}]),
            httpx.Response(204),
        ]
    )
    await hoster.delete("fip-1")
    assert route.call_count == 2


@respx.mock
async def test_delete_is_idempotent_on_404(hoster: CloudRuHoster) -> None:
    respx.post(IAM).mock(return_value=_token_ok())
    respx.delete(f"{C}/api/v1/floating-ips/gone").mock(return_value=httpx.Response(404))
    await hoster.delete("gone")  # must not raise


@respx.mock
async def test_list_servers(hoster: CloudRuHoster) -> None:
    respx.post(IAM).mock(return_value=_token_ok())
    respx.get(f"{C}/api/v1/floating-ips").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "fip-1",
                        "name": "wlfinder-a",
                        "ip_address": "1.2.3.4",
                        "availability_zone": {"name": "ru.AZ-1"},
                    }
                ]
            },
        )
    )
    servers = await hoster.list_servers()
    assert servers[0].server_id == "fip-1"
    assert servers[0].public_ipv4 == "1.2.3.4"
    assert servers[0].region == "ru.AZ-1"
