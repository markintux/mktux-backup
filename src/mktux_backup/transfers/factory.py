"""Resolve credentials and construct the configured remote source."""

from __future__ import annotations

from mktux_backup.config import LoadedConfig
from mktux_backup.errors import ConfigError
from mktux_backup.models import FtpFiles, FtpsFiles, SftpFiles, SiteConfig
from mktux_backup.transfers.base import FileSource
from mktux_backup.transfers.ftp import FtpSource
from mktux_backup.transfers.sftp import SftpSource


def _required_secret(loaded: LoadedConfig, name: str) -> str:
    value = loaded.secrets.get(name)
    if value is None:
        raise ConfigError(f"variável obrigatória ausente: {name}")
    return value


def create_file_source(loaded: LoadedConfig, site: SiteConfig) -> FileSource:
    config = site.files
    if config is None:
        raise ConfigError(f"site sem configuração de arquivos: {site.id}")
    username = _required_secret(loaded, config.username_env)
    password = loaded.secrets.get(config.password_env) if config.password_env else None
    if isinstance(config, (FtpFiles, FtpsFiles)):
        if password is None:
            raise ConfigError(f"senha ausente para {site.id}")
        return FtpSource(config, username, password)
    if isinstance(config, SftpFiles):
        passphrase = (
            loaded.secrets.get(config.key_passphrase_env) if config.key_passphrase_env else None
        )
        return SftpSource(config, username, password, passphrase, loaded.base_directory)
    raise ConfigError(f"protocolo de arquivos não suportado em {site.id}")
