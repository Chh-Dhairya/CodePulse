# CodePulse Development Progress

## Last Updated

2026-09-09

## Current Phase

Phase 1 — Production Core (fifth sprint item complete)

## Current Milestone

None actively in progress. **Phase 1.5 is complete and verified.** Awaiting explicit instruction to begin Phase 1.6 (structured error response format across all endpoints) — do not start it opportunistically.

## Last Completed Milestone

Phase 1.5 — Ingestion safety: path allow-list, SSRF check, size caps (verified COMPLETE)

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
| Phase 1.5 — Ingestion safety: path allow-list, SSRF check, size caps | COMPLETE | 204/204 full suite passed + 1 environment-driven skip (179 pre-existing + 25 new); audited against the roadmap requirement-by-requirement (see Context Handoff Notes) before commit; no migration required; real `codepulse.db` untouched | *(uncommitted as of this update — see Exact Next Action)* |

Only mark a milestone COMPLETE here after actual verification (tests run, diff inspected, roadmap cross-checked) — not because code merely exists.

## Current Implementation State

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
- **Current Alembic revision (real `codepulse.db`): `de22df2fc3ce`** -- **unchanged by Phase 1.5**. No migration was created, needed, or considered: this milestone adds only application-layer/config logic (a new safety module, config fields, and two checks inside `IndexingEngine`), no new table or column.
- Row counts, confirmed **unchanged** before, during, and after this milestone: `repositories` = 1, `files` = 18, `symbols` = 128, `dependencies` = 528, `code_embeddings` = 0, `api_keys` = 0, `api_key_repositories` = 0, `index_jobs` = 0.
- The real `codepulse.db` was not written to by any test during this milestone -- every test uses either pure functions with no DB at all (`validate_local_path`/`validate_git_url`) or the shared `isolated_app` fixture (a `tmp_path`-based temporary SQLite database).

## Test State

- Last full suite result: **204 passed, 1 skipped, 13 warnings** (`.venv/Scripts/python.exe -m pytest -v`), ~228s. The 1 skip is `tests/test_ingestion_safety.py::test_symlink_escaping_the_allowed_root_is_rejected`, skipped because this Windows environment does not permit unprivileged symlink creation -- empirically confirmed genuine (`os.symlink` raises `OSError [WinError 1314]`, "A required privilege is not held by the client," i.e. missing `SeCreateSymbolicLinkPrivilege`), not masking a bug in the test itself; the equivalent `..`-traversal-escape test (`test_dot_dot_traversal_escaping_the_allowed_root_is_rejected`) exercises the same `validate_local_path` code path and does pass unconditionally.
- **2 boundary tests added during a post-implementation compliance audit** (`test_file_count_exactly_at_the_cap_succeeds`, `test_file_exactly_at_the_size_cap_is_not_skipped`) -- the original test set only exercised comfortably-under/clearly-over cases, not the exact `== max` boundary; the audit found this gap (both caps use an inclusive `> max` comparison, so a count/size exactly equal to the configured max must succeed, not fail) and added tests proving it, rather than just asserting the gap existed. One of these tests initially failed for an unrelated reason -- a test bug, not a production defect: `Path.write_text` on Windows translates `\n` to `\r\n`, so `len(content)` in characters did not equal the file's real on-disk byte size; fixed by reading the real size via `os.path.getsize` instead of assuming it.
- New focused Phase 1.5 tests:
  - `tests/test_ingestion_safety.py` (15 tests, 1 skipped): `validate_local_path`/`validate_git_url` exercised directly, no HTTP, no database -- unrestricted-by-default allow-list, a path inside/equal-to/outside an allowed root, a same-prefix sibling directory NOT falsely permitted (guards against a naive string-prefix bug), `..`-traversal that escapes vs. stays inside the allowed root, a symlink escape (skipped per above), a bad URL scheme rejected, https/ssh with a safe public IP host permitted (literal IPs used throughout so no test depends on real DNS/network access), loopback/link-local(the classic cloud-metadata SSRF target)/private-range hosts all rejected, a URL with no host rejected.
  - `tests/test_ingestion_safety_endpoint.py` (11 tests): real HTTP behavior against `/v1/repositories/index` -- an SSRF-unsafe git URL rejected (400) before anything else happens; a bad scheme rejected (400); a *safe* git URL still gets an honest 501 (no clone implementation exists); a local path outside a configured allow-list rejected (400), inside it accepted (202), and unrestricted by default (202); a repository exceeding the configured file-count cap fails its job with the real error message (polled via `GET /v1/jobs/{job_id}`); a repository within the cap succeeds; a repository with a file count *exactly equal* to the cap succeeds (boundary test); an oversized file is skipped but the job still succeeds, with the real (reduced) file count in its result; a file whose size is *exactly equal* to the cap is NOT skipped (boundary test).
  - `tests/test_config.py` extended (not a new file): `test_builtin_defaults_loaded` now asserts the three new fields' defaults; `test_env_var_type_coercion` now asserts `CODEPULSE_INGESTION_*` env vars coerce correctly (list/int).
