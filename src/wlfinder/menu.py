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

# (key, label, action) — порядок = порядок на экране.
# Разделители "" в label — вставляют пустую строку перед пунктом.
_MENU: list[tuple[str, str, str]] = [
    # ─── Первичная настройка ───
    ("1", "📋  Создать config.yaml + .env из шаблонов", "init"),
    ("t", "📖  Инструкция: как получить токены/ключи API", "tokens"),
    # ─── Whitelist ───
    ("2", "🔄  Обновить базу whitelist (TWL)", "wl_update"),
    ("3", "📊  Статус базы whitelist", "wl_stats"),
    # ─── Хостеры ───
    ("4", "🩺  Проверить всех хостеров (доступ + баланс)", "ping"),
    ("s", "🔍  Скан Selectel: найти /24 в TWL и обновить config", "scan"),
    ("6", "📈  ASN-статистика — шансы попадания по хостерам", "asn"),
    # ─── Прогон ───
    ("7", "🧪  Пробный прогон (--dry-run, ничего не создаёт)", "dry"),
    ("8", "▶️   ЗАПУСТИТЬ поиск белого IP", "run"),
    ("m", "📺  Live-дашборд текущего прогона", "monitor"),
    ("9", "📚  Статистика прошлых прогонов", "stats"),
    # ─── Прочее ───
    ("5", "💬  Проверить Telegram-уведомления", "notify"),
    ("d", "⚠️   Снести ВСЕ wlfinder-серверы (паник-кнопка)", "destroy"),
    ("0", "🚪  Выход", "quit"),
]


def _resolve_menu_config(config: Path) -> Path:
    """Locate config.yaml for the menu: cwd -> ~/wlfinder -> ask the user."""
    if config.exists():
        return config
    fallback = Path.home() / "wlfinder" / "config.yaml"
    if fallback.exists():
        console.print(
            f"[dim]config.yaml нет в {Path.cwd()} — использую {fallback}[/dim]"
        )
        return fallback
    console.print(f"[yellow]config.yaml не найден[/yellow]  (текущая папка: {Path.cwd()})")
    answer = Prompt.ask(
        "Путь к config.yaml (Enter — создать новый в текущей папке)",
        default=str(config),
    )
    chosen = Path(answer).expanduser()
    if not chosen.exists():
        cli.do_init(chosen, force=False)
    return chosen


def _short_status(config: Path) -> str:
    """Короткая диагностика: что настроено, что нет — для приветствия."""
    lines: list[str] = []
    # .env
    env_path = config.with_name(".env")
    if env_path.exists():
        lines.append("[green]✓[/green] .env найден")
    else:
        lines.append("[yellow]•[/yellow] .env не создан (пункт 1)")
    # config → сколько хостеров включено
    try:
        cfg = cli._load_config(config)
        by_type: dict[str, int] = {}
        for h in cfg.enabled_hosters:
            by_type[h.type] = by_type.get(h.type, 0) + 1
        if by_type:
            summary = ", ".join(f"{v}× {k}" for k, v in by_type.items())
            lines.append(f"[green]✓[/green] хостеров включено: {summary}")
        else:
            lines.append("[yellow]•[/yellow] в config.yaml нет включённых хостеров")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"[red]✗[/red] config.yaml битый: {exc}")
    # whitelist cache
    try:
        import pickle
        cache_path = Path(cfg.general.cache_dir).expanduser() / "whitelist.pkl"
        if cache_path.exists():
            with cache_path.open("rb") as fh:
                cache = pickle.load(fh)
            n = getattr(cache, "networks", None)
            n_count = len(n) if n is not None else "?"
            lines.append(f"[green]✓[/green] whitelist в кэше: {n_count:,} сетей")
        else:
            lines.append("[yellow]•[/yellow] whitelist ещё не скачивался (пункт 2)")
    except Exception:  # noqa: BLE001
        lines.append("[yellow]•[/yellow] кэш whitelist пуст или устарел")
    return "\n".join(lines)


def run_menu(config: Path) -> None:
    """Основной цикл интерактивного меню. Выход — по пункту 0."""
    config = _resolve_menu_config(config)
    status = _short_status(config)
    console.print(
        Panel.fit(
            f"[bold]wlfinder[/bold]  ·  v{__version__}\n"
            "[dim]IP-рулетка по белым спискам мобильных операторов РФ[/dim]\n"
            f"[dim]конфиг: {config}[/dim]\n\n"
            f"{status}",
            border_style="cyan",
            title="🎯  Главное меню",
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
        console.print(
            "[dim]теперь отредактируйте config.yaml и .env "
            "(пункт меню «t» — как получить токены)[/dim]"
        )
        return

    if action == "tokens":
        cli.tokens()
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
    elif action == "scan":
        apply = Confirm.ask(
            "Записать найденные /24 в config.yaml? (--apply)",
            default=False,
        )
        asyncio.run(cli._scan(cfg, config, apply=apply, min_verified=1))
    elif action == "monitor":
        from wlfinder.monitor import run_monitor
        default_log = Path("run-loop.log")
        path_str = Prompt.ask(
            "Путь к лог-файлу прогона",
            default=str(default_log if default_log.exists() else "run-loop.log"),
        )
        run_monitor(Path(path_str).expanduser())
    elif action == "dry":
        asyncio.run(cli._run(cfg, [], None, dry_run=True))
    elif action == "stats":
        asyncio.run(cli._stats(cfg))
    elif action == "destroy":
        # _destroy_all lists the servers and then asks for an interactive y/n,
        # so passing yes=True here still keeps the confirmation step.
        asyncio.run(cli._destroy_all(cfg, yes=True))
    elif action == "run":
        console.print(
            "[yellow]Внимание:[/yellow] это создаёт реальные VPS у хостеров — "
            "каждая попытка тратит деньги."
        )
        if not Confirm.ask("Запустить поиск?", default=False):
            console.print("[dim]отменено[/dim]")
            return
        asyncio.run(cli._run(cfg, [], None, dry_run=False))
