from __future__ import annotations

import io
import threading
from pathlib import Path

import pytest

from mktux_backup.config import load_config
from mktux_backup.errors import BackupCancelled
from mktux_backup.mysql import DatabaseInspection, MySqlDumper, MySqlInspector, verify_zstd

from .test_config import valid_yaml, write_config
from .test_preflight import write_all_secrets


class FakeProcess:
    def __init__(self, output: bytes = b"CREATE DATABASE app;\n") -> None:
        self.stdout = io.BytesIO(output)
        self.stderr = io.BytesIO()
        self.returncode: int | None = None
        self.terminated = False

    def wait(self, timeout: int | None = None) -> int:
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


class BlockingProcess(FakeProcess):
    class BlockingOutput:
        def __init__(self, stopped: threading.Event) -> None:
            self.stopped = stopped

        def read(self, size: int = -1) -> bytes:
            self.stopped.wait(timeout=3)
            return b""

    def __init__(self) -> None:
        super().__init__(b"")
        self.stopped = threading.Event()
        self.stdout = self.BlockingOutput(self.stopped)  # type: ignore[assignment]

    def terminate(self) -> None:
        super().terminate()
        self.stopped.set()

    def kill(self) -> None:
        super().kill()
        self.stopped.set()


def prepare_dumper(tmp_path: Path) -> tuple[MySqlDumper, object]:
    destination = tmp_path / "backups"
    destination.mkdir()
    write_all_secrets(tmp_path)
    loaded = load_config(write_config(tmp_path, valid_yaml(destination)))
    return MySqlDumper(loaded, "/usr/bin/mysqldump", "mysqldump 8", 1), loaded.config.sites[0]


def inspection() -> DatabaseInspection:
    return DatabaseInspection("8.0", 100, {"InnoDB": 1}, "TLS_AES_256_GCM_SHA384")


def test_mysql_dump_streams_to_zstd_without_password_in_command(
    tmp_path: Path, monkeypatch
) -> None:
    dumper, site = prepare_dumper(tmp_path)
    process = FakeProcess()
    commands: list[list[str]] = []

    def popen(command, **kwargs):
        commands.append(command)
        return process

    monkeypatch.setattr("mktux_backup.mysql.subprocess.Popen", popen)
    monkeypatch.setattr(
        "mktux_backup.mysql._mysqldump_help", lambda path: "--ssl-mode --no-tablespaces"
    )
    destination = tmp_path / "database.sql.zst"

    result = dumper.dump(site, inspection(), destination)

    assert result.source_bytes == len(b"CREATE DATABASE app;\n")
    assert verify_zstd(destination) == result.source_bytes
    assert "db-password" not in " ".join(commands[0])
    assert "--single-transaction" in commands[0]
    assert "--ssl-mode=PREFERRED" in commands[0]


def test_mysql_dump_terminates_process_on_cancellation(tmp_path: Path, monkeypatch) -> None:
    dumper, site = prepare_dumper(tmp_path)
    process = FakeProcess()
    monkeypatch.setattr("mktux_backup.mysql.subprocess.Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr("mktux_backup.mysql._mysqldump_help", lambda path: "")
    destination = tmp_path / "database.sql.zst"

    with pytest.raises(BackupCancelled):
        dumper.dump(
            site,
            inspection(),
            destination,
            progress=lambda progress: (_ for _ in ()).throw(BackupCancelled("stop")),
        )

    assert process.terminated
    assert not destination.exists()
    assert not destination.with_name("database.sql.zst.partial").exists()


def test_mysql_dump_monitor_interrupts_a_stalled_process(tmp_path: Path, monkeypatch) -> None:
    dumper, site = prepare_dumper(tmp_path)
    process = BlockingProcess()
    monkeypatch.setattr("mktux_backup.mysql.subprocess.Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr("mktux_backup.mysql._mysqldump_help", lambda path: "")

    with pytest.raises(BackupCancelled):
        dumper.dump(
            site,
            inspection(),
            tmp_path / "database.sql.zst",
            cancelled=lambda: True,
        )

    assert process.terminated


class FakeCursor:
    def __init__(self) -> None:
        self.query = ""

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def execute(self, query: str, params=None) -> None:
        self.query = query

    def fetchone(self):
        if "VERSION" in self.query:
            return ("8.0.36",)
        return ("Ssl_cipher", "")

    def fetchall(self):
        return [("InnoDB", 2, 100), ("MyISAM", 1, 50)]


class FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def cursor(self) -> FakeCursor:
        return FakeCursor()


def test_mysql_inspector_reports_plaintext_fallback_and_nontransactional_engine(
    tmp_path: Path, monkeypatch
) -> None:
    dumper, site = prepare_dumper(tmp_path)
    calls = []

    def connect(**kwargs):
        calls.append(kwargs)
        if "ssl" in kwargs:
            import pymysql

            raise pymysql.OperationalError(2026, "TLS unavailable")
        return FakeConnection()

    monkeypatch.setattr("mktux_backup.mysql.pymysql.connect", connect)

    result = MySqlInspector(dumper.loaded).inspect(site)

    assert result.estimated_bytes == 150
    assert result.engines == {"InnoDB": 2, "MyISAM": 1}
    assert "conexão MySQL sem TLS" in result.warnings
    assert any("não transacionais" in warning for warning in result.warnings)
    assert len(calls) == 2
