"""Cloud.ru Evolution hoster integration.

The real Cloud.ru Compute API is REST — the spec's gRPC-ish
``/compute/v1/instances`` sketch was wrong. Verified against the public
API reference and a working client (Emilmeister/openclaw-skills):

  auth:    POST https://iam.api.cloud.ru/api/v1/auth/token  {keyId, secret}
           -> {"access_token": ...}
  compute: https://compute.api.cloud.ru/api/v1[.1]/...  (Bearer auth)

A Cloud.ru VM only gets a *private* IP on creation. The public IPv4 comes
from a **floating IP** allocated from Cloud.ru's pool and attached to the
VM's network interface — so ``create()`` does: create VM -> wait for an
interface -> allocate a floating IP, and that floating IP is the
``public_ipv4`` wlfinder checks against the whitelist.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from typing import Any, Literal

import httpx
import structlog
from pydantic import BaseModel, ConfigDict

from wlfinder.config import resolve_secret
from wlfinder.hosters._http import request_with_retries
from wlfinder.hosters.base import CreatedServer, HosterAuthError, HosterError
from wlfinder.models import ServerInfo

log = structlog.get_logger(__name__)

_IAM_URL = "https://iam.api.cloud.ru"
_COMPUTE_URL = "https://compute.api.cloud.ru"
_POLL_INTERVAL = 4.0
_POLL_TIMEOUT = 300.0


class CloudRuConfig(BaseModel):
    """The slice of ``config.yaml`` that a Cloud.ru hoster needs.

    ``flavor`` / ``image`` / ``zone`` / ``subnet`` are *names* — list the
    available ones via the API (GET /api/v1/flavors, /images, /subnets,
    /availability-zones) or the console.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    type: Literal["cloudru"] = "cloudru"
    enabled: bool = True
    key_id_env: str = "CLOUDRU_KEY_ID"
    key_secret_env: str = "CLOUDRU_KEY_SECRET"
    project_id_env: str = "CLOUDRU_PROJECT_ID"
    flavor: str = "lowcost10-1-1"  # flavor_name
    image: str = "ubuntu-22.04"  # image_name
    zone: str = "ru.AZ-1"  # availability_zone_name
    subnet: str = "default"  # subnet_name
    disk_type: str = "SSD"
    disk_size: int = 10  # GB


