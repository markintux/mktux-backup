from pathlib import Path

import pytest

from mktux_backup.config import load_config
from mktux_backup.errors import ConfigError
from mktux_backup.models import FtpFiles, SftpFiles


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "sites.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def valid_yaml(destination: Path) -> str:
    return f"""
version: 1
backup:
  destination: {destination}
sites:
  - id: site-sftp
    files:
      protocol: sftp
      host: ssh.example.com
      username_env: SSH_USER
      password_env: SSH_PASSWORD
      paths:
        - remote: /uploads
          archive_as: uploads
    database:
      enabled: true
      host: db.example.com
      name: app
      username_env: DB_USER
      password_env: DB_PASSWORD
  - id: site-ftp
    files:
      protocol: ftp
      host: ftp.example.com
      username_env: FTP_USER
      password_env: FTP_PASSWORD
      allow_insecure: true
      paths:
        - remote: /storage
          archive_as: storage
    database:
      enabled: false
"""


def test_loads_mixed_sites_and_environment_takes_precedence(tmp_path: Path, monkeypatch) -> None:
    config_path = write_config(tmp_path, valid_yaml(tmp_path / "backups"))
    (tmp_path / ".env").write_text(
        "SSH_USER=file-user\nSSH_PASSWORD=file-password\nDB_USER=db\nDB_PASSWORD=pw\n"
        "FTP_USER=ftp\nFTP_PASSWORD=ftp-pw\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SSH_USER", "process-user")

    loaded = load_config(config_path)

    assert isinstance(loaded.config.sites[0].files, SftpFiles)
    assert isinstance(loaded.config.sites[1].files, FtpFiles)
    assert loaded.secrets.get("SSH_USER") == "process-user"
    assert loaded.destination == (tmp_path / "backups").resolve()
    assert set(loaded.secrets.values) == {
        "SSH_USER",
        "SSH_PASSWORD",
        "DB_USER",
        "DB_PASSWORD",
        "FTP_USER",
        "FTP_PASSWORD",
    }
    assert loaded.config.sites[0].sensitive_references() == {"SSH_PASSWORD", "DB_PASSWORD"}


def test_rejects_plain_ftp_without_explicit_opt_in(tmp_path: Path) -> None:
    body = valid_yaml(tmp_path).replace("      allow_insecure: true\n", "")

    with pytest.raises(ConfigError, match="allow_insecure"):
        load_config(write_config(tmp_path, body))


def test_rejects_duplicate_site_ids(tmp_path: Path) -> None:
    body = valid_yaml(tmp_path).replace("id: site-ftp", "id: site-sftp")

    with pytest.raises(ConfigError, match="id único"):
        load_config(write_config(tmp_path, body))


def test_rejects_unsafe_remote_parent_segment(tmp_path: Path) -> None:
    body = valid_yaml(tmp_path).replace("remote: /uploads", "remote: /uploads/../private")

    with pytest.raises(ConfigError, match="não pode conter"):
        load_config(write_config(tmp_path, body))


def test_rejects_control_characters_in_remote_path(tmp_path: Path) -> None:
    body = valid_yaml(tmp_path).replace("remote: /uploads", 'remote: "/uploads\\nprivate"')

    with pytest.raises(ConfigError, match="caracteres de controle"):
        load_config(write_config(tmp_path, body))


def test_reports_missing_selected_site(tmp_path: Path) -> None:
    loaded = load_config(write_config(tmp_path, valid_yaml(tmp_path)))

    with pytest.raises(ConfigError, match="site não encontrado"):
        loaded.selected_sites(["unknown"])
