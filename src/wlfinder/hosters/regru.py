"""REG.ru CloudVPS (Рег.облако) hoster integration.

API docs: https://developers.cloudvps.reg.ru/  (base: /v1, Bearer auth).
The API is DigitalOcean-shaped: servers are "reglets", SSH keys are referenced
by fingerprint, and the create response carries the public IP immediately.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field

from wlfinder.config import resolve_secret
from wlfinder.hosters._http import request_with_retries
from wlfinder.hosters.base import CreatedServer, HosterError

log = structlog.get_logger(__name__)

_BASE_URL = "https://api.cloudvps.reg.ru/v1"
_IP_POLL_INTERVAL = 2.0
_IP_POLL_TIMEOUT = 60.0


class RegruConfig(BaseModel):
    """The slice of ``config.yaml`` that a REG.ru hoster needs."""

    model_config = ConfigDict(extra="ignore")

    name: str
    type: Literal["regru"] = "regru"
    enabled: bool = True
    token_env: str = "REGRU_TOKEN"
    size: str = "cloud-1"
    image: str = "ubuntu-22-04-amd64"
    region_slug: str = "msk1"
    # Optional: pre-registered key fingerprints. If empty, the wlfinder key is
    # uploaded automatically on first use.
    ssh_key_fingerprints: list[str] = Field(default_factory=list)


class RegruHoster:
    """Thin async client over the REG.ru CloudVPS v1 API."""

    def __init__(self, cfg: RegruConfig, client: httpx.AsyncClient) -> None:
        self.name = cfg.name
        self._cfg = cfg
        self._client = client
        self._token = resolve_secret(cfg.token_env)
        self._fingerprints: list[str] | None = list(cfg.ssh_key_fingerprints) or None

    @classmethod
    def from_config(cls, raw: dict[str, Any], client: httpx.AsyncClient) -> RegruHoster:
        return cls(RegruConfig.model_validate(raw), client)

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
            label="regru",
        )

    # ------------------------------------------------------------- ssh keys
    async def _ensure_ssh_key(self, ssh_pub_key: str) -> list[str]:
        """Return SSH key fingerprints, uploading the wlfinder key if needed."""
        if self._fingerprints is not None:
            return self._fingerprints
        listed = await self._request("GET", "/account/keys")
        for key in listed.json().get("ssh_keys", []):
            if str(key.get("public_key", "")).strip() == ssh_pub_key.strip():
                self._fingerprints = [str(key["fingerprint"])]
                return self._fingerprints
        created = await self._request(
            "POST", "/account/keys", json={"name": "wlfinder", "public_key": ssh_pub_key}
        )
        self._fingerprints = [str(created.json()["ssh_key"]["fingerprint"])]
        return self._fingerprints

    # -------------------------------------------------------------- protocol
    async def create(
        self,
        *,
        name: str,
        ssh_pub_key: str,
        user_data: str | None,
    ) -> CreatedServer:
        fingerprints = await self._ensure_ssh_key(ssh_pub_key)
        body: dict[str, Any] = {
            "name": name,
            "size": self._cfg.size,
            "image": self._cfg.image,
            "region": self._cfg.region_slug,
            "ssh_keys": fingerprints,
        }
        if user_data:
            body["user_data"] = user_data  # REG.ru takes plain cloud-init, not base64

        resp = await self._request("POST", "/reglets", json=body, ok=(200, 201, 202))
        reglet = resp.json()["reglet"]
        reglet_id = str(reglet["id"])

        ipv4 = _extract_ipv4(reglet)
        ipv6 = _normalise_ipv6(reglet.get("ipv6"))
        if ipv4 is None:
            ipv4, ipv6, reglet = await self._poll_for_ip(reglet_id)
        if ipv4 is None:
            raise HosterError(
                f"regru: reglet {reglet_id} got no public IPv4 within "
                f"{_IP_POLL_TIMEOUT:.0f}s"
            )

        return CreatedServer(
            hoster=self.name,
            server_id=reglet_id,
            public_ipv4=ipv4,
            public_ipv6=ipv6,
            region=self._cfg.region_slug,
            raw=reglet,
        )

    async def _poll_for_ip(
        self, reglet_id: str
    ) -> tuple[str | None, str | None, dict[str, Any]]:
        """Reglet IPs are usually immediate; poll while status is still 'new'."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + _IP_POLL_TIMEOUT
        reglet: dict[str, Any] = {}
        while loop.time() < deadline:
            await asyncio.sleep(_IP_POLL_INTERVAL)
            resp = await self._request("GET", f"/reglets/{reglet_id}")
            reglet = resp.json()["reglet"]
            ipv4 = _extract_ipv4(reglet)
            if ipv4 is not None:
                return ipv4, _normalise_ipv6(reglet.get("ipv6")), reglet
        return None, None, reglet

    async def delete(self, server_id: str) -> None:
        """Delete a reglet. Idempotent: a 404 (already gone) counts as success."""
        resp = await self._request("DELETE", f"/reglets/{server_id}", ok=(200, 202, 204, 404))
        log.info("regru.deleted", server_id=server_id, status=resp.status_code)

    async def health_check(self) -> bool:
        # /account/keys is the cheapest authenticated endpoint — validates the token.
        await self._request("GET", "/account/keys")
        log.info("regru.health", hoster=self.name)
        return True

    async def get_balance(self) -> float | None:
        # The REG.ru CloudVPS API does not expose an account balance endpoint.
        return None

    async def estimate_cost_per_hour(self) -> float | None:
        # No public per-size pricing endpoint; left as best-effort unknown.
        return None


# --------------------------------------------------------------------- helpers
def _extract_ipv4(reglet: dict[str, Any]) -> str | None:
    ip = reglet.get("ip")
    if ip and ":" not in str(ip):
        return str(ip)
    return None


def _normalise_ipv6(value: Any) -> str | None:
    if value and ":" in str(value):
        return str(value)
    return None
