"""Tests for the IP-roulette orchestrator (hosters + notifier faked)."""

import asyncio
from collections.abc import AsyncIterator, Sequence
from ipaddress import ip_network

import pytest

from wlfinder.checker import WhitelistChecker
from wlfinder.config import Config
from wlfinder.db import Database
from wlfinder.hosters.base import BalanceError, HosterAuthError, HosterError
from wlfinder.keeper import SshKeyPair
from wlfinder.models import CreatedServer
from wlfinder.notifier import HitNotification
from wlfinder.orchestrator import (
    _MAX_CONSECUTIVE_ERRORS,
    NoHitError,
    Orchestrator,
)


class FakeHoster:
    def __init__(
        self,
        name: str,
        ips: Sequence[str],
        balance: float = 1000.0,
        batch_size: int | None = 1,
    ) -> None:
        self.name = name
        self.batch_size = batch_size
        self._ips = list(ips)
        self._balance = balance
        self.created: list[str] = []
        self.deleted: list[str] = []
        self._n = 0

    async def create(self, *, name: str, ssh_pub_key: str, user_data: str | None) -> CreatedServer:
        await asyncio.sleep(0)  # a suspension point so parallel workers interleave
        ip = self._ips[self._n % len(self._ips)]
        self._n += 1
        sid = f"srv-{self._n}"
        self.created.append(sid)
        return CreatedServer(
            hoster=self.name, server_id=sid, public_ipv4=ip, region="ru-1", raw={}
        )

    async def promote(self, server: CreatedServer, ssh_pub_key: str) -> CreatedServer:
        return server

    async def delete(self, server_id: str) -> None:
        self.deleted.append(server_id)

    async def health_check(self) -> bool:
        return True

    async def get_balance(self) -> float | None:
        return self._balance

    async def estimate_cost_per_hour(self) -> float | None:
        return 0.05


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[HitNotification] = []

    async def notify_hit(self, notification: HitNotification) -> bool:
        self.sent.append(notification)
        return True


def _cfg(**orch: object) -> Config:
    return Config.model_validate(
        {
            "whitelist": {"sources": [{"type": "github", "name": "x", "url": "http://x"}]},
            "hosters": [],
            "orchestrator": {"delay_between_attempts_sec": 0, **orch},
        }
    )


def _checker(*cidrs: str) -> WhitelistChecker:
    return WhitelistChecker([ip_network(c) for c in cidrs])


@pytest.fixture
def ssh_key(tmp_path) -> SshKeyPair:
    return SshKeyPair(
        private_path=tmp_path / "wlfinder",
        public_path=tmp_path / "wlfinder.pub",
        public="ssh-ed25519 AAA test",
    )


@pytest.fixture
async def db(tmp_path) -> AsyncIterator[Database]:
    async with Database(tmp_path / "t.db") as database:
        yield database


async def test_hit_keeps_server_and_notifies(db: Database, ssh_key: SshKeyPair) -> None:
    hoster = FakeHoster("h1", ["192.168.0.5"])
    notifier = FakeNotifier()
    orch = Orchestrator(
        _cfg(max_attempts=5), db, _checker("192.168.0.0/24"), [hoster], notifier, ssh_key
    )

    result = await orch.run()

    assert result.hit is True
    assert result.attempts == 1
    assert result.notified is True
    assert hoster.deleted == []  # winner is kept, never deleted
    assert len(notifier.sent) == 1
    assert notifier.sent[0].ipv4 == "192.168.0.5"
    assert "192.168.0.5" in notifier.sent[0].ssh_command


async def test_miss_deletes_and_continues(db: Database, ssh_key: SshKeyPair) -> None:
    # first two miss, third lands in the whitelist
    hoster = FakeHoster("h1", ["8.8.8.8", "9.9.9.9", "192.168.0.10"])
    orch = Orchestrator(
        _cfg(max_attempts=5), db, _checker("192.168.0.0/24"), [hoster], FakeNotifier(), ssh_key
    )

    result = await orch.run()

    assert result.hit is True
    assert result.attempts == 3
    assert hoster.deleted == ["srv-1", "srv-2"]  # the two misses, not the hit