- All 179 pre-existing tests re-run unchanged and pass, confirming no regression to Phase 0.1-0.10/1.1-1.4 behavior. No existing ingestion-endpoint test needed a change: the default `ingestion_allowed_roots=[]` (unrestricted) and generous default caps (50000 files, 5MB/file) never engage for any existing test's small `tmp_path` fixtures.
- No existing test was weakened or deleted; the 1 skip is environment-driven with an explicit, honest reason (see above), not a disguised weakening.
- Known warnings: `StarletteDeprecationWarning` (httpx/TestClient) and `FutureWarning`s from `tree_sitter` -- pre-existing, not addressed (out of scope).

## Known Issues

**Roadmap work remaining:** Phase 1.5 (ingestion safety) is complete. Next per the roadmap's own sprint order (§37) is **Phase 1.6 — structured error response format across all endpoints**, then 1.7 (security test suite). **None of Phase 1.6+ has been started or implemented — do not infer it exists.**

**Known technical debt (unchanged from Phase 1.4, re-verified still accurate unless noted):**
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

**Blockers:** none currently known for starting Phase 1.6.

## Deferred Work

Per roadmap §39 — do not implement until the roadmap explicitly reaches these:

- Kubernetes, Redis, distributed workers, microservices
- Enterprise SSO, full organization/user tenancy
- Learned/adaptive ranking, LLM reranking, LLM-generated explanations as source of truth
- PostgreSQL/pgvector, Tantivy-backed lexical index, embedded ANN vector index (Phase 2)
- Structured error responses, security test suite (Phase 1.6–1.7)
- Actual git-URL cloning/fetching; request-body size caps; a global/per-tenant concurrent-job cap (distinct from Phase 1.3's per-repository lock) -- none assigned a milestone anywhere in this roadmap
- `RepositorySnapshot` / git-diff incremental indexing / rename detection / heartbeat-based job reaper (Phase 3, roadmap §9/§17)
- Role-based permissions (viewer/indexer/admin) on API keys (Phase 7 / B25)
- API key revoke() flow / key expiry / audit logging; a job cancel() flow
- Call-graph confidence scoring, impact-analysis depth bucketing, module clustering, stale-data cleanup for newly-ignored files (Phase 3/4)
- MCP server (Phase 5)
- VS Code extension, Web UI (Phase 6)
- Observability infrastructure (Phase 7)

## Current Risks

- `_persist_to_db` still has four upsert blocks plus the dependency-insert block in one function/session — unchanged by Phase 1.5.
- The `languages`/fallback-counts repository-isolation gap (Phase 0.7 finding) remains unfixed at the data level.
- `StorageManager.create_tables()`'s `create_all()`-alongside-Alembic behavior remains a demonstrated real risk (Phase 1.3's incident) -- unchanged by Phase 1.5, which introduced no new model.
- Rate-limit identity evasion via key rotation (Phase 1.4) means the limiter is a resource-exhaustion speed bump, not a strong anti-abuse guarantee.
- `ingestion_allowed_roots` is unrestricted by default (Phase 1.5, deliberate) -- do not describe a fresh install as "path-restricted."
- `validate_git_url`'s SSRF check has no live fetch path to protect yet -- do not describe git-URL ingestion as supported.
- Whoever starts Phase 1.6 should re-verify this document against the actual repository per CLAUDE.md §13 before trusting it, as always.

## Exact Next Action

