"""The IP-roulette main loop."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from wlfinder.checker import WhitelistChecker
from wlfinder.config import Config
from wlfinder.db import Database
from wlfinder.hosters.base import BalanceError, Hoster
from wlfinder.keeper import KeptServer, SshKeyPair, ensure_local_ssh_key, keep_server
from wlfinder.models import Attempt, CreatedServer, SuccessfulDeployment
from wlfinder.notifier import HitNotification, Notifier

log = structlog.get_logger(__name__)


class NoHitError(RuntimeError):
    """max_attempts was exhausted without landing in the whitelist."""


@dataclass
class RunResult:
    hit: bool
    attempts: int
    kept: KeptServer | None = None
    notified: bool = False
    cost_per_hour_rub: float | None = None


class Orchestrator:
    """Round-robins hosters, creating servers until one IP is whitelisted.

    On a hit the server is kept running, the admin is notified, and the run
    stops. On a miss the server is deleted and the loop continues.
    """

    def __init__(
        self,
        cfg: Config,
        db: Database,
        checker: WhitelistChecker,
        hosters: list[Hoster],
        notifier: Notifier,
        ssh_key: SshKeyPair | None = None,
    ) -> None:
        if not hosters:
            raise ValueError("orchestrator needs at least one enabled hoster")
        self._cfg = cfg
        self._db = db
        self._checker = checker
        self._hosters = hosters
        self._notifier = notifier
        self._ssh_key = ssh_key or ensure_local_ssh_key()

    def _pick_hoster(self, attempt: int) -> Hoster:
        return self._hosters[attempt % len(self._hosters)]

    async def _check_balance_or_bail(self, hoster: Hoster) -> None:
        threshold = self._cfg.orchestrator.bail_on_balance_threshold_rub
        balance = await hoster.get_balance()
        if balance is not None and balance < threshold:
            raise BalanceError(
                f"{hoster.name}: balance {balance:.2f}₽ is below the bail "
                f"threshold {threshold:.2f}₽"
            )

    async def run(self, *, max_attempts: int | None = None) -> RunResult:
        limit = max_attempts or self._cfg.orchestrator.max_attempts
        delay = self._cfg.orchestrator.delay_between_attempts_sec
        log.info(
            "orchestrator.start",
            max_attempts=limit,
            hosters=[h.name for h in self._hosters],
        )

        for attempt in range(limit):
            hoster = self._pick_hoster(attempt)
            await self._check_balance_or_bail(hoster)

            server: CreatedServer | None = None
            attempt_id: int | None = None
            try:
                server = await hoster.create(
                    name=f"wlfinder-{_timestamp()}",
                    ssh_pub_key=self._ssh_key.public,
                    user_data=None,
                )
                hit = self._checker.is_whitelisted(server.public_ipv4)
                attempt_id = await self._db.record_attempt(
                    Attempt(
                        hoster=hoster.name,
                        region=server.region,
                        server_id=server.server_id,
                        ipv4=server.public_ipv4,
                        ipv6=server.public_ipv6,
                        hit=hit,
                        raw_create=server.raw or None,
                    )
                )

                if hit:
                    log.info(
                        "orchestrator.hit",
                        ipv4=server.public_ipv4,
                        hoster=hoster.name,
                        attempt=attempt + 1,
                    )
                    result = await self._handle_hit(hoster, server, attempt + 1)
                    server = None  # kept on purpose — do not let cleanup delete it
                    return result

                log.info(
                    "orchestrator.miss",
                    ipv4=server.public_ipv4,
                    hoster=hoster.name,
                    attempt=attempt + 1,
                )
                await hoster.delete(server.server_id)
                await self._db.mark_deleted(attempt_id)
                server = None
            except BaseException as exc:  # noqa: BLE001 - cleanup then re-raise
                if isinstance(exc, KeyboardInterrupt | asyncio.CancelledError):
                    log.warning("orchestrator.interrupted")
                if server is not None:
                    await _safe_delete(hoster, server)
                    if attempt_id is not None:
                        await self._db.mark_deleted(attempt_id)
                raise

            if attempt + 1 < limit:
                await asyncio.sleep(delay)

        raise NoHitError(f"exhausted {limit} attempts without a whitelist hit")

    async def _handle_hit(
        self, hoster: Hoster, server: CreatedServer, attempt_no: int
    ) -> RunResult:
        kept = keep_server(server, self._ssh_key)
        cost = await _safe_cost(hoster)
        notified = await self._notifier.notify_hit(
            HitNotification(
                hoster=server.hoster,
                ipv4=server.public_ipv4,
                region=server.region,
                server_id=server.server_id,
                ts=datetime.now(UTC),
                ssh_command=kept.ssh_command,
                cost_per_hour_rub=cost,
            )
        )
        await self._db.record_deployment(
            SuccessfulDeployment(
                hoster=server.hoster,
                server_id=server.server_id,
                ipv4=server.public_ipv4,
                proxy_type="notify-only",
            )
        )
        return RunResult(
            hit=True,
            attempts=attempt_no,
            kept=kept,
            notified=notified,
            cost_per_hour_rub=cost,
        )


async def _safe_delete(hoster: Hoster, server: CreatedServer) -> None:
    try:
        await hoster.delete(server.server_id)
        log.info("orchestrator.cleanup_deleted", server_id=server.server_id)
    except Exception as exc:  # noqa: BLE001 - cleanup must not mask the original error
        log.error(
            "orchestrator.cleanup_failed",
            server_id=server.server_id,
            error=str(exc),
        )


async def _safe_cost(hoster: Hoster) -> float | None:
    try:
        return await hoster.estimate_cost_per_hour()
    except Exception as exc:  # noqa: BLE001 - cost is best-effort, never fatal
        log.warning("orchestrator.cost_estimate_failed", error=str(exc))
        return None


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
