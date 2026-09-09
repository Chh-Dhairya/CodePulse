# CodePulse Development Progress

## Last Updated

2026-09-09

## Current Phase

Phase 1 — Production Core (fourth sprint item complete)

## Current Milestone

None actively in progress. **Phase 1.4 is complete and verified.** Awaiting explicit instruction to begin Phase 1.5 (ingestion safety: path allow-list, SSRF check, size caps) — do not start it opportunistically.

## Last Completed Milestone

Phase 1.4 — In-process token-bucket rate limiter (verified COMPLETE)

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
| Phase 0.10 — Restart-survivability + idempotency test suite | COMPLETE | 99/99 full suite passed (95 pre-existing + 4 new); no production code changed; no migration required; real `codepulse.db` untouched | `4dd890d` |
| Phase 1.1 — API key model + hashing + creation flow | COMPLETE | 112/112 full suite passed (99 pre-existing + 13 new); migration applied to real `codepulse.db`; re-verified in isolation (clean checkout, 112/112) before Phase 1.2 began | `190d9bb` |
| Phase 1.2 — Central authorization dependency + endpoint enforcement | COMPLETE | 139/139 full suite passed (112 pre-existing + 27 new); no migration required; real `codepulse.db` untouched | `a3a9b24` (+ docs follow-up `16fa747`) |
| Phase 1.3 — IndexJob state machine + `/v1/jobs/{id}` | COMPLETE | 165/165 full suite passed (139 pre-existing + 26 new); migration applied to real `codepulse.db` (via stamp, see incident note below); re-verified post-commit (clean status, 165/165) | `3feabf8` (+ docs follow-up `c9d27fa`) |
| Phase 1.4 — In-process token-bucket rate limiter | COMPLETE | 179/179 full suite passed (165 pre-existing + 14 new); no migration required; real `codepulse.db` untouched; re-verified post-commit (clean status, 179/179) | `571cf3a` |

Only mark a milestone COMPLETE here after actual verification (tests run, diff inspected, roadmap cross-checked) — not because code merely exists.

## Current Implementation State

### Phase 1.4 — In-process token-bucket rate limiter

- **Scope determined from the roadmap's own text** (§37 item 1.4: "In-process token-bucket rate limiter"; §6 "Recommended Architecture" grouping rate limits into the control plane alongside auth/API keys/job tracking/permissions; §35's architecture diagram placing "Rate limits" directly beside "Auth (API keys)" and "Central authz" in the API Gateway layer; backlog B11: Technical Scope "New middleware", DoD "Excess requests receive 429 with retry-after"; §37/§45 "no Redis yet"):
  - IN scope (implemented): a classic in-memory token-bucket algorithm (`codepulse/rate_limit.py`'s `TokenBucketLimiter` -- capacity + refill rate, per-identity buckets, thread-safe via a single lock); a new FastAPI middleware in `codepulse/api/app.py` applying it to every request, keyed by the raw `X-API-Key` header value (or the client's IP as a fallback for unauthenticated requests); a real, non-fabricated `Retry-After` header (computed from the actual token deficit and refill rate) on every 429; three new `CodePulseConfig` fields (`rate_limit_enabled`, `rate_limit_requests_per_second`, `rate_limit_burst`) so the limiter is fully config-driven, matching the project's established config precedence (CLI > env > TOML > defaults) rather than hardcoded constants.
  - OUT of scope (explicitly NOT implemented, reserved for later milestones or never assigned to this one): per-role/per-plan differentiated limits (roadmap §15's RBAC is Phase 7 / B25, not modeled at all); distributed/shared rate limiting across multiple processes (explicitly deferred -- "no Redis yet"); a structured `{"error": {...}}` response envelope for the 429 body (Phase 1.6 -- the 429 body is a plain `{"detail": "..."}`, matching FastAPI's own default `HTTPException` shape and every other error path already in `api/app.py`); ingestion-specific safety limits -- path allow-listing, SSRF checks, size caps (Phase 1.5 / B10, an entirely separate concern from request-rate limiting).
