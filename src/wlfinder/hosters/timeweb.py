"""Timeweb Cloud hoster integration.

Reverse-engineered against the live API (api.timeweb.cloud/api/v1) — a
fresh Timeweb server has a public network interface but **no IP**; the
public IPv4 is a separately-allocated "floating IP":

  POST /servers                  -> create the server (no public IP yet)
  POST /floating-ips             -> allocate a floating IPv4 in the server's AZ
  POST /floating-ips/{id}/bind   -> attach that floating IP to the server
  DELETE /floating-ips/{id}  /  DELETE /servers/{id}

``create()`` runs all three steps and deletes the server (and floating IP)
itself if any step fails, so a partial failure never leaks resources.
"""

from __future__ import annotations

import base64
from typing import Any, Literal

import httpx
import structlog
from pydantic import BaseModel, ConfigDict

from wlfinder.config import resolve_secret
from wlfinder.hosters._http import request_with_retries
from wlfinder.hosters.base import CreatedServer, HosterError
from wlfinder.models import ServerInfo

log = structlog.get_logger(__name__)

_BASE_URL = "https://api.timeweb.cloud/api/v1"
_HOURS_PER_MONTH = 720  # Timeweb presets are priced per month.


class TimewebConfig(BaseModel):
    """The slice of ``config.yaml`` that a Timeweb hoster needs."""

    model_config = ConfigDict(extra="ignore")

    name: str
    type: Literal["timeweb"] = "timeweb"
    enabled: bool = True
    token_env: str = "TIMEWEB_TOKEN"
    preset_id: int
    os_id: int
    region: str = "ru-1"  # cosmetic label; the real AZ comes from the API
    bandwidth: int = 100


