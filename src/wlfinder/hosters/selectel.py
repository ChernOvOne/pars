"""Selectel hoster integration — floating-IP roulette (OpenStack Neutron).

Auth is OpenStack Keystone v3: a *service user* (created once from the account
API key) gets a token scoped to the project, which then drives the per-region
Neutron API at ``<region>.cloud.api.selcloud.ru/network``. The roulette
allocates standalone floating IPs (no VM at all), checks each address against
the whitelist, and releases — distinct addresses are forced by holding a whole
batch at once.

Two Selectel-specific gotchas, both learned the hard way:
- the Keystone **domain name is the account NUMBER** (e.g. ``599260``), NOT the
  legacy selvpc id;
- the floating-IP quota is **per region**; some regions (e.g. ru-2) ship with a
  zero quota, so run the roulette in regions that have one (ru-1, ru-3, ru-8…).

Two roles must be assigned to the service user in the project:
- ``compute.admin`` — needed for the promote-on-hit path (build a VM);
- ``vpc.external_access.admin`` — required since 2026-07 to POST /floatingips;
  without it Neutron replies 403 ``rule:create_floatingip is disallowed by
  policy`` and the roulette is dead in the water.

Subnet camping (``target_subnet_cidr`` in config)
-------------------------------------------------
When a target CIDR is set, ``create()`` refuses to hand back an IP outside
that /24 by passing ``subnet_id`` to Neutron. Selectel's popular /24s are
usually 100 % allocated to other tenants, so ``POST /floatingips`` will reply
409 ``IpAddressGenerationFailure`` most of the time — that is not an error but
"pool is currently full, try again". We raise :class:`HosterError` with the
tag ``pool_exhausted``; the orchestrator counts the batch as a soft failure
and picks the next slot, so many tight loops eventually catch an IP the
moment its previous owner releases it. The winning IP is guaranteed to fall
inside the target /24 — no more roulette, only camping.
"""

from __future__ import annotations

import asyncio
import ipaddress
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


class _TokenCacheEntry:
    """Один shared Keystone-токен + expiry + lock, чтобы не логиниться дважды."""

    __slots__ = ("token", "expiry", "lock")

    def __init__(self) -> None:
        self.token: str | None = None
        self.expiry: float = 0.0
        self.lock = asyncio.Lock()


# Модульный кеш: ключ = (account_id, user, project_id) → shared token.
# Все SelectelHoster с одинаковыми creds делят один токен вместо N параллельных
# auth-запросов, которые триггерят Selectel Keystone rate-limit (transport_retry).
_TOKEN_CACHE: dict[tuple[str, str, str], _TokenCacheEntry] = {}
_CACHE_LOCK = asyncio.Lock()


async def _get_shared_token_entry(
    account_id: str, user: str, project_id: str
) -> _TokenCacheEntry:
    """Найти/создать общий entry для тройки credentials (thread-safe init)."""
    key = (account_id, user, project_id)
    async with _CACHE_LOCK:
        entry = _TOKEN_CACHE.get(key)
        if entry is None:
            entry = _TokenCacheEntry()
            _TOKEN_CACHE[key] = entry
    return entry


