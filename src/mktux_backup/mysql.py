"""Read-only MySQL inspection and a no-lock streaming mysqldump pipeline."""

from __future__ import annotations

import hashlib
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO

import pymysql
import zstandard

from mktux_backup.archive import HashingWriter
from mktux_backup.config import LoadedConfig
from mktux_backup.errors import BackupCancelled, ConfigError, DatabaseError, VerificationError
from mktux_backup.models import DatabaseConfig, SiteConfig
from mktux_backup.security import SecretRedactor


@dataclass(frozen=True)
class DatabaseInspection:
    server_version: str
    estimated_bytes: int
    engines: dict[str, int]
    tls_cipher: str | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class DumpProgress:
    source_bytes: int


@dataclass(frozen=True)
class DumpResult:
    path: Path
    sha256: str
    compressed_bytes: int
    source_bytes: int
    mysqldump_version: str


def _database_credentials(loaded: LoadedConfig, site: SiteConfig) -> tuple[str, str]:
    config = site.database
    assert config.username_env is not None
    assert config.password_env is not None
    username = loaded.secrets.get(config.username_env)
    password = loaded.secrets.get(config.password_env)
    if not username or not password:
        raise ConfigError(f"credenciais de banco ausentes para {site.id}")
    return username, password


class MySqlInspector:
    def __init__(self, loaded: LoadedConfig) -> None:
        self.loaded = loaded

    def inspect(self, site: SiteConfig) -> DatabaseInspection:
        config = site.database
        if not config.enabled:
            raise DatabaseError(f"banco desabilitado para {site.id}")
        assert config.host and config.name
        username, password = _database_credentials(self.loaded, site)
        warnings: list[str] = []

        try:
            connection, encrypted = self._connect(config, username, password)
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT VERSION()")
                    version_row = cursor.fetchone()
                    server_version = str(version_row[0])
                    cursor.execute(
                        "SELECT COALESCE(ENGINE, 'UNKNOWN'), COUNT(*), "
                        "COALESCE(SUM(DATA_LENGTH + INDEX_LENGTH), 0) "
                        "FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s "
                        "GROUP BY COALESCE(ENGINE, 'UNKNOWN')",
                        (config.name,),
                    )
                    rows = cursor.fetchall()
                    cursor.execute("SHOW STATUS LIKE 'Ssl_cipher'")
                    cipher_row = cursor.fetchone()
            engines = {str(row[0]): int(row[1]) for row in rows}
            estimated_bytes = sum(int(row[2]) for row in rows)
            tls_cipher = str(cipher_row[1]) if cipher_row and cipher_row[1] else None
            if config.tls == "required" and not tls_cipher:
                raise DatabaseError(
                    f"MySQL de {site.id} não confirmou TLS, mas tls: required foi configurado"
                )
            if not encrypted or not tls_cipher:
                warnings.append("conexão MySQL sem TLS")
            non_transactional = sorted(
                engine for engine in engines if engine.upper() not in {"INNODB", "UNKNOWN"}
            )
            if non_transactional:
                warnings.append(
                    "engines não transacionais sem garantia de snapshot: "
                    + ", ".join(non_transactional)
                )
            if estimated_bytes == 0 and config.size_hint_mb:
                estimated_bytes = config.size_hint_mb * 1024 * 1024
                warnings.append("estimativa do banco obtida de size_hint_mb")
            return DatabaseInspection(
                server_version=server_version,
                estimated_bytes=estimated_bytes,
                engines=engines,
                tls_cipher=tls_cipher,
                warnings=tuple(warnings),
            )
        except pymysql.MySQLError as error:
            raise DatabaseError(f"falha ao inspecionar MySQL de {site.id}: {error}") from error

    @staticmethod
    def _connect(
        config: DatabaseConfig, username: str, password: str
    ) -> tuple[pymysql.Connection, bool]:
        assert config.host and config.name
        common = {
            "host": config.host,
            "port": config.port,
            "user": username,
            "password": password,
            "database": config.name,
            "charset": "utf8mb4",
            "connect_timeout": 15,
            "read_timeout": 30,
            "write_timeout": 30,
            "autocommit": True,
        }
        if config.tls == "disabled":
            return pymysql.connect(**common, ssl_disabled=True), False

        context = ssl.create_default_context()
        try:
            return pymysql.connect(**common, ssl=context), True
        except pymysql.MySQLError:
            if config.tls == "required":
                raise
            return pymysql.connect(**common, ssl_disabled=True), False


