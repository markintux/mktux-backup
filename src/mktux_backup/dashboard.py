"""Live terminal dashboard backed by the atomic current run state."""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from mktux_backup.errors import BackupCancelled, BackupError
from mktux_backup.orchestrator import BackupOrchestrator, RunResult
from mktux_backup.presentation import human_bytes
from mktux_backup.state import StateStore

TERMINAL_STATUSES = {"success", "completed_with_errors", "cancelled", "crashed"}
STATUS_LABELS = {
    "preparing": ("PREPARANDO", "cyan"),
    "ready": ("PRONTO", "cyan"),
    "running": ("EXECUTANDO", "bold blue"),
    "queued": ("NA FILA", "dim"),
    "success": ("SUCESSO", "bold green"),
    "completed": ("CONCLUÍDO", "bold green"),
    "completed_with_errors": ("COM ERROS", "bold yellow"),
    "failed": ("FALHOU", "bold red"),
    "cancelled": ("CANCELADO", "bold yellow"),
    "crashed": ("INTERROMPIDO", "bold red"),
}


def _status(value: str) -> Text:
    label, style = STATUS_LABELS.get(value, (value.upper(), "white"))
    return Text(label, style=style)


def _progress(site: dict[str, Any]) -> str:
    done = int(site.get("bytes_done") or 0)
    total = site.get("bytes_total")
    files_done = int(site.get("files_done") or 0)
    files_total = site.get("files_total")
    if isinstance(total, int) and total > 0:
        percent = min(100, done * 100 / total)
        bytes_text = f"{human_bytes(done)} / {human_bytes(total)} ({percent:.0f}%)"
    else:
        bytes_text = human_bytes(done)
    if isinstance(files_total, int):
        return f"{files_done}/{files_total} arq · {bytes_text}"
    return bytes_text


def render_run_state(payload: dict[str, Any] | None, *, footer: str) -> RenderableType:
    if not payload:
        return Panel(
            "Aguardando o início de uma execução...",
            title="mktux-backup",
            border_style="cyan",
        )

    run_id = escape(str(payload.get("run_id", "—")))
    destination = escape(str(payload.get("destination", "—")))
    message = escape(str(payload.get("message", "")))
    summary = Table.grid(expand=True)
    summary.add_column(ratio=1)
    summary.add_column(justify="right")
    summary.add_row(f"Run: [bold]{run_id}[/bold]", _status(str(payload.get("status", ""))))
    summary.add_row(f"Destino: {destination}", message)

    table = Table(expand=True, header_style="bold cyan")
    table.add_column("Site", no_wrap=True)
    table.add_column("Estado", no_wrap=True)
    table.add_column("Etapa", no_wrap=True)
    table.add_column("Progresso", no_wrap=True)
    table.add_column("Item / mensagem", overflow="fold")
    for site_id, raw_site in dict(payload.get("sites") or {}).items():
        site = dict(raw_site or {})
        detail = site.get("error") or site.get("current_item") or site.get("message") or "—"
        table.add_row(
            Text(str(site_id)),
            _status(str(site.get("status", "queued"))),
            Text(str(site.get("stage", "—"))),
            Text(_progress(site)),
            Text(str(detail)),
        )

    return Group(
        Panel(summary, title="mktux-backup", border_style="cyan"),
        table,
        Text(footer, style="dim"),
    )


class TerminalKeyReader:
    """Read a single key without blocking when stdin is an interactive terminal."""

    def __init__(self) -> None:
        self.enabled = False
        self._fd: int | None = None
        self._settings: list[Any] | None = None

    def __enter__(self) -> Self:
        if not sys.stdin.isatty():
            return self
        self.enabled = True
        if os.name != "nt":
            import termios
            import tty

            self._fd = sys.stdin.fileno()
            self._settings = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def poll(self) -> str | None:
        if not self.enabled:
            return None
        if os.name == "nt":
            import msvcrt

            return msvcrt.getwch() if msvcrt.kbhit() else None

        import select

        readable, _, _ = select.select([sys.stdin], [], [], 0)
        return sys.stdin.read(1) if readable else None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._settings is not None and self._fd is not None:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._settings)


def run_with_dashboard(
    orchestrator: BackupOrchestrator,
    state_directory: Path,
    console: Console,
) -> RunResult:
    store = StateStore(state_directory)
    footer = "q oculta o painel; Ctrl+C cancela o backup com segurança"
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="orchestrator") as executor:
        future = executor.submit(orchestrator.run)
        hidden = False
        try:
            with TerminalKeyReader() as keys:
                with Live(
                    render_run_state(None, footer=footer),
                    console=console,
                    refresh_per_second=4,
                ) as live:
                    while not future.done():
                        payload = store.read()
                        if payload and payload.get("run_id") != orchestrator.run_id:
                            payload = None
                        live.update(render_run_state(payload, footer=footer))
                        if keys.poll() == "q":
                            hidden = True
                            break
                        time.sleep(0.15)
                    if future.done():
                        payload = store.read()
                        if payload and payload.get("run_id") != orchestrator.run_id:
                            payload = None
                        live.update(render_run_state(payload, footer=footer), refresh=True)
            if hidden:
                console.print("[dim]Painel oculto; o backup continua em primeiro plano.[/dim]")
            return future.result()
        except KeyboardInterrupt:
            orchestrator.cancel()
            console.print("\n[yellow]Cancelando transferências em andamento...[/yellow]")
            try:
                return future.result()
            except BackupCancelled:
                raise


def watch_state(
    state_directory: Path,
    console: Console,
    *,
    once: bool = False,
    interval: float = 0.5,
) -> dict[str, Any]:
    store = StateStore(state_directory)
    payload = store.read()
    if payload is None:
        raise BackupError(f"nenhuma execução encontrada em {store.path}")
    footer = "q encerra o monitor sem cancelar o backup"
    if once:
        console.print(render_run_state(payload, footer=footer))
        return payload

    with TerminalKeyReader() as keys:
        with Live(
            render_run_state(payload, footer=footer),
            console=console,
            refresh_per_second=4,
        ) as live:
            while str(payload.get("status", "")) not in TERMINAL_STATUSES:
                if keys.poll() == "q":
                    break
                time.sleep(interval)
                payload = store.read() or payload
                live.update(render_run_state(payload, footer=footer))
            live.update(render_run_state(payload, footer=footer), refresh=True)
    return payload
