"""Deliver 'this IP is in the whitelist' notifications to the admin.

On a hit wlfinder does not provision anything on the box — it simply tells
the admin (over Telegram) which hoster handed out a whitelisted IP, and how
to reach the (still-running) server.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Protocol, runtime_checkable

import httpx
import structlog
from pydantic import BaseModel

from wlfinder.config import NotifyConfig, TelegramConfig, resolve_secret

log = structlog.get_logger(__name__)

_TELEGRAM_API = "https://api.telegram.org"


class HitNotification(BaseModel):
    """Everything the admin needs to know about a whitelisted server."""

    hoster: str
    ipv4: str
    region: str
    server_id: str
    ts: datetime
    ssh_command: str
    cost_per_hour_rub: float | None = None


def format_hit_message(n: HitNotification) -> str:
    """Render a Telegram HTML message body for a hit."""
    cost = (
        f"{n.cost_per_hour_rub:.2f} ₽/ч"
        if n.cost_per_hour_rub is not None
        else "неизвестно"
    )
    return (
        "🎯 <b>wlfinder: IP в белом списке</b>\n\n"
        f"<b>Хостер:</b>  {escape(n.hoster)}\n"
        f"<b>IP:</b>      <code>{escape(n.ipv4)}</code>\n"
        f"<b>Регион:</b>  {escape(n.region)}\n"
        f"<b>Server:</b>  <code>{escape(n.server_id)}</code>\n"
        f"<b>Время:</b>   {escape(n.ts.isoformat())}\n"
        f"<b>~Цена:</b>   {escape(cost)}\n\n"
        "<b>SSH-доступ:</b>\n"
        f"<pre>{escape(n.ssh_command)}</pre>\n\n"
        "<i>Сервер оставлен запущенным.</i>"
    )


@runtime_checkable
class Notifier(Protocol):
    """Anything that can deliver a hit notification."""

    async def notify_hit(self, notification: HitNotification) -> bool:
        """Deliver the notification. Returns True on success."""
        ...


class NullNotifier:
    """Fallback used when no notifier is configured — just logs."""

    async def notify_hit(self, notification: HitNotification) -> bool:
        log.warning(
            "notify.no_channel_configured",
            hoster=notification.hoster,
            ipv4=notification.ipv4,
        )
        return False


class TelegramNotifier:
    """Sends hit notifications through the Telegram Bot API."""

    def __init__(self, cfg: TelegramConfig, client: httpx.AsyncClient) -> None:
        self._chat_id = cfg.chat_id
        self._client = client
        self._token = resolve_secret(cfg.bot_token_env)

    async def notify_hit(self, notification: HitNotification) -> bool:
        url = f"{_TELEGRAM_API}/bot{self._token.get_secret_value()}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": format_hit_message(notification),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            resp = await self._client.post(url, json=payload)
        except httpx.HTTPError as exc:
            log.error("notify.telegram_failed", error=str(exc))
            return False
        if resp.status_code != 200 or not resp.json().get("ok", False):
            log.error(
                "notify.telegram_rejected",
                status=resp.status_code,
                body=resp.text[:300],
            )
            return False
        log.info("notify.telegram_sent", chat_id=self._chat_id, ipv4=notification.ipv4)
        return True

    async def send_test_message(self) -> bool:
        """Send a plain 'wlfinder is wired up' probe message."""
        url = f"{_TELEGRAM_API}/bot{self._token.get_secret_value()}/sendMessage"
        try:
            resp = await self._client.post(
                url,
                json={"chat_id": self._chat_id, "text": "✅ wlfinder: Telegram подключён"},
            )
        except httpx.HTTPError as exc:
            log.error("notify.telegram_failed", error=str(exc))
            return False
        return resp.status_code == 200 and bool(resp.json().get("ok", False))


def build_notifier(cfg: NotifyConfig, client: httpx.AsyncClient) -> Notifier:
    """Build the configured notifier, falling back to :class:`NullNotifier`."""
    if cfg.telegram is not None and cfg.telegram.enabled:
        return TelegramNotifier(cfg.telegram, client)
    return NullNotifier()
