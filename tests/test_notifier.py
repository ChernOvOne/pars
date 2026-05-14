"""Tests for the Telegram notifier."""

from datetime import UTC, datetime

import httpx
import pytest
import respx

from wlfinder.config import NotifyConfig, TelegramConfig
from wlfinder.notifier import (
    HitNotification,
    NullNotifier,
    TelegramNotifier,
    build_notifier,
    format_hit_message,
)

TG = "https://api.telegram.org"


def _notification() -> HitNotification:
    return HitNotification(
        hoster="timeweb-spb",
        ipv4="203.0.113.50",
        region="ru-1",
        server_id="777",
        ts=datetime(2026, 5, 14, 8, 30, tzinfo=UTC),
        ssh_command="ssh -i ~/.ssh/wlfinder root@203.0.113.50",
        cost_per_hour_rub=1.25,
    )


def test_format_hit_message_contains_all_fields() -> None:
    msg = format_hit_message(_notification())
    assert "timeweb-spb" in msg
    assert "203.0.113.50" in msg
    assert "ru-1" in msg
    assert "777" in msg
    assert "2026-05-14T08:30:00+00:00" in msg
    assert "1.25" in msg
    assert "ssh -i" in msg


def test_format_hit_message_handles_unknown_cost() -> None:
    n = _notification()
    n.cost_per_hour_rub = None
    assert "неизвестно" in format_hit_message(n)


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token-xyz")


def _tg_cfg() -> TelegramConfig:
    return TelegramConfig(enabled=True, bot_token_env="TELEGRAM_BOT_TOKEN", chat_id="123456")


@respx.mock
async def test_telegram_notifier_sends_hit() -> None:
    route = respx.post(f"{TG}/botbot-token-xyz/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    async with httpx.AsyncClient() as client:
        notifier = TelegramNotifier(_tg_cfg(), client)
        ok = await notifier.notify_hit(_notification())

    assert ok is True
    assert route.called
    body = route.calls.last.request.content
    assert b"123456" in body
    assert b"203.0.113.50" in body


@respx.mock
async def test_telegram_notifier_reports_rejection() -> None:
    respx.post(f"{TG}/botbot-token-xyz/sendMessage").mock(
        return_value=httpx.Response(400, json={"ok": False, "description": "bad chat"})
    )
    async with httpx.AsyncClient() as client:
        notifier = TelegramNotifier(_tg_cfg(), client)
        ok = await notifier.notify_hit(_notification())

    assert ok is False


async def test_null_notifier_returns_false() -> None:
    notifier = NullNotifier()
    assert await notifier.notify_hit(_notification()) is False


async def test_build_notifier_picks_telegram_or_null() -> None:
    async with httpx.AsyncClient() as client:
        with_tg = build_notifier(NotifyConfig(telegram=_tg_cfg()), client)
        assert isinstance(with_tg, TelegramNotifier)

        disabled = TelegramConfig(
            enabled=False, bot_token_env="TELEGRAM_BOT_TOKEN", chat_id="1"
        )
        assert isinstance(build_notifier(NotifyConfig(telegram=disabled), client), NullNotifier)
        assert isinstance(build_notifier(NotifyConfig(), client), NullNotifier)
