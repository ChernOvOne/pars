"""Cloud.ru Evolution hoster integration — floating-IP roulette.

Like Timeweb, wlfinder's roulette here runs on **floating IPs**: a
floating IPv4 can be allocated standalone (no VM at all) via
``POST /api/v1/floating-ips``, its address checked against the whitelist,
and released. Cloud.ru floating IPs pass through a ``creating`` state
(~40 s) before they become ``available`` (and deletable), so ``create()``
waits for that.

Auth: a service-account key (Key ID + Key Secret) exchanged for a Bearer
token at iam.api.cloud.ru.
"""

from __future__ import annotations

import asyncio
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
_POLL_INTERVAL = 5.0
# A Cloud.ru floating IP is only stable / deletable once it leaves "creating".
_STABLE_STATES = frozenset({"available", "active", "in_use", "bound"})
_DELETE_RETRIES = 8
_DELETE_RETRY_DELAY = 8.0


class CloudRuConfig(BaseModel):
    """The slice of ``config.yaml`` that a Cloud.ru hoster needs.

    wlfinder allocates *floating IPs* here — only the credentials, project
    and availability zone are needed for the roulette.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    type: Literal["cloudru"] = "cloudru"
    enabled: bool = True
    key_id_env: str = "CLOUDRU_KEY_ID"
    key_secret_env: str = "CLOUDRU_KEY_SECRET"
    project_id_env: str = "CLOUDRU_PROJECT_ID"
    availability_zone: str = "ru.AZ-1"  # availability_zone_name
    create_timeout_sec: int = 300  # how long to wait for a floating IP to settle


class CloudRuHoster:
    """Floating-IP roulette client over the Cloud.ru Evolution REST API."""

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
        json: dict[str, Any] | None = None,
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
        """Allocate a standalone floating IPv4. ``ssh_pub_key``/``user_data``
        are unused — no VM is created, only an IP to test."""
        body: dict[str, Any] = {
            "name": name,
            "project_id": self._project,
            "availability_zone_name": self._cfg.availability_zone,
        }
        resp = await self._request("POST", "/api/v1/floating-ips", json=body, ok=(200, 201, 202))
        fip = resp.json()
        fip_id = str(fip["id"])

        ip, raw = await self._wait_available(fip_id, fip.get("ip_address"))
        if not ip:
            await self._safe_release(fip_id)
            raise HosterError(
                f"cloudru: floating IP {fip_id} got no address within "
                f"{self._cfg.create_timeout_sec}s"
            )
        return CreatedServer(
            hoster=self.name,
            server_id=fip_id,
            public_ipv4=str(ip),
            region=self._cfg.availability_zone,
            raw=raw,
        )

    async def _wait_available(
        self, fip_id: str, ip: str | None
    ) -> tuple[str | None, dict[str, Any]]:
        """Poll the floating IP until it leaves the transient 'creating' state."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self._cfg.create_timeout_sec
        raw: dict[str, Any] = {}
        while loop.time() < deadline:
            resp = await self._request("GET", f"/api/v1/floating-ips/{fip_id}")
            raw = resp.json()
            ip = raw.get("ip_address") or ip
            state = str(raw.get("state", ""))
            if state in _STABLE_STATES:
                return ip, raw
            if state.startswith("error"):
                raise HosterError(f"cloudru: floating IP {fip_id} entered state {state!r}")
            await asyncio.sleep(_POLL_INTERVAL)
        return ip, raw  # timed out — return whatever we have

    async def promote(self, server: CreatedServer, ssh_pub_key: str) -> CreatedServer:
        # Cloud.ru VM provisioning proved unreliable in testing (it could hang
        # in "creating" for hours), so a hit just keeps the whitelisted
        # floating IP reserved — attach a VM to it manually for now.
        log.info("cloudru.promote_skipped", floating_ip=server.public_ipv4)
        return server

    async def delete(self, server_id: str) -> None:
        """Release a floating IP. ``server_id`` is the floating-IP id.

        Cloud.ru rejects deletes of a floating IP still in 'creating' with a
        422, so this retries through that.
        """
        for attempt in range(_DELETE_RETRIES + 1):
            resp = await self._request(
                "DELETE", f"/api/v1/floating-ips/{server_id}", ok=(200, 202, 204, 404, 422)
            )
            if resp.status_code != 422:
                log.info("cloudru.released", floating_ip_id=server_id, status=resp.status_code)
                return
            if attempt < _DELETE_RETRIES:
                await asyncio.sleep(_DELETE_RETRY_DELAY)
        raise HosterError(f"cloudru: floating IP {server_id} not deletable after retries")

    async def _safe_release(self, fip_id: str) -> None:
        try:
            await self.delete(fip_id)
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask the cause
            log.error("cloudru.cleanup_failed", floating_ip_id=fip_id, error=str(exc))

    async def list_servers(self) -> list[ServerInfo]:
        resp = await self._request(
            "GET", "/api/v1/floating-ips", params={"project_id": self._project}
        )
        data = resp.json()
        items = data.get("items", []) if isinstance(data, dict) else data
        out: list[ServerInfo] = []
        for fip in items:
            zone = fip.get("availability_zone")
            zone_name = zone.get("name") if isinstance(zone, dict) else None
            out.append(
                ServerInfo(
                    hoster=self.name,
                    server_id=str(fip["id"]),
                    name=str(fip.get("name", "")),
                    public_ipv4=fip.get("ip_address"),
                    region=str(zone_name or self._cfg.availability_zone),
                )
            )
        return out

    async def health_check(self) -> bool:
        await self._ensure_token()
        await self._request("GET", "/api/v1/flavors")
        log.info("cloudru.health", hoster=self.name)
        return True

    async def get_balance(self) -> float | None:
        return None  # Cloud.ru billing is a separate API.

    async def estimate_cost_per_hour(self) -> float | None:
        return None  # floating IPs are cheap and billed separately
