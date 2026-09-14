from __future__ import annotations

import io
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from mktux_backup.config import load_config
from mktux_backup.errors import BackupCancelled, TransferError
from mktux_backup.manifest import verify_backup
from mktux_backup.orchestrator import BackupOrchestrator
from mktux_backup.preflight import CheckResult, CheckStatus, PreflightReport, SitePreflight
from mktux_backup.state import RunStatus, StateStore
from mktux_backup.transfers.base import EntryKind, FileInventory, RemoteEntry

from .test_config import valid_yaml, write_config
from .test_preflight import write_all_secrets


class MemorySource:
    def __init__(self, data: dict[str, bytes], *, fail: bool = False) -> None:
        self.data = data
        self.fail = fail

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        pass

    @contextmanager
    def open_file(self, entry: RemoteEntry):
        if self.fail:
            raise TransferError("falha contendo password")
        yield io.BytesIO(self.data[entry.remote_path])


def make_inventory(remote_root: str, archive_root: str, contents: bytes) -> FileInventory:
    return FileInventory(
        entries=[
            RemoteEntry(remote_root, archive_root, EntryKind.DIRECTORY),
            RemoteEntry(
                f"{remote_root}/file.txt",
                f"{archive_root}/file.txt",
                EntryKind.FILE,
                size=len(contents),
            ),
        ]
    )


def make_loaded(tmp_path: Path):
    destination = tmp_path / "backups"
    destination.mkdir()
    body = valid_yaml(destination).replace("      enabled: true\n", "      enabled: false\n", 1)
    write_all_secrets(tmp_path)
    return load_config(write_config(tmp_path, body))


def test_orchestrator_finalizes_and_verifies_file_backup(tmp_path: Path, monkeypatch) -> None:
    loaded = make_loaded(tmp_path)
    site = loaded.config.sites[1]
    contents = b"monthly backup"
    inventory = make_inventory("/storage", "storage", contents)
    report = PreflightReport(
        destination=loaded.destination,
        available_bytes=10_000,
        concurrency=1,
        global_checks=[],
        sites=[SitePreflight(site=site, inventory=inventory, size_known=True)],
    )
    stale = loaded.destination / "_partial" / "older-incomplete-run"
    stale.mkdir(parents=True)
    monkeypatch.setattr(
        "mktux_backup.orchestrator.create_file_source",
        lambda loaded_config, selected: MemorySource({"/storage/file.txt": contents}),
    )

    result = BackupOrchestrator(loaded, report).run()

    assert result.status == RunStatus.SUCCESS
    assert (result.final_path / "site-ftp" / "files.tar.zst").is_file()
    assert (result.final_path / "run.log").is_file()
    assert verify_backup(result.final_path).valid
    assert stale.is_dir(), "execuções antigas nunca devem ser apagadas automaticamente"
    state = StateStore(loaded.state_directory).read()
    assert state is not None
    assert state["final_path"] == str(result.final_path)

    artifact = result.final_path / "site-ftp" / "files.tar.zst"
    with artifact.open("ab") as handle:
        handle.write(b"tampered")
    assert not verify_backup(result.final_path).valid


def test_verify_detects_missing_successful_site_manifest(tmp_path: Path, monkeypatch) -> None:
    loaded = make_loaded(tmp_path)
    site = loaded.config.sites[1]
    contents = b"data"
    inventory = make_inventory("/storage", "storage", contents)
    report = PreflightReport(
        destination=loaded.destination,
        available_bytes=10_000,
        concurrency=1,
        global_checks=[],
        sites=[SitePreflight(site=site, inventory=inventory, size_known=True)],
    )
    monkeypatch.setattr(
        "mktux_backup.orchestrator.create_file_source",
        lambda loaded_config, selected: MemorySource({"/storage/file.txt": contents}),
    )
    result = BackupOrchestrator(loaded, report).run()
    (result.final_path / "site-ftp" / "manifest.json").unlink()

    verification = verify_backup(result.final_path)

    assert not verification.valid
    assert any("manifesto de site ausente" in item.message for item in verification.items)


