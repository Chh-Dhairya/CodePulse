# CodePulse — Future Changes

A running list of improvements identified during QA testing (see `CODEPULSE_QA_CHECKLIST` — the artifact, not a file in this repo) that are **deliberately deferred**, not forgotten. Per CLAUDE.md's own change-scope discipline, none of these get implemented opportunistically mid-testing — they're recorded here and picked up as their own, separate milestones once the QA checklist itself is done.

Not a roadmap sprint item. Nothing here is authoritative the way `CodePulse_Strategy_and_Roadmap.md` is — this is a holding pen for real, found issues, not a plan.

---

## 1. Search relevance scores are per-chunk, not per-symbol

**Found**: 2026-09-26, while manually testing `search` via the QA checklist (CLI-q-1 vs. CLI-q-2).

**The problem**: `codepulse/search/ranker.py`'s `fuse()` computes `relevance_score` per candidate `doc_id` — and a single symbol is split into multiple chunks (signature, docstring, body) at index time. BM25 (keyword) and the vector index each independently pick their own best-matching chunk for a symbol, and those are frequently *different* chunk rows. The fusion formula (`0.40×KW + 0.40×Vec + 0.20×Graph`) then never gets to combine a high keyword score and a high vector score into one number for that symbol, because no single row has both.

**Concretely observed**: querying `"distance between points"` against `Test_Repo` surfaced `calculate_distance` three times — `KW=1.00/Vec=0.00` (→ `0.44`), `KW=0.00/Vec=0.84` (→ `0.34`), `KW=0.00/Vec=0.83` (→ `0.33`) — three separate rows, each scored on only one axis, instead of one row reflecting that the symbol actually matched on *both*. Compared against a genuinely irrelevant query (`"xyzzy quantum toaster nonsense"`, top hit `0.24`, pure vector noise with `KW=0.00, Graph=0.00`), the gap between "this is a great match" (0.44) and "this is noise" (0.24) reads far smaller than it should, because the good match's true combined signal was never actually computed as one score.

**Verified formula** (`ranker.py`'s `fuse()`): `combined = weight_keyword×KW + weight_vector×Vec + weight_graph×Graph` (+ an exact-name-match boost when the query literally names the symbol), defaults `0.40 / 0.40 / 0.20`. Confirmed by hand-computing both cases above against the real weights and getting exact matches.

**Not a bug** — chunking finely for embeddings is a reasonable, deliberate design choice. But it means a general user reading the CLI/VS Code's `relevance_score` column has no way to tell "this scored low because it's genuinely irrelevant" apart from "this scored low because its keyword-match and semantic-match landed on different chunks" — both look like an unremarkable ~0.2–0.4 number.

### Options considered (none implemented yet)

1. **Symbol-level result aggregation (recommended starting point)** — group chunk-level candidates by `(symbol_name, file_path)` *after* fusion, take the max KW/Vec/Graph across a symbol's own chunks, recompute one combined score from those maxes. `calculate_distance` would show as one row at `~0.74` instead of three rows at `0.44/0.34/0.33`. Should live at the **display layer** (CLI output formatting, VS Code's search quick-pick) — not inside `IndexingEngine.search()`/`ranker.fuse()` itself, since MCP tool callers (agents) may genuinely want the finer per-chunk granularity `search_code` currently returns. Needs a decision on which chunk's snippet represents the merged row, and real verification against `tests/relevance/dataset.json` that this doesn't distort anything currently depended on.
2. **Pure presentation fix, no scoring change** — collapse same-symbol rows in the CLI/VS Code UI without changing the underlying numbers; show "3 matching chunks — keyword ✓, semantic ✓" instead of 3 separate scores. Lower risk than #1 (touches only rendering, not `ranker.py`), but doesn't fix the actual "0.44 looks weak" perception problem, just makes the multi-chunk pattern visible.
3. **Align BM25 and vector chunk boundaries** — the deeper fix, so keyword and semantic search are more likely to land on the *same* chunk in the first place. Real risk to indexing internals; would need `tests/relevance/dataset.json` re-validation before trusting it, and is a bigger unit of work than #1/#2.

**Recommendation when this gets picked up**: start with #1, scoped to the CLI/VS Code display layer only, `search()`'s own return contract to MCP/REST callers left untouched.

---

## 2. Repository path matching doesn't normalize backslash vs. forward-slash

**Found**: 2026-09-26, while building the QA checklist's REST API test cases (rest-6).

Already flagged as its own tracked follow-up task (`task_de0daaea`, spawned mid-session) rather than duplicated in full here — see that task's own description for the full repro and fix sketch. Summary: `POST /v1/repositories/index`'s "is this repository already known" check (`RepositoryModel.url == req.url`, in `codepulse/api/app.py`) is a plain string comparison, so indexing the same directory with backslashes vs. forward slashes creates two separate repository rows instead of recognizing them as the same repo — and, as a side effect, lets a viewer-role key bypass the "re-indexing an existing repo requires indexer role" check, since the differently-spelled path looks like a brand-new repository to that check.

---

