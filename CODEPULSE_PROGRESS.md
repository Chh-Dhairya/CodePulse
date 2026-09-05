# CodePulse Development Progress

## Last Updated

2026-09-05

## Current Phase

Phase 0 — Foundation / Correctness

## Current Milestone

None actively in progress. Awaiting start of Phase 0.4 — Symbol Upsert.

## Last Completed Milestone

Phase 0.3 — Repository/File Upsert with content_hash (verified COMPLETE)

## Completed Milestones

| Milestone | Status | Verification | Commit |
|---|---|---|---|
| B1 — Config system | COMPLETE | 7/7 tests in `tests/test_config.py`; config wired into `storage/db.py`, `embedder.py`, `bm25.py`, `cli.py`, `api/app.py` | `101607d` |
| Alembic migration infrastructure | IMPLEMENTED | 3/3 tests in `tests/test_migrations.py`; live `codepulse.db` confirmed stamped at initial revision | `4ccba7a` |
| Phase 0.3 — Repository/File Upsert + content_hash | COMPLETE | 41/41 full suite passed (30 pre-existing + 11 new); migration verified non-destructive against a disposable copy of `codepulse.db` | `571a21d` |
| Phase 0.4 — Symbol Upsert | NOT IMPLEMENTED | — | — |

Only mark a milestone COMPLETE here after actual verification (tests run, diff inspected, roadmap cross-checked) — not because code merely exists.

## Current Implementation State

- Repository identity: natural key = `url` (explicit repo/git URL, or absolute local path when none given). Enforced by `UNIQUE(url)` on `repositories` (DB-level) plus query-then-upsert logic in `IndexingEngine._persist_to_db` (`codepulse/indexer/engine.py`).
- File identity: natural key = `(repo_id, path)`. Enforced by `UNIQUE(repo_id, path)` on `files` (DB-level) plus the same upsert logic. Change detection uses `FileModel.content_hash` (SHA-256 hex digest of raw UTF-8 file content, computed in `LanguageParser.parse_file`, `codepulse/parser/base.py`; helper `compute_content_hash` in `codepulse/models.py`).
- Re-indexing an unchanged repository/file now reuses existing rows (no duplicates). Re-indexing a changed file updates the existing `File` row in place.
- **Symbols and Dependencies are still NOT upserted.** `SymbolModel`/`DependencyModel` rows are unconditionally inserted on every index run — re-indexing the same repository still duplicates Symbol and Dependency rows. This is documented and intentionally deferred to Phase 0.4 (symbols) and Phase 3 (call-graph confidence/dependencies). A dedicated test (`test_symbols_are_still_duplicated_on_reindex_pending_phase_0_4` in `tests/test_repository_file_upsert.py`) exists specifically to catch an unnoticed regression/advance of this fact.
- `StorageManager.create_tables()` still calls `Base.metadata.create_all()` unconditionally on every instantiation, alongside Alembic. This was flagged as a pre-existing architecture finding in the forensic audit; it was not in scope for Phase 0.3 and has not been changed.
- Embeddings are still not persisted to `code_embeddings` (in-memory `VectorIndex` only). `CodeEmbedder` still contains its original silent hash-based pseudo-embedding fallback if `SentenceTransformer` fails to load — **this violates the "no silent pseudo-embedding fallback" invariant and remains unfixed.**
- Search (`IndexingEngine.search`) is still global/unscoped — no `repository_id` filtering.
- `api/app.py` still contains hardcoded/fabricated metric values (`index_size_mb`, `total_queries`, `avg_search_latency_ms`) and a non-persisting `/v1/search/feedback` endpoint.
- No `.gitignore`/`.codepulseignore` discovery — file discovery still uses only the hardcoded `IGNORE_DIRS`/`IGNORE_EXTENSIONS` sets in `codepulse/indexer/engine.py`.
- `pyproject.toml` not yet trimmed (still declares unused `psycopg[binary]`, `pgvector`, `tantivy`, `redis`, `prometheus-client`, `structlog`; still missing `rich`, which `cli.py` actually imports).

## Database State

