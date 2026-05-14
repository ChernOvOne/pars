"""Shared data-transfer objects used across wlfinder."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    """Timezone-aware UTC now — everything internal is UTC (spec §15.9)."""
    return datetime.now(UTC)


class CreatedServer(BaseModel):
    """A VPS that a hoster has just created for us."""

    hoster: str
    server_id: str
    public_ipv4: str
    public_ipv6: str | None = None
    region: str
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)


class Attempt(BaseModel):
    """One IP-roulette attempt, persisted to the ``attempts`` table."""

    id: int | None = None
    ts: datetime = Field(default_factory=utcnow)
    hoster: str
    region: str | None = None
    server_id: str
    ipv4: str
    ipv6: str | None = None
    hit: bool
    deleted: bool = False
    cost_estimate_rub: float | None = None
    raw_create: dict[str, Any] | None = None
    notes: str | None = None


class SuccessfulDeployment(BaseModel):
    """A kept server with a provisioned proxy (``successful_deployments`` table)."""

    id: int | None = None
    ts: datetime = Field(default_factory=utcnow)
    hoster: str
    server_id: str
    ipv4: str
    proxy_type: str
    client_config_path: str | None = None
