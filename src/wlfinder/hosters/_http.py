"""Shared HTTP plumbing for hoster API clients.

A single auth-aware request helper: retries 429/5xx and transport errors with
exponential backoff, and maps auth/billing/rate-limit failures onto the shared
hoster exceptions. Tokens live in headers and are never logged.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog

from wlfinder.hosters.base import (
    BalanceError,
    HosterAuthError,
    HosterError,
    RateLimitError,
)

log = structlog.get_logger(__name__)

DEFAULT_MAX_RETRIES = 4
_MAX_BACKOFF = 30.0


def _retry_after(resp: httpx.Response, fallback: float) -> float:
    raw = resp.headers.get("Retry-After")
    if raw:
        try:
            return min(float(raw), _MAX_BACKOFF)
        except ValueError:
            pass
    return min(fallback, _MAX_BACKOFF)


def _safe_body(resp: httpx.Response) -> str:
    text = resp.text
    return text[:300] if text else "<empty>"


async def request_with_retries(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json: Any = None,
    params: dict[str, Any] | None = None,
    ok: tuple[int, ...] = (200, 201, 204),
    max_retries: int = DEFAULT_MAX_RETRIES,
    label: str = "hoster",
) -> httpx.Response:
    """Issue an HTTP request, retrying transient failures with backoff.

    - 429 / 5xx and transport errors are retried with exponential backoff
    - 401/403 -> :class:`HosterAuthError`, 402 -> :class:`BalanceError`
    - 429 with retries exhausted -> :class:`RateLimitError`
    - any other unexpected status -> :class:`HosterError`

    *ok* lists the success codes; include 404 for idempotent deletes.
    """
    delay = 1.0
    for attempt in range(max_retries + 1):
        try:
            resp = await client.request(method, url, json=json, params=params, headers=headers)
        except httpx.TransportError as exc:
            if attempt >= max_retries:
                raise HosterError(f"{label}: transport error on {url}: {exc}") from exc
            log.warning("hoster.transport_retry", label=label, error=str(exc))
            await asyncio.sleep(delay)
            delay *= 2
            continue

        status = resp.status_code
        log.debug("hoster.request", label=label, method=method, url=url, status=status)

        # 403 from RU hosters is often soft rate-limiting under bursty load
        # (not a dead token), so retry it with backoff and only treat a
        # *persistent* 403 as fatal. A 401 is always an immediate auth failure.
        if status in (403, 429) or status >= 500:
            if attempt < max_retries:
                sleep_for = _retry_after(resp, delay)
                log.warning("hoster.retry", label=label, status=status, sleep=sleep_for)
                await asyncio.sleep(sleep_for)
                delay *= 2
                continue
            if status == 429:
                raise RateLimitError(f"{label}: rate limited on {url}")
            if status == 403:
                raise HosterAuthError(
                    f"{label}: forbidden (403) — token rejected or rate limited"
                )
            raise HosterError(f"{label}: server error {status} on {url}")

        if status == 401:
            raise HosterAuthError(f"{label}: token rejected (401)")
        if status == 402:
            raise BalanceError(f"{label}: insufficient balance (HTTP 402)")
        if status not in ok:
            raise HosterError(f"{label}: unexpected {status} on {url}: {_safe_body(resp)}")
        return resp

    raise HosterError(f"{label}: retries exhausted on {url}")  # pragma: no cover
