"""Concurrent site orchestration with isolated failures and atomic finalization."""

from __future__ import annotations

import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mktux_backup.archive import ArchiveResult, ArchiveWriter, TransferProgress
from mktux_backup.config import LoadedConfig
from mktux_backup.errors import BackupCancelled, BackupError, DatabaseError
from mktux_backup.manifest import write_run_manifest, write_site_artifacts
from mktux_backup.mysql import DumpProgress, DumpResult, MySqlDumper
from mktux_backup.preflight import PreflightReport, SitePreflight
from mktux_backup.security import SecretRedactor
from mktux_backup.state import (
    EventLog,
    RunLock,
    RunState,
    RunStatus,
    SiteState,
    StateStore,
    new_run_id,
    now_iso,
)
from mktux_backup.storage import BackupStorage
from mktux_backup.transfers import create_file_source


@dataclass(frozen=True)
class SiteRunResult:
    site_id: str
    success: bool
    error: str = ""


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: RunStatus
    final_path: Path
    successful_sites: tuple[str, ...]
    failed_sites: dict[str, str]


class RunReporter:
    def __init__(self, state: RunState, store: StateStore, events: EventLog) -> None:
        self.state = state
        self.store = store
        self.events = events
        self._mutex = threading.Lock()
        self._last_write: dict[str, float] = {}

    def write(self) -> None:
        with self._mutex:
            self.store.write(self.state)

    def update_site(self, site_id: str, *, force: bool = True, **changes: Any) -> None:
        with self._mutex:
            site = self.state.sites[site_id]
            for key, value in changes.items():
                setattr(site, key, value)
            now = time.monotonic()
            if force or now - self._last_write.get(site_id, 0) >= 0.25:
                self.store.write(self.state)
                self._last_write[site_id] = now

    def event(self, event: str, **fields: Any) -> None:
        self.events.append(event, **fields)


