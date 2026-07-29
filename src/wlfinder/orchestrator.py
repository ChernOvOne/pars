"""The IP-roulette main loop — batch-hold edition.

Each *batch* allocates ``batch_size`` IPs for one hoster and holds them all at
once while checking them: a provider cannot hand back an IP it is still
holding, so every IP within a batch is forced to be distinct. This is what
stops the roulette from drawing the same handful of recycled addresses over
and over.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from wlfinder.checker import WhitelistChecker
from wlfinder.config import Config
from wlfinder.db import Database
from wlfinder.hosters.base import (
    BalanceError,
    Hoster,
    HosterAuthError,
    HosterError,
)
from wlfinder.keeper import KeptServer, SshKeyPair, ensure_local_ssh_key, keep_server
from wlfinder.models import Attempt, CreatedServer, SuccessfulDeployment
from wlfinder.notifier import HitNotification, Notifier

log = structlog.get_logger(__name__)

# Circuit breaker: if this many batches fail in a row (every hoster erroring,
# a network outage, ...) the run aborts instead of spinning uselessly.
_MAX_CONSECUTIVE_ERRORS = 15


class NoHitError(RuntimeError):
    """max_attempts was exhausted without landing in the whitelist."""


@dataclass
class RunResult:
    hit: bool
    attempts: int
    kept: KeptServer | None = None
    notified: bool = False
    cost_per_hour_rub: float | None = None
    error_count: int = 0


class Orchestrator:
    """Creates servers in batches until one IP is whitelisted.

    A batch allocates ``batch_size`` IPs for one hoster and holds them all
    simultaneously while checking — forcing the provider to hand out distinct
    addresses across the batch. With ``parallel_workers > 1`` several batches
    (typically on different hosters) run concurrently; the first whitelist hit
    is kept + notified while every other worker is cancelled and its in-flight
    IPs released.
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
        self._allocated = 0
        self._batch_index = 0
        self._slot_lock = asyncio.Lock()
        self._result: RunResult | None = None
        self._error: BaseException | None = None
        self._won = False
        self._workers: list[asyncio.Task[None]] = []
        self._error_count = 0
        self._consecutive_errors = 0

    def _pick_hoster(self, batch_index: int) -> Hoster:
        return self._hosters[batch_index % len(self._hosters)]

    def _batch_size_for(self, hoster: Hoster) -> int:
        size = hoster.batch_size or self._cfg.orchestrator.batch_size
        return max(size, 1)

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

        self._allocated = 0
        self._batch_index = 0
        self._slot_lock = asyncio.Lock()
        self._result = None
        self._error = None
        self._won = False
        self._error_count = 0
        self._consecutive_errors = 0

        log.info(
            "orchestrator.start",
            max_attempts=limit,
            parallel_workers=workers_n,
            batch_size=self._cfg.orchestrator.batch_size,
            hosters=[h.name for h in self._hosters],
        )

        self._workers = [
            asyncio.create_task(self._worker(limit, delay)) for _ in range(workers_n)
        ]
        try:
            await asyncio.gather(*self._workers, return_exceptions=True)
        except asyncio.CancelledError:
            # run() itself was cancelled (e.g. Ctrl-C): stop the workers and
            # let them release their in-flight IPs before propagating.
            log.warning("orchestrator.interrupted")
            for w in self._workers:
                w.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)
            raise

        if self._error is not None:
            raise self._error
        if self._result is not None:
            return self._result
        raise NoHitError(
            f"exhausted {limit} attempts without a whitelist hit "
            f"({self._error_count} batch error(s) along the way)"
        )

    async def _worker(self, limit: int, delay: int) -> None:
        """Claim batch slots and run them until a hit, a fatal error, or exhaustion."""
        try:
            while True:
                async with self._slot_lock:
                    if self._won or self._error is not None or self._allocated >= limit:
                        return
                    batch_index = self._batch_index
                    self._batch_index += 1
                    hoster = self._pick_hoster(batch_index)
                    size = min(self._batch_size_for(hoster), limit - self._allocated)
                    self._allocated += size
                    attempts_so_far = self._allocated
                if size <= 0:
                    return
                try:
                    result = await self._run_batch(
                        batch_index, hoster, size, attempts_so_far
                    )
                except (BalanceError, HosterAuthError):
                    # Fatal: out of money / broken credentials — no point
                    # burning more batches.
                    raise
                except HosterError as exc:
                    # A whole batch failing (transient API outage, exhausted
                    # rate-limit) must NOT kill the run — log it, count it,
                    # take the next slot.
                    #
                    # `pool_exhausted` (subnet-camp: Selectel вернул 409
                    # IpAddressGenerationFailure) — это НЕ авария, а «сейчас
                    # свободных IP в /24 нет, попробуем ещё раз». Такой батч
                    # НЕ должен считаться в circuit-breaker, иначе первые же
                    # 15 попыток camp'а убьют весь прогон.
                    is_soft = "pool_exhausted" in str(exc)
                    async with self._slot_lock:
                        self._error_count += 1
                        if not is_soft:
                            self._consecutive_errors += 1
                        consec = self._consecutive_errors
                    log.warning(
                        "orchestrator.batch_error",
                        batch=batch_index,
                        hoster=hoster.name,
                        error=str(exc),
                        errors_total=self._error_count,
                        consecutive=consec,
                        soft=is_soft,
                    )
                    if not is_soft and consec >= _MAX_CONSECUTIVE_ERRORS:
                        raise HosterError(
                            f"aborting run: {consec} batches failed in a row"
                        ) from exc
                    if delay:
                        await asyncio.sleep(delay)
                    continue
                if result is not None:
                    result.error_count = self._error_count
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
        """Cancel every other worker (its in-flight IPs are released on cancel)."""
        current = asyncio.current_task()
        for w in self._workers:
            if w is not current and not w.done():
                w.cancel()

    async def _run_batch(
        self, batch_index: int, hoster: Hoster, size: int, attempts_so_far: int
    ) -> RunResult | None:
        """Allocate ``size`` IPs at once, hold them all, then check/keep/release."""
        await self._check_balance_or_bail(hoster)

        servers: list[CreatedServer] = []
        attempt_ids: dict[str, int] = {}
        try:
            # Allocate the whole batch concurrently. Every IP stays held until
            # the batch is resolved, so the provider is forced to hand out
            # distinct addresses across the batch.
            results = await asyncio.gather(
                *(
                    hoster.create(
                        name=f"wlfinder-{_timestamp()}",
                        ssh_pub_key=self._ssh_key.public,
                        user_data=None,
                    )
                    for _ in range(size)
                ),
                return_exceptions=True,
            )
            fatal: BaseException | None = None
            transient = 0
            for r in results:
                if isinstance(r, CreatedServer):
                    servers.append(r)
                elif isinstance(r, (BalanceError, HosterAuthError)):
                    fatal = fatal or r
                elif isinstance(r, BaseException):
                    transient += 1

            unique_ips = len({s.public_ipv4 for s in servers})
            log.info(
                "orchestrator.batch",
                batch=batch_index,
                hoster=hoster.name,
                requested=size,
                created=len(servers),
                unique_ips=unique_ips,
                errors=transient,
            )

            if fatal is not None:
                raise fatal
            if not servers:
                # The whole batch failed to allocate — surface it so the
                # worker counts it and the circuit breaker can trip.
                raise HosterError(
                    f"{hoster.name}: all {size} allocations in the batch failed"
                )
            async with self._slot_lock:
                # A batch that produced IPs counts as progress: clear the
                # consecutive-error streak, but still tally partial failures.
                self._consecutive_errors = 0
                if transient:
                    self._error_count += transient

            # Record every attempt; find the first whitelist hit in the batch.
            hit_server: CreatedServer | None = None
            for server in servers:
                is_hit = self._checker.is_whitelisted(server.public_ipv4)
                attempt_id = await self._db.record_attempt(
                    Attempt(
                        hoster=hoster.name,
                        region=server.region,
                        server_id=server.server_id,
                        ipv4=server.public_ipv4,
                        ipv6=server.public_ipv6,
                        hit=is_hit,
                        raw_create=server.raw or None,
                    )
                )
                attempt_ids[server.server_id] = attempt_id
                if is_hit:
                    if hit_server is None:
                        hit_server = server
                else:
                    log.info(
                        "orchestrator.miss",
                        ipv4=server.public_ipv4,
                        hoster=hoster.name,
                        batch=batch_index,
                    )

            if hit_server is not None:
                # Only the first hit across all workers is kept; a later hit
                # (parallel race) is released like a miss.
                async with self._slot_lock:
                    we_won = not self._won
                    if we_won:
                        self._won = True
                if we_won:
                    log.info(
                        "orchestrator.hit",
                        ipv4=hit_server.public_ipv4,
                        hoster=hoster.name,
                        batch=batch_index,
                    )
                    result = await self._handle_hit(
                        hoster, hit_server, attempts_so_far
                    )
                    # Release every other IP in the batch; keep the winner.
                    losers = [s for s in servers if s is not hit_server]
                    await self._release_all(hoster, losers, attempt_ids)
                    servers = []  # winner kept on purpose — skip finally cleanup
                    return result
                log.info(
                    "orchestrator.hit_superseded",
                    ipv4=hit_server.public_ipv4,
                    hoster=hoster.name,
                )

            # No kept hit — release the whole held batch.
            await self._release_all(hoster, servers, attempt_ids)
            servers = []
            return None
        except BaseException:
            # Cancelled by a sibling's win, or a fatal error mid-batch — release
            # whatever IPs we are still holding so nothing leaks.
            if servers:
                await self._release_all(hoster, servers, attempt_ids)
            raise

    async def _release_all(
        self,
        hoster: Hoster,
        servers: list[CreatedServer],
        attempt_ids: dict[str, int],
    ) -> None:
        """Release a set of held IPs concurrently; failures are logged, not fatal."""

        async def _one(server: CreatedServer) -> None:
            await _safe_delete(hoster, server)
            attempt_id = attempt_ids.get(server.server_id)
            if attempt_id is not None:
                await self._db.mark_deleted(attempt_id)

        await asyncio.gather(*(_one(s) for s in servers), return_exceptions=True)

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
