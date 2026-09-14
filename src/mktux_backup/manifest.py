"""Write manifests and verify completed backups without restoring them."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from mktux_backup.archive import ArchiveResult, verify_tar_zst
from mktux_backup.errors import VerificationError
from mktux_backup.mysql import DatabaseInspection, DumpResult, verify_zstd
from mktux_backup.transfers.base import FileInventory


@dataclass(frozen=True)
class VerificationItem:
    path: str
    valid: bool
    message: str


@dataclass(frozen=True)
class VerificationReport:
    root: Path
    items: tuple[VerificationItem, ...]

    @property
    def valid(self) -> bool:
        return bool(self.items) and all(item.valid for item in self.items)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def write_site_artifacts(
    site_directory: Path,
    *,
    site_id: str,
    protocol: str | None,
    remote_paths: list[dict[str, str]],
    inventory: FileInventory | None,
    archive: ArchiveResult | None,
    database_name: str | None,
    inspection: DatabaseInspection | None,
    dump: DumpResult | None,
    started_at: str,
    completed_at: str,
    warnings: list[str],
) -> None:
    artifacts: list[dict[str, Any]] = []
    checksums: list[tuple[str, str]] = []
    if archive:
        artifacts.append(
            {
                "kind": "files",
                "path": archive.path.name,
                "sha256": archive.sha256,
                "compressed_bytes": archive.compressed_bytes,
                "source_bytes": archive.source_bytes,
                "file_count": archive.file_count,
            }
        )
        checksums.append((archive.sha256, archive.path.name))
    if dump:
        artifacts.append(
            {
                "kind": "database",
                "path": dump.path.name,
                "sha256": dump.sha256,
                "compressed_bytes": dump.compressed_bytes,
                "source_bytes": dump.source_bytes,
                "mysqldump_version": dump.mysqldump_version,
            }
        )
        checksums.append((dump.sha256, dump.path.name))

    payload: dict[str, Any] = {
        "schema_version": 1,
        "site_id": site_id,
        "status": "success",
        "started_at": started_at,
        "completed_at": completed_at,
        "files": {
            "protocol": protocol,
            "remote_paths": remote_paths,
            "inventory": {
                "entries": len(inventory.entries) if inventory else 0,
                "files": inventory.file_count if inventory else 0,
                "bytes": inventory.total_bytes if inventory else 0,
            },
        },
        "database": {
            "name": database_name,
            "server_version": inspection.server_version if inspection else None,
            "estimated_bytes": inspection.estimated_bytes if inspection else None,
            "engines": inspection.engines if inspection else {},
            "tls_cipher": inspection.tls_cipher if inspection else None,
        },
        "artifacts": artifacts,
        "warnings": warnings,
    }
    write_json_atomic(site_directory / "manifest.json", payload)
    checksum_text = "".join(f"{checksum}  {name}\n" for checksum, name in checksums)
    (site_directory / "checksums.sha256").write_text(checksum_text, encoding="utf-8")


def write_run_manifest(
    staging: Path,
    *,
    run_id: str,
    status: str,
    started_at: str,
    completed_at: str,
    destination: Path,
    successful_sites: list[str],
    failed_sites: dict[str, str],
    warnings: list[str],
) -> None:
    write_json_atomic(
        staging / "run-manifest.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "status": status,
            "started_at": started_at,
            "completed_at": completed_at,
            "destination": str(destination),
            "successful_sites": successful_sites,
            "failed_sites": failed_sites,
            "warnings": warnings,
        },
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_backup(root: Path) -> VerificationReport:
    root = root.expanduser().resolve()
    run_manifest_path = root / "run-manifest.json"
    if not run_manifest_path.is_file():
        raise VerificationError(f"run-manifest.json não encontrado em {root}")

    items: list[VerificationItem] = []
    try:
        run_payload = json.loads(run_manifest_path.read_text(encoding="utf-8"))
        successful_sites = run_payload["successful_sites"]
        if not isinstance(successful_sites, list) or not all(
            isinstance(site_id, str) for site_id in successful_sites
        ):
            raise ValueError("successful_sites inválido")
        if len(successful_sites) != len(set(successful_sites)):
            raise ValueError("successful_sites contém ids repetidos")
        items.append(VerificationItem(str(run_manifest_path), True, "manifesto legível"))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        return VerificationReport(
            root=root,
            items=(
                VerificationItem(str(run_manifest_path), False, f"manifesto inválido: {error}"),
            ),
        )

    if not successful_sites:
        items.append(VerificationItem(str(root), False, "nenhum site concluído para verificar"))

    site_manifests: list[Path] = []
    for site_id in successful_sites:
        relative = Path(site_id)
        if (
            relative.name != site_id
            or site_id in {".", ".."}
            or any(separator in site_id for separator in ("/", "\\"))
        ):
            items.append(VerificationItem(site_id, False, "id de site inseguro no manifesto"))
            continue
        manifest_path = root / site_id / "manifest.json"
        if not manifest_path.is_file() or manifest_path.parent.is_symlink():
            items.append(
                VerificationItem(str(manifest_path), False, "manifesto de site ausente ou inseguro")
            )
            continue
        site_manifests.append(manifest_path)

    for manifest_path in site_manifests:
        site_directory = manifest_path.parent.resolve()
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            items.append(
                VerificationItem(str(manifest_path), False, f"manifesto inválido: {error}")
            )
            continue
        if not isinstance(payload, dict):
            items.append(
                VerificationItem(str(manifest_path), False, "manifesto precisa ser um objeto JSON")
            )
            continue
        if payload.get("site_id") != manifest_path.parent.name:
            items.append(
                VerificationItem(str(manifest_path), False, "site_id não corresponde ao diretório")
            )
            continue
        artifacts = payload.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            items.append(
                VerificationItem(str(manifest_path), False, "lista de artefatos vazia ou inválida")
            )
            continue
        checksum_path = site_directory / "checksums.sha256"
        try:
            checksum_lines = checksum_path.read_text(encoding="utf-8").splitlines()
            parsed_checksums: dict[str, str] = {}
            for line in checksum_lines:
                checksum, name = line.split("  ", 1)
                if name in parsed_checksums:
                    raise ValueError(f"checksum repetido para {name}")
                parsed_checksums[name] = checksum
            expected_checksums = {
                str(artifact.get("path", "")): str(artifact.get("sha256", ""))
                for artifact in artifacts
                if isinstance(artifact, dict)
            }
            if parsed_checksums != expected_checksums:
                raise ValueError("conteúdo diverge do manifesto")
            items.append(
                VerificationItem(str(checksum_path), True, "lista de checksums coerente")
            )
        except (OSError, UnicodeError, ValueError) as error:
            items.append(
                VerificationItem(str(checksum_path), False, f"lista de checksums inválida: {error}")
            )
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                items.append(
                    VerificationItem(str(manifest_path), False, "entrada de artefato inválida")
                )
                continue
            relative_text = str(artifact.get("path", ""))
            relative = Path(relative_text)
            artifact_path = (site_directory / relative).resolve()
            if (
                "\\" in relative_text
                or artifact_path.parent != site_directory
                or not artifact_path.is_file()
            ):
                items.append(
                    VerificationItem(str(relative), False, "artefato ausente ou caminho inseguro")
                )
                continue
            expected = str(artifact.get("sha256", ""))
            try:
                actual = sha256_file(artifact_path)
            except OSError as error:
                items.append(
                    VerificationItem(str(artifact_path), False, f"não foi possível ler: {error}")
                )
                continue
            if actual != expected:
                items.append(VerificationItem(str(artifact_path), False, "checksum divergente"))
                continue
            try:
                if artifact.get("kind") == "files":
                    members = verify_tar_zst(artifact_path)
                    message = f"checksum e TAR válidos ({members} entradas)"
                elif artifact.get("kind") == "database":
                    source_bytes = verify_zstd(artifact_path)
                    message = f"checksum e Zstandard válidos ({source_bytes} bytes SQL)"
                else:
                    raise VerificationError("tipo de artefato desconhecido")
                items.append(VerificationItem(str(artifact_path), True, message))
            except VerificationError as error:
                items.append(VerificationItem(str(artifact_path), False, str(error)))
    return VerificationReport(root=root, items=tuple(items))


def verification_as_dict(report: VerificationReport) -> dict[str, Any]:
    return {
        "root": str(report.root),
        "valid": report.valid,
        "items": [asdict(item) for item in report.items],
    }