class MySqlDumper:
    def __init__(
        self,
        loaded: LoadedConfig,
        mysqldump_path: str,
        mysqldump_version: str,
        compression_level: int,
    ) -> None:
        self.loaded = loaded
        self.mysqldump_path = mysqldump_path
        self.mysqldump_version = mysqldump_version
        self.compression_level = compression_level

    def dump(
        self,
        site: SiteConfig,
        inspection: DatabaseInspection,
        destination: Path,
        progress: Callable[[DumpProgress], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> DumpResult:
        config = site.database
        assert config.enabled and config.host and config.name
        username, password = _database_credentials(self.loaded, site)
        partial = destination.with_name(f"{destination.name}.partial")
        partial.unlink(missing_ok=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        redactor = SecretRedactor([password])
        process: subprocess.Popen[bytes] | None = None
        monitor_done = threading.Event()
        cancellation_requested = threading.Event()
        monitor_thread: threading.Thread | None = None

        try:
            with tempfile.TemporaryDirectory(prefix="mktux-mysql-") as temp_directory:
                defaults_file = Path(temp_directory) / "client.cnf"
                defaults_file.write_text(
                    "[client]\n"
                    f"host={_option_value(config.host)}\n"
                    f"port={config.port}\n"
                    f"user={_option_value(username)}\n"
                    f"password={_option_value(password)}\n"
                    "default-character-set=utf8mb4\n",
                    encoding="utf-8",
                )
                if os.name != "nt":
                    defaults_file.chmod(0o600)
                command = self._command(config, defaults_file)
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                assert process.stdout is not None
                assert process.stderr is not None
                if cancelled:
                    monitor_thread = threading.Thread(
                        target=_monitor_cancellation,
                        args=(process, cancelled, monitor_done, cancellation_requested),
                        daemon=True,
                    )
                    monitor_thread.start()
                stderr_chunks: list[bytes] = []
                stderr_thread = threading.Thread(
                    target=_read_stderr,
                    args=(process.stderr, stderr_chunks),
                    daemon=True,
                )
                stderr_thread.start()
                source_bytes = 0
                with partial.open("xb") as raw_output:
                    hashing_output = HashingWriter(raw_output)
                    compressor = zstandard.ZstdCompressor(level=self.compression_level)
                    with compressor.stream_writer(hashing_output, closefd=False) as compressed:
                        while chunk := process.stdout.read(1024 * 1024):
                            compressed.write(chunk)
                            source_bytes += len(chunk)
                            if progress:
                                progress(DumpProgress(source_bytes=source_bytes))
                    raw_output.flush()
                    os.fsync(raw_output.fileno())
                    checksum = hashing_output.digest.hexdigest()
                    compressed_bytes = hashing_output.bytes_written
                return_code = process.wait()
                monitor_done.set()
                if monitor_thread:
                    monitor_thread.join(timeout=2)
                stderr_thread.join(timeout=5)
                if cancellation_requested.is_set():
                    raise BackupCancelled("execução cancelada pelo usuário")
                if return_code != 0:
                    stderr_text = b"".join(stderr_chunks)[-8000:].decode("utf-8", errors="replace")
                    raise DatabaseError(
                        f"mysqldump falhou para {site.id} (código {return_code}): "
                        f"{redactor.text(stderr_text).strip()}"
                    )
            os.replace(partial, destination)
            return DumpResult(
                path=destination,
                sha256=checksum,
                compressed_bytes=compressed_bytes,
                source_bytes=source_bytes,
                mysqldump_version=self.mysqldump_version,
            )
        except BaseException:
            monitor_done.set()
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if monitor_thread:
                monitor_thread.join(timeout=2)
            partial.unlink(missing_ok=True)
            raise

    def _command(self, config: DatabaseConfig, defaults_file: Path) -> list[str]:
        help_text = _mysqldump_help(self.mysqldump_path)
        command = [self.mysqldump_path, f"--defaults-extra-file={defaults_file}"]
        options = [
            "--single-transaction",
            "--quick",
            "--skip-lock-tables",
            "--routines",
            "--events",
            "--triggers",
            "--hex-blob",
        ]
        if "--no-tablespaces" in help_text:
            options.append("--no-tablespaces")
        if "--set-gtid-purged" in help_text:
            options.append("--set-gtid-purged=OFF")
        if "--column-statistics" in help_text:
            options.append("--column-statistics=0")
        command.extend(options)
        if "--ssl-mode" in help_text:
            ssl_mode = config.tls.upper()
            command.append(f"--ssl-mode={ssl_mode}")
        elif config.tls != "disabled" and "--ssl" in help_text:
            command.append("--ssl")
        elif config.tls == "disabled" and "--skip-ssl" in help_text:
            command.append("--skip-ssl")
        command.extend(["--databases", config.name or ""])
        return command


def _option_value(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        .replace('"', '\\"')
    )
    return f'"{escaped}"'


@lru_cache(maxsize=4)
def _mysqldump_help(path: str) -> str:
    try:
        result = subprocess.run(
            [path, "--help"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DatabaseError(f"não foi possível consultar mysqldump: {error}") from error
    return f"{result.stdout}\n{result.stderr}"


def _read_stderr(stream: BinaryIO, chunks: list[bytes]) -> None:
    while chunk := stream.read(64 * 1024):
        chunks.append(chunk)
        if sum(map(len, chunks)) > 1024 * 1024:
            del chunks[:-8]


def _monitor_cancellation(
    process: subprocess.Popen[bytes],
    cancelled: Callable[[], bool],
    done: threading.Event,
    requested: threading.Event,
) -> None:
    while not done.wait(0.2):
        if cancelled():
            requested.set()
            if process.poll() is None:
                process.terminate()
            return


def verify_zstd(path: Path) -> int:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as compressed:
            with zstandard.ZstdDecompressor().stream_reader(compressed) as reader:
                while chunk := reader.read(1024 * 1024):
                    digest.update(chunk)
                    total += len(chunk)
    except (OSError, zstandard.ZstdError) as error:
        raise VerificationError(f"arquivo Zstandard inválido: {path}: {error}") from error
    if total == 0:
        raise VerificationError(f"arquivo Zstandard vazio: {path}")
    return total


def find_mysqldump() -> tuple[str | None, str | None]:
    path = shutil.which("mysqldump") or shutil.which("mariadb-dump")
    if not path:
        return None, None
    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    if result.returncode != 0:
        return None, None
    version = (result.stdout or result.stderr).strip()
    return path, version or "versão não informada"
