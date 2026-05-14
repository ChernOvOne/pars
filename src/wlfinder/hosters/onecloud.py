"""1cloud.ru hoster integration.

API docs: https://1cloud.ru/api  (base: https://api.1cloud.ru, Bearer auth).
The 1cloud API uses PascalCase JSON fields and returns bare arrays for list
endpoints. Endpoint shapes are best-effort — verify against a live token.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

import httpx
import structlog
from pydantic import BaseModel, ConfigDict

from wlfinder.config import resolve_secret
from wlfinder.hosters._http import request_with_retries
from wlfinder.hosters.base import CreatedServer, HosterError
from wlfinder.models import ServerInfo

log = structlog.get_logger(__name__)

_BASE_URL = "https://api.1cloud.ru"
_IP_POLL_INTERVAL = 3.0
_IP_POLL_TIMEOUT = 120.0


class OneCloudConfig(BaseModel):
    """The slice of ``config.yaml`` that a 1cloud hoster needs."""

    model_config = ConfigDict(extra="ignore")

    name: str
    type: Literal["1cloud"] = "1cloud"
    enabled: bool = True
    token_env: str = "ONECLOUD_TOKEN"
    cpu: int = 1
    ram: int = 1024  # MB
    hdd: int = 20  # GB
    hdd_type: str = "SSD"
    image_id: int
    dc_location: str = "SdnLandsbergiPlatz"
    high_performance: bool = False


class OneCloudHoster:
    """Thin async client over the 1cloud.ru API."""

    def __init__(self, cfg: OneCloudConfig, client: httpx.AsyncClient) -> None:
        self.name = cfg.name
        self._cfg = cfg
        self._client = client
        self._token = resolve_secret(cfg.token_env)
        self._ssh_key_id: int | None = None

    @classmethod
    def from_config(cls, raw: dict[str, Any], client: httpx.AsyncClient) -> OneCloudHoster:
        return cls(OneCloudConfig.model_validate(raw), client)

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
            label="1cloud",
        )

    async def _ensure_ssh_key(self, ssh_pub_key: str) -> int:
        if self._ssh_key_id is not None:
            return self._ssh_key_id
        listed = await self._request("GET", "/sshkey")
        for key in listed.json():
            if str(key.get("Value", "")).strip() == ssh_pub_key.strip():
                self._ssh_key_id = int(key["ID"])
                return self._ssh_key_id
        created = await self._request(
            "POST", "/sshkey", json={"Name": "wlfinder", "Value": ssh_pub_key}
        )
        self._ssh_key_id = int(created.json()["ID"])
        return self._ssh_key_id

    async def create(
        self,
        *,
        name: str,
        ssh_pub_key: str,
        user_data: str | None,
    ) -> CreatedServer:
        ssh_key_id = await self._ensure_ssh_key(ssh_pub_key)
        body: dict[str, Any] = {
            "Name": name,
            "CPU": self._cfg.cpu,
            "RAM": self._cfg.ram,
            "HDD": self._cfg.hdd,
            "ImageID": self._cfg.image_id,
            "HDDType": self._cfg.hdd_type,
            "IsHighPerformance": self._cfg.high_performance,
            "DCLocation": self._cfg.dc_location,
            "SshKeys": [ssh_key_id],
        }
        resp = await self._request("POST", "/server", json=body, ok=(200, 201))
        server = resp.json()
        server_id = str(server["ID"])

        ipv4 = _extract_ip(server)
        if ipv4 is None:
            ipv4, server = await self._poll_for_ip(server_id)
        if ipv4 is None:
            raise HosterError(
                f"1cloud: server {server_id} got no public IP within "
                f"{_IP_POLL_TIMEOUT:.0f}s"
            )

        return CreatedServer(
            hoster=self.name,
            server_id=server_id,
            public_ipv4=ipv4,
            region=self._cfg.dc_location,
            raw=server,
        )

    async def _poll_for_ip(self, server_id: str) -> tuple[str | None, dict[str, Any]]:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + _IP_POLL_TIMEOUT
        server: dict[str, Any] = {}
        while loop.time() < deadline:
            await asyncio.sleep(_IP_POLL_INTERVAL)
            resp = await self._request("GET", f"/server/{server_id}")
            server = resp.json()
            ipv4 = _extract_ip(server)
            if ipv4 is not None:
                return ipv4, server
        return None, server

    async def delete(self, server_id: str) -> None:
        resp = await self._request("DELETE", f"/server/{server_id}", ok=(200, 202, 204, 404))
        log.info("1cloud.deleted", server_id=server_id, status=resp.status_code)

    async def list_servers(self) -> list[ServerInfo]:
        resp = await self._request("GET", "/server")
        return [
            ServerInfo(
                hoster=self.name,
                server_id=str(s["ID"]),
                name=str(s.get("Name", "")),
                public_ipv4=_extract_ip(s),
                region=self._cfg.dc_location,
            )
            for s in resp.json()
        ]

    async def health_check(self) -> bool:
        await self._request("GET", "/account")
        log.info("1cloud.health", hoster=self.name)
        return True

    async def get_balance(self) -> float | None:
        resp = await self._request("GET", "/account")
        data = resp.json()
        balance = data.get("Balance") if isinstance(data, dict) else None
        try:
            return float(balance) if balance is not None else None
        except (TypeError, ValueError):
            return None

    async def estimate_cost_per_hour(self) -> float | None:
        return None


def _extract_ip(server: dict[str, Any]) -> str | None:
    ip = server.get("IP")
    if ip and ":" not in str(ip):
        return str(ip)
    return None
