"""Atomic run state, append-only events, and a cross-platform execution lock."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, BinaryIO, Self

from mktux_backup.errors import LockError
from mktux_backup.security import SecretRedactor


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_run_id() -> str:
    timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    return f"{timestamp}-{secrets.token_hex(3)}"


class RunStatus(StrEnum):
    PREPARING = "preparing"
    READY = "ready"
    RUNNING = "running"
    SUCCESS = "success"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    CANCELLED = "cancelled"
    CRASHED = "crashed"


@dataclass
class SiteState:
    site_id: str
    status: str = "queued"
    stage: str = "waiting"
    files_done: int = 0
    files_total: int | None = None
    bytes_done: int = 0
    bytes_total: int | None = None
    message: str = ""
    current_item: str = ""
    error: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class RunState:
    run_id: str
    destination: str
    selected_sites: list[str]
    status: RunStatus = RunStatus.PREPARING
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    sites: dict[str, SiteState] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    completed_at: str | None = None
    final_path: str | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        self.updated_at = now_iso()
        data = asdict(self)
        data["status"] = self.status.value
        return data


class StateStore:
    def __init__(self, directory: Path, redactor: SecretRedactor | None = None) -> None:
        self.directory = directory
        self.path = directory / "current.json"
        self._redactor = redactor or SecretRedactor()
        self._mutex = threading.Lock()

    def write(self, state: RunState) -> None:
        self.write_payload(state.to_dict())

    def write_payload(self, payload: dict[str, Any]) -> None:
        with self._mutex:
            self.directory.mkdir(parents=True, exist_ok=True)
            sanitized = self._redactor.value(payload)
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.directory,
                    prefix=".current-",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    json.dump(sanitized, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                    temporary_path = Path(handle.name)
                os.replace(temporary_path, self.path)
            finally:
                if temporary_path and temporary_path.exists():
                    temporary_path.unlink()

    def read(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        with self.path.open("r", encoding="utf-8") as handle:
            return json.load(handle)


class EventLog:
    def __init__(self, directory: Path, redactor: SecretRedactor | None = None) -> None:
        self.directory = directory
        self.path = directory / "events.jsonl"
        self._redactor = redactor or SecretRedactor()
        self._mutex = threading.Lock()

    def append(self, event: str, **fields: Any) -> None:
        record = {"at": now_iso(), "event": event, **fields}
        sanitized = self._redactor.value(record)
        with self._mutex:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")


@dataclass(frozen=True)
class LockInfo:
    pid: int
    run_id: str
    token: str
    started_at: str
    active: bool


def _try_lock(handle: BinaryIO) -> bool:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b" ")
            handle.flush()
            handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class RunLock:
    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self.token = secrets.token_hex(16)
        self._owned = False
        self._handle: BinaryIO | None = None

    @classmethod
    def inspect(cls, path: Path) -> LockInfo | None:
        if not path.is_file():
            return None
        try:
            with path.open("r+b") as handle:
                active = not _try_lock(handle)
                handle.seek(0)
                raw = handle.read().decode("utf-8").strip()
                if not active:
                    _unlock(handle)
            if not raw or raw == "{}":
                return None
            data = json.loads(raw)
            pid = int(data["pid"])
            return LockInfo(
                pid=pid,
                run_id=str(data.get("run_id", "unknown")),
                token=str(data.get("token", "")),
                started_at=str(data.get("started_at", "unknown")),
                active=active,
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return LockInfo(pid=-1, run_id="unknown", token="", started_at="unknown", active=False)

    def acquire(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if not _try_lock(handle):
            handle.close()
            info = self.inspect(self.path)
            detail = f"PID {info.pid}, run {info.run_id}" if info else "detalhes indisponíveis"
            raise LockError(f"já existe uma execução ativa ({detail})") from None

        payload = {
            "pid": os.getpid(),
            "run_id": self.run_id,
            "token": self.token,
            "started_at": now_iso(),
        }
        handle.seek(0)
        handle.truncate()
        handle.write((json.dumps(payload) + "\n").encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle
        self._owned = True
        return self

    def release(self) -> None:
        if not self._owned:
            return
        assert self._handle is not None
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(b"{}\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())
        _unlock(self._handle)
        self._handle.close()
        self._handle = None
        self._owned = False

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()
