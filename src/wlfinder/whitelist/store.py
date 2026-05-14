"""Load, normalise, merge and cache whitelist networks."""

from __future__ import annotations

import hashlib
import pickle
from dataclasses import dataclass, field
from datetime import UTC, datetime
from ipaddress import IPv4Network, IPv6Network, collapse_addresses, ip_network
from pathlib import Path

import httpx
import structlog

from wlfinder.checker import Network, WhitelistChecker
from wlfinder.config import WhitelistConfig, WhitelistSourceConfig
from wlfinder.whitelist.base import WhitelistSource
from wlfinder.whitelist.file_source import FileSource
from wlfinder.whitelist.github_source import GitHubSource
from wlfinder.whitelist.twl_source import TwlSubnetsSource

log = structlog.get_logger(__name__)

_CACHE_VERSION = 1


def parse_lines(lines: list[str]) -> list[Network]:
    """Normalise raw whitelist lines into ``ip_network`` objects.

    - strips ``#`` comments and surrounding whitespace
    - skips blank lines
    - a bare IP becomes a /32 (or /128) host network
    - silently drops lines that do not parse as an IP/CIDR
    """
    nets: list[Network] = []
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        token = line.split()[0]
        try:
            nets.append(ip_network(token, strict=False))
        except ValueError:
            log.debug("whitelist.skip_unparseable", line=raw)
            continue
    return nets


def collapse(nets: list[Network]) -> list[Network]:
    """Merge + sort networks, de-duplicating overlaps. v4 and v6 handled apart."""
    v4 = sorted(n for n in nets if isinstance(n, IPv4Network))
    v6 = sorted(n for n in nets if isinstance(n, IPv6Network))
    out: list[Network] = []
    if v4:
        out.extend(collapse_addresses(v4))
    if v6:
        out.extend(collapse_addresses(v6))
    return out


@dataclass
class WhitelistCache:
    version: int
    fetched_at: datetime
    networks: list[Network]
    source_counts: dict[str, int] = field(default_factory=dict)
    source_sha256: dict[str, str] = field(default_factory=dict)


class WhitelistStore:
    """Owns the on-disk pickle cache and builds a :class:`WhitelistChecker`."""

    def __init__(
        self,
        cfg: WhitelistConfig,
        cache_dir: Path,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._cfg = cfg
        self._cache_dir = Path(cache_dir).expanduser()
        self._client = client

    @property
    def cache_path(self) -> Path:
        return self._cache_dir / "whitelist.pkl"

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("WhitelistStore needs an httpx client to refresh sources")
        return self._client

    async def get_checker(self, *, force: bool = False) -> WhitelistChecker:
        """Return a checker, served from cache when fresh, refreshed otherwise."""
        cache = self.load_cache()
        if cache is not None and not force and self._is_fresh(cache):
            log.info(
                "whitelist.cache_hit",
                networks=len(cache.networks),
                fetched_at=cache.fetched_at.isoformat(),
            )
            return WhitelistChecker(cache.networks)
        cache = await self.refresh()
        return WhitelistChecker(cache.networks)

    async def refresh(self) -> WhitelistCache:
        """Fetch every source, normalise, collapse, and persist the cache."""
        sources = _build_sources(self._cfg.sources, self._require_client())
        all_nets: list[Network] = []
        counts: dict[str, int] = {}
        shas: dict[str, str] = {}
        for src in sources:
            lines = await src.fetch()
            nets = parse_lines(lines)
            all_nets.extend(nets)
            counts[src.name] = len(nets)
            shas[src.name] = hashlib.sha256("\n".join(lines).encode()).hexdigest()

        collapsed = collapse(all_nets)
        cache = WhitelistCache(
            version=_CACHE_VERSION,
            fetched_at=datetime.now(UTC),
            networks=collapsed,
            source_counts=counts,
            source_sha256=shas,
        )
        self._save_cache(cache)
        log.info(
            "whitelist.refreshed",
            networks=len(collapsed),
            sources=len(sources),
        )
        return cache

    def load_cache(self) -> WhitelistCache | None:
        path = self.cache_path
        if not path.exists():
            return None
        try:
            with path.open("rb") as fh:
                cache = pickle.load(fh)
        except (pickle.UnpicklingError, EOFError, AttributeError, ValueError) as exc:
            log.warning("whitelist.cache_unreadable", error=str(exc))
            return None
        if not isinstance(cache, WhitelistCache) or cache.version != _CACHE_VERSION:
            log.warning("whitelist.cache_stale_format")
            return None
        return cache

    def _save_cache(self, cache: WhitelistCache) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".pkl.tmp")
        with tmp.open("wb") as fh:
            pickle.dump(cache, fh)
        tmp.replace(self.cache_path)

    def _is_fresh(self, cache: WhitelistCache) -> bool:
        age_hours = (datetime.now(UTC) - cache.fetched_at).total_seconds() / 3600
        return age_hours < self._cfg.refresh_ttl_hours


def _build_sources(
    sources: list[WhitelistSourceConfig],
    client: httpx.AsyncClient,
) -> list[WhitelistSource]:
    built: list[WhitelistSource] = []
    for s in sources:
        if s.type == "github":
            assert s.url is not None  # guaranteed by WhitelistSourceConfig validator
            built.append(GitHubSource(s.name, s.url, client))
        elif s.type == "twl_subnets":
            assert s.url is not None
            built.append(TwlSubnetsSource(s.name, s.url, client, s.min_percent))
        elif s.type == "file":
            assert s.path is not None
            built.append(FileSource(s.name, s.path))
        else:  # pragma: no cover - guarded by the Literal type
            raise ValueError(f"unknown whitelist source type: {s.type}")
    return built
