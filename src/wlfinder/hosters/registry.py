"""Factory: build :class:`Hoster` instances from config by their ``type``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from wlfinder.config import Config, HosterConfig
from wlfinder.hosters.base import Hoster
from wlfinder.hosters.timeweb import TimewebHoster

HosterBuilder = Callable[[dict[str, Any], httpx.AsyncClient], Hoster]

# Phase 1 ships Timeweb only; regru/selectel/cloudru/clo/1cloud arrive in Phase 3.
_BUILDERS: dict[str, HosterBuilder] = {
    "timeweb": TimewebHoster.from_config,
}

# Hoster types named in the spec that are not implemented yet.
_PLANNED = {"regru", "selectel", "cloudru", "clo", "1cloud"}


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
