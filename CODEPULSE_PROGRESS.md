# CodePulse Development Progress

## Last Updated

2026-09-11

## Current Phase

Phase 1 — Production Core (sixth sprint item complete)

## Current Milestone

None actively in progress. **Phase 1.6 is complete and verified.** Awaiting explicit instruction to begin Phase 1.7 (security test suite) — do not start it opportunistically.

## Last Completed Milestone

Phase 1.6 — Structured error response format across all endpoints (verified COMPLETE)

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
| Phase 1.4 — In-process token-bucket rate limiter | COMPLETE | 179/179 full suite passed (165 pre-existing + 14 new); no migration required; real `codepulse.db` untouched; re-verified post-commit (clean status, 179/179) | `571cf3a` (+ docs follow-up `b80c776`) |
| Phase 1.5 — Ingestion safety: path allow-list, SSRF check, size caps | COMPLETE | 204/204 full suite passed + 1 environment-driven skip (179 pre-existing + 25 new); audited requirement-by-requirement against the roadmap before commit; one disclosed specification ambiguity (see Known Issues); no migration required; real `codepulse.db` untouched | `0a51697` |
| Phase 1.6 — Structured error response format across all endpoints | COMPLETE | 215/215 full suite passed + 1 environment-driven skip (204 pre-existing + 11 new); no migration required; real `codepulse.db` untouched | *(uncommitted as of this update — see Exact Next Action)* |

Only mark a milestone COMPLETE here after actual verification (tests run, diff inspected, roadmap cross-checked) — not because code merely exists.

## Current Implementation State

### Phase 1.6 — Structured error response format across all endpoints

