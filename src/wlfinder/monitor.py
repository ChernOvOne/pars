"""Live-дашборд текущего прогона wlfinder.

Читает лог-файл ``pars run``, парсит structlog-события и рисует Rich Live
TUI: сводка + таблица по хостерам + шатал последних событий. При HIT
всплывает большая зелёная панель с IP и SSH-командой.

Использование::

    pars monitor run.log              # мониторить существующий/растущий файл
    pars run > run.log 2>&1 & \\
      pars monitor run.log            # запустил прогон в фоне, потом смотрим

Формат парсера — стандартный structlog console-рендер, который уже
использует wlfinder (ANSI-цветной ``key=value``). Мы игнорируем цвета и
цепляемся за ключи ``batch=``, ``hoster=``, ``ipv4=``, ``soft=`` и т.д.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()

# Удаляет ANSI escape-коды из строки лога (structlog console rendering).
_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
# key=value в structlog (value может быть в кавычках или без)
_KV = re.compile(r"(\w+)=('([^']*)'|\"([^\"]*)\"|(\S+))")
# первая колонка — ISO-8601 таймстамп
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)")
# имя события — самое первое НЕ-ключ-значение слово после уровня
_EVENT = re.compile(r"\](?:\s*\[\S+\])?\s*(\w[\w\.]+)")


def _parse_event(raw: str) -> dict[str, str] | None:
    """Разобрать одну строку structlog: вернуть {event, ts, ...kv} или None."""
    line = _ANSI.sub("", raw).strip()
    if not line:
        return None
    ts_m = _TS.match(line)
    ev_m = _EVENT.search(line)
    if not ev_m:
        return None
    out: dict[str, str] = {"event": ev_m.group(1)}
    if ts_m:
        out["ts"] = ts_m.group(1)
    for m in _KV.finditer(line):
        key = m.group(1)
        val = m.group(3) or m.group(4) or m.group(5)
        # уровень тоже парсится как ключ ("info"="") — отбрасываем пустые
        if val is not None:
            out[key] = val
    return out


class RunState:
    """Накопленная статистика прогона."""

    def __init__(self) -> None:
        self.started_at: float | None = None
        self.max_attempts: int = 0
        self.parallel_workers: int = 0
        self.hosters: list[str] = []
        # per-hoster счётчики
        self.batches: dict[str, int] = defaultdict(int)
        self.soft_miss: dict[str, int] = defaultdict(int)
        self.hard_err: dict[str, int] = defaultdict(int)
        self.created: dict[str, int] = defaultdict(int)
        self.miss_ip: dict[str, int] = defaultdict(int)
        self.last_ip: dict[str, str] = {}
        self.last_msg: dict[str, str] = {}
        # общие
        self.total_batches = 0
        self.total_soft = 0
        self.total_hard = 0
        self.total_miss_ip = 0
        self.hits: list[dict[str, str]] = []
        self.recent: deque[str] = deque(maxlen=8)
        self.finished = False
        self.finish_reason: str = ""

    def ingest(self, ev: dict[str, str]) -> None:
        name = ev.get("event", "")
        hoster = ev.get("hoster", "")

        if name == "orchestrator.start":
            self.started_at = self.started_at or time.time()
            self.max_attempts = int(ev.get("max_attempts", "0") or 0)
            self.parallel_workers = int(ev.get("parallel_workers", "0") or 0)
            hlist = ev.get("hosters", "")
            self.hosters = [
                h.strip(" '\"[]") for h in hlist.split(",") if h.strip(" '\"[]")
            ]
            self.recent.append(
                f"[cyan]старт:[/cyan] {len(self.hosters)} хостеров, "
                f"workers={self.parallel_workers}, max={self.max_attempts}"
            )
            return

        if name == "orchestrator.batch":
            self.total_batches += 1
            if hoster:
                self.batches[hoster] += 1
                created = int(ev.get("created", "0") or 0)
                self.created[hoster] += created
            return

        if name == "orchestrator.batch_error":
            is_soft = ev.get("soft", "False") == "True"
            if is_soft:
                self.total_soft += 1
                if hoster:
                    self.soft_miss[hoster] += 1
                    self.last_msg[hoster] = "pool_exhausted"
            else:
                self.total_hard += 1
                if hoster:
                    self.hard_err[hoster] += 1
                    err = ev.get("error", "?")
                    self.last_msg[hoster] = err[:60]
                    self.recent.append(f"[red]{hoster}:[/red] {err[:80]}")
            return

        if name == "orchestrator.miss":
            self.total_miss_ip += 1
            if hoster:
                self.miss_ip[hoster] += 1
                ip = ev.get("ipv4", "")
                if ip:
                    self.last_ip[hoster] = ip
                    self.recent.append(f"[yellow]{hoster}:[/yellow] miss {ip}")
            return

        if name == "orchestrator.hit":
            hit = {
                "hoster": hoster,
                "ipv4": ev.get("ipv4", "?"),
                "server_id": ev.get("server_id", ev.get("batch", "?")),
                "ts": ev.get("ts", ""),
            }
            self.hits.append(hit)
            self.recent.append(
                f"[bold green]🎯 HIT[/bold green] {hit['ipv4']} ({hoster})"
            )
            return

        if name.startswith("orchestrator.aborting") or "aborting run" in raw_msg(ev):
            self.finished = True
            self.finish_reason = "circuit breaker"
            self.recent.append("[red]прогон остановлен: circuit breaker[/red]")


def raw_msg(ev: dict[str, str]) -> str:
    return " ".join(f"{k}={v}" for k, v in ev.items())


def _summary_panel(state: RunState) -> Panel:
    elapsed = int(time.time() - state.started_at) if state.started_at else 0
    mm, ss = divmod(elapsed, 60)
    hh, mm = divmod(mm, 60)
    elapsed_str = f"{hh:02d}:{mm:02d}:{ss:02d}"

    hit_line = ""
    if state.hits:
        h = state.hits[0]
        hit_line = (
            f"\n[bold green]🎯 HIT![/bold green]  [bold]{h['ipv4']}[/bold]  "
            f"(хостер: {h['hoster']})"
        )

    body = Text.assemble(
        ("Прогон wlfinder\n", "bold cyan"),
        (f"Время:       {elapsed_str}\n", ""),
        (f"Хостеров:    {len(state.hosters)}  ", ""),
        (f"workers: {state.parallel_workers}  ", "dim"),
        (f"max: {state.max_attempts}\n\n", "dim"),
        (f"Всего попыток: {state.total_batches}\n", ""),
        (f"  созданo IP:      {sum(state.created.values())}\n", "green"),
        (f"  miss (IP не в whitelist):  {state.total_miss_ip}\n", "yellow"),
        (f"  pool_exhausted (soft):     {state.total_soft}\n", "dim"),
        (f"  hard-errors:               {state.total_hard}\n", "red" if state.total_hard else "dim"),
    )
    if hit_line:
        body.append(hit_line)
    if state.finished:
        body.append(f"\n[bold]Прогон завершён:[/bold] {state.finish_reason}", style="magenta")
    return Panel(body, title="Сводка", border_style="cyan")


def _hosters_table(state: RunState) -> Table:
    t = Table(title="По хостерам", title_style="bold cyan", expand=True)
    t.add_column("хостер", style="cyan", no_wrap=True)
    t.add_column("batches", justify="right")
    t.add_column("created", justify="right", style="green")
    t.add_column("miss IP", justify="right", style="yellow")
    t.add_column("pool_exh", justify="right", style="dim")
    t.add_column("hard", justify="right", style="red")
    t.add_column("последний IP / ошибка", style="dim")
    all_names = set(state.hosters) | set(state.batches)
    for name in sorted(all_names):
        last = state.last_ip.get(name) or state.last_msg.get(name, "—")
        t.add_row(
            name,
            str(state.batches.get(name, 0)),
            str(state.created.get(name, 0)),
            str(state.miss_ip.get(name, 0)),
            str(state.soft_miss.get(name, 0)),
            str(state.hard_err.get(name, 0)),
            last,
        )
    return t


def _recent_panel(state: RunState) -> Panel:
    if not state.recent:
        body = Text("(пока пусто)", style="dim")
    else:
        body = Text("\n").join(Text.from_markup(x) for x in state.recent)
    return Panel(body, title="Последние события", border_style="dim")


def _hit_panel(state: RunState) -> Panel:
    h = state.hits[0]
    body = Text.assemble(
        ("🎯  ПОПАЛИ В WHITELIST!\n\n", "bold green"),
        ("IP:       ", "dim"),
        (f"{h['ipv4']}\n", "bold"),
        ("Хостер:   ", "dim"),
        (f"{h['hoster']}\n", "bold"),
        ("Server ID:", "dim"),
        (f" {h['server_id']}\n\n", ""),
        ("SSH: ", "dim"),
        (f"ssh -i /root/.ssh/wlfinder root@{h['ipv4']}\n", "yellow"),
        ("\nДальше: в панели Selectel создайте VPS в том же регионе и "
         "выберите этот IP из списка ваших floating IPs.", "dim"),
    )
    return Panel(body, border_style="green", title="🎉 HIT", title_align="left")


def _build_layout(state: RunState) -> Layout:
    layout = Layout()
    if state.hits:
        layout.split_column(
            Layout(_hit_panel(state), size=12, name="hit"),
            Layout(name="body"),
        )
        body = layout["body"]
    else:
        body = layout
    body.split_column(
        Layout(_summary_panel(state), size=13, name="summary"),
        Layout(_hosters_table(state), name="hosters"),
        Layout(_recent_panel(state), size=10, name="recent"),
    )
    return layout


def _tail(path: Path):
    """Генератор, возвращающий новые строки по мере появления."""
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        while True:
            line = fh.readline()
            if line:
                yield line
            else:
                time.sleep(0.5)
                yield None  # heartbeat — чтобы UI обновлял elapsed


def run_monitor(logfile: Path) -> None:
    """Основной цикл live-дашборда."""
    if not logfile.exists():
        console.print(f"[red]Лог не найден: {logfile}[/red]")
        console.print(
            "[dim]Запусти сначала: [bold]pars run > run.log 2>&1 &[/bold]  "
            "(или через меню, пункт «▶️ ЗАПУСТИТЬ поиск»)[/dim]"
        )
        return
    state = RunState()
    state.started_at = time.time()
    try:
        with Live(_build_layout(state), console=console, refresh_per_second=2, screen=True) as live:
            for line in _tail(logfile):
                if line is None:
                    live.update(_build_layout(state))
                    continue
                ev = _parse_event(line)
                if ev:
                    state.ingest(ev)
                # ре-рендер только когда есть событие или heartbeat
                live.update(_build_layout(state))
                if state.finished or state.hits:
                    # HIT остаётся на экране, ждём Ctrl+C или ~15с автосворачивание
                    if state.hits:
                        deadline = time.time() + 30
                        while time.time() < deadline:
                            live.update(_build_layout(state))
                            time.sleep(0.5)
                    break
    except KeyboardInterrupt:
        pass
    console.print("[dim]Мониторинг остановлен.[/dim]")
    if state.hits:
        h = state.hits[0]
        console.print(f"[green]HIT:[/green] {h['ipv4']} ({h['hoster']})")