async def test_max_attempts_exhausted_raises(db: Database, ssh_key: SshKeyPair) -> None:
    hoster = FakeHoster("h1", ["8.8.8.8"])
    orch = Orchestrator(
        _cfg(max_attempts=3), db, _checker("192.168.0.0/24"), [hoster], FakeNotifier(), ssh_key
    )

    with pytest.raises(NoHitError):
        await orch.run()

    assert hoster.created == ["srv-1", "srv-2", "srv-3"]
    assert hoster.deleted == ["srv-1", "srv-2", "srv-3"]


async def test_release_failure_is_non_fatal(
    db: Database, ssh_key: SshKeyPair
) -> None:
    # A hoster whose delete() always fails must not crash the run — releases
    # are best-effort, the failure is logged and the run carries on to
    # exhaust its attempts.
    class ExplodingHoster(FakeHoster):
        async def delete(self, server_id: str) -> None:
            raise RuntimeError("boom")

    hoster = ExplodingHoster("h1", ["8.8.8.8"])
    orch = Orchestrator(
        _cfg(max_attempts=3), db, _checker("192.168.0.0/24"), [hoster], FakeNotifier(), ssh_key
    )

    with pytest.raises(NoHitError):
        await orch.run()

    assert hoster.created == ["srv-1", "srv-2", "srv-3"]  # all attempted, none kept


async def test_batch_hold_allocates_whole_batch_before_release(
    db: Database, ssh_key: SshKeyPair
) -> None:
    # With batch_size=4, all 4 IPs of a batch are created (and held) before
    # any are released — this is what forces the provider to hand out
    # distinct addresses.
    hoster = FakeHoster("h1", ["8.8.8.8"], batch_size=4)
    orch = Orchestrator(
        _cfg(max_attempts=4), db, _checker("192.168.0.0/24"), [hoster], FakeNotifier(), ssh_key
    )

    with pytest.raises(NoHitError):
        await orch.run()

    # one batch of 4: every IP created, then every IP released
    assert hoster.created == ["srv-1", "srv-2", "srv-3", "srv-4"]
    assert set(hoster.deleted) == set(hoster.created)
    assert await db.count_attempts() == 4


async def test_round_robin_across_hosters(db: Database, ssh_key: SshKeyPair) -> None:
    h1 = FakeHoster("h1", ["8.8.8.8"])  # always miss
    h2 = FakeHoster("h2", ["192.168.0.1"])  # always hit
    orch = Orchestrator(
        _cfg(max_attempts=4), db, _checker("192.168.0.0/24"), [h1, h2], FakeNotifier(), ssh_key
    )

    result = await orch.run()

    # attempt 0 -> h1 (miss), attempt 1 -> h2 (hit)
    assert result.hit is True
    assert result.attempts == 2
    assert h1.deleted == ["srv-1"]
    assert h2.deleted == []


async def test_balance_bail_before_create(db: Database, ssh_key: SshKeyPair) -> None:
    hoster = FakeHoster("h1", ["8.8.8.8"], balance=10.0)
    orch = Orchestrator(
        _cfg(max_attempts=3, bail_on_balance_threshold_rub=100.0),
        db,
        _checker("192.168.0.0/24"),
        [hoster],
        FakeNotifier(),
        ssh_key,
    )

    with pytest.raises(BalanceError):
        await orch.run()

    assert hoster.created == []  # bailed before spending anything


async def test_attempts_are_persisted(db: Database, ssh_key: SshKeyPair) -> None:
    hoster = FakeHoster("h1", ["8.8.8.8", "192.168.0.2"])
    orch = Orchestrator(
        _cfg(max_attempts=5), db, _checker("192.168.0.0/24"), [hoster], FakeNotifier(), ssh_key
    )

    await orch.run()

    assert await db.count_attempts() == 2
    rates = await db.hit_rate_by_hoster()
    assert rates == [{"hoster": "h1", "attempts": 2, "hits": 1, "hit_rate": 0.5}]