class BackupOrchestrator:
    def __init__(self, loaded: LoadedConfig, report: PreflightReport) -> None:
        self.loaded = loaded
        self.report = report
        self.run_id = new_run_id()
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def _raise_if_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise BackupCancelled("execução cancelada pelo usuário")

    def run(self) -> RunResult:
        if not self.report.can_run:
            raise BackupError("o preflight não autorizou a execução")
        ready = self.report.ready_sites
        if any(
            (site.site.files and site.inventory is None)
            or (site.site.database.enabled and site.database_inspection is None)
            for site in ready
        ):
            raise BackupError("o preflight remoto está incompleto")
        if any(site.site.database.enabled for site in ready) and not self.report.mysqldump_path:
            raise DatabaseError("mysqldump não está disponível")

        run_id = self.run_id
        references = set().union(
            *(item.site.sensitive_references() for item in self.report.sites)
        )
        redactor = SecretRedactor(self.loaded.secrets.redaction_values(references))
        state_store = StateStore(self.loaded.state_directory, redactor)
        event_directory = self.loaded.state_directory / "runs" / run_id
        events = EventLog(event_directory, redactor)
        storage = BackupStorage(self.loaded.destination)
        state = RunState(
            run_id=run_id,
            destination=str(self.loaded.destination),
            selected_sites=[item.site.id for item in self.report.sites],
            sites={
                item.site.id: SiteState(site_id=item.site.id) for item in self.report.sites
            },
            warnings=[
                check.message
                for item in self.report.sites
                for check in item.checks
                if check.status.value == "warning"
            ],
        )
        skipped_results: list[SiteRunResult] = []
        for item in self.report.invalid_sites:
            message = "; ".join(
                check.message for check in item.checks if check.status.value == "error"
            ) or "site reprovado no preflight"
            message = redactor.text(message)
            state.sites[item.site.id].status = "failed"
            state.sites[item.site.id].stage = "preflight"
            state.sites[item.site.id].message = "ignorado após falha no preflight"
            state.sites[item.site.id].error = message
            skipped_results.append(SiteRunResult(item.site.id, False, message))
        reporter = RunReporter(state, state_store, events)
        lock = RunLock(self.loaded.state_directory / "run.lock", run_id)
        staging: Path | None = None
        started_at = now_iso()

        with lock:
            try:
                staging = storage.create_staging(run_id)
                state.status = RunStatus.RUNNING
                state.message = "backup em execução"
                reporter.write()
                reporter.event("run_started", run_id=run_id)
                for skipped in skipped_results:
                    reporter.event(
                        "site_skipped",
                        site_id=skipped.site_id,
                        reason=skipped.error,
                    )

                results: list[SiteRunResult] = list(skipped_results)
                with ThreadPoolExecutor(
                    max_workers=min(self.report.concurrency, len(ready)),
                    thread_name_prefix="backup",
                ) as executor:
                    futures = {
                        executor.submit(self._run_site, item, staging, reporter): item.site.id
                        for item in ready
                    }
                    for future in as_completed(futures):
                        site_id = futures[future]
                        try:
                            results.append(future.result())
                        except BackupCancelled:
                            self.cancel()
                            for pending in futures:
                                pending.cancel()
                            raise
                        except Exception as error:
                            message = redactor.text(str(error))
                            reporter.update_site(
                                site_id,
                                status="failed",
                                stage="failed",
                                error=message,
                                message="falha inesperada",
                            )
                            reporter.event("site_failed", site_id=site_id, error=message)
                            results.append(SiteRunResult(site_id, False, message))

                successful = sorted(result.site_id for result in results if result.success)
                failed = {
                    result.site_id: redactor.text(result.error)
                    for result in results
                    if not result.success
                }
                completed_at = now_iso()
                final_status = (
                    RunStatus.SUCCESS if not failed else RunStatus.COMPLETED_WITH_ERRORS
                )
                write_run_manifest(
                    staging,
                    run_id=run_id,
                    status=final_status.value,
                    started_at=started_at,
                    completed_at=completed_at,
                    destination=self.loaded.destination,
                    successful_sites=successful,
                    failed_sites=failed,
                    warnings=state.warnings,
                )
                state.status = final_status
                state.completed_at = completed_at
                state.message = "backup concluído" if not failed else "backup concluído com erros"
                reporter.event(
                    "run_completed",
                    status=final_status.value,
                    successful_sites=successful,
                    failed_sites=failed,
                    final_path=str(self.loaded.destination / run_id),
                )
                shutil.copyfile(events.path, staging / "run.log")
                final_path = storage.finalize(run_id)
                state.final_path = str(final_path)
                reporter.write()
                return RunResult(
                    run_id=run_id,
                    status=final_status,
                    final_path=final_path,
                    successful_sites=tuple(successful),
                    failed_sites=failed,
                )
            except (KeyboardInterrupt, BackupCancelled):
                self.cancel()
                state.status = RunStatus.CANCELLED
                state.completed_at = now_iso()
                state.message = (
                    f"cancelado; parcial preservado em {staging}"
                    if staging
                    else "cancelado pelo usuário"
                )
                reporter.event(
                    "run_cancelled", partial_path=str(staging) if staging else None
                )
                reporter.write()
                raise
            except Exception as error:
                state.status = RunStatus.CRASHED
                state.completed_at = now_iso()
                state.message = redactor.text(
                    f"{error}; parcial preservado em {staging}" if staging else str(error)
                )
                reporter.event("run_crashed", error=state.message)
                reporter.write()
                raise

    def _run_site(
        self, item: SitePreflight, staging: Path, reporter: RunReporter
    ) -> SiteRunResult:
        site = item.site
        site_directory = staging / site.id
        site_directory.mkdir(parents=True, exist_ok=False)
        started_at = now_iso()
        archive_result: ArchiveResult | None = None
        dump_result: DumpResult | None = None
        warnings = [*item.inventory.warnings] if item.inventory else []
        if item.database_inspection:
            warnings.extend(item.database_inspection.warnings)

        try:
            self._raise_if_cancelled()
            reporter.update_site(site.id, status="running", stage="starting", message="iniciando")
            reporter.event("site_started", site_id=site.id)
            if site.files:
                assert item.inventory is not None
                reporter.update_site(
                    site.id,
                    stage="files",
                    files_total=item.inventory.file_count,
                    bytes_total=item.inventory.total_bytes,
                    message="transferindo arquivos",
                )
                with create_file_source(self.loaded, site) as source:
                    archive_result = ArchiveWriter(
                        self.loaded.config.backup.compression_level
                    ).write(
                        source,
                        item.inventory,
                        site_directory / "files.tar.zst",
                        progress=lambda progress: self._file_progress(
                            reporter, site.id, progress
                        ),
                    )
                reporter.event(
                    "files_completed",
                    site_id=site.id,
                    files=archive_result.file_count,
                    bytes=archive_result.source_bytes,
                )

            if site.database.enabled:
                assert item.database_inspection is not None
                assert self.report.mysqldump_path and self.report.mysqldump_version
                reporter.update_site(
                    site.id,
                    stage="database",
                    current_item=site.database.name or "database",
                    files_done=0,
                    files_total=None,
                    bytes_done=0,
                    bytes_total=item.database_inspection.estimated_bytes,
                    message="exportando banco",
                )
                dump_result = MySqlDumper(
                    self.loaded,
                    self.report.mysqldump_path,
                    self.report.mysqldump_version,
                    self.loaded.config.backup.compression_level,
                ).dump(
                    site,
                    item.database_inspection,
                    site_directory / "database.sql.zst",
                    progress=lambda progress: self._dump_progress(
                        reporter, site.id, progress
                    ),
                    cancelled=self._cancel_event.is_set,
                )
                reporter.event(
                    "database_completed",
                    site_id=site.id,
                    bytes=dump_result.source_bytes,
                )

            completed_at = now_iso()
            write_site_artifacts(
                site_directory,
                site_id=site.id,
                protocol=site.files.protocol if site.files else None,
                remote_paths=(
                    [
                        {"remote": path.remote, "archive_as": path.archive_as}
                        for path in site.files.paths
                    ]
                    if site.files
                    else []
                ),
                inventory=item.inventory,
                archive=archive_result,
                database_name=site.database.name if site.database.enabled else None,
                inspection=item.database_inspection,
                dump=dump_result,
                started_at=started_at,
                completed_at=completed_at,
                warnings=warnings,
            )
            reporter.update_site(
                site.id,
                status="success",
                stage="completed",
                current_item="",
                message="concluído",
                warnings=warnings,
            )
            reporter.event("site_completed", site_id=site.id)
            return SiteRunResult(site.id, True)
        except Exception as error:
            if isinstance(error, BackupCancelled):
                raise
            self._preserve_failed_site(site_directory, staging, site.id)
            reporter.update_site(
                site.id,
                status="failed",
                stage="failed",
                error=str(error),
                message="falhou",
                warnings=warnings,
            )
            reporter.event("site_failed", site_id=site.id, error=str(error))
            return SiteRunResult(site.id, False, str(error))

    @staticmethod
    def _preserve_failed_site(site_directory: Path, staging: Path, site_id: str) -> None:
        if not site_directory.exists():
            return
        try:
            failed_directory = staging / "_failed"
            failed_directory.mkdir(exist_ok=True)
            site_directory.replace(failed_directory / site_id)
        except OSError:
            # Keep the original directory in place if even the local rename fails.
            pass

    def _file_progress(
        self, reporter: RunReporter, site_id: str, progress: TransferProgress
    ) -> None:
        self._raise_if_cancelled()
        reporter.update_site(
            site_id,
            force=False,
            current_item=progress.archive_path,
            files_done=progress.files_completed,
            files_total=progress.files_total,
            bytes_done=progress.bytes_completed,
            bytes_total=progress.bytes_total,
        )

    def _dump_progress(
        self, reporter: RunReporter, site_id: str, progress: DumpProgress
    ) -> None:
        self._raise_if_cancelled()
        reporter.update_site(site_id, force=False, bytes_done=progress.source_bytes)
