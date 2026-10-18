# CodePulse

**A local-first code intelligence engine for developers and AI agents.**

CodePulse indexes a repository once, then answers questions about it through one shared core, reachable from a CLI, a REST API, or an MCP server:

```
Index → Retrieve → Explain (structure) → Navigate → Decide (impact) → Act
```

- **Hybrid retrieval.** Keyword (BM25 via Tantivy), semantic (sentence-transformers embeddings in sqlite-vec) and structural (call graph) signals are fused into one ranking.
- **Agent-native.** Responses are structured and token-efficient, and they say explicitly when results were truncated or omitted.
- **Local-first.** Everything runs on your machine: SQLite, embedded indexes, a local embedding model. No external service or API key is needed to index and search.

Python and JavaScript/TypeScript sources are parsed (tree-sitter) into symbols, a call graph, and inheritance edges.

## Install

Requires **Python 3.11 or 3.12** (3.13+ is not supported yet).

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

This installs a `codepulse` command (`python -m codepulse.cli` works too). The first run downloads the `all-MiniLM-L6-v2` embedding model from Hugging Face (about 90 MB) and caches it. After that CodePulse works offline.

## Quick start

```bash
codepulse init .                                 # writes codepulse.toml here
codepulse index path/to/repo                     # full index
codepulse search path/to/repo -q "parse config"  # hybrid search (also: -t keyword|semantic)
codepulse symbol path/to/repo load_config        # definition lookup
codepulse callers path/to/repo load_config       # who calls it (direct + transitive)
codepulse impact path/to/repo load_config        # blast radius if it changes
codepulse status path/to/repo                    # index freshness
codepulse doctor                                 # environment diagnostics
```

Every query command accepts `--json`. Run `codepulse --help` for the full list: `parse`, `search`, `impact`, `init`, `index`, `update`, `symbol`, `refs`, `callers`, `callees`, `similar`, `status`, `modules`, `doctor`, `serve`.

### Where the database lives

The default database is `sqlite:///codepulse.db`, **relative to the directory you run the command from**. A `codepulse.toml` is picked up only from that same directory. Run commands from the same place each time, or pin the location with `--db-url`, `CODEPULSE_DB_URL`, or `db_url` in `codepulse.toml`. When a command is about to create a new database, it says so on stderr.

### Incremental indexing

`codepulse update <repo>` re-indexes using `git diff` when the repository has git history and a prior snapshot exists. Otherwise it falls back to a full index. The call graph is held in memory, so the incremental path only applies inside a long-lived process such as `codepulse serve`. A one-shot CLI run always does a full index and reports `incremental: false`.

## REST API

```bash
codepulse serve                    # http://127.0.0.1:8000
```

Interactive docs are at `/docs`. Health checks are `/health/live` and `/health/ready`; `/metrics` exposes Prometheus metrics. All other endpoints need an `X-API-Key` header.

Keys are scoped to repositories and carry a role: `viewer` < `indexer` < `admin`. Creating a key needs an admin key, so mint the first one locally (run from the directory that holds your `codepulse.db`):

```bash
python -c "
from codepulse.storage.db import StorageManager
from codepulse.auth import create_api_key
key = create_api_key(StorageManager(db_url='sqlite:///codepulse.db'), 'first-admin', repository_ids=[1], role='admin')
print(key.raw_key)"
```

The raw key is shown once and only its hash is stored. `repository_ids` must reference an existing repository, so index something first. Further keys come from `POST /v1/admin/keys`. A key can index a new repository only if it already holds the `indexer` role (or higher) on some existing repository; the key that creates a repository becomes its `admin`, so it can follow the indexing job and see the result.

Main endpoints: `POST /v1/search`, `POST /v1/repositories/index`, `GET /v1/repositories/{id}/index-status`, `GET /v1/symbols/{id}`, `POST /v1/symbols/{id}/impact-analysis`, `POST /v1/symbols/{id}/similar`, `GET /v1/symbols/{id}/references`, `POST /v1/agent/context`. Errors always use one shape: `{"error": {"code", "message", "details"}}`. Requests are rate-limited per key (5 req/s, burst 20, configurable).