async def test_parallel_workers_single_winner(db: Database, ssh_key: SshKeyPair) -> None:
    # 3 workers, every IP hits — exactly one server is kept + notified, and
    # every other server that got created is deleted (no leaks).
    hoster = FakeHoster("h1", ["192.168.0.7"])
    notifier = FakeNotifier()
    orch = Orchestrator(
        _cfg(max_attempts=9, parallel_workers=3),
        db,
        _checker("192.168.0.0/24"),
        [hoster],
        notifier,
        ssh_key,
    )

    result = await orch.run()

    assert result.hit is True
    assert result.kept is not None
    assert len(notifier.sent) == 1  # exactly one winner, even with 3 workers racing
    kept_id = result.kept.server.server_id
    # no leaks: every created server other than the kept one was deleted
    assert set(hoster.created) - {kept_id} == set(hoster.deleted)


async def test_parallel_workers_exhausted_raises(db: Database, ssh_key: SshKeyPair) -> None:
    # 4 workers, nothing ever hits — all attempts spent, NoHitError, no leaks.
    hoster = FakeHoster("h1", ["8.8.8.8"])
    orch = Orchestrator(
        _cfg(max_attempts=8, parallel_workers=4),
        db,
        _checker("192.168.0.0/24"),
        [hoster],
        FakeNotifier(),
        ssh_key,
    )

    with pytest.raises(NoHitError):
        await orch.run()

    assert len(hoster.created) == 8
    assert set(hoster.deleted) == set(hoster.created)
    assert await db.count_attempts() == 8


async def test_transient_hoster_error_does_not_kill_run(
    db: Database, ssh_key: SshKeyPair
) -> None:
    # The first two attempts blow up with a transient HosterError; the run must
    # survive them, count them, and still reach the whitelist hit.
    class FlakyHoster(FakeHoster):
        async def create(
            self, *, name: str, ssh_pub_key: str, user_data: str | None
        ) -> CreatedServer:
            self._n += 1
            if self._n <= 2:
                raise HosterError("transient api hiccup")
            return CreatedServer(
                hoster=self.name,
                server_id=f"srv-{self._n}",
                public_ipv4="192.168.0.10",
                region="ru-1",
                raw={},
            )

    hoster = FlakyHoster("h1", ["unused"])
    orch = Orchestrator(
        _cfg(max_attempts=10), db, _checker("192.168.0.0/24"), [hoster], FakeNotifier(), ssh_key
    )

    result = await orch.run()

    assert result.hit is True
    assert result.error_count == 2


async def test_consecutive_errors_trip_circuit_breaker(
    db: Database, ssh_key: SshKeyPair
) -> None:
    # Every attempt errors — the run must abort via the circuit breaker rather
    # than spinning through all max_attempts.
    class BrokenHoster(FakeHoster):
        async def create(
            self, *, name: str, ssh_pub_key: str, user_data: str | None
        ) -> CreatedServer:
            self._n += 1
            raise HosterError("always broken")

    hoster = BrokenHoster("h1", ["unused"])
    orch = Orchestrator(
        _cfg(max_attempts=1000),
        db,
        _checker("192.168.0.0/24"),
        [hoster],
        FakeNotifier(),
        ssh_key,
    )

    with pytest.raises(HosterError, match="in a row"):
        await orch.run()

    # tripped the breaker well before max_attempts
    assert hoster._n <= _MAX_CONSECUTIVE_ERRORS + 2


async def test_auth_error_is_fatal(db: Database, ssh_key: SshKeyPair) -> None:
    # A broken token is not something to retry around — it stops the run.
    class AuthBrokenHoster(FakeHoster):
        async def create(
            self, *, name: str, ssh_pub_key: str, user_data: str | None
        ) -> CreatedServer:
            self._n += 1
            raise HosterAuthError("token rejected")

    hoster = AuthBrokenHoster("h1", ["unused"])
    orch = Orchestrator(
        _cfg(max_attempts=50), db, _checker("192.168.0.0/24"), [hoster], FakeNotifier(), ssh_key
    )

    with pytest.raises(HosterAuthError):
        await orch.run()

    assert hoster._n == 1  # stopped on the very first failure
