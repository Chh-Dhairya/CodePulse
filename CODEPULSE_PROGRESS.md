# CodePulse Development Progress

## Last Updated

2026-09-05

## Current Phase

Phase 0 — Foundation / Correctness

## Current Milestone

None actively in progress. Awaiting start of Phase 0.5 — Embedding persistence + load-on-startup hydration.

## Last Completed Milestone

Phase 0.4 — Symbol Upsert by `(file_id, name, start_line)` (verified COMPLETE)

## Completed Milestones

| Milestone | Status | Verification | Commit |
|---|---|---|---|
| B1 — Config system | COMPLETE | 7/7 tests in `tests/test_config.py`; config wired into `storage/db.py`, `embedder.py`, `bm25.py`, `cli.py`, `api/app.py` | `101607d` |
| Alembic migration infrastructure | IMPLEMENTED | 3/3 tests in `tests/test_migrations.py`; live `codepulse.db` confirmed stamped at initial revision | `4ccba7a` |
| Phase 0.3 — Repository/File Upsert + content_hash | COMPLETE | 41/41 full suite passed; migration verified non-destructive against a disposable copy, then applied to the real `codepulse.db` and re-verified | `571a21d` (implementation), migration applied to real DB in a later session (uncommitted app-state, not a code change) |
| Phase 0.4 — Symbol Upsert by `(file_id, name, start_line)` | COMPLETE | 49/49 full suite passed (41 pre-existing + 8 new/updated); migration verified non-destructive against a disposable copy, then applied to the real `codepulse.db` and re-verified | `e0b3ec0` |
| Phase 0.5 — Embedding persistence + hydration | NOT IMPLEMENTED | — | — |

Only mark a milestone COMPLETE here after actual verification (tests run, diff inspected, roadmap cross-checked) — not because code merely exists.

## Current Implementation State

- Repository identity: natural key = `url`. Enforced by `UNIQUE(url)` on `repositories` (DB-level) plus query-then-upsert logic in `IndexingEngine._persist_to_db` (`codepulse/indexer/engine.py`).
- File identity: natural key = `(repo_id, path)`. Enforced by `UNIQUE(repo_id, path)` on `files` (DB-level). Change detection uses `FileModel.content_hash` (SHA-256 hex digest of raw UTF-8 file content, computed in `LanguageParser.parse_file`, `codepulse/parser/base.py`; helper `compute_content_hash` in `codepulse/models.py`).
- **Symbol identity (Phase 0.4, new): natural key = `(file_id, name, start_line)`.** Enforced by `UniqueConstraint("file_id", "name", "start_line")` on `symbols` (DB-level, `codepulse/storage/db.py`). Change detection uses the pre-existing `SymbolModel.body_hash` (already computed by both parsers via the existing SHA-256 convention — no new hashing logic was introduced). Same insert/update/reuse-untouched pattern as the Phase 0.3 file upsert:
  - new `(file_id, name, start_line)` → insert.
  - existing key, unchanged `body_hash` → reuse the row untouched (literally zero writes — `SymbolModel` has no freshness timestamp to refresh, unlike `File`).
  - existing key, changed `body_hash` → update the existing row in place.
- Re-indexing an unchanged repository now produces **zero new Repository, File, or Symbol rows** end-to-end (verified by `tests/test_symbol_upsert.py::test_full_reindex_of_unchanged_repo_produces_zero_new_symbol_rows`, which re-indexes the same repo 3x in a row and asserts a stable Symbol count).
- **Dependency (call graph edge) rows are still NOT upserted.** `DependencyModel` rows are unconditionally inserted on every index run — re-indexing the same repository still duplicates Dependency rows. This is intentionally deferred to Phase 3 (call-graph confidence scoring), per the roadmap. This is the one remaining non-idempotent write path in the core indexing loop.
- `StorageManager.create_tables()` still calls `Base.metadata.create_all()` unconditionally on every instantiation, alongside Alembic. Pre-existing architecture finding from the forensic audit; not in scope for Phase 0.3 or 0.4, unchanged.
- Embeddings are still not persisted to `code_embeddings` (in-memory `VectorIndex` only). `CodeEmbedder` still contains its original silent hash-based pseudo-embedding fallback if `SentenceTransformer` fails to load — **this violates the "no silent pseudo-embedding fallback" invariant and remains unfixed.** This is exactly Phase 0.5's scope.
- Search (`IndexingEngine.search`) is still global/unscoped — no `repository_id` filtering (Phase 0.6 scope).
- `api/app.py` still contains hardcoded/fabricated metric values and a non-persisting `/v1/search/feedback` endpoint (Phase 0.7 scope).
- No `.gitignore`/`.codepulseignore` discovery (Phase 0.8 scope).
- `pyproject.toml` not yet trimmed; `rich` still undeclared (Phase 0.9 scope).

