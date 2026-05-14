"""Cloud.ru Evolution hoster integration.

API docs: https://cloud.ru/docs/foundation/ug/topics/api-list.html
Auth: OAuth2 client_credentials — Key ID + Key Secret exchanged for an
access_token. The compute endpoint path is only sketched in the spec
("проверить точный path в swagger"); treat the create/list shapes as
best-effort and verify against a live key.
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any, Literal

import httpx
import structlog
from pydantic import BaseModel, ConfigDict

from wlfinder.config import resolve_secret
from wlfinder.hosters._http import request_with_retries
from wlfinder.hosters.base import CreatedServer, HosterAuthError, HosterError
from wlfinder.models import ServerInfo

log = structlog.get_logger(__name__)

_TOKEN_URL = "https://iam.api.cloud.ru/api/v1/auth/system/openid/token"
_BASE_URL = "https://api.cloud.ru/compute/v1"
_IP_POLL_INTERVAL = 3.0
_IP_POLL_TIMEOUT = 120.0


class CloudRuConfig(BaseModel):
    """The slice of ``config.yaml`` that a Cloud.ru hoster needs."""

    model_config = ConfigDict(extra="ignore")

    name: str
    type: Literal["cloudru"] = "cloudru"
    enabled: bool = True
    key_id_env: str = "CLOUDRU_KEY_ID"
    key_secret_env: str = "CLOUDRU_KEY_SECRET"
    project_id_env: str = "CLOUDRU_PROJECT_ID"
    flavor: str
    image: str
    region: str = "ru-central-1"
    network_id: str | None = None


class CloudRuHoster:
    """Thin async client over the Cloud.ru Evolution compute API."""

    def __init__(self, cfg: CloudRuConfig, client: httpx.AsyncClient) -> None:
        self.name = cfg.name
        self._cfg = cfg
        self._client = client
        self._key_id = resolve_secret(cfg.key_id_env)
        self._key_secret = resolve_secret(cfg.key_secret_env)
        self._project_id = resolve_secret(cfg.project_id_env)
        self._token: str | None = None
        self._token_expiry = 0.0

    @classmethod
    def from_config(cls, raw: dict[str, Any], client: httpx.AsyncClient) -> CloudRuHoster:
        return cls(CloudRuConfig.model_validate(raw), client)

    async def _ensure_token(self) -> str:
        if self._token is not None and time.time() < self._token_expiry - 30:
            return self._token
        resp = await self._client.post(
            _TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self._key_id.get_secret_value(),
                "client_secret": self._key_secret.get_secret_value(),
            },
        )
        if resp.status_code in (401, 403):
            raise HosterAuthError(f"cloudru: token exchange rejected ({resp.status_code})")
        if resp.status_code != 200:
            raise HosterError(f"cloudru: token exchange failed ({resp.status_code})")
        data = resp.json()
        self._token = str(data["access_token"])
        self._token_expiry = time.time() + float(data.get("expires_in", 3600))
        return self._token

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "X-Project-Id": self._project_id.get_secret_value(),
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
        url = f"{_BASE_URL}{path}"
        token = await self._ensure_token()
        try:
            return await request_with_retries(
                self._client, method, url, headers=self._headers(token), json=json, ok=ok,
                label="cloudru",
            )
        except HosterAuthError:
            self._token = None  # expired/revoked — refresh once and retry
            token = await self._ensure_token()
            return await request_with_retries(
                self._client, method, url, headers=self._headers(token), json=json, ok=ok,
                label="cloudru",
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
            "ssh_public_key": ssh_pub_key,
        }
        if self._cfg.network_id:
            body["network_id"] = self._cfg.network_id
        if user_data:
            body["user_data"] = base64.b64encode(user_data.encode()).decode()

        resp = await self._request("POST", "/instances", json=body, ok=(200, 201, 202))
        instance = _unwrap(resp.json())
        instance_id = str(instance["id"])

        ipv4 = _extract_ipv4(instance)
        if ipv4 is None:
            ipv4, instance = await self._poll_for_ip(instance_id)
        if ipv4 is None:
            raise HosterError(
                f"cloudru: instance {instance_id} got no public IPv4 within "
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
        log.info("cloudru.deleted", server_id=server_id, status=resp.status_code)

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
        await self._ensure_token()
        await self._request("GET", "/instances")
        log.info("cloudru.health", hoster=self.name)
        return True

    async def get_balance(self) -> float | None:
        return None  # Cloud.ru billing is a separate API.

    async def estimate_cost_per_hour(self) -> float | None:
        return None


def _unwrap(payload: Any) -> dict[str, Any]:
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
