# CodePulse Development Progress

## Last Updated

2026-09-05

## Current Phase

Phase 0 — Foundation / Correctness

## Current Milestone

None actively in progress. Awaiting start of Phase 0.9 — Fix CORS policy defaults; trim `pyproject.toml`; add `rich`; delete shipped `codepulse.db`; git init (git init already done in a prior phase).

## Last Completed Milestone

Phase 0.8 — `.gitignore`/`.codepulseignore` discovery and ignore-rule handling (verified COMPLETE)

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
| Phase 0.8 — `.gitignore`/`.codepulseignore` discovery | COMPLETE | 86/86 full suite passed (74 pre-existing + 12 new); no migration required; verified against the real `codepulse.db` (revision unchanged) | `b27381a` |
| Phase 0.9 — CORS/pyproject/rich cleanup | NOT IMPLEMENTED | — | — |

Only mark a milestone COMPLETE here after actual verification (tests run, diff inspected, roadmap cross-checked) — not because code merely exists.

## Current Implementation State

- **Ignore-rule handling is now implemented (Phase 0.8).** New module `codepulse/indexer/ignore.py` (`RepositoryIgnoreRules`) loads a repository's root-level `.gitignore` and `.codepulseignore` files once per `index_directory()` call and answers `is_ignored(rel_path, is_dir)` for every discovered path, using the real `pathspec` library (`gitignore`/gitwildmatch pattern semantics — negation, `**` globs, directory-only trailing-slash patterns, standard precedence) rather than a hand-rolled matcher.
- **Exact semantics implemented (see `ignore.py`'s module docstring for the full rationale):**
  - `.gitignore`: standard gitignore pattern syntax, root-level only.
  - `.codepulseignore`: same syntax, **additive** to `.gitignore` ("beyond git's own," per roadmap §16) — not an override/replacement.
  - The two files are evaluated **independently**; a path is excluded if *either* file's own ruleset matches it (each file's negations only resolve within that same file — a negation in one file cannot reach across and un-ignore something the other file excludes).
  - **Root-only, no nested/per-directory ignore files** — a documented, deliberate limitation (the roadmap doesn't call for git's full nested-ignore-file behavior, and it's materially more scope than "respect the repo's ignore rules").
  - Absent ignore file → empty ruleset, no error.
  - Present-but-unreadable or present-but-unparseable ignore file → that file's ruleset is treated as empty, a warning is logged (file name + error), indexing proceeds rather than aborting.
  - `.gitignore`/`.codepulseignore` themselves are never indexed — not because of new special-case logic, but because no `LanguageParser` recognizes them (a pre-existing fact of `parser/factory.py`'s extension dispatch), now explicitly asserted by a test.
- **Discovery integration point:** `IndexingEngine.index_directory()`, step 1 (`codepulse/indexer/engine.py`). `RepositoryIgnoreRules` is constructed **once** per call (not stored on `self` — no shared/global state between repositories) and layered on top of the pre-existing hardcoded `IGNORE_DIRS`/`IGNORE_EXTENSIONS` filtering (both unchanged, still checked first as a cheap short-circuit). Ignored directories are **pruned** from `os.walk`'s `dirs` list in place (matching the pre-existing pruning mechanism for `IGNORE_DIRS`), so nothing beneath an ignored directory is walked, read, or parsed — not merely filtered out afterward. Ignore filtering happens entirely within step 1, before step 2 (parsing) even begins, so an ignored file is never opened, read, parsed, chunked, embedded, or persisted.
- **Ignored files create zero downstream artifacts** for that indexing run: no `FileModel` row, no `SymbolModel` row, no `EmbeddingModel` row, no BM25/vector-index entry, no call-graph entry — verified directly (DB queries + in-memory index inspection + a real search-miss check), not inferred.
- **Re-indexing / stale-data limitation (deliberately NOT implemented):** if a file was indexed in a *previous* run and later becomes newly ignored (a new `.gitignore` line added, etc.), its existing `FileModel`/`SymbolModel`/`EmbeddingModel` rows from the prior run are **NOT deleted or cleaned up** by this milestone. Phase 0.8 only affects *discovery for new indexing operations* — it does not implement stale-data cleanup for previously-indexed-now-ignored files. This matches the roadmap's own framing (`ON DELETE CASCADE`-based stale-data cleanup is explicitly Phase 3 territory) and this session's explicit instruction not to invent that system now. **This is a real, user-visible limitation**: re-indexing a repository after adding a new ignore rule will not retroactively remove already-indexed rows for now-ignored files.
- Paths: ignore-matching uses the exact same `os.path.relpath(path, repo_path).replace("\\", "/")` convention already established elsewhere in the pipeline for `FileParseResult.file_path`/`Symbol.file_path`/DB storage — no new path-normalization scheme was introduced, and no existing stored path format changed.
- New runtime dependency: `pathspec>=1.1.0,<1.2.0` (added to `pyproject.toml`) — a small, standard, pure-Python library for gitignore-pattern matching (used by many major Python tools). Chosen over hand-rolling gitignore semantics (order-dependent negation, `**`, directory-only patterns) because "same syntax" (§16) means *real* gitignore behavior is expected, and a well-tested library is lower-risk than a bespoke matcher for a famously fiddly spec.
- No file-size or file-type caps were implemented (§8 mentions them alongside ignore-file handling, but they're a distinct, not-yet-built concern, out of this milestone's scope).

## Database State

- `codepulse.db` exists at the repository root, untracked by git.
- **Current Alembic revision (real `codepulse.db`): `49572f834366`** — **unchanged by Phase 0.8**. No migration was created or needed: ignore-rule handling is a discovery-time filter, not a schema concern.
- Row counts (unchanged since Phase 0.3, re-verified this session): `repositories` = 1, `files` = 18, `symbols` = 128, `dependencies` = 528, `code_embeddings` = 0.
- The real `codepulse.db` was **not modified** during Phase 0.8. All verification used temporary, disposable SQLite databases and temporary repository directories under `tmp_path`; the real DB's revision and row counts were re-checked identical before and after the full test run.

## Test State

- Last full suite result: **86 passed, 0 failed, 13 warnings** (`.venv/Scripts/python.exe -m pytest tests/ -v`), ~157s.
- Focused Phase 0.8 tests: `tests/test_ignore_discovery.py` (12 tests, exercising the real `IndexingEngine.index_directory()`/`.search()` pipeline end-to-end against temp repos/DBs, real embedding model via a module-scoped shared `CodeEmbedder`): no ignore files (baseline); `.gitignore` excludes a file; `.gitignore` excludes a directory and everything nested beneath it; `.codepulseignore` excludes a file and a directory; combined `.gitignore` + `.codepulseignore` (proves additive union, not override); pattern matches a nested (non-root) path; negation pattern re-includes a file standard gitignore syntax requires; an ignored file produces no `FileModel`/`SymbolModel` rows, no BM25/vector-index entry, and no search hit for content unique to it; two repositories indexed by the same engine instance have fully independent ignore rules (no shared/global state); `.gitignore`/`.codepulseignore` themselves are never indexed; a malformed pattern (`[z-a]`, verified directly against `pathspec` to actually raise) does not crash indexing and the rest of the repo still indexes correctly; a `RepositoryIgnoreRules` unit-level sanity check for the no-ignore-files case.
- All 74 pre-existing tests re-run unchanged and pass, confirming no regression to Phase 0.3–0.7 behavior (upsert idempotency, embedding persistence/hydration, no pseudo-embedding fallback, repository-scoped search, fabricated-metric removal).
- No test was weakened, skipped, or deleted.
- Known warnings: `StarletteDeprecationWarning` (httpx/TestClient) and `FutureWarning`s from `tree_sitter` — pre-existing, not addressed. (A `pathspec` `DeprecationWarning` for the `"gitwildmatch"` factory name was hit during development and fixed by switching to the non-deprecated `"gitignore"` factory name before finalizing — not present in the final result.)

## Known Issues

**Roadmap work remaining (Phase 0, not yet implemented):**
- 0.9 Fix CORS policy defaults; trim `pyproject.toml`; add `rich`
- 0.10 Restart-survivability + idempotency test suite (beyond what 0.3-0.8's own focused tests cover)

**Known technical debt:**
- `StorageManager.create_tables()` still unconditionally calls `Base.metadata.create_all()` alongside Alembic.
- `SymbolType.VARIABLE` / `SymbolType.INTERFACE` enum values defined but never produced by either parser.
- Dead/unused `job_id` generation in `api/app.py` — Phase 1 (`IndexJob`) scope.
- Dependency (call graph edge) rows still duplicate on every re-index (Phase 3 scope).
- `languages` breakdown (`get_repository_stats`) and the "repo not found" fallback counts (`get_index_status`) are not repository-scoped (discovered in Phase 0.7's audit, not fabrication, still unfixed).
- `BM25Index`/`CallGraphBuilder` are still in-memory-only with no durable backing.
- `IndexingEngine.search(search_type="semantic"/"hybrid")` still does not catch `EmbeddingUnavailableError` for the query-side embed call.
- No `/health/live` or `/health/ready` endpoint exists yet.
- **New from Phase 0.8: no stale-data cleanup for previously-indexed files that become newly ignored** — see "Re-indexing / stale-data limitation" above. A repository re-indexed after its ignore rules change will retain database rows for files that are now ignored, until a future (Phase 3-adjacent) stale-data-cleanup milestone addresses it.
- **New from Phase 0.8: no nested/per-directory `.gitignore`/`.codepulseignore` support** — only the repository-root files are read. A `.gitignore` in a subdirectory is not consulted (this differs from real git's own behavior, which does support nested ignore files) — a deliberate scope-limiting decision, documented in `codepulse/indexer/ignore.py`.
- **New from Phase 0.8: no file-size or file-type caps** — mentioned alongside ignore-file handling in roadmap §8 but not implemented; a distinct future concern.

**Blockers:** none currently known for starting Phase 0.9.

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
- Default-exclusion of secret-shaped filenames (`.env*`, `*.pem`, `*.key`, `id_rsa*`) with an opt-in override flag, and lockfile/minified-bundle detection beyond the existing `IGNORE_DIRS`/`IGNORE_EXTENSIONS` sets — both mentioned in roadmap §16 alongside `.gitignore`/`.codepulseignore` support, but not part of B6's narrow definition of done ("ignored files never appear in the index") and not implemented in Phase 0.8; flagged here as a plausible future near-term item, not assumed to be required.

## Current Risks

- `_persist_to_db` still has four upsert blocks plus the dependency-insert block in one function/session — unchanged by Phase 0.8.
- The `languages`/fallback-counts repository-isolation gap (Phase 0.7 finding) remains unfixed.
- Only `repositories`, `files`, `symbols`, `code_embeddings` are idempotent; `dependencies` is not (Phase 3 scope).
- Anyone relying on ignore rules to *retroactively* remove already-indexed secrets/noise from the database should be aware Phase 0.8 only prevents *new* indexing of ignored files — it does not purge rows from before the ignore rule existed (see Known Issues).

## Exact Next Action

NEXT ACTION:
Implement Phase 0.9 — Fix CORS policy defaults; trim `pyproject.toml`; add `rich`, according to the authoritative roadmap (`codepulse/config.py`'s `cors_origins`/`cors_allow_credentials` defaults are still permissive `["*"]`/`True`; `pyproject.toml` still declares unused `psycopg[binary]`, `pgvector`, `tantivy`, `redis`; `cli.py` imports `rich`, which is still undeclared as a dependency).

## Context Handoff Notes

Phase 0.8 implementation is complete, verified, and committed (`b27381a`):

- What was done: audited the complete discovery pipeline first (confirmed `os.walk`-based discovery in `IndexingEngine.index_directory` step 1, with pre-existing `IGNORE_DIRS`/`IGNORE_EXTENSIONS` hardcoded filtering and directory-pruning already in place, and confirmed no `.gitignore`/`.codepulseignore` support existed at all); added `codepulse/indexer/ignore.py` (`RepositoryIgnoreRules`, built on the real `pathspec` library); integrated it into step 1's `os.walk` loop (directory pruning + file filtering, layered on top of the existing hardcoded sets); added `pathspec>=1.1.0,<1.2.0` to `pyproject.toml`; added `tests/test_ignore_discovery.py` (12 new tests against the real indexing pipeline).
- What was verified: full suite 86/86 passed (re-confirmed again at commit time); real `codepulse.db` confirmed unchanged (revision `49572f834366`, row counts 1/18/128/528/0) before and after.
- No Alembic migration was created (none needed).
- Committed as `b27381a` (5 files changed: `codepulse/indexer/engine.py`, `pyproject.toml` modified; `codepulse/indexer/ignore.py`, `tests/test_ignore_discovery.py` new; this file updated).
- Exact tests run: `.venv/Scripts/python.exe -m pytest tests/ -v` → 86 passed, 0 failed.
- Exact next step: follow CLAUDE.md §5's roadmap-discipline checklist for Phase 0.9, starting with re-reading roadmap sprint item 0.9 ("Fix CORS policy; trim pyproject.toml; add rich; delete committed codepulse.db; git init" — note git init and codepulse.db-never-committed are already satisfied from earlier phases, so 0.9's remaining real work is CORS defaults + dependency trimming + declaring `rich`).
