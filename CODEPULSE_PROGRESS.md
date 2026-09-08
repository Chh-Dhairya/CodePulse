# CodePulse Development Progress

## Last Updated

2026-09-08

## Current Phase

Phase 0 — Foundation / Correctness (final milestone of the phase)

## Current Milestone

None actively in progress. **Phase 0 is now fully implemented per its sprint list (0.1–0.10).** Awaiting start of Phase 1 — Production Core.

## Last Completed Milestone

Phase 0.10 — Restart-survivability + idempotency test suite (verified COMPLETE)

## Completed Milestones

| Milestone | Status | Verification | Commit |
|---|---|---|---|
| B1 — Config system | COMPLETE | 7/7 tests in `tests/test_config.py` | `101607d` |
| Alembic migration infrastructure | IMPLEMENTED | 3/3 tests in `tests/test_migrations.py` | `4ccba7a` |
| Phase 0.3 — Repository/File Upsert + content_hash | COMPLETE | 41/41 full suite passed; migration applied to real `codepulse.db` | `571a21d` |
| Phase 0.4 — Symbol Upsert by `(file_id, name, start_line)` | COMPLETE | 49/49 full suite passed; migration applied to real `codepulse.db` | `e0b3ec0` |
| Phase 0.5 — Embedding Persistence + Hydration | COMPLETE | 58/58 full suite passed; migration applied to real `codepulse.db` | `50dbe22` |
| Phase 0.6 — Repository-scoped search (remove global singleton) | COMPLETE | 69/69 full suite passed; no migration required | `aea7518` |
| Phase 0.7 — Remove fabricated metric values from API responses | COMPLETE | 74/74 full suite passed; no migration required | `704c41d` |
| Phase 0.8 — `.gitignore`/`.codepulseignore` discovery | COMPLETE | 86/86 full suite passed; no migration required | `b27381a` |
| Phase 0.9 — CORS defaults + `pyproject.toml` trim + `rich` | COMPLETE | 95/95 full suite passed; no migration required | `8f77aaf` |
| Phase 0.10 — Restart-survivability + idempotency test suite | COMPLETE | 99/99 full suite passed (95 pre-existing + 4 new); no production code changed; no migration required; real `codepulse.db` untouched | *(uncommitted as of this update — see Exact Next Action)* |

Only mark a milestone COMPLETE here after actual verification (tests run, diff inspected, roadmap cross-checked) — not because code merely exists.

## Current Implementation State

