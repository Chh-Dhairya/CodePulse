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

The raw key is shown once and only its hash is stored. `repository_ids` must reference an existing repository, so index something first. Further keys come from `POST /v1/admin/keys`.

Main endpoints: `POST /v1/search`, `POST /v1/repositories/index`, `GET /v1/repositories/{id}/index-status`, `GET /v1/symbols/{id}`, `POST /v1/symbols/{id}/impact-analysis`, `POST /v1/symbols/{id}/similar`, `GET /v1/symbols/{id}/references`, `POST /v1/agent/context`. Errors always use one shape: `{"error": {"code", "message", "details"}}`. Requests are rate-limited per key (5 req/s, burst 20, configurable).

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

## Development

```bash
pytest tests/ -q
```

Schema changes go through Alembic (`alembic/`, `alembic.ini`). PostgreSQL, Redis, and distributed workers are intentionally out of scope for now.

## Project documents

- `CodePulse_Strategy_and_Roadmap.md`: the authoritative strategy, architecture, and milestone plan.
- `CODEPULSE_PROGRESS.md`: what is built and verified, milestone by milestone.
- `CODEPULSE_FUTURE_CHANGES.md`: known limitations and planned improvements.

## License

MIT. See [LICENSE](LICENSE).
