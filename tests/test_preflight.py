import os
from pathlib import Path

from mktux_backup.config import load_config
from mktux_backup.preflight import CheckStatus, GlobalPreflight

from .test_config import valid_yaml, write_config


def write_all_secrets(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "SSH_USER=user\nSSH_PASSWORD=password\nDB_USER=db\nDB_PASSWORD=db-password\n"
        "FTP_USER=ftp\nFTP_PASSWORD=ftp-password\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        (tmp_path / ".env").chmod(0o600)


def test_missing_mysqldump_invalidates_only_database_site(
    tmp_path: Path, monkeypatch
) -> None:
    destination = tmp_path / "backups"
    destination.mkdir()
    write_all_secrets(tmp_path)
    loaded = load_config(write_config(tmp_path, valid_yaml(destination)))
    monkeypatch.setattr("mktux_backup.preflight.find_mysqldump", lambda: (None, None))

    report = GlobalPreflight(loaded).run(loaded.config.enabled_sites(), remote=False)

    assert not report.has_global_errors
    assert [site.site.id for site in report.ready_sites] == ["site-ftp"]
    assert [site.site.id for site in report.invalid_sites] == ["site-sftp"]
    assert report.concurrency == 2


def test_missing_destination_is_a_global_error(tmp_path: Path) -> None:
    write_all_secrets(tmp_path)
    loaded = load_config(write_config(tmp_path, valid_yaml(tmp_path / "missing")))

    report = GlobalPreflight(loaded).run(loaded.config.enabled_sites(), remote=False)

    assert report.has_global_errors
    destination_check = next(check for check in report.global_checks if check.name == "Destino")
    assert destination_check.status == CheckStatus.ERROR


def test_stale_partial_directory_is_reported(tmp_path: Path) -> None:
    destination = tmp_path / "backups"
    (destination / "_partial" / "old-run").mkdir(parents=True)
    write_all_secrets(tmp_path)
    loaded = load_config(write_config(tmp_path, valid_yaml(destination)))

    report = GlobalPreflight(loaded).run([loaded.config.sites[1]], remote=False)

    partial_check = next(check for check in report.global_checks if check.name == "Parciais")
    assert partial_check.status == CheckStatus.WARNING


def test_local_preflight_error_skips_remote_connections(tmp_path: Path, monkeypatch) -> None:
    write_all_secrets(tmp_path)
    loaded = load_config(write_config(tmp_path, valid_yaml(tmp_path / "missing")))

    def unexpected(*args, **kwargs):
        raise AssertionError("não deveria conectar")

    monkeypatch.setattr("mktux_backup.preflight.create_file_source", unexpected)
    monkeypatch.setattr("mktux_backup.preflight.MySqlInspector.inspect", unexpected)

    report = GlobalPreflight(loaded).run(loaded.config.enabled_sites(), remote=True)

    assert report.has_global_errors
    skipped = next(
        check for check in report.global_checks if check.name == "Conexões remotas"
    )
    assert skipped.status == CheckStatus.WARNING
