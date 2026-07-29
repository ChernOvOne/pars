"""wlfinder command-line interface."""

import asyncio
import logging
from importlib import resources
from ipaddress import IPv4Network
from pathlib import Path

import httpx
import structlog
import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from wlfinder import __version__
from wlfinder.asn import AsnOverlap, AsnStore, compute_overlap, resolve_asns
from wlfinder.config import Config
from wlfinder.db import Database
from wlfinder.hosters.base import Hoster, HosterError
from wlfinder.hosters.registry import build_hoster
from wlfinder.models import ServerInfo
from wlfinder.notifier import NullNotifier, TelegramNotifier, build_notifier
from wlfinder.orchestrator import NoHitError, Orchestrator
from wlfinder.whitelist.store import WhitelistStore

console = Console()

app = typer.Typer(
    name="pars",
    help="IP-roulette: find a Russian-hoster VPS whose IPv4 sits in the "
    "mobile-operator whitelist, then notify the admin over Telegram. "
    "Run `pars` with no arguments for an interactive menu.",
    add_completion=False,
)
whitelist_app = typer.Typer(help="Manage whitelist sources and cache.", no_args_is_help=True)
hoster_app = typer.Typer(help="Inspect configured hosters.", no_args_is_help=True)
notify_app = typer.Typer(help="Test the notification channel.", no_args_is_help=True)
app.add_typer(whitelist_app, name="whitelist")
app.add_typer(hoster_app, name="hoster")
app.add_typer(notify_app, name="notify")

_DEFAULT_CONFIG = Path("config.yaml")
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

ConfigOption = typer.Option(_DEFAULT_CONFIG, "--config", "-c", help="Path to config.yaml.")


