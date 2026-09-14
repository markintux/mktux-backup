import json
from pathlib import Path

import pytest

from mktux_backup.errors import LockError
from mktux_backup.security import SecretRedactor
from mktux_backup.state import EventLog, RunLock, RunState, StateStore


def test_state_write_is_readable_and_redacted(tmp_path: Path) -> None:
    store = StateStore(tmp_path, SecretRedactor(["super-secret"]))
    state = RunState(
        run_id="run-1",
        destination=str(tmp_path),
        selected_sites=["site-a"],
        warnings=["password=super-secret"],
    )

    store.write(state)

    payload = store.read()
    assert payload is not None
    assert payload["warnings"] == ["password=[REDACTED]"]
    assert not list(tmp_path.glob(".current-*.tmp"))


def test_event_log_redacts_nested_values(tmp_path: Path) -> None:
    log = EventLog(tmp_path, SecretRedactor(["pw-123"]))

    log.append("failure", message="credential pw-123", details={"value": "pw-123"})

    record = json.loads(log.path.read_text(encoding="utf-8"))
    assert record["message"] == "credential [REDACTED]"
    assert record["details"]["value"] == "[REDACTED]"


def test_lock_prevents_a_second_active_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "run.lock"
    first = RunLock(lock_path, "run-1").acquire()
    try:
        with pytest.raises(LockError, match="execução ativa"):
            RunLock(lock_path, "run-2").acquire()
    finally:
        first.release()

    assert RunLock.inspect(lock_path) is None


def test_stale_lock_is_replaced(tmp_path: Path) -> None:
    lock_path = tmp_path / "run.lock"
    lock_path.write_text(
        json.dumps(
            {"pid": 999_999_999, "run_id": "old", "token": "old", "started_at": "old"}
        ),
        encoding="utf-8",
    )

    lock = RunLock(lock_path, "new").acquire()
    try:
        assert RunLock.inspect(lock_path).run_id == "new"  # type: ignore[union-attr]
    finally:
        lock.release()
