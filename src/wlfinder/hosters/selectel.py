"""Selectel hoster integration (OpenStack Keystone v3 + Nova).

Auth is two-step: a service user gets a Keystone token from
cloud.api.selcloud.ru, then that token is used against the OpenStack Nova
API at ``<region>.cloud.api.selcloud.ru``. The most complex hoster — the
flavor / image / network IDs must be looked up once in your project and
put into config.yaml.
"""

from __future__ import annotations

import asyncio
import base64
import time
from datetime import datetime
from typing import Any, Literal

import httpx
import structlog
from pydantic import BaseModel, ConfigDict

from wlfinder.config import resolve_secret
from wlfinder.hosters._http import request_with_retries
from wlfinder.hosters.base import CreatedServer, HosterAuthError, HosterError
from wlfinder.models import ServerInfo

log = structlog.get_logger(__name__)

_KEYSTONE_URL = "https://cloud.api.selcloud.ru/identity/v3/auth/tokens"
_IP_POLL_INTERVAL = 3.0
_IP_POLL_TIMEOUT = 180.0


class SelectelConfig(BaseModel):
    """The slice of ``config.yaml`` that a Selectel hoster needs.

    ``flavor_id`` / ``image_id`` / ``network_id`` are OpenStack UUIDs — look
    them up once in the Selectel panel or via the Nova/Neutron APIs.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    type: Literal["selectel"] = "selectel"
    enabled: bool = True
    account_id_env: str = "SELECTEL_ACCOUNT_ID"
    service_user_env: str = "SELECTEL_SERVICE_USER"
    service_pass_env: str = "SELECTEL_SERVICE_PASS"
    project_id_env: str = "SELECTEL_PROJECT_ID"
    region: str = "ru-2"
    flavor_id: str
    image_id: str
    network_id: str


class SelectelHoster:
    """Thin async client over Selectel's OpenStack Keystone + Nova APIs."""

    def __init__(self, cfg: SelectelConfig, client: httpx.AsyncClient) -> None:
        self.name = cfg.name
        self._cfg = cfg
        self._client = client
        self._account_id = resolve_secret(cfg.account_id_env)
        self._user = resolve_secret(cfg.service_user_env)
        self._password = resolve_secret(cfg.service_pass_env)
        self._project_id = resolve_secret(cfg.project_id_env)
        self._token: str | None = None
        self._token_expiry = 0.0
        self._key_name: str | None = None

    @classmethod
    def from_config(cls, raw: dict[str, Any], client: httpx.AsyncClient) -> SelectelHoster:
        return cls(SelectelConfig.model_validate(raw), client)

    @property
    def _compute_url(self) -> str:
        return f"https://{self._cfg.region}.cloud.api.selcloud.ru/compute/v2.1"

    async def _ensure_token(self) -> str:
        if self._token is not None and time.time() < self._token_expiry - 60:
            return self._token
        body = {
            "auth": {
                "identity": {
                    "methods": ["password"],
                    "password": {
                        "user": {
                            "name": self._user.get_secret_value(),
                            "domain": {"name": self._account_id.get_secret_value()},
                            "password": self._password.get_secret_value(),
                        }
                    },
                },
                "scope": {"project": {"id": self._project_id.get_secret_value()}},
            }
        }
        resp = await request_with_retries(
            self._client,
            "POST",
            _KEYSTONE_URL,
            headers={"Content-Type": "application/json"},
            json=body,
            ok=(200, 201),
            label="selectel-auth",
        )
        token: str | None = resp.headers.get("X-Subject-Token")
        if not token:
            raise HosterAuthError("selectel: Keystone returned no X-Subject-Token")
        self._token = token
        try:
            self._token_expiry = _parse_iso(resp.json()["token"]["expires_at"])
        except (KeyError, ValueError, TypeError):
            self._token_expiry = time.time() + 3600
        return token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        ok: tuple[int, ...] = (200, 201, 202, 204),
    ) -> httpx.Response:
        url = f"{self._compute_url}{path}"
        token = await self._ensure_token()
        try:
            return await request_with_retries(
                self._client, method, url,
                headers={"X-Auth-Token": token, "Content-Type": "application/json"},
                json=json, ok=ok, label="selectel",
            )
        except HosterAuthError:
            self._token = None  # expired — refresh once and retry
            token = await self._ensure_token()
            return await request_with_retries(
                self._client, method, url,
                headers={"X-Auth-Token": token, "Content-Type": "application/json"},
                json=json, ok=ok, label="selectel",
            )

    async def _ensure_ssh_key(self, ssh_pub_key: str) -> str:
        if self._key_name is not None:
            return self._key_name
        listed = await self._request("GET", "/os-keypairs")
        for entry in listed.json().get("keypairs", []):
            kp = entry.get("keypair", entry)
            if str(kp.get("public_key", "")).strip() == ssh_pub_key.strip():
                self._key_name = str(kp["name"])
                return self._key_name
        await self._request(
            "POST",
            "/os-keypairs",
            json={"keypair": {"name": "wlfinder", "public_key": ssh_pub_key}},
            ok=(200, 201),
        )
        self._key_name = "wlfinder"
        return self._key_name

    async def create(
        self,
        *,
        name: str,
        ssh_pub_key: str,
        user_data: str | None,
    ) -> CreatedServer:
        key_name = await self._ensure_ssh_key(ssh_pub_key)
        server: dict[str, Any] = {
            "name": name,
            "flavorRef": self._cfg.flavor_id,
            "imageRef": self._cfg.image_id,
            "networks": [{"uuid": self._cfg.network_id}],
            "key_name": key_name,
        }
        if user_data:
            server["user_data"] = base64.b64encode(user_data.encode()).decode()

        resp = await self._request("POST", "/servers", json={"server": server}, ok=(200, 202))
        server_id = str(resp.json()["server"]["id"])

        ipv4, raw = await self._poll_for_ip(server_id)
        if ipv4 is None:
            raise HosterError(
                f"selectel: server {server_id} got no IPv4 within {_IP_POLL_TIMEOUT:.0f}s"
            )
        return CreatedServer(
            hoster=self.name,
            server_id=server_id,
            public_ipv4=ipv4,
            region=self._cfg.region,
            raw=raw,
        )

    async def _poll_for_ip(self, server_id: str) -> tuple[str | None, dict[str, Any]]:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + _IP_POLL_TIMEOUT
        server: dict[str, Any] = {}
        while loop.time() < deadline:
            await asyncio.sleep(_IP_POLL_INTERVAL)
            resp = await self._request("GET", f"/servers/{server_id}")
            server = resp.json()["server"]
            ipv4 = _extract_ipv4(server)
            if ipv4 is not None:
                return ipv4, server
        return None, server

    async def delete(self, server_id: str) -> None:
        resp = await self._request("DELETE", f"/servers/{server_id}", ok=(200, 202, 204, 404))
        log.info("selectel.deleted", server_id=server_id, status=resp.status_code)

    async def list_servers(self) -> list[ServerInfo]:
        resp = await self._request("GET", "/servers/detail")
        return [
            ServerInfo(
                hoster=self.name,
                server_id=str(s["id"]),
                name=str(s.get("name", "")),
                public_ipv4=_extract_ipv4(s),
                region=self._cfg.region,
            )
            for s in resp.json().get("servers", [])
        ]

    async def health_check(self) -> bool:
        await self._ensure_token()
        await self._request("GET", "/servers")
        log.info("selectel.health", hoster=self.name)
        return True

    async def get_balance(self) -> float | None:
        return None  # Selectel billing is a separate API.

    async def estimate_cost_per_hour(self) -> float | None:
        return None


def _extract_ipv4(server: dict[str, Any]) -> str | None:
    """Pull an IPv4 out of a Nova ``addresses`` map, preferring a floating IP."""
    addresses = server.get("addresses")
    if not isinstance(addresses, dict):
        return None
    floating: str | None = None
    fixed: str | None = None
    for entries in addresses.values():
        for entry in entries:
            if entry.get("version") != 4 or not entry.get("addr"):
                continue
            if entry.get("OS-EXT-IPS:type") == "floating":
                floating = str(entry["addr"])
            else:
                fixed = fixed or str(entry["addr"])
    return floating or fixed


def _parse_iso(value: str) -> float:
    # Keystone returns e.g. "2026-05-14T12:00:00.000000Z".
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
