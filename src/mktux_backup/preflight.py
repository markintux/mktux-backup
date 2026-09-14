"""Global, remote-file, and database preflight checks."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from platform import python_version

from mktux_backup.config import LoadedConfig
from mktux_backup.errors import BackupError
from mktux_backup.models import FtpFiles, FtpsFiles, SiteConfig
from mktux_backup.mysql import DatabaseInspection, MySqlInspector, find_mysqldump
from mktux_backup.state import RunLock
from mktux_backup.storage import BackupStorage
from mktux_backup.transfers import FileInventory, create_file_source


class CheckStatus(StrEnum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    message: str


@dataclass
class SitePreflight:
    site: SiteConfig
    checks: list[CheckResult] = field(default_factory=list)
    inventory: FileInventory | None = None
    database_inspection: DatabaseInspection | None = None
    size_known: bool = False

    @property
    def ready(self) -> bool:
        return not any(check.status == CheckStatus.ERROR for check in self.checks)

    @property
    def warnings(self) -> int:
        return sum(check.status == CheckStatus.WARNING for check in self.checks)

    @property
    def estimated_bytes(self) -> int:
        files = self.inventory.total_bytes if self.inventory else 0
        database = self.database_inspection.estimated_bytes if self.database_inspection else 0
        return files + database


@dataclass
class PreflightReport:
    destination: Path
    available_bytes: int | None
    concurrency: int
    global_checks: list[CheckResult]
    sites: list[SitePreflight]
    mysqldump_version: str | None = None
    mysqldump_path: str | None = None

    @property
    def has_global_errors(self) -> bool:
        return any(check.status == CheckStatus.ERROR for check in self.global_checks)

    @property
    def ready_sites(self) -> list[SitePreflight]:
        return [site for site in self.sites if site.ready]

    @property
    def invalid_sites(self) -> list[SitePreflight]:
        return [site for site in self.sites if not site.ready]

    @property
    def can_run(self) -> bool:
        return not self.has_global_errors and bool(self.ready_sites)

    @property
    def estimated_bytes(self) -> int | None:
        if not self.ready_sites or not all(site.size_known for site in self.ready_sites):
            return None
        return sum(site.estimated_bytes for site in self.ready_sites)


class GlobalPreflight:
    def __init__(self, loaded: LoadedConfig) -> None:
        self.loaded = loaded

    def run(
        self,
        sites: list[SiteConfig],
        concurrency: int | None = None,
        remote: bool = True,
    ) -> PreflightReport:
        global_checks: list[CheckResult] = []
        available_bytes: int | None = None
        selected_concurrency = concurrency or self.loaded.config.backup.concurrency

        global_checks.append(CheckResult("Python", CheckStatus.OK, python_version()))

        state_directory = self.loaded.state_directory
        try:
            state_directory.mkdir(parents=True, exist_ok=True)
            if not state_directory.is_dir():
                raise NotADirectoryError(state_directory)
            with tempfile.NamedTemporaryFile(
                dir=state_directory, prefix=".write-probe-", delete=True
            ):
                pass
            global_checks.append(
                CheckResult("Estado", CheckStatus.OK, f"gravável: {state_directory}")
            )
        except OSError as error:
            global_checks.append(
                CheckResult("Estado", CheckStatus.ERROR, f"não pode ser gravado: {error}")
            )

        destination = self.loaded.destination
        destination_ok = False
        if not destination.exists():
            global_checks.append(
                CheckResult("Destino", CheckStatus.ERROR, f"não existe: {destination}")
            )
        elif not destination.is_dir():
            global_checks.append(
                CheckResult("Destino", CheckStatus.ERROR, f"não é um diretório: {destination}")
            )
        else:
            try:
                with tempfile.NamedTemporaryFile(
                    dir=destination, prefix=".write-probe-", delete=True
                ):
                    pass
                destination_ok = True
                global_checks.append(
                    CheckResult("Destino", CheckStatus.OK, f"gravável: {destination}")
                )
            except OSError as error:
                global_checks.append(
                    CheckResult("Destino", CheckStatus.ERROR, f"sem permissão de escrita: {error}")
                )

        if destination_ok:
            usage = shutil.disk_usage(destination)
            available_bytes = usage.free
            storage = BackupStorage(destination)
            stale = storage.stale_partial_runs()
            if stale:
                global_checks.append(
                    CheckResult(
                        "Parciais",
                        CheckStatus.WARNING,
                        f"{len(stale)} execução(ões) parcial(is) antiga(s) preservada(s); "
                        "revise manualmente",
                    )
                )
            else:
                global_checks.append(CheckResult("Parciais", CheckStatus.OK, "nenhuma encontrada"))

        lock_path = self.loaded.state_directory / "run.lock"
        lock = RunLock.inspect(lock_path)
        if lock and lock.active:
            global_checks.append(
                CheckResult(
                    "Execução ativa",
                    CheckStatus.ERROR,
                    f"PID {lock.pid}, run {lock.run_id}",
                )
            )
        elif lock:
            global_checks.append(
                CheckResult(
                    "Lock", CheckStatus.WARNING, "lock obsoleto será substituído ao executar"
                )
            )
        else:
            global_checks.append(CheckResult("Lock", CheckStatus.OK, "livre"))

        env_path = self.loaded.secrets.env_path
        if env_path.is_file():
            permission_warning = self._env_permission_warning(env_path)
            global_checks.append(
                CheckResult(
                    ".env",
                    CheckStatus.WARNING if permission_warning else CheckStatus.OK,
                    permission_warning or f"carregado de {env_path}",
                )
            )
        else:
            global_checks.append(
                CheckResult(
                    ".env",
                    CheckStatus.WARNING,
                    f"não encontrado; serão usadas apenas variáveis do processo ({env_path})",
                )
            )

        mysqldump_path, mysqldump_version = find_mysqldump()
        remote_enabled = remote and not any(
            check.status == CheckStatus.ERROR for check in global_checks
        )
        if remote and not remote_enabled and sites:
            global_checks.append(
                CheckResult(
                    "Conexões remotas",
                    CheckStatus.WARNING,
                    "não testadas porque o preflight local encontrou erro",
                )
            )
        if remote_enabled and len(sites) > 1:
            with ThreadPoolExecutor(
                max_workers=min(selected_concurrency, len(sites)),
                thread_name_prefix="preflight",
            ) as executor:
                site_reports = list(
                    executor.map(
                        lambda site: self._check_site(
                            site, mysqldump_path, mysqldump_version, remote_enabled
                        ),
                        sites,
                    )
                )
        else:
            site_reports = [
                self._check_site(site, mysqldump_path, mysqldump_version, remote_enabled)
                for site in sites
            ]

        if not sites:
            global_checks.append(
                CheckResult("Sites", CheckStatus.ERROR, "nenhum site habilitado foi selecionado")
            )

        ready_sites = [site for site in site_reports if site.ready]
        if destination_ok:
            if ready_sites and all(site.size_known for site in ready_sites):
                estimated = sum(site.estimated_bytes for site in ready_sites)
                margin = self.loaded.config.backup.free_space_margin_percent
                required = estimated + (estimated * margin // 100)
                if available_bytes is not None and available_bytes < required:
                    global_checks.append(
                        CheckResult(
                            "Espaço",
                            CheckStatus.ERROR,
                            f"insuficiente: necessários {required} bytes com margem de {margin}%",
                        )
                    )
                else:
                    global_checks.append(
                        CheckResult(
                            "Espaço",
                            CheckStatus.OK,
                            f"estimativa {estimated} bytes + margem de {margin}%",
                        )
                    )
            else:
                global_checks.append(
                    CheckResult(
                        "Espaço",
                        CheckStatus.WARNING,
                        "suficiência desconhecida porque há inventários indisponíveis",
                    )
                )

        return PreflightReport(
            destination=destination,
            available_bytes=available_bytes,
            concurrency=selected_concurrency,
            global_checks=global_checks,
            sites=site_reports,
            mysqldump_version=mysqldump_version,
            mysqldump_path=mysqldump_path,
        )

    def _check_site(
        self,
        site: SiteConfig,
        mysqldump_path: str | None,
        mysqldump_version: str | None,
        remote: bool,
    ) -> SitePreflight:
        report = SitePreflight(site=site)
        missing = self.loaded.secrets.missing_for(site)
        if missing:
            report.checks.append(
                CheckResult(
                    "Segredos",
                    CheckStatus.ERROR,
                    f"variáveis ausentes: {', '.join(missing)}",
                )
            )
        else:
            report.checks.append(CheckResult("Segredos", CheckStatus.OK, "presentes"))

        if site.files:
            if isinstance(site.files, FtpFiles):
                report.checks.append(
                    CheckResult("Arquivos", CheckStatus.WARNING, "FTP sem criptografia autorizado")
                )
            else:
                report.checks.append(
                    CheckResult("Arquivos", CheckStatus.OK, site.files.protocol.upper())
                )
            if isinstance(site.files, FtpsFiles) and not site.files.verify_certificate:
                report.checks.append(
                    CheckResult("Certificado FTPS", CheckStatus.WARNING, "verificação desabilitada")
                )
            if missing:
                report.checks.append(
                    CheckResult(
                        "Conexão de arquivos", CheckStatus.WARNING, "não testada sem segredos"
                    )
                )
            elif remote:
                try:
                    with create_file_source(self.loaded, site) as source:
                        report.inventory = source.inventory()
                    report.checks.append(
                        CheckResult(
                            "Conexão de arquivos",
                            CheckStatus.OK,
                            f"{report.inventory.file_count} arquivo(s), "
                            f"{report.inventory.total_bytes} bytes",
                        )
                    )
                    report.checks.extend(
                        CheckResult("Inventário", CheckStatus.WARNING, warning)
                        for warning in report.inventory.warnings
                    )
                except BackupError as error:
                    report.checks.append(
                        CheckResult("Conexão de arquivos", CheckStatus.ERROR, str(error))
                    )
            else:
                report.checks.append(
                    CheckResult(
                        "Conexão de arquivos", CheckStatus.WARNING, "teste remoto desabilitado"
                    )
                )

        if site.database.enabled:
            if mysqldump_path:
                report.checks.append(
                    CheckResult(
                        "mysqldump",
                        CheckStatus.OK,
                        f"{mysqldump_path} · {mysqldump_version}",
                    )
                )
            else:
                report.checks.append(
                    CheckResult(
                        "mysqldump",
                        CheckStatus.ERROR,
                        "não encontrado no PATH",
                    )
                )
            if missing:
                report.checks.append(
                    CheckResult("Conexão MySQL", CheckStatus.WARNING, "não testada sem segredos")
                )
            elif remote:
                try:
                    report.database_inspection = MySqlInspector(self.loaded).inspect(site)
                    inspection = report.database_inspection
                    report.checks.append(
                        CheckResult(
                            "Conexão MySQL",
                            CheckStatus.OK,
                            f"MySQL {inspection.server_version}; "
                            f"estimativa {inspection.estimated_bytes} bytes",
                        )
                    )
                    report.checks.extend(
                        CheckResult("MySQL", CheckStatus.WARNING, warning)
                        for warning in inspection.warnings
                    )
                except BackupError as error:
                    report.checks.append(
                        CheckResult("Conexão MySQL", CheckStatus.ERROR, str(error))
                    )
            else:
                report.checks.append(
                    CheckResult("Conexão MySQL", CheckStatus.WARNING, "teste remoto desabilitado")
                )
        report.size_known = remote and (
            (site.files is None or report.inventory is not None)
            and (not site.database.enabled or report.database_inspection is not None)
        )
        return report

    @staticmethod
    def _env_permission_warning(path: Path) -> str | None:
        if os.name == "nt":
            return None
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            return "não foi possível inspecionar as permissões"
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            return f"permissões amplas ({mode:o}); recomendado 600"
        return None
