"""CLO.ru hoster integration.

API docs: https://clo.ru/docs/  (Bearer token, OpenStack-style wrapper).
The spec only sketches this one ("эндпоинты в стиле OpenStack-обёртки,
аналогично Selectel, но проще") — endpoint shapes here are best-effort and
must be verified against a live token before production use.
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

_BASE_URL = "https://api.clo.ru/v1"
_IP_POLL_INTERVAL = 3.0
_IP_POLL_TIMEOUT = 120.0


class CloConfig(BaseModel):
    """The slice of ``config.yaml`` that a CLO.ru hoster needs."""

    model_config = ConfigDict(extra="ignore")

    name: str
    type: Literal["clo"] = "clo"
    enabled: bool = True
    token_env: str = "CLO_TOKEN"
    flavor: str  # plan / flavor id or slug
    image: str  # OS image id or slug
    region: str = "msk"
    network_id: str | None = None  # optional explicit public network


class CloHoster:
    """Thin async client over the CLO.ru API (OpenStack-style wrapper)."""

    def __init__(self, cfg: CloConfig, client: httpx.AsyncClient) -> None:
        self.name = cfg.name
        self._cfg = cfg
        self._client = client
        self._token = resolve_secret(cfg.token_env)

    @classmethod
    def from_config(cls, raw: dict[str, Any], client: httpx.AsyncClient) -> CloHoster:
        return cls(CloConfig.model_validate(raw), client)

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
        ok: tuple[int, ...] = (200, 201, 202, 204),
    ) -> httpx.Response:
        return await request_with_retries(
            self._client,
            method,
            f"{_BASE_URL}{path}",
            headers=self._headers,
            json=json,
            ok=ok,
            label="clo",
        )

    async def create(
        self,
        *,
        name: str,
        ssh_pub_key: str,
        user_data: str | None,
    ) -> CreatedServer:
        body: dict[str, Any] = {
            "name": name,
            "flavor": self._cfg.flavor,
            "image": self._cfg.image,
            "region": self._cfg.region,
            "ssh_key": ssh_pub_key,
        }
        if self._cfg.network_id:
            body["network_id"] = self._cfg.network_id
        if user_data:
            body["user_data"] = user_data

        resp = await self._request("POST", "/instances", json=body, ok=(200, 201, 202))
        instance = _unwrap(resp.json())
        instance_id = str(instance["id"])

        ipv4 = _extract_ipv4(instance)
        if ipv4 is None:
            ipv4, instance = await self._poll_for_ip(instance_id)
        if ipv4 is None:
            raise HosterError(
                f"clo: instance {instance_id} got no public IPv4 within "
                f"{_IP_POLL_TIMEOUT:.0f}s"
            )

        return CreatedServer(
            hoster=self.name,
            server_id=instance_id,
            public_ipv4=ipv4,
            region=self._cfg.region,
            raw=instance,
        )

    async def _poll_for_ip(self, instance_id: str) -> tuple[str | None, dict[str, Any]]:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + _IP_POLL_TIMEOUT
        instance: dict[str, Any] = {}
        while loop.time() < deadline:
            await asyncio.sleep(_IP_POLL_INTERVAL)
            resp = await self._request("GET", f"/instances/{instance_id}")
            instance = _unwrap(resp.json())
            ipv4 = _extract_ipv4(instance)
            if ipv4 is not None:
                return ipv4, instance
        return None, instance

    async def delete(self, server_id: str) -> None:
        resp = await self._request("DELETE", f"/instances/{server_id}", ok=(200, 202, 204, 404))
        log.info("clo.deleted", server_id=server_id, status=resp.status_code)

    async def list_servers(self) -> list[ServerInfo]:
        resp = await self._request("GET", "/instances")
        payload = resp.json()
        items = payload.get("instances", payload) if isinstance(payload, dict) else payload
        return [
            ServerInfo(
                hoster=self.name,
                server_id=str(i["id"]),
                name=str(i.get("name", "")),
                public_ipv4=_extract_ipv4(i),
                region=self._cfg.region,
            )
            for i in items
        ]

    async def health_check(self) -> bool:
        await self._request("GET", "/instances")
        log.info("clo.health", hoster=self.name)
        return True

    async def get_balance(self) -> float | None:
        try:
            resp = await self._request("GET", "/account")
        except HosterError:
            return None
        data = _unwrap(resp.json())
        balance = data.get("balance")
        try:
            return float(balance) if balance is not None else None
        except (TypeError, ValueError):
            return None

    async def estimate_cost_per_hour(self) -> float | None:
        return None


def _unwrap(payload: Any) -> dict[str, Any]:
    """CLO sometimes wraps the object as {"instance": {...}} / {"data": {...}}."""
    if isinstance(payload, dict):
        for key in ("instance", "data", "result"):
            inner = payload.get(key)
            if isinstance(inner, dict):
                return inner
        return payload
    return {}


def _extract_ipv4(instance: dict[str, Any]) -> str | None:
    for key in ("public_ip", "public_ipv4", "ip_address", "ip"):
        value = instance.get(key)
        if value and ":" not in str(value):
            return str(value)
    return None
