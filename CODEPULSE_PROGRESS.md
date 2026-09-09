# CodePulse Development Progress

## Last Updated

2026-09-09

## Current Phase

Phase 1 — Production Core (third sprint item complete)

## Current Milestone

None actively in progress. **Phase 1.3 is complete and verified.** Awaiting explicit instruction to begin Phase 1.4 (in-process token-bucket rate limiter) — do not start it opportunistically.

## Last Completed Milestone

Phase 1.3 — IndexJob state machine + `/v1/jobs/{id}` (verified COMPLETE)

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
| Phase 1.3 — IndexJob state machine + `/v1/jobs/{id}` | COMPLETE | 165/165 full suite passed (139 pre-existing + 26 new); migration applied to real `codepulse.db` (via stamp, see incident note below); re-verified post-commit (clean status, 165/165) | `3feabf8` |

Only mark a milestone COMPLETE here after actual verification (tests run, diff inspected, roadmap cross-checked) — not because code merely exists.

## Current Implementation State

### Phase 1.3 — IndexJob state machine + `/v1/jobs/{id}`

- **Scope determined from the roadmap's own text** (§37 item 1.3: "IndexJob state machine + /v1/jobs/{id}"; §8's literal state-machine description: `queued -> running -> succeeded|failed|cancelled`, "one active job per repository (advisory lock on repo_id)"; §21: "jobs (new -- real job status, replacing the fake job_id)"; §29 Data Model: `IndexJob` grouped with `APIKey`/`AuditEvent` as boundary/operational entities, not core intelligence ones; backlog B9's DoD: "Returned job_id is pollable to real completion"):
  - IN scope (implemented): a new `IndexJobModel` (roadmap §8's exact state values); a new top-level `codepulse/jobs.py` module (`create_job`, `mark_running`, `mark_succeeded`, `mark_failed`, `get_job`, `get_active_job`) providing the state-machine's create/transition/query logic, independent of both `IndexingEngine` and FastAPI; `POST /v1/repositories/index` rewired to create a real job row and drive it through the state machine around its existing `index_directory()` call, replacing the previous fake `job_id = f"job_{int(time.time())}"` (no backing state at all); a new `GET /v1/jobs/{job_id}` endpoint, authenticated and repository-scope-authorized exactly like every other endpoint since Phase 1.2 (job's owning repository resolved via its `repo_id` FK, the same established pattern used for symbol/file endpoints); an advisory "one active job per repository" check (§8's own wording) rejecting a second concurrent indexing request for a repository that already has a `queued`/`running` job (HTTP 409).
  - OUT of scope (explicitly NOT implemented, reserved for later milestones or never assigned a specific sprint item at all): `RepositorySnapshot` / git commit tracking / incremental indexing (roadmap §9, explicitly Phase 3 sprint items 3.1-3.5 -- `index_directory()` is still a full re-index every time, unchanged); a heartbeat-based crash reaper for a `running` job whose worker died (§17 mentions this as a reliability mechanism but no sprint item assigns it to 1.3, and this project has no subprocess/worker infrastructure for that failure mode to be distinct from "this process's background task raised," which IS handled); a cancel endpoint/flow (`cancelled` is a valid, permanent schema value -- matching how Phase 1.1 left `revoked_at` schema-only -- but unreachable, no code path writes it); job expiry/cleanup; rate limiting (Phase 1.4); ingestion safety (Phase 1.5); a structured error envelope (Phase 1.6).
- **Design decisions made explicitly, not inferred from later milestones**:
  1. **A job's repository is resolved synchronously, up front**, via the exact same natural-key resolution `IndexingEngine.index_directory()` already uses internally (`IndexingEngine._resolve_repo_id`, established Phase 0.6) -- not a second identity system. This is required so the new `IndexJob` row can be linked to a real `repo_id`, and so the advisory concurrency check has something to check against, before indexing itself has run.
  2. **The advisory lock is an application-level check, not a hard DB constraint** (`codepulse.jobs.get_active_job`), matching the roadmap's own "advisory lock" wording -- this project runs indexing as in-process background tasks, not multiple concurrent worker processes, so a stronger lock was judged unnecessary at this scale. This is a documented trade-off (see Known Issues), not an oversight.
  3. **`job_id` keeps its pre-existing `"job_<n>"` string shape** (now backed by a real integer id) for response-shape continuity; `GET /v1/jobs/{job_id}` accepts either that form or a bare integer.
  4. **`/v1/repositories/index`'s Phase 1.2 authorization decision is unchanged**: authentication only, still no repository-scope check on this endpoint (it creates/re-indexes a repository that may not exist yet). Phase 1.3 only added job-tracking to it; it did not revisit or expand that endpoint's authorization model, which was Phase 1.2's decision, not this milestone's to make.
  5. **`IndexingEngine`/`codepulse/indexer/` is completely untouched.** All job-lifecycle orchestration (`mark_running`/`mark_succeeded`/`mark_failed` around the `index_directory()` call) lives in `codepulse/api/app.py`'s background task, using `codepulse/jobs.py` -- `index_directory()` itself has no knowledge that jobs exist, mirroring exactly how it has no knowledge of `codepulse/auth.py`.
- **Architecture rule preserved**: `codepulse/jobs.py` is a new top-level module (not under `codepulse/api/` or `codepulse/indexer/`), mirroring `codepulse/auth.py`'s placement -- reusable identically by a future CLI or MCP surface that also triggers indexing, per CLAUDE.md §3's "REST, CLI, and MCP must call the same underlying intelligence functions."
- **Testability preserved without touching the real DB**: all new tests use the shared `isolated_app`/`make_api_key_headers` fixtures (`tests/conftest.py`, established Phase 1.2).

### ⚠️ Incident during migration verification (found and fully remediated this session)

While running an ad-hoc Python one-liner to sanity-check the new routes (`import codepulse.api.app as app_module; ...`), that import's module-level `storage_manager = StorageManager()` executed against the **default** `db_url` (`sqlite:///codepulse.db`, the real database) because the command was run without a `CODEPULSE_DB_URL` override. `StorageManager.__init__` unconditionally calls `Base.metadata.create_all(bind=self.engine)` **alongside** Alembic -- an already-documented, pre-existing piece of technical debt (see Known Issues, tracked since Phase 1.1) -- which silently created the new `index_jobs` table (and its `ix_index_jobs_repo_id` index) directly on the real `codepulse.db`, bypassing Alembic entirely and leaving `alembic_version` stale at `b0bdd63949f4`.

**This was caught immediately**, before the intended migration-safety-verification step even ran, when applying the real Phase 1.3 migration to a disposable copy of `codepulse.db` failed with `table index_jobs already exists` (the copy had been made *after* the accidental creation, so it carried the same table).

**Verified before remediating**: the accidentally-created table's schema (columns, types, nullability, the `ix_index_jobs_repo_id` index, the `repo_id` FK with `ON DELETE CASCADE`) was inspected directly via `PRAGMA table_info`/`sqlite_master` on both the disposable copy and the real database and found **byte-for-byte identical** to what the Alembic migration's `op.create_table` defines -- `Base.metadata.create_all()` uses the exact same `IndexJobModel` SQLAlchemy definition the migration was generated from, so this is expected, not coincidental. The table was empty (0 rows) in both. No pre-existing data (`repositories`, `files`, `symbols`, `dependencies`, `code_embeddings`, `api_keys`, `api_key_repositories`) was touched or altered -- `create_all()` only creates *missing* tables, never modifies existing ones.

**Remediation**: since the schema already matched exactly and held no data, the correct fix was `codepulse.storage.migrations.stamp_database(revision="de22df2fc3ce")` -- Alembic's own mechanism for bringing an already-correct schema under version tracking without re-running DDL (the same mechanism `test_existing_database_stamping_scenario_2` already exercises) -- applied first to the disposable copy (verified: revision now `de22df2fc3ce`, all row counts unchanged, a real insert into `index_jobs` succeeds), then identically to the real `codepulse.db` (verified: same outcome). **No data was lost. No destructive operation was performed.** The real database's substantive verification workflow (copy first, verify, only then apply) was still followed for the *remediation* itself, even though the root-cause DDL had already happened via the accidental `create_all()` call rather than through the normal `run_migrations` path.

**Lesson recorded**: any future ad-hoc Python command that imports `codepulse.api.app` (even just to inspect routes) must set `CODEPULSE_DB_URL` to an isolated/disposable path first, or must avoid importing that module directly -- it has a real-DB-touching import-time side effect via the pre-existing `create_all()`-alongside-Alembic tech debt. This tech debt item (documented since Phase 1.1, unfixed) is now a *demonstrated*, not just theoretical, risk -- see Known Issues.

## Database State

- `codepulse.db` exists at the repository root, untracked by git.
- **Current Alembic revision (real `codepulse.db`): `de22df2fc3ce`** -- reached via `stamp_database`, not `run_migrations`, for the reason described in the incident note above; verified to produce byte-for-byte the same schema `run_migrations` would have.
- Row counts, confirmed **unchanged** before and after this milestone (including through the incident): `repositories` = 1, `files` = 18, `symbols` = 128, `dependencies` = 528, `code_embeddings` = 0, `api_keys` = 0, `api_key_repositories` = 0. New table `index_jobs` = 0 rows (correctly empty).
- The real `codepulse.db` was not written to by any test during this milestone -- every test uses a `tmp_path`-based temporary SQLite database via `isolated_app`. The only real-DB write this session was the unintended `create_all()` side effect (schema only, no rows) described above, and its subsequent `stamp`-based reconciliation (also schema-tracking only, no rows).

## Test State

- Last full suite result: **165 passed, 0 failed, 13 warnings** (`.venv/Scripts/python.exe -m pytest -q`), ~195-236s.
- New focused Phase 1.3 tests:
  - `tests/test_index_jobs.py` (14 tests): `codepulse.jobs` exercised directly against a temporary database -- initial `queued` state, distinct ids per job, `mark_running`/`mark_succeeded`/`mark_failed` transitions with real timestamps, `result` round-trips a real dict through JSON, defensive no-ops for a nonexistent job id, `get_job` returns None for a nonexistent id, `get_active_job` finds queued/running jobs but ignores succeeded/failed ones and is scoped to its own repository.
  - `tests/test_job_endpoints.py` (11 tests): real HTTP behavior -- `POST /v1/repositories/index` returns a real `job_id`/`repository_id`; that `job_id` is genuinely pollable to real completion via `GET /v1/jobs/{job_id}` (status `succeeded`, real `result` matching `index_directory()`'s actual measured return value); a real `index_directory()` failure (monkeypatched) transitions the job to `failed` with the real error message; the advisory lock rejects a second request while a job is active (409) and allows a new one once the prior job reached a terminal state; `GET /v1/jobs/{job_id}` requires authentication (401 without a key), is repository-scope-authorized (403 for a key scoped to a different repository, 200 for a correctly-scoped one), 404s for a nonexistent or malformed job id, and accepts both the `"job_<n>"` and bare-integer id forms.
  - `tests/test_migrations.py::test_phase_1_3_migration_preserves_data_and_adds_index_jobs_table` (new): seeds a temp DB at the Phase 1.1 head (`b0bdd63949f4`), upgrades explicitly to `de22df2fc3ce`, asserts repo/file/symbol/api_key counts preserved, asserts `index_jobs` appears with the expected columns and `ix_index_jobs_repo_id` index, and that a real insert succeeds.
  - `test_fresh_database_migration_scenario_1`/`test_existing_database_stamping_scenario_2` updated: head-revision assertions changed from `b0bdd63949f4` to `de22df2fc3ce`; `expected_tables` extended with `index_jobs`; new assertion block verifying `index_jobs`'s columns and index on a from-scratch migration.
- **Deliberately NOT added**: a test asserting that deleting a `RepositoryModel` row cascades to delete its `IndexJobModel` rows. Written and run during this session -- it failed, revealing that SQLite's `PRAGMA foreign_keys` is never enabled anywhere in this codebase (`codepulse/storage/db.py`), so declared `ON DELETE CASCADE` clauses (on *every* FK in this schema, not just the new one) are not actually enforced by SQLite at the database level, and no ORM-level `cascade="all, delete-orphan"` reverse relationship exists for jobs either. This is pre-existing behavior common to the whole schema, not a Phase 1.3 regression, and fixing it (enabling the pragma project-wide, or adding ORM cascades) is unrelated scope creep for this milestone -- removed the test rather than assert behavior that doesn't hold, and recorded this as a newly-*discovered* (not newly-introduced) known issue below.
- All 139 pre-existing tests re-run unchanged and pass, confirming no regression to Phase 0.1-0.10/1.1/1.2 behavior. `tests/test_no_fabricated_metrics.py::test_index_repository_response_has_no_fabricated_duration_estimate` and `tests/test_api_authorization.py`'s ingestion-endpoint tests needed no changes -- the response shape gained a field (`repository_id`) but no existing assertion broke.
- No existing test was weakened, skipped, or deleted.
- Known warnings: `StarletteDeprecationWarning` (httpx/TestClient) and `FutureWarning`s from `tree_sitter` -- pre-existing, not addressed (out of scope).

## Known Issues

**Roadmap work remaining:** Phase 1.3 (IndexJob state machine + `/v1/jobs/{id}`) is complete. Next per the roadmap's own sprint order (§37) is **Phase 1.4 — in-process token-bucket rate limiter**, then 1.5-1.7 (ingestion safety, structured error responses, security test suite). **None of Phase 1.4+ has been started or implemented — do not infer it exists.**

**Known technical debt (unchanged from Phase 1.2, re-verified still accurate unless noted):**
- `StorageManager.create_tables()` still unconditionally calls `Base.metadata.create_all()` alongside Alembic. **This is no longer purely theoretical debt** -- it caused a real (harmlessly-remediated) incident this session; see the incident note above. Still not fixed in this milestone (unrelated to 1.3's literal scope; fixing it project-wide is its own focused change), but its risk is now demonstrated, not hypothetical. Recommend prioritizing a fix (e.g., gating `create_tables()` behind an explicit opt-in flag, or removing it now that Alembic covers every table) before it causes a real problem outside a disposable/test context.
- **Newly discovered this session (pre-existing, not a Phase 1.3 regression):** SQLite's `PRAGMA foreign_keys` is never enabled anywhere in this codebase, so every `ON DELETE CASCADE` clause declared in the schema (not just `index_jobs`'s) is not actually enforced by SQLite itself at the database level. Where cascade behavior is observed today (e.g. `Repository -> File -> Symbol -> Embedding`), it happens via SQLAlchemy ORM-level `cascade="all, delete-orphan"` on the *relationship* objects, which only fires for ORM-driven deletes with the relationship loaded -- not for raw SQL deletes, and not automatically for every FK (e.g. `IndexJobModel.repo_id` has no such ORM-side cascade set up). No repository-deletion endpoint exists yet, so this has no current user-facing impact, but should be checked before one is added.
- `SymbolType.VARIABLE` / `SymbolType.INTERFACE` enum values defined but never produced by either parser.
- `IndexRepoRequest.repository_id` field in `api/app.py` remains dead/unused (the endpoint resolves the real repository id from `req.url`, not this field) -- not addressed by 1.3, since fixing it isn't required by "IndexJob state machine + /v1/jobs/{id}" and would be unrelated cleanup.
- Dependency (call graph edge) rows still duplicate on every re-index (Phase 3 scope).
- `/v1/autocomplete` and the `languages`/fallback-counts parts of `get_repository_stats`/`get_index_status` are still not repository-scoped in their actual data (Phase 0.7 finding) -- still unfixed, still documented.
- `BM25Index`/`CallGraphBuilder` have no durable backing and do not survive restart (Phase 0.10 finding, regression-tested, not fixed).
- `IndexingEngine.search(search_type="semantic"/"hybrid")` still does not catch `EmbeddingUnavailableError` for the query-side embed call.
- No `/health/live` or `/health/ready` endpoint exists yet -- when one is added, remember §21's "except `/health/*`" exemption from the auth requirement.
- No stale-data cleanup for previously-indexed files that become newly ignored (Phase 0.8 finding).
- No nested/per-directory `.gitignore`/`.codepulseignore` support (Phase 0.8 finding).
- No file-size or file-type caps (Phase 0.8 finding).
- `prometheus-client`/`structlog` remain declared-but-unused (Phase 0.9 finding, deferred to Phase 7).
- **New from Phase 1.3 (not a defect, a scope note):** the advisory one-active-job-per-repository lock is a plain application-level check-then-act (`get_active_job` followed by `create_job`), not wrapped in a transaction/hard lock -- under genuinely concurrent requests for the same repository there is a narrow TOCTOU race where two jobs could both pass the check. The roadmap's own wording ("advisory lock") and this project's single-process/no-concurrent-workers architecture make this an accepted, documented trade-off, not an oversight -- revisit if/when true concurrent request handling for the same repository becomes a real scenario.
- **New from Phase 1.3 (not a defect, a scope note):** a `running` job whose background task is killed by a real process crash (not a caught Python exception -- those already transition to `failed`) would remain stuck at `running` forever; no heartbeat/reaper mechanism exists (§17 describes one, but no sprint item assigns it to this milestone, and no subprocess infrastructure exists for this failure mode to be reachable in this project's current architecture anyway).

**Blockers:** none currently known for starting Phase 1.4.

## Deferred Work

Per roadmap §39 — do not implement until the roadmap explicitly reaches these:

- Kubernetes, Redis, distributed workers, microservices
- Enterprise SSO, full organization/user tenancy
- Learned/adaptive ranking, LLM reranking, LLM-generated explanations as source of truth
- PostgreSQL/pgvector, Tantivy-backed lexical index, embedded ANN vector index (Phase 2)
- Rate limiting, ingestion safety, structured error responses, security test suite (Phase 1.4–1.7)
- `RepositorySnapshot` / git-diff incremental indexing / rename detection / heartbeat-based job reaper (Phase 3, roadmap §9/§17)
- Role-based permissions (viewer/indexer/admin) on API keys (Phase 7 / B25)
- API key revoke() flow / key expiry / audit logging; a job cancel() flow
- Call-graph confidence scoring, impact-analysis depth bucketing, module clustering, stale-data cleanup for newly-ignored files (Phase 3/4)
- MCP server (Phase 5)
- VS Code extension, Web UI (Phase 6)
- Observability infrastructure (Phase 7)

## Current Risks

- `_persist_to_db` still has four upsert blocks plus the dependency-insert block in one function/session — unchanged by Phase 1.3.
- The `languages`/fallback-counts repository-isolation gap (Phase 0.7 finding) remains unfixed at the data level.
- Only `repositories`, `files`, `symbols`, `code_embeddings` are idempotent; `dependencies` is not (Phase 3 scope). `index_jobs` is intentionally NOT idempotent either (see `IndexJobModel`'s docstring) -- every indexing attempt gets its own row by design, a run-history table, not an upsert entity.
- `StorageManager.create_tables()`'s `create_all()`-alongside-Alembic behavior is now a demonstrated real risk (see the incident note above), not just documented tech debt -- any future ad-hoc script importing `codepulse.api.app`/constructing a default `StorageManager()` without an explicit isolated `db_url` can silently mutate the real database's schema (though, per the incident, never its data).
- Whoever starts Phase 1.4 should re-verify this document against the actual repository per CLAUDE.md §13 before trusting it, as always.

## Exact Next Action

NEXT ACTION:
Both Phase 1.1 (`190d9bb`), Phase 1.2 (`a3a9b24`/`16fa747`), and Phase 1.3 (`3feabf8`) are committed and independently verified. Next, begin Phase 1.4 — in-process token-bucket rate limiter (§37), which will add request-rate limiting (429 + retry-after) without introducing Redis or any external dependency (backlog B11: "Excess requests receive 429 with retry-after"). **Do not begin Phase 1.4 without an explicit instruction to do so.**

## Context Handoff Notes

Phase 1.3 implementation is complete and verified in this session:

- **What was done**: audited the existing `api/app.py`, Phase 1.1/1.2's `codepulse/auth.py`/`codepulse/api/security.py`, and roadmap §8/§9/§17/§21/§29/§37/backlog-B9 text first, then added: `IndexJobModel` in `codepulse/storage/db.py`; a new top-level `codepulse/jobs.py` module (state-machine create/transition/query logic, independent of `IndexingEngine` and FastAPI); rewired `POST /v1/repositories/index` in `codepulse/api/app.py` to resolve a real `repo_id` up front, check/enforce the advisory one-active-job-per-repository lock, create a real job row, and drive it through `queued -> running -> succeeded|failed` around the existing `index_directory()` call; added a new `GET /v1/jobs/{job_id}` endpoint (authenticated + repository-scope-authorized via the job's `repo_id` FK, the established Phase 1.2 pattern); a new Alembic migration `de22df2fc3ce` (purely additive, one new table + one index); `tests/test_index_jobs.py` (14 tests) and `tests/test_job_endpoints.py` (11 tests); updated `tests/test_migrations.py` (new Phase 1.3 migration test + updated head-revision/table assertions in the fresh-DB and stamping tests).
- **Incident, found and fully remediated during this session**: an ad-hoc sanity-check import of `codepulse.api.app` (run without an isolated `CODEPULSE_DB_URL`) triggered `StorageManager`'s pre-existing `create_all()`-alongside-Alembic tech debt against the real `codepulse.db`, silently creating the new `index_jobs` table (schema only, 0 rows, verified byte-for-byte identical to what the migration defines) without updating `alembic_version`. Caught immediately when the intended migration-safety verification step (applying the migration to a disposable copy) failed with "table already exists." Both the disposable copy and the real database were remediated identically and safely via `stamp_database(revision="de22df2fc3ce")` (Alembic's own mechanism for tracking an already-correct schema, not a destructive operation) -- verified before and after: all pre-existing row counts unchanged, table present and empty, a real insert succeeds. Full details in "Current Implementation State" above.
- **What was explicitly NOT done** (by design, matching 1.3's exact scope): no `RepositorySnapshot`/incremental indexing, no heartbeat/reaper, no cancel endpoint, no rate limiting, no ingestion safety, no structured error envelope. `IndexingEngine`/`codepulse/indexer/` was not touched at all — confirmed via `git diff --stat`.
- **What was verified**: 14 + 11 + 1 = 26 new focused tests passed; full suite **165/165 passed, 0 failed**; the migration was verified on a disposable copy first (after resolving the incident above), then the real `codepulse.db` was brought to the same verified state; real `codepulse.db` row counts confirmed unchanged throughout (`repositories=1, files=18, symbols=128, dependencies=528, code_embeddings=0, api_keys=0, api_key_repositories=0, index_jobs=0`), revision now `de22df2fc3ce`.
- **Exact files committed (`3feabf8`, on top of Phase 1.1/1.2's already-committed files)**:
  - New: `codepulse/jobs.py`, `alembic/versions/de22df2fc3ce_add_index_job_model.py`, `tests/test_index_jobs.py`, `tests/test_job_endpoints.py`.
  - Modified: `codepulse/storage/db.py` (added `IndexJobModel`), `codepulse/api/app.py` (job creation/tracking wired into `index_repository`; new `get_job_status` endpoint), `tests/test_migrations.py`, this file.
  - No other file touched. `codepulse/indexer/engine.py` and `codepulse/api/security.py`/`codepulse/auth.py` were **not** modified by this milestone -- confirmed via `git diff --stat` before committing.
- **Exact tests run**: `.venv/Scripts/python.exe -m pytest -q` → 165 passed, 0 failed, 13 warnings (run three times: before the incident remediation, after it, and again post-commit -- all green).
- **Committed**: `3feabf8` (`feat: implement Phase 1.3 index job state machine`), on top of Phase 1.2's `a3a9b24`/`16fa747`. Diff reviewed before committing -- exactly the 8 intended files, nothing else; no Phase 1.4+ code present (confirmed via `grep`); `codepulse/indexer/` untouched. Post-commit: `git status` clean, commit present in `git log` in the correct position, full suite re-run and green.
- **What remains**: nothing for this milestone. The next roadmap step is Phase 1.4, which must **not** begin without an explicit instruction to do so.
