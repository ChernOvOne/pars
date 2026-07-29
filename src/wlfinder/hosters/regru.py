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
from wlfinder.models import ServerInfo

log = structlog.get_logger(__name__)

_BASE_URL = "https://api.cloudvps.reg.ru/v1"
_IP_POLL_INTERVAL = 2.0
_IP_POLL_TIMEOUT = 60.0
# A freshly created reglet rejects DELETE with 400 until it finishes
# provisioning (status new -> active). Retry through that window.
_DELETE_INTERVAL = 6.0
_DELETE_TIMEOUT = 180.0


class RegruConfig(BaseModel):
    """The slice of ``config.yaml`` that a REG.ru hoster needs."""

    model_config = ConfigDict(extra="ignore")

    name: str
    type: Literal["regru"] = "regru"
    enabled: bool = True
    batch_size: int | None = None
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
        self.batch_size = cfg.batch_size
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
        # The list endpoint has shipped both `ssh_keys` and `ssh_key` as the
        # array key across API revisions — accept either.
        body = listed.json()
        existing = body.get("ssh_keys") or body.get("ssh_key") or []
        for key in existing:
            if str(key.get("public_key", "")).strip() == ssh_pub_key.strip():
                self._fingerprints = [str(key["fingerprint"])]
                return self._fingerprints
        created = await self._request(
            "POST", "/account/keys", json={"name": "wlfinder", "public_key": ssh_pub_key}
        )
        payload = created.json()
        key = payload.get("ssh_keys") or payload.get("ssh_key") or payload
        self._fingerprints = [str(key["fingerprint"])]
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
        # RegletCreate schema: required `size` + `image`; `region_slug` is the
        # documented field name (not `region`). There is no `user_data` field —
        # cloud-init is not accepted by this API, so it is ignored.
        #
        # `floating_ip` MUST be false: with the API default (true) the create
        # call 500s on every request (allocating an extra reservable IP fails
        # server-side). The reglet still comes up with its own public IPv4 in
        # the `ip` field — that is the address the roulette checks.
        body: dict[str, Any] = {
            "name": name,
            "size": self._cfg.size,
            "image": self._cfg.image,
            "region_slug": self._cfg.region_slug,
            "ssh_keys": fingerprints,
            "floating_ip": False,
        }
        _ = user_data  # accepted for protocol parity; REG.ru has no user_data field

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

    async def promote(self, server: CreatedServer, ssh_pub_key: str) -> CreatedServer:
        return server  # create() already provisioned a real reglet

    async def delete(self, server_id: str) -> None:
        """Delete a reglet, waiting out the provisioning window.

        A reglet that is still ``new`` (mid-provisioning) rejects DELETE with
        400; it only becomes deletable once ``active``. We retry through that
        window. Idempotent: a 404 (already gone) counts as success.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + _DELETE_TIMEOUT
        while True:
            resp = await self._request(
                "DELETE", f"/reglets/{server_id}", ok=(200, 202, 204, 400, 404)
            )
            if resp.status_code != 400:
                log.info("regru.deleted", server_id=server_id, status=resp.status_code)
                return
            if loop.time() >= deadline:
                log.warning(
                    "regru.delete_timeout", server_id=server_id, body=resp.text[:200]
                )
                return
            await asyncio.sleep(_DELETE_INTERVAL)

    async def list_servers(self) -> list[ServerInfo]:
        resp = await self._request("GET", "/reglets")
        return [
            ServerInfo(
                hoster=self.name,
                server_id=str(reglet["id"]),
                name=str(reglet.get("name", "")),
                public_ipv4=_extract_ipv4(reglet),
                region=self._cfg.region_slug,
            )
            for reglet in resp.json().get("reglets", [])
        ]

    async def health_check(self) -> bool:
        # /account/keys is the cheapest authenticated endpoint — validates the token.
        await self._request("GET", "/account/keys")
        log.info("regru.health", hoster=self.name)
        return True

    async def get_balance(self) -> float | None:
        """Account balance in rubles, via /balance_data."""
        try:
            resp = await self._request("GET", "/balance_data")
        except HosterError:
            return None
        data = resp.json().get("balance_data", {})
        balance = data.get("balance")
        return float(balance) if balance is not None else None

    async def estimate_cost_per_hour(self) -> float | None:
        """Hourly price (rub) of the configured size, from /sizes."""
        try:
            resp = await self._request("GET", "/sizes")
        except HosterError:
            return None
        for size in resp.json().get("sizes", []):
            if str(size.get("slug")) == self._cfg.size:
                price = size.get("price")
                return float(price) if price is not None else None
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
