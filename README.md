# mktux-backup

**English** | [Português (Brasil)](README.pt-BR.md)

`mktux-backup` is a cross-platform Python CLI for creating local, auditable
backups of selected directories and MySQL databases hosted across multiple
providers.

It is designed for environments where some accounts provide SFTP access, other
accounts only provide FTP or FTPS, and the remote shell may be restricted or
completely unavailable. Nothing is installed on the hosting servers: files are
read through the configured transfer protocol, while MySQL is accessed through
the provider's external database endpoint.

## Main features

- Multiple sites in a single configuration file.
- Multiple selected remote directories per site.
- SFTP without requiring a usable remote shell.
- FTP with an explicit insecure-protocol opt-in.
- Explicit FTPS with certificate verification enabled by default.
- FTP and FTPS transfer recovery with bounded retries, byte-offset resume, and
  periodic control-connection renewal.
- Zero or one MySQL database per site.
- File-only, database-only, or combined backups.
- Concurrent site processing with configurable limits.
- Streaming TAR and Zstandard compression with no uncompressed local copy.
- `mysqldump` streaming directly into Zstandard compression.
- SHA-256 checksums and JSON manifests.
- A preflight that checks local storage, credentials, connections, remote file
  inventories, MySQL metadata, and estimated free-space requirements.
- An interactive confirmation before any backup starts.
- A live terminal dashboard and a separate `watch` command.
- Failure isolation: one unavailable provider does not block ready sites.
- Cross-platform execution lock and atomic finalization.
- No automatic deletion or retention policy.

## Scope and non-goals

The current version creates and verifies backups. It intentionally does not:

- restore files or databases;
- upload backups to cloud storage;
- encrypt backup files at rest;
- delete old completed or partial backups;
- follow remote symbolic links;
- install software or execute shell commands on the hosting server.

Because local snapshots are not encrypted, the destination disk and any
external drive containing them must be protected appropriately.

## How it works

### 1. Configuration and secret loading

The application reads site definitions from `sites.yaml`. Passwords and other
secrets are referenced by environment-variable name and loaded from `.env` or
from the process environment. A value already present in the process
environment overrides the same value in `.env`.

Secrets are never expected directly inside the YAML file. Known secret values
are redacted before state, event, or failure information is persisted.

### 2. Preflight

Both `check` and `run` execute a preflight before any backup is written. It:

1. validates the complete YAML schema;
2. checks that the state directory is writable;
3. requires the destination directory to already exist and be writable;
4. checks whether another run owns the execution lock;
5. reports old `_partial` directories without removing them;
6. checks `.env` permissions on POSIX systems;
7. locates `mysqldump` or `mariadb-dump` when a database is enabled;
8. inventories every configured remote directory;
9. inspects each enabled MySQL database;
10. estimates the source volume and compares it with free local space plus the
    configured safety margin.

Independent remote checks run concurrently. If a local global check already
fails—for example, the destination does not exist—the remote connections are
skipped.

### 3. Confirmation and locking

`run` displays the complete preflight summary and asks for confirmation. After
confirmation, it acquires an operating-system-level lock so that only one
backup run can write at a time.

Use `--yes` to bypass the question in a trusted non-interactive job. Without a
TTY, `--yes` is required.

### 4. Per-site backup

Ready sites are processed by a thread pool. File and database work inside one
site is sequential, which limits pressure on a small hosting account:

1. the application opens a fresh FTP, FTPS, or SFTP connection;
2. each selected directory is added to a streaming TAR archive under its
   configured `archive_as` name;
3. TAR output is compressed directly to `files.tar.zst`;
4. the application rejects a file if its size changes during transfer;
5. when enabled, `mysqldump` output is streamed directly to
   `database.sql.zst`;
6. the site manifest and checksum list are written after its artifacts finish.

