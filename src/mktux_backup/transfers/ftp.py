"""FTP and explicit FTPS source implementation."""

from __future__ import annotations

import ftplib
import io
import posixpath
import socket
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import BinaryIO, Self

from mktux_backup.errors import TransferError
from mktux_backup.models import FtpFiles, FtpsFiles
from mktux_backup.transfers.base import EntryKind, FileInventory, RemoteEntry, sorted_inventory


class _RetryingFtpReader(io.RawIOBase):
    _MAX_RETRIES = 3
    _READ_ALL_CHUNK_SIZE = 1024 * 1024

    def __init__(self, source: FtpSource, entry: RemoteEntry) -> None:
        super().__init__()
        self.source = source
        self.entry = entry
        self.offset = 0
        self.retries = 0
        self.data_socket: socket.socket | None = None
        self.reader: BinaryIO | None = None
        self.finished = False
        self._open_with_retry()

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if self.closed:
            raise ValueError("I/O operation on closed FTP stream")
        if size == 0 or self.finished:
            return b""
        if size < 0:
            chunks: list[bytes] = []
            while chunk := self.read(self._READ_ALL_CHUNK_SIZE):
                chunks.append(chunk)
            return b"".join(chunks)

        chunks = []
        remaining = size
        while remaining:
            chunk = self._read_once(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_once(self, size: int) -> bytes:
        while True:
            assert self.reader is not None
            try:
                read = getattr(self.reader, "read1", None) or self.reader.read
                chunk = read(size)
            except (OSError, ftplib.Error, EOFError) as error:
                self._recover(error)
                continue
            if chunk:
                self.offset += len(chunk)
                return chunk

            try:
                self._complete_transfer()
            except (OSError, ftplib.Error, EOFError) as error:
                self._recover(error)
                continue
            if self.offset < self.entry.size:
                self._recover(
                    TransferError(
                        f"{self.entry.remote_path} terminou antes do esperado: "
                        f"{self.offset}/{self.entry.size} bytes"
                    )
                )
                continue
            self.finished = True
            self.source._files_since_connect += 1
            return b""

    def _open_with_retry(self) -> None:
        while True:
            try:
                if self.source.client is None:
                    self.source._connect()
                client = self.source._client()
                client.voidcmd("TYPE I")
                command = f"RETR {self.entry.remote_path}"
                if self.offset:
                    self.data_socket = client.transfercmd(command, rest=self.offset)
                else:
                    self.data_socket = client.transfercmd(command)
                self.data_socket.settimeout(self.source.config.timeout_seconds)
                self.reader = self.data_socket.makefile("rb")
                return
            except (OSError, ftplib.Error, EOFError) as error:
                self._close_data()
                self.source._discard_connection()
                if self.retries >= self._MAX_RETRIES:
                    self._raise_exhausted(error)
                self.retries += 1

    def _recover(self, error: BaseException) -> None:
        self._close_data()
        self.source._discard_connection()
        if self.retries >= self._MAX_RETRIES:
            self._raise_exhausted(error)
        self.retries += 1
        self._open_with_retry()

    def _complete_transfer(self) -> None:
        self._close_data()
        self.source._client().voidresp()

    def _close_data(self) -> None:
        if self.reader is not None:
            self.reader.close()
            self.reader = None
        if self.data_socket is not None:
            self.data_socket.close()
            self.data_socket = None

    def _raise_exhausted(self, error: BaseException) -> None:
        raise TransferError(
            f"falha ao ler {self.entry.remote_path} após "
            f"{self._MAX_RETRIES + 1} tentativa(s), retomando do byte {self.offset}: {error}"
        ) from error

    def close(self) -> None:
        if self.closed:
            return
        self._close_data()
        if not self.finished:
            self.source._discard_connection()
        super().close()


class FtpSource:
    _MAX_FILES_PER_CONNECTION = 100

    def __init__(self, config: FtpFiles | FtpsFiles, username: str, password: str) -> None:
        self.config = config
        self.username = username
        self.password = password
        self.client: ftplib.FTP | None = None
        self._files_since_connect = 0

    def __enter__(self) -> Self:
        self._connect()
        return self

    def _connect(self) -> None:
        self.close()
        try:
            if isinstance(self.config, FtpsFiles):
                context = (
                    ssl.create_default_context()
                    if self.config.verify_certificate
                    else ssl._create_unverified_context()
                )
                client: ftplib.FTP = ftplib.FTP_TLS(context=context)
            else:
                client = ftplib.FTP()
            client.connect(self.config.host, self.config.port, timeout=self.config.timeout_seconds)
            client.login(self.username, self.password)
            if isinstance(client, ftplib.FTP_TLS):
                client.prot_p()
            client.set_pasv(self.config.passive)
            client.voidcmd("TYPE I")
            self.client = client
            self._files_since_connect = 0
        except (OSError, ftplib.Error) as error:
            self.close()
            raise TransferError(
                f"não foi possível conectar por {self.config.protocol.upper()} "
                f"a {self.config.host}:{self.config.port}: {error}"
            ) from error

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self.client is None:
            return
        try:
            self.client.quit()
        except (OSError, ftplib.Error, EOFError):
            try:
                self.client.close()
            except OSError:
                pass
        self.client = None

    def _discard_connection(self) -> None:
        client = self.client
        self.client = None
        self._files_since_connect = 0
        if client is not None:
            try:
                client.close()
            except OSError:
                pass

    def inventory(self) -> FileInventory:
        client = self._client()
        warnings: list[str] = []
        entries: list[RemoteEntry] = []
        try:
            for configured_path in self.config.paths:
                remote_root = self._normalize(configured_path.remote)
                self._ensure_directory(client, remote_root)
                entries.append(
                    RemoteEntry(
                        remote_path=remote_root,
                        archive_path=configured_path.archive_as,
                        kind=EntryKind.DIRECTORY,
                        mode=0o755,
                    )
                )
                entries.extend(
                    self._walk(
                        client,
                        remote_root,
                        configured_path.archive_as,
                        warnings,
                        set(),
                    )
                )
        except (OSError, ftplib.Error) as error:
            raise TransferError(
                f"falha ao inventariar FTP em {self.config.host}: {error}"
            ) from error
        return sorted_inventory(iter(entries), warnings)

    def _walk(
        self,
        client: ftplib.FTP,
        remote_dir: str,
        archive_dir: str,
        warnings: list[str],
        seen_remote: set[str],
    ) -> list[RemoteEntry]:
        if remote_dir in seen_remote:
            warnings.append(f"diretório repetido ignorado: {remote_dir}")
            return []
        seen_remote.add(remote_dir)

        discovered: list[RemoteEntry] = []
        for name, facts in self._list_directory(client, remote_dir):
            child_name = posixpath.basename(name.rstrip("/"))
            if not child_name or child_name in {".", ".."}:
                continue
            remote_path = self._normalize(posixpath.join(remote_dir, child_name))
            if self._unsafe_child_name(child_name):
                warnings.append(f"nome de entrada FTP inseguro ignorado: {remote_path}")
                continue
            archive_path = posixpath.join(archive_dir, child_name)
            entry_type = facts.get("type", "")
            if "slink" in entry_type:
                warnings.append(f"link simbólico ignorado: {remote_path}")
                continue
            if entry_type == "dir":
                entry = RemoteEntry(
                    remote_path=remote_path,
                    archive_path=archive_path,
                    kind=EntryKind.DIRECTORY,
                    modified_at=self._parse_modify(facts.get("modify")),
                    mode=self._parse_mode(facts.get("unix.mode"), 0o755),
                )
                discovered.append(entry)
                discovered.extend(
                    self._walk(client, remote_path, archive_path, warnings, seen_remote)
                )
                continue
            if entry_type == "file":
                size = self._file_size(client, remote_path, facts.get("size"))
                discovered.append(
                    RemoteEntry(
                        remote_path=remote_path,
                        archive_path=archive_path,
                        kind=EntryKind.FILE,
                        size=size,
                        modified_at=self._parse_modify(facts.get("modify")),
                        mode=self._parse_mode(facts.get("unix.mode"), 0o644),
                    )
                )
                continue
            warnings.append(f"entrada FTP de tipo desconhecido ignorada: {remote_path}")
        return discovered

    def _list_directory(
        self, client: ftplib.FTP, remote_dir: str
    ) -> list[tuple[str, dict[str, str]]]:
        try:
            return list(client.mlsd(remote_dir, facts=["type", "size", "modify", "unix.mode"]))
        except (ftplib.error_perm, ftplib.error_reply):
            return self._list_directory_fallback(client, remote_dir)

    def _list_directory_fallback(
        self, client: ftplib.FTP, remote_dir: str
    ) -> list[tuple[str, dict[str, str]]]:
        results: list[tuple[str, dict[str, str]]] = []
        for raw_name in client.nlst(remote_dir):
            child_name = posixpath.basename(raw_name.rstrip("/"))
            if not child_name or child_name in {".", ".."}:
                continue
            path = self._normalize(posixpath.join(remote_dir, child_name))
            if self._is_directory(client, path):
                results.append((child_name, {"type": "dir"}))
                continue
            size = client.size(path)
            results.append((child_name, {"type": "file", "size": str(size or 0)}))
        return results

    @contextmanager
    def open_file(self, entry: RemoteEntry) -> Iterator[BinaryIO]:
        if entry.kind != EntryKind.FILE:
            raise TransferError(f"não é um arquivo remoto: {entry.remote_path}")
        if self._files_since_connect >= self._MAX_FILES_PER_CONNECTION:
            self._connect()
        reader = _RetryingFtpReader(self, entry)
        try:
            yield reader
        finally:
            reader.close()

    def _ensure_directory(self, client: ftplib.FTP, path: str) -> None:
        if not self._is_directory(client, path):
            raise TransferError(f"diretório remoto não encontrado: {path}")

    @staticmethod
    def _is_directory(client: ftplib.FTP, path: str) -> bool:
        current = client.pwd()
        try:
            client.cwd(path)
            return True
        except ftplib.error_perm:
            return False
        finally:
            try:
                client.cwd(current)
            except ftplib.Error:
                pass

    @staticmethod
    def _normalize(path: str) -> str:
        normalized = posixpath.normpath(path.replace("\\", "/"))
        return "/" if normalized == "." else normalized

    @staticmethod
    def _parse_modify(value: str | None) -> int | None:
        if not value:
            return None
        try:
            modified = datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
            return int(modified.timestamp())
        except ValueError:
            return None

    @staticmethod
    def _parse_mode(value: str | None, default: int) -> int:
        try:
            return int(value, 8) if value else default
        except ValueError:
            return default

    @staticmethod
    def _file_size(client: ftplib.FTP, path: str, value: str | None) -> int:
        try:
            if value is not None:
                return max(0, int(value))
        except ValueError:
            pass
        size = client.size(path)
        if size is None:
            raise TransferError(f"servidor FTP não informou o tamanho de {path}")
        return max(0, size)

    @staticmethod
    def _unsafe_child_name(value: str) -> bool:
        return "\\" in value or any(character in value for character in ("\0", "\r", "\n"))

    def _client(self) -> ftplib.FTP:
        if self.client is None:
            raise TransferError("a conexão FTP não está aberta")
        return self.client