- `codepulse.db` exists at the repository root, untracked by git (correctly excluded via `.gitignore`'s `*.db` rule).
- Current Alembic revision (as of last verification): `21a7cb7f1a85` (the Phase 0.3 migration `20fc2d0607e2` exists in `alembic/versions/` and is committed, but has **not** been applied to the real `codepulse.db`  — it was verified only against a disposable copy, per Phase 0.3 instructions. It must be applied — `alembic upgrade head` / `codepulse.storage.migrations.run_migrations()` — before Phase 0.4 work assumes the new schema is live in the real database).
- Row counts as of last verification: `repositories` = 1, `files` = 18, `symbols` = 128, `dependencies` = 528, `code_embeddings` = 0.
- Schema facts: `files.content_hash` and the `UNIQUE(repo_id, path)` / `UNIQUE(url)` constraints exist in the migration and ORM models, but are only live in the real `codepulse.db` once that migration is actually applied there (see above).
- The real `codepulse.db` was **not modified** during Phase 0.3 implementation or verification — all migration verification used a disposable scratch copy, which was deleted afterward.
- Existing (pre-Phase-0.3) `files` rows will have `content_hash = NULL` once the migration is applied to the real database, because no historically-accurate hash could be reconstructed from previously-stored data (only path/metadata was stored, not whole-file content). Each such row will get a real hash the next time its file is re-indexed.

## Test State

- Last full suite result: **41 passed, 0 failed, 12 warnings** (`.venv/Scripts/python.exe -m pytest tests/ -v`), from the Phase 0.3 implementation.
- Focused Phase 0.3 tests: `tests/test_repository_file_upsert.py` (10 tests — repo/file upsert, isolation across repos, DB-level uniqueness, content_hash correctness, documented symbol-duplication limitation).
- Migration tests: `tests/test_migrations.py` (5 tests — fresh-DB migration, existing-DB stamping, Phase 0.3 migration preserves data + adds content_hash, Phase 0.3 migration refuses to apply over duplicate data).
- No known failing tests.
- Known warnings: a `StarletteDeprecationWarning` (httpx/TestClient) and `FutureWarning`s from `tree_sitter` — pre-existing, not introduced by Phase 0.3, not yet addressed.

## Known Issues

**Roadmap work remaining (Phase 0, not yet implemented):**
- 0.4 Symbol upsert by `(file_id, name, start_line)`
- 0.5 Embedding persistence + load-on-startup hydration
- 0.6 Repository-scoped search (remove global singleton)
- 0.7 Remove fabricated metric values from API responses
- 0.8 `.gitignore`/`.codepulseignore`-respecting file discovery
- 0.9 Fix CORS policy defaults; trim `pyproject.toml`; add `rich`
- 0.10 Restart-survivability + idempotency test suite (beyond Phase 0.3's own scope)

**Known technical debt (not currently scheduled to a specific milestone unless noted):**
- `StorageManager.create_tables()` still unconditionally calls `Base.metadata.create_all()` alongside Alembic on every instantiation.
- `SymbolType.VARIABLE` / `SymbolType.INTERFACE` enum values defined but never produced by either parser.
- Dead/unused `job_id` generation in `api/app.py` (`POST /v1/repositories/index`) — not pollable, superseded properly only once `IndexJob` (Phase 1) exists.

**Blockers:** none currently known for starting Phase 0.4.

## Deferred Work

Per roadmap §39 — do not implement until the roadmap explicitly reaches these:

- Kubernetes, Redis, distributed workers, microservices
- Enterprise SSO, full organization/user tenancy
- Learned/adaptive ranking, LLM reranking, LLM-generated explanations as source of truth
- PostgreSQL/pgvector, Tantivy-backed lexical index, embedded ANN vector index (Phase 2)
- `RepositorySnapshot`, `IndexJob`, `APIKey`, authentication/authorization (Phase 1/3)
- VS Code extension, Web UI (Phase 6)

## Current Risks

- The Phase 0.3 migration (`20fc2d0607e2`) is committed but **not yet applied to the real `codepulse.db`**. Any agent starting Phase 0.4 must apply it first (or confirm it's already been applied by a later session) and re-verify actual DB state — do not assume this document is still accurate; re-check per CLAUDE.md §10/§13.
- The silent pseudo-embedding fallback in `codepulse/embeddings/embedder.py` is still live. Any work touching embeddings must not treat its output as a real embedding signal.
- `_persist_to_db` in `codepulse/indexer/engine.py` now has more moving parts (repo/file upsert branches) — Phase 0.4's symbol upsert work will need to extend this same function/session carefully without breaking the Phase 0.3 file-upsert behavior it now depends on (`file_record_map` keyed by path).

## Exact Next Action

NEXT ACTION:
Apply the committed Phase 0.3 Alembic migration (`20fc2d0607e2`) to the real `codepulse.db` (verify via `get_current_revision`), then implement Phase 0.4 — Symbol Upsert by `(file_id, name, start_line)` according to the authoritative roadmap.

## Context Handoff Notes

No implementation is currently in progress. Phase 0.3 was completed, tested (41 passed), verified against a disposable database copy, and committed (`571a21d`) in a prior session. This document (`CODEPULSE_PROGRESS.md`) and `CLAUDE.md` were created in this session as permanent project memory; no application code, tests, or `codepulse.db` were modified in this session.

Files changed in this session: `CLAUDE.md` (created), `CODEPULSE_PROGRESS.md` (created). No other files touched.

Tests run in this session: none (not required — no application code was changed).

Exact next step for the next agent/session: follow CLAUDE.md §10's "starting a fresh context" checklist, then proceed with the Exact Next Action above.