For FTP and FTPS, binary mode is reaffirmed before every file and the control
connection is renewed after every 100 completed files. If a data transfer is
interrupted, the client reconnects and resumes from the last received byte with
FTP `REST`, for up to three retries after the initial attempt. A server that
cannot resume the file, or a transfer that exhausts those retries, fails the
site instead of accepting a truncated artifact.

The database command uses options including `--single-transaction`, `--quick`,
`--skip-lock-tables`, routines, events, triggers, and binary-safe hexadecimal
output. Supported compatibility flags are detected from the locally installed
dump client.

### 5. Failure isolation

If one site fails during preflight, ready sites can still run. The skipped site
is recorded in the run manifest and the final exit code is `2`.

If a site fails during transfer, the other site workers continue. Any locally
useful data from the failed site is moved under `_failed/<site-id>` and is not
treated as a valid backup by `verify`.

If the complete run crashes or is cancelled, its run directory remains under
`_partial`. The application never deletes old snapshots or old partial runs.

### 6. Finalization

All work starts in:

```text
<destination>/_partial/<run-id>/
```

After the site workers finish, the application writes the run manifest and
event log, then atomically renames the directory to:

```text
<destination>/<run-id>/
```

An existing final directory is never overwritten.

## Requirements

- Python 3.11 or newer.
- Network access from the backup machine to the configured file services.
- External MySQL access allowed for the backup machine's public IP.
- `mysqldump` or `mariadb-dump` in `PATH` for database backups.
- Enough local space for the estimated source data plus the configured margin.

The dump client should ideally be compatible with the remote MySQL or MariaDB
server version.

## Installation

Clone the repository and enter its directory:

```bash
git clone https://github.com/markintux/mktux-backup.git
cd mktux-backup
```

### Using uv

```bash
uv sync
uv run mktux-backup --help
```

For development dependencies:

```bash
uv sync --extra dev
```

When using `uv`, prefix the examples in this guide with `uv run`, such as
`uv run mktux-backup check`.

### Using venv and pip on macOS or Linux

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
mktux-backup --help
```

### Using venv and pip on Windows PowerShell

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
mktux-backup --help
```

The CLI can also be invoked as a module:

```bash
python -m mktux_backup --help
```

## Installing a MySQL dump client

Database-only functionality is optional. Sites without a database do not need
this executable.

### macOS with Homebrew

```bash
brew install mysql-client
export PATH="$(brew --prefix mysql-client)/bin:$PATH"
mysqldump --version
```

Add the exported path to your shell configuration if it must persist across
terminal sessions or scheduled jobs.

### Linux

Install the MySQL or MariaDB client package provided by the distribution, then
verify one of these commands:

```bash
mysqldump --version
mariadb-dump --version
```

### Windows

Install the MySQL command-line tools and add the directory containing
`mysqldump.exe` to the system or task-specific `PATH`.

## Quick start

On macOS or Linux:

```bash
cp sites.example.yaml sites.yaml
cp .env.example .env
chmod 600 .env
mkdir -p /path/to/backups
```

On Windows PowerShell:

```powershell
Copy-Item sites.example.yaml sites.yaml
Copy-Item .env.example .env
New-Item -ItemType Directory -Force C:\Backups\Sites
```

Then:

1. set the destination and sites in `sites.yaml`;
2. fill the referenced credentials in `.env`;
3. register and verify SFTP host keys;
4. authorize the backup machine's IP in each MySQL hosting panel;
5. run `mktux-backup check`;
6. resolve every unexpected error or warning;
7. run `mktux-backup run` and confirm;
8. run `mktux-backup verify <completed-run-directory>`;
9. copy the verified run directory to the external drive.

`sites.yaml`, `.env`, `.mktux-backup`, `.harness`, real snapshots under
`backups/sites`, and temporary restore directories under `restores` are
excluded from Git by the repository's `.gitignore` rules. Only
`backups/sites/.gitkeep` preserves the local snapshot directory structure.

## Configuration files

The default paths are:

