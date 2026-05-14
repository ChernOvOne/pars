"""Pydantic models for config.yaml + .env credential resolution."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class MissingCredentialError(RuntimeError):
    """An enabled hoster references an environment variable that is not set."""


def resolve_secret(env_var: str) -> SecretStr:
    """Read a credential from the environment, raising a clear error if absent."""
    val = os.environ.get(env_var)
    if not val:
        raise MissingCredentialError(f"environment variable {env_var!r} is not set")
    return SecretStr(val)


def _expand(p: str | Path) -> Path:
    return Path(p).expanduser()


class GeneralConfig(BaseModel):
    log_level: str = "INFO"
    db_path: Path = Path("~/.local/share/wlfinder/wlfinder.db")
    cache_dir: Path = Path("~/.cache/wlfinder")

    @field_validator("db_path", "cache_dir")
    @classmethod
    def _expanduser(cls, v: Path) -> Path:
        return _expand(v)


class WhitelistSourceConfig(BaseModel):
    type: Literal["github", "file"]
    name: str
    url: str | None = None
    path: Path | None = None

    @model_validator(mode="after")
    def _check_target(self) -> WhitelistSourceConfig:
        if self.type == "github" and not self.url:
            raise ValueError(f"whitelist source {self.name!r}: github type needs 'url'")
        if self.type == "file" and not self.path:
            raise ValueError(f"whitelist source {self.name!r}: file type needs 'path'")
        return self


class WhitelistConfig(BaseModel):
    sources: list[WhitelistSourceConfig]
    refresh_ttl_hours: int = 24


class OrchestratorConfig(BaseModel):
    max_attempts: int = 30
    delay_between_attempts_sec: int = 15
    parallel_workers: int = 1
    bail_on_balance_threshold_rub: float = 50.0


class HosterConfig(BaseModel):
    """Loose per-hoster config; each concrete hoster validates its own slice.

    Extra keys (``preset_id``, ``token_env``, ...) are preserved so that
    ``as_dict()`` can be handed to the hoster-specific Pydantic model.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    type: str
    enabled: bool = True

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump()


class TelegramConfig(BaseModel):
    """Telegram bot delivery for hit notifications."""

    enabled: bool = True
    bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    chat_id: str


class NotifyConfig(BaseModel):
    """Where to send 'IP is in the whitelist' notifications."""

    telegram: TelegramConfig | None = None


class Config(BaseModel):
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    whitelist: WhitelistConfig
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    hosters: list[HosterConfig] = Field(default_factory=list)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)

    @property
    def enabled_hosters(self) -> list[HosterConfig]:
        return [h for h in self.hosters if h.enabled]

    @classmethod
    def load(cls, path: str | Path) -> Config:
        path = Path(path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"config not found: {path}  (run `wlfinder init` first)")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)
