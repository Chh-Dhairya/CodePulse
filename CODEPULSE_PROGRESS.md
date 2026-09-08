# CodePulse Development Progress

## Last Updated

2026-09-08

## Current Phase

Phase 1 — Production Core (second sprint item complete)

## Current Milestone

None actively in progress. **Phase 1.2 is complete and verified.** Awaiting explicit instruction to begin Phase 1.3 (real `IndexJob` state machine) — do not start it opportunistically.

## Last Completed Milestone

Phase 1.2 — Central authorization dependency + endpoint enforcement (verified COMPLETE)

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
| Phase 1.2 — Central authorization dependency + endpoint enforcement | COMPLETE | 139/139 full suite passed (112 pre-existing + 27 new); no migration required; real `codepulse.db` untouched; re-verified post-commit (clean status, 139/139) | `a3a9b24` |

Only mark a milestone COMPLETE here after actual verification (tests run, diff inspected, roadmap cross-checked) — not because code merely exists.

## Current Implementation State

### Phase 1.2 — Central authorization dependency + endpoint enforcement

- **Scope determined from the roadmap's own text** (§37 item 1.2: "Central authorization dependency (FastAPI dependency injected into every route)"; §15/§30's central-checkpoint requirement; §21: "Every endpoint requires an API key except `/health/*`"; backlog B8's DoD: "No endpoint callable without a valid, scoped key"):
  - IN scope (implemented): one central FastAPI dependency (`codepulse.api.security.require_api_key`) injected into **every one of the app's 12 routes** (no `/health/*` endpoint exists yet, so this currently means all 12); it verifies the `X-API-Key` header via Phase 1.1's `codepulse.auth.verify_api_key` (401 on missing/unknown/revoked key) and returns an `AuthorizedKey` context carrying the exact set of repository ids the key is scoped to; every endpoint that names a specific repository -- directly (a path param or a body field) or indirectly (a symbol/file resolved to its owning repository via FK) -- calls `AuthorizedKey.authorize_repository`/`authorize_search_scope`, which raises HTTP 403 unless the key is scoped to that exact repository.
  - OUT of scope (explicitly NOT implemented, reserved for later milestones): role-based permissions (Phase 7 / B25); rate limiting (Phase 1.4); ingestion-specific safety -- path allow-listing, SSRF checks, size caps (Phase 1.5 / B10); a structured `{"error": {...}}` response envelope for every endpoint (Phase 1.6 -- this milestone still raises plain `HTTPException`s with a `detail` string, matching every pre-existing error path); a dedicated security test suite (Phase 1.7 -- this milestone's tests cover only its own behavior).
- **Design decisions made explicitly, not inferred from later milestones** (all documented in code, in `codepulse/api/security.py`'s and `codepulse/api/app.py`'s comments):
  1. **Fail-closed for unscoped keys.** Phase 1.1 permits creating an API key with zero repository grants. Phase 1.2 treats that as authenticated-but-authorized-for-nothing-repository-scoped (an empty `AuthorizedKey.repository_ids` set) -- least-privilege, matching the roadmap's literal "valid, SCOPED key" wording (not "valid key" alone). There is no wildcard/superuser key in this milestone.
  2. **A `None`/omitted `repository_id` on `/v1/search` and `/v1/ide-search` is rejected (403), not silently honored.** `IndexingEngine.search(repository_id=None)` is an existing (Phase 0.6) feature meaning "search across every indexed repository" -- authorizing that against one key's specific, limited repository grants is impossible without either unsafely exposing repositories outside its scope (the exact cross-repo leakage class §30 names as this milestone's reason to exist) or teaching `IndexingEngine.search()` to filter by a *set* of ids, which would couple authorization logic into the intelligence core (explicitly disallowed: "Do not couple authentication into IndexingEngine or the intelligence core"). So a `None` `repository_id` is rejected outright at the API boundary; the caller must name one specific repository.
  3. **`/v1/repositories/index` (ingestion) requires only authentication, no repository-scope check.** It creates/re-indexes a repository that doesn't yet have an established scope to check the request against -- ingestion-path-specific safety (allow-listing, SSRF, size caps) is explicitly Phase 1.5/B10, not this milestone.
  4. **`/v1/autocomplete` and `/v1/search/feedback` require only authentication.** Neither has a `repository_id` parameter or a resolvable-to-a-repository field at all; `/v1/autocomplete`'s pre-existing lack of repository scoping (documented since Phase 0.7) is a known limitation this milestone's literal scope -- "a FastAPI dependency injected into every route" -- does not extend to fixing (that would mean redesigning the endpoint's signature).
  5. **Symbol/file endpoints resolve their owning repository via the existing FK chain** (`Symbol.file.repo_id`, `File.repo_id`) and authorize it *after* a genuine 404 check but *before* returning any content -- a nonexistent symbol/file is always 404 regardless of the caller's scope (no scope-dependent information disclosure about existence), while a real-but-out-of-scope one is 403.
- **Architecture rule preserved**: the dependency lives in `codepulse/api/security.py` -- the API boundary -- not in `codepulse/indexer/`. `IndexingEngine` is completely untouched by this milestone (confirmed by `git diff`: `codepulse/indexer/engine.py` does not appear in this milestone's diff).
- **Reuses Phase 1.1 primitives, no second auth mechanism**: `require_api_key` calls `codepulse.auth.verify_api_key` directly; it does not re-implement hashing or lookup.
- **Testability preserved without touching the real DB**: `require_api_key` resolves `codepulse.api.app`'s `storage_manager` via a *deferred* import inside the function body (not a module-level one), so the established test pattern of monkeypatching `codepulse.api.app.storage_manager` to an isolated database (used since Phase 0.7's ingestion test) transparently applies to authorization too -- no test authenticates against, or writes to, the real `codepulse.db`.
- **Checkpoint discipline note**: Phase 1.1 was committed (`190d9bb`) and re-verified in a clean, isolated working tree (112/112) via `git stash` *before* any Phase 1.2 code was written into a commit -- Phase 1.2's own code had already been drafted in the working tree by the time this was caught, so it was stashed out, the Phase 1.1 checkpoint was completed and verified in isolation exactly as instructed, and Phase 1.2 was then restored (`git stash pop`) to continue. No commit contains a mix of 1.1 and 1.2 changes.

## Database State

- `codepulse.db` exists at the repository root, untracked by git.
- **Current Alembic revision (real `codepulse.db`): `b0bdd63949f4`** -- **unchanged by Phase 1.2**. No migration was created, needed, or considered: this milestone adds only application-layer authorization logic, reusing Phase 1.1's `api_keys`/`api_key_repositories` schema as-is.
- Row counts, confirmed **unchanged** before and after this milestone: `repositories` = 1, `files` = 18, `symbols` = 128, `dependencies` = 528, `code_embeddings` = 0, `api_keys` = 0, `api_key_repositories` = 0.
- The real `codepulse.db` was not read from or written to at all during Phase 1.2 -- every test uses a `tmp_path`-based temporary SQLite database via the shared `isolated_app` fixture (`tests/conftest.py`).

## Test State

- Last full suite result: **139 passed, 0 failed, 13 warnings** (`.venv/Scripts/python.exe -m pytest -v`), ~202s.
- New focused Phase 1.2 tests: `tests/test_api_authorization.py` (27 new tests): missing/empty/unknown/revoked key -> 401 (4 tests); unscoped or wrong-scope key -> 403 on repository-scoped endpoints (index-status, stats, symbol details, file symbols, similar, impact-analysis, within-symbol-search -- 9 tests); correctly-scoped key -> 200 success preserved (index-status, stats, symbol details, file symbols, within-symbol-search, search -- 6 tests); a genuinely-nonexistent symbol still 404s for an unscoped key (1 test); `repository_id: null` rejected (403) on both `/v1/search` and `/v1/ide-search` even for an otherwise-valid scoped key (2 tests); a correctly-scoped search still succeeds (1 test); auth-only endpoints (autocomplete, feedback, ingestion) succeed with an unscoped key and are still rejected with no key at all (5 tests).
- Existing API-level tests updated to add the now-required `X-API-Key` header (a roadmap-approved, intentional behavior change per CLAUDE.md §9 -- not a weakened assertion): `tests/test_api.py` (all 3 tests), `tests/test_cors.py` (2 of 9 tests -- the two hitting `/v1/autocomplete` on the real global app with a real `Origin` header; the preflight-OPTIONS test and the 6 tests using a separate minimal CORS-only test app were untouched, since FastAPI/Starlette's `CORSMiddleware` answers preflight `OPTIONS` requests itself, before any route dependency runs), `tests/test_no_fabricated_metrics.py` (all 5 tests). Every original assertion in these files is preserved unchanged; only request setup (an isolated DB + a real key) was added. Two of `test_no_fabricated_metrics.py`'s tests (`index-status`/`stats` "no fabricated field" checks) were adapted from a deliberately-nonexistent `repository_id=999999999` to a real, seeded, correctly-scoped repository, because a key can never be scoped to a nonexistent repository (Phase 1.1's `create_api_key` validates this) -- see that file's module docstring for the full rationale. The assertion being verified (no fabricated metric field) is identical either way.
- New shared fixture: `tests/conftest.py` (`isolated_app`, `make_api_key_headers`) -- swaps `codepulse.api.app`'s `storage_manager`/`indexing_engine` for a temporary, disposable database and mints a real API key against it; used by all four of the above test files.
- All 112 pre-existing tests re-run unchanged (except the 3+2+5 = 10 adapted-for-the-new-auth-requirement tests described above) and pass, confirming no regression to Phase 0.1-0.10/1.1 behavior.
- No existing test was weakened, skipped, or deleted -- every original assertion still runs and still passes.
- Known warnings: `StarletteDeprecationWarning` (httpx/TestClient) and `FutureWarning`s from `tree_sitter` -- pre-existing, not addressed (out of scope).

## Known Issues

**Roadmap work remaining:** Phase 1.2 (central authorization dependency) is complete. Next per the roadmap's own sprint order (§37) is **Phase 1.3 — `IndexJob` state machine + `/v1/jobs/{id}`**, then 1.4-1.7 (rate limiting, ingestion safety, structured error responses, security test suite). **None of Phase 1.3+ has been started or implemented — do not infer it exists.**

**Known technical debt (unchanged from Phase 1.1, re-verified still accurate unless noted):**
- `StorageManager.create_tables()` still unconditionally calls `Base.metadata.create_all()` alongside Alembic.
- `SymbolType.VARIABLE` / `SymbolType.INTERFACE` enum values defined but never produced by either parser.
- Dead/unused `job_id` generation and `IndexRepoRequest.repository_id` field in `api/app.py` — Phase 1.3 (`IndexJob`) scope, not yet addressed.
- Dependency (call graph edge) rows still duplicate on every re-index (Phase 3 scope).
- `/v1/autocomplete` and the `languages`/fallback-counts parts of `get_repository_stats`/`get_index_status` are still not repository-scoped in their actual data (Phase 0.7 finding; Phase 1.2 gates *access* to these endpoints by key/scope where a repository_id exists, but does not change what data they return) -- still unfixed, still documented.
- `BM25Index`/`CallGraphBuilder` have no durable backing and do not survive restart (Phase 0.10 finding, regression-tested, not fixed).
- `IndexingEngine.search(search_type="semantic"/"hybrid")` still does not catch `EmbeddingUnavailableError` for the query-side embed call.
- No `/health/live` or `/health/ready` endpoint exists yet -- when one is added, remember §21's "except `/health/*`" exemption from the auth requirement.
- No stale-data cleanup for previously-indexed files that become newly ignored (Phase 0.8 finding).
- No nested/per-directory `.gitignore`/`.codepulseignore` support (Phase 0.8 finding).
- No file-size or file-type caps (Phase 0.8 finding).
- `prometheus-client`/`structlog` remain declared-but-unused (Phase 0.9 finding, deferred to Phase 7).
- **New from Phase 1.2 (not a defect, a scope note):** `get_index_status`'s pre-existing "repository not found -> fallback in-memory stats" branch is now unreachable through any authorized request in practice: a key can only ever be scoped to a repository id that actually exists (Phase 1.1's `create_api_key` enforces this), and `api_key_repositories` rows cascade-delete when their repository is deleted, so a key's scope can never outlive the repository it names. The branch itself was left in place (removing it is opportunistic scope creep beyond this milestone's literal ask) but is now effectively dead code reachable only via a narrow request-timing race (repo deleted between the auth check and the handler's own lookup) or by direct, non-HTTP use of `IndexingEngine`.
- **New from Phase 1.2 (not a defect, a scope note):** error responses from `require_api_key`/`AuthorizedKey` are plain `HTTPException(status_code, detail=str)`, matching every other error path already in `api/app.py` -- not the structured `{"error": {"code", "message", "details"}}` envelope §21 describes for the API generally. Reshaping every endpoint's error responses to that shape is Phase 1.6, not this milestone.

**Blockers:** none currently known for starting Phase 1.3.

## Deferred Work

Per roadmap §39 — do not implement until the roadmap explicitly reaches these:

- Kubernetes, Redis, distributed workers, microservices
- Enterprise SSO, full organization/user tenancy
- Learned/adaptive ranking, LLM reranking, LLM-generated explanations as source of truth
- PostgreSQL/pgvector, Tantivy-backed lexical index, embedded ANN vector index (Phase 2)
- Real `IndexJob` state machine, rate limiting, ingestion safety, structured error responses, security test suite (Phase 1.3–1.7)
- Role-based permissions (viewer/indexer/admin) on API keys (Phase 7 / B25)
- API key revoke() flow / key expiry / audit logging (not yet scheduled to a specific sub-phase beyond "not 1.1/1.2")
- Call-graph confidence scoring, impact-analysis depth bucketing, module clustering, stale-data cleanup for newly-ignored files (Phase 3/4)
- MCP server (Phase 5)
- VS Code extension, Web UI (Phase 6)
- Observability infrastructure (Phase 7)

## Current Risks

- `_persist_to_db` still has four upsert blocks plus the dependency-insert block in one function/session — unchanged by Phase 1.2.
- The `languages`/fallback-counts repository-isolation gap (Phase 0.7 finding) remains unfixed at the data level (Phase 1.2 only gates *access*, not what the underlying data-assembly code returns).
- Only `repositories`, `files`, `symbols`, `code_embeddings` are idempotent; `dependencies` is not (Phase 3 scope).
- `/v1/autocomplete` and `/v1/search/feedback` are authenticated but have no meaningful repository-scope concept at all -- do not describe them as "repository-isolated."
- Whoever starts Phase 1.3 should re-verify this document against the actual repository per CLAUDE.md §13 before trusting it, as always.

## Exact Next Action

NEXT ACTION:
Both Phase 1.1 (`190d9bb`) and Phase 1.2 (`a3a9b24`) are committed and independently verified. Next, begin Phase 1.3 — `IndexJob` state machine + `/v1/jobs/{id}` (§37), which will replace the fake `job_id`/`status: "queued"` response `/v1/repositories/index` currently returns with a real, pollable job record. **Do not begin Phase 1.3 without an explicit instruction to do so** — the milestone that requested this work explicitly said to stop after Phase 1.2's Definition of Done.

## Context Handoff Notes

Phase 1.2 implementation is complete and verified in this session:

- **What was done**: audited the existing 12-endpoint `codepulse/api/app.py`, Phase 1.1's `codepulse/auth.py`/`APIKeyModel`/`APIKeyRepositoryModel`, and existing test conventions first, then added: `codepulse/api/security.py` (the central `require_api_key` FastAPI dependency + `AuthorizedKey` authorization context, with `authorize_repository`/`authorize_search_scope` methods); wired `Depends(require_api_key)` into all 12 routes in `codepulse/api/app.py`, adding an `auth.authorize_repository(...)`/`auth.authorize_search_scope(...)` call to the 9 of them that name a specific repository (directly or via a symbol/file's FK chain); added `tests/conftest.py` (shared `isolated_app`/`make_api_key_headers` fixtures); added `tests/test_api_authorization.py` (27 new focused tests); updated `tests/test_api.py`, `tests/test_cors.py` (2 of 9 tests), and `tests/test_no_fabricated_metrics.py` to authenticate via the new shared fixtures instead of the real global app/DB.
- **Process note**: Phase 1.1 was committed and re-verified in a clean, isolated working tree (via `git stash`) strictly before Phase 1.2's code was committed, per the explicit checkpoint-first instruction for this session -- see "Checkpoint discipline note" above.
- **What was explicitly NOT done** (by design, matching 1.2's exact scope): no RBAC/role column, no rate limiting, no ingestion-path safety (allow-listing/SSRF/size caps), no structured `{"error": {...}}` envelope, no `/health/*` endpoint (none exists yet to exempt). `IndexingEngine`/`codepulse/indexer/` was not touched at all — confirmed via `git diff --stat`.
- **What was verified**: 27 new focused tests passed; full suite **139/139 passed, 0 failed**; no Alembic migration needed (confirmed: Phase 1.2 adds no schema); real `codepulse.db` confirmed completely untouched before and after (revision `b0bdd63949f4`, row counts unchanged including `api_keys`/`api_key_repositories` staying at 0 — no test ever read from or wrote to it).
- **Exact files changed this session (uncommitted at time of writing, on top of Phase 1.1's now-committed files)**:
  - New: `codepulse/api/security.py`, `tests/conftest.py`, `tests/test_api_authorization.py`.
  - Modified: `codepulse/api/app.py` (dependency wiring on all 12 routes), `tests/test_api.py`, `tests/test_cors.py`, `tests/test_no_fabricated_metrics.py`, this file.
  - No other file touched. `codepulse/indexer/engine.py`, `codepulse/storage/db.py`, `codepulse/auth.py` were **not** modified by this milestone (Phase 1.1's files, already committed, stay exactly as Phase 1.1 left them).
- **Exact tests run**: `.venv/Scripts/python.exe -m pytest -v` → 139 passed, 0 failed, 13 warnings, ~202s (pre-commit); re-run post-commit, `-q` → 139 passed, 0 failed, ~216s.
- **Committed**: `a3a9b24` (`feat: implement Phase 1.2 API key enforcement`), on top of Phase 1.1's `190d9bb`. Diff reviewed before committing — exactly the 8 intended files (`codepulse/api/security.py`, `codepulse/api/app.py`, `tests/conftest.py`, `tests/test_api_authorization.py`, `tests/test_api.py`, `tests/test_cors.py`, `tests/test_no_fabricated_metrics.py`, this file), nothing else; no `IndexJob`/Phase 1.3 code present (confirmed via `grep`); `codepulse/indexer/` untouched. Post-commit: `git status` clean, commit present in `git log`, full suite re-run and green.
- **What remains**: nothing for this milestone. The next roadmap step is Phase 1.3, which must **not** begin without an explicit instruction to do so, per this milestone's own directive to stop and report at exactly 1.2's Definition of Done.