def setup_logging(level: str = "INFO") -> None:
    lvl = getattr(logging, level.upper(), logging.INFO)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _load_config(config: Path) -> Config:
    # Load secrets from a .env next to config.yaml. Real environment variables
    # still win — load_dotenv does not override existing os.environ entries.
    load_dotenv(Path(config).expanduser().with_name(".env"))
    try:
        cfg = Config.load(config)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except Exception as exc:  # noqa: BLE001 - surface validation errors cleanly
        console.print(f"[red]config error:[/red] {exc}")
        raise typer.Exit(1) from exc
    setup_logging(cfg.general.log_level)
    return cfg


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"wlfinder {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """pars / wlfinder — IP-roulette for Russian mobile-operator whitelists.

    With no subcommand, launches an interactive menu.
    """
    if ctx.invoked_subcommand is None:
        from wlfinder.menu import run_menu

        run_menu(_DEFAULT_CONFIG)
        raise typer.Exit()


# --------------------------------------------------------------------------- init
def _bundled(name: str) -> str:
    """Read a file bundled inside the wlfinder package."""
    return resources.files("wlfinder").joinpath(name).read_text(encoding="utf-8")


def do_init(config: Path, *, force: bool) -> None:
    """Create config.yaml plus a .env template next to it.

    Each file is handled independently — an existing config.yaml does not
    stop the .env template from being written. Shared by the `init` command
    and the interactive menu.
    """
    if config.exists() and not force:
        console.print(
            f"[yellow]{config} already exists[/yellow] — left as-is "
            "(use --force to overwrite)"
        )
    else:
        try:
            config.write_text(_bundled("config.example.yaml"), encoding="utf-8")
        except (FileNotFoundError, ModuleNotFoundError) as exc:
            console.print("[red]bundled config.example.yaml is missing[/red]")
            raise typer.Exit(1) from exc
        console.print(f"[green]wrote {config.resolve()}[/green]")

    env_path = config.with_name(".env")
    if env_path.exists():
        console.print(f"[dim].env already exists ({env_path.resolve()}) — left as-is[/dim]")
    else:
        try:
            env_path.write_text(_bundled(".env.example"), encoding="utf-8")
            console.print(f"[green]wrote {env_path.resolve()}[/green] — put your tokens here")
        except (FileNotFoundError, ModuleNotFoundError):
            console.print("[yellow].env template missing — create .env manually[/yellow]")
    console.print(
        "[dim]next: fill .env with tokens and edit config.yaml — `pars tokens` for help[/dim]"
    )


@app.command()
def init(
    config: Path = ConfigOption,
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config.yaml."),
) -> None:
    """Create config.yaml from the bundled template."""
    do_init(config, force=force)


@app.command()
def tokens() -> None:
    """Show step-by-step instructions for obtaining API keys / tokens."""
    try:
        text = resources.files("wlfinder").joinpath("tokens.md").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        console.print(
            "[yellow]bundled guide not found[/yellow] — see docs/tokens.md at "
            "https://github.com/ChernOvOne/pars"
        )
        raise typer.Exit(1) from exc
    console.print(Markdown(text))


# ---------------------------------------------------------------------- whitelist
@whitelist_app.command("update")
def whitelist_update(config: Path = ConfigOption) -> None:
    """Force-refresh the whitelist cache from all configured sources."""
    cfg = _load_config(config)
    asyncio.run(_whitelist_update(cfg))


async def _whitelist_update(cfg: Config) -> None:
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        store = WhitelistStore(cfg.whitelist, cfg.general.cache_dir, client)
        cache = await store.refresh()
        async with Database(cfg.general.db_path) as db:
            for name, count in cache.source_counts.items():
                await db.upsert_whitelist_meta(
                    name, cache.fetched_at, count, cache.source_sha256.get(name, "")
                )
    _print_whitelist_table("whitelist updated", cache.source_counts, len(cache.networks))


@whitelist_app.command("stats")
def whitelist_stats(config: Path = ConfigOption) -> None:
    """Show the cached whitelist size and per-source breakdown."""
    _whitelist_stats(_load_config(config))


def _whitelist_stats(cfg: Config) -> None:
    store = WhitelistStore(cfg.whitelist, cfg.general.cache_dir)
    cache = store.load_cache()
    if cache is None:
        console.print("[yellow]no cache yet[/yellow] — run `pars whitelist update`")
        raise typer.Exit(1)
    _print_whitelist_table(
        f"whitelist cache (fetched {cache.fetched_at.isoformat()})",
        cache.source_counts,
        len(cache.networks),
    )


def _print_whitelist_table(title: str, counts: dict[str, int], collapsed: int) -> None:
    table = Table(title=title)
    table.add_column("source")
    table.add_column("networks", justify="right")
    for name, count in counts.items():
        table.add_row(name, str(count))
    table.add_row("[bold]collapsed total[/bold]", f"[bold]{collapsed}[/bold]")
    console.print(table)


# ------------------------------------------------------------------------- hoster
@hoster_app.command("ping")
def hoster_ping(config: Path = ConfigOption) -> None:
    """Health-check every enabled hoster (token + balance)."""
    cfg = _load_config(config)
    code = asyncio.run(_hoster_ping(cfg))
    if code:
        raise typer.Exit(code)


async def _hoster_ping(cfg: Config) -> int:
    table = Table(title="hoster ping")
    table.add_column("hoster")
    table.add_column("type")
    table.add_column("status")
    table.add_column("balance ₽", justify="right")
    exit_code = 0
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        for hcfg in cfg.enabled_hosters:
            try:
                hoster = build_hoster(hcfg, client)
                ok = await hoster.health_check()
                balance = await hoster.get_balance()
                table.add_row(
                    hcfg.name,
                    hcfg.type,
                    "[green]ok[/green]" if ok else "[red]fail[/red]",
                    f"{balance:.2f}" if balance is not None else "—",
                )
            except Exception as exc:  # noqa: BLE001 - report, don't crash
                exit_code = 1
                table.add_row(hcfg.name, hcfg.type, f"[red]{exc}[/red]", "—")
    console.print(table)
    return exit_code


# ------------------------------------------------------------------------- notify
@notify_app.command("test")
def notify_test(config: Path = ConfigOption) -> None:
    """Send a test message through the configured notification channel."""
    cfg = _load_config(config)
    asyncio.run(_notify_test(cfg))


async def _notify_test(cfg: Config) -> None:
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        notifier = build_notifier(cfg.notify, client)
        if isinstance(notifier, NullNotifier):
            console.print("[yellow]no notifier configured[/yellow] — add a `notify.telegram` block")
            raise typer.Exit(1)
        assert isinstance(notifier, TelegramNotifier)
        ok = await notifier.send_test_message()
    if ok:
        console.print("[green]Telegram: test message delivered[/green]")
    else:
        console.print("[red]Telegram: delivery failed[/red] — check token / chat_id")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------- run
@app.command()
def run(
    config: Path = ConfigOption,
    hoster: list[str] = typer.Option(
        None, "--hoster", help="Restrict to these hoster name(s)."
    ),
    max_attempts: int = typer.Option(
        None, "--max-attempts", help="Override config max_attempts."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Validate the pipeline without creating servers."
    ),
) -> None:
    """Run the IP-roulette: create servers until one IP is whitelisted."""
    cfg = _load_config(config)
    asyncio.run(_run(cfg, hoster or [], max_attempts, dry_run))


async def _run(cfg: Config, only: list[str], max_attempts: int | None, dry_run: bool) -> None:
    selected = [h for h in cfg.enabled_hosters if not only or h.name in only]
    if not selected:
        console.print("[red]no enabled hosters match[/red]")
        raise typer.Exit(1)

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        store = WhitelistStore(cfg.whitelist, cfg.general.cache_dir, client)
        checker = await store.get_checker()
        console.print(f"whitelist: [bold]{checker.network_count}[/bold] networks loaded")

        hosters = [build_hoster(h, client) for h in selected]
        notifier = build_notifier(cfg.notify, client)
        if isinstance(notifier, NullNotifier):
            console.print(
                "[yellow]warning:[/yellow] no notifier configured — hits will only be logged"
            )

        if dry_run:
            console.print("[cyan]--dry-run[/cyan]: checking hosters, not creating servers")
            for h in hosters:
                try:
                    ok = await h.health_check()
                    balance = await h.get_balance()
                    console.print(
                        f"  {h.name}: {'ok' if ok else 'FAIL'}  "
                        f"balance={balance if balance is not None else '—'}"
                    )
                except Exception as exc:  # noqa: BLE001
                    console.print(f"  {h.name}: [red]{exc}[/red]")
            console.print("[green]dry-run complete[/green] — pipeline looks wired up")
            return

        async with Database(cfg.general.db_path) as db:
            orch = Orchestrator(cfg, db, checker, hosters, notifier)
            try:
                result = await orch.run(max_attempts=max_attempts)
            except NoHitError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(2) from exc
            except HosterError as exc:
                console.print(f"[red]hoster error:[/red] {exc}")
                raise typer.Exit(1) from exc

    if result.kept is not None:
        srv = result.kept.server
        console.print()
        console.print(f"[bold green]HIT[/bold green] after {result.attempts} attempt(s)")
        console.print(f"  hoster:   {srv.hoster}")
        console.print(f"  IPv4:     [bold]{srv.public_ipv4}[/bold]")
        console.print(f"  region:   {srv.region}")
        console.print(f"  server:   {srv.server_id}")
        if result.cost_per_hour_rub is not None:
            console.print(f"  ~cost:    {result.cost_per_hour_rub:.2f} ₽/h")
        console.print(f"  SSH:      {result.kept.ssh_command}")
        console.print(
            "  Telegram: "
            + ("[green]notified[/green]" if result.notified else "[yellow]not sent[/yellow]")
        )
        console.print("[dim]server kept running — not deleted[/dim]")


# --------------------------------------------------------------------------- stats
@app.command()
def stats(config: Path = ConfigOption) -> None:
    """Show hit-rate per hoster from the SQLite history."""
    cfg = _load_config(config)
    asyncio.run(_stats(cfg))


async def _stats(cfg: Config) -> None:
    async with Database(cfg.general.db_path) as db:
        rows = await db.hit_rate_by_hoster()
        total = await db.count_attempts()
    if not rows:
        console.print("[yellow]no attempts recorded yet[/yellow]")
        return
    table = Table(title=f"hit-rate by hoster ({total} attempts total)")
    table.add_column("hoster")
    table.add_column("attempts", justify="right")
    table.add_column("hits", justify="right")
    table.add_column("hit-rate", justify="right")
    for r in rows:
        table.add_row(
            r["hoster"], str(r["attempts"]), str(r["hits"]), f"{r['hit_rate'] * 100:.1f}%"
        )
    console.print(table)


# ----------------------------------------------------------------------- asn-stats
@app.command("asn-stats")
def asn_stats(config: Path = ConfigOption) -> None:
    """Estimate hit probability per hoster: announced prefixes ∩ whitelist."""
    cfg = _load_config(config)
    asyncio.run(_asn_stats(cfg))


async def _asn_stats(cfg: Config) -> None:
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        store = WhitelistStore(cfg.whitelist, cfg.general.cache_dir, client)
        checker = await store.get_checker()
        asn_store = AsnStore(cfg.general.cache_dir, client)
        console.print(f"whitelist: [bold]{checker.network_count}[/bold] networks\n")
        if not cfg.enabled_hosters:
            console.print("[yellow]no enabled hosters[/yellow]")
            return
        for hcfg in cfg.enabled_hosters:
            asns = resolve_asns(hcfg.type, hcfg.as_dict())
            if not asns:
                console.print(
                    f"[yellow]{hcfg.name}[/yellow]: ASNs unknown for type "
                    f"{hcfg.type!r} — add an `asns:` list to its config\n"
                )
                continue
            prefixes: list[IPv4Network] = []
            for asn in asns:
                try:
                    prefixes.extend(await asn_store.fetch_prefixes(asn))
                except Exception as exc:  # noqa: BLE001 - report, keep going
                    console.print(f"  [red]AS{asn}: {exc}[/red]")
            _print_overlap(compute_overlap(hcfg.name, asns, prefixes, checker))


def _print_overlap(o: AsnOverlap) -> None:
    asn_label = ", ".join(f"AS{a}" for a in o.asns)
    console.print(f"[bold cyan]{o.hoster}[/bold cyan] ({asn_label})")
    console.print(
        f"  announced:    {o.announced_addresses:,} addr  "
        f"(~{o.announced_addresses // 256} ×/24)"
    )
    console.print(
        f"  in whitelist: {o.whitelisted_addresses:,} addr  ([bold]{o.percent:.3f}%[/bold])"
    )
    console.print(f"  matched:      {len(o.matched_prefixes)} / {o.total_prefixes} prefixes")
    if o.matched_prefixes:
        shown = ", ".join(str(p) for p in o.matched_prefixes[:8])
        extra = "" if len(o.matched_prefixes) <= 8 else f"  (+{len(o.matched_prefixes) - 8} more)"
        console.print(f"  e.g.: [dim]{shown}{extra}[/dim]")
    console.print()


# ------------------------------------------------------------------------ monitor
@app.command()
def monitor(
    logfile: Path = typer.Argument(
        Path("run-loop.log"), help="Путь к лог-файлу текущего прогона."
    ),
) -> None:
    """Live-дашборд текущего прогона (Rich TUI).

    Читает growing log-файл в реальном времени, парсит structlog-события,
    рисует сводку + таблицу по хостерам + шатал последних событий. При
    HIT — большая зелёная панель с IP и SSH-командой.

    Работает и с уже запущенным прогоном: если ``pars run > run.log &`` —
    ``pars monitor run.log`` подхватит его с текущего момента.
    """
    from wlfinder.monitor import run_monitor
    run_monitor(logfile)


# --------------------------------------------------------------------------- scan
@app.command()
def scan(
    config: Path = ConfigOption,
    apply: bool = typer.Option(
        False, "--apply", help="Записать найденные /24 в config.yaml как camp-хостеров."
    ),
    min_verified: int = typer.Option(
        1, "--min", help="Минимум TWL-verified IP на /24, чтобы попасть в отчёт."
    ),
) -> None:
    """Скан: пересекает external-подсети Selectel с TWL-whitelist.

    Автоматически:
      1. авторизуется в Selectel по SERVICE_* переменным из .env;
      2. тянет /floatingip_pools по всем регионам (272 подсети типично);
      3. подгружает свежий TWL;
      4. считает сколько TWL-verified IP попадает в каждую /24;
      5. печатает таблицу «регион / CIDR / TWL / статус в config»;
      6. по флагу --apply — обновляет config.yaml, добавляя недостающих
         selectel-camp хостеров и удаляя неактуальных.

    Никакого ручного curl'а — всё внутри процесса.
    """
    cfg = _load_config(config)
    asyncio.run(_scan(cfg, config, apply=apply, min_verified=min_verified))


async def _scan(cfg: Config, config_path: Path, *, apply: bool, min_verified: int) -> None:
    import ipaddress

    from wlfinder.hosters.selectel import SelectelConfig, SelectelHoster

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        # Свежий TWL
        console.print("[dim]загружаю whitelist (TWL)…[/dim]")
        store = WhitelistStore(cfg.whitelist, cfg.general.cache_dir, client)
        checker = await store.get_checker()
        console.print(f"[dim]  TWL: {checker.network_count:,} collapsed сетей[/dim]")

        # Кредо-донор: первый включённый selectel в config.yaml.
        selectel_cfgs = [h for h in cfg.enabled_hosters if h.type == "selectel"]
        if not selectel_cfgs:
            console.print(
                "[red]В config.yaml нет ни одного включённого selectel-хостера — "
                "нельзя определить, какими кредами логиниться.[/red]"
            )
            raise typer.Exit(1)
        creds_dict = selectel_cfgs[0].as_dict()

        # Обходим все регионы, тянем /floatingip_pools
        REGIONS = ["ru-1", "ru-3", "ru-7", "ru-8", "ru-9"]
        console.print(f"[dim]тяну /floatingip_pools по {len(REGIONS)} регионам…[/dim]")
        pools: list[tuple[str, ipaddress.IPv4Network, str]] = []
        for region in REGIONS:
            probe_cfg = SelectelConfig.model_validate(
                {**creds_dict, "name": f"scan-{region}", "region": region}
            )
            hoster = SelectelHoster(probe_cfg, client)
            try:
                region_pools = await hoster.list_floating_ip_pools()
            except HosterError as exc:
                console.print(f"  [yellow]{region}: {exc}[/yellow]")
                continue
            for p in region_pools:
                cidr = p.get("cidr")
                sid = p.get("subnet_id")
                if not cidr or not sid:
                    continue
                try:
                    pools.append((region, ipaddress.ip_network(cidr, strict=False), sid))
                except ValueError:
                    pass
            console.print(f"[dim]  {region}: {len(region_pools)} pools[/dim]")

        # count_overlap(net) даёт точное число whitelist-IP, попадающих в
        # данную /24 (учитывает и одиночные /32, и широкие CIDR-hits).
        matches: list[tuple[str, ipaddress.IPv4Network, str, int]] = []
        for region, net, sid in pools:
            count = checker.count_overlap(net)
            if count >= min_verified:
                matches.append((region, net, sid, count))

        matches.sort(key=lambda m: (-m[3], m[0], str(m[1])))
        console.print()

        # Какие уже в конфиге как camp?
        camp_cidrs = {
            str(h.as_dict().get("target_subnet_cidr"))
            for h in selectel_cfgs
            if h.as_dict().get("target_subnet_cidr")
        }

        table = Table(
            title=f"Selectel × TWL — {len(matches)} подсетей в whitelist",
            title_style="bold cyan",
        )
        table.add_column("регион", style="cyan")
        table.add_column("CIDR")
        table.add_column("TWL IP", justify="right", style="magenta")
        table.add_column("в config?", justify="center")
        table.add_column("рекомендация")

        for region, net, _sid, count in matches:
            in_config = str(net) in camp_cidrs
            if count >= 40:
                reco = "[green bold]плотная — приоритет[/green bold]"
            elif count >= 5:
                reco = "[green]камп-кандидат[/green]"
            elif count >= 1:
                reco = "[yellow]тонкий след[/yellow]"
            else:
                reco = "[dim]—[/dim]"
            table.add_row(
                region,
                str(net),
                str(count),
                "[green]✓[/green]" if in_config else "[dim]—[/dim]",
                reco,
            )
        console.print(table)

        # Что делать
        if not apply:
            missing = [m for m in matches if str(m[1]) not in camp_cidrs]
            stale = [c for c in camp_cidrs if c not in {str(m[1]) for m in matches}]
            console.print()
            if missing:
                console.print(
                    f"[yellow]{len(missing)} подсетей в TWL, но не в config.yaml.[/yellow]"
                )
            if stale:
                console.print(
                    f"[yellow]{len(stale)} camp-хостеров в config.yaml больше не в TWL "
                    f"(фантомные): {', '.join(sorted(stale))}[/yellow]"
                )
            if missing or stale:
                console.print("[dim]Запусти `pars scan --apply` чтобы обновить config.yaml.[/dim]")
            else:
                console.print("[green]config.yaml синхронизирован с TWL ✓[/green]")
            return

        # --apply: перезаписываем блок selectel-хостеров в config.yaml
        _apply_scan_to_config(config_path, creds_dict, matches)


def _apply_scan_to_config(
    config_path: Path,
    creds_dict: dict,
    matches: list,
) -> None:
    """Переписать selectel-хостеров в config.yaml согласно результатам scan.

    Сохраняет остальные разделы (whitelist, orchestrator, notify, non-selectel
    хостеров) без изменений — генерирует блок только для type: selectel.
    """
    import yaml

    with config_path.open() as f:
        raw = yaml.safe_load(f) or {}

    # Оставить не-selectel хостеров, добавить свежих selectel
    non_selectel = [h for h in raw.get("hosters", []) if h.get("type") != "selectel"]

    creds_keys = (
        "account_id_env",
        "service_user_env",
        "service_pass_env",
        "project_id_env",
    )
    creds_only = {k: creds_dict.get(k) for k in creds_keys if creds_dict.get(k)}

    new_selectel = []
    for region, net, _sid, count in matches:
        # Имя: "selectel-<region>-<первый.второй.третий>" без последнего октета
        octets = str(net.network_address).split(".")
        name = f"selectel-{region}-{octets[0]}.{octets[1]}.{octets[2]}"
        new_selectel.append(
            {
                "name": name,
                "type": "selectel",
                "enabled": True,
                **creds_only,
                "region": region,
                "target_subnet_cidr": str(net),
                "batch_size": 1,
            }
        )

    raw["hosters"] = non_selectel + new_selectel

    # backup
    backup = config_path.with_suffix(".yaml.bak")
    backup.write_text(config_path.read_text())
    with config_path.open("w") as f:
        yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False, indent=2)

    console.print(
        f"[green]✓ config.yaml обновлён:[/green] "
        f"{len(new_selectel)} selectel-camp хостеров, "
        f"{len(non_selectel)} других сохранено. "
        f"[dim](старая версия в {backup.name})[/dim]"
    )


# ------------------------------------------------------------------------- destroy
_WLFINDER_PREFIX = "wlfinder-"


@app.command()
def destroy(
    config: Path = ConfigOption,
    all_: bool = typer.Option(False, "--all", help="Destroy every wlfinder-* server."),
    yes: bool = typer.Option(False, "--yes", help="Required: confirm you really mean it."),
) -> None:
    """Panic button: tear down every wlfinder-* server across all hosters."""
    cfg = _load_config(config)
    if not all_:
        console.print("[yellow]destroy currently supports only --all[/yellow]")
        raise typer.Exit(1)
    asyncio.run(_destroy_all(cfg, yes))


async def _destroy_all(cfg: Config, yes: bool) -> None:
    found: list[ServerInfo] = []
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        by_name: dict[str, Hoster] = {}
        for hcfg in cfg.enabled_hosters:
            try:
                by_name[hcfg.name] = build_hoster(hcfg, client)
            except Exception as exc:  # noqa: BLE001 - report, skip this hoster
                console.print(f"[red]{hcfg.name}: {exc}[/red]")

        for name, hoster in by_name.items():
            try:
                servers = await hoster.list_servers()
            except Exception as exc:  # noqa: BLE001 - report, skip this hoster
                console.print(f"[red]{name}: list failed: {exc}[/red]")
                continue
            found.extend(s for s in servers if s.name.startswith(_WLFINDER_PREFIX))

        if not found:
            console.print("[green]no wlfinder-* servers found[/green]")
            return

        table = Table(title=f"{len(found)} wlfinder-* server(s)")
        table.add_column("hoster")
        table.add_column("server_id")
        table.add_column("name")
        table.add_column("ipv4")
        for s in found:
            table.add_row(s.hoster, s.server_id, s.name, s.public_ipv4 or "—")
        console.print(table)

        # Double confirmation (spec §14): the --yes flag *and* an interactive y/n.
        if not yes:
            console.print("[yellow]pass --yes to actually destroy these servers[/yellow]")
            raise typer.Exit(1)
        if not typer.confirm(f"Destroy all {len(found)} server(s)? This cannot be undone"):
            console.print("[dim]aborted[/dim]")
            return

        destroyed = 0
        for s in found:
            try:
                await by_name[s.hoster].delete(s.server_id)
                destroyed += 1
            except Exception as exc:  # noqa: BLE001 - report, keep destroying the rest
                console.print(f"[red]{s.hoster}/{s.server_id}: {exc}[/red]")
        console.print(f"[green]destroyed {destroyed}/{len(found)} server(s)[/green]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