- **Phase 0.10 is a pure validation milestone: zero production code was changed.** The audit found the existing Phase 0.3–0.6 persistence/hydration implementation already correct against every invariant this milestone re-verifies; the new test file only ADDS coverage for gaps the audit identified (see below), it does not fix any defect.
- **Existing coverage audited first** (per CLAUDE.md §5's roadmap discipline), confirmed already thorough:
  - Repository/File idempotency across re-indexing (`tests/test_repository_file_upsert.py`, Phase 0.3): new-row insertion, reused-row-on-unchanged-content, updated-row-on-changed-content, natural-key DB-level uniqueness, cross-repository isolation of identical paths.
  - Symbol idempotency (`tests/test_symbol_upsert.py`, Phase 0.4): same pattern, plus a 3x-repeated-reindex stability check for symbol counts specifically.
  - Embedding persistence + hydration + no-pseudo-embedding-fallback (`tests/test_embedding_persistence.py`, Phase 0.5): real embedding persisted on success, a second independently-constructed engine hydrates it, semantic search works from hydration alone, unchanged/changed-content embedding reuse/update semantics, dimension/model metadata survives restart, and a genuine model-load failure produces zero pseudo-embeddings.
  - Alembic migration survivability (`tests/test_migrations.py`, all phases): fresh database reaches head, existing-database stamping, and every phase's migration individually verified non-destructive against seeded legacy data. **Not duplicated in Phase 0.10** — already fully satisfies the roadmap's migration-survivability concern.
  - Repository isolation for search (`tests/test_repository_scoped_search.py`, Phase 0.6) — but only within one *continuous* engine session, never combined with a restart.
- **Gaps the audit identified and Phase 0.10 fills** (new file: `tests/test_restart_idempotency.py`, 4 tests):
  1. **Combined repo+file+symbol+embedding idempotency with identifier stability, across 5 repeated re-indexes of one repository** (existing tests proved each entity type individually, mostly across exactly 2 indexing calls) — verifies repository id, the full set of file ids, symbol ids, and embedding ids are all *identical* after every single pass, not just that counts stay the same.
  2. **An explicit test of what actually survives a restart vs. what doesn't**: semantic/vector state (durable, hydrated automatically) vs. lexical (`BM25Index`) and call-graph (`CallGraphBuilder`) state (genuinely NOT durable — no backing DB table for either). No prior test asserted the *absence* of lexical/graph state after a restart, only the presence of hydrated vector state — this closes that gap and guards against ever silently claiming full restart-survivability.
  3. **A test proving hydration itself never calls the embedding model** — not merely "doesn't crash" (the existing `hydrate_all()`/`__init__` try/except would silently swallow a real call failure, so that alone wouldn't prove non-invocation). Uses a spy wrapping a deliberately-broken `CodeEmbedder.encode` for the "restart" engine and asserts zero calls, while confirming hydration still succeeds.
  4. **Repository isolation for semantic search specifically after a restart** (existing isolation tests only cover one continuous engine session; this test indexes two repositories with deliberately overlapping content, restarts, then re-verifies isolation holds against the *hydrated* state).
- **Restart model tested, precisely**: every "restart" in the new tests means `engine.storage.engine.dispose()` (closing the first `StorageManager`'s SQLAlchemy connection pool) followed by constructing a brand-new `StorageManager` + `IndexingEngine` against the same on-disk SQLite file — Restart Model B (closing the database/session and creating a fresh engine), **not** a real OS-level process kill/restart (Model C). No subprocess infrastructure was introduced; the roadmap does not require literal process-level restart testing, and the existing architecture (one Python library, not a client/server split) makes Model B correct and sufficient to prove the persistence contract. **This document does not claim "process restart" was tested — only fresh-engine/fresh-connection restart was.**
- **One test-authoring bug was found and fixed during this milestone — not a production defect.** The first draft of the repository-isolation-after-restart test asserted on `SearchResultItem.docstring` for semantic-search results; that field has never been populated by the vector-index metadata pipeline (neither at fresh-index time nor by `hydrate_repository`) — only `snippet` is. Verified directly (a real run showed `docstring=None` but `snippet` correctly containing the repository-distinguishing text and `doc_id` correctly repo-prefixed) before concluding this was a wrong test assumption, not an isolation failure. Fixed by asserting on `snippet`/`doc_id` instead. No production code was touched for this.

## Restart Model

| Level | Description | Tested by Phase 0.10? |
|---|---|---|
| A — new engine object, same process | A second `IndexingEngine` instance without closing the first's DB connection | Implicitly (all restart tests also happen to run in one process) |
| **B — closed DB/session, fresh engine** | `engine.storage.engine.dispose()` then a brand-new `StorageManager`/`IndexingEngine` against the same SQLite file | **Yes — this is what all 4 new tests verify** |
| C — real OS process kill/restart | A genuine subprocess terminated and relaunched | **No — not tested, not required by the roadmap for this milestone, no subprocess infrastructure introduced** |

## Database State

- `codepulse.db` exists at the repository root, untracked by git.
- **Current Alembic revision (real `codepulse.db`): `49572f834366`** — **unchanged by Phase 0.10**. No migration was created, needed, or even considered: this milestone is test-only.
- Row counts (unchanged since Phase 0.3, re-verified this session): `repositories` = 1, `files` = 18, `symbols` = 128, `dependencies` = 528, `code_embeddings` = 0.
- The real `codepulse.db` was **not modified, read for test purposes, or indexed against** during Phase 0.10. Every new test uses a `tmp_path`-based temporary SQLite database and a `tmp_path`-based temporary repository directory.

## Test State

- Last full suite result: **99 passed, 0 failed, 13 warnings** (`.venv/Scripts/python.exe -m pytest tests/ -v`), ~167s.
- Focused Phase 0.10 tests: `tests/test_restart_idempotency.py` (4 new tests, real embedding model via a module-scoped shared `CodeEmbedder`, ~32s standalone once the model is warm): `test_repeated_reindex_is_idempotent_and_identifiers_stay_stable`, `test_restart_restores_semantic_state_but_not_lexical_or_graph_state`, `test_hydration_never_calls_the_embedding_model`, `test_repository_isolation_holds_after_restart`.
- All 95 pre-existing tests re-run unchanged and pass, confirming no regression to Phase 0.3–0.9 behavior (config precedence, Alembic, repository/file/symbol/embedding upsert, hydration, repository-scoped search, no fabricated metrics, ignore-rule discovery, CORS defaults, dependency state).
- No existing test was weakened, skipped, or deleted. No production code was changed.
- Test-duration note: the 4 new tests took ~32s standalone (dominated by real `SentenceTransformer` model loads/encodes, consistent with every other Phase 0.5/0.6-era test file's cost); no significant impact on the full-suite duration (~167s total vs. ~145-166s in recent prior phases, within normal variance).
- Known warnings: `StarletteDeprecationWarning` (httpx/TestClient) and `FutureWarning`s from `tree_sitter` — pre-existing, not addressed.

## Known Issues

**Roadmap work remaining:** Phase 0 (0.1–0.10) is now fully implemented. Next is **Phase 1 — Production Core** (API keys + central authorization, real `IndexJob` state machine, rate limiting, ingestion safety, structured error responses) — not started, not in scope for this session.

**Known technical debt (unchanged from Phase 0.9, re-verified still accurate):**
- `StorageManager.create_tables()` still unconditionally calls `Base.metadata.create_all()` alongside Alembic.
- `SymbolType.VARIABLE` / `SymbolType.INTERFACE` enum values defined but never produced by either parser.
- Dead/unused `job_id` generation in `api/app.py` — Phase 1 (`IndexJob`) scope.
- Dependency (call graph edge) rows still duplicate on every re-index (Phase 3 scope).
- `languages` breakdown (`get_repository_stats`) and the "repo not found" fallback counts (`get_index_status`) are not repository-scoped (Phase 0.7 finding, still unfixed).
- **`BM25Index`/`CallGraphBuilder` have no durable backing and do not survive restart** — now explicitly regression-tested (Phase 0.10), not just documented in prose.
- `IndexingEngine.search(search_type="semantic"/"hybrid")` still does not catch `EmbeddingUnavailableError` for the query-side embed call.
- No `/health/live` or `/health/ready` endpoint exists yet.
- No stale-data cleanup for previously-indexed files that become newly ignored (Phase 0.8 finding).
- No nested/per-directory `.gitignore`/`.codepulseignore` support (Phase 0.8 finding).
- No file-size or file-type caps (Phase 0.8 finding).
- `prometheus-client`/`structlog` remain declared-but-unused (Phase 0.9 finding, deferred to Phase 7).
- No `/health/ready` endpoint (Phase 0.9 finding, deferred to Phase 7).
- **New from Phase 0.10 (not a defect, a scope note):** the new restart tests prove Restart Model B (closed connection + fresh engine object) survives correctly. They do NOT prove true OS-level process-kill survivability (Model C) — if a future milestone needs that guarantee tested literally, it will require new subprocess-based test infrastructure not built here.

**Blockers:** none currently known for starting Phase 1.

## Deferred Work

Per roadmap §39 — do not implement until the roadmap explicitly reaches these:

- Kubernetes, Redis, distributed workers, microservices
- Enterprise SSO, full organization/user tenancy
- Learned/adaptive ranking, LLM reranking, LLM-generated explanations as source of truth
- PostgreSQL/pgvector, Tantivy-backed lexical index, embedded ANN vector index (Phase 2)
- `RepositorySnapshot`, `IndexJob`, `APIKey`, authentication/authorization, real `SearchFeedback` persistence (Phase 1/3)
- Call-graph confidence scoring, impact-analysis depth bucketing, module clustering, stale-data cleanup for newly-ignored files (Phase 3/4)
- MCP server (Phase 5)
- VS Code extension, Web UI (Phase 6)
- Observability infrastructure (Phase 7)

## Current Risks

- `_persist_to_db` still has four upsert blocks plus the dependency-insert block in one function/session — unchanged by Phase 0.10.
- The `languages`/fallback-counts repository-isolation gap (Phase 0.7 finding) remains unfixed.
- Only `repositories`, `files`, `symbols`, `code_embeddings` are idempotent; `dependencies` is not (Phase 3 scope) — Phase 0.10 confirmed this remains true and did not attempt to fix it (out of scope).
- Whoever starts Phase 1 should re-verify this document against the actual repository per CLAUDE.md §13 before trusting it, as always.

## Exact Next Action

NEXT ACTION:
Begin Phase 1 — Production Core, starting with its first sprint item per the authoritative roadmap (§37): API key model + hashing + creation flow (1.1), followed by the central authorization dependency (1.2). Phase 0 (0.1–0.10) is now complete and should not be revisited opportunistically.

## Context Handoff Notes

Phase 0.10 implementation is complete and verified in this session:

- What was done: audited existing test coverage across `test_repository_file_upsert.py`, `test_symbol_upsert.py`, `test_embedding_persistence.py`, `test_migrations.py`, and `test_repository_scoped_search.py` first (per the milestone's explicit "audit before editing" instruction), confirmed it was already extensive, and identified exactly four gaps (combined multi-entity idempotency with ID stability across repeated re-indexing; explicit lexical/graph-does-NOT-survive-restart assertion; hydration-never-calls-the-model proof; repository isolation specifically after a restart). Added `tests/test_restart_idempotency.py` with exactly those four tests, using Restart Model B (dispose + fresh engine) precisely defined and documented, not claimed as process-level restart.
- What was verified: full suite 99/99 passed; real `codepulse.db` confirmed completely untouched (revision `49572f834366`, row counts 1/18/128/528/0, unchanged before and after — no test read from or wrote to it at all).
- One test-authoring bug was found (asserting on an unpopulated `docstring` field for semantic results) and fixed within the same new test before it was ever committed as "existing" — this was NOT a production defect; verified directly against a real run first, confirming isolation actually worked correctly (`snippet`/`doc_id` were correct) before concluding the assertion, not the implementation, was wrong.
- **No production code was changed in this milestone.** This is purely additive test coverage.
- No Alembic migration was created (none needed; not even considered relevant).
- What remains: **committing this work to git** (not yet done as of this document update — the user's instructions for this milestone explicitly said not to commit), then starting Phase 1.
- Exact files changed this session (uncommitted at time of writing): `tests/test_restart_idempotency.py` (new); this file updated. No other file touched.
- Exact tests run: `.venv/Scripts/python.exe -m pytest tests/ -v` → 99 passed, 0 failed.
- Exact next step: this is the end of Phase 0 as scoped by the roadmap's own sprint list (§37, items 0.1–0.10, all now complete). The next work is Phase 1 — Production Core (§37 items 1.1–1.7: API keys, central authorization, real IndexJob state machine, rate limiting, ingestion safety, structured errors, security test suite) — follow CLAUDE.md §5's roadmap-discipline checklist and re-read roadmap §15/§30 (security architecture, central authorization) before starting 1.1.
