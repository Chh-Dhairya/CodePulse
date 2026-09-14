# Pre-Phase 3 Audit

## Baseline

- HEAD: `693dcf8b1c68f963b2211acc4efc16e0818aaf0d` (unchanged throughout this audit)
- Date: 2026-09-14
- Python: 3.11.0, venv `.venv/`
- Key installed versions: `tantivy==0.22.2`, `sqlite-vec==0.1.9`, `sqlalchemy==2.0.52`, `alembic==1.19.1`, `fastapi==0.141.1`, `sentence-transformers==6.0.0`, `torch==2.13.0+cpu`, `tree-sitter==0.21.3`
- Git status at start: clean. Git status at end: only `docs/PRE_PHASE_3_AUDIT.md` added.
- Branch: `master`

## Test Results

- Full suite: **303 passed, 1 skipped, 0 failed, 0 errors**, 13 warnings, ~313s (`.venv/Scripts/python.exe -m pytest -q`).
- The 1 skip is the pre-existing, environment-driven `test_symlink_escaping_the_allowed_root_is_rejected` (Windows lacks `SeCreateSymbolicLinkPrivilege`) — classification **E (expected/known limitation)**, unchanged since Phase 1.5.
- Warnings are all pre-existing and already documented (`StarletteDeprecationWarning` from httpx/TestClient; `FutureWarning` from `tree_sitter`'s `Language(path, name)` constructor) — no new warnings introduced.
- No failures to classify.

## Findings

| ID | Severity | Area | Finding | Impact | Action |
|----|----------|------|---------|--------|--------|
| F1 | P3 | Documentation (`CODEPULSE_PROGRESS.md`) | Three locations (Completed Milestones table row for Phase 2.5; "Exact Next Action"; "Context Handoff Notes" intro) still say Phase 2.5 is "uncommitted"/"not yet committed as of this document update" — stale, since it was committed as `693dcf8` in the very next action after that text was written. | Misleading to a reader trusting the doc literally; no functional impact. | DOCUMENTATION ONLY, safe to defer or fix in next doc touch. |
| F2 | P2 | Documentation (`README.md`) | The entire "Architecture Highlights" section (lines 5-9) describes a pre-roadmap prototype architecture that was never updated: "PostgreSQL `pgvector` (HNSW)" (actual: SQLite + `sqlite-vec`, exact not HNSW), "Redis Streams for distributed workers" (actual: none, explicitly deferred), "Docker & Docker Compose" as a stated prerequisite (actual: zero-infra local-first, per §25), and "Incremental Indexing: Git diff-based..." presented as a current feature (actual: Phase 3, not started). CLAUDE.md §1 designates README as secondary/roadmap-overridden, so this is not a roadmap-compliance violation, but it directly contradicts the codebase a new reader would actually find. | A new contributor or user reading README first would form a materially wrong picture of the architecture before ever reading the roadmap. | SHOULD FIX BEFORE OR EARLY IN PHASE 3 (doc-only, no code risk). |
| F3 | P2 | `codepulse/search/vector.py` (Phase 2.2, known/accepted) | `VectorIndex.add`/`add_batch` remain pure-append with no doc_id/repository dedup — confirmed still true and still regression-tested (`test_known_limitation_repeated_add_of_the_same_doc_id_accumulates_duplicates`, passing). Re-indexing an already-indexed repository accumulates unbounded duplicate vector entries. | Unbounded memory growth + degraded semantic-search precision (duplicate near-identical candidates) on repeated re-indexing. Not a Phase-3 blocker by itself, but **Phase 3's own objective is more-frequent (incremental) re-indexing**, which would exercise this gap far more often than today's occasional full re-index. | SHOULD FIX BEFORE OR EARLY IN PHASE 3. Already known/disclosed (Phase 2.2); re-flagged here specifically because Phase 3 changes its risk profile. |
| F4 | P2 | `codepulse/search/vector.py` / `codepulse/rate_limit.py` interaction (new observation) | F3's unbounded `doc_ids` growth directly feeds the Phase 2.5 `k<=4096` clamp's own bound (`min(top_k, len(self.doc_ids), 4096)`) — as duplicate accumulation inflates `len(self.doc_ids)` beyond a repo's real symbol count, the 4096 cap is reached (and the fix's own disclosed "may under-return for the target repo in a very large multi-repo deployment" edge case becomes reachable) at a *smaller real repository size* than it otherwise would. Two independently-disclosed limitations compound. | Same class of risk as F3, arriving sooner than F3's own disclosure implies in isolation. | SAFE TO DEFER alongside F3 (not a new independent defect, a compounding observation). |
| F5 | P2 | `codepulse/rate_limit.py` | `TokenBucketLimiter._buckets` is a plain `dict[str, _Bucket]`, one entry per distinct identity (API key or client IP) seen, with **no eviction** except the test-only `reset()`. In a long-running server/team deployment with many distinct callers over time, this dict grows unbounded for the life of the process. Not previously disclosed anywhere in `CODEPULSE_PROGRESS.md`. | Slow, real memory growth in a long-lived server-mode process; no impact for the local-first, single-developer default. | SAFE TO DEFER (server/team mode is not yet a real deployment target; revisit if/when Phase 7 server hardening is scoped). |
| F6 | P3 | Test coverage | `codepulse/cli.py` has **zero test coverage** — no `tests/test_cli*.py` file exists anywhere in the project, at any phase. This means the already-disclosed CLI/API `search_type` mismatch (CLI's `--type` choices exclude `"rrf"`, API accepts it) has no test asserting either side, and any future CLI regression (including in Phase 3, if CLI commands are touched) would go undetected by the suite. | A real, user-facing surface (§22 CLI Strategy) with no regression protection at all. | SAFE TO DEFER, but worth flagging as a real gap — not a Phase 3 blocker since Phase 3's own literal scope (git-diff detection, confidence scoring) doesn't obviously require touching the CLI. |
| F7 | INFO | Real `codepulse.db` data | All 18 `files` rows have `content_hash IS NULL`. Verified this is **not a code defect**: `IndexingEngine._persist_to_db` does write `content_hash` on every new/updated file row (confirmed by direct code inspection, line 516), and Phase 0.3's own test suite asserts a real, non-empty hash is set on fresh indexing. The real DB's single indexed repository predates Phase 0.3 and has never been re-indexed since — this is stale legacy data, not evidence of a bug in current code. | None — cosmetic staleness of a demo/dev-only real database, already known to be a long-untouched fixture. | NO ACTION REQUIRED. |
| F8 | INFO | Dependency/packaging | Re-confirmed (read-only) the already-documented `pip install -e .` failure ("Multiple top-level packages discovered in a flat-layout: ['alembic', 'codepulse']"). No new dependency drift found: `tantivy` 0.22.2 and `sqlite-vec` 0.1.9 both satisfy their declared `pyproject.toml` constraints; `structlog`/`prometheus-client`/`httpx` remain genuinely unused/transitive-only, matching the documented Phase 7 deferral. | None beyond what's already known. | NO ACTION REQUIRED (already tracked). |

## Confirmed Healthy Areas

- **Static health**: every file under `codepulse/` and `tests/` compiles cleanly (`py_compile`/`compileall`); every `codepulse` submodule imports without error (no circular imports, no broken references); AST-based scan found zero duplicate top-level/method definitions and zero obviously-unreachable code after `return`/`raise`; zero `TODO`/`FIXME`/`HACK`/`XXX` markers anywhere in `codepulse/`.
- **Database/persistence**: real DB's Alembic revision (`de22df2fc3ce`) exactly matches the script directory's own head (6 linear migration files, no branching, no drift); zero orphaned foreign keys across `files→repositories`, `symbols→files`, `code_embeddings→symbols`, `index_jobs→repositories`, `api_key_repositories→api_keys/repositories`; zero duplicate natural keys in `files`/`symbols`; `dependencies.source_file` values all resolve to real `files.path` rows.
- **Search/index consistency**: `BM25Index.remove_repo` (Phase 2.1 idempotency fix) confirmed still wired into `IndexingEngine.index_directory()`; `VectorIndex`'s module docstring still accurately, explicitly states sqlite-vec is exact search, not ANN/HNSW — no code or doc anywhere claims otherwise; the Phase 2.5 `k<=4096` fix confirmed present and regression-tested.
- **Security/isolation**: exactly 13 API routes, exactly 13 `Depends(require_api_key)` occurrences — every endpoint still requires authentication, no regression. Full Phase 1.2/1.5/1.7 auth/ingestion-safety/security-suite tests all pass as part of the 303.
- **Architecture placement**: no MCP module exists yet, consistent with its documented Phase 5 deferral (nothing to check for inconsistency).

## Known Accepted Deviations

- **`sqlite-vec` provides exact (brute-force) vector search, not approximate/ANN/HNSW** — a formally accepted, explicit, documented roadmap deviation from Phase 2.2 (reconciliation gate, user-confirmed). Re-verified this audit: `codepulse/search/vector.py`'s own module docstring still states this accurately and prominently; no code or doc claims ANN/HNSW behavior.
- **`VectorIndex` repository re-indexing accumulates duplicate entries** (no dedup) — disclosed Phase 2.2 limitation, deliberately not fixed at the time. Still true (see F3 above, re-flagged for its changed relevance under Phase 3).
- **`codepulse/cli.py`'s `--type` argument does not accept `"rrf"`** — disclosed Phase 2.3 scope boundary (§37 item 2.3 named the API specifically).
- **No CI system exists in this repository** — disclosed Phase 2.4/2.5 finding; the relevance and benchmark harnesses run only via manual `pytest`/script invocation.
- **The Phase 2.5 "large" benchmark tier used 25,000 files, not the roadmap's literal ~100K+** — disclosed, throughput-calibrated scope decision.
- **Every measured Phase 2.5 search p95 latency exceeds its §19 budget** — disclosed, not optimized (out of that milestone's scope).

## Phase 3 Blockers

No Phase 3 blockers identified by this audit.

(F3/F4's compounding scalability risk is flagged as **should-fix-before-or-early-in** Phase 3, not a blocker to *starting* Phase 3 — Phase 3's own implementation does not require `VectorIndex` deduplication to exist first, but Phase 3's stated objective of more frequent incremental re-indexing will make this gap materially more consequential once live.)

## Deferred Issues

- F3/F4 — `VectorIndex` duplicate accumulation and its interaction with the Phase 2.5 `k<=4096` clamp.
- F5 — unbounded `TokenBucketLimiter._buckets` growth in a long-lived server-mode process.
- F6 — zero CLI test coverage.
- F2 — stale `README.md` "Architecture Highlights."
- F1 — three stale "uncommitted" phrases in `CODEPULSE_PROGRESS.md`.
- All previously-documented Known Issues/Current Risks in `CODEPULSE_PROGRESS.md` not restated above (re-verified still accurate; no new contradictions found).

## Audit Conclusion

The repository is internally consistent, fully test-covered at 303/303 passing, free of syntax/import/static defects, relationally coherent in its real database, and accurately self-documents its one significant architectural deviation (`sqlite-vec` exact search). All findings are P2/P3/INFO — technical debt, scalability risk, or stale documentation — with none rising to a correctness, security, or data-integrity defect, and none blocking Phase 3's own literal scope from beginning. **READY FOR PHASE 3**, with F2-F6 recommended as near-term (not blocking) follow-ups given Phase 3's own re-indexing-frequency objective makes F3/F4 more consequential than before.
