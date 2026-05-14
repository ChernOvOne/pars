"""Hoster protocol + shared hoster errors."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from wlfinder.models import CreatedServer

__all__ = [
    "BalanceError",
    "CreatedServer",
    "Hoster",
    "HosterAuthError",
    "HosterError",
    "RateLimitError",
]


class HosterError(RuntimeError):
    """Generic, non-recoverable error talking to a hoster API."""


class HosterAuthError(HosterError):
    """Token rejected (HTTP 401/403)."""


class BalanceError(HosterError):
    """Account is out of money (HTTP 402, or balance below the bail threshold)."""


class RateLimitError(HosterError):
    """Rate limited (HTTP 429) and the internal retries were exhausted."""


@runtime_checkable
class Hoster(Protocol):
    """The contract every VPS provider integration must satisfy.

    Concrete implementations additionally expose a
    ``classmethod from_config(raw: dict, client: httpx.AsyncClient)`` used by
    :mod:`wlfinder.hosters.registry`.
    """

    name: str

    async def create(
        self,
        *,
        name: str,
        ssh_pub_key: str,
        user_data: str | None,
    ) -> CreatedServer: ...

    async def delete(self, server_id: str) -> None: ...

    async def health_check(self) -> bool:
        """Ping the API and validate the token. Raises on auth failure."""
        ...

    async def get_balance(self) -> float | None:
        """Account balance in RUB, or None if the hoster cannot report it."""
        ...

    async def estimate_cost_per_hour(self) -> float | None:
        """Best-effort hourly price in RUB, or None if unknown."""
        ...
