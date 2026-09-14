"""Validated configuration models."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator, model_validator

ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
SAFE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_default=True)


class RemotePath(StrictModel):
    remote: str = Field(min_length=1)
    archive_as: str = Field(min_length=1)

    @field_validator("archive_as")
    @classmethod
    def archive_name_is_safe(cls, value: str) -> str:
        if not SAFE_NAME_RE.fullmatch(value):
            raise ValueError("use apenas letras minúsculas, números, ponto, hífen ou underscore")
        if value in {".", ".."}:
            raise ValueError("o nome não pode ser '.' ou '..'")
        return value

    @field_validator("remote")
    @classmethod
    def remote_path_is_safe(cls, value: str) -> str:
        if any(character in value for character in ("\0", "\r", "\n")):
            raise ValueError("o caminho remoto não pode conter caracteres de controle")
        normalized = value.replace("\\", "/")
        if any(part == ".." for part in normalized.split("/")):
            raise ValueError("o caminho remoto não pode conter '..'")
        return value


class FilesBase(StrictModel):
    host: str = Field(min_length=1)
    port: PositiveInt
    timeout_seconds: PositiveInt = 30
    username_env: str
    password_env: str | None = None
    paths: list[RemotePath] = Field(min_length=1)

    @field_validator("username_env", "password_env")
    @classmethod
    def environment_name_is_valid(cls, value: str | None) -> str | None:
        if value is not None and not ENV_NAME_RE.fullmatch(value):
            raise ValueError("use um nome de variável de ambiente em MAIÚSCULAS")
        return value

    @model_validator(mode="after")
    def archive_names_are_unique(self) -> Self:
        names = [path.archive_as for path in self.paths]
        if len(names) != len(set(names)):
            raise ValueError("archive_as deve ser único dentro do site")
        return self


class FtpFiles(FilesBase):
    protocol: Literal["ftp"]
    port: PositiveInt = 21
    allow_insecure: bool = False
    passive: bool = True

    @model_validator(mode="after")
    def insecure_access_is_explicit(self) -> Self:
        if not self.allow_insecure:
            raise ValueError("FTP puro exige allow_insecure: true")
        if not self.password_env:
            raise ValueError("FTP exige password_env")
        return self


class FtpsFiles(FilesBase):
    protocol: Literal["ftps"]
    port: PositiveInt = 21
    passive: bool = True
    verify_certificate: bool = True

    @model_validator(mode="after")
    def password_is_present(self) -> Self:
        if not self.password_env:
            raise ValueError("FTPS exige password_env")
        return self


class SftpFiles(FilesBase):
    protocol: Literal["sftp"]
    port: PositiveInt = 22
    key_file: Path | None = None
    key_passphrase_env: str | None = None
    allow_agent: bool = True
    look_for_keys: bool = True
    known_hosts_file: Path | None = None

    @field_validator("key_passphrase_env")
    @classmethod
    def passphrase_environment_name_is_valid(cls, value: str | None) -> str | None:
        if value is not None and not ENV_NAME_RE.fullmatch(value):
            raise ValueError("use um nome de variável de ambiente em MAIÚSCULAS")
        return value

    @model_validator(mode="after")
    def at_least_one_authentication_method_exists(self) -> Self:
        if not any((self.password_env, self.key_file, self.allow_agent, self.look_for_keys)):
            raise ValueError("SFTP exige senha, chave, agente SSH ou descoberta de chaves")
        if self.key_passphrase_env and not self.key_file:
            raise ValueError("key_passphrase_env só pode ser usado com key_file")
        return self


FilesConfig = Annotated[FtpFiles | FtpsFiles | SftpFiles, Field(discriminator="protocol")]


class DatabaseConfig(StrictModel):
    enabled: bool = False
    host: str | None = None
    port: PositiveInt = 3306
    name: str | None = None
    username_env: str | None = None
    password_env: str | None = None
    tls: Literal["required", "preferred", "disabled"] = "preferred"
    size_hint_mb: PositiveInt | None = None

    @field_validator("username_env", "password_env")
    @classmethod
    def database_environment_name_is_valid(cls, value: str | None) -> str | None:
        if value is not None and not ENV_NAME_RE.fullmatch(value):
            raise ValueError("use um nome de variável de ambiente em MAIÚSCULAS")
        return value

    @model_validator(mode="after")
    def enabled_database_is_complete(self) -> Self:
        if self.enabled:
            missing = [
                field
                for field in ("host", "name", "username_env", "password_env")
                if not getattr(self, field)
            ]
            if missing:
                raise ValueError(f"banco habilitado requer: {', '.join(missing)}")
        return self


class SiteConfig(StrictModel):
    id: str
    enabled: bool = True
    files: FilesConfig | None = None
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)

    @field_validator("id")
    @classmethod
    def site_id_is_safe(cls, value: str) -> str:
        if not SAFE_NAME_RE.fullmatch(value):
            raise ValueError("use apenas letras minúsculas, números, ponto, hífen ou underscore")
        return value

    @model_validator(mode="after")
    def site_has_something_to_back_up(self) -> Self:
        if self.files is None and not self.database.enabled:
            raise ValueError("o site precisa habilitar arquivos, banco ou ambos")
        return self

    def secret_references(self) -> set[str]:
        if not self.enabled:
            return set()
        references: set[str] = set()
        if self.files:
            references.add(self.files.username_env)
            if self.files.password_env:
                references.add(self.files.password_env)
            if isinstance(self.files, SftpFiles) and self.files.key_passphrase_env:
                references.add(self.files.key_passphrase_env)
        if self.database.enabled:
            assert self.database.username_env is not None
            assert self.database.password_env is not None
            references.update((self.database.username_env, self.database.password_env))
        return references

    def sensitive_references(self) -> set[str]:
        if not self.enabled:
            return set()
        references: set[str] = set()
        if self.files:
            if self.files.password_env:
                references.add(self.files.password_env)
            if isinstance(self.files, SftpFiles) and self.files.key_passphrase_env:
                references.add(self.files.key_passphrase_env)
        if self.database.enabled:
            assert self.database.password_env is not None
            references.add(self.database.password_env)
        return references


class BackupConfig(StrictModel):
    destination: Path
    state_directory: Path = Path(".mktux-backup")
    concurrency: int = Field(default=2, ge=1, le=16)
    compression_level: int = Field(default=1, ge=-5, le=22)
    free_space_margin_percent: int = Field(default=20, ge=0, le=100)


class AppConfig(StrictModel):
    version: Literal[1]
    backup: BackupConfig
    sites: list[SiteConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def site_ids_are_unique(self) -> Self:
        ids = [site.id for site in self.sites]
        if len(ids) != len(set(ids)):
            raise ValueError("cada site precisa ter um id único")
        return self

    def enabled_sites(self) -> list[SiteConfig]:
        return [site for site in self.sites if site.enabled]

    def find_site(self, site_id: str) -> SiteConfig | None:
        return next((site for site in self.sites if site.id == site_id), None)
