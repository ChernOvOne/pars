"""Protocol for a whitelist source."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class WhitelistSource(Protocol):
    """Anything that can yield raw CIDR/IP lines.

    Implementations return the text *as-is* (comments, blank lines, bare IPs);
    normalisation happens centrally in ``whitelist.store.parse_lines``.
    """

    name: str

    async def fetch(self) -> list[str]: ...
