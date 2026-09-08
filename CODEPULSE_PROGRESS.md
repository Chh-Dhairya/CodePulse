# CodePulse Development Progress

## Last Updated

2026-09-08

## Current Phase

Phase 0 — Foundation / Correctness

## Current Milestone

None actively in progress. Awaiting start of Phase 0.10 — Restart-survivability + idempotency test suite.

## Last Completed Milestone

Phase 0.9 — Fix CORS policy defaults; trim `pyproject.toml`; add `rich` (verified COMPLETE)

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
| Phase 0.9 — CORS defaults + `pyproject.toml` trim + `rich` | COMPLETE | 95/95 full suite passed (86 pre-existing + 9 new); no migration required; verified against the real `codepulse.db` (revision unchanged) | `8f77aaf` |
| Phase 0.10 — Restart-survivability + idempotency test suite | NOT IMPLEMENTED | — | — |

Only mark a milestone COMPLETE here after actual verification (tests run, diff inspected, roadmap cross-checked) — not because code merely exists.

## Current Implementation State

- **CORS policy is now secure by default (Phase 0.9).** `CodePulseConfig.cors_origins` default changed from `["*"]` to `[]` (deny all cross-origin browser requests by default); `cors_allow_credentials` default changed from `True` to `False`. This directly fixes the exact misconfiguration the roadmap flagged (§3-C.3): `allow_origins=["*"]` combined with `allow_credentials=True` is not just an "unconsidered default" but a combination browsers themselves reject for credentialed requests, per the CORS spec.
- **A new `@model_validator` on `CodePulseConfig` permanently rejects the invalid combination** (`"*"` in `cors_origins` together with `cors_allow_credentials=True`), raising a clear `ValidationError` — this guards the *setting itself*, not just its default, so a future TOML/env override can't silently reintroduce the exact flagged misconfiguration. A non-credentialed wildcard (`cors_origins=["*"]`, `cors_allow_credentials=False`) remains a legal, permissive-but-valid configuration.
- **The existing Phase 0.1 configuration system was reused unchanged** — no new config mechanism, no new precedence layer. `cors_origins`/`cors_allow_credentials` were already wired through `CLI > env (CODEPULSE_CORS_ORIGINS/CODEPULSE_CORS_ALLOW_CREDENTIALS) > TOML ([server] section) > defaults`; only the *default* values (and the new validator) changed. Anyone who needs browser-based cross-origin access (e.g. a future Web UI) configures specific origin(s) through this same, already-established system.
- **`rich` is now an explicit declared dependency** (`pyproject.toml`) — it was already used throughout `codepulse/cli.py` (Console, Panel, Progress, Table, Tree, Syntax) but was never declared; the CLI only worked because `rich` happened to already be present in the dev `.venv` transitively. **No new CLI integration was added** — the roadmap's own audit note (§3-C.2) is exactly "declared but not imported anywhere in the manifest," not "not used at all," so declaring it was the entire fix. `cli.py` itself is unchanged.
- **`pyproject.toml` dependency trim, exactly matching the roadmap's explicit instruction (§34 migration table):** removed `redis`, `pgvector`, `psycopg[binary]` (none imported anywhere in `codepulse/`, `tests/`, or `alembic/` — confirmed by repository-wide search; none were even installed in the dev `.venv`, so this has zero effect on the current environment and only changes what a fresh `pip install -e .` pulls in going forward). `alembic` was **kept** (the roadmap's own conditional — "alembic-if-unused-still" — no longer applies; it's been genuinely used since Phase 0.2). `tantivy` was **kept** despite being currently unused, per the roadmap's own explicit instruction ("keep tantivy, finally use it") — it's earmarked for Phase 2's lexical-index replacement, not a Phase 0.9 removal target.
- **`prometheus-client`, `httpx`, `structlog` were deliberately left untouched** — none appear in the roadmap's explicit Phase 0.9 removal list (only `redis`/`pgvector`/`psycopg[binary]` are named), so removing them would have been unrequested dependency modernization beyond this milestone's scope. `httpx` is a genuine transitive runtime requirement of FastAPI's `TestClient` today; `prometheus-client`/`structlog` remain declared-but-unused, to be activated in Phase 7.
- `pip check` reports no broken requirements after the trim; `redis`/`psycopg`/`pgvector` were confirmed absent from the dev `.venv` (never installed), and `rich` confirmed already installed and working (version 15.0.0, matching the pin added).

## CORS Behavior — Before / After

