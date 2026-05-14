"""Interactive `pars` menu — a friendly front-end over the wlfinder commands.

Running ``pars`` with no arguments lands here. Every menu item reuses the
exact same code paths as the corresponding ``pars <command>`` subcommand.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from wlfinder import __version__, cli

console = Console()

# (key, label, action) — order is the on-screen order.
_MENU: list[tuple[str, str, str]] = [
    ("1", "Настройка — создать config.yaml из шаблона", "init"),
    ("2", "Обновить базу whitelist", "wl_update"),
    ("3", "Показать статус базы whitelist", "wl_stats"),
    ("4", "Проверить хостеров (токены, баланс)", "ping"),
    ("5", "Проверить Telegram-уведомления", "notify"),
    ("6", "ASN-статистика — шансы попадания по хостерам", "asn"),
    ("7", "Пробный прогон (--dry-run, без создания серверов)", "dry"),
    ("8", "▶  ЗАПУСТИТЬ поиск IP", "run"),
    ("9", "Статистика прошлых запусков", "stats"),
    ("0", "Выход", "quit"),
]


def run_menu(config: Path) -> None:
    """Show the interactive menu loop until the user picks Exit."""
    console.print(
        Panel.fit(
            f"[bold]wlfinder[/bold]  ·  v{__version__}\n"
            "IP-рулетка по белым спискам мобильных операторов РФ\n"
            f"[dim]конфиг: {config}[/dim]",
            border_style="cyan",
        )
    )
    while True:
        console.print()
        for key, label, _ in _MENU:
            console.print(f"  [bold cyan]{key}[/bold cyan]  {label}")
        choice = Prompt.ask(
            "\nВыберите пункт",
            choices=[k for k, _, _ in _MENU],
            default="8",
            show_choices=False,
        )
        action = next(act for k, _, act in _MENU if k == choice)
        if action == "quit":
            console.print("[dim]До встречи.[/dim]")
            return

        console.print()
        try:
            _dispatch(action, config)
        except typer.Exit as exc:
            if exc.exit_code:
                console.print(f"[yellow](завершено с кодом {exc.exit_code})[/yellow]")
        except KeyboardInterrupt:
            console.print("\n[yellow]прервано[/yellow]")
        except Exception as exc:  # noqa: BLE001 - the menu must survive any failure
            console.print(f"[red]ошибка:[/red] {exc}")

        Prompt.ask("\n[dim]Enter — вернуться в меню[/dim]", default="", show_default=False)


def _dispatch(action: str, config: Path) -> None:
    if action == "init":
        force = False
        if config.exists():
            force = Confirm.ask(f"{config} уже существует — перезаписать?", default=False)
            if not force:
                console.print("[dim]оставлено без изменений[/dim]")
                return
        cli.do_init(config, force=force)
        console.print("[dim]теперь отредактируйте config.yaml и .env (см. docs/tokens.md)[/dim]")
        return

    # everything else needs a valid config
    cfg = cli._load_config(config)

    if action == "wl_update":
        asyncio.run(cli._whitelist_update(cfg))
    elif action == "wl_stats":
        cli._whitelist_stats(cfg)
    elif action == "ping":
        asyncio.run(cli._hoster_ping(cfg))
    elif action == "notify":
        asyncio.run(cli._notify_test(cfg))
    elif action == "asn":
        asyncio.run(cli._asn_stats(cfg))
    elif action == "dry":
        asyncio.run(cli._run(cfg, [], None, dry_run=True))
    elif action == "stats":
        asyncio.run(cli._stats(cfg))
    elif action == "run":
        console.print(
            "[yellow]Внимание:[/yellow] это создаёт реальные VPS у хостеров — "
            "каждая попытка тратит деньги."
        )
        if not Confirm.ask("Запустить поиск?", default=False):
            console.print("[dim]отменено[/dim]")
            return
        asyncio.run(cli._run(cfg, [], None, dry_run=False))