- configuration: `sites.yaml` in the current directory;
- secrets: `.env` next to the selected YAML file;
- state: `.mktux-backup` relative to the YAML file;
- backup destination: the value of `backup.destination`.

Global CLI options must appear before the command:

```bash
mktux-backup --config /path/to/sites.yaml --env-file /path/to/.env check
```

Relative values for `destination`, `state_directory`, `key_file`, and
`known_hosts_file` are resolved relative to the directory containing the YAML
file, not necessarily the current working directory.

## Complete configuration example

See [sites.example.yaml](sites.example.yaml) for a ready-to-copy file. The
following example shows SFTP with MySQL, plain FTP, and explicit FTPS:

```yaml
version: 1

backup:
  destination: "/Volumes/Backups/sites"
  state_directory: ".mktux-backup"
  concurrency: 2
  compression_level: 1
  free_space_margin_percent: 20

sites:
  - id: "customer-sftp"
    enabled: true
    files:
      protocol: "sftp"
      host: "server.example.com"
      port: 22
      timeout_seconds: 30
      username_env: "CUSTOMER_SSH_USER"
      password_env: "CUSTOMER_SSH_PASSWORD"
      key_file: null
      key_passphrase_env: null
      allow_agent: true
      look_for_keys: true
      known_hosts_file: null
      paths:
        - remote: "/home/account/app/storage/app"
          archive_as: "storage-app"
        - remote: "/home/account/app/public/uploads"
          archive_as: "uploads"
    database:
      enabled: true
      host: "mysql.example.com"
      port: 3306
      name: "customer_database"
      username_env: "CUSTOMER_DB_USER"
      password_env: "CUSTOMER_DB_PASSWORD"
      tls: "preferred"

  - id: "customer-ftp"
    enabled: true
    files:
      protocol: "ftp"
      host: "ftp.example.com"
      port: 21
      timeout_seconds: 30
      username_env: "CUSTOMER_FTP_USER"
      password_env: "CUSTOMER_FTP_PASSWORD"
      passive: true
      allow_insecure: true
      paths:
        - remote: "/public_html/uploads"
          archive_as: "uploads"
    database:
      enabled: false

  - id: "customer-ftps"
    enabled: false
    files:
      protocol: "ftps"
      host: "secure-ftp.example.com"
      port: 21
      timeout_seconds: 30
      username_env: "CUSTOMER_FTPS_USER"
      password_env: "CUSTOMER_FTPS_PASSWORD"
      passive: true
      verify_certificate: true
      paths:
        - remote: "/public_html/uploads"
          archive_as: "uploads"
    database:
      enabled: false
```

The matching `.env` file would contain:

```dotenv
CUSTOMER_SSH_USER=account-name
CUSTOMER_SSH_PASSWORD=replace-me
CUSTOMER_DB_USER=database-user
CUSTOMER_DB_PASSWORD=replace-me

CUSTOMER_FTP_USER=ftp-user
CUSTOMER_FTP_PASSWORD=replace-me

CUSTOMER_FTPS_USER=ftps-user
CUSTOMER_FTPS_PASSWORD=replace-me
```

Do not commit the real `.env` file.

## Global backup settings

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `destination` | yes | none | Existing directory that receives all run snapshots. |
| `state_directory` | no | `.mktux-backup` | Atomic state, lock, and event-log directory. |
| `concurrency` | no | `2` | Number of sites processed at once, from 1 to 16. |
| `compression_level` | no | `1` | Zstandard level from -5 to 22. Lower values favor speed. |
| `free_space_margin_percent` | no | `20` | Extra free-space margin applied to the preflight estimate. |

The destination is global. Every selected site in one run is stored under the
same dated run directory.

## Site settings

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `id` | yes | none | Unique safe identifier used as the output directory name. |
| `enabled` | no | `true` | Disabled sites are ignored unless later enabled. |
| `files` | conditional | none | FTP, FTPS, or SFTP configuration. May be omitted for database-only sites. |
| `database` | no | disabled | Optional single MySQL database configuration. |