class CloudRuHoster:
    """Thin async client over the Cloud.ru Evolution Compute REST API."""

    def __init__(self, cfg: CloudRuConfig, client: httpx.AsyncClient) -> None:
        self.name = cfg.name
        self._cfg = cfg
        self._client = client
        self._key_id = resolve_secret(cfg.key_id_env)
        self._key_secret = resolve_secret(cfg.key_secret_env)
        self._project_id = resolve_secret(cfg.project_id_env)
        self._token: str | None = None

    @classmethod
    def from_config(cls, raw: dict[str, Any], client: httpx.AsyncClient) -> CloudRuHoster:
        return cls(CloudRuConfig.model_validate(raw), client)

    @property
    def _project(self) -> str:
        return self._project_id.get_secret_value()

    # ----------------------------------------------------------------- auth
    async def _ensure_token(self) -> str:
        if self._token is not None:
            return self._token
        resp = await self._client.post(
            f"{_IAM_URL}/api/v1/auth/token",
            json={
                "keyId": self._key_id.get_secret_value(),
                "secret": self._key_secret.get_secret_value(),
            },
        )
        if resp.status_code in (401, 403):
            raise HosterAuthError(f"cloudru: auth rejected ({resp.status_code})")
        if resp.status_code != 200:
            raise HosterError(f"cloudru: auth failed ({resp.status_code}): {resp.text[:200]}")
        self._token = str(resp.json()["access_token"])
        return self._token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        ok: tuple[int, ...] = (200, 201, 202, 204),
    ) -> httpx.Response:
        url = f"{_COMPUTE_URL}{path}"
        token = await self._ensure_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Request-ID": str(uuid.uuid4()),
        }
        try:
            return await request_with_retries(
                self._client, method, url, headers=headers, json=json, params=params, ok=ok,
                label="cloudru",
            )
        except HosterAuthError:
            self._token = None  # expired/revoked — refresh once and retry
            headers["Authorization"] = f"Bearer {await self._ensure_token()}"
            return await request_with_retries(
                self._client, method, url, headers=headers, json=json, params=params, ok=ok,
                label="cloudru",
            )

    # ------------------------------------------------------------- protocol
    async def create(
        self,
        *,
        name: str,
        ssh_pub_key: str,
        user_data: str | None,
    ) -> CreatedServer:
        payload: dict[str, Any] = {
            "project_id": self._project,
            "name": name,
            "flavor_name": self._cfg.flavor,
            "image_name": self._cfg.image,
            "availability_zone_name": self._cfg.zone,
            "disks": [
                {
                    "name": f"{name}-boot",
                    "size": self._cfg.disk_size,
                    "disk_type_name": self._cfg.disk_type,
                }
            ],
            "interfaces": [{"subnet_name": self._cfg.subnet}],
            "image_metadata": {
                "name": "wlfinder",
                "hostname": name,
                "public_key": ssh_pub_key,
            },
        }
        if user_data:
            payload["cloud_init"] = base64.b64encode(user_data.encode()).decode()

        # The v1.1 endpoint takes a *list* and returns a *list*.
        resp = await self._request("POST", "/api/v1.1/vms", json=[payload], ok=(200, 201, 202))
        body = resp.json()
        vm = body[0] if isinstance(body, list) else body
        vm_id = str(vm["id"])

        interface_id, raw_vm = await self._wait_for_interface(vm_id)
        if interface_id is None:
            raise HosterError(
                f"cloudru: VM {vm_id} got no network interface within {_POLL_TIMEOUT:.0f}s"
            )

        zone_id = await self._resolve_zone_id(self._cfg.zone)
        fip = await self._create_floating_ip(name, zone_id, interface_id)
        public_ipv4 = fip.get("ip_address")
        if not public_ipv4:
            raise HosterError(f"cloudru: floating IP for VM {vm_id} has no address")

        return CreatedServer(
            hoster=self.name,
            server_id=vm_id,
            public_ipv4=str(public_ipv4),
            region=self._cfg.zone,
            raw={"vm": raw_vm, "floating_ip": fip},
        )

    async def _wait_for_interface(self, vm_id: str) -> tuple[str | None, dict[str, Any]]:
        """Poll the VM until it has a network interface with an id."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + _POLL_TIMEOUT
        vm: dict[str, Any] = {}
        while loop.time() < deadline:
            await asyncio.sleep(_POLL_INTERVAL)
            resp = await self._request("GET", f"/api/v1/vms/{vm_id}")
            vm = resp.json()
            state = str(vm.get("state", ""))
            if state.startswith("error"):
                raise HosterError(f"cloudru: VM {vm_id} entered state {state!r}")
            for iface in vm.get("interfaces", []):
                if iface.get("id"):
                    return str(iface["id"]), vm
        return None, vm

    async def _resolve_zone_id(self, zone_name: str) -> str:
        resp = await self._request("GET", "/api/v1/availability-zones")
        data = resp.json()
        zones = data if isinstance(data, list) else data.get("items", [])
        for zone in zones:
            if zone.get("name") == zone_name:
                return str(zone["id"])
        raise HosterError(f"cloudru: availability zone {zone_name!r} not found")

    async def _create_floating_ip(
        self, name: str, zone_id: str, interface_id: str
    ) -> dict[str, Any]:
        payload = {
            "name": f"wlfinder-fip-{name}"[:60],
            "project_id": self._project,
            "availability_zone_id": zone_id,
            "interface_id": interface_id,
        }
        resp = await self._request(
            "POST", "/api/v1/floating-ips", json=payload, ok=(200, 201, 202)
        )
        result: dict[str, Any] = resp.json()
        return result

    async def delete(self, server_id: str) -> None:
        """Delete a VM. Floating IPs are released first (Cloud.ru requires it)."""
        try:
            resp = await self._request("GET", f"/api/v1/vms/{server_id}", ok=(200, 404))
            if resp.status_code == 200:
                for iface in resp.json().get("interfaces", []):
                    fip = iface.get("floating_ip")
                    if isinstance(fip, dict) and fip.get("id"):
                        await self._request(
                            "DELETE",
                            f"/api/v1/floating-ips/{fip['id']}",
                            ok=(200, 202, 204, 404),
                        )
        except HosterError as exc:  # cleanup is best-effort — still try the VM
            log.warning("cloudru.fip_cleanup_failed", server_id=server_id, error=str(exc))

        resp = await self._request("DELETE", f"/api/v1/vms/{server_id}", ok=(200, 202, 204, 404))
        log.info("cloudru.deleted", server_id=server_id, status=resp.status_code)

    async def list_servers(self) -> list[ServerInfo]:
        resp = await self._request("GET", "/api/v1/vms", params={"project_id": self._project})
        data = resp.json()
        items = data.get("items", []) if isinstance(data, dict) else data
        return [
            ServerInfo(
                hoster=self.name,
                server_id=str(vm["id"]),
                name=str(vm.get("name", "")),
                public_ipv4=_public_ip(vm),
                region=self._cfg.zone,
            )
            for vm in items
        ]

    async def health_check(self) -> bool:
        await self._ensure_token()
        await self._request("GET", "/api/v1/flavors")
        log.info("cloudru.health", hoster=self.name)
        return True

    async def get_balance(self) -> float | None:
        return None  # Cloud.ru billing is a separate API.

    async def estimate_cost_per_hour(self) -> float | None:
        return None


def _public_ip(vm: dict[str, Any]) -> str | None:
    """The VM's floating (public) IPv4, if one is attached."""
    for iface in vm.get("interfaces", []):
        fip = iface.get("floating_ip")
        if isinstance(fip, dict) and fip.get("ip_address"):
            return str(fip["ip_address"])
    return None