A local repository is identified by its canonical path (absolute, symlinks and `..` resolved, true case and an uppercase drive letter on Windows, no trailing separator), so `C:\\proj`, `c:/proj/` and `C:\\proj\\sub\\..` are one repository, not three.

Local-path ingestion can be restricted to an allow-list with `ingestion_allowed_roots` (or `CODEPULSE_INGESTION_ALLOWED_ROOTS`). Cloning from git URLs is not implemented: the URL is validated (https/ssh, SSRF check) and the request is then rejected with 501.

## MCP server

```bash
export CODEPULSE_MCP_API_KEY=cpk_...   # or pass --api-key
codepulse serve --mcp                  # stdio transport
```

The connection is scoped to the repositories the key can access. The MCP tools call the same functions as the CLI and REST API; there is no separate implementation.

## VS Code extension

A separate extension (CodeLens, hovers, impact tree, MCP registration) lives in its own repository, `codepulse-vscode`. It talks to a running `codepulse serve` using a scoped API key.

## Configuration

Precedence: CLI flags > `CODEPULSE_*` environment variables > `codepulse.toml` > built-in defaults. Common settings are `db_url`, `embedding_model`, `embedding_device`, `ingestion_allowed_roots`, and the `rate_limit_*` options. See `codepulse/config.py` for the full list.

## Upgrading an existing database

After updating CodePulse, bring the database up to date with one command:

```bash
codepulse migrate            # apply (backs the SQLite file up first)
codepulse migrate --check    # only report: exit 0 up to date, 1 needs migrating, 2 cannot be migrated safely
```

It works on whatever database CodePulse is configured for (`--db-url`, `CODEPULSE_DB_URL`, a `codepulse.toml` in the current directory, or the default `sqlite:///codepulse.db`) and does the safe thing for each case:

| The database is | `migrate` |
|---|---|
| new (no tables yet) | creates it at the current schema |
| behind | copies it to `<file>.pre-<revision>-<time>.bak`, then upgrades it |
| from an older CodePulse and was never tracked | checks its schema against the models; if it matches (tables it does not have yet are simply created, empty), records it as up to date, after a backup |
| not tracked and its schema differs in any other way (a missing or extra column, an extra table) | refuses, lists what differs, changes nothing |
| from a newer CodePulse | refuses, changes nothing |

Every CodePulse command prints a warning on stderr when the database is behind, and `codepulse doctor` reports the schema state. Until you migrate, CodePulse keeps working; features that need a newer table (for example module structure after a restart) are simply unavailable and a warning is logged. A database CodePulse creates from scratch is tracked from the start, so it can always be upgraded this way. The migration scripts ship inside the package, so this works from an installed copy as well as from a checkout.

## Development

```bash
pytest tests/ -q
```

Schema changes go through Alembic (`codepulse/storage/schema_migrations/`; `alembic.ini` in the repository root points at it for `alembic revision`/`alembic upgrade` during development). PostgreSQL, Redis, and distributed workers are intentionally out of scope for now.

## Project documents

- [`docs/CodePulse_Strategy_and_Roadmap.md`](docs/CodePulse_Strategy_and_Roadmap.md): the authoritative strategy, architecture, and milestone plan.
- [`docs/CODEPULSE_PRODUCT_AND_ARCHITECTURE.md`](docs/CODEPULSE_PRODUCT_AND_ARCHITECTURE.md): what CodePulse is and how it is built, as shipped.
- [`docs/CODEPULSE_CLIENT_STRATEGY.md`](docs/CODEPULSE_CLIENT_STRATEGY.md): the plan for the VS Code extension and other clients.
- [`docs/BACKUP_RESTORE.md`](docs/BACKUP_RESTORE.md): backing up and restoring a database.
- [`docs/ORIGINAL_DESIGN.md`](docs/ORIGINAL_DESIGN.md): the original design, kept for history (it describes infrastructure that was never built).
- [`docs/internal/`](docs/internal/): the working log (`CODEPULSE_PROGRESS.md`: what is built and verified, milestone by milestone) and `CODEPULSE_FUTURE_CHANGES.md` (known limitations and planned improvements).

## License

MIT. See [LICENSE](LICENSE).