- **Design decisions made explicitly, not inferred from later milestones**:
  1. **Identity = raw API key header value, or client IP as a fallback.** The limiter does NOT verify the key (no DB lookup, no import of `codepulse.auth`/`codepulse.api.security`) -- it buckets on the raw string alone, deliberately cheap and independent of validity checking, which remains exactly `require_api_key`'s job (Phase 1.2), unchanged and un-duplicated. This means an unauthenticated flood still gets throttled (by IP) rather than an unlimited exemption, while distinct real callers get independent budgets.
  2. **Implemented as global middleware, not a per-route `Depends()`** (per backlog B11's own "Technical Scope: New middleware", and because rate limiting must run before -- and independently of -- whether a request would even authenticate, to protect the server from excessive traffic regardless of auth outcome).
  3. **Registered BEFORE `CORSMiddleware`** in `codepulse/api/app.py`, so that (per Starlette's "last-added-wraps-outermost" rule) CORS ends up as the outer layer and a 429 rejection still gets real CORS headers attached on its way back to the client -- exactly like a 401/403 auth rejection already does. Verified empirically (not just reasoned about) via a dedicated test against a minimal app with an explicitly-configured allowed origin, since the real global app's own CORS config defaults to deny-all and is frozen at first import (same reason `tests/test_cors.py` already tests *configured* CORS behavior against a separate lightweight app).
  4. **Config-driven defaults (5 req/s, burst 20), explicitly NOT presented as a benchmarked/measured figure** -- documented in both the field descriptions and `codepulse/rate_limit.py`'s module docstring as a reasonable *starting* configuration for local-first, single-developer use, mirroring roadmap §19's own caution against presenting unvalidated numbers as fact (applied here by analogy, since no rate-limiting-specific number is given anywhere in the roadmap either).
  5. **Purely in-memory, no persistence** -- "in-process" is read literally: bucket state lives only in the running Python process's memory and resets on restart, matching this project's single-process architecture and the explicit "no Redis yet" deferral. No Alembic migration, no new DB table.
- **Architecture rule preserved**: `codepulse/rate_limit.py` is a new top-level module (not under `codepulse/api/` or `codepulse/indexer/`), mirroring `codepulse/auth.py`/`codepulse/jobs.py`'s placement -- reusable identically by a future CLI or MCP surface, per the roadmap's own "control plane" grouping (§6) and CLAUDE.md §3's "REST, CLI, and MCP must call the same underlying intelligence functions." `IndexingEngine`/`codepulse/indexer/` is completely untouched.
- **Test-isolation addition**: `tests/conftest.py` gained an autouse `_reset_rate_limiter` fixture (project-wide, not just API tests) resetting the limiter's bucket state before/after every test -- without it, the `request.client.host` fallback bucket every *unauthenticated* request across the whole suite shares under Starlette's `TestClient` (a fixed synthetic host string) would persist for the entire pytest process, risking an unrelated later test's "no key -> 401" assertion occasionally observing a 429 instead depending on exactly how much such traffic happened to run beforehand. Resetting between tests removes that class of flakiness entirely rather than relying on wall-clock refill timing to avoid it.

### ⚠️ Incident during migration verification (Phase 1.3, found and fully remediated in that session -- kept here verbatim as accurate project history, not modified by Phase 1.4)

While running an ad-hoc Python one-liner to sanity-check the new routes (`import codepulse.api.app as app_module; ...`), that import's module-level `storage_manager = StorageManager()` executed against the **default** `db_url` (`sqlite:///codepulse.db`, the real database) because the command was run without a `CODEPULSE_DB_URL` override. `StorageManager.__init__` unconditionally calls `Base.metadata.create_all(bind=self.engine)` **alongside** Alembic -- an already-documented, pre-existing piece of technical debt (see Known Issues, tracked since Phase 1.1) -- which silently created the new `index_jobs` table (and its `ix_index_jobs_repo_id` index) directly on the real `codepulse.db`, bypassing Alembic entirely and leaving `alembic_version` stale at `b0bdd63949f4`.

**This was caught immediately**, before the intended migration-safety-verification step even ran, when applying the real Phase 1.3 migration to a disposable copy of `codepulse.db` failed with `table index_jobs already exists` (the copy had been made *after* the accidental creation, so it carried the same table).

**Verified before remediating**: the accidentally-created table's schema (columns, types, nullability, the `ix_index_jobs_repo_id` index, the `repo_id` FK with `ON DELETE CASCADE`) was inspected directly via `PRAGMA table_info`/`sqlite_master` on both the disposable copy and the real database and found **byte-for-byte identical** to what the Alembic migration's `op.create_table` defines -- `Base.metadata.create_all()` uses the exact same `IndexJobModel` SQLAlchemy definition the migration was generated from, so this is expected, not coincidental. The table was empty (0 rows) in both. No pre-existing data (`repositories`, `files`, `symbols`, `dependencies`, `code_embeddings`, `api_keys`, `api_key_repositories`) was touched or altered -- `create_all()` only creates *missing* tables, never modifies existing ones.

**Remediation**: since the schema already matched exactly and held no data, the correct fix was `codepulse.storage.migrations.stamp_database(revision="de22df2fc3ce")` -- Alembic's own mechanism for bringing an already-correct schema under version tracking without re-running DDL -- applied first to the disposable copy (verified), then identically to the real `codepulse.db` (verified). **No data was lost. No destructive operation was performed.**

**Lesson recorded, re-applied this session**: any future ad-hoc Python command that imports `codepulse.api.app` must set `CODEPULSE_DB_URL` to an isolated/disposable path first, or must avoid importing that module directly. During Phase 1.4's own middleware-ordering verification, this lesson was followed: `codepulse.api.app` was imported for a route/middleware sanity check without an isolated DB, but since Phase 1.4 introduced **no new model** to `codepulse/storage/db.py`, `create_all()` had nothing new to create, and the real `codepulse.db` was explicitly re-verified unchanged (schema and all row counts) immediately afterward -- see Database State below.

## Database State

- `codepulse.db` exists at the repository root, untracked by git.
- **Current Alembic revision (real `codepulse.db`): `de22df2fc3ce`** -- **unchanged by Phase 1.4**. No migration was created, needed, or considered: this milestone adds only application-layer/config logic (a middleware and three config fields), no new table or column.
- Row counts, confirmed **unchanged** before, during, and after this milestone: `repositories` = 1, `files` = 18, `symbols` = 128, `dependencies` = 528, `code_embeddings` = 0, `api_keys` = 0, `api_key_repositories` = 0, `index_jobs` = 0. Explicitly re-verified after this session's `codepulse.api.app` sanity-check import (see incident-lesson note above) precisely because that import pattern caused a real (harmlessly-remediated) incident in the immediately preceding milestone.
- The real `codepulse.db` was not written to by any test during this milestone -- every test uses either the direct `TokenBucketLimiter` class (no DB at all) or the shared `isolated_app` fixture (a `tmp_path`-based temporary SQLite database).

## Test State

- Last full suite result: **179 passed, 0 failed, 13 warnings** (`.venv/Scripts/python.exe -m pytest -v`), ~236s.
- New focused Phase 1.4 tests:
  - `tests/test_rate_limit.py` (7 tests): `TokenBucketLimiter` exercised directly, no HTTP, no database -- first request allowed, requests up to burst capacity all allowed, a request beyond capacity denied, a denied request's `retry_after` is real and positive (not zero or fabricated), tokens genuinely refill over real elapsed time (a fast-refill-rate + short-real-sleep test, not a mocked clock), distinct identities have fully independent buckets, `reset()` clears all state.
  - `tests/test_rate_limit_middleware.py` (7 tests): real HTTP behavior against the actual `codepulse.api.app` middleware -- requests within a configured burst all succeed; exceeding it returns 429 with a real positive `Retry-After` header and a `detail` message; two different API keys have fully independent limits (one being throttled never affects the other); unauthenticated requests are rate-limited too (by IP), so an unauthenticated flood cannot bypass the limiter by omitting a key; the limiter can be disabled entirely via `rate_limit_enabled=False`; the real default settings (5 req/s, burst 20) comfortably tolerate ordinary test-sized traffic (a regression guard against the defaults being *too* tight for legitimate interactive use); and -- against a minimal dedicated app mirroring `test_cors.py`'s own established pattern -- a 429 rejection still carries the correct `access-control-allow-origin` header, empirically proving the CORS/rate-limit middleware ordering decision above.
  - `tests/test_config.py` extended (not a new file): `test_builtin_defaults_loaded` now asserts the three new fields' defaults; `test_env_var_type_coercion` now asserts `CODEPULSE_RATE_LIMIT_*` env vars coerce correctly (bool/float/int).
- New shared fixture: `tests/conftest.py`'s autouse `_reset_rate_limiter` (see "Current Implementation State" above) -- applies to the whole suite, not just rate-limit-specific tests.
- All 165 pre-existing tests re-run unchanged and pass, confirming no regression to Phase 0.1-0.10/1.1-1.3 behavior. No existing test needed a request-count or timing change -- the default burst (20) comfortably exceeds every existing test's call volume per identity, and per-test random API keys (`make_api_key_headers` generates a fresh random key every call) mean distinct tests essentially never share a bucket identity even without the reset fixture; the reset fixture is a robustness addition, not a fix for an observed failure.
- No existing test was weakened, skipped, or deleted.
- Known warnings: `StarletteDeprecationWarning` (httpx/TestClient) and `FutureWarning`s from `tree_sitter` -- pre-existing, not addressed (out of scope).

## Known Issues

**Roadmap work remaining:** Phase 1.4 (in-process token-bucket rate limiter) is complete. Next per the roadmap's own sprint order (§37) is **Phase 1.5 — ingestion safety: path allow-list, SSRF check, size caps**, then 1.6-1.7 (structured error responses, security test suite). **None of Phase 1.5+ has been started or implemented — do not infer it exists.**

**Known technical debt (unchanged from Phase 1.3, re-verified still accurate unless noted):**
- `StorageManager.create_tables()` still unconditionally calls `Base.metadata.create_all()` alongside Alembic -- demonstrated risk (Phase 1.3's incident), still not fixed (unrelated to 1.4's literal scope). Recommend prioritizing a fix before it causes a real problem outside a disposable/test context.
- SQLite's `PRAGMA foreign_keys` is never enabled anywhere in this codebase (discovered Phase 1.3), so every `ON DELETE CASCADE` clause declared in the schema is not actually enforced by SQLite itself. No repository-deletion endpoint exists yet, so this has no current user-facing impact.
- `SymbolType.VARIABLE` / `SymbolType.INTERFACE` enum values defined but never produced by either parser.
- `IndexRepoRequest.repository_id` field in `api/app.py` remains dead/unused.
- Dependency (call graph edge) rows still duplicate on every re-index (Phase 3 scope).
- `/v1/autocomplete` and the `languages`/fallback-counts parts of `get_repository_stats`/`get_index_status` are still not repository-scoped in their actual data (Phase 0.7 finding) -- still unfixed, still documented.
- `BM25Index`/`CallGraphBuilder` have no durable backing and do not survive restart (Phase 0.10 finding, regression-tested, not fixed).
- `IndexingEngine.search(search_type="semantic"/"hybrid")` still does not catch `EmbeddingUnavailableError` for the query-side embed call.
- No `/health/live` or `/health/ready` endpoint exists yet -- when one is added, remember §21's "except `/health/*`" exemption from the auth requirement (and consider whether it should also be exempt from rate limiting, a decision this milestone did not need to make since no such endpoint exists to exempt).
- No stale-data cleanup for previously-indexed files that become newly ignored (Phase 0.8 finding).
- No nested/per-directory `.gitignore`/`.codepulseignore` support (Phase 0.8 finding).
- No file-size or file-type caps (Phase 0.8 finding) -- note this is a *different* limit than Phase 1.4's rate limiter: Phase 1.5 / B10's ingestion size caps bound a single request's resource cost, while Phase 1.4 bounds request *frequency*. Neither substitutes for the other.
- `prometheus-client`/`structlog` remain declared-but-unused (Phase 0.9 finding, deferred to Phase 7).
- The advisory one-active-job-per-repository lock (Phase 1.3) remains a TOCTOU-prone application-level check, unaffected by Phase 1.4.
- A `running` job whose background task is killed by a real process crash would remain stuck at `running` forever (Phase 1.3 finding); no heartbeat/reaper exists.
- **New from Phase 1.4 (not a defect, a scope note):** the rate limiter's identity extraction reads the raw `X-API-Key` header value without validating it -- a caller can trivially evade per-identity throttling by rotating through many distinct (even entirely fake) key strings, since each new string gets a fresh, full bucket. This is an accepted consequence of keeping the limiter deliberately independent of the (comparatively expensive) DB-backed key-verification path; a determined abuser could still be caught by the IP-fallback path only if they also share an IP, which isn't guaranteed. Revisit if this proves to be a real abuse vector in practice -- not assumed to be one now, per this project's "no fabricated ...claims" ethos (this is a documented limitation, not a claimed mitigation).
- **New from Phase 1.4 (not a defect, a scope note):** rate-limit state is entirely in-memory and per-process, so it does not survive a restart and would not be shared across multiple worker processes if this project ever ran with more than one (it currently does not -- "no Redis yet" is an explicit, current-scope deferral, not an oversight).

**Blockers:** none currently known for starting Phase 1.5.

## Deferred Work

Per roadmap §39 — do not implement until the roadmap explicitly reaches these:

- Kubernetes, Redis, distributed workers, microservices
- Enterprise SSO, full organization/user tenancy
- Learned/adaptive ranking, LLM reranking, LLM-generated explanations as source of truth
- PostgreSQL/pgvector, Tantivy-backed lexical index, embedded ANN vector index (Phase 2)
- Ingestion safety, structured error responses, security test suite (Phase 1.5–1.7)
- `RepositorySnapshot` / git-diff incremental indexing / rename detection / heartbeat-based job reaper (Phase 3, roadmap §9/§17)
- Role-based permissions (viewer/indexer/admin) on API keys (Phase 7 / B25)
- API key revoke() flow / key expiry / audit logging; a job cancel() flow
- Call-graph confidence scoring, impact-analysis depth bucketing, module clustering, stale-data cleanup for newly-ignored files (Phase 3/4)
- MCP server (Phase 5)
- VS Code extension, Web UI (Phase 6)
- Observability infrastructure (Phase 7)

## Current Risks

- `_persist_to_db` still has four upsert blocks plus the dependency-insert block in one function/session — unchanged by Phase 1.4.
- The `languages`/fallback-counts repository-isolation gap (Phase 0.7 finding) remains unfixed at the data level.
- `StorageManager.create_tables()`'s `create_all()`-alongside-Alembic behavior remains a demonstrated real risk (Phase 1.3's incident) -- unchanged by Phase 1.4, which introduced no new model and so could not trigger it again even via the same careless import pattern.
- Rate-limit identity evasion via key rotation (see Known Issues) means the limiter is a resource-exhaustion speed bump, not a strong anti-abuse guarantee -- do not describe it as more than that.
- Whoever starts Phase 1.5 should re-verify this document against the actual repository per CLAUDE.md §13 before trusting it, as always.

## Exact Next Action

NEXT ACTION:
Phase 1.1 (`190d9bb`), Phase 1.2 (`a3a9b24`/`16fa747`), Phase 1.3 (`3feabf8`/`c9d27fa`), and Phase 1.4 (`571cf3a`) are committed and independently verified. Next, begin Phase 1.5 — ingestion safety: path allow-list, SSRF check, size caps (§37; backlog B10, depends on B8), which will harden `/v1/repositories/index`'s local-path/git-URL handling. **Do not begin Phase 1.5 without an explicit instruction to do so.**

## Context Handoff Notes

Phase 1.4 implementation is complete and verified in this session:

- **What was done**: audited the existing `api/app.py` (specifically its existing `CORSMiddleware` wiring, since ordering interacts directly with this milestone), `codepulse/config.py`'s established field/env-var-mapping pattern, and roadmap §6/§35/§37/§45/backlog-B11 text first, then added: three new `CodePulseConfig` fields (`rate_limit_enabled`, `rate_limit_requests_per_second`, `rate_limit_burst`) with env var mappings; a new top-level `codepulse/rate_limit.py` module (`TokenBucketLimiter` class + `get_limiter`/`reset_limiter` module-level accessors, mirroring `codepulse.config`'s own singleton-with-reset pattern); a new FastAPI middleware in `codepulse/api/app.py`, registered BEFORE `CORSMiddleware` (empirically verified to preserve CORS headers on a 429, not just reasoned about) that checks/consumes a token per request, keyed by the raw API-key header or client IP; `tests/test_rate_limit.py` (7) + `tests/test_rate_limit_middleware.py` (7); extended `tests/test_config.py` (2 tests) for the new fields; added an autouse rate-limiter-reset fixture to `tests/conftest.py` for whole-suite test isolation.
- **What was explicitly NOT done** (by design, matching 1.4's exact scope): no RBAC/per-role limits, no distributed/Redis-backed limiting, no structured error envelope, no ingestion-specific safety limits. `IndexingEngine`/`codepulse/indexer/` and `codepulse/storage/db.py` were not touched at all -- confirmed via `git diff --stat`; no new Alembic migration was needed or created.
- **What was verified**: 14 new focused tests passed (7+7) plus 2 extended config tests; full suite **179/179 passed, 0 failed**; the real `codepulse.db` was explicitly re-checked (schema and all row counts) after this session's own `codepulse.api.app` sanity-check import, specifically because of Phase 1.3's incident with that exact import pattern -- confirmed completely unaffected (no new model was added this milestone, so there was nothing for the pre-existing `create_all()` tech debt to accidentally create).
- **Exact files committed (`571cf3a`, on top of Phase 1.1/1.2/1.3's already-committed files)**:
  - New: `codepulse/rate_limit.py`, `tests/test_rate_limit.py`, `tests/test_rate_limit_middleware.py`.
  - Modified: `codepulse/config.py` (3 new fields + env mappings), `codepulse/api/app.py` (rate-limit middleware, registered before CORS), `tests/conftest.py` (autouse reset fixture), `tests/test_config.py` (2 tests extended), this file.
  - No other file touched. `codepulse/indexer/engine.py`, `codepulse/storage/db.py`, `codepulse/auth.py`, `codepulse/jobs.py`, `codepulse/api/security.py` were **not** modified by this milestone -- confirmed via `git diff --stat` before committing.
- **Exact tests run**: `.venv/Scripts/python.exe -m pytest -q` → 179 passed, 0 failed, 13 warnings (run before committing, and again post-commit -- both green).
- **Committed**: `571cf3a` (`feat: implement Phase 1.4 rate limiter`), on top of Phase 1.3's `3feabf8`/`c9d27fa`. Diff reviewed before committing -- exactly the 8 intended files, nothing else; no Phase 1.5+ code present (confirmed via `grep`); `codepulse/indexer/` and `codepulse/storage/db.py` untouched. Post-commit: `git status` clean, commit present in `git log` in the correct position, full suite re-run and green.
- **What remains**: nothing for this milestone. The next roadmap step is Phase 1.5, which must **not** begin without an explicit instruction to do so.