| | Before (pre-0.9) | After (Phase 0.9) |
|---|---|---|
| `cors_origins` default | `["*"]` | `[]` (deny all by default) |
| `cors_allow_credentials` default | `True` | `False` |
| Wildcard + credentials together | Allowed to be configured (spec-invalid, silently broken for credentialed requests) | **Rejected at config load** with a clear `ValidationError`, regardless of how it's set (default, TOML, env, or CLI) |
| Configuration mechanism | Phase 0.1 config system (`CLI > env > TOML > defaults`) | **Unchanged** — same mechanism, only defaults + validation changed |

**Compatibility impact:** any deployment that was implicitly relying on the old wildcard-open default (e.g., calling the API cross-origin from an arbitrary browser page without ever setting `cors_origins`) will now see CORS-blocked requests until `cors_origins` is explicitly configured. This is the intended, security-motivated behavior change the roadmap calls for, not an accidental regression. No API endpoint, payload, or route changed.

## Database State

- `codepulse.db` exists at the repository root, untracked by git.
- **Current Alembic revision (real `codepulse.db`): `49572f834366`** — **unchanged by Phase 0.9**. No migration was created or needed: CORS defaults, dependency trimming, and declaring `rich` are not schema concerns.
- Row counts (unchanged since Phase 0.3, re-verified this session): `repositories` = 1, `files` = 18, `symbols` = 128, `dependencies` = 528, `code_embeddings` = 0.
- The real `codepulse.db` was **not modified** during Phase 0.9. No test indexed anything or wrote to it; all CORS tests use either the real (already-constructed, read-only-from-a-DB-perspective) API app or a lightweight standalone FastAPI app with no database at all.

## Test State

- Last full suite result: **95 passed, 0 failed, 13 warnings** (`.venv/Scripts/python.exe -m pytest tests/ -v`), ~145s.
- Focused Phase 0.9 tests: `tests/test_cors.py` (9 new tests, verifying real HTTP response headers, not source constants): default policy denies all origins (both a plain GET and an OPTIONS preflight against the real running app); default response carries no `access-control-allow-credentials` header; a configured specific origin is allowed (header echoes it back); a non-configured origin is denied; credentials header present/absent correctly tracking the configured flag; the wildcard+credentials combination raises `ValidationError` at config construction; a non-credentialed wildcard remains valid and is honored.
- `tests/test_config.py::test_builtin_defaults_loaded` updated (2 assertions: `cors_origins == []`, `cors_allow_credentials is False`) with the change documented in the test's own docstring — this is the one existing test that encoded the old insecure defaults; not a weakened test, it now asserts the roadmap-required secure default. All other `test_config.py` tests were already testing explicit overrides (TOML/env), unaffected by the default change, and pass unchanged.
- All other pre-existing tests re-run unchanged and pass, confirming no regression to Phase 0.3-0.8 behavior.
- No test was weakened, skipped, or deleted.
- Known warnings: `StarletteDeprecationWarning` (httpx/TestClient) and `FutureWarning`s from `tree_sitter` — pre-existing, not addressed.

## Known Issues