A site must enable files, a database, or both.

Every path contains:

- `remote`: the source directory as seen by the hosting account;
- `archive_as`: a unique safe top-level name inside `files.tar.zst`.

Only selected data directories need to be listed. Application source code that
already exists in Git does not need to be backed up.

## SFTP configuration

SFTP supports password authentication, an explicit private key, the local SSH
agent, and normal key discovery.

| Field | Default | Description |
| --- | --- | --- |
| `port` | `22` | SFTP/SSH port. |
| `timeout_seconds` | `30` | Connection and channel timeout. |
| `username_env` | required | Environment variable containing the username. |
| `password_env` | none | Environment variable containing the optional password. |
| `key_file` | none | Private-key path, relative to the YAML file or absolute. |
| `key_passphrase_env` | none | Environment variable containing the key passphrase. |
| `allow_agent` | `true` | Allows credentials from the SSH agent. |
| `look_for_keys` | `true` | Searches normal local SSH key locations. |
| `known_hosts_file` | system default | Optional explicit known-hosts file. |

Unknown host keys are always rejected. The tool never automatically trusts a
new host. Verify the fingerprint with the provider and register it first:

```bash
ssh -p 22 account-name@server.example.com
```

A restricted shell may close the interactive session after authentication;
SFTP can still work because the backup does not execute remote shell commands.

## FTP configuration

Plain FTP transmits usernames, passwords, and data without encryption. It is
accepted only when the risk is explicitly acknowledged:

```yaml
protocol: "ftp"
allow_insecure: true
passive: true
```

`password_env` is mandatory for FTP. Passive mode is enabled by default and is
usually required behind local routers and hosting firewalls.

Use FTP only when the provider offers no encrypted alternative.

## FTPS configuration

The `ftps` protocol uses explicit TLS: it connects to the FTP port and upgrades
the control and data connections.

```yaml
protocol: "ftps"
verify_certificate: true
passive: true
```

Certificate verification is enabled by default. Disabling it allows the run but
produces a preflight warning.

Implicit FTPS on port 990 is not implemented.

## MySQL configuration

| Field | Required when enabled | Default | Description |
| --- | --- | --- | --- |
| `enabled` | yes | `false` | Enables the site's database backup. |
| `host` | yes | none | Externally reachable MySQL hostname. |
| `port` | no | `3306` | MySQL port. |
| `name` | yes | none | The single database to dump. |
| `username_env` | yes | none | Environment variable containing the database user. |
| `password_env` | yes | none | Environment variable containing the password. |
| `tls` | no | `preferred` | `required`, `preferred`, or `disabled`. |
| `size_hint_mb` | no | none | Fallback estimate when metadata reports zero bytes. |

TLS modes behave as follows:

- `required`: the preflight fails unless TLS is established;
- `preferred`: TLS is attempted first, with a plaintext fallback and warning;
- `disabled`: the connection is intentionally plaintext.

The preflight queries the server version, table engines, approximate data and
index size, and the active TLS cipher. Non-transactional engines generate a
warning because `--single-transaction` cannot guarantee a consistent snapshot
for them.

The database password is written to a temporary client option file, not passed
on the command line. On POSIX systems this file is set to mode `0600` and is
removed when the dump process ends.

## Command reference

### Show general help

```bash
mktux-backup --help
mktux-backup --version
```

### List configured sites

```bash
mktux-backup list
```

This command validates and displays configuration metadata without printing
secret values or opening remote connections.

### Run the preflight

```bash
mktux-backup check
mktux-backup check --site customer-sftp
mktux-backup check --site customer-sftp --site customer-ftp
```

`--site ID` can be repeated. A missing, disabled, or repeated ID is rejected.
`check` does not create a backup. It returns a failure status when any selected
site is invalid.

### Create a backup

```bash
mktux-backup run
mktux-backup run --site customer-sftp
mktux-backup run --concurrency 3
mktux-backup run --yes --no-dashboard
```

