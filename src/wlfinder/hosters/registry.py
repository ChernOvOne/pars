"""Factory: build :class:`Hoster` instances from config by their ``type``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from wlfinder.config import Config, HosterConfig
from wlfinder.hosters.base import Hoster
from wlfinder.hosters.clo import CloHoster
from wlfinder.hosters.cloudru import CloudRuHoster
from wlfinder.hosters.onecloud import OneCloudHoster
from wlfinder.hosters.regru import RegruHoster
from wlfinder.hosters.selectel import SelectelHoster
from wlfinder.hosters.timeweb import TimewebHoster

HosterBuilder = Callable[[dict[str, Any], httpx.AsyncClient], Hoster]

_BUILDERS: dict[str, HosterBuilder] = {
    "timeweb": TimewebHoster.from_config,
    "regru": RegruHoster.from_config,
    "selectel": SelectelHoster.from_config,
    "cloudru": CloudRuHoster.from_config,
    "clo": CloHoster.from_config,
    "1cloud": OneCloudHoster.from_config,
}

# All hoster types from the spec are implemented; none are merely planned.
_PLANNED: set[str] = set()


class UnknownHosterError(RuntimeError):
    """config.yaml references a hoster type the registry cannot build."""


def build_hoster(cfg: HosterConfig, client: httpx.AsyncClient) -> Hoster:
    builder = _BUILDERS.get(cfg.type)
    if builder is None:
        if cfg.type in _PLANNED:
            raise UnknownHosterError(
                f"hoster {cfg.name!r}: type {cfg.type!r} is planned but not "
                f"implemented yet (Phase 3) — set 'enabled: false' for now"
            )
        raise UnknownHosterError(f"hoster {cfg.name!r}: unknown type {cfg.type!r}")
    return builder(cfg.as_dict(), client)


def build_enabled_hosters(cfg: Config, client: httpx.AsyncClient) -> list[Hoster]:
    return [build_hoster(h, client) for h in cfg.enabled_hosters]
