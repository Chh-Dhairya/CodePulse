# CodePulse Development Progress

## Last Updated

2026-09-08

## Current Phase

Phase 1 — Production Core (first sprint item complete)

## Current Milestone

None actively in progress. **Phase 1.1 is complete and verified.** Awaiting explicit instruction to begin Phase 1.2 (central authorization dependency) — do not start it opportunistically.

## Last Completed Milestone

Phase 1.1 — API key model + hashing + creation flow (verified COMPLETE)

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
| Phase 1.1 — API key model + hashing + creation flow | COMPLETE | 112/112 full suite passed (99 pre-existing + 13 new); migration applied to real `codepulse.db` | *(uncommitted as of this update — see Exact Next Action)* |

Only mark a milestone COMPLETE here after actual verification (tests run, diff inspected, roadmap cross-checked) — not because code merely exists.

## Current Implementation State

### Phase 1.1 — API key model + hashing + creation flow

- **Scope determined from the roadmap's own text** (§15 Security Architecture, §21 API Strategy, §29 Data Model, §37 sprint item 1.1), deliberately excluding anything belonging to later milestones:
  - IN scope (implemented): an `APIKeyModel` (id, name, `key_hash`, `key_prefix`, `created_at`, `revoked_at`) + an `APIKeyRepositoryModel` many-to-many grant table for "scoped to one or more repositories" (§15); cryptographically random raw-key generation; SHA-256 hashing for storage (raw key never persisted anywhere); a creation flow returning the raw key to the caller exactly once; a narrow verification *primitive* (hash a candidate key, look up the matching non-revoked row) proving the hash/lookup mechanism works.
  - OUT of scope (explicitly NOT implemented, reserved for later milestones): wiring verification into a FastAPI dependency, request-header parsing, HTTP 401/403 responses, or any endpoint (**Phase 1.2**); a mutating `revoke()` flow (only the `revoked_at` schema column exists, defensively respected by `verify_api_key`); role-based permissions / viewer-indexer-admin roles (**Phase 7 / B25** — no `role` column exists); rate limiting, key expiry, audit logging.
