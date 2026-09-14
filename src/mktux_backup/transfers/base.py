"""Language-neutral contract implemented by FTP and SFTP sources."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import BinaryIO, Protocol, Self


class EntryKind(StrEnum):
    DIRECTORY = "directory"
    FILE = "file"


@dataclass(frozen=True)
class RemoteEntry:
    remote_path: str
    archive_path: str
    kind: EntryKind
    size: int = 0
    modified_at: int | None = None
    mode: int | None = None


@dataclass
class FileInventory:
    entries: list[RemoteEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def files(self) -> list[RemoteEntry]:
        return [entry for entry in self.entries if entry.kind == EntryKind.FILE]

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.files)


class FileSource(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(self, *args: object) -> None: ...

    def inventory(self) -> FileInventory: ...

    def open_file(self, entry: RemoteEntry) -> AbstractContextManager[BinaryIO]: ...


def sorted_inventory(entries: Iterator[RemoteEntry], warnings: list[str]) -> FileInventory:
    return FileInventory(
        entries=sorted(entries, key=lambda entry: (entry.archive_path, entry.kind.value)),
        warnings=warnings,
    )
