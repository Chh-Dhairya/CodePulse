# CodePulse Development Progress

## Last Updated

2026-09-05

## Current Phase

Phase 0 — Foundation / Correctness

## Current Milestone

None actively in progress. Awaiting start of Phase 0.6 — Repository-scoped search (remove global singleton).

## Last Completed Milestone

Phase 0.5 — Embedding Persistence + Hydration (verified COMPLETE)

## Completed Milestones

| Milestone | Status | Verification | Commit |
|---|---|---|---|
| B1 — Config system | COMPLETE | 7/7 tests in `tests/test_config.py`; config wired into `storage/db.py`, `embedder.py`, `bm25.py`, `cli.py`, `api/app.py` | `101607d` |
| Alembic migration infrastructure | IMPLEMENTED | 3/3 tests in `tests/test_migrations.py`; live `codepulse.db` confirmed stamped at initial revision | `4ccba7a` |
| Phase 0.3 — Repository/File Upsert + content_hash | COMPLETE | 41/41 full suite passed; migration verified non-destructive against a disposable copy, then applied to the real `codepulse.db` and re-verified | `571a21d` |
| Phase 0.4 — Symbol Upsert by `(file_id, name, start_line)` | COMPLETE | 49/49 full suite passed; migration verified non-destructive against a disposable copy, then applied to the real `codepulse.db` and re-verified | `e0b3ec0` |
| Phase 0.5 — Embedding Persistence + Hydration | COMPLETE | 58/58 full suite passed (49 pre-existing + 9 new: 7 embedding tests + 2 migration tests); migration verified non-destructive against a disposable copy, then applied to the real `codepulse.db` and re-verified | `50dbe22` |
| Phase 0.6 — Repository-scoped search | NOT IMPLEMENTED | — | — |

Only mark a milestone COMPLETE here after actual verification (tests run, diff inspected, roadmap cross-checked) — not because code merely exists.

## Current Implementation State

- Repository identity: natural key = `url`. Enforced by `UNIQUE(url)` on `repositories` (DB-level).
- File identity: natural key = `(repo_id, path)`. Enforced by `UNIQUE(repo_id, path)` on `files` (DB-level). Change detection uses `FileModel.content_hash`.
- Symbol identity: natural key = `(file_id, name, start_line)`. Enforced by `UniqueConstraint` on `symbols` (DB-level). Change detection uses `SymbolModel.body_hash`.
- **Embedding identity (Phase 0.5, new): natural key = `(symbol_id, chunk_type, chunk_index)`**, per roadmap §8. Enforced by `UniqueConstraint("symbol_id", "chunk_type", "chunk_index")` on `code_embeddings` (DB-level, `codepulse/storage/db.py`). Change detection uses a new `EmbeddingModel.content_hash` (SHA-256 of the chunk's own text, via the existing `compute_content_hash` helper — not stripped, same convention as `File.content_hash`). Same insert/update/reuse-untouched pattern as Files/Symbols.
- **`codepulse/embeddings/embedder.py` no longer has a pseudo-embedding fallback.** `CodeEmbedder._get_model()`/`.encode()` now raise a new `EmbeddingUnavailableError` (typed, explicit) if the real `SentenceTransformer` model can't be loaded or fails to encode — **the "no silent pseudo-embedding fallback" invariant is now satisfied**, closing the last open architectural violation from the original forensic audit.
- **Embeddings are now durably persisted.** `IndexingEngine._persist_to_db` (`codepulse/indexer/engine.py`) upserts `EmbeddingModel` rows after the Symbol upsert block (chunks are linked back to their originating Symbol via an in-memory `chunk_symbol_map`, since a chunk's own `start_line`/`end_line` differ from its parent symbol's for body_chunk pieces). `EmbeddingModel` also now records `embedding_model` and `embedding_dim` so a later model/dimension change can be detected safely rather than silently mixed in.
- **Load-on-startup hydration is implemented.** New `IndexingEngine.hydrate_repository(repo_url)` / `hydrate_all()` methods load persisted embeddings straight into the in-memory `vector_index` with no re-parsing or model calls; `hydrate_all()` runs automatically at the end of `__init__`, so a freshly constructed engine (e.g. a new process) has semantic search available immediately from durable storage. Hydration skips (and counts) any row whose `embedding_dim` doesn't match the currently configured `vector_index.dimension`, rather than loading an incompatible vector. **Only semantic retrieval is restored this way** — `bm25_index`/`symbols_by_id`/`call_graph` are still in-memory-only and need a fresh `index_directory()` call after restart (no durable backing table for them yet; that's Phase 2/3 territory).
- **Embedding-generation failure is now a documented "degraded mode," not a crash or a silent success.** `index_directory()` catches `EmbeddingUnavailableError`, logs it, skips embedding persistence/vector-index population for that run only, and reports `embedding_status`/`embedding_error`/`degraded_mode` explicitly in its returned stats — while File/Symbol/BM25 persistence still proceed normally (lexical-only search stays available), per roadmap §17.
- Re-indexing an unchanged repository now produces **zero new Repository, File, Symbol, or Embedding rows** end-to-end.
- **Dependency (call graph edge) rows are still NOT upserted** — Phase 3 scope, unchanged, the one remaining non-idempotent write path in the core loop.
- `StorageManager.create_tables()` still calls `Base.metadata.create_all()` unconditionally on every instantiation, alongside Alembic — unchanged, pre-existing finding.
- Search (`IndexingEngine.search`) is still global/unscoped — no `repository_id` filtering (Phase 0.6 scope, next).
- `api/app.py` still contains hardcoded/fabricated metric values and a non-persisting `/v1/search/feedback` endpoint (Phase 0.7 scope). **`api/app.py` was NOT modified this session** — hydration works without any app.py change because it's wired into `IndexingEngine.__init__` itself, which `app.py`'s existing `IndexingEngine(storage=storage_manager)` construction already benefits from automatically.
- No `.gitignore`/`.codepulseignore` discovery (Phase 0.8 scope).
- `pyproject.toml` not yet trimmed; `rich` still undeclared (Phase 0.9 scope).