## Database State

- `codepulse.db` exists at the repository root, untracked by git (correctly excluded via `.gitignore`'s `*.db` rule).
- **Current Alembic revision (real `codepulse.db`, verified this session): `3ba396e9c78f`** (head — includes both the Phase 0.3 migration `20fc2d0607e2` and the Phase 0.4 migration `3ba396e9c78f`, both now actually applied to the real database, not just committed).
- Row counts as of last verification (unchanged since Phase 0.3): `repositories` = 1, `files` = 18, `symbols` = 128, `dependencies` = 528, `code_embeddings` = 0. Both migrations were confirmed to preserve every row (verified before/after on the real DB directly, in addition to disposable-copy testing).
- Schema facts, live in the real database now:
  - `files.content_hash` column present; `NULL` for all 18 pre-existing rows (honest — no historically-accurate hash could be reconstructed; each gets a real hash on next re-index).
  - `UNIQUE(repo_id, path)` on `files`, `UNIQUE(url)` on `repositories`, and **`UNIQUE(file_id, name, start_line)` on `symbols`** are all live and confirmed to reject duplicate inserts (tested directly against a disposable copy of the real DB with real data).
- The real `codepulse.db` **was modified this session** — both the Phase 0.3 and Phase 0.4 migrations were applied to it, at explicit user instruction. This was a schema-only, non-destructive `ALTER`-equivalent operation (Alembic batch mode); no row was added, deleted, or had its business-data columns changed. Both migrations were verified against a disposable scratch copy first, and the real DB's row counts/content were re-checked identical before and after each real application.

## Test State

- Last full suite result: **49 passed, 0 failed, 12 warnings** (`.venv/Scripts/python.exe -m pytest tests/ -v`), ~114s.
- Focused Phase 0.4 tests: `tests/test_symbol_upsert.py` (6 tests — new symbols inserted, unchanged symbol reused, changed body updates in place, same name isolated across files, DB-level uniqueness enforced, full unchanged-repo re-index produces zero new Symbol rows across 3 repeated runs).
- Updated from Phase 0.3: `tests/test_repository_file_upsert.py::test_symbols_are_deduplicated_on_reindex_phase_0_4` (was `test_symbols_are_still_duplicated_on_reindex_pending_phase_0_4`, which explicitly anticipated this update in its own docstring — this is a roadmap-approved behavior change, not a weakened test).
- Migration tests: `tests/test_migrations.py` (9 tests total — 2 base scenarios + 2 Phase 0.3 migration tests + 2 Phase 0.4 migration tests, plus schema assertions for all three natural-key constraints in the fresh-DB scenario). One pre-existing test (`test_phase_0_3_migration_preserves_data_and_adds_content_hash`) needed a one-line fix: it upgraded to generic `"head"`, which now resolves to the Phase 0.4 revision instead of the Phase 0.3 revision it's specifically testing — changed to target `20fc2d0607e2` explicitly (matching what its own docstring already said), not a weakening.
- No known failing tests.
- Known warnings: `StarletteDeprecationWarning` (httpx/TestClient) and `FutureWarning`s from `tree_sitter` — pre-existing, not addressed.

## Known Issues

**Roadmap work remaining (Phase 0, not yet implemented):**
- 0.5 Embedding persistence + load-on-startup hydration
- 0.6 Repository-scoped search (remove global singleton)
- 0.7 Remove fabricated metric values from API responses
- 0.8 `.gitignore`/`.codepulseignore`-respecting file discovery
- 0.9 Fix CORS policy defaults; trim `pyproject.toml`; add `rich`
- 0.10 Restart-survivability + idempotency test suite (beyond what 0.3/0.4's own focused tests cover)

**Known technical debt:**
- `StorageManager.create_tables()` still unconditionally calls `Base.metadata.create_all()` alongside Alembic on every instantiation.
- `SymbolType.VARIABLE` / `SymbolType.INTERFACE` enum values defined but never produced by either parser.
- Dead/unused `job_id` generation in `api/app.py` (`POST /v1/repositories/index`) — not pollable, superseded properly only once `IndexJob` (Phase 1) exists.
- Dependency (call graph edge) rows still duplicate on every re-index (Phase 3 scope, not Phase 0).

**Blockers:** none currently known for starting Phase 0.5.

## Deferred Work

Per roadmap §39 — do not implement until the roadmap explicitly reaches these:

- Kubernetes, Redis, distributed workers, microservices
- Enterprise SSO, full organization/user tenancy
- Learned/adaptive ranking, LLM reranking, LLM-generated explanations as source of truth
- PostgreSQL/pgvector, Tantivy-backed lexical index, embedded ANN vector index (Phase 2)
- `RepositorySnapshot`, `IndexJob`, `APIKey`, authentication/authorization (Phase 1/3)
- VS Code extension, Web UI (Phase 6)

## Current Risks

- The silent pseudo-embedding fallback in `codepulse/embeddings/embedder.py` is still live. Any work touching embeddings must not treat its output as a real embedding signal — this is precisely what Phase 0.5 must fix.
- `_persist_to_db` in `codepulse/indexer/engine.py` now has three upsert blocks (repo/file/symbol) in one function/session. Phase 0.5's embedding-persistence work will need to extend this same function carefully, keyed off the now-stable `symbol_record_map` (keyed by `(file_path, name, start_line)`) without breaking the Phase 0.3/0.4 behavior it depends on.
- Dependency (call graph edge) rows are the last remaining non-idempotent write in the core loop — do not assume the whole indexing pipeline is idempotent; only Repository/File/Symbol are, as of Phase 0.4.

## Exact Next Action

NEXT ACTION:
Implement Phase 0.5 — Embedding persistence + load-on-startup hydration according to the authoritative roadmap (see roadmap §37 0.5, §11 EmbeddingModel notes, and the "no silent pseudo-embedding fallback" invariant this milestone must finally satisfy in `codepulse/embeddings/embedder.py`).

## Context Handoff Notes

Phase 0.4 implementation is complete, verified, and committed (`e0b3ec0`):

- What was done: added `UniqueConstraint(file_id, name, start_line)` to `SymbolModel` (`codepulse/storage/db.py`); rewrote the symbol persistence block in `IndexingEngine._persist_to_db` (`codepulse/indexer/engine.py`) to query-then-insert-or-update by that natural key, using `body_hash` for change detection; added Alembic migration `3ba396e9c78f` (chained after `20fc2d0607e2`) with a pre-flight duplicate check; added `tests/test_symbol_upsert.py` (6 new tests); updated `tests/test_repository_file_upsert.py` (1 test updated from "still duplicates" to "now deduplicated," per its own anticipated-update docstring); updated `tests/test_migrations.py` (revision literals + 2 new Phase 0.4 migration tests + fixed one Phase 0.3 test that used generic `"head"`).
- What was verified: full suite 49/49 passed; Phase 0.4 migration verified non-destructive against a disposable copy of the real `codepulse.db` (128 symbols preserved, new constraint confirmed to reject a real duplicate insert); both the Phase 0.3 and Phase 0.4 migrations were then applied to the real `codepulse.db` itself (at explicit user instruction) and re-verified (revision `3ba396e9c78f`, all row counts unchanged: 1/18/128/528/0).
- Committed as `e0b3ec0` (7 files changed: `codepulse/storage/db.py`, `codepulse/indexer/engine.py`, `tests/test_migrations.py`, `tests/test_repository_file_upsert.py` modified; `alembic/versions/3ba396e9c78f_add_symbol_natural_key_uniqueness.py`, `tests/test_symbol_upsert.py` new; this file updated).
- Exact tests run: `.venv/Scripts/python.exe -m pytest tests/ -v` → 49 passed, 0 failed.
- Exact next step: follow CLAUDE.md §5's roadmap-discipline checklist for Phase 0.5, starting with re-reading the roadmap's §17/§34 guidance on removing the silent pseudo-embedding fallback and making embedding persistence real.
