from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

from mktux_backup.cli import ExitCode, main
from mktux_backup.preflight import GlobalPreflight

from .test_config import valid_yaml, write_config
from .test_preflight import write_all_secrets


def make_console() -> tuple[Console, object]:
    from io import StringIO

    stream = StringIO()
    return Console(file=stream, force_terminal=False, width=120), stream


def test_list_does_not_print_secret_values(tmp_path: Path) -> None:
    destination = tmp_path / "backups"
    destination.mkdir()
    config_path = write_config(tmp_path, valid_yaml(destination))
    write_all_secrets(tmp_path)
    console, stream = make_console()

    result = main(["--config", str(config_path), "list"], console=console)

    assert result == ExitCode.OK
    output = stream.getvalue()  # type: ignore[attr-defined]
    assert "site-sftp" in output
    assert "password" not in output


def test_check_reports_failure_when_mysqldump_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    destination = tmp_path / "backups"
    destination.mkdir()
    config_path = write_config(tmp_path, valid_yaml(destination))
    write_all_secrets(tmp_path)
    monkeypatch.setattr("mktux_backup.preflight.find_mysqldump", lambda: (None, None))
    monkeypatch.setattr("mktux_backup.cli._preflight", _offline_preflight)
    console, stream = make_console()

    result = main(["--config", str(config_path), "check"], console=console)

    assert result == ExitCode.FAILED
    assert "mysqldump" in stream.getvalue()  # type: ignore[attr-defined]


def test_run_yes_executes_orchestrator(tmp_path: Path, monkeypatch) -> None:
    destination = tmp_path / "backups"
    destination.mkdir()
    body = valid_yaml(destination).replace("      enabled: true\n", "      enabled: false\n", 1)
    config_path = write_config(tmp_path, body)
    write_all_secrets(tmp_path)
    monkeypatch.setattr("mktux_backup.cli._preflight", _offline_preflight)
    final_path = destination / "run-test"

    class FakeOrchestrator:
        run_id = "run-test"

        def __init__(self, loaded, report) -> None:
            pass

        def run(self):
            return SimpleNamespace(final_path=final_path, failed_sites={})

    monkeypatch.setattr("mktux_backup.cli.BackupOrchestrator", FakeOrchestrator)
    console, stream = make_console()

    result = main(["--config", str(config_path), "run", "--yes"], console=console)

    assert result == ExitCode.OK
    assert "run-test" in stream.getvalue()  # type: ignore[attr-defined]
    assert not (destination / "_partial").exists()


def test_run_requires_yes_without_interactive_terminal(tmp_path: Path, monkeypatch) -> None:
    destination = tmp_path / "backups"
    destination.mkdir()
    body = valid_yaml(destination).replace("      enabled: true\n", "      enabled: false\n", 1)
    config_path = write_config(tmp_path, body)
    write_all_secrets(tmp_path)
    monkeypatch.setattr("mktux_backup.cli._preflight", _offline_preflight)
    console, stream = make_console()

    result = main(["--config", str(config_path), "run"], console=console)

    assert result == ExitCode.FAILED
    assert "exige --yes" in stream.getvalue()  # type: ignore[attr-defined]


def test_verify_does_not_require_sites_configuration(tmp_path: Path) -> None:
    console, stream = make_console()

    result = main(["verify", str(tmp_path)], console=console)

    assert result == ExitCode.FAILED
    assert "run-manifest.json" in stream.getvalue()  # type: ignore[attr-defined]


def _offline_preflight(args, loaded):
    sites = loaded.selected_sites(args.sites)
    return GlobalPreflight(loaded).run(
        sites, concurrency=getattr(args, "concurrency", None), remote=False
    )
