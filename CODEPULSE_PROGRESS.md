# CodePulse Development Progress

## Last Updated

2026-09-05

## Current Phase

Phase 0 — Foundation / Correctness

## Current Milestone

None actively in progress. Awaiting start of Phase 0.7 — Remove all hardcoded metric values from API responses.

## Last Completed Milestone

Phase 0.6 — Repository-scoped search (remove global singleton) (verified COMPLETE)

## Completed Milestones

| Milestone | Status | Verification | Commit |
|---|---|---|---|
| B1 — Config system | COMPLETE | 7/7 tests in `tests/test_config.py`; config wired into `storage/db.py`, `embedder.py`, `bm25.py`, `cli.py`, `api/app.py` | `101607d` |
| Alembic migration infrastructure | IMPLEMENTED | 3/3 tests in `tests/test_migrations.py`; live `codepulse.db` confirmed stamped at initial revision | `4ccba7a` |
| Phase 0.3 — Repository/File Upsert + content_hash | COMPLETE | 41/41 full suite passed; migration verified non-destructive, then applied to the real `codepulse.db` | `571a21d` |
| Phase 0.4 — Symbol Upsert by `(file_id, name, start_line)` | COMPLETE | 49/49 full suite passed; migration verified non-destructive, then applied to the real `codepulse.db` | `e0b3ec0` |
| Phase 0.5 — Embedding Persistence + Hydration | COMPLETE | 58/58 full suite passed; migration verified non-destructive, then applied to the real `codepulse.db` | `50dbe22` |
| Phase 0.6 — Repository-scoped search (remove global singleton) | COMPLETE | 69/69 full suite passed (58 pre-existing + 11 new); no migration required; verified against the real `codepulse.db` read-only | *(uncommitted as of this update — see Exact Next Action)* |
| Phase 0.7 — Remove fabricated metric values from API responses | NOT IMPLEMENTED | — | — |

Only mark a milestone COMPLETE here after actual verification (tests run, diff inspected, roadmap cross-checked) — not because code merely exists.

## Current Implementation State

