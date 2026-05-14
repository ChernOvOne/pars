"""Whitelist source backed by a raw-text URL (e.g. raw.githubusercontent.com)."""

from __future__ import annotations

import httpx
import structlog

log = structlog.get_logger(__name__)


class GitHubSource:
    """Fetches a whitelist from a raw-text URL."""

    def __init__(self, name: str, url: str, client: httpx.AsyncClient) -> None:
        self.name = name
        self.url = url
        self._client = client

    async def fetch(self) -> list[str]:
        log.debug("whitelist.fetch", source=self.name, url=self.url)
        resp = await self._client.get(self.url, follow_redirects=True)
        resp.raise_for_status()
        lines = resp.text.splitlines()
        log.info("whitelist.fetched", source=self.name, lines=len(lines))
        return lines
