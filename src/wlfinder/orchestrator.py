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
    """Creates servers until one IP is whitelisted.

    With ``orchestrator.parallel_workers > 1`` several attempts run
    concurrently: workers pull attempt slots from a shared counter, and the
    first whitelist hit is kept + notified while every other worker is
    cancelled and its in-flight server deleted. With ``parallel_workers: 1``
    this is a plain sequential loop.
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
        # Shared per-run state — (re)initialised at the top of run().
        self._next_attempt = 0
        self._slot_lock = asyncio.Lock()
        self._result: RunResult | None = None
        self._error: BaseException | None = None
        self._won = False
        self._workers: list[asyncio.Task[None]] = []

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
        workers_n = min(max(self._cfg.orchestrator.parallel_workers, 1), max(limit, 1))

        self._next_attempt = 0
        self._slot_lock = asyncio.Lock()
        self._result = None
        self._error = None
        self._won = False

        log.info(
            "orchestrator.start",
            max_attempts=limit,
            parallel_workers=workers_n,
            hosters=[h.name for h in self._hosters],
        )

        self._workers = [
            asyncio.create_task(self._worker(limit, delay)) for _ in range(workers_n)
        ]
        try:
            await asyncio.gather(*self._workers, return_exceptions=True)
        except asyncio.CancelledError:
            # run() itself was cancelled (e.g. Ctrl-C): stop the workers and
            # let them delete their in-flight servers before propagating.
            log.warning("orchestrator.interrupted")
            for w in self._workers:
                w.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)
            raise

        if self._error is not None:
            raise self._error
        if self._result is not None:
            return self._result
        raise NoHitError(f"exhausted {limit} attempts without a whitelist hit")

    async def _worker(self, limit: int, delay: int) -> None:
        """Claim attempt slots and run them until a hit, a fatal error, or exhaustion."""
        try:
            while True:
                async with self._slot_lock:
                    if self._won or self._error is not None or self._next_attempt >= limit:
                        return
                    attempt = self._next_attempt
                    self._next_attempt += 1
                result = await self._attempt(attempt)
                if result is not None:
                    self._result = result
                    self._cancel_siblings()
                    return
                if delay:
                    await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - fatal: stop the whole run
            async with self._slot_lock:
                if self._error is None:
                    self._error = exc
            self._cancel_siblings()

    def _cancel_siblings(self) -> None:
        """Cancel every other worker (its in-flight server is cleaned up on cancel)."""
        current = asyncio.current_task()
        for w in self._workers:
            if w is not current and not w.done():
                w.cancel()

    async def _attempt(self, attempt: int) -> RunResult | None:
        """One create -> check -> keep/delete cycle. Returns a RunResult on a hit."""
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
                # Only the first hit across all workers is kept; a later hit
                # (parallel race) deletes its server like a miss.
                async with self._slot_lock:
                    we_won = not self._won
                    if we_won:
                        self._won = True
                if we_won:
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
                    "orchestrator.hit_superseded",
                    ipv4=server.public_ipv4,
                    hoster=hoster.name,
                )
                await hoster.delete(server.server_id)
                await self._db.mark_deleted(attempt_id)
                server = None
                return None

            log.info(
                "orchestrator.miss",
                ipv4=server.public_ipv4,
                hoster=hoster.name,
                attempt=attempt + 1,
            )
            await hoster.delete(server.server_id)
            await self._db.mark_deleted(attempt_id)
            server = None
            return None
        except BaseException:  # noqa: BLE001 - clean up the in-flight server, then re-raise
            if server is not None:
                await _safe_delete(hoster, server)
                if attempt_id is not None:
                    await self._db.mark_deleted(attempt_id)
            raise

    async def _handle_hit(
        self, hoster: Hoster, server: CreatedServer, attempt_no: int
    ) -> RunResult:
        # Turn the kept resource into a usable server. Best-effort: the
        # whitelisted IP is the prize, so a failed promotion still notifies.
        try:
            server = await hoster.promote(server, self._ssh_key.public)
        except Exception as exc:  # noqa: BLE001 - promotion must not lose the hit
            log.warning(
                "orchestrator.promote_failed", ipv4=server.public_ipv4, error=str(exc)
            )
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
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