class SelectelConfig(BaseModel):
    """The slice of ``config.yaml`` that a Selectel hoster needs.

    wlfinder allocates *floating IPs* here, so only the service-user
    credentials, the project and the region are required. The external network
    is auto-discovered per region unless ``external_network_id`` is pinned.

    ``target_subnet_cidr`` turns on camp mode: every create() will only accept
    an IP from that /24 (see module docstring for the details).
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    type: Literal["selectel"] = "selectel"
    enabled: bool = True
    batch_size: int | None = None
    account_id_env: str = "SELECTEL_ACCOUNT_ID"
    service_user_env: str = "SELECTEL_SERVICE_USER"
    service_pass_env: str = "SELECTEL_SERVICE_PASS"
    project_id_env: str = "SELECTEL_PROJECT_ID"
    region: str = "ru-1"
    external_network_id: str | None = None
    # Целевая /24 (или /25 и т.п.) — если задана, floating IP будет пиниться в
    # ЭТУ конкретную подсеть через subnet_id, а не браться рандомно из пула
    # региона. Значение — CIDR-строка, например "46.182.24.0/24".
    target_subnet_cidr: str | None = None


class SelectelHoster:
    """Floating-IP roulette client over Selectel's OpenStack Keystone + Neutron."""

    def __init__(self, cfg: SelectelConfig, client: httpx.AsyncClient) -> None:
        self.name = cfg.name
        self.batch_size = cfg.batch_size
        self._cfg = cfg
        self._client = client
        self._account_id = resolve_secret(cfg.account_id_env)
        self._user = resolve_secret(cfg.service_user_env)
        self._password = resolve_secret(cfg.service_pass_env)
        self._project_id = resolve_secret(cfg.project_id_env)
        # Ключ shared-token'а: creds identity. Все hoster'ы с одинаковой
        # тройкой (account, user, project) шарят один Keystone-токен.
        self._creds_key = (
            self._account_id.get_secret_value(),
            self._user.get_secret_value(),
            self._project_id.get_secret_value(),
        )
        self._external_net: str | None = cfg.external_network_id
        # Ленивое сопоставление target_subnet_cidr → subnet_id (заполняется
        # первым же create()-запросом, потом кешируется до перезапуска).
        self._target_subnet_id: str | None = None
        self._target_cidr: ipaddress.IPv4Network | None = (
            ipaddress.ip_network(cfg.target_subnet_cidr, strict=False)  # type: ignore[assignment]
            if cfg.target_subnet_cidr
            else None
        )

    @classmethod
    def from_config(cls, raw: dict[str, Any], client: httpx.AsyncClient) -> SelectelHoster:
        return cls(SelectelConfig.model_validate(raw), client)

    @property
    def _network_url(self) -> str:
        return f"https://{self._cfg.region}.cloud.api.selcloud.ru/network/v2.0"

    # ----------------------------------------------------------------- auth
    async def _ensure_token(self, *, force: bool = False) -> str:
        """Вернуть shared Keystone-токен. При ``force=True`` — новый (после 401).

        Все SelectelHoster с одинаковыми credentials (account/user/project)
        делят один токен через модульный `_TOKEN_CACHE`. Логин в один момент
        времени делает только один hoster; остальные ждут его результата.
        Это критично: при 14 camp-хостерах одновременный старт триггерил
        Selectel Keystone rate-limit и порождал `hoster.transport_retry`.
        """
        entry = await _get_shared_token_entry(*self._creds_key)
        now = time.time()
        # быстрый путь: живой токен, не форсим
        if not force and entry.token is not None and now < entry.expiry - 60:
            return entry.token
        async with entry.lock:
            now = time.time()
            # пере-проверка: пока ждали lock, другой hoster уже обновил
            if not force and entry.token is not None and now < entry.expiry - 60:
                return entry.token
            body = {
                "auth": {
                    "identity": {
                        "methods": ["password"],
                        "password": {
                            "user": {
                                "name": self._user.get_secret_value(),
                                # Keystone domain name == Selectel account number.
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
            entry.token = token
            try:
                entry.expiry = _parse_iso(resp.json()["token"]["expires_at"])
            except (KeyError, ValueError, TypeError):
                entry.expiry = time.time() + 3600
            log.info(
                "selectel.token_refreshed",
                hoster=self.name,
                expires_in=int(entry.expiry - time.time()),
            )
            return token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        ok: tuple[int, ...] = (200, 201, 202, 204),
    ) -> httpx.Response:
        url = f"{self._network_url}{path}"
        token = await self._ensure_token()
        try:
            return await request_with_retries(
                self._client, method, url,
                headers={"X-Auth-Token": token, "Content-Type": "application/json"},
                json=json, params=params, ok=ok, label="selectel",
            )
        except HosterAuthError:
            # expired / rejected — форс-рефреш общего токена и одна попытка
            token = await self._ensure_token(force=True)
            return await request_with_retries(
                self._client, method, url,
                headers={"X-Auth-Token": token, "Content-Type": "application/json"},
                json=json, params=params, ok=ok, label="selectel",
            )

    async def _ensure_external_net(self) -> str:
        if self._external_net is not None:
            return self._external_net
        resp = await self._request(
            "GET", "/networks", params={"router:external": "true"}
        )
        nets = resp.json().get("networks", [])
        if not nets:
            raise HosterError(
                f"selectel: no external network in region {self._cfg.region}"
            )
        self._external_net = str(nets[0]["id"])
        log.info(
            "selectel.external_net",
            hoster=self.name,
            region=self._cfg.region,
            network_id=self._external_net,
        )
        return self._external_net

    async def _ensure_target_subnet_id(self) -> str | None:
        """При включённом camp-режиме резолвит CIDR → Neutron subnet_id.

        Один HTTP-запрос за инстанс (кеш живёт до перезапуска). Если целевой
        CIDR не найден в external pool региона — сразу выбрасываем
        HosterError, потому что дальше POST /floatingips с чужим subnet_id
        всё равно даст 400.
        """
        if self._target_cidr is None:
            return None
        if self._target_subnet_id is not None:
            return self._target_subnet_id
        resp = await self._request("GET", "/floatingip_pools")
        for pool in resp.json().get("floatingip_pools", []):
            cidr = pool.get("cidr")
            if not cidr:
                continue
            try:
                pool_net = ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                continue
            if pool_net == self._target_cidr:
                self._target_subnet_id = str(pool["subnet_id"])
                log.info(
                    "selectel.target_subnet_resolved",
                    hoster=self.name,
                    cidr=str(self._target_cidr),
                    subnet_id=self._target_subnet_id,
                )
                return self._target_subnet_id
        raise HosterError(
            f"selectel: target subnet {self._target_cidr} is not present in "
            f"the external pool of region {self._cfg.region}"
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
        are unused — no VM is created, only an IP to test.

        In camp mode (``target_subnet_cidr`` set) we pass ``subnet_id`` so the
        IP is forced into that specific /24. A 409 ``IpAddressGenerationFailure``
        means the /24 is momentarily 100 % allocated to other tenants —
        raise :class:`HosterError` with a ``pool_exhausted`` marker so the
        orchestrator treats the batch as a soft miss and tries again.
        """
        ext = await self._ensure_external_net()
        subnet_id = await self._ensure_target_subnet_id()
        body: dict[str, Any] = {"floating_network_id": ext}
        if subnet_id:
            body["subnet_id"] = subnet_id
        try:
            resp = await self._request(
                "POST",
                "/floatingips",
                json={"floatingip": body},
                ok=(200, 201),
            )
        except HosterError as exc:
            # request_with_retries сериализует тело ошибки в str(exc),
            # ищем в нём маркер Neutron'а.
            if "IpAddressGenerationFailure" in str(exc):
                raise HosterError(
                    f"selectel: pool_exhausted — {self._target_cidr or 'region '+self._cfg.region} "
                    f"has no free IPs right now (retry)"
                ) from exc
            raise
        fip = resp.json()["floatingip"]
        fip_id = str(fip["id"])
        ip = fip.get("floating_ip_address")
        if not ip:
            await self._safe_release(fip_id)
            raise HosterError(f"selectel: floating IP {fip_id} came back without an address")
        return CreatedServer(
            hoster=self.name,
            server_id=fip_id,
            public_ipv4=str(ip),
            region=self._cfg.region,
            raw=fip,
        )

    async def promote(self, server: CreatedServer, ssh_pub_key: str) -> CreatedServer:
        # Notify-only: a hit keeps the whitelisted floating IP reserved. Attach
        # it to a server manually (the IP is the prize, not a running VM).
        log.info("selectel.promote_skipped", floating_ip=server.public_ipv4)
        return server

    async def delete(self, server_id: str) -> None:
        """Release a floating IP. ``server_id`` is the floating-IP id."""
        resp = await self._request(
            "DELETE", f"/floatingips/{server_id}", ok=(200, 202, 204, 404)
        )
        log.info("selectel.released", floating_ip_id=server_id, status=resp.status_code)

    async def _safe_release(self, fip_id: str) -> None:
        try:
            await self.delete(fip_id)
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask the cause
            log.error("selectel.cleanup_failed", floating_ip_id=fip_id, error=str(exc))

    async def list_floating_ip_pools(self) -> list[dict[str, Any]]:
        """Все внешние подсети региона: [{cidr, subnet_id, subnet_name, network_id}].

        Используется командой ``pars scan``, чтобы построить карту «Selectel
        /24 × TWL» без ручного curl. Доступно всем service-user'ам (не нужна
        роль network.admin).
        """
        resp = await self._request("GET", "/floatingip_pools")
        return list(resp.json().get("floatingip_pools", []))

    async def list_servers(self) -> list[ServerInfo]:
        resp = await self._request("GET", "/floatingips")
        return [
            ServerInfo(
                hoster=self.name,
                server_id=str(f["id"]),
                name=str(f.get("description", "")),
                public_ipv4=f.get("floating_ip_address"),
                region=self._cfg.region,
            )
            for f in resp.json().get("floatingips", [])
        ]

    async def health_check(self) -> bool:
        await self._ensure_token()
        await self._request("GET", "/floatingips")
        log.info("selectel.health", hoster=self.name)
        return True

    async def get_balance(self) -> float | None:
        return None  # Selectel billing is a separate API.

    async def estimate_cost_per_hour(self) -> float | None:
        return None  # floating IPs are cheap and billed separately


def _parse_iso(value: str) -> float:
    # Keystone returns e.g. "2026-05-14T12:00:00.000000Z".
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