**Roadmap work remaining (Phase 0, not yet implemented):**
- 0.10 Restart-survivability + idempotency test suite (beyond what 0.3-0.9's own focused tests cover)

**Known technical debt:**
- `StorageManager.create_tables()` still unconditionally calls `Base.metadata.create_all()` alongside Alembic.
- `SymbolType.VARIABLE` / `SymbolType.INTERFACE` enum values defined but never produced by either parser.
- Dead/unused `job_id` generation in `api/app.py` — Phase 1 (`IndexJob`) scope.
- Dependency (call graph edge) rows still duplicate on every re-index (Phase 3 scope).
- `languages` breakdown (`get_repository_stats`) and the "repo not found" fallback counts (`get_index_status`) are not repository-scoped (Phase 0.7 finding, still unfixed).
- `BM25Index`/`CallGraphBuilder` are still in-memory-only with no durable backing.
- `IndexingEngine.search(search_type="semantic"/"hybrid")` still does not catch `EmbeddingUnavailableError` for the query-side embed call.
- No `/health/live` or `/health/ready` endpoint exists yet.
- No stale-data cleanup for previously-indexed files that become newly ignored (Phase 0.8 finding).
- No nested/per-directory `.gitignore`/`.codepulseignore` support (Phase 0.8 finding).
- No file-size or file-type caps (Phase 0.8 finding).
- **New from Phase 0.9:** `prometheus-client` and `structlog` remain declared in `pyproject.toml` but genuinely unused anywhere in the codebase — deliberately not removed (not in the roadmap's explicit Phase 0.9 removal list) and not yet activated (that's Phase 7's observability wiring). Anyone auditing "declared vs. actually used" dependencies should expect these two specifically until Phase 7.
- **New from Phase 0.9:** no `/health/ready` endpoint exists to report "DB reachable, embedding model loaded, index storage writable" as described in roadmap §18 — unrelated to CORS/dependencies but worth noting since Phase 0.9 touched the API app's middleware setup; not built here (Phase 7 scope, deliberately not opportunistically added).

**Blockers:** none currently known for starting Phase 0.10.

## Deferred Work

Per roadmap §39 — do not implement until the roadmap explicitly reaches these:

- Kubernetes, Redis, distributed workers, microservices
- Enterprise SSO, full organization/user tenancy
- Learned/adaptive ranking, LLM reranking, LLM-generated explanations as source of truth
- PostgreSQL/pgvector, Tantivy-backed lexical index (finally *using* the already-kept `tantivy` dependency), embedded ANN vector index (Phase 2)
- `RepositorySnapshot`, `IndexJob`, `APIKey`, authentication/authorization, real `SearchFeedback` persistence (Phase 1/3)
- Call-graph confidence scoring, impact-analysis depth bucketing, module clustering, stale-data cleanup for newly-ignored files (Phase 3/4)
- MCP server (Phase 5)
- VS Code extension, Web UI (Phase 6)
- Observability infrastructure (`structlog`/`prometheus-client` activation, health endpoints) — Phase 7

## Current Risks

- `_persist_to_db` still has four upsert blocks plus the dependency-insert block in one function/session — unchanged by Phase 0.9.
- The `languages`/fallback-counts repository-isolation gap (Phase 0.7 finding) remains unfixed.
- Only `repositories`, `files`, `symbols`, `code_embeddings` are idempotent; `dependencies` is not (Phase 3 scope).
- Any external tooling/scripts that assumed the old wildcard-open CORS default (calling the API cross-origin from a browser without configuring `cors_origins`) will now be blocked until `cors_origins` is explicitly configured — this is intentional (see CORS Behavior table above), but worth flagging for whoever operates a deployment across this change.

## Exact Next Action

NEXT ACTION:
Implement Phase 0.10 — Restart-survivability + idempotency test suite, according to the authoritative roadmap (roadmap §20 "Restart tests: index → kill process → restart → search must return the same results," and the Phase 0 Definition of Done: "indexing the same repo twice produces zero duplicate rows; killing and restarting the server preserves semantic search results; no endpoint returns a hardcoded number" — the first two clauses are individually exercised by existing Phase 0.3-0.5 focused tests already, but Phase 0.10 is the roadmap's own explicit milestone for a dedicated suite proving this end-to-end, beyond what any single prior phase's tests cover in isolation).

## Context Handoff Notes

Phase 0.9 implementation is complete, verified, and committed (`8f77aaf`):

- What was done: audited CORS wiring (confirmed already routed through the Phase 0.1 config system, `allow_origins=["*"]` + `allow_credentials=True` default confirmed as the exact flagged invalid/insecure combination), pyproject.toml's complete dependency list, and rich's actual usage in `cli.py` (already used, never declared) before making any change. Changed `CodePulseConfig.cors_origins` default to `[]` and `cors_allow_credentials` default to `False`; added a `@model_validator` rejecting the wildcard+credentials combination permanently (not just by default); removed `redis`/`pgvector`/`psycopg[binary]` from `pyproject.toml` (confirmed unused and not even installed); kept `alembic`/`tantivy` per the roadmap's explicit instruction; added `rich>=15.0.0,<16.0.0` as an explicit dependency (no CLI code changes); added `tests/test_cors.py` (9 new tests against real HTTP response headers); updated `tests/test_config.py`'s one affected default-value assertion with the change documented in its own docstring.
- What was verified: full suite 95/95 passed (re-confirmed again at commit time); `pip check` clean; `redis`/`psycopg`/`pgvector` confirmed absent from the dev `.venv` (never installed, zero environment impact); real `codepulse.db` confirmed unchanged (revision `49572f834366`, row counts 1/18/128/528/0) before and after.
- No Alembic migration was created (none needed).
- Committed as `8f77aaf` (5 files changed: `codepulse/config.py`, `pyproject.toml`, `tests/test_config.py` modified; `tests/test_cors.py` new; this file updated).
- Exact tests run: `.venv/Scripts/python.exe -m pytest tests/ -v` → 95 passed, 0 failed.
- Exact next step: follow CLAUDE.md §5's roadmap-discipline checklist for Phase 0.10, starting with re-reading roadmap §20's "Restart tests" and the Phase 0 Definition of Done, and identifying exactly which restart/idempotency scenarios are NOT already covered by existing Phase 0.3-0.5 focused tests before designing new ones (to avoid duplicating coverage that already exists).
