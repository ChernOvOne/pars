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