class TimewebHoster:
    """Thin async client over the Timeweb Cloud v1 API."""

    def __init__(self, cfg: TimewebConfig, client: httpx.AsyncClient) -> None:
        self.name = cfg.name
        self._cfg = cfg
        self._client = client
        self._token = resolve_secret(cfg.token_env)
        self._ssh_key_id: int | None = None

    @classmethod
    def from_config(cls, raw: dict[str, Any], client: httpx.AsyncClient) -> TimewebHoster:
        return cls(TimewebConfig.model_validate(raw), client)

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token.get_secret_value()}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        ok: tuple[int, ...] = (200, 201, 204),
    ) -> httpx.Response:
        return await request_with_retries(
            self._client,
            method,
            f"{_BASE_URL}{path}",
            headers=self._headers,
            json=json,
            ok=ok,
            label="timeweb",
        )

    # ------------------------------------------------------------- ssh keys
    async def _ensure_ssh_key(self, ssh_pub_key: str) -> int:
        """Upload our SSH key once, reusing it if Timeweb already has it."""
        if self._ssh_key_id is not None:
            return self._ssh_key_id
        listed = await self._request("GET", "/ssh-keys")
        for key in listed.json().get("ssh_keys", []):
            if str(key.get("body", "")).strip() == ssh_pub_key.strip():
                self._ssh_key_id = int(key["id"])
                return self._ssh_key_id
        created = await self._request(
            "POST",
            "/ssh-keys",
            json={"name": "wlfinder", "body": ssh_pub_key, "is_default": False},
        )
        self._ssh_key_id = int(created.json()["ssh_key"]["id"])
        return self._ssh_key_id

    # -------------------------------------------------------------- protocol
    async def create(
        self,
        *,
        name: str,
        ssh_pub_key: str,
        user_data: str | None,
    ) -> CreatedServer:
        ssh_key_id = await self._ensure_ssh_key(ssh_pub_key)
        body: dict[str, Any] = {
            "name": name,
            "preset_id": self._cfg.preset_id,
            "os_id": self._cfg.os_id,
            "bandwidth": self._cfg.bandwidth,
            "is_ddos_guard": False,
            "is_local_network": False,
            "ssh_keys_ids": [ssh_key_id],
        }
        if user_data:
            body["cloud_init"] = base64.b64encode(user_data.encode()).decode()

        resp = await self._request("POST", "/servers", json=body, ok=(200, 201))
        server = resp.json()["server"]
        server_id = str(server["id"])
        az = server.get("availability_zone")

        # From here on we own a server — delete it if anything below fails.
        fip_id: str | None = None
        try:
            fip = await self._create_floating_ip(az)
            fip_id = str(fip["id"])
            await self._bind_floating_ip(fip_id, server_id)
            public_ipv4 = fip.get("ip")
            if not public_ipv4:
                raise HosterError(
                    f"timeweb: floating IP for server {server_id} has no address"
                )
            return CreatedServer(
                hoster=self.name,
                server_id=server_id,
                public_ipv4=str(public_ipv4),
                region=str(az) if az else self._cfg.region,
                raw={"server": server, "floating_ip": fip},
            )
        except BaseException:
            await self._safe_cleanup(server_id, fip_id)
            raise

    async def _create_floating_ip(self, availability_zone: str | None) -> dict[str, Any]:
        if not availability_zone:
            raise HosterError(
                "timeweb: server response had no availability_zone — "
                "cannot allocate a floating IP"
            )
        resp = await self._request(
            "POST",
            "/floating-ips",
            json={"availability_zone": availability_zone, "is_ddos_guard": False},
            ok=(200, 201),
        )
        result: dict[str, Any] = resp.json()["ip"]
        return result

    async def _bind_floating_ip(self, fip_id: str, server_id: str) -> None:
        await self._request(
            "POST",
            f"/floating-ips/{fip_id}/bind",
            json={"resource_id": server_id, "resource_type": "server"},
            ok=(200, 201, 204),
        )

    async def _safe_cleanup(self, server_id: str, fip_id: str | None) -> None:
        """Best-effort teardown of a half-created server — never raises."""
        if fip_id is not None:
            try:
                await self._request(
                    "DELETE", f"/floating-ips/{fip_id}", ok=(200, 202, 204, 404)
                )
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask the cause
                log.error("timeweb.cleanup_fip_failed", fip_id=fip_id, error=str(exc))
        try:
            await self._request("DELETE", f"/servers/{server_id}", ok=(200, 202, 204, 404))
            log.info("timeweb.cleanup_deleted", server_id=server_id)
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask the cause
            log.error("timeweb.cleanup_failed", server_id=server_id, error=str(exc))

    async def delete(self, server_id: str) -> None:
        """Delete a server, releasing its floating IP first. A 404 == already gone."""
        try:
            resp = await self._request("GET", f"/servers/{server_id}", ok=(200, 404))
            if resp.status_code == 200:
                server = resp.json()["server"]
                for net in server.get("networks", []):
                    for ip in net.get("ips", []):
                        fip_id = ip.get("id")
                        if fip_id:
                            await self._request(
                                "DELETE", f"/floating-ips/{fip_id}", ok=(200, 202, 204, 404)
                            )
        except HosterError as exc:  # still try to delete the server itself
            log.warning("timeweb.fip_cleanup_failed", server_id=server_id, error=str(exc))
        resp = await self._request("DELETE", f"/servers/{server_id}", ok=(200, 202, 204, 404))
        log.info("timeweb.deleted", server_id=server_id, status=resp.status_code)

    async def list_servers(self) -> list[ServerInfo]:
        resp = await self._request("GET", "/servers")
        return [
            ServerInfo(
                hoster=self.name,
                server_id=str(server["id"]),
                name=str(server.get("name", "")),
                public_ipv4=_extract_ip(server, "ipv4"),
                region=str(server.get("availability_zone") or self._cfg.region),
            )
            for server in resp.json().get("servers", [])
        ]

    async def health_check(self) -> bool:
        await self._request("GET", "/account/status")
        log.info("timeweb.health", hoster=self.name)
        return True

    async def get_balance(self) -> float | None:
        resp = await self._request("GET", "/account/status")
        return _extract_balance(resp.json())

    async def estimate_cost_per_hour(self) -> float | None:
        try:
            resp = await self._request("GET", "/presets/servers")
        except HosterError:
            return None
        for preset in resp.json().get("server_presets", []):
            if preset.get("id") == self._cfg.preset_id and preset.get("price") is not None:
                return round(float(preset["price"]) / _HOURS_PER_MONTH, 4)
        return None


# --------------------------------------------------------------------- helpers
def _extract_ip(server: dict[str, Any], family: Literal["ipv4", "ipv6"]) -> str | None:
    """Pull the first public IP of *family* out of a Timeweb server object."""
    for net in server.get("networks", []):
        if net.get("type") != "public":
            continue
        for ip in net.get("ips", []):
            if isinstance(ip, dict):
                if ip.get("type") == family and ip.get("ip"):
                    return str(ip["ip"])
            elif isinstance(ip, str):
                is_v6 = ":" in ip
                if (family == "ipv6") == is_v6:
                    return ip
    return None


def _extract_balance(data: dict[str, Any]) -> float | None:
    for container in (data, data.get("status"), data.get("account")):
        if isinstance(container, dict) and "balance" in container:
            try:
                return float(container["balance"])
            except (TypeError, ValueError):
                return None
    return None
