# mktux-backup

[English](README.md) | **Português (Brasil)**

CLI Python para criar snapshots locais e auditáveis de diretórios remotos e
bancos MySQL distribuídos entre diferentes hospedagens. Os sites são processados
em paralelo; uma falha fica isolada ao site afetado.

O projeto não instala nem executa scripts no servidor remoto. Arquivos são lidos
por SFTP, FTP ou FTPS, e o MySQL é acessado externamente a partir da máquina que
executa a CLI.

## O que está incluído

- vários sites e vários diretórios por site;
- SFTP com validação estrita da chave do host;
- FTP passivo, aceito apenas com autorização explícita;
- FTPS explícito com verificação de certificado por padrão;
- um banco MySQL opcional por site;
- `mysqldump` sem lock de tabelas e com snapshot transacional para InnoDB;
- compactação em fluxo, sem cópia descompactada intermediária;
- arquivos em `tar.zst` e bancos em `sql.zst`;
- checksums SHA-256, manifestos JSON e verificação posterior;
- preflight remoto, estimativa de espaço, confirmação e lock de execução;
- painel no terminal e acompanhamento em outro terminal com `watch`;
- nenhuma retenção automática e nenhuma restauração nesta versão.

## Requisitos

- Python 3.11 ou superior;
- `mysqldump` ou `mariadb-dump` no `PATH` quando houver banco habilitado;
- acesso externo ao MySQL liberado para o IP da máquina de backup;
- credenciais SFTP, FTP ou FTPS para os diretórios selecionados.

No macOS com Homebrew, o cliente pode ser instalado com:

```bash
brew install mysql-client
export PATH="$(brew --prefix mysql-client)/bin:$PATH"
mysqldump --version
```

No Linux, instale o cliente MySQL/MariaDB da distribuição. No Windows, instale
as ferramentas de linha de comando do MySQL e adicione o diretório que contém
`mysqldump.exe` ao `PATH`.

## Instalação

Com `uv`:

```bash
uv sync --extra dev
uv run mktux-backup --help
```

Com `venv` e `pip`:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
mktux-backup --help
```

No PowerShell, ative o ambiente com:

```powershell
.venv\Scripts\Activate.ps1
```

## Configuração inicial

```bash
cp sites.example.yaml sites.yaml
cp .env.example .env
chmod 600 .env
```

Edite o YAML com hosts e caminhos. Coloque os valores secretos somente no
`.env`; os campos terminados em `_env` recebem o nome da variável, nunca a senha
em texto puro. Variáveis já definidas no processo têm prioridade sobre o `.env`.

O destino é global e precisa existir antes do preflight. Caminhos relativos de
`destination`, `state_directory`, `key_file` e `known_hosts_file` são resolvidos
a partir do diretório do `sites.yaml`.

### SFTP

O SFTP usa senha, chave, agente SSH ou descoberta das chaves locais. Ele não
depende de shell remoto. Hosts desconhecidos são recusados; confirme antes a
impressão digital com o provedor e registre o host no `known_hosts`, por exemplo
fazendo uma primeira conexão com o cliente `ssh`:

```bash
ssh -p 22 usuario@servidor.example.com
```

Mesmo que a hospedagem encerre a sessão por oferecer shell restrito, a chave
pode ser registrada depois da confirmação. Use `known_hosts_file` para apontar
para um arquivo diferente do padrão do sistema.

### FTP e FTPS

FTP envia usuário, senha e dados sem criptografia. Por isso a configuração só é
aceita quando contém:

```yaml
protocol: ftp
allow_insecure: true
```

Quando a hospedagem oferecer FTPS explícito, prefira `protocol: ftps`. A
verificação do certificado vem ligada; desligá-la gera aviso no preflight.

### MySQL

`tls` aceita `required`, `preferred` ou `disabled`. Use `required` quando o
provedor suportar TLS. O preflight consulta versão, tamanho aproximado e engines.
Tabelas que não sejam InnoDB geram aviso porque `--single-transaction` não
garante um snapshot consistente para engines não transacionais.

O dump usa um arquivo de opções temporário com permissão restrita, de forma que
a senha não aparece na lista de processos. São usados `--single-transaction`,
`--quick` e `--skip-lock-tables` para reduzir impacto no servidor.

## Uso mensal

Primeiro valide tudo sem criar backup:

```bash
mktux-backup check
```

O `check` conecta aos serviços, inventaria os diretórios, testa o banco, estima
o volume e compara com o espaço livre mais uma margem configurável.

Depois execute:

```bash
mktux-backup run
```

A CLI mostra o resumo e pede confirmação. Para automação não interativa:

```bash
mktux-backup run --yes --no-dashboard
```

Para processar somente alguns sites ou ajustar o paralelismo:

```bash
mktux-backup run --site site-sftp --site site-ftp --concurrency 2
```

Em outro terminal, acompanhe a mesma execução:

```bash
mktux-backup watch
mktux-backup watch --once
```

No painel de `run`, `q` apenas oculta a interface; o processo continua. No
`watch`, `q` fecha somente o monitor. `Ctrl+C` no processo de `run` solicita
cancelamento e preserva o staging daquela execução para inspeção manual.

Para usar arquivos de configuração em outro local, as opções globais vêm antes
do comando:

```bash
mktux-backup --config config/sites.yaml --env-file config/.env check
```

Se apenas alguns sites passarem no preflight, `run` pode processá-los. Os sites
reprovados são registrados no manifesto como falhas e a execução retorna código
`2`, evitando que um provedor indisponível bloqueie os demais backups.

## Saída e verificação

Cada execução final recebe um identificador baseado em data e hora:

```text
/Volumes/Backups/sites/
└── 20260913T220000-0300-a1b2c3/
    ├── run-manifest.json
    ├── run.log
    ├── _failed/                 # aparece somente se algum site falhar
    ├── site-ftp/
    │   ├── files.tar.zst
    │   ├── manifest.json
    │   └── checksums.sha256
    └── site-sftp/
        ├── files.tar.zst
        ├── database.sql.zst
        ├── manifest.json
        └── checksums.sha256
```

Verifique checksums e a estrutura dos fluxos compactados sem restaurá-los:

```bash
mktux-backup verify /Volumes/Backups/sites/20260913T220000-0300-a1b2c3
mktux-backup verify /caminho/do/backup --json
```

Snapshots concluídos nunca são sobrescritos ou apagados pela aplicação. Pastas
em `_partial`, inclusive da execução atual quando ela falha ou é cancelada,
também são preservadas e só devem ser removidas manualmente depois da revisão.
Dados aproveitáveis de um site que falhou ficam em `_failed/<site>` e não são
considerados válidos pelo manifesto nem pelo comando `verify`.

## Códigos de saída

- `0`: sucesso;
- `1`: configuração, preflight, verificação ou execução falhou;
- `2`: execução concluída, mas um ou mais sites falharam;
- `4`: execução cancelada pelo usuário.

## Limites desta versão

- não restaura arquivos nem bancos;
- não envia para nuvem;
- não criptografa os snapshots locais;
- não remove backups antigos;
- não segue links simbólicos remotos.

O diretório de destino deve ser copiado para o HD externo somente depois de o
comando terminar e, idealmente, depois de `verify` retornar código `0`.

## Desenvolvimento

```bash
uv run ruff check .
uv run pytest
uv build
```

A CI executa lint, testes e build em Linux, macOS e Windows.