Options:

- `--site ID`: selects one site and can be repeated;
- `--yes`: bypasses confirmation and is required without an interactive stdin;
- `--no-dashboard`: uses simple linear output;
- `--concurrency N`: overrides the configured site concurrency for this run.

In the built-in dashboard:

- press `q` to hide the dashboard without stopping the backup;
- press `Ctrl+C` to request a safe cancellation.

Cancellation is cooperative. A network operation may take up to its configured
timeout to return. A stalled `mysqldump` is monitored and terminated after a
cancellation request.

### Watch from another terminal

```bash
mktux-backup watch
mktux-backup watch --once
mktux-backup watch --interval 1.0
```

`watch` reads the atomic `current.json` state. Pressing `q` exits only the
monitor and never cancels the backup process. Non-interactive use requires
`--once`.

### Verify a completed run

```bash
mktux-backup verify /path/to/backups/20260913T220000-0300-a1b2c3
mktux-backup verify /path/to/completed-run --json
```

Verification checks:

- the run manifest is readable;
- every successful site has a site manifest;
- `checksums.sha256` agrees with the site manifest;
- each artifact's computed SHA-256 matches;
- file archives can be decompressed and parsed as TAR;
- TAR member paths are safe;
- database archives can be fully decompressed and contain data.

This protects against accidental corruption. It is not a digital signature and
does not prove authenticity if an attacker can modify both data and manifests.

## Backup directory layout

```text
/Volumes/Backups/sites/
├── _partial/
│   └── <cancelled-or-crashed-run-id>/
└── 20260913T220000-0300-a1b2c3/
    ├── run-manifest.json
    ├── run.log
    ├── _failed/                       # only when a runtime site failure occurs
    │   └── failed-site/
    ├── customer-ftp/
    │   ├── files.tar.zst
    │   ├── manifest.json
    │   └── checksums.sha256
    └── customer-sftp/
        ├── files.tar.zst
        ├── database.sql.zst
        ├── manifest.json
        └── checksums.sha256
```

`run.log` contains newline-delimited JSON events. The run and site manifests
record timestamps, protocols, selected remote paths, source sizes, checksums,
warnings, MySQL metadata, successful sites, and failed sites.

The state directory has a separate operational layout:

```text
.mktux-backup/
├── current.json
├── run.lock
└── runs/
    └── <run-id>/
        └── events.jsonl
```

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | The command completed successfully. |
| `1` | Configuration, preflight, execution, or verification failed. |
| `2` | A run completed, but at least one selected site failed or was skipped. |
| `4` | The operation was cancelled by the user. |

Scheduled jobs should treat only `0` as complete success. Code `2` means that a
valid partial run may exist, but the failed-site details require attention.

## Recommended monthly workflow

1. Connect or mount the local/external destination disk.
2. Confirm that the destination configured in `sites.yaml` exists.
3. Run `mktux-backup check`.
4. Review FTP, TLS, engine, size, and partial-run warnings.
5. Run `mktux-backup run` and confirm the summary.
6. Check that the exit code is `0`.
7. Run `mktux-backup verify <new-run-directory>`.
8. Copy or synchronize the completed run directory to the external disk.
9. Keep the manifests and `run.log` with the artifacts.
10. Review `_partial` and `_failed` manually; the application will never remove
    them automatically.

## Monthly scheduling

Always use absolute paths in scheduled jobs. Ensure the external destination is
mounted and that the job's `PATH` includes `mysqldump`.

Example cron entry for 02:00 on the first day of every month:

```cron
0 2 1 * * cd /absolute/path/mktux-backup && /absolute/path/mktux-backup/.venv/bin/mktux-backup --config /absolute/path/mktux-backup/sites.yaml run --yes --no-dashboard >> /absolute/path/mktux-backup/backup.log 2>&1
```

