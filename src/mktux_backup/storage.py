"""Safe staging and atomic finalization inside the configured destination."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from mktux_backup.errors import StorageError

RUN_ID_RE = re.compile(r"^[A-Za-z0-9._+-]+$")


class BackupStorage:
    def __init__(self, destination: Path) -> None:
        self.destination = destination.resolve()
        self.partial_root = self.destination / "_partial"

    def stale_partial_runs(self) -> list[Path]:
        if not self.partial_root.is_dir():
            return []
        return sorted(path for path in self.partial_root.iterdir() if path.is_dir())

    def create_staging(self, run_id: str) -> Path:
        target = self._partial_path(run_id)
        try:
            self.partial_root.mkdir(parents=True, exist_ok=True)
            target.mkdir(exist_ok=False)
        except OSError as error:
            raise StorageError(f"não foi possível criar staging {target}: {error}") from error
        return target

    def finalize(self, run_id: str) -> Path:
        source = self._partial_path(run_id)
        target = self._final_path(run_id)
        if not source.is_dir():
            raise StorageError(f"staging não encontrado: {source}")
        if target.exists():
            raise StorageError(f"o backup final já existe e não será sobrescrito: {target}")
        try:
            os.replace(source, target)
        except OSError as error:
            raise StorageError(f"não foi possível finalizar {target}: {error}") from error
        return target

    def discard_staging(self, run_id: str) -> None:
        target = self._partial_path(run_id)
        try:
            if target.exists():
                shutil.rmtree(target)
        except OSError as error:
            raise StorageError(f"não foi possível remover o staging {target}: {error}") from error

    def _partial_path(self, run_id: str) -> Path:
        self._validate_run_id(run_id)
        target = (self.partial_root / run_id).resolve()
        if target.parent != self.partial_root.resolve():
            raise StorageError("caminho de staging escapou da raiz parcial")
        return target

    def _final_path(self, run_id: str) -> Path:
        self._validate_run_id(run_id)
        target = (self.destination / run_id).resolve()
        if target.parent != self.destination:
            raise StorageError("caminho final escapou da raiz de backup")
        return target

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not RUN_ID_RE.fullmatch(run_id) or run_id in {".", "..", "_partial"}:
            raise StorageError(f"run id inseguro: {run_id!r}")
