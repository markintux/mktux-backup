"""One-pass TAR + Zstandard archive writer with an inline SHA-256 digest."""

from __future__ import annotations

import hashlib
import os
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

import zstandard

from mktux_backup.errors import TransferError, VerificationError
from mktux_backup.transfers.base import EntryKind, FileInventory, FileSource, RemoteEntry


@dataclass(frozen=True)
class TransferProgress:
    archive_path: str
    bytes_delta: int
    files_completed: int
    files_total: int
    bytes_completed: int
    bytes_total: int


@dataclass(frozen=True)
class ArchiveResult:
    path: Path
    sha256: str
    compressed_bytes: int
    source_bytes: int
    file_count: int


class HashingWriter:
    def __init__(self, output: BinaryIO) -> None:
        self.output = output
        self.digest = hashlib.sha256()
        self.bytes_written = 0

    def write(self, data: bytes) -> int:
        written = self.output.write(data)
        if written:
            self.digest.update(data[:written])
            self.bytes_written += written
        return written

    def flush(self) -> None:
        self.output.flush()

    def fileno(self) -> int:
        return self.output.fileno()


class ProgressReader:
    def __init__(self, source: BinaryIO, on_read: Callable[[int], None]) -> None:
        self.source = source
        self.on_read = on_read
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        data = self.source.read(size)
        if data:
            self.bytes_read += len(data)
            self.on_read(len(data))
        return data


class ArchiveWriter:
    def __init__(self, compression_level: int = 1) -> None:
        self.compression_level = compression_level

    def write(
        self,
        source: FileSource,
        inventory: FileInventory,
        destination: Path,
        progress: Callable[[TransferProgress], None] | None = None,
    ) -> ArchiveResult:
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(f"{destination.name}.partial")
        partial.unlink(missing_ok=True)
        bytes_completed = 0
        files_completed = 0

        try:
            with partial.open("xb") as raw_output:
                hashing_output = HashingWriter(raw_output)
                compressor = zstandard.ZstdCompressor(level=self.compression_level)
                with compressor.stream_writer(hashing_output, closefd=False) as compressed:
                    with tarfile.open(
                        fileobj=compressed,
                        mode="w|",
                        format=tarfile.PAX_FORMAT,
                    ) as archive:
                        for entry in inventory.entries:
                            info = self._tar_info(entry)
                            if entry.kind == EntryKind.DIRECTORY:
                                archive.addfile(info)
                                continue

                            completed_before = files_completed

                            def report_read(
                                delta: int,
                                current: RemoteEntry = entry,
                                completed: int = completed_before,
                            ) -> None:
                                nonlocal bytes_completed
                                bytes_completed += delta
                                if progress:
                                    progress(
                                        TransferProgress(
                                            archive_path=current.archive_path,
                                            bytes_delta=delta,
                                            files_completed=completed,
                                            files_total=inventory.file_count,
                                            bytes_completed=bytes_completed,
                                            bytes_total=inventory.total_bytes,
                                        )
                                    )

                            with source.open_file(entry) as remote_file:
                                reader = ProgressReader(remote_file, report_read)
                                try:
                                    archive.addfile(info, reader)
                                except OSError as error:
                                    if reader.bytes_read != entry.size:
                                        raise TransferError(
                                            f"{entry.remote_path} mudou ou terminou antes do "
                                            f"esperado: {reader.bytes_read}/{entry.size} bytes"
                                        ) from error
                                    raise
                                if reader.bytes_read != entry.size:
                                    raise TransferError(
                                        f"{entry.remote_path} mudou ou terminou antes do esperado: "
                                        f"{reader.bytes_read}/{entry.size} bytes"
                                    )
                                if remote_file.read(1):
                                    raise TransferError(
                                        f"{entry.remote_path} cresceu durante o backup"
                                    )
                            files_completed += 1
                            if progress:
                                progress(
                                    TransferProgress(
                                        archive_path=entry.archive_path,
                                        bytes_delta=0,
                                        files_completed=files_completed,
                                        files_total=inventory.file_count,
                                        bytes_completed=bytes_completed,
                                        bytes_total=inventory.total_bytes,
                                    )
                                )
                raw_output.flush()
                os.fsync(raw_output.fileno())
                checksum = hashing_output.digest.hexdigest()
                compressed_bytes = hashing_output.bytes_written
            os.replace(partial, destination)
            return ArchiveResult(
                path=destination,
                sha256=checksum,
                compressed_bytes=compressed_bytes,
                source_bytes=inventory.total_bytes,
                file_count=inventory.file_count,
            )
        except Exception:
            partial.unlink(missing_ok=True)
            raise

    @staticmethod
    def _tar_info(entry: RemoteEntry) -> tarfile.TarInfo:
        info = tarfile.TarInfo(entry.archive_path)
        info.size = entry.size if entry.kind == EntryKind.FILE else 0
        info.type = tarfile.REGTYPE if entry.kind == EntryKind.FILE else tarfile.DIRTYPE
        info.mode = entry.mode or (0o644 if entry.kind == EntryKind.FILE else 0o755)
        info.mtime = entry.modified_at or 0
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        return info


def verify_tar_zst(path: Path) -> int:
    members = 0
    try:
        with path.open("rb") as compressed:
            with zstandard.ZstdDecompressor().stream_reader(compressed) as stream:
                with tarfile.open(fileobj=stream, mode="r|") as archive:
                    for member in archive:
                        member_path = PurePosixPath(member.name)
                        if (
                            member_path.is_absolute()
                            or ".." in member_path.parts
                            or "\\" in member.name
                        ):
                            raise VerificationError(
                                f"caminho inseguro dentro de {path.name}: {member.name}"
                            )
                        members += 1
                        if member.isfile():
                            extracted = archive.extractfile(member)
                            if extracted is None:
                                raise VerificationError(
                                    f"não foi possível ler {member.name} em {path.name}"
                                )
                            for _ in iter(
                                lambda current=extracted: current.read(1024 * 1024), b""
                            ):
                                pass
    except (OSError, tarfile.TarError, zstandard.ZstdError) as error:
        raise VerificationError(f"arquivo TAR/Zstandard inválido: {path}: {error}") from error
    return members
