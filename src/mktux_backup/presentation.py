"""Rich renderers for configuration and preflight output."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from mktux_backup.config import LoadedConfig
from mktux_backup.manifest import VerificationReport
from mktux_backup.preflight import CheckResult, CheckStatus, PreflightReport


def human_bytes(value: int | None) -> str:
    if value is None:
        return "desconhecido"
    amount = float(value)
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    for unit in units:
        if abs(amount) < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} PB"


def _status_text(status: CheckStatus) -> Text:
    if status == CheckStatus.OK:
        return Text("OK", style="bold green")
    if status == CheckStatus.WARNING:
        return Text("AVISO", style="bold yellow")
    return Text("ERRO", style="bold red")


def render_sites(console: Console, loaded: LoadedConfig) -> None:
    table = Table(title="Sites configurados", header_style="bold cyan")
    table.add_column("Site")
    table.add_column("Estado")
    table.add_column("Arquivos")
    table.add_column("Caminhos", justify="right")
    table.add_column("Banco")
    for site in loaded.config.sites:
        protocol = site.files.protocol.upper() if site.files else "—"
        path_count = str(len(site.files.paths)) if site.files else "0"
        table.add_row(
            site.id,
            "habilitado" if site.enabled else "desabilitado",
            protocol,
            path_count,
            "sim" if site.database.enabled else "não",
        )
    console.print(table)


def _add_checks(table: Table, scope: str, checks: list[CheckResult]) -> None:
    for check in checks:
        table.add_row(
            Text(scope), Text(check.name), _status_text(check.status), Text(check.message)
        )


def render_preflight(console: Console, report: PreflightReport) -> None:
    summary = (
        f"Destino: [bold]{report.destination}[/bold]\n"
        f"Espaço disponível: [bold]{human_bytes(report.available_bytes)}[/bold]\n"
        f"Volume estimado: [bold]{human_bytes(report.estimated_bytes)}[/bold]\n"
        f"Concorrência: [bold]{report.concurrency}[/bold]\n"
        f"Sites prontos: [bold green]{len(report.ready_sites)}[/bold green] · "
        f"Inválidos: [bold red]{len(report.invalid_sites)}[/bold red]"
    )
    console.print(Panel(summary, title="Preflight", border_style="cyan"))

    table = Table(header_style="bold cyan", show_lines=False)
    table.add_column("Escopo", no_wrap=True)
    table.add_column("Verificação", no_wrap=True)
    table.add_column("Estado", no_wrap=True)
    table.add_column("Detalhe")
    _add_checks(table, "global", report.global_checks)
    for site in report.sites:
        _add_checks(table, site.site.id, site.checks)
    console.print(table)

    if not report.can_run:
        console.print("[bold red]O preflight encontrou erros que impedem a execução.[/bold red]")


def render_verification(console: Console, report: VerificationReport) -> None:
    table = Table(title=f"Verificação · {report.root}", header_style="bold cyan")
    table.add_column("Estado", no_wrap=True)
    table.add_column("Artefato")
    table.add_column("Detalhe")
    for item in report.items:
        status = Text("OK", style="bold green") if item.valid else Text("ERRO", style="bold red")
        table.add_row(status, item.path, item.message)
    console.print(table)
    if report.valid:
        console.print("[bold green]Todos os artefatos são íntegros.[/bold green]")
    else:
        console.print("[bold red]O backup contém artefatos inválidos.[/bold red]")