On macOS, a LaunchAgent or LaunchDaemon is generally more reliable than cron
when the machine may be asleep. On Windows, create a Task Scheduler job whose
program is the absolute path to `.venv\Scripts\mktux-backup.exe` and whose
arguments are:

```text
--config C:\absolute\path\sites.yaml run --yes --no-dashboard
```

Automation bypasses the interactive confirmation, so run the same configuration
manually with `check` first.

## Performance tuning

The default settings favor low resource usage on small hosting accounts:

```yaml
concurrency: 2
compression_level: 1
```

Guidelines:

- Increase `concurrency` only when sites are hosted by independent providers and
  the local network/disk can handle more simultaneous streams.
- Reduce concurrency to `1` if the backup machine, disk, or network is limited.
- Compression level `1` is a good speed-oriented default.
- Negative Zstandard levels can be faster but usually produce larger files.
- High compression levels consume considerably more CPU and can reduce overall
  throughput.
- Work inside one site is intentionally sequential to avoid hitting a weak
  account with file transfer and database export at the same time.
- The preflight performs a full directory inventory, so accounts with very many
  tiny files can spend noticeable time in `check` before transfer begins.

## Security notes

- Never commit `.env`.
- On macOS and Linux, use `chmod 600 .env`.
- Protect the backup destination because artifacts are not encrypted at rest.
- Prefer SFTP or FTPS over FTP.
- Keep `verify_certificate: true` for FTPS whenever possible.
- Use `tls: required` for MySQL when the provider supports a valid TLS setup.
- Verify SFTP host fingerprints through an independent trusted channel.
- Give backup credentials only the permissions necessary to read selected files
  and dump the selected database.
- Do not expose `.mktux-backup/current.json` or event logs through a web server.

## Troubleshooting

### `mysqldump` was not found

Run:

```bash
mysqldump --version
mariadb-dump --version
```

If both fail, install a client or fix `PATH`. Scheduled jobs often receive a
smaller `PATH` than an interactive terminal.

### SFTP reports an unknown host key

This is intentional. Verify the fingerprint with the provider, connect once
using an SSH client, or provide a verified `known_hosts_file`. The backup tool
does not offer an insecure auto-accept mode.

### The account has SFTP but no shell

That is supported. The application uses only the SFTP subsystem and never runs
`tar`, `find`, or another command remotely.

### FTP works in a desktop client but not in the backup

Confirm the host, port, root-relative path, passive-mode requirement, and whether
the provider actually expects FTPS. Use `protocol: ftp` only for plain FTP and
remember that it requires `allow_insecure: true`.

### MySQL works in the hosting panel but not externally

Verify that:

- remote MySQL access is enabled;
- the backup machine's current public IP is authorized;
- the external hostname and port are correct;
- the database user is allowed to connect from that IP;
- local or provider firewalls allow the connection;
- the configured TLS mode matches the provider.

### The preflight reports non-transactional engines

The backup can continue, but tables using engines such as MyISAM can change
during a no-lock dump. Run the backup during a low-traffic period or migrate the
tables to InnoDB if consistency is critical.

### Available space is unknown

The preflight needs complete file and database inventories to calculate the
estimate. Fix the failed remote checks or set `size_hint_mb` when an otherwise
valid database reports no size.

### An old `_partial` directory is reported

It belongs to a cancelled or crashed run. Inspect its state and files manually.
The application reports but never deletes it.

### A run returned code `2`

Open `run-manifest.json`, inspect `failed_sites`, and check `_failed/<site-id>`
when present. Successful site directories can still be verified normally.

## Development and validation

Install development dependencies and run:

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest
uv build
```

The test suite covers configuration validation, secret redaction, locks,
staging, FTP/SFTP adapters, streaming archives, MySQL inspection and dumping,
cancellation, failure isolation, manifests, verification, the dashboard, and
CLI behavior.

The GitHub Actions workflow runs lint, tests, and package builds on Linux,
macOS, and Windows, including the minimum supported Python version.

## License

This project is distributed under the terms in [LICENSE](LICENSE).
