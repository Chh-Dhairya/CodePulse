# CodePulse Development Progress

## Last Updated

2026-09-05

## Current Phase

Phase 0 — Foundation / Correctness

## Current Milestone

None actively in progress. Awaiting start of Phase 0.8 — `.gitignore`/`.codepulseignore` discovery and ignore-rule handling.

## Last Completed Milestone

Phase 0.7 — Remove all hardcoded metric values from API responses (verified COMPLETE)

## Completed Milestones

| Milestone | Status | Verification | Commit |
|---|---|---|---|
| B1 — Config system | COMPLETE | 7/7 tests in `tests/test_config.py` | `101607d` |
| Alembic migration infrastructure | IMPLEMENTED | 3/3 tests in `tests/test_migrations.py` | `4ccba7a` |
| Phase 0.3 — Repository/File Upsert + content_hash | COMPLETE | 41/41 full suite passed; migration applied to real `codepulse.db` | `571a21d` |
| Phase 0.4 — Symbol Upsert by `(file_id, name, start_line)` | COMPLETE | 49/49 full suite passed; migration applied to real `codepulse.db` | `e0b3ec0` |
| Phase 0.5 — Embedding Persistence + Hydration | COMPLETE | 58/58 full suite passed; migration applied to real `codepulse.db` | `50dbe22` |
| Phase 0.6 — Repository-scoped search (remove global singleton) | COMPLETE | 69/69 full suite passed; no migration required | `aea7518` |
| Phase 0.7 — Remove fabricated metric values from API responses | COMPLETE | 74/74 full suite passed (69 pre-existing + 5 new); no migration required; verified against the real `codepulse.db` read-only | *(uncommitted as of this update — see Exact Next Action)* |
| Phase 0.8 — `.gitignore`/`.codepulseignore` discovery | NOT IMPLEMENTED | — | — |

Only mark a milestone COMPLETE here after actual verification (tests run, diff inspected, roadmap cross-checked) — not because code merely exists.

## Current Implementation State

- **Fabricated metrics removed from `codepulse/api/app.py` (Phase 0.7):**
  - `index_size_mb` (hardcoded `12`/`15`) — removed from both branches of `GET /v1/repositories/{id}/index-status`. No on-disk/in-memory index-footprint measurement exists anywhere in the system.
  - `estimated_time_seconds` (hardcoded `10`) — removed from `POST /v1/repositories/index`. No timing model or historical job-duration data exists.
  - `total_queries` (hardcoded `100`) and `avg_search_latency_ms` (hardcoded `45`) — removed from `GET /v1/repositories/{id}/stats`. No query-log or latency-aggregation table exists (real query analytics is a later roadmap milestone, `SearchFeedback`/B28, Phase 1 — not built here).
  - `POST /v1/search/feedback`'s response previously claimed `"status": "recorded"` and `"message": "...Weights updated."` while writing nothing anywhere and touching no ranking weights — a fabricated success/effect claim, not just a numeric one. Changed to `"status": "received"` and an honest message ("Feedback received. Not yet persisted or used to adjust ranking.") that doesn't claim an effect that didn't happen. No new persistence was built (that's B28/Phase 1 — explicitly out of scope here).
