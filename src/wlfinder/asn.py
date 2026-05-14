"""Fetch hoster BGP prefixes (ipverse/asn-ip) and intersect with the whitelist.

This powers ``pars asn-stats``: before spending money, estimate the hit
probability for each hoster as ``whitelisted_addresses / announced_addresses``
over the prefixes its ASNs announce.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from ipaddress import IPv4Network, collapse_addresses, ip_network
from pathlib import Path
from typing import Any

import httpx
import structlog

from wlfinder.checker import WhitelistChecker

log = structlog.get_logger(__name__)

_IPVERSE_URL = (
    "https://raw.githubusercontent.com/ipverse/asn-ip/master/as/{asn}/ipv4-aggregated.txt"
)
_CACHE_TTL_SECONDS = 7 * 24 * 3600  # BGP announcements change slowly.

# Default hoster-type -> ASNs (spec §10; verify periodically, these can change).
DEFAULT_ASNS: dict[str, list[int]] = {
    "timeweb": [9123, 197695],
    "regru": [197695, 47593],
    "selectel": [49505, 50340],
    "cloudru": [199524, 209156],
}


def resolve_asns(hoster_type: str, raw_cfg: dict[str, Any]) -> list[int]:
    """ASNs for a hoster: an explicit ``asns:`` in config wins over the default."""
    override = raw_cfg.get("asns")
    if override:
        return [int(a) for a in override]
    return list(DEFAULT_ASNS.get(hoster_type, []))


def parse_asn_prefixes(text: str) -> list[IPv4Network]:
    """Parse ipverse ``ipv4-aggregated.txt`` (``#`` comments + CIDR per line)."""
    nets: list[IPv4Network] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            net = ip_network(line, strict=False)
        except ValueError:
            continue
        if isinstance(net, IPv4Network):
            nets.append(net)
    return nets


@dataclass
class AsnOverlap:
    """Whitelist overlap for one hoster across all of its ASNs."""

    hoster: str
    asns: list[int]
    announced_addresses: int
    whitelisted_addresses: int
    matched_prefixes: list[IPv4Network]
    total_prefixes: int

    @property
    def percent(self) -> float:
        if not self.announced_addresses:
            return 0.0
        return self.whitelisted_addresses / self.announced_addresses * 100


def compute_overlap(
    hoster: str,
    asns: list[int],
    prefixes: list[IPv4Network],
    checker: WhitelistChecker,
) -> AsnOverlap:
    """Collapse *prefixes* (ASNs may share/overlap) and measure whitelist overlap."""
    collapsed = list(collapse_addresses(prefixes)) if prefixes else []
    announced = 0
    whitelisted = 0
    matched: list[IPv4Network] = []
    for net in collapsed:
        announced += net.num_addresses
        overlap = checker.count_overlap(net)
        whitelisted += overlap
        if overlap > 0:
            matched.append(net)
    return AsnOverlap(
        hoster=hoster,
        asns=asns,
        announced_addresses=announced,
        whitelisted_addresses=whitelisted,
        matched_prefixes=matched,
        total_prefixes=len(collapsed),
    )


class AsnStore:
    """Fetches per-ASN prefix lists from ipverse/asn-ip, cached on disk."""

    def __init__(self, cache_dir: Path, client: httpx.AsyncClient) -> None:
        self._cache_dir = Path(cache_dir).expanduser() / "asn"
        self._client = client

    async def fetch_prefixes(self, asn: int) -> list[IPv4Network]:
        return parse_asn_prefixes(await self._fetch_text(asn))

    async def _fetch_text(self, asn: int) -> str:
        cached = self._cache_dir / f"{asn}.txt"
        if cached.exists() and (time.time() - cached.stat().st_mtime) < _CACHE_TTL_SECONDS:
            return cached.read_text(encoding="utf-8")
        url = _IPVERSE_URL.format(asn=asn)
        log.debug("asn.fetch", asn=asn, url=url)
        resp = await self._client.get(url, follow_redirects=True)
        resp.raise_for_status()
        text = resp.text
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        cached.write_text(text, encoding="utf-8")
        log.info("asn.fetched", asn=asn, bytes=len(text))
        return text
