from __future__ import annotations

import ftplib
import io
import socket
import stat
from dataclasses import dataclass
from pathlib import Path

from mktux_backup.models import FtpFiles, SftpFiles
from mktux_backup.transfers.base import EntryKind, RemoteEntry
from mktux_backup.transfers.ftp import FtpSource
from mktux_backup.transfers.sftp import SftpSource


class FakeFtp:
    def __init__(self) -> None:
        self.current = "/"
        self.directories = {"/", "/uploads", "/uploads/nested"}
        self.responses = 0

    def pwd(self) -> str:
        return self.current

    def cwd(self, path: str) -> None:
        if path not in self.directories:
            raise ftplib.error_perm("not a directory")
        self.current = path

    def mlsd(self, path: str, facts):
        if path == "/uploads":
            return iter(
                [
                    ("photo.jpg", {"type": "file", "size": "4"}),
                    ("nested", {"type": "dir"}),
                    ("shortcut", {"type": "OS.unix=slink"}),
                ]
            )
        return iter([("data.txt", {"type": "file", "size": "3"})])

    def transfercmd(self, command: str) -> socket.socket:
        reader, writer = socket.socketpair()
        writer.sendall(b"data")
        writer.close()
        return reader

    def voidresp(self) -> None:
        self.responses += 1


def test_ftp_inventory_recurses_and_skips_links() -> None:
    config = FtpFiles.model_validate(
        {
            "protocol": "ftp",
            "host": "ftp.example.com",
            "username_env": "USER",
            "password_env": "PASSWORD",
            "allow_insecure": True,
            "paths": [{"remote": "/uploads", "archive_as": "uploads"}],
        }
    )
    source = FtpSource(config, "user", "password")
    source.client = FakeFtp()  # type: ignore[assignment]

    result = source.inventory()

    assert result.file_count == 2
    assert result.total_bytes == 7
    assert any("link simbólico" in warning for warning in result.warnings)
    file_entry = next(entry for entry in result.files if entry.remote_path.endswith("photo.jpg"))
    with source.open_file(file_entry) as handle:
        assert handle.read() == b"data"
    assert source.client.responses == 1  # type: ignore[union-attr]


@dataclass
class FakeAttributes:
    filename: str
    st_mode: int
    st_size: int = 0
    st_mtime: int = 1


class FakeSftpHandle(io.BytesIO):
    def prefetch(self, size: int, max_concurrent_requests: int | None = None) -> None:
        self.prefetched = (size, max_concurrent_requests)


class FakeSftp:
    def lstat(self, path: str) -> FakeAttributes:
        return FakeAttributes(path, stat.S_IFDIR | 0o755)

    def listdir_attr(self, path: str) -> list[FakeAttributes]:
        if path == "/storage":
            return [
                FakeAttributes("app.log", stat.S_IFREG | 0o640, 3),
                FakeAttributes("cache", stat.S_IFDIR | 0o750),
                FakeAttributes("latest", stat.S_IFLNK | 0o777),
            ]
        return [FakeAttributes("item", stat.S_IFREG | 0o600, 2)]

    def open(self, path: str, mode: str, bufsize: int) -> FakeSftpHandle:
        return FakeSftpHandle(b"abc" if path.endswith("app.log") else b"xy")

    def close(self) -> None:
        pass


def test_sftp_inventory_does_not_need_remote_shell(tmp_path: Path) -> None:
    config = SftpFiles.model_validate(
        {
            "protocol": "sftp",
            "host": "ssh.example.com",
            "username_env": "USER",
            "password_env": "PASSWORD",
            "paths": [{"remote": "/storage", "archive_as": "storage"}],
        }
    )
    source = SftpSource(config, "user", "password", None, tmp_path)
    source.sftp = FakeSftp()  # type: ignore[assignment]

    result = source.inventory()

    assert [(entry.archive_path, entry.kind) for entry in result.entries] == [
        ("storage", EntryKind.DIRECTORY),
        ("storage/app.log", EntryKind.FILE),
        ("storage/cache", EntryKind.DIRECTORY),
        ("storage/cache/item", EntryKind.FILE),
    ]
    assert any("link simbólico" in warning for warning in result.warnings)
    with source.open_file(
        RemoteEntry("/storage/app.log", "storage/app.log", EntryKind.FILE, size=3)
    ) as handle:
        assert handle.read() == b"abc"


def test_sftp_connection_rejects_unknown_host_keys(tmp_path: Path, monkeypatch) -> None:
    import paramiko

    config = SftpFiles.model_validate(
        {
            "protocol": "sftp",
            "host": "ssh.example.com",
            "username_env": "USER",
            "password_env": "PASSWORD",
            "paths": [{"remote": "/storage", "archive_as": "storage"}],
        }
    )

    class FakeSsh:
        def __init__(self) -> None:
            self.policy = None
            self.connection = None

        def load_system_host_keys(self, filename=None) -> None:
            pass

        def set_missing_host_key_policy(self, policy) -> None:
            self.policy = policy

        def connect(self, **kwargs) -> None:
            self.connection = kwargs

        def open_sftp(self):
            return FakeSftp()

        def close(self) -> None:
            pass

    client = FakeSsh()
    monkeypatch.setattr("mktux_backup.transfers.sftp.paramiko.SSHClient", lambda: client)

    with SftpSource(config, "user", "password", None, tmp_path):
        pass

    assert isinstance(client.policy, paramiko.RejectPolicy)
    assert client.connection["look_for_keys"] is True
