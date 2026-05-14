"""Timeweb Cloud hoster integration — floating-IP roulette.

Reverse-engineered against the live API. wlfinder's IP-roulette here runs
on **floating IPs**, not servers: a floating IPv4 can be allocated
standalone (no VPS at all), its address checked against the whitelist,
and released — instantly and for pennies, with no VPS provisioning, no
"не оплачено" billing failures, and no anti-abuse server-churn. On a hit
the floating IP is simply kept (reserved); attach a server to it later.

  POST /floating-ips        -> allocate a floating IPv4 in an availability zone
  GET  /floating-ips        -> list allocated floating IPs
  DELETE /floating-ips/{id} -> release one
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

_BASE_URL = "https://api.timeweb.cloud/api/v1"
# Timeweb rate-limits bursts with 403, so every API call across all parallel
# workers is spaced by at least this many seconds (one shared instance).
_MIN_REQUEST_INTERVAL = 0.8


class TimewebConfig(BaseModel):
    """The slice of ``config.yaml`` that a Timeweb hoster needs.

    wlfinder allocates *floating IPs* here, so no server preset/OS is
    required — only the availability zone the IPs come from.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    type: Literal["timeweb"] = "timeweb"
    enabled: bool = True
    token_env: str = "TIMEWEB_TOKEN"
    # Where to allocate floating IPs. Known zones: msk-1 (Moscow),
    # spb-1 / spb-2 (St. Petersburg), nsk-1 (Novosibirsk).
    availability_zone: str = "msk-1"


class TimewebHoster:
    """Floating-IP roulette client over the Timeweb Cloud v1 API."""

    def __init__(self, cfg: TimewebConfig, client: httpx.AsyncClient) -> None:
        self.name = cfg.name
        self._cfg = cfg
        self._client = client
        self._token = resolve_secret(cfg.token_env)
        # Shared request pacing — Timeweb 403s bursty traffic.
        self._rate_lock = asyncio.Lock()
        self._last_request = 0.0

    @classmethod
    def from_config(cls, raw: dict[str, Any], client: httpx.AsyncClient) -> TimewebHoster:
        return cls(TimewebConfig.model_validate(raw), client)

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token.get_secret_value()}",
            "Content-Type": "application/json",
        }

    async def _pace(self) -> None:
        """Space API calls by at least _MIN_REQUEST_INTERVAL, run-wide."""
        async with self._rate_lock:
            loop = asyncio.get_event_loop()
            wait = _MIN_REQUEST_INTERVAL - (loop.time() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = loop.time()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        ok: tuple[int, ...] = (200, 201, 204),
    ) -> httpx.Response:
        await self._pace()
        return await request_with_retries(
            self._client,
            method,
            f"{_BASE_URL}{path}",
            headers=self._headers,
            json=json,
            ok=ok,
            label="timeweb",
        )

    # -------------------------------------------------------------- protocol
    async def create(
        self,
        *,
        name: str,
        ssh_pub_key: str,
        user_data: str | None,
    ) -> CreatedServer:
        """Allocate a floating IPv4. ``ssh_pub_key``/``user_data`` are unused —
        no VPS is created, only an IP to test against the whitelist."""
        # `comment` carries the wlfinder-<ts> name so list_servers / destroy
        # can recognise IPs this tool allocated.
        body: dict[str, Any] = {
            "availability_zone": self._cfg.availability_zone,
            "is_ddos_guard": False,
            "comment": name,
        }
        resp = await self._request("POST", "/floating-ips", json=body, ok=(200, 201))
        fip = resp.json()["ip"]
        public_ipv4 = fip.get("ip")
        if not public_ipv4:
            fip_id = fip.get("id")
            if fip_id:
                await self._safe_release(str(fip_id))
            raise HosterError("timeweb: allocated floating IP has no address")
        return CreatedServer(
            hoster=self.name,
            server_id=str(fip["id"]),
            public_ipv4=str(public_ipv4),
            region=str(fip.get("availability_zone") or self._cfg.availability_zone),
            raw=fip,
        )

    async def delete(self, server_id: str) -> None:
        """Release a floating IP. ``server_id`` is the floating-IP id; 404 == gone."""
        resp = await self._request(
            "DELETE", f"/floating-ips/{server_id}", ok=(200, 202, 204, 404)
        )
        log.info("timeweb.released", floating_ip_id=server_id, status=resp.status_code)

    async def _safe_release(self, fip_id: str) -> None:
        try:
            await self._request("DELETE", f"/floating-ips/{fip_id}", ok=(200, 202, 204, 404))
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask the cause
            log.error("timeweb.cleanup_failed", floating_ip_id=fip_id, error=str(exc))

    async def list_servers(self) -> list[ServerInfo]:
        resp = await self._request("GET", "/floating-ips")
        return [
            ServerInfo(
                hoster=self.name,
                server_id=str(fip["id"]),
                name=str(fip.get("comment", "")),
                public_ipv4=fip.get("ip"),
                region=str(fip.get("availability_zone") or self._cfg.availability_zone),
            )
            for fip in resp.json().get("ips", [])
        ]

    async def health_check(self) -> bool:
        await self._request("GET", "/account/status")
        log.info("timeweb.health", hoster=self.name)
        return True

    async def get_balance(self) -> float | None:
        return None  # Timeweb does not expose a balance on /account/status

    async def estimate_cost_per_hour(self) -> float | None:
        return None  # floating IPs are billed monthly and cost pennies
