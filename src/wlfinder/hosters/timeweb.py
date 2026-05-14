"""Timeweb Cloud hoster integration.

API docs: https://timeweb.cloud/api-docs  (base: /api/v1, Bearer auth).
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any, Literal

import httpx
import structlog
from pydantic import BaseModel, ConfigDict

from wlfinder.config import resolve_secret
from wlfinder.hosters._http import request_with_retries
from wlfinder.hosters.base import CreatedServer, HosterError

log = structlog.get_logger(__name__)

_BASE_URL = "https://api.timeweb.cloud/api/v1"
_IP_POLL_INTERVAL = 2.0
_IP_POLL_TIMEOUT = 60.0
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
    region: str = "ru-1"
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
            "POST", "/ssh-keys", json={"name": "wlfinder", "body": ssh_pub_key}
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

        ipv4 = _extract_ip(server, "ipv4")
        ipv6 = _extract_ip(server, "ipv6")
        if ipv4 is None:
            ipv4, ipv6, server = await self._poll_for_ip(server_id)
        if ipv4 is None:
            raise HosterError(
                f"timeweb: server {server_id} got no public IPv4 within "
                f"{_IP_POLL_TIMEOUT:.0f}s"
            )

        return CreatedServer(
            hoster=self.name,
            server_id=server_id,
            public_ipv4=ipv4,
            public_ipv6=ipv6,
            region=self._cfg.region,
            raw=server,
        )

    async def _poll_for_ip(
        self, server_id: str
    ) -> tuple[str | None, str | None, dict[str, Any]]:
        """Poll GET /servers/{id} until a public IPv4 shows up (spec §7.1)."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + _IP_POLL_TIMEOUT
        server: dict[str, Any] = {}
        while loop.time() < deadline:
            await asyncio.sleep(_IP_POLL_INTERVAL)
            resp = await self._request("GET", f"/servers/{server_id}")
            server = resp.json()["server"]
            ipv4 = _extract_ip(server, "ipv4")
            if ipv4 is not None:
                return ipv4, _extract_ip(server, "ipv6"), server
        return None, None, server

    async def delete(self, server_id: str) -> None:
        """Delete a server. Idempotent: a 404 (already gone) counts as success."""
        resp = await self._request("DELETE", f"/servers/{server_id}", ok=(200, 202, 204, 404))
        log.info("timeweb.deleted", server_id=server_id, status=resp.status_code)

    async def health_check(self) -> bool:
        resp = await self._request("GET", "/account/status")
        log.info("timeweb.health", hoster=self.name, balance=_extract_balance(resp.json()))
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
