"""Whitelist source for openlibrecommunity/twl ``subnets.c.json``."""

from __future__ import annotations

import httpx
import structlog

log = structlog.get_logger(__name__)


class TwlSubnetsSource:
    """Parses openlibrecommunity/twl ``subnets.c.json`` into CIDR lines.

    Each JSON entry looks like ``{cidr, count, total, percent, ips}`` where
    ``percent`` is the share of addresses in that /24 confirmed to respond
    from a SIM. Subnets below ``min_percent`` are dropped — raise the
    threshold to trade recall for precision.
    """

    def __init__(
        self,
        name: str,
        url: str,
        client: httpx.AsyncClient,
        min_percent: float = 0.0,
    ) -> None:
        self.name = name
        self.url = url
        self._client = client
        self._min_percent = min_percent

    async def fetch(self) -> list[str]:
        log.debug("whitelist.fetch", source=self.name, url=self.url)
        resp = await self._client.get(self.url, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()

        cidrs: list[str] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            cidr = entry.get("cidr")
            if not cidr:
                continue
            try:
                percent = float(entry.get("percent", 0.0))
            except (TypeError, ValueError):
                percent = 0.0
            if percent >= self._min_percent:
                cidrs.append(str(cidr))

        log.info(
            "whitelist.fetched",
            source=self.name,
            networks=len(cidrs),
            min_percent=self._min_percent,
        )
        return cidrs