- Repository identity: natural key = `url` (`RepositoryModel.id` is the canonical integer scope identifier used everywhere, including the new search scoping — no second identity system was introduced).
- File identity: natural key = `(repo_id, path)`. Symbol identity: `(file_id, name, start_line)`. Embedding identity: `(symbol_id, chunk_type, chunk_index)`. All three DB-enforced, unchanged by Phase 0.6.
- **Repository-scoped search is now implemented (Phase 0.6).** `IndexingEngine.search(query, limit, search_type, repository_id=None)` (`codepulse/indexer/engine.py`) accepts an optional `repository_id`. When given, keyword (BM25), semantic (vector), and hybrid search are all filtered to that repository only, enforced *inside* `search()` itself (not left to a caller) and *before* truncation to `limit` (so a well-populated target repository is never starved by another repository's higher-scoring documents). When `repository_id` is `None`, behavior is byte-for-byte identical to pre-Phase-0.6 (fully unscoped/global) — fully backward compatible.
- **How scoping is enforced:** every in-memory BM25/vector document is now tagged with `repo_id` in its metadata (resolved *before* parsing begins, via a new `IndexingEngine._resolve_repo_id()` that does the same natural-key upsert-by-`url` lookup `_persist_to_db` already does, just early enough to tag documents as they're created) and given a repo-qualified `doc_id` (`f"{repo_id}::{file_path}::..."`) to prevent collisions when two repositories share a file path + symbol name — a real, tested scenario (two repos both containing `main.py::process_data`). `search()` filters on the `repo_id` metadata tag, not by parsing doc_id strings.
- **Call graph is now per-repository.** `IndexingEngine.call_graphs: dict[int, CallGraphBuilder]` (keyed by `repo_id`) replaces the previous single `self.call_graph`, which used to get silently overwritten by whichever repository was indexed *last* — a real cross-repo bug for hybrid search's graph-derived score, now fixed as part of this milestone since hybrid search is explicitly in scope. `self.call_graph` is kept in sync as a "most recently indexed repository" alias for `cli.py`'s `cmd_impact` / `api/app.py`'s `analyze_impact`, which are unaffected/out of scope and behave exactly as before.
- **Hydration (Phase 0.5) is now repository-tagged too.** `hydrate_repository()` tags loaded vectors with the same `repo_id` metadata scheme, so `search(..., repository_id=...)` works correctly against hydrated (not just freshly indexed) embeddings, and `hydrate_all()` can load multiple repositories into the same shared `vector_index` without them becoming indistinguishable.
- **API wiring:** `SearchRequest.repository_id` and `IdeSearchRequest.repository_id` — pre-existing fields that were defined but silently ignored — are now actually passed through to `indexing_engine.search(...)` in `/v1/search` and `/v1/ide-search` (`codepulse/api/app.py`). No new endpoints, no schema/default changes; this is "make the existing parameter real," per the roadmap's B4 definition of done ("`repository_id` actually filters every search").
- `/v1/symbols/{id}/similar`, `/v1/autocomplete`, and other endpoints that touch `vector_index`/`symbols_by_id` directly (bypassing `IndexingEngine.search()`) were **not** touched — out of Phase 0.6's scope (not one of the three search modes), unchanged behavior.
- Re-indexing an unchanged repository still produces zero new Repository/File/Symbol/Embedding rows (Phase 0.3-0.5, unaffected by this milestone). Dependency rows still non-idempotent (Phase 3 scope, unchanged).

## Database State

- `codepulse.db` exists at the repository root, untracked by git.
- **Current Alembic revision (real `codepulse.db`): `49572f834366`** — **unchanged by Phase 0.6**. No migration was created or needed: repository scoping reuses the existing `RepositoryModel.id` primary key entirely in-memory/at-query-time; no new column or constraint was required.
- Row counts (unchanged since Phase 0.3, re-verified this session): `repositories` = 1, `files` = 18, `symbols` = 128, `dependencies` = 528, `code_embeddings` = 0.
- The real `codepulse.db` was **not modified** during Phase 0.6 (read-only verification only: querying the real repository's id, constructing a fresh `IndexingEngine` against it to confirm hydration doesn't crash with real data present, and calling `search(..., repository_id=<real id>)` for all three modes to confirm no crash — all read-only; `hydrate_repository`/`hydrate_all` never write to the DB). No re-indexing was performed against the real repo (would have been legitimate, not "fake data," but was avoided as unnecessary for verifying this milestone and to keep the change minimal).

## Test State

- Last full suite result: **69 passed, 0 failed, 13 warnings** (`.venv/Scripts/python.exe -m pytest tests/ -v`), ~116s.
- Focused Phase 0.6 tests: `tests/test_repository_scoped_search.py` (11 tests, real embedding model via a module-scoped shared `CodeEmbedder`). Two repositories (A, B) are indexed into one shared `IndexingEngine`, deliberately with an **overlapping** symbol (`process_data` at the same relative path `main.py` in both, differing only in docstring content) — the adversarial case that would leak or collide under a naive same-repo-only doc_id scheme. Covers: keyword/semantic/hybrid search each scoped to A returns only A (verified by doc_id repo-prefix *and* distinguishing docstring content, not counts); same for B; unscoped search still mixes both (proves scoping is additive, not a behavior change to the default); a query unique to repo A's second file (`utils.py::helper_a`) is found when scoped to A and never leaks when scoped to B; nonexistent `repository_id` → `[]` for all three modes; a freshly indexed empty repository → `[]`; empty-query string doesn't crash under scoping; repeated identical scoped queries return byte-identical ordered results (determinism).
- All 27 pre-existing tests that touch `IndexingEngine.search`, `BM25Index`, `api/app.py`'s search endpoints, or hydration (`test_api.py`, `test_bm25_vector.py`, `test_embedding_persistence.py`, `test_repository_file_upsert.py`, `test_symbol_upsert.py`) were re-run explicitly before the full suite and pass unchanged — confirming the unscoped/default code path is untouched.
- No test was weakened, skipped, or deleted. No migration tests needed updating (no new migration).
- Known warnings: `StarletteDeprecationWarning` (httpx/TestClient) and `FutureWarning`s from `tree_sitter` — pre-existing, not addressed.

## Known Issues

**Roadmap work remaining (Phase 0, not yet implemented):**
- 0.7 Remove fabricated metric values from API responses
- 0.8 `.gitignore`/`.codepulseignore`-respecting file discovery
- 0.9 Fix CORS policy defaults; trim `pyproject.toml`; add `rich`
- 0.10 Restart-survivability + idempotency test suite (beyond what 0.3-0.6's own focused tests cover)

**Known technical debt:**
- `StorageManager.create_tables()` still unconditionally calls `Base.metadata.create_all()` alongside Alembic.
- `SymbolType.VARIABLE` / `SymbolType.INTERFACE` enum values defined but never produced by either parser.
- Dead/unused `job_id` generation in `api/app.py` — Phase 1 (`IndexJob`) scope.
- Dependency (call graph edge) rows still duplicate on every re-index (Phase 3 scope).
- A chunk whose parent symbol changes such that it no longer produces a chunk at a given `(chunk_type, chunk_index)` can leave a stale `EmbeddingModel` row behind (Phase 3 stale-data-cleanup scope, documented in Phase 0.5).
- `BM25Index`/`CallGraphBuilder` are still in-memory-only with no durable backing — only embeddings/semantic search survive a real process restart; keyword/hybrid search still requires a fresh `index_directory()` call after restart (now correctly repo-scoped once re-indexed, but not durable across restart yet — that's Phase 2/3 territory, not Phase 0.6).
- **New from Phase 0.6:** a symbol/file with the *exact same name and start_line* as another symbol *within the same file, same repository* still collides in `self.symbols_by_id`'s keying (pre-existing bug, not introduced or worsened by Phase 0.6, and out of this milestone's scope, which only fixes *cross-repository* collisions). Not expected in normal code (would require two distinct symbols starting on the identical line) but noted for completeness.
- `IndexingEngine.search(search_type="semantic"/"hybrid")` still does not catch `EmbeddingUnavailableError` for the query-side embed call (noted since Phase 0.5) — unaffected/unchanged by Phase 0.6, still an open item for whoever next touches the search path.
- `_resolve_repo_id()` (new) performs a small, separately-committed upsert of the Repository row before `_persist_to_db()` performs its own (idempotent) upsert of the same row later in the same `index_directory()` call — two small transactions instead of one. Harmless (the row is keyed by the same natural key `url` both times, so no duplicate is ever created), but worth knowing if a future phase wants to unify indexing into fewer transactions.

**Blockers:** none currently known for starting Phase 0.7.

## Deferred Work

Per roadmap §39 — do not implement until the roadmap explicitly reaches these:

- Kubernetes, Redis, distributed workers, microservices
- Enterprise SSO, full organization/user tenancy
- Learned/adaptive ranking, LLM reranking, LLM-generated explanations as source of truth
- PostgreSQL/pgvector, Tantivy-backed lexical index, embedded ANN vector index (Phase 2)
- `RepositorySnapshot`, `IndexJob`, `APIKey`, authentication/authorization (Phase 1/3)
- Call-graph confidence scoring, impact-analysis depth bucketing, module clustering (Phase 3/4)
- MCP server (Phase 5)
- VS Code extension, Web UI (Phase 6)

## Current Risks

- The Phase 0.6 code changes (engine, bm25, api/app.py, tests) are implemented, fully tested, and verified against the real `codepulse.db`, but **not yet committed to git** as of this update — see Exact Next Action. Do not assume they're committed without checking `git status`/`git log`.
- `_persist_to_db` still has four upsert blocks plus the dependency-insert block in one function/session — unchanged by Phase 0.6 (this milestone didn't need to touch it), still worth splitting up eventually.
- Only `repositories`, `files`, `symbols`, `code_embeddings` are idempotent; `dependencies` is not (Phase 3 scope) — unaffected by, and unrelated to, this milestone's repository-scoping work (scoping and idempotency are separate concerns).

## Exact Next Action

NEXT ACTION:
Implement Phase 0.7 — Remove all hardcoded/fabricated metric values from API responses, according to the authoritative roadmap (`index_size_mb`, `total_queries`, `avg_search_latency_ms` in `codepulse/api/app.py`, and the non-persisting `/v1/search/feedback` endpoint).

## Context Handoff Notes

Phase 0.6 implementation is complete and verified in this session:

- What was done: `IndexingEngine.search()` gained a `repository_id: int | None = None` parameter (`codepulse/indexer/engine.py`) that filters BM25/vector candidates by a `repo_id` metadata tag before truncation to `limit`, for all three search modes; `_resolve_repo_id()` resolves the real `RepositoryModel.id` before parsing so documents can be tagged as they're created; `call_graph` became per-repository (`call_graphs: dict[int, CallGraphBuilder]`); `hydrate_repository()` tags hydrated vectors the same way; `BM25Index.add_symbol()` gained an optional `repo_id` parameter (`codepulse/search/bm25.py`); `api/app.py`'s `/v1/search` and `/v1/ide-search` now pass their existing (previously unused) `repository_id` request field through.
- What was verified: full suite 69/69 passed (27 potentially-affected pre-existing tests re-run explicitly first); 11 new isolation tests using two repositories with deliberately overlapping content (same file path, same symbol name) prove no cross-repo leakage for keyword/semantic/hybrid search, correct unscoped behavior, nonexistent/empty-repository handling, and determinism; real `codepulse.db` confirmed unchanged (revision `49572f834366`, row counts 1/18/128/528/0) and a fresh engine constructed against it hydrates and accepts scoped search calls without crashing.
- No Alembic migration was created (none needed — `RepositoryModel.id`, already established, is the scope identifier).
- What remains: **committing this work to git** (not yet done as of this document update — the user's instructions for this milestone explicitly said not to commit unless asked), then starting Phase 0.7.
- Exact files changed this session (uncommitted at time of writing): `codepulse/indexer/engine.py`, `codepulse/search/bm25.py`, `codepulse/api/app.py` (all modified); `tests/test_repository_scoped_search.py` (new); this file updated.
- Exact tests run: `.venv/Scripts/python.exe -m pytest tests/ -v` → 69 passed, 0 failed.
- Exact next step: follow CLAUDE.md §5's roadmap-discipline checklist for Phase 0.7, starting with re-reading roadmap §21 ("No endpoint may return a metric it did not actually measure") and §3's fabricated-metrics finding table.
