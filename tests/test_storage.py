from pathlib import Path

import pytest

from mktux_backup.errors import StorageError
from mktux_backup.storage import BackupStorage


def test_staging_can_be_finalized_without_overwrite(tmp_path: Path) -> None:
    storage = BackupStorage(tmp_path)
    staging = storage.create_staging("run-1")
    (staging / "manifest.json").write_text("{}", encoding="utf-8")

    final = storage.finalize("run-1")

    assert (final / "manifest.json").is_file()
    with pytest.raises(StorageError, match="staging não encontrado"):
        storage.finalize("run-1")


def test_discard_only_accepts_safe_partial_run_ids(tmp_path: Path) -> None:
    storage = BackupStorage(tmp_path)
    staging = storage.create_staging("safe-run")
    storage.discard_staging("safe-run")
    assert not staging.exists()

    with pytest.raises(StorageError, match="run id inseguro"):
        storage.discard_staging("../outside")


def test_lists_only_partial_directories(tmp_path: Path) -> None:
    storage = BackupStorage(tmp_path)
    expected = storage.create_staging("old-run")
    (storage.partial_root / "note.txt").write_text("ignore", encoding="utf-8")

    assert storage.stale_partial_runs() == [expected]