- **Legitimate metrics preserved, verified as genuinely measured/computed, not touched:** `execution_time_ms` (search + ide-search — real `time.time()` delta around the actual `search()` call), all ranker output fields (`final_score`/`keyword_score`/`vector_score`/`embedding_distance`/`graph_score` — real Phase 0.6 hybrid ranker output), `complexity` (real static-analysis branch count from the parser), `similarity`/`relevance_score` in find_similar_code (real cosine similarity), `direct_callers`/`indirect_callers` in impact-analysis (real graph-traversal counts — heuristic accuracy is a separate, already-documented Phase 3 concern, not fabrication), `embedding_model` (a configured value, not a metric — correctly left alone).
- **`job_id`** (`api/app.py`'s `index_repository`) was deliberately left untouched — not a numeric metric (an identifier), and already separately categorized in this project's own audit history as a distinct "fake job ID" issue tied to Phase 1's real `IndexJob` model (B9), not Phase 0.7's "fabricated metrics" (B5). Building real job tracking now would be Phase-1 scope creep.
- **`"last_indexed": "live"`** in the index-status fallback branch was left untouched — an honest placeholder signaling "this came from live memory, not a stored timestamp," not an invented measurement.
- **Discovered but explicitly NOT fixed (documented, not silently expanded into this milestone):** the `languages` breakdown in `get_repository_stats` and the fallback counts in `get_index_status`'s "repo not found" branch are computed from `indexing_engine.parse_results`/`symbols_by_id` — global, unscoped structures that Phase 0.6's repository-scoping work did not reach (Phase 0.6 only tagged `bm25_index`/`vector_index`/`call_graphs`). These values are **real, computed counts** (not fabricated — no invented/placeholder/hardcoded numbers involved), just potentially cross-repository-contaminated in a multi-repo process. This is a repository-isolation gap, not a fabricated-metric issue, and fixing it would require the same kind of tagging work Phase 0.6 did for the search structures — out of Phase 0.7's scope. Flagged here per CLAUDE.md §12 ("record in CODEPULSE_PROGRESS.md rather than silently expanding scope").
- No API endpoint was added, removed, renamed, or had its URL/HTTP-method contract changed. No response field was added. Only fabricated fields were removed, and two string values (feedback's `status`/`message`) were corrected.

## Database State

- `codepulse.db` exists at the repository root, untracked by git.
- **Current Alembic revision (real `codepulse.db`): `49572f834366`** — **unchanged by Phase 0.7**. No migration was created or needed: removing hardcoded response literals and correcting two string values requires no schema change.
- Row counts (unchanged since Phase 0.3, re-verified this session): `repositories` = 1, `files` = 18, `symbols` = 128, `dependencies` = 528, `code_embeddings` = 0.
- The real `codepulse.db` was **not modified** during Phase 0.7. All verification was either fully read-only (`GET` endpoints, queried with a deliberately nonexistent `repository_id` so no dependency on the real DB's specific content) or performed against a temporary, disposable database via monkeypatching the API module's global `storage_manager`/`indexing_engine` for the one test (`POST /v1/repositories/index`) that would otherwise have written to the real DB (FastAPI's `TestClient` runs `BackgroundTasks` synchronously, so that endpoint's background indexing call actually executes during the test). The swap was verified to have actually redirected the write (confirmed present in the temp DB), and the real `codepulse.db`'s row counts were re-checked identical before and after the full test run.

## Test State

- Last full suite result: **74 passed, 0 failed, 13 warnings** (`.venv/Scripts/python.exe -m pytest tests/ -v`), ~156s.
- Focused Phase 0.7 tests: `tests/test_no_fabricated_metrics.py` (5 tests, calling the real FastAPI endpoints via `TestClient` and inspecting actual JSON responses, not just source code): `index_size_mb` absent from index-status; `total_queries`/`avg_search_latency_ms` absent from stats; `estimated_time_seconds` absent from the index-repository response (verified via a monkeypatched temp DB, confirmed the actual write landed there and not in the real `codepulse.db`); the feedback endpoint no longer claims `"recorded"` or "weights updated"; `execution_time_ms` is confirmed to remain present and be a real non-negative measured value (locking in its "legitimate metric" classification, not accidentally removed).
- Updated existing test: `tests/test_api.py::test_api_feedback_endpoint` — previously asserted `status == "recorded"` (which encoded the exact fabricated claim this milestone removes); now asserts `status == "received"` and that the response no longer says "weights updated." This is a documented, deliberate API-contract compatibility change (see below), not a weakened test — it now asserts a strictly more truthful contract.
- All other pre-existing tests re-run unchanged and pass, confirming no regression to Phase 0.6 repository-scoping, Phase 0.5 hydration/no-pseudo-embedding-fallback, or Phase 0.3/0.4 upsert behavior.
- No test was skipped or deleted.
- Known warnings: `StarletteDeprecationWarning` (httpx/TestClient) and `FutureWarning`s from `tree_sitter` — pre-existing, not addressed.

## Known Issues

**Roadmap work remaining (Phase 0, not yet implemented):**
- 0.8 `.gitignore`/`.codepulseignore`-respecting file discovery
- 0.9 Fix CORS policy defaults; trim `pyproject.toml`; add `rich`
- 0.10 Restart-survivability + idempotency test suite (beyond what 0.3-0.7's own focused tests cover)

**Known technical debt:**
- `StorageManager.create_tables()` still unconditionally calls `Base.metadata.create_all()` alongside Alembic.
- `SymbolType.VARIABLE` / `SymbolType.INTERFACE` enum values defined but never produced by either parser.
- Dead/unused `job_id` generation in `api/app.py` — Phase 1 (`IndexJob`) scope, deliberately not touched in Phase 0.7 (see Current Implementation State).
- Dependency (call graph edge) rows still duplicate on every re-index (Phase 3 scope).
- **`languages` breakdown (`get_repository_stats`) and the "repo not found" fallback counts (`get_index_status`) are not repository-scoped** — computed from global `indexing_engine.parse_results`/`symbols_by_id`, which Phase 0.6 didn't tag with `repo_id` (only `bm25_index`/`vector_index`/`call_graphs` were). In a multi-repository process, these values can reflect more than the requested repository. Discovered during Phase 0.7's audit; not fabrication (real computed counts), so intentionally left unfixed here — flagged for a future, explicitly-scoped fix.
- `BM25Index`/`CallGraphBuilder` are still in-memory-only with no durable backing — only embeddings/semantic search survive a real process restart.
- `IndexingEngine.search(search_type="semantic"/"hybrid")` still does not catch `EmbeddingUnavailableError` for the query-side embed call (noted since Phase 0.5, unaffected by Phase 0.7).
- No `/health/live` or `/health/ready` endpoint exists at all yet (described in the roadmap's §18 design but never implemented) — nothing to audit there for Phase 0.7; noted for whichever future milestone builds it, so it's built honest from the start.

**Blockers:** none currently known for starting Phase 0.8.

## API Compatibility Impact (Phase 0.7)

- `GET /v1/repositories/{id}/index-status`: response no longer includes `index_size_mb`. Any consumer reading that field will now get a missing key instead of a fake number.
- `POST /v1/repositories/index`: response no longer includes `estimated_time_seconds`.
- `GET /v1/repositories/{id}/stats`: response no longer includes `total_queries` or `avg_search_latency_ms`.
- `POST /v1/search/feedback`: `status` value changed from the literal `"recorded"` to `"received"`; `message` text changed from `"Thank you for feedback. Weights updated."` to `"Feedback received. Not yet persisted or used to adjust ranking."` Any consumer doing an exact string match on either value will need updating — this is the one behavior-visible contract change in this milestone, made because the old values were factually false, not a redesign.
- No endpoint's URL, HTTP method, request schema, or status code changed. No field was added.

## Deferred Work

Per roadmap §39 — do not implement until the roadmap explicitly reaches these:

- Kubernetes, Redis, distributed workers, microservices
- Enterprise SSO, full organization/user tenancy
- Learned/adaptive ranking, LLM reranking, LLM-generated explanations as source of truth
- PostgreSQL/pgvector, Tantivy-backed lexical index, embedded ANN vector index (Phase 2)
- `RepositorySnapshot`, `IndexJob`, `APIKey`, authentication/authorization, real `SearchFeedback` persistence (Phase 1/3)
- Call-graph confidence scoring, impact-analysis depth bucketing, module clustering (Phase 3/4)
- MCP server (Phase 5)
- VS Code extension, Web UI (Phase 6)
- Observability infrastructure (`structlog`/`prometheus-client` wiring, health endpoints) — Phase 7

## Current Risks

- `_persist_to_db` still has four upsert blocks plus the dependency-insert block in one function/session — unchanged by Phase 0.7, still worth splitting up eventually.
- The `languages`/fallback-counts repository-isolation gap noted above is real and pre-dates this audit having found it — anyone building on `get_repository_stats` or the index-status fallback branch in a multi-repo deployment should be aware the numbers can be wrong until that's fixed.
- Only `repositories`, `files`, `symbols`, `code_embeddings` are idempotent; `dependencies` is not (Phase 3 scope).

## Exact Next Action

NEXT ACTION:
Implement Phase 0.8 — `.gitignore`/`.codepulseignore`-respecting file discovery, according to the authoritative roadmap (`codepulse/indexer/engine.py`'s file-discovery loop currently only filters via hardcoded `IGNORE_DIRS`/`IGNORE_EXTENSIONS` sets; no `.gitignore` or `.codepulseignore` parsing exists at all).

## Context Handoff Notes

Phase 0.7 implementation is complete and verified in this session:

- What was done: audited every field in `codepulse/api/app.py`'s 12 endpoints (confirmed to be the only FastAPI app/API surface in the codebase) against a 5-way classification (legit/configured/fabricated/unavailable/ambiguous); removed `index_size_mb`, `estimated_time_seconds`, `total_queries`, `avg_search_latency_ms`; corrected `/v1/search/feedback`'s false `"recorded"`/"weights updated" claims to honest `"received"`/"not yet persisted" wording; added `tests/test_no_fabricated_metrics.py` (5 new tests against real HTTP responses); updated `tests/test_api.py`'s one affected assertion with the change documented in its own docstring.
- What was verified: full suite 74/74 passed; the one test that would otherwise have written to the real `codepulse.db` (`POST /v1/repositories/index`, since `TestClient` runs `BackgroundTasks` synchronously) was safely isolated via monkeypatching the app module's `storage_manager`/`indexing_engine` to a temp DB, with the redirect explicitly confirmed; real `codepulse.db` re-verified unchanged (revision `49572f834366`, row counts 1/18/128/528/0) before and after the full run.
- No Alembic migration was created (none needed).
- What remains: **committing this work to git** (not yet done as of this document update — the user's instructions for this milestone explicitly said not to commit), then starting Phase 0.8.
- Exact files changed this session (uncommitted at time of writing): `codepulse/api/app.py`, `tests/test_api.py` (both modified); `tests/test_no_fabricated_metrics.py` (new); this file updated.
- Exact tests run: `.venv/Scripts/python.exe -m pytest tests/ -v` → 74 passed, 0 failed.
- Exact next step: follow CLAUDE.md §5's roadmap-discipline checklist for Phase 0.8, starting with re-reading roadmap §16 (secrets/ignore-file handling design) and B6's definition of done ("ignored files never appear in the index").
