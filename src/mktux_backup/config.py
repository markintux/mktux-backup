"""Load YAML configuration and secrets without mutating the process environment."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values
from pydantic import ValidationError

from mktux_backup.errors import ConfigError
from mktux_backup.models import AppConfig, SiteConfig


@dataclass(frozen=True)
class SecretStore:
    values: dict[str, str]
    env_path: Path

    def get(self, name: str) -> str | None:
        value = self.values.get(name)
        return value if value else None

    def missing_for(self, site: SiteConfig) -> list[str]:
        return sorted(name for name in site.secret_references() if not self.get(name))

    def redaction_values(self, references: Iterable[str]) -> tuple[str, ...]:
        values = {self.values[name] for name in references if self.values.get(name)}
        return tuple(sorted(values, key=len, reverse=True))


@dataclass(frozen=True)
class LoadedConfig:
    config: AppConfig
    config_path: Path
    secrets: SecretStore

    @property
    def base_directory(self) -> Path:
        return self.config_path.parent

    @property
    def destination(self) -> Path:
        return _resolve_path(self.config.backup.destination, self.base_directory)

    @property
    def state_directory(self) -> Path:
        return _resolve_path(self.config.backup.state_directory, self.base_directory)

    def selected_sites(self, site_ids: list[str] | None = None) -> list[SiteConfig]:
        if not site_ids:
            return self.config.enabled_sites()

        duplicate_ids = sorted({site_id for site_id in site_ids if site_ids.count(site_id) > 1})
        if duplicate_ids:
            raise ConfigError(f"site repetido na linha de comando: {', '.join(duplicate_ids)}")

        selected: list[SiteConfig] = []
        for site_id in site_ids:
            site = self.config.find_site(site_id)
            if site is None:
                raise ConfigError(f"site não encontrado: {site_id}")
            if not site.enabled:
                raise ConfigError(f"site está desabilitado: {site_id}")
            selected.append(site)
        return selected


def _resolve_path(path: Path, base_directory: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = base_directory / expanded
    return expanded.resolve()


def _format_validation_error(error: ValidationError) -> str:
    lines = []
    for item in error.errors(include_url=False):
        location = ".".join(str(part) for part in item["loc"])
        lines.append(f"- {location}: {item['msg']}")
    return "\n".join(lines)


def load_config(config_path: Path, env_path: Path | None = None) -> LoadedConfig:
    config_path = config_path.expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f"arquivo de configuração não encontrado: {config_path}")

    try:
        with config_path.open("r", encoding="utf-8") as handle:
            raw: Any = yaml.safe_load(handle)
    except yaml.MarkedYAMLError as error:
        mark = error.problem_mark
        location = f" linha {mark.line + 1}, coluna {mark.column + 1}" if mark else ""
        raise ConfigError(f"YAML inválido em{location}: {error.problem or error}") from error
    except yaml.YAMLError as error:
        raise ConfigError(f"YAML inválido: {error}") from error
    except OSError as error:
        raise ConfigError(f"não foi possível ler {config_path}: {error}") from error

    if not isinstance(raw, dict):
        raise ConfigError("a configuração YAML precisa ter um objeto na raiz")

    try:
        config = AppConfig.model_validate(raw)
    except ValidationError as error:
        raise ConfigError(f"configuração inválida:\n{_format_validation_error(error)}") from error

    resolved_env_path = (env_path or config_path.with_name(".env")).expanduser().resolve()
    try:
        env_values = dotenv_values(resolved_env_path) if resolved_env_path.is_file() else {}
    except OSError as error:
        raise ConfigError(f"não foi possível ler {resolved_env_path}: {error}") from error

    references = set().union(*(site.secret_references() for site in config.sites))
    merged_values: dict[str, str] = {}
    for name in references:
        value = os.environ.get(name, env_values.get(name))
        if value is not None:
            merged_values[name] = value
    secrets = SecretStore(values=merged_values, env_path=resolved_env_path)
    return LoadedConfig(config=config, config_path=config_path, secrets=secrets)
