"""Command line entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from enum import IntEnum
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm

from mktux_backup import __version__
from mktux_backup.config import LoadedConfig, load_config
from mktux_backup.dashboard import run_with_dashboard, watch_state
from mktux_backup.errors import BackupCancelled, BackupError, ConfigError
from mktux_backup.manifest import verification_as_dict, verify_backup
from mktux_backup.orchestrator import BackupOrchestrator
from mktux_backup.preflight import GlobalPreflight, PreflightReport
from mktux_backup.presentation import render_preflight, render_sites, render_verification
from mktux_backup.state import RunStatus


class ExitCode(IntEnum):
    OK = 0
    FAILED = 1
    PARTIAL = 2
    CANCELLED = 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mktux-backup",
        description="Backup local de arquivos remotos e bancos MySQL.",
        epilog="Use 'mktux-backup <comando> --help' para detalhes.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("sites.yaml"),
        help="arquivo YAML de configuração (padrão: sites.yaml)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="arquivo de segredos (padrão: .env ao lado do YAML)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="lista sites configurados sem revelar segredos")

    check_parser = subparsers.add_parser("check", help="executa o preflight sem criar backup")
    _add_site_filter(check_parser)

    run_parser = subparsers.add_parser("run", help="executa um backup completo")
    _add_site_filter(run_parser)
    run_parser.add_argument(
        "--yes",
        action="store_true",
        help="confirma o resumo sem perguntar (necessário sem terminal interativo)",
    )
    run_parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="usa saída linear em vez do painel ao vivo",
    )
    run_parser.add_argument(
        "--concurrency",
        type=int,
        choices=range(1, 17),
        metavar="N",
        help="sobrescreve a concorrência configurada (1 a 16)",
    )

    verify_parser = subparsers.add_parser(
        "verify", help="verifica checksums e estrutura de um backup concluído"
    )
    verify_parser.add_argument("path", type=Path, help="diretório da execução de backup")
    verify_parser.add_argument(
        "--json", action="store_true", help="imprime o resultado em JSON"
    )

    watch_parser = subparsers.add_parser(
        "watch", help="acompanha a execução registrada em current.json"
    )
    watch_parser.add_argument(
        "--once", action="store_true", help="mostra o estado atual uma vez e encerra"
    )
    watch_parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        metavar="SEGUNDOS",
        help="intervalo de leitura do estado (padrão: 0.5)",
    )
    return parser


def _add_site_filter(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--site",
        action="append",
        dest="sites",
        metavar="ID",
        help="processa somente este site; pode ser repetido",
    )


def _load(args: argparse.Namespace) -> LoadedConfig:
    return load_config(args.config, args.env_file)


def _preflight(args: argparse.Namespace, loaded: LoadedConfig) -> PreflightReport:
    sites = loaded.selected_sites(args.sites)
    return GlobalPreflight(loaded).run(sites, concurrency=getattr(args, "concurrency", None))


def _confirm(report: PreflightReport, assume_yes: bool, console: Console) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise ConfigError("execução não interativa exige --yes")
    return Confirm.ask("Continuar?", default=False, console=console)


def dispatch(args: argparse.Namespace, console: Console) -> int:
    if args.command == "verify":
        report = verify_backup(args.path)
        if args.json:
            console.print_json(json.dumps(verification_as_dict(report), ensure_ascii=False))
        else:
            render_verification(console, report)
        return ExitCode.OK if report.valid else ExitCode.FAILED

    loaded = _load(args)

    if args.command == "list":
        render_sites(console, loaded)
        return ExitCode.OK

    if args.command == "watch":
        if args.interval <= 0:
            raise ConfigError("--interval precisa ser maior que zero")
        if not args.once and not console.is_terminal:
            raise ConfigError("watch sem terminal interativo exige --once")
        payload = watch_state(
            loaded.state_directory,
            console,
            once=args.once,
            interval=args.interval,
        )
        status = str(payload.get("status", ""))
        if status == RunStatus.COMPLETED_WITH_ERRORS.value:
            return ExitCode.PARTIAL
        if status in {RunStatus.CANCELLED.value, RunStatus.CRASHED.value}:
            return ExitCode.FAILED
        return ExitCode.OK

    report = _preflight(args, loaded)
    render_preflight(console, report)

    if args.command == "check":
        return ExitCode.OK if report.can_run and not report.invalid_sites else ExitCode.FAILED

    if not report.can_run:
        return ExitCode.FAILED
    if not _confirm(report, args.yes, console):
        console.print("[yellow]Execução cancelada; nenhum arquivo foi criado.[/yellow]")
        return ExitCode.CANCELLED

    orchestrator = BackupOrchestrator(loaded, report)
    if args.no_dashboard or not console.is_terminal:
        console.print(f"[cyan]Executando backup {orchestrator.run_id}...[/cyan]")
        result = orchestrator.run()
    else:
        result = run_with_dashboard(orchestrator, loaded.state_directory, console)
    if result.failed_sites:
        console.print(
            f"[bold yellow]Execução finalizada em {result.final_path}. "
            f"{len(result.failed_sites)} site(s) falharam; "
            "consulte o manifesto da execução.[/bold yellow]"
        )
        return ExitCode.PARTIAL
    console.print(f"[bold green]Backup finalizado em {result.final_path}[/bold green]")
    return ExitCode.OK


def main(argv: Sequence[str] | None = None, console: Console | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    output = console or Console()
    try:
        return int(dispatch(args, output))
    except BackupCancelled:
        output.print("[yellow]Execução cancelada pelo usuário.[/yellow]")
        return int(ExitCode.CANCELLED)
    except BackupError as error:
        output.print(f"[bold red]Erro:[/bold red] {error}")
        return int(ExitCode.FAILED)
    except KeyboardInterrupt:
        output.print("\n[yellow]Operação cancelada pelo usuário.[/yellow]")
        return int(ExitCode.CANCELLED)


if __name__ == "__main__":
    raise SystemExit(main())