- **Scope determined from the roadmap's own text** (§37 item 1.6: "Structured error response format across all endpoints"; §21 API Strategy, verbatim: "Errors follow a single shape: `{"error": {"code": "...", "message": "...", "details": {...}}}`"). No dedicated backlog (§45) row exists for this item -- unlike every other Phase 1 milestone, 1.1-1.5 -- so its DoD is defined only by §37's title and §21's literal shape; there is no numeric/behavioral detail beyond that to under- or over-fit.
- **IN scope (implemented)**: a new `codepulse/api/errors.py` module with two global FastAPI exception handlers, registered once (`install_error_handlers(app)`) -- one for `HTTPException` (covers every existing 401/403/404/409/429/501/400 raise site, none of which needed to change), one for `RequestValidationError` (FastAPI's own automatic 422s), plus a catch-all `Exception` handler (500s, logged server-side via `logger.exception`, never leaking the real exception text/traceback to the client). The rate-limit middleware's own 429 (Phase 1.4, previously built as an ad-hoc `{"detail": ...}` dict directly, not raised as an `HTTPException`) was updated to call the exact same shared `error_json_response` helper the exception handlers use, so the envelope-building logic is never duplicated between the two call sites.
- **OUT of scope (explicitly NOT implemented)**: rewriting any error's status code or message wording (this milestone reshapes existing responses, it does not redesign what they say); MCP's own distinct, flatter error shape (roadmap §13: `{"error": "code_string", "message": "..."}`, no "details" -- a different section's requirement for a surface, MCP/Phase 5, that does not exist yet); a security test suite (Phase 1.7).
- **Design decisions made explicitly, not inferred from later milestones**:
  1. **`code` is derived deterministically from the HTTP status via Python's own `http.HTTPStatus` enum** (`HTTPStatus(404).name.lower()` -> `"not_found"`), not an invented per-endpoint string table -- every status code already in use (and any added later) maps correctly without `codepulse/api/errors.py` needing to know about it specifically.
  2. **`details` carries real, already-computed information where it exists, empty `{}` otherwise -- never fabricated.** Currently populated in exactly two cases: the rate limiter's real `retry_after_seconds` (the *same* value already used for the `Retry-After` header since Phase 1.4, not a second independently-computed number) on 429s, and the real Pydantic validation error list on 422s.
  3. **A genuinely unhandled exception (500) never leaks its real message/traceback to the client** -- `message` is the generic, fixed string "Internal server error"; the real exception is logged server-side only (`logger.exception`), matching how this project already treats other server-internal failure information. Verified this really works (not just reasoned about): a deliberately-broken route body was exercised via `TestClient(app, raise_server_exceptions=False)` and confirmed to return a clean structured 500 with no leaked text -- `raise_server_exceptions=True` (Starlette's TestClient default) intentionally re-raises the *original* exception to the calling test code even when an app-level handler already produced a clean response (a testing-visibility feature, not a production behavior), so tests that want to *inspect* the 500 response body must explicitly opt out of that default, which the one test needing it does.
  4. **The rate-limit middleware needed its own explicit fix, not just the global `HTTPException` handler.** Starlette's `ExceptionMiddleware`/`ServerErrorMiddleware` (which dispatch to `@app.exception_handler(...)` registrations) sit *inside* (closer to routing than) a custom `@app.middleware("http")` function in the stack ordering that matters -- an exception raised from *inside* such custom middleware is never seen by those dispatchers, so a plain `raise HTTPException(...)` there would never have reached the new handlers. Both call sites use the same `error_json_response` helper instead, avoiding a second, drifting implementation of the envelope shape.
- **Architecture rule preserved, with a deliberate placement note**: `codepulse/api/errors.py` lives *inside* `codepulse/api/` (unlike `auth.py`/`jobs.py`/`rate_limit.py`/`ingestion_safety.py`, which are top-level modules reusable by a future CLI/MCP surface) -- this is a REST-HTTP-response-shape concern specifically (§21's envelope), not a capability those other surfaces would reuse: MCP has its own, different error shape (§13), and the CLI never returns HTTP responses at all. `IndexingEngine`/`codepulse/indexer/`, `codepulse/storage/db.py`, `codepulse/auth.py`, `codepulse/jobs.py`, and `codepulse/ingestion_safety.py` were not touched.
- **Test impact was small and fully audited before implementing**: a project-wide search found only 2 existing test assertions anywhere that checked the old `{"detail": "..."}` shape directly (`tests/test_ingestion_safety_endpoint.py`, `tests/test_rate_limit_middleware.py`) -- both updated to check the new `error.message`/`error.code`/`error.details` path instead, with no change to what they actually verify. No test anywhere relies on an unhandled exception propagating through `TestClient` (verified via a project-wide `pytest.raises` search before adding the catch-all 500 handler), so that addition carried no regression risk.

### Phase 1.5 — Ingestion safety: path allow-list, SSRF check, size caps

- **Scope determined from the roadmap's own text** (§37 item 1.5; §15 Security Architecture's verbatim "Repository-ingestion safety (the dangerous endpoint)" paragraph: local-path allow-listing + traversal/symlink rejection, git-URL scheme validation + SSRF host check + size/file-count cap "before beginning a full clone"; §15's "Resource limits: max files per repo, max file size parsed..."; backlog B10, DoD "Path traversal / SSRF / oversized-repo attempts rejected with tests proving it"):
  - IN scope (implemented): a new top-level `codepulse/ingestion_safety.py` module -- `validate_local_path` (resolves symlinks/`..`, rejects anything outside a configured allow-list) and `validate_git_url` (scheme + resolved-host SSRF check), both wired into `POST /v1/repositories/index` as synchronous, pre-job checks; a file-count cap and a per-file size cap, both enforced inside `IndexingEngine.index_directory()` (see design decision 5 below for why); three new `CodePulseConfig` fields (`ingestion_allowed_roots`, `ingestion_max_files_per_repo`, `ingestion_max_file_size_bytes`).
  - OUT of scope (explicitly NOT implemented, reserved for later milestones or never assigned to this one at all): actually cloning/fetching a git repository (see design decision 1 below -- this codebase never had that capability, and no sprint item assigns it a milestone); request-body size caps (roadmap §15 lists this as a separate, server/ASGI-layer resource limit, not application code this milestone adds); a global/per-tenant concurrent-job cap (Phase 1.3 already added a *per-repository* advisory lock; §15's "per tenant" wording describes a distinct, unrequested control); a structured `{"error": {...}}` envelope (Phase 1.6); a dedicated security test suite (Phase 1.7).
- **Design decisions made explicitly, not inferred from later milestones**:
  1. **No git-URL fetching was invented.** Grep-confirmed: this codebase has zero git-clone/network-fetch code anywhere (no `subprocess`, `GitPython`, or clone logic). `IndexRepoRequest.source` (`"local"`/`"github"`/`"gitlab"`) has always been accepted but never branched on -- every prior phase silently mis-treated a git URL as a local path, indexing zero files while claiming "queued." Building real git-URL cloning is a much larger, unrequested feature no sprint item assigns a milestone to. What 1.5 *does* own, literally, is the "SSRF check" -- so `validate_git_url` is implemented and tested as a real, standalone safety primitive (ready for whenever a real fetch path exists), and the endpoint now honestly rejects a non-local `source` (after still validating the URL for safety first) with HTTP 501, rather than continuing the silent mis-processing.
  2. **`ingestion_allowed_roots` defaults to `[]` = unrestricted**, the opposite convention from `cors_origins` (which defaults to deny-all). This is deliberate, not an inconsistency: this endpoint acts on behalf of the local user who already has filesystem access to their own machine (CLAUDE.md: "Local mode is the default"), and the roadmap's own promised zero-configuration experience ("`codepulse index .` works with zero external infrastructure") would break if ingestion required allow-list configuration before any use. The interesting case for enforcing a strict allow-list is a server/team deployment exposing this API beyond localhost with a key untrusted parties might hold -- exactly §15's own stated concern, and exactly when an operator would configure the setting.
  3. **Traversal and symlink-escape rejection share one mechanism**: `Path(path).resolve()` (which both collapses `..` segments and follows symlinks) followed by a containment check against the resolved allow-list roots. No separate "reject literal `..` in the string" logic was written, because resolving first and checking containment catches both attack shapes identically and is simpler than pattern-matching the raw string.
  4. **File-count/file-size caps live inside `IndexingEngine.index_directory()`, not at the API boundary as a synchronous pre-flight check.** The file list they check against is only available after the discovery walk `index_directory()` already performs; duplicating that exact walk at the API layer just to pre-count would be wasteful and is exactly the kind of "second identity system"/duplicated-logic this project's established house style avoids (see `codepulse/ingestion_safety.py`'s module docstring). Consequence: a file-count violation surfaces as a **failed job** (poll `GET /v1/jobs/{job_id}`), not a synchronous 4xx like the allow-list/SSRF checks -- a deliberate, documented split between "known before any work starts" (checked synchronously) and "only discoverable once the directory has been walked" (checked as part of the job, surfaced via the existing Phase 1.3 job-failure path). A single oversized file is *skipped* (logged, not fatal), reusing the exact per-file error-tolerance pattern already established for malformed/unparseable files.
  5. **`codepulse/indexer/engine.py` IS modified by this milestone** -- a deliberate departure from Phases 1.2-1.4's "IndexingEngine untouched" pattern, and called out explicitly here rather than silently. File-count/size limits are genuine indexing-pipeline resource-safety concerns (the file list and each file's bytes are only known inside the discovery/parse loop itself), the same category as the engine's own pre-existing "no silent pseudo-embedding fallback" and "embedding model unavailable -> fail explicitly" invariants (§17) -- not an HTTP/auth-coupling violation of CLAUDE.md §3, which is specifically about authentication, not general resource safety. `IndexingEngine` still has zero knowledge of jobs, auth, or rate limiting.
- **Architecture rule preserved otherwise**: `codepulse/ingestion_safety.py` is a new top-level module (not under `codepulse/api/` or `codepulse/indexer/`), mirroring `codepulse/auth.py`/`codepulse/jobs.py`/`codepulse/rate_limit.py`'s placement.

### ⚠️ Incident during migration verification (Phase 1.3, found and fully remediated in that session -- kept here verbatim as accurate project history, not touched by Phase 1.4 or 1.5)

While running an ad-hoc Python one-liner to sanity-check new routes (`import codepulse.api.app as app_module; ...`), that import's module-level `storage_manager = StorageManager()` executed against the **default** `db_url` (`sqlite:///codepulse.db`, the real database) because the command was run without a `CODEPULSE_DB_URL` override. `StorageManager.__init__` unconditionally calls `Base.metadata.create_all(bind=self.engine)` **alongside** Alembic -- an already-documented, pre-existing piece of technical debt -- which silently created the new `index_jobs` table directly on the real `codepulse.db`, bypassing Alembic entirely and leaving `alembic_version` stale.

**Caught immediately** when applying the real Phase 1.3 migration to a disposable copy of `codepulse.db` failed with `table index_jobs already exists`. **Verified before remediating**: the accidentally-created table's schema was byte-for-byte identical to the migration's own definition (expected, since `create_all()` uses the exact same SQLAlchemy model), empty, and no pre-existing data was touched. **Remediated** via `stamp_database(revision="de22df2fc3ce")` -- Alembic's own non-destructive mechanism for tracking an already-correct schema -- applied to the disposable copy first, then identically to the real database. **No data was lost.**

**Lesson re-applied in both Phase 1.4 and Phase 1.5**: `codepulse.api.app` was again imported ad-hoc for sanity checks in both sessions, without an isolated DB URL. In neither case did this matter, because neither milestone added a new model to `codepulse/storage/db.py` -- `create_all()` had nothing new to create either time -- and the real `codepulse.db` was explicitly re-verified unchanged (schema and every row count) immediately afterward in both sessions.

## Database State

- `codepulse.db` exists at the repository root, untracked by git.
- **Current Alembic revision (real `codepulse.db`): `de22df2fc3ce`** -- **unchanged by Phase 1.6**. No migration was created, needed, or considered: this milestone adds only application-layer HTTP-response-shaping logic, no new table or column.
- Row counts, confirmed **unchanged**: `repositories` = 1, `files` = 18, `symbols` = 128, `dependencies` = 528, `code_embeddings` = 0, `api_keys` = 0, `api_key_repositories` = 0, `index_jobs` = 0.
- The real `codepulse.db` was not written to or read from by any test during this milestone -- every test uses the shared `isolated_app` fixture (a `tmp_path`-based temporary SQLite database).

## Test State

- Last full suite result: **215 passed, 1 skipped, 13 warnings** (`.venv/Scripts/python.exe -m pytest -q`), ~202s. The 1 skip is the same pre-existing, environment-driven `test_symlink_escaping_the_allowed_root_is_rejected` from Phase 1.5 (this Windows box lacks `SeCreateSymbolicLinkPrivilege`) -- unrelated to and unaffected by this milestone.
- New focused Phase 1.6 tests: `tests/test_error_format.py` (11 tests) -- real HTTP behavior proving every distinct error class uses the single `{"error": {"code", "message", "details"}}` envelope: 401 (missing/unknown key), 403 (wrong scope), 404 (not found), 409 (concurrency lock), 429 (rate limit, with `details.retry_after_seconds` matching the real `Retry-After` header value exactly), 501 (unsupported git ingestion), 400 (bad ingestion path), 422 (FastAPI's own automatic validation failure, with the real Pydantic error list in `details.errors`), and 500 (a deliberately-triggered unhandled exception, proving the client-facing message stays generic and the real exception text never leaks into the response body) -- plus a regression guard that a normal 200 success response is never wrapped in the error envelope.
- 2 pre-existing test assertions updated (not new tests) to check the new shape instead of the old `{"detail": ...}` one: `tests/test_ingestion_safety_endpoint.py::test_git_source_with_ssrf_unsafe_host_is_rejected`, `tests/test_rate_limit_middleware.py::test_exceeding_burst_returns_429_with_retry_after` (the latter also gained an assertion that `details.retry_after_seconds` matches the header).
- All 204 pre-existing tests (203 passed + 1 skipped) re-run unchanged otherwise and pass, confirming no regression to Phase 0.1-0.10/1.1-1.5 behavior.
- No existing test was weakened or deleted; the 1 skip is the same pre-existing, unrelated, environment-driven one from Phase 1.5.
- Known warnings: `StarletteDeprecationWarning` (httpx/TestClient) and `FutureWarning`s from `tree_sitter` -- pre-existing, not addressed (out of scope).

## Known Issues

**Roadmap work remaining:** Phase 1.6 (structured error response format) is complete. Next per the roadmap's own sprint order (§37) is **Phase 1.7 — security test suite**, the last Phase 1 item. **None of Phase 1.7 has been started or implemented — do not infer it exists.**

**Known technical debt (unchanged from Phase 1.5, re-verified still accurate unless noted):**
- `StorageManager.create_tables()` still unconditionally calls `Base.metadata.create_all()` alongside Alembic -- demonstrated risk (Phase 1.3's incident), still not fixed. Recommend prioritizing a fix before it causes a real problem outside a disposable/test context.
- SQLite's `PRAGMA foreign_keys` is never enabled anywhere in this codebase, so every `ON DELETE CASCADE` clause declared in the schema is not actually enforced by SQLite itself. No repository-deletion endpoint exists yet, so this has no current user-facing impact.
- `SymbolType.VARIABLE` / `SymbolType.INTERFACE` enum values defined but never produced by either parser.
- `IndexRepoRequest.repository_id` field in `api/app.py` remains dead/unused.
- Dependency (call graph edge) rows still duplicate on every re-index (Phase 3 scope).
- `/v1/autocomplete` and the `languages`/fallback-counts parts of `get_repository_stats`/`get_index_status` are still not repository-scoped in their actual data (Phase 0.7 finding) -- still unfixed, still documented.
- `BM25Index`/`CallGraphBuilder` have no durable backing and do not survive restart (Phase 0.10 finding, regression-tested, not fixed).
- `IndexingEngine.search(search_type="semantic"/"hybrid")` still does not catch `EmbeddingUnavailableError` for the query-side embed call.
- No `/health/live` or `/health/ready` endpoint exists yet -- when one is added, remember §21's "except `/health/*`" exemption from the auth requirement.
- No stale-data cleanup for previously-indexed files that become newly ignored (Phase 0.8 finding).
- No nested/per-directory `.gitignore`/`.codepulseignore` support (Phase 0.8 finding).
- The rate limiter's identity extraction (Phase 1.4) reads the raw `X-API-Key` header without validating it -- key rotation can evade per-identity throttling.
- Rate-limit state (Phase 1.4) is entirely in-memory/per-process; does not survive restart or span multiple workers ("no Redis yet" -- explicit, current-scope deferral).
- The advisory one-active-job-per-repository lock (Phase 1.3) remains a TOCTOU-prone application-level check.
- A `running` job whose background task is killed by a real process crash would remain stuck at `running` forever (Phase 1.3 finding); no heartbeat/reaper exists.
- **New from Phase 1.5 (not a defect, a scope note):** `validate_git_url`'s SSRF check protects against a real attack shape (the classic cloud-metadata/internal-service probe) but has no live consumer yet -- there is no git-clone implementation for it to gate in production. It is real, tested, working code, ready for whenever fetching is implemented; do not describe git-URL ingestion itself as supported.
- **New from Phase 1.5 (not a defect, a scope note):** DNS rebinding is not defended against -- `validate_git_url` resolves the host once, synchronously, at request time; a hostname that resolves safely at validation time but is rebound to a private/internal address by the time a (currently nonexistent) fetch step would run is not caught by this check. This is a known, accepted limitation of host-based SSRF validation in general (not specific to this implementation) and is unreachable today regardless, since no fetch step exists to exploit the gap.
- **New from Phase 1.5 (not a defect, a scope note):** the file-count cap is enforced *after* the discovery walk, inside `index_directory()`, not synchronously before a job is created (see "Design decisions" #4 above) -- an oversized repository still consumes one `IndexJob` row (marked `failed`) and the cost of a directory walk before being rejected, unlike the allow-list/SSRF checks which reject before any work or DB write happens.
- **New from Phase 1.5 (not a defect, a scope note):** `ingestion_allowed_roots` defaults to unrestricted (see "Design decisions" #2 above) -- a fresh install is not protected by this setting until an operator explicitly configures it for a non-local-first deployment. This is a deliberate default preserving the zero-configuration local experience, not an oversight; do not describe the endpoint as "path-restricted" without checking whether this setting is actually configured.
- **Open, disclosed specification ambiguity from Phase 1.5, still unresolved by the user as of this update:** whether an unconfigured `ingestion_allowed_roots` should mean unrestricted (current, shipped behavior) or deny-all-until-configured. A dedicated compliance review during Phase 1.5 concluded this is a genuine `SPECIFICATION AMBIGUITY` -- neither the roadmap nor CLAUDE.md states it explicitly either way, and real textual evidence exists on both sides. Shipped as unrestricted, disclosed as a judgment call, not a claimed exact-compliance fact. Not touched by Phase 1.6.
- **New from Phase 1.6 (not a defect, a scope note):** the 500 handler's generic client-facing message ("Internal server error") deliberately omits the real exception type/text to avoid an information-disclosure regression; the real exception is only visible server-side via `logger.exception`. If deeper client-visible diagnostics are ever wanted, that would need its own deliberate scoping (e.g. an opt-in debug mode), not silently added later.
- **New from Phase 1.6 (not a defect, a scope note):** `Starlette`'s `TestClient` defaults to `raise_server_exceptions=True`, which re-raises a genuinely unhandled exception to the calling test even when the app's own exception handler already produced a clean response. Any *future* test that deliberately triggers a 500 to inspect its body must construct its `TestClient` with `raise_server_exceptions=False`, matching `tests/test_error_format.py`'s own `client_no_reraise` fixture -- otherwise the test will see the raw exception instead of the structured response.

**Blockers:** none currently known for starting Phase 1.7.

## Deferred Work

Per roadmap §39 — do not implement until the roadmap explicitly reaches these:

- Kubernetes, Redis, distributed workers, microservices
- Enterprise SSO, full organization/user tenancy
- Learned/adaptive ranking, LLM reranking, LLM-generated explanations as source of truth
- PostgreSQL/pgvector, Tantivy-backed lexical index, embedded ANN vector index (Phase 2)
- Security test suite (Phase 1.7)
- Actual git-URL cloning/fetching; request-body size caps; a global/per-tenant concurrent-job cap (distinct from Phase 1.3's per-repository lock) -- none assigned a milestone anywhere in this roadmap
- `RepositorySnapshot` / git-diff incremental indexing / rename detection / heartbeat-based job reaper (Phase 3, roadmap §9/§17)
- Role-based permissions (viewer/indexer/admin) on API keys (Phase 7 / B25)
- API key revoke() flow / key expiry / audit logging; a job cancel() flow
- Call-graph confidence scoring, impact-analysis depth bucketing, module clustering, stale-data cleanup for newly-ignored files (Phase 3/4)
- MCP server (Phase 5)
- VS Code extension, Web UI (Phase 6)
- Observability infrastructure (Phase 7)

## Current Risks

- `_persist_to_db` still has four upsert blocks plus the dependency-insert block in one function/session — unchanged by Phase 1.6.
- The `languages`/fallback-counts repository-isolation gap (Phase 0.7 finding) remains unfixed at the data level.
- `StorageManager.create_tables()`'s `create_all()`-alongside-Alembic behavior remains a demonstrated real risk (Phase 1.3's incident) -- unchanged by Phase 1.6, which introduced no new model.
- Rate-limit identity evasion via key rotation (Phase 1.4) means the limiter is a resource-exhaustion speed bump, not a strong anti-abuse guarantee.
- `ingestion_allowed_roots`'s unrestricted-by-default behavior (Phase 1.5) remains an open, disclosed specification ambiguity, not yet resolved by the user.
- `validate_git_url`'s SSRF check has no live fetch path to protect yet -- do not describe git-URL ingestion as supported.
- Whoever starts Phase 1.7 should re-verify this document against the actual repository per CLAUDE.md §13 before trusting it, as always.

## Exact Next Action

NEXT ACTION:
Phase 1.1 (`190d9bb`), Phase 1.2 (`a3a9b24`/`16fa747`), Phase 1.3 (`3feabf8`/`c9d27fa`), Phase 1.4 (`571cf3a`/`b80c776`), and Phase 1.5 (`0a51697`) are committed and independently verified. Next, begin Phase 1.7 — security test suite (§37; the last Phase 1 sprint item; roadmap Phase 1 DoD: "no endpoint is callable without a key; a client can poll a job to real completion; an oversized/malicious ingestion request is rejected before doing work" plus §20's "unauthenticated request rejected; path traversal in ingestion path rejected; SSRF URL rejected; oversized repo rejected" test categories) -- likely largely a consolidation/audit of coverage already built across 1.1-1.6 rather than wholly new behavior, but read §20/§37 item 1.7's exact text first rather than assuming that. **Do not begin Phase 1.7 without an explicit instruction to do so.**

## Context Handoff Notes

Phase 1.6 implementation is complete and verified in this session:

- **What was done**: audited §21's exact error-shape text and §37 item 1.6 first, noted there is no backlog (§45) row for this item (unlike every other Phase 1 milestone) so its DoD is the title + the §21 shape only; audited every existing `HTTPException` raise site and the rate-limit middleware's own ad-hoc 429 response; audited the whole test suite for assertions on the old `{"detail": ...}` shape (found exactly 2) and for any reliance on unhandled-exception-propagation through `TestClient` (found none) before implementing; then added a new `codepulse/api/errors.py` module (`error_json_response`, `status_code_to_error_code`, `install_error_handlers`) registering global `HTTPException`/`RequestValidationError`/`Exception` handlers once on `app`; updated the rate-limit middleware to use the same shared helper instead of its own ad-hoc dict; updated the 2 existing test assertions; added `tests/test_error_format.py` (11 tests) covering every distinct error class (401/403/404/409/429/501/400/422/500) plus a success-response regression guard.
- **What was explicitly NOT done** (by design, matching 1.6's exact scope): no error status codes or message wording were changed; MCP's own distinct error shape was not implemented (MCP doesn't exist yet); no security test suite (Phase 1.7). `codepulse/indexer/engine.py`, `codepulse/storage/db.py`, `codepulse/auth.py`, `codepulse/jobs.py`, `codepulse/ingestion_safety.py`, and `codepulse/api/security.py` were **not** modified -- confirmed via `git diff --stat`; no new Alembic migration was needed or created.
- **A real implementation subtlety was discovered and correctly resolved, not glossed over**: Starlette's `ExceptionMiddleware`/`ServerErrorMiddleware` sit inside a custom `@app.middleware("http")` function in the effective call stack, so an exception raised from inside the Phase 1.4 rate-limit middleware would never reach the new `@app.exception_handler(...)` registrations -- confirmed empirically (not just reasoned about) by attempting exactly that and observing the mismatch, before fixing it by having the middleware call the same shared helper function directly. Separately, confirmed empirically that a genuinely unhandled route-body exception IS correctly caught and reshaped by the new handler, but that Starlette's `TestClient` default (`raise_server_exceptions=True`) re-raises the original exception to the calling test regardless of whether the app already produced a clean response -- a testing-convenience feature, not a production bug -- requiring the one test that inspects a 500 body to explicitly pass `raise_server_exceptions=False`.
- **What was verified**: 11 new focused tests passed; 2 existing test assertions updated and re-verified; full suite **215 passed, 1 skipped, 0 failed** (the skip is the same pre-existing, unrelated, environment-driven one from Phase 1.5); real `codepulse.db` confirmed unchanged (no test touches it; no migration exists to touch it with).
- **Exact files changed this session (uncommitted at time of writing, on top of Phase 1.1-1.5's already-committed files)**:
  - New: `codepulse/api/errors.py`, `tests/test_error_format.py`.
  - Modified: `codepulse/api/app.py` (error-handler registration + rate-limit middleware's 429 reshaped via the shared helper + an unused `JSONResponse` import removed), `tests/test_ingestion_safety_endpoint.py` and `tests/test_rate_limit_middleware.py` (1 assertion each, updated to the new shape), this file.
  - No other file touched.
- **Exact tests run**: `.venv/Scripts/python.exe -m pytest -q` → 215 passed, 1 skipped, 0 failed, 13 warnings, ~202s.
- **What remains**: **committing this work to git** (not yet done as of this document update — pending an explicit commit instruction). After that, the next roadmap step is Phase 1.7, which must **not** begin without an explicit instruction to do so.