- **Architecture rule preserved**: `codepulse/auth.py` lives outside `codepulse/indexer/`. `IndexingEngine` and the rest of the intelligence core are completely untouched by this milestone — authentication logic lives only at the module that will later back the API boundary, per CLAUDE.md §3.
- **Hashing algorithm choice**: SHA-256 (matching the project's existing `compute_content_hash` convention), not bcrypt/argon2. Reasoned explicitly: the input (`secrets.token_urlsafe(32)`, 256 bits of entropy) is already a high-entropy random secret, not a low-entropy user-chosen password, so there is no offline-guessing risk a slow/salted hash would mitigate — and a fast hash keeps Phase 1.2's lookup-by-hash a simple indexed equality query.
- **Raw key lifecycle**: generated with a `cpk_` prefix via `secrets.token_urlsafe(32)`; returned to the caller exactly once, inside `CreatedAPIKey` (a frozen dataclass), at creation time; never recoverable afterward (GitHub/AWS/Stripe-style lifecycle). `key_prefix` (first ~12 chars, non-secret) is persisted purely for a future display/identification UI.
- **`StorageManager.SessionLocal` default (`expire_on_commit=True`) respected**: `create_api_key` extracts every needed value into `CreatedAPIKey` before the session closes, rather than returning the raw (soon-to-be-detached) ORM object — the same pattern already used by `_resolve_repo_id` in the indexer.
- **Repository scoping validated defensively**: `create_api_key(..., repository_ids=[...])` raises `ValueError` (rolling back, leaving no partial state) if asked to scope a key to a nonexistent repository id.

## Database State

- `codepulse.db` exists at the repository root, untracked by git.
- **Current Alembic revision (real `codepulse.db`): `b0bdd63949f4`** — advanced from `49572f834366` this session via the new Phase 1.1 migration (`add_api_key_model`), purely additive (creates `api_keys` and `api_key_repositories`; touches no existing table/column/row).
- **Migration safety workflow followed**: the real `codepulse.db` was first copied to a disposable scratch location; the migration was applied to that copy and its post-migration schema (both new tables' columns and unique constraints) and unchanged row counts were verified there first; only then was the same migration applied to the real database, with an identical before/after row-count check.
- Row counts on real `codepulse.db`, confirmed **unchanged** before and after this migration: `repositories` = 1, `files` = 18, `symbols` = 128, `dependencies` = 528, `code_embeddings` = 0. New tables `api_keys` = 0 rows, `api_key_repositories` = 0 rows (correctly empty — Phase 1.1 added no API keys to the real database, only schema).

## Test State

- Last full suite result: **112 passed, 0 failed, 13 warnings** (`.venv/Scripts/python.exe -m pytest -v`), ~199s.
- Focused Phase 1.1 tests: `tests/test_api_keys.py` (12 new tests, all against `tmp_path`-based temporary SQLite databases, never the real `codepulse.db`): raw-key prefix/randomness, SHA-256 hashing convention + determinism + distinguishing different keys, creation persists only the hash (never the raw key, verified by direct inequality checks against every persisted column), two created keys differ, DB-level uniqueness on `key_hash` (`IntegrityError`), verification succeeds for a valid key / fails for an unknown key / fails for a revoked key, repository scoping records the correct grants, no-repository-ids creates no scope rows, scoping to a nonexistent repository id raises `ValueError` and leaves no partial `APIKeyModel` row behind.
- Also added: `tests/test_migrations.py::test_phase_1_1_migration_preserves_data_and_adds_api_key_tables` (seeds a temp DB at the Phase 0.5 head, upgrades explicitly to `b0bdd63949f4`, asserts repo/file/symbol counts preserved, asserts both new tables exist with working inserts, asserts the `key_hash` unique constraint is enforced at the DB level on the freshly-migrated schema).
- `tests/test_migrations.py::test_fresh_database_migration_scenario_1` and `test_existing_database_stamping_scenario_2` updated: head-revision assertions changed from `49572f834366` to `b0bdd63949f4`; `expected_tables` extended with `api_keys`/`api_key_repositories`; new assertion block verifying both new tables' columns and unique constraints on a from-scratch migration.
- **Bug fixed (test-only, not production code)**: `test_phase_0_5_migration_preserves_data_and_adds_embedding_fields` was still upgrading to generic `revision="head"` for its own migration step — harmless while `49572f834366` was head, but silently wrong now that `b0bdd63949f4` is head (this is the same class of bug already fixed for the Phase 0.3 and Phase 0.4 migration tests in earlier sessions). Pinned to `revision="49572f834366"` explicitly. The negative/failure-case tests (`test_phase_0_X_migration_refuses_to_apply_over_duplicate_data`) were re-checked and confirmed safe as-is: they expect failure at their own migration step, reached before any later migration in the chain, so generic `"head"` never actually reaches a later revision in those tests.
- All 99 pre-existing tests re-run unchanged and pass, confirming no regression to Phase 0.1–0.10 behavior.
- No existing test was weakened, skipped, or deleted.
- Known warnings: `StarletteDeprecationWarning` (httpx/TestClient) and `FutureWarning`s from `tree_sitter` — pre-existing, not addressed (out of scope).

## Known Issues

**Roadmap work remaining:** Phase 1.1 (API key model + hashing + creation flow) is complete. Next per the roadmap's own sprint order (§37) is **Phase 1.2 — central authorization dependency** (wiring key verification into a FastAPI dependency enforced on routes), then 1.3+ (real `IndexJob` state machine, rate limiting, ingestion safety, structured error responses, security test suite). **None of Phase 1.2+ has been started or implemented — do not infer it exists.**

**Known technical debt (unchanged from Phase 0.10, re-verified still accurate unless noted):**
- `StorageManager.create_tables()` still unconditionally calls `Base.metadata.create_all()` alongside Alembic.
- `SymbolType.VARIABLE` / `SymbolType.INTERFACE` enum values defined but never produced by either parser.
- Dead/unused `job_id` generation in `api/app.py` — Phase 1 (`IndexJob`) scope, not yet addressed by 1.1.
- Dependency (call graph edge) rows still duplicate on every re-index (Phase 3 scope).
- `languages` breakdown (`get_repository_stats`) and the "repo not found" fallback counts (`get_index_status`) are not repository-scoped (Phase 0.7 finding, still unfixed).
- `BM25Index`/`CallGraphBuilder` have no durable backing and do not survive restart (Phase 0.10 finding, regression-tested, not fixed — out of scope for both 0.10 and 1.1).
- `IndexingEngine.search(search_type="semantic"/"hybrid")` still does not catch `EmbeddingUnavailableError` for the query-side embed call.
- No `/health/live` or `/health/ready` endpoint exists yet.
- No stale-data cleanup for previously-indexed files that become newly ignored (Phase 0.8 finding).
- No nested/per-directory `.gitignore`/`.codepulseignore` support (Phase 0.8 finding).
- No file-size or file-type caps (Phase 0.8 finding).
- `prometheus-client`/`structlog` remain declared-but-unused (Phase 0.9 finding, deferred to Phase 7).
- **New from Phase 1.1 (not a defect, a scope note):** `api_keys`/`api_key_repositories` exist and are fully functional (create + verify), but nothing in the API layer currently checks them — every endpoint remains completely unauthenticated until Phase 1.2 wires in enforcement. Do not describe the API as "secured" or "protected" based on this milestone alone.
- **New from Phase 1.1 (not a defect, a scope note):** no `role` column exists on `APIKeyModel` — role-based permissions are explicitly Phase 7 / B25 scope, not modeled at all yet.
- **New from Phase 1.1 (not a defect, a scope note):** no `revoke()` mutating flow exists — only the `revoked_at` schema column, which `verify_api_key` respects defensively. There is currently no way to actually set it outside of a direct DB write (as done in the corresponding test).

**Blockers:** none currently known for starting Phase 1.2.

## Deferred Work

Per roadmap §39 — do not implement until the roadmap explicitly reaches these:

- Kubernetes, Redis, distributed workers, microservices
- Enterprise SSO, full organization/user tenancy
- Learned/adaptive ranking, LLM reranking, LLM-generated explanations as source of truth
- PostgreSQL/pgvector, Tantivy-backed lexical index, embedded ANN vector index (Phase 2)
- Central authorization dependency / endpoint enforcement, real `IndexJob` state machine, rate limiting, ingestion safety, structured error responses, security test suite (Phase 1.2–1.7)
- Role-based permissions (viewer/indexer/admin) on API keys (Phase 7 / B25)
- API key revoke() flow / key expiry / audit logging (not yet scheduled to a specific sub-phase beyond "not 1.1")
- Call-graph confidence scoring, impact-analysis depth bucketing, module clustering, stale-data cleanup for newly-ignored files (Phase 3/4)
- MCP server (Phase 5)
- VS Code extension, Web UI (Phase 6)
- Observability infrastructure (Phase 7)

## Current Risks

- `_persist_to_db` still has four upsert blocks plus the dependency-insert block in one function/session — unchanged by Phase 1.1.
- The `languages`/fallback-counts repository-isolation gap (Phase 0.7 finding) remains unfixed.
- Only `repositories`, `files`, `symbols`, `code_embeddings` are idempotent; `dependencies` is not (Phase 3 scope).
- Every API endpoint remains fully unauthenticated — `api_keys` exist in the schema and are creatable/verifiable, but nothing currently enforces them. Anyone reading this document out of context could wrongly assume Phase 1.1 "adds security" to the running API; it does not, by itself.
- Whoever starts Phase 1.2 should re-verify this document against the actual repository per CLAUDE.md §13 before trusting it, as always.

## Exact Next Action

NEXT ACTION:
Commit the Phase 1.1 checkpoint (once explicitly instructed to do so — not yet requested as of this update). Then begin Phase 1.2 — central authorization dependency (§37), which will wire `codepulse.auth.verify_api_key` into an actual FastAPI dependency and enforce it on routes. **Do not begin Phase 1.2 without an explicit instruction to do so** — the milestone that requested this work explicitly said to stop after Phase 1.1's Definition of Done.

## Context Handoff Notes

Phase 1.1 implementation is complete and verified in this session:

- **What was done**: audited existing models/storage/migrations/API/config/test conventions first (per CLAUDE.md §5 and the milestone's explicit instructions), then added: `APIKeyModel` + `APIKeyRepositoryModel` in `codepulse/storage/db.py`; a new `codepulse/auth.py` module (`generate_raw_api_key`, `hash_api_key`, `create_api_key`, `verify_api_key`, `CreatedAPIKey`) living outside `codepulse/indexer/` per the architecture rule; a new Alembic migration `alembic/versions/b0bdd63949f4_add_api_key_model.py` (purely additive, creates both new tables); a new focused test file `tests/test_api_keys.py` (12 tests); and updates to `tests/test_migrations.py` (new Phase 1.1 migration test, updated head-revision/table assertions in the fresh-DB and stamping tests, and a fix to a latent generic-`"head"` bug in the Phase 0.5 migration test — the same class of bug already fixed twice before in earlier phases).
- **What was explicitly NOT done** (by design, matching 1.1's exact scope): no FastAPI dependency, no endpoint wiring, no HTTP 401/403 handling, no `role` column/RBAC, no mutating `revoke()` flow, no rate limiting, no key expiry, no audit logging. `IndexingEngine` and the rest of the intelligence core were not touched at all.
- **What was verified**: 12 new focused tests passed (`tests/test_api_keys.py`) plus 1 new migration test; full suite **112/112 passed, 0 failed**; the new migration was tested on a disposable copy of the real `codepulse.db` first (schema + unchanged row counts confirmed on the copy), then applied to the real `codepulse.db` itself, with an identical before/after check (`repositories=1, files=18, symbols=128, dependencies=528, code_embeddings=0` unchanged; revision advanced `49572f834366` → `b0bdd63949f4`; new tables present and correctly empty).
- **Exact files changed this session (uncommitted at time of writing)**:
  - Modified: `codepulse/storage/db.py` (added `APIKeyModel`, `APIKeyRepositoryModel`), `tests/test_migrations.py` (new test + assertion updates + the generic-`"head"` fix described above), this file.
  - New: `codepulse/auth.py`, `alembic/versions/b0bdd63949f4_add_api_key_model.py`, `tests/test_api_keys.py`.
  - No other file touched. `codepulse/api/app.py` and `codepulse/indexer/engine.py` were **not** modified — no endpoint or intelligence-core wiring exists yet, by design.
- **Exact tests run**: `.venv/Scripts/python.exe -m pytest -v` → 112 passed, 0 failed, 13 warnings, ~199s.
- **What remains**: **committing this work to git** (not yet done as of this document update — no explicit commit instruction was given for this specific milestone within this session; the established pattern throughout this project is to commit only on an explicit separate instruction). After that, the next roadmap step is Phase 1.2, which must **not** begin without an explicit instruction to do so, per this milestone's own directive to stop and report at exactly 1.1's Definition of Done.