NEXT ACTION:
Phase 1.1 (`190d9bb`), Phase 1.2 (`a3a9b24`/`16fa747`), Phase 1.3 (`3feabf8`/`c9d27fa`), and Phase 1.4 (`571cf3a`/`b80c776`) are committed and independently verified. Next, begin Phase 1.6 — structured error response format across all endpoints (§37), which will introduce the `{"error": {"code", "message", "details"}}` envelope §21 describes, replacing the plain `{"detail": "..."}` shape every endpoint (including this milestone's new 400/501 ingestion-safety responses) currently uses. **Do not begin Phase 1.6 without an explicit instruction to do so.**

## Context Handoff Notes

Phase 1.5 implementation is complete and verified in this session:

- **What was done**: audited `api/app.py`'s existing `index_repository` endpoint, `IndexingEngine.index_directory()`'s discovery/parse loop, `codepulse/config.py`'s established field/env-var pattern, and roadmap §15/§37/backlog-B10 text first (confirmed via `grep` that no git-clone implementation exists anywhere in this codebase before deciding how to scope the "SSRF check" requirement), then added: three new `CodePulseConfig` fields (`ingestion_allowed_roots`, `ingestion_max_files_per_repo`, `ingestion_max_file_size_bytes`) with env var mappings; a new top-level `codepulse/ingestion_safety.py` module (`IngestionSafetyError`, `validate_local_path`, `validate_git_url`); wired both validators into `POST /v1/repositories/index` (git-URL SSRF check + honest 501 for non-local sources; local-path allow-list check) in `codepulse/api/app.py`; added a file-count cap and a per-file size cap inside `IndexingEngine.index_directory()` in `codepulse/indexer/engine.py` (a deliberate, explicitly-justified exception to the "IndexingEngine untouched" pattern of Phases 1.2-1.4); `tests/test_ingestion_safety.py` (15, 1 environment-driven skip) + `tests/test_ingestion_safety_endpoint.py` (9); extended `tests/test_config.py` (2 tests) for the new fields.
- **What was explicitly NOT done** (by design, matching 1.5's exact scope): no actual git-URL cloning/fetching (no sprint item assigns this a milestone at all); no request-body size caps (a server/ASGI-layer concern, not application code); no global/per-tenant concurrent-job cap (distinct from Phase 1.3's per-repository lock); no structured error envelope (Phase 1.6); no security test suite (Phase 1.7). `codepulse/auth.py`, `codepulse/jobs.py`, `codepulse/rate_limit.py`, `codepulse/api/security.py`, and `codepulse/storage/db.py` were **not** modified -- confirmed via `git diff --stat`; no new Alembic migration was needed or created.
- **What was verified**: 15+11=26 new focused tests passed (1 environment-driven skip, empirically confirmed genuine, not a weakened test) plus 2 extended config tests; full suite **204 passed, 1 skipped, 0 failed**; real `codepulse.db` confirmed unchanged (schema and every row count) both before and after this session's own ad-hoc `codepulse.api.app` sanity-check import (Phase 1.3's incident lesson re-applied: no new model was added this milestone, so `create_all()` had nothing new to create).
- **A mandatory post-implementation compliance audit was performed before committing** (requirement-by-requirement against the roadmap's exact §15/B10 text, not the milestone title): confirmed no git-clone implementation exists anywhere (grep-verified fresh); confirmed `codepulse/cli.py` calls `IndexingEngine.index_directory()` directly, bypassing the API-boundary allow-list/SSRF checks entirely -- determined this is CORRECT, not a gap, because those two checks protect "the dangerous endpoint" (B10's own words; the REST API specifically, per its Technical Scope column: "`api/app.py` ingestion handler"), while the CLI is a trusted-local-user surface with no API-key/network-exposure concept at all; confirmed the file-count/size caps (living inside `IndexingEngine` itself, not just the API layer) ARE inherited by the CLI path too, which is a positive, CLAUDE.md-§3-consistent property ("REST, CLI, and MCP must call the same underlying intelligence functions") rather than an inconsistency; empirically verified Windows path-casing normalizes consistently through `Path.resolve()` for the allow-list containment check; empirically confirmed the symlink-test skip is a genuine environment restriction, not a masked bug; identified and fixed a genuine test-coverage gap (missing exact-boundary tests for both caps) by adding the 2 tests described above. No implementation defect was found -- see the full audit trace in this session's transcript for the requirement-by-requirement table.
- **Exact files changed this session (uncommitted at time of writing, on top of Phase 1.1-1.4's already-committed files)**:
  - New: `codepulse/ingestion_safety.py`, `tests/test_ingestion_safety.py`, `tests/test_ingestion_safety_endpoint.py`.
  - Modified: `codepulse/config.py` (3 new fields + env mappings), `codepulse/api/app.py` (ingestion-safety checks wired into `index_repository`), `codepulse/indexer/engine.py` (file-count/size caps -- see the explicit justification in "Current Implementation State" above), `tests/test_config.py` (2 tests extended), this file.
  - No other file touched.
- **Exact tests run**: `.venv/Scripts/python.exe -m pytest -v` → 204 passed, 1 skipped, 0 failed, 13 warnings, ~228s (includes the 2 boundary tests added during the compliance audit).
- **What remains**: **committing this work to git** (not yet done as of this document update — pending an explicit commit instruction). After that, the next roadmap step is Phase 1.6, which must **not** begin without an explicit instruction to do so.
