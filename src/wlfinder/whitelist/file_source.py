"""Whitelist source backed by a local text file."""

from __future__ import annotations

from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


class FileSource:
    """Reads a whitelist from a local text file."""

    def __init__(self, name: str, path: Path) -> None:
        self.name = name
        self.path = Path(path).expanduser()

    async def fetch(self) -> list[str]:
        log.debug("whitelist.fetch", source=self.name, path=str(self.path))
        if not self.path.exists():
            raise FileNotFoundError(f"whitelist file not found: {self.path}")
        lines = self.path.read_text(encoding="utf-8").splitlines()
        log.info("whitelist.fetched", source=self.name, lines=len(lines))
        return lines