## Database State

- `codepulse.db` exists at the repository root, untracked by git (correctly excluded via `.gitignore`'s `*.db` rule).
- **Current Alembic revision (real `codepulse.db`, verified this session): `49572f834366`** (head — includes the Phase 0.5 migration, applied and re-verified this session).
- Row counts as of last verification (unchanged since Phase 0.3): `repositories` = 1, `files` = 18, `symbols` = 128, `dependencies` = 528, **`code_embeddings` = 0** (this repo's existing indexed data predates Phase 0.5 and has never been re-indexed since; it will gain real embedding rows the next time it's indexed — no data was fabricated to fill this in).
- Schema facts, live in the real database now: `code_embeddings.chunk_index` (NOT NULL, default 0 for pre-existing rows — moot here since there were none), `content_hash`/`embedding_model`/`embedding_dim` (all nullable, NULL for any pre-existing row), and `UNIQUE(symbol_id, chunk_type, chunk_index)` are all live.
- The real `codepulse.db` **was modified this session**: the Phase 0.5 migration was applied to it (schema-only, non-destructive `ALTER`-equivalent via Alembic batch mode). Verified against a disposable scratch copy first; the real DB's row counts were re-checked identical before and after (1/18/128/528/0 unchanged).

## Test State

- Last full suite result: **58 passed, 0 failed, 13 warnings** (`.venv/Scripts/python.exe -m pytest tests/ -v`), ~122s.
- Focused Phase 0.5 tests: `tests/test_embedding_persistence.py` (7 tests, using the REAL configured embedding model, not a mock/fake — a module-scoped shared `CodeEmbedder` fixture avoids repeatedly paying the model-load cost across tests): real embedding persisted on success; hydration via a second, independently-constructed engine populates the vector index from the DB alone; semantic search works after hydration with no re-indexing; re-indexing unchanged content creates zero new embedding rows; changed content updates in place (not stale reuse); dimension/model metadata survives a simulated restart; a genuine embedding-model load failure (bad model name, real network 401) produces zero embedding rows, zero fake vectors in the vector index, and an explicit `degraded_mode=True` status while files/symbols still persist.
- Migration tests: `tests/test_migrations.py` (11 tests total — added `test_phase_0_5_migration_preserves_data_and_adds_embedding_fields` and `test_phase_0_5_migration_refuses_to_apply_over_duplicate_data`, plus schema assertions for the new embedding constraint in the fresh-DB scenario). Three pre-existing tests needed their expected-head-revision literal updated to `49572f834366` (mechanical consequence of a new migration existing, same as every prior phase) — no assertion was weakened.
- No known failing tests.
- Known warnings: `StarletteDeprecationWarning` (httpx/TestClient) and `FutureWarning`s from `tree_sitter` — pre-existing, not addressed.

## Known Issues

**Roadmap work remaining (Phase 0, not yet implemented):**
- 0.6 Repository-scoped search (remove global singleton)
- 0.7 Remove fabricated metric values from API responses
- 0.8 `.gitignore`/`.codepulseignore`-respecting file discovery
- 0.9 Fix CORS policy defaults; trim `pyproject.toml`; add `rich`
- 0.10 Restart-survivability + idempotency test suite (beyond what 0.3/0.4/0.5's own focused tests cover)

**Known technical debt:**
- `StorageManager.create_tables()` still unconditionally calls `Base.metadata.create_all()` alongside Alembic on every instantiation.
- `SymbolType.VARIABLE` / `SymbolType.INTERFACE` enum values defined but never produced by either parser.
- Dead/unused `job_id` generation in `api/app.py` — not pollable, Phase 1 (`IndexJob`) scope.
- Dependency (call graph edge) rows still duplicate on every re-index (Phase 3 scope).
- A chunk whose parent symbol changes such that it no longer produces a chunk at a given `(chunk_type, chunk_index)` (e.g. a large function shrinks) can leave a stale `EmbeddingModel` row behind — documented, intentionally deferred to Phase 3's stale-data-cleanup design, mirrors the same Phase 0.4 decision for Symbol rows.
- `BM25Index`/`CallGraphBuilder` are still in-memory-only with no durable backing — only embeddings/semantic search survive restart as of Phase 0.5; keyword/hybrid search and impact analysis still require a fresh `index_directory()` call after a restart.

**Blockers:** none currently known for starting Phase 0.6.

## Deferred Work

Per roadmap §39 — do not implement until the roadmap explicitly reaches these:

- Kubernetes, Redis, distributed workers, microservices
- Enterprise SSO, full organization/user tenancy
- Learned/adaptive ranking, LLM reranking, LLM-generated explanations as source of truth
- PostgreSQL/pgvector, Tantivy-backed lexical index, embedded ANN vector index (Phase 2)
- `RepositorySnapshot`, `IndexJob`, `APIKey`, authentication/authorization (Phase 1/3)
- MCP server (Phase 5)
- VS Code extension, Web UI (Phase 6)

## Current Risks

- `CodeEmbedder.encode()` now raises `EmbeddingUnavailableError` instead of ever returning a fallback vector. Any new caller of `.encode()` (or `IndexingEngine.search(search_type="semantic"/"hybrid")`, which calls it for the query) must be prepared for this exception — `search()` itself does NOT currently catch it, so a semantic/hybrid query while the model is unavailable will raise, not degrade gracefully. This wasn't in Phase 0.5's stated scope (which was about *indexing-time* embedding generation) but is worth flagging for whoever next touches the search path.
- `_persist_to_db` now has four upsert blocks (repo/file/symbol/embedding) plus the dependency-insert block, all in one function/session — getting large. Phase 0.6 (repo-scoped search) shouldn't need to touch this function, but future phases extending persistence further should consider whether it's time to split it up (not done now, to avoid unrelated refactoring during a focused milestone).
- Only `repositories`, `files`, `symbols`, `code_embeddings` are idempotent as of Phase 0.5; `dependencies` is not (Phase 3 scope) — do not assume the whole indexing pipeline is duplicate-free.

## Exact Next Action

NEXT ACTION:
Implement Phase 0.6 — Repository-scoped search (remove global singleton) according to the authoritative roadmap.

## Context Handoff Notes

Phase 0.5 implementation is complete, verified, and committed (`50dbe22`):

- What was done: removed the silent pseudo-embedding fallback from `codepulse/embeddings/embedder.py`, replacing it with an explicit `EmbeddingUnavailableError`; added `chunk_index`/`content_hash`/`embedding_model`/`embedding_dim` columns + a `UniqueConstraint(symbol_id, chunk_type, chunk_index)` to `EmbeddingModel` (`codepulse/storage/db.py`); restructured `IndexingEngine.index_directory`/`_persist_to_db` (`codepulse/indexer/engine.py`) to link chunks back to their originating Symbol (`chunk_symbol_map`), catch embedding-generation failure explicitly (degraded mode, no fake vectors), and upsert Embedding rows by natural key after the Symbol upsert block; added `hydrate_repository`/`hydrate_all` methods, the latter called automatically from `__init__`; added Alembic migration `49572f834366` (chained after `3ba396e9c78f`) with a pre-flight duplicate check; added `tests/test_embedding_persistence.py` (7 new tests) and 2 new Phase 0.5 migration tests + 3 head-revision-literal fixes in `tests/test_migrations.py`.
- What was verified: full suite 58/58 passed; Phase 0.5 migration verified non-destructive against a disposable copy of the real `codepulse.db`, then applied to the real `codepulse.db` itself and re-verified (revision `49572f834366`, all row counts unchanged: 1/18/128/528/0).
- Committed as `50dbe22` (7 files changed: `codepulse/embeddings/embedder.py`, `codepulse/indexer/engine.py`, `codepulse/storage/db.py`, `tests/test_migrations.py` modified; `alembic/versions/49572f834366_add_embedding_persistence_fields.py`, `tests/test_embedding_persistence.py` new; this file updated).
- Exact tests run: `.venv/Scripts/python.exe -m pytest tests/ -v` → 58 passed, 0 failed.
- Exact next step: follow CLAUDE.md §5's roadmap-discipline checklist for Phase 0.6, starting with re-reading the roadmap's §15/§30 central-authorization-checkpoint framing (even though auth itself is Phase 1, the *scoping filter* on search is Phase 0.6). Also worth noting for whoever starts 0.6: `IndexingEngine.search(search_type="semantic"/"hybrid")` does not currently catch `EmbeddingUnavailableError` for the query-side embed call (see Current Risks) — decide whether that's in 0.6's scope or a separate concern before touching `search()`.
