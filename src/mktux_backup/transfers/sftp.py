"""SFTP source that never depends on a usable remote shell."""

from __future__ import annotations

import posixpath
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Self

import paramiko

from mktux_backup.errors import TransferError
from mktux_backup.models import SftpFiles
from mktux_backup.transfers.base import EntryKind, FileInventory, RemoteEntry, sorted_inventory


class SftpSource:
    def __init__(
        self,
        config: SftpFiles,
        username: str,
        password: str | None,
        passphrase: str | None,
        base_directory: Path,
    ) -> None:
        self.config = config
        self.username = username
        self.password = password
        self.passphrase = passphrase
        self.base_directory = base_directory
        self.ssh: paramiko.SSHClient | None = None
        self.sftp: paramiko.SFTPClient | None = None

    def __enter__(self) -> Self:
        client = paramiko.SSHClient()
        try:
            if self.config.known_hosts_file:
                client.load_system_host_keys(str(self._resolve(self.config.known_hosts_file)))
            else:
                client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            key_filename = (
                str(self._resolve(self.config.key_file)) if self.config.key_file else None
            )
            client.connect(
                hostname=self.config.host,
                port=self.config.port,
                username=self.username,
                password=self.password,
                key_filename=key_filename,
                passphrase=self.passphrase,
                allow_agent=self.config.allow_agent,
                look_for_keys=self.config.look_for_keys,
                timeout=self.config.timeout_seconds,
                banner_timeout=self.config.timeout_seconds,
                auth_timeout=self.config.timeout_seconds,
                channel_timeout=self.config.timeout_seconds,
            )
            self.sftp = client.open_sftp()
            self.ssh = client
            return self
        except (OSError, paramiko.SSHException) as error:
            client.close()
            raise TransferError(
                f"não foi possível conectar por SFTP a {self.config.host}:{self.config.port}: "
                f"{error}. Confirme a chave do host em known_hosts antes de executar."
            ) from error

    def __exit__(self, *_: object) -> None:
        if self.sftp is not None:
            try:
                self.sftp.close()
            except (OSError, paramiko.SSHException):
                pass
            self.sftp = None
        if self.ssh is not None:
            try:
                self.ssh.close()
            except (OSError, paramiko.SSHException):
                pass
            self.ssh = None

    def inventory(self) -> FileInventory:
        sftp = self._sftp()
        warnings: list[str] = []
        entries: list[RemoteEntry] = []
        try:
            for configured_path in self.config.paths:
                remote_root = posixpath.normpath(configured_path.remote.replace("\\", "/"))
                root_stat = sftp.lstat(remote_root)
                if not stat.S_ISDIR(root_stat.st_mode or 0):
                    raise TransferError(f"não é um diretório remoto: {remote_root}")
                entries.append(
                    RemoteEntry(
                        remote_path=remote_root,
                        archive_path=configured_path.archive_as,
                        kind=EntryKind.DIRECTORY,
                        modified_at=root_stat.st_mtime,
                        mode=stat.S_IMODE(root_stat.st_mode or 0o755),
                    )
                )
                entries.extend(
                    self._walk(
                        remote_root,
                        configured_path.archive_as,
                        warnings,
                        set(),
                    )
                )
        except (OSError, paramiko.SSHException) as error:
            raise TransferError(
                f"falha ao inventariar SFTP em {self.config.host}: {error}"
            ) from error
        return sorted_inventory(iter(entries), warnings)

    def _walk(
        self,
        remote_dir: str,
        archive_dir: str,
        warnings: list[str],
        seen: set[str],
    ) -> list[RemoteEntry]:
        if remote_dir in seen:
            warnings.append(f"diretório repetido ignorado: {remote_dir}")
            return []
        seen.add(remote_dir)
        discovered: list[RemoteEntry] = []
        for attributes in self._sftp().listdir_attr(remote_dir):
            name = attributes.filename
            if not name or name in {".", ".."}:
                continue
            remote_path = posixpath.join(remote_dir, name)
            if self._unsafe_child_name(name):
                warnings.append(f"nome de entrada SFTP inseguro ignorado: {remote_path}")
                continue
            archive_path = posixpath.join(archive_dir, name)
            mode = attributes.st_mode or 0
            if stat.S_ISLNK(mode):
                warnings.append(f"link simbólico ignorado: {remote_path}")
                continue
            if stat.S_ISDIR(mode):
                discovered.append(
                    RemoteEntry(
                        remote_path=remote_path,
                        archive_path=archive_path,
                        kind=EntryKind.DIRECTORY,
                        modified_at=attributes.st_mtime,
                        mode=stat.S_IMODE(mode),
                    )
                )
                discovered.extend(self._walk(remote_path, archive_path, warnings, seen))
                continue
            if stat.S_ISREG(mode):
                discovered.append(
                    RemoteEntry(
                        remote_path=remote_path,
                        archive_path=archive_path,
                        kind=EntryKind.FILE,
                        size=attributes.st_size or 0,
                        modified_at=attributes.st_mtime,
                        mode=stat.S_IMODE(mode),
                    )
                )
                continue
            warnings.append(f"entrada especial ignorada: {remote_path}")
        return discovered

    @contextmanager
    def open_file(self, entry: RemoteEntry) -> Iterator[BinaryIO]:
        if entry.kind != EntryKind.FILE:
            raise TransferError(f"não é um arquivo remoto: {entry.remote_path}")
        try:
            handle = self._sftp().open(entry.remote_path, "rb", bufsize=256 * 1024)
            try:
                handle.prefetch(entry.size, max_concurrent_requests=64)
            except TypeError:
                handle.prefetch(entry.size)
            try:
                yield handle
            finally:
                handle.close()
        except (OSError, paramiko.SSHException) as error:
            raise TransferError(f"falha ao ler {entry.remote_path}: {error}") from error

    def _resolve(self, path: Path) -> Path:
        expanded = path.expanduser()
        return expanded if expanded.is_absolute() else (self.base_directory / expanded).resolve()

    @staticmethod
    def _unsafe_child_name(value: str) -> bool:
        return any(character in value for character in ("/", "\\", "\0", "\r", "\n"))

    def _sftp(self) -> paramiko.SFTPClient:
        if self.sftp is None:
            raise TransferError("a conexão SFTP não está aberta")
        return self.sftp
