from __future__ import annotations

import io
import tarfile
from contextlib import contextmanager
from pathlib import Path

import pytest
import zstandard

from mktux_backup.archive import ArchiveWriter, verify_tar_zst
from mktux_backup.errors import TransferError
from mktux_backup.manifest import sha256_file
from mktux_backup.transfers.base import EntryKind, FileInventory, RemoteEntry


class MemorySource:
    def __init__(self, data: dict[str, bytes]) -> None:
        self.data = data

    @contextmanager
    def open_file(self, entry: RemoteEntry):
        yield io.BytesIO(self.data[entry.remote_path])


def inventory() -> FileInventory:
    return FileInventory(
        entries=[
            RemoteEntry("/uploads", "uploads", EntryKind.DIRECTORY),
            RemoteEntry("/uploads/a.txt", "uploads/a.txt", EntryKind.FILE, size=5),
            RemoteEntry("/uploads/b.bin", "uploads/b.bin", EntryKind.FILE, size=3),
        ]
    )


def test_writes_streaming_tar_zstd_with_checksum(tmp_path: Path) -> None:
    destination = tmp_path / "files.tar.zst"
    progress = []

    result = ArchiveWriter().write(
        MemorySource({"/uploads/a.txt": b"hello", "/uploads/b.bin": b"\x00\x01\x02"}),
        inventory(),
        destination,
        progress.append,
    )

    assert result.file_count == 2
    assert result.source_bytes == 8
    assert result.sha256 == sha256_file(destination)
    assert progress[-1].files_completed == 2
    assert verify_tar_zst(destination) == 3
    with destination.open("rb") as compressed:
        with zstandard.ZstdDecompressor().stream_reader(compressed) as reader:
            with tarfile.open(fileobj=reader, mode="r|") as archive:
                contents = {
                    member.name: archive.extractfile(member).read()
                    for member in archive
                    if member.isfile()
                }
    assert contents == {"uploads/a.txt": b"hello", "uploads/b.bin": b"\x00\x01\x02"}


def test_short_remote_read_removes_partial_output(tmp_path: Path) -> None:
    destination = tmp_path / "files.tar.zst"
    short = FileInventory(
        entries=[RemoteEntry("/file", "file", EntryKind.FILE, size=10)]
    )

    with pytest.raises(TransferError, match="antes do esperado"):
        ArchiveWriter().write(MemorySource({"/file": b"short"}), short, destination)

    assert not destination.exists()
    assert not destination.with_name("files.tar.zst.partial").exists()


def test_growing_remote_file_is_rejected(tmp_path: Path) -> None:
    destination = tmp_path / "files.tar.zst"
    old_inventory = FileInventory(
        entries=[RemoteEntry("/file", "file", EntryKind.FILE, size=4)]
    )

    with pytest.raises(TransferError, match="cresceu"):
        ArchiveWriter().write(MemorySource({"/file": b"longer"}), old_inventory, destination)

    assert not destination.exists()