def test_one_site_failure_does_not_discard_successful_site(tmp_path: Path, monkeypatch) -> None:
    loaded = make_loaded(tmp_path)
    sftp_site, ftp_site = loaded.config.sites
    sftp_inventory = make_inventory("/uploads", "uploads", b"bad")
    ftp_inventory = make_inventory("/storage", "storage", b"good")
    report = PreflightReport(
        destination=loaded.destination,
        available_bytes=10_000,
        concurrency=2,
        global_checks=[],
        sites=[
            SitePreflight(site=sftp_site, inventory=sftp_inventory, size_known=True),
            SitePreflight(site=ftp_site, inventory=ftp_inventory, size_known=True),
        ],
    )

    def source_for(loaded_config, selected):
        if selected.id == "site-sftp":
            return MemorySource({"/uploads/file.txt": b"bad"}, fail=True)
        return MemorySource({"/storage/file.txt": b"good"})

    monkeypatch.setattr("mktux_backup.orchestrator.create_file_source", source_for)

    result = BackupOrchestrator(loaded, report).run()

    assert result.status == RunStatus.COMPLETED_WITH_ERRORS
    assert result.successful_sites == ("site-ftp",)
    assert "site-sftp" in result.failed_sites
    assert (result.final_path / "site-ftp" / "manifest.json").is_file()
    assert not (result.final_path / "site-sftp").exists()
    assert (result.final_path / "_failed" / "site-sftp").is_dir()
    run_manifest = json.loads(
        (result.final_path / "run-manifest.json").read_text(encoding="utf-8")
    )
    assert "[REDACTED]" in run_manifest["failed_sites"]["site-sftp"]


def test_preflight_failure_is_recorded_as_partial_run(tmp_path: Path, monkeypatch) -> None:
    loaded = make_loaded(tmp_path)
    sftp_site, ftp_site = loaded.config.sites
    ftp_inventory = make_inventory("/storage", "storage", b"good")
    report = PreflightReport(
        destination=loaded.destination,
        available_bytes=10_000,
        concurrency=2,
        global_checks=[],
        sites=[
            SitePreflight(
                site=sftp_site,
                checks=[CheckResult("SFTP", CheckStatus.ERROR, "host indisponível")],
            ),
            SitePreflight(site=ftp_site, inventory=ftp_inventory, size_known=True),
        ],
    )
    monkeypatch.setattr(
        "mktux_backup.orchestrator.create_file_source",
        lambda loaded_config, selected: MemorySource({"/storage/file.txt": b"good"}),
    )

    result = BackupOrchestrator(loaded, report).run()

    assert result.status == RunStatus.COMPLETED_WITH_ERRORS
    assert result.failed_sites == {"site-sftp": "host indisponível"}
    assert result.successful_sites == ("site-ftp",)
    manifest = json.loads((result.final_path / "run-manifest.json").read_text())
    assert manifest["failed_sites"] == {"site-sftp": "host indisponível"}
    state = StateStore(loaded.state_directory).read()
    assert state["sites"]["site-sftp"]["stage"] == "preflight"  # type: ignore[index]


def test_cancelled_run_preserves_its_staging(tmp_path: Path, monkeypatch) -> None:
    loaded = make_loaded(tmp_path)
    site = loaded.config.sites[1]
    inventory = make_inventory("/storage", "storage", b"data")
    report = PreflightReport(
        destination=loaded.destination,
        available_bytes=10_000,
        concurrency=1,
        global_checks=[],
        sites=[SitePreflight(site=site, inventory=inventory, size_known=True)],
    )
    monkeypatch.setattr(
        "mktux_backup.orchestrator.create_file_source",
        lambda loaded_config, selected: MemorySource({"/storage/file.txt": b"data"}),
    )
    orchestrator = BackupOrchestrator(loaded, report)
    orchestrator.cancel()

    with pytest.raises(BackupCancelled):
        orchestrator.run()

    assert not (loaded.destination / orchestrator.run_id).exists()
    assert (loaded.destination / "_partial" / orchestrator.run_id).is_dir()
    assert StateStore(loaded.state_directory).read()["status"] == "cancelled"  # type: ignore[index]