## Items 3-9: found by the 2026-10-02 resumption audit

Full evidence, root causes and verification are in `CODEPULSE_PROGRESS.md` ("Audit 2026-10-02"). Each was reproduced live; none is fixed. Listed in suggested priority order, not finding-letter order.

### 3. No `codepulse` executable (audit finding E) — Medium
`pyproject.toml` has no `[project.scripts]`, so only `python -m codepulse.cli` works. The VS Code extension's default `codepulse.serverCommand` is `codepulse`, which therefore fails out of the box (the manual had to tell users to change it). **Fix sketch**: add `[project.scripts] codepulse = "codepulse.cli:main"`, `pip install -e .`, verify `codepulse --help` and the extension's default launch. Smallest change with the largest "works first time" payoff.

### 4. Nonexistent local paths are accepted and create phantom repositories (finding A) — Medium
`python -m codepulse.cli callers /no/such/path foo` exits 0 with "0 found", and `POST /v1/repositories/index` with a bogus path returns `202` and a job that "succeeds" with 0 files — both leave an empty repository row. The real `codepulse.db` contained one from the manual's literal placeholder command (removed 2026-10-02 at the user's instruction; the bug itself is still open). **Fix sketch**: in `ingestion_safety.validate_local_path` (and the CLI's path handling) require `Path(...).is_dir()`; REST → `400` with a clear message, CLI → error + non-zero exit. Add tests for both. Interacts with item 2 (path normalization): do them together.

### 5. README is inaccurate and has no usage section (finding B) — Medium (docs)
Claims `pgvector`, Redis Streams and Docker prerequisites; none exist (actual stack: SQLite + sqlite-vec + Tantivy, in-process, no Docker). No instructions for the CLI, `serve`, or bootstrapping the first API key (which currently requires a Python snippet because the REST key endpoint needs an existing admin key). **Fix sketch**: rewrite to the real architecture; add a short "first run" section; consider a `codepulse keys create` CLI command so bootstrapping isn't a Python one-liner.

### 6. Routing-level 404/405 skip the structured error envelope (finding F) — Low
Unknown route → `{"detail":"Not Found"}`, wrong method → `{"detail":"Method Not Allowed"}`, while every application-raised error uses the §21 `{"error":{...}}` envelope. Cause: `api/errors.py:129` registers its handler on `fastapi.HTTPException`, but Starlette's router raises the base `starlette.exceptions.HTTPException` (fastapi's is a subclass, so the handler never matches). **Fix sketch**: register the handler on `starlette.exceptions.HTTPException`; add a test for an unmatched route and a wrong method.

### 7. Default rate limit vs. the VS Code extension (finding D) — Low-Medium
5 req/s sustained, burst 20, per API key. Measured with the extension's real compiled client: 12 cursor settles in 5.5 s (48 requests) → 3 got `429` (6%); the extension has no retry, and `listRepositories()` (uncached) is ~25% of its traffic. **Fix sketch** (extension repo): cache the workspace→repository resolution for ~30 s and retry once on `429` honoring `retry_after_seconds`. Optionally revisit the server default for loopback/local-first use (it is already tunable via `CODEPULSE_RATE_LIMIT_*`).

### 8. Fresh databases are never Alembic-stamped (finding C) — Low-Medium, latent
Any DB created by the first CLI/`serve` run goes through `create_all()` with no `alembic_version` table, and nothing stamps it; `migrations.stamp_database()` exists but is never called, and the CLI has no migrate/stamp command. Harmless until the next schema migration ships — then every user DB needs a manual stamp first. **Fix sketch**: stamp `head` right after `create_all()` on a brand-new DB, and/or add `codepulse migrate`.

### 9. SSRF check rejects NAT64 addresses (finding G) — Low, latent
`validate_git_url` treats `64:ff9b::/96` (DNS64-synthesized) as reserved, so on an IPv6-only/DNS64 network every public git host (github.com → `64:ff9b::14cf:4952`) is refused as an SSRF risk. No current impact (git ingestion returns 501 anyway). **Fix sketch** when git cloning is implemented: extract the embedded IPv4 from NAT64 addresses and validate that instead.

### Recorded, not necessarily defects
- **403 vs 404 existence oracle (finding H)**: a key not scoped to a repository gets `403` for an existing symbol id and `404` for a nonexistent one; ids are sequential. A common tradeoff; decide deliberately whether to return `404` for both.
- **Process lesson (finding I)**: cleaning up with raw SQL on a connection without `PRAGMA foreign_keys=ON` leaves orphaned child rows, and SQLite id reuse can then attach them to an unrelated new repository. Always delete through `StorageManager` (FK-enforcing). Orphans from two such cleanups were found and repaired during the audit.
- **Housekeeping (done 2026-10-02)**: the real `codepulse.db` held two rows from QA (repo 2 `Test_Repo`, repo 3 phantom `C:\path\to\your\repo`). Both were removed on 2026-10-02 at the user's explicit instruction (CLAUDE.md §7): backup first, cascade delete via `StorageManager`, repo 1 verified identical.
