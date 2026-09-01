# CodePulse: Industry-Grade Product & Implementation Strategy

*Principal architecture & product plan. No code has been written or modified. This document is the single source of truth for what to build, in what order, and why.*

---

## 1. Executive Assessment

CodePulse today is a well-engineered **single-process prototype**: solid tree-sitter parsing, a real (if unscaled) hybrid-retrieval algorithm, and a 12-endpoint API skeleton. It is **not** the distributed, multi-tenant, production system its own documentation describes. The prior audit correctly identified the core blockers — global mutable state, non-durable embeddings, non-idempotent indexing, fabricated metrics, no auth. This document accepts those findings, verifies them independently against the code, adds several the audit under-emphasized, and — critically — reframes the problem from "finish the original spec" to "build the *right* product," which is narrower and more local-first than the original `Implementation.md`.

The central strategic call this document makes: **CodePulse should ship as a local-first, embedded-by-default tool that also scales to a team server**, not a PostgreSQL/Redis/Tantivy/Kubernetes system from day one. Every architecture decision below is filtered through that lens, and through treating AI agents as a first-class API consumer alongside humans.

---

## 2. Product Vision

**Category:** Local-first, agent-native **code intelligence engine** — not a search box, not a SaaS code-search product, not an IDE.

**Problem it solves:** Understanding unfamiliar or large codebases is slow because existing tools only offer one of three things: text matching (grep/ripgrep — fast, zero understanding), IDE symbol tools (accurate but single-language, single-repo, no semantics), or cloud code search (semantic-ish, but centralized, privacy-costly, and not graph-aware). Nobody combines lexical + semantic + structural (call-graph) retrieval in one self-hosted, low-latency engine that both a human and an AI agent can query the same way.

**Why semantic search alone is insufficient:** embeddings answer "what looks similar" but not "what calls what," "what breaks if I change this," or "give me the exact three symbols relevant to this task." Real code understanding requires structure (call graph, imports) as much as meaning.

**Differentiation:**
- **vs. grep/ripgrep:** understands symbols, structure, and intent, not just text.
- **vs. GitHub code search:** self-hosted, private, works offline, understands call graphs and impact — GitHub's is lexical-only and doesn't reason about your specific repo's dependency structure.
- **vs. IDE symbol search:** cross-language, cross-file, exposed as an API any tool (including agents/CI) can call — not locked inside one editor.
- **vs. generic RAG-over-code:** structured, deterministic retrieval (symbols, edges, graphs) as the source of truth, with embeddings as one signal among several rather than the only signal.

**Core moat (see §28 for ranking):** the combination of (1) local-first privacy/simplicity and (2) structured, graph-aware, token-efficient retrieval that is *equally good for humans and agents*. Neither of these alone is defensible; together they are.

**What should NOT be built:** a generic developer portal, a Copilot-style code generator, a full APM/observability product, a general project-management tool, or a distributed system before there is a single user who needs one.

**Core loop (revised from "Index → Understand → Retrieve → Navigate → Analyze → Act"):**

```
Index → Retrieve → Explain (structure) → Navigate → Decide (impact) → Act
```

The change from the original wording: "Understand" is renamed "Explain (structure)" because CodePulse's job is to expose *structure* (call graph, imports, symbol relationships) as understandable output — not to generate a natural-language understanding via an LLM. That distinction is the whole point of Rule 7 (§47).

---

## 3. Current-State Verification (Audit Cross-Check)

Independently re-verified against the repository. Status key: ✅ confirmed accurate, 🔧 needs refinement, ➕ audit missed this.

| Audit Finding | Verification |
|---|---|
| Global mutable `IndexingEngine` state, no repo scoping | ✅ Confirmed. `BM25Index`, `VectorIndex`, `CallGraphBuilder` are single unscoped instances on one `IndexingEngine` object; `search()` never filters by `repository_id`. |
| Embeddings not durable | ✅ Confirmed. `EmbeddingModel` table defined and imported in `engine.py` but never instantiated/saved in `_persist_to_db`. |
| Indexing not idempotent | ✅ Confirmed. `_persist_to_db` always does `session.add(RepositoryModel(...))` — no upsert-by-path key anywhere. |
| Fake job IDs | ✅ Confirmed. `job_id = f"job_{int(time.time())}"` is generated, returned, and never stored or looked up by any other endpoint. |
| Fabricated metrics | ✅ Confirmed exactly as described: `index_size_mb: 12/15`, `total_queries: 100`, `avg_search_latency_ms: 45` are literal constants in `app.py`. |
| No auth | ✅ Confirmed. Zero auth middleware/dependency anywhere in `api/app.py`. |
| No rate limiting | ✅ Confirmed. |
| BM25/vector search don't scale | 🔧 Refinement: this is *correct* but the audit undersold how good the underlying math is — `BM25Index` and `HybridRanker` implement textbook-correct BM25 and RRF/weighted fusion. The problem is purely the **data structure** (linear scan vs. postings list / ANN), not the algorithm. This matters for planning: we can keep the ranking logic and only replace the storage/lookup layer. |
| Git incremental indexing missing | ✅ Confirmed, and worse than stated: there is **no `.git` directory in the delivered project at all**, so there is currently no commit concept to diff against, independent of code. |
| No observability | ✅ Confirmed; `structlog`/`prometheus-client` are declared dependencies but never imported anywhere. |
| No deployment infra | ✅ Confirmed — no Dockerfile, no `docker-compose.yml`, no CI config exist in the delivered tree, despite being fully specified (with YAML) in `Implementation.md`. |
| Documentation mismatch | ✅ Confirmed — `README.md`'s "Architecture Highlights" describes Tantivy/pgvector as current, not planned. |

### C. Problems the Audit Under-Emphasized or Missed

1. **Dependency footprint mismatch is worse than "unused deps."** `pyproject.toml` declares `psycopg[binary]`, `pgvector`, `tantivy`, `redis`, `alembic`, `prometheus-client`, `structlog`, `torch` — none imported in code except `torch` (transitively via `sentence-transformers`). A fresh `pip install -e ".[dev]"` will attempt multi-GB downloads and compiled-wheel builds (`psycopg[binary]`, `tantivy`) for zero present functionality. This is a **first-run developer-experience failure**, not just tech debt — exactly the opposite of the "simple local install" the vision requires.
2. **`rich` is used in `cli.py` but not declared** in `pyproject.toml` at all. The CLI currently only works because `rich` happens to be present transitively. This will break on a clean environment and was not flagged.
3. **CORS misconfiguration** (`allow_origins=["*"]` + `allow_credentials=True`) is a real (if currently low-severity) security smell the audit noted but under-weighted — it signals no actual CORS policy was designed.
4. **No config system at all.** DB URL, embedding model name, host/port are all hardcoded defaults with no env var / config file override anywhere in the code. This blocks *any* deployment mode beyond "run exactly as checked out" and must be Phase 0, not later — the audit listed this only implicitly.
5. **Call graph resolution is name-only and cross-class-ambiguous.** `CallGraphBuilder` matches callees by bare identifier name against local symbols / import map. Two unrelated classes both defining `save()` will conflate their call edges. This isn't a bug to "fix" cheaply — it's a fundamental limitation of AST-only (no type/scope resolution) call graphs that must be **surfaced as a confidence score**, not silently presented as ground truth. The audit didn't propose this framing.
6. **The shipped `codepulse.db` contains real indexed data (128 symbols, 528 dependency edges) despite `.gitignore` excluding `*.db`,** and there is no `.git` directory at all — meaning version control was never actually initialized for this project. This is more than packaging hygiene: it means "clone and run" is currently untested as a concept, and the git-based incremental-indexing feature has literally nothing to diff against yet.
7. **No secrets/PII scrubbing exists, and the ingestion endpoint accepts arbitrary local paths.** The audit flagged the auth gap but underplayed the compounding risk: indexing stores full source bodies (`Symbol.body`) verbatim in SQLite with no scrubbing, no ignore-file support (`.gitignore` is not respected during file discovery), and no size/file-count caps. A developer accidentally pointing this at `~/` would index every dotfile, `.env`, and SSH key structure it can parse-adjacent-detect as text.
8. **`SymbolType.VARIABLE` and `SymbolType.INTERFACE` are defined but never produced by either parser** — dead enum values suggesting incomplete original scope (TS interfaces, module-level variables) that should either be implemented or removed from the model.

---

## 4. User / Persona Analysis

### Persona 1 — Individual Developer
- **JTBD:** "I just cloned/inherited this repo and need to be productive today," or "I need to change this function safely."
- **Pain points:** grep gives 200 irrelevant hits; IDE "find usages" only works within one language/project; no single tool shows "what will break."
- **Workflow:** `codepulse index .` once, then dozens of `search`/`impact`/`symbol` calls per day, mostly from a terminal or editor integration.
- **Desired experience:** zero infrastructure, instant setup, fast enough to use *instead of* grep, not alongside it.
- **Key features:** hybrid search, impact analysis, similar-code discovery, symbol navigation — all local, no network dependency beyond the one-time embedding-model download.
- **Failure modes:** if setup requires Docker/Postgres, they will not adopt it; if search is slower than grep for simple cases, they'll stop trusting it.

### Persona 2 — Engineering Team
- **JTBD:** shared, always-fresh index of team repositories that anyone can query without re-indexing locally.
- **Pain points:** onboarding new engineers into large codebases; knowing blast radius before a risky change; keeping multiple people's local indices in sync.
- **Workflow:** a shared CodePulse server, repos indexed via CI hook on push, engineers query via CLI/API/IDE.
- **Desired experience:** "it's just always up to date," simple RBAC (who can index which repos), visibility into indexing health.
- **Key features:** multi-repo isolation, incremental indexing on push, per-repo access control, indexing status dashboard.
- **Failure modes:** stale index without visible warning; one team's repo data leaking into another team's search results (isolation failure is catastrophic for trust).

### Persona 3 — AI Coding Agent (first-class API consumer)
- **Retrieval requirements:** structured, not prose — symbol + relationships + minimal source, not "top 20 fuzzy chunks."
- **Context requirements:** the smallest sufficient bundle (definition + direct callers/callees + related symbols), not whole files.
- **Structured graph requirements:** explicit caller/callee/import edges with confidence scores, so the agent can reason about certainty.
- **Token efficiency requirements:** responses must be sized for LLM context budgets — truncation and relevance ranking matter more here than for a human scanning a results page.
- **Determinism requirements:** the same query against the same index snapshot should return the same result — no randomness in ranking, no hidden LLM calls that vary output.
- **Freshness requirements:** the agent must be able to know the index snapshot's commit/timestamp so it doesn't reason about stale code.
- **Explainability requirements:** every returned edge/result should carry *why* (which score(s) contributed, and the call-graph confidence caveat from §3.5).

### Persona 4 — CI/CD System
- Needs: changed-file → affected-symbol → affected-test mapping for smarter test selection; architecture/dependency-boundary checks (e.g., "no import from `internal/` outside its module"); dead-code detection; search-based regression checks (e.g., "did this PR remove the only caller of X").
- Needs to trigger **incremental** indexing keyed to a commit SHA, not full re-index per PR.

---

## 5. Product-Market / Use-Case Summary

CodePulse's addressable use cases, ranked by near-term value delivered per unit of engineering effort:

1. Individual developer local search/impact-analysis (**highest value, lowest infra cost** — build first).
2. Agent-facing structured retrieval API (**high value, low infra cost** — the intelligence layer needed for #1 is 80% of what agents need too).
3. Small-team shared server (**medium value, medium infra cost** — needs multi-tenancy + auth).
4. CI/CD integration (**medium value, depends on #1–#3 being stable**).
5. IDE extension / Web UI (**presentation layer on top of a stable API — deliberately last**).

---

## 6. Recommended Architecture (Overview)

Four logical layers, deliberately decoupled so the "intelligence" core never depends on how it's being called:

- **Control plane:** auth, API keys, repositories registry, job tracking, permissions, rate limits, configuration.
- **Data plane:** ingestion, parsing, symbol/graph extraction, embeddings, lexical + vector storage, metadata.
- **Intelligence layer:** hybrid retrieval, graph expansion, ranking, context assembly, impact analysis, similarity.
- **Client layer:** CLI, REST API, MCP server, (later) IDE extension, (later) Web UI.

Clients never talk to the data plane directly — only through the Intelligence layer's API, which is also what the Control plane wraps with auth/limits. This means the MCP server and the human REST API are **both thin adapters over one internal library**, not separate implementations (Rule 6, §47).

---

## 7. Storage Decision

Evaluated against the product vision's local-first requirement.

| Option | Operational complexity | Local dev/install | Multi-user scalability | Notes |
|---|---|---|---|---|
| A: Postgres + pgvector + Postgres FTS | Medium | Requires Postgres even solo | Good | Avoids a second search engine; Postgres FTS is weaker than BM25 for code identifiers (poor tokenization control) |
| B: Postgres + pgvector + Tantivy | High | Requires Postgres + Tantivy solo | Good | Best lexical quality, but two storage engines to operate even for one developer — violates local-first goal |
| C: Postgres metadata + dedicated vector DB + dedicated lexical engine | Highest | Worst | Best at large scale | Enterprise-grade but premature; three systems to run/backup/upgrade |
| **D: Embedded/local-first, upgrading to server mode** | **Lowest for individuals; moderate for teams** | **Best** | **Good enough for small-to-mid teams; extensible later** | Recommended |

**Recommendation — Option D, with an explicit two-mode design:**

- **Local mode (default):** SQLite for metadata (already in place), an embedded lexical index (a real inverted-index library — e.g., `tantivy-py` is actually fine to keep *if* it ships as a single wheel with no external server, which it is — Tantivy is embedded, not a service; the audit's complaint was that it's declared-but-unused, not that it's the wrong choice), and a local vector index (e.g., `sqlite-vec` or a persisted HNSW structure via `hnswlib`, avoiding pgvector's Postgres dependency entirely for local mode).
- **Server/team mode:** Postgres (metadata + optionally pgvector for vectors) + the same embedded Tantivy index per repository shard (Tantivy doesn't need to become a network service — it can run inside the server process against a shared disk/volume). Redis is **deferred** (§38) until there's an actual multi-worker deployment that needs a shared queue/cache — a single server process with an in-process queue is sufficient until then.

This gives one indexing/retrieval **codebase** with a pluggable storage backend (interface-based), so "local" and "server" modes are configuration, not a fork.

---

## 8. Indexing Architecture

Lifecycle (adopting the prompt's stages, with persistence made explicit at each step):

```
Repository registered (path or git URL)
        ↓ [persist: Repository row]
Source acquisition (local copy or git clone/fetch)
        ↓ [persist: RepositorySnapshot(commit_sha, acquired_at)]
File discovery (respecting .gitignore + .codepulseignore + size/type caps)
        ↓ [persist: File rows with content_hash]
Change detection (diff content_hash vs. last snapshot)
        ↓ [in-memory: changed/added/deleted file sets]
Parsing (changed files only)
        ↓ [persist: Symbol rows, upserted by (file_id, name, start_line) key]
Dependency/import extraction
        ↓ [persist: Dependency/Import rows]
Call graph construction (incremental: only recompute edges touching changed symbols)
        ↓ [persist: CallEdge rows with confidence score]
Chunking (only for new/changed symbols, by body_hash comparison)
        ↓ [in-memory: CodeChunk list]
Embedding (only chunks whose body_hash changed)
        ↓ [persist: Embedding rows — durable, keyed by chunk content hash]
Lexical indexing (upsert changed docs into embedded index)
        ↓ [persist: lexical index segment on disk]
Vector indexing (upsert changed vectors into local/HNSW index)
        ↓ [persist: vector index segment on disk]
Repository snapshot finalized
        ↓ [persist: RepositorySnapshot.status = ready, indexed_commit_sha]
Ready
```

**Idempotency rule (non-negotiable, Phase 0):** every persistence step upserts by a natural key (`(repo_id, file_path)` for files; `(file_id, name, start_line)` for symbols; `(symbol_id, chunk_type, chunk_index)` for embeddings). Re-running indexing on unchanged input must produce zero net writes.

**Concurrency/failure handling:** indexing runs as a job with a state machine `queued → running → succeeded|failed|cancelled`, one active job per repository (advisory lock on `repo_id`) to avoid concurrent conflicting writes. A failed job leaves the previous snapshot as the "ready" one — **new writes go to a new snapshot row, and reads always serve the latest `status=ready` snapshot**, so a crash mid-index never serves partial data (see §18 for detail).

---

## 9. Incremental Indexing Design

**File hashing:** every `File` row stores a content hash (SHA-256) computed at discovery time. A file is "changed" iff its hash differs from the last snapshot's recorded hash for that path.

**Commit tracking:** `RepositorySnapshot.commit_sha` records the git commit the snapshot was built from (for git-backed repos) or a synthetic monotonic version for path-only local repos without git. Incremental indexing for a git repo uses `git diff <last_commit>..<current_commit> --name-status` to get the file-level change set directly — cheaper and more precise than hashing every file.

**Rename/move detection:** for git repos, use git's own rename detection (`git diff -M`) to map `(old_path → new_path)` and re-key existing `Symbol`/`Embedding` rows to the new path instead of delete+recreate — this preserves symbol IDs (important for any client caching those IDs, e.g., an agent holding a reference across a session).

**Symbol-level change detection:** within a changed file, compare `body_hash` per extracted symbol against the previous snapshot's symbols (matched by `(name, type)` first, falling back to signature similarity if the name changed — a simple heuristic, not a semantic diff). Unchanged symbols skip re-embedding and re-chunking entirely — this is the single biggest cost saver on incremental runs.

**Call graph updates:** rather than rebuilding the whole graph, recompute only edges whose *source* symbol is in the changed set, plus a local BFS one hop out to catch edges whose *target* changed (a symbol being renamed/deleted must have inbound edges from unrelated files corrected too).

**Stale data cleanup:** deleted files/symbols get their `Symbol`/`Embedding`/`CallEdge` rows deleted via `ON DELETE CASCADE` (already present in the schema for `Symbol → Embedding`); lexical/vector index entries are deleted via their `doc_id` before the new snapshot is marked ready.

**Consistency/rollback:** the whole "changed files → new snapshot" unit executes inside a single DB transaction plus a two-phase commit convention for the on-disk lexical/vector segments (write new segment, flip a pointer/manifest file, delete old segment) — so a crash between DB commit and index-segment flip is recoverable by re-running the last incomplete job, which is idempotent by construction.

**When a full re-index is still required:** if no prior successful snapshot exists, if the embedding model version changed (embedding dimension/model must be recorded per snapshot), or if more than some threshold (e.g., 50%) of files changed at once (rebase, mass reformat) — in which case a full re-index is actually cheaper than incremental bookkeeping.

---

## 10. Search / Retrieval Architecture

```
Query
 ↓ normalization (lowercase for BM25 path, raw for embeddings, identifier-aware tokenization)
 ↓ parallel candidate generation:
     - Lexical (BM25 over embedded inverted index) — repo-scoped
     - Semantic (vector similarity over embedded/HNSW index) — repo-scoped
     - Exact symbol match (direct name lookup) — repo-scoped
 ↓ graph-aware expansion (optional): pull in 1-hop callers/callees of top lexical/semantic hits
 ↓ metadata filtering (language, path prefix, symbol type, repo_id — mandatory, not optional)
 ↓ candidate fusion (RRF by default; weighted-linear as a configurable alternative — both already implemented in `HybridRanker`, just need to be reachable via the API's `search_type` and actually wired for the RRF path, which currently exists but is unused)
 ↓ reranking (exact-match boost already exists; defer learned reranking — see §11)
 ↓ context assembly (for agent-facing endpoint only — see §12)
 ↓ Results
```

**Weight configuration:** keep the current `HybridRanker` weights (0.4/0.4/0.2 + exact-match boost) as sane defaults, exposed via config, not hardcoded. **Do not build learned/adaptive ranking yet** — there isn't enough labeled relevance data (the feedback endpoint doesn't even persist data yet; see §32). Revisit only after §32's feedback logging has produced a meaningful dataset (hundreds to low-thousands of judged queries).

---

## 11. Code Intelligence Architecture

- **Symbol intelligence:** definitions and references are already well-supported by the parser; "implementations/overrides/inheritance" require tracking `class` base-list resolution (currently unparsed for Python `class Foo(Base):` — the parser captures `class_definition` but does not currently extract the base-class list into the `Symbol` model). This is a concrete near-term parser enhancement, not a new subsystem.
- **Dependency intelligence:** module-level (`ImportStatement`, already extracted) vs. package-level (would require parsing `pyproject.toml`/`package.json` — new, small, high-value addition for "what packages does this repo depend on").
- **Call graph confidence:** every `CallEdge` should carry a `confidence` field — `high` when resolved through an explicit import mapping to a known file+symbol, `low` when resolved only by bare-name match against same-file symbols, `unresolved` when the callee couldn't be matched to any known symbol at all. This directly addresses the false-positive risk found in §3.5, and it's a **cheap** addition (the resolution logic already distinguishes these cases internally in `CallGraphBuilder._build_graph`; it just needs to be surfaced rather than silently discarded).
- **Impact analysis output:** extend beyond the current caller/callee list to explicitly bucket **directly affected** (depth 1) vs. **transitively affected** (depth 2+) symbols/files, and — once a test-runner integration exists — affected tests (defer to Phase 4+/CI integration, not sooner).
- **Architecture discovery ("explain this repo's architecture"):** should be **deterministic first** — module/dependency-cluster detection via the existing import graph (connected-components / community detection over the import graph is a well-understood graph algorithm, no LLM needed) to identify likely "modules" and "entry points" (files with many inbound, few outbound imports look like APIs/entry points; files with many outbound, few inbound look like utility/leaf modules). An LLM-generated narrative summary *on top of* this deterministic structure is a legitimate later enhancement (§27) — but the graph itself must not depend on one.

---

## 12. Agent-Native Retrieval / Context API

A dedicated `/v1/agent/context` endpoint (or MCP tool — same underlying function, see §13) returning a compact, structured bundle rather than raw search hits:

```json
{
  "symbol": "create_db_connection",
  "file": "db/connection.py",
  "definition": { "signature": "...", "docstring": "...", "body": "... (truncated if large)" },
  "callers": [{ "symbol": "...", "file": "...", "confidence": "high" }],
  "callees": [{ "symbol": "...", "file": "...", "confidence": "high" }],
  "related_symbols": [{ "symbol": "...", "file": "...", "relevance": 0.81, "reason": "semantic_similarity" }],
  "snapshot_commit": "a1b2c3d",
  "truncated": false
}
```

**Token-efficiency rules:** (1) bodies are truncated to signature+docstring by default, full body only on explicit request; (2) `callers`/`callees` are capped (default 10, configurable) and sorted by confidence then call-count; (3) every response includes the `snapshot_commit` so the agent can detect staleness; (4) no field is ever silently omitted without a `truncated`/`omitted_count` flag — determinism and explainability (persona 3 requirements) are treated as hard API contracts, not nice-to-haves.

---

## 13. MCP Strategy

**Recommendation: yes, expose an MCP server — but strictly as an adapter**, not a parallel implementation. Each MCP tool calls the exact same internal Python functions the REST API calls.

Proposed tools: `search_code`, `get_symbol`, `find_callers`, `find_callees`, `impact_analysis`, `find_similar`, `get_context` (the §12 bundle). Each tool's input schema mirrors its REST counterpart's request model; each tool's output is the same JSON contract (agents and humans should never see different data shapes for the same operation).

**Permissions/repository selection:** MCP connections are scoped to one or more repositories at connection-setup time (via the same API-key/permission model as REST — §16), not per-tool-call — this avoids an agent needing to pass credentials on every call and matches how MCP clients typically configure a server once.

**Failure behavior:** tool calls return structured errors (`{"error": "repository_not_indexed", "message": "..."}`) rather than throwing opaque exceptions — agents need machine-parseable failure reasons to decide whether to retry, ask the user, or give up.

---

## 14. Language Roadmap

| Language | Priority | Rationale | Parser maturity (tree-sitter) |
|---|---|---|---|
| Python | Done | Core MVP language | Mature |
| JS/TS | Done | Core MVP language | Mature |
| Go | 1 | High backend-service adoption; simple import model; good tree-sitter grammar | Mature |
| Java | 2 | Enterprise adoption is high, but scope/generics resolution is harder — good ROI once call-graph confidence scoring (§11) exists to be honest about limitations | Mature |
| C# | 3 | Enterprise demand, similar complexity profile to Java | Mature |
| Rust | 4 | Smaller but growing user base; module system is straightforward for import resolution | Mature |
| Kotlin | 5 | Piggybacks on Java/Android demand once Java exists | Mature |
| Ruby | 6 | Dynamic-typing makes call-graph confidence low; lower priority despite historical popularity | Mature |
| PHP | 7 | Large legacy install base but lower priority for target market (modern dev tooling, AI-agent users) | Mature |
| C/C++ | 8 | High implementation difficulty (preprocessor, headers, build-system awareness needed for real import resolution) — defer until there's clear demand | Mature grammar, hard semantics |

**Interface stability requirement:** every new language is an implementation of the existing `LanguageParser` ABC — no changes to the ABC's contract should be needed through at least the Go and Java additions, validating that the current abstraction is genuinely extensible (confirmed by direct code reading, not just documentation claim).

---

## 15. Security Architecture

- **Authentication:** API keys (hashed at rest, scoped to one or more repositories) as the default for both local and server mode — **JWT is not needed** for local/single-user mode and should be introduced only in server/team mode for browser-based Web UI sessions (§21) where short-lived tokens make more sense than long-lived API keys. This directly follows Rule 4 (don't add infra the spec mentions without a concrete need) — the original doc specified JWT unconditionally; this plan scopes it to where it's actually needed.
- **RBAC:** minimal viable model — roles `viewer` (search/read), `indexer` (viewer + trigger indexing), `admin` (all + manage keys/repos). Enforced at the repository-permission level, not globally.
- **Tenant/repo isolation:** every query (search, symbol lookup, graph traversal) is filtered by `repository_id` **and** the caller's permitted repository set — enforced in one central authorization function all endpoints must call, not re-implemented per endpoint (this is exactly the kind of central choke point that prevents the audit's "search ignores repository_id" class of bug from recurring).
- **Repository-ingestion safety (the dangerous endpoint):** for local-path ingestion, restrict to an explicit allow-listed root directory (configurable), reject `..`-traversal and symlink escapes; for Git-URL ingestion, validate the URL scheme (`https`/`ssh` only), resolve and check the target host isn't a private/link-local IP (SSRF prevention), and enforce a repository size/file-count cap before beginning a full clone.
- **Resource limits:** max files per repo, max file size parsed, max concurrent indexing jobs per tenant, request-body size caps.
- **Secrets detection:** see §16.
- **Encryption:** TLS termination at the reverse proxy for server mode (not CodePulse's own responsibility to implement); at-rest encryption is deferred to the deployment environment's disk encryption rather than app-level — reduces complexity per Rule 4.
- **Audit logs:** who indexed what, who queried what repo, when keys were created/revoked — append-only `AuditEvent` table, server mode only.

---

## 16. Secrets & Sensitive Code Handling

- Respect `.gitignore` during file discovery (**currently not done at all** — a concrete near-term fix).
- Support a `.codepulseignore` file (same syntax) for CodePulse-specific exclusions beyond git's own.
- Default-exclude common secret-shaped filenames (`.env*`, `*.pem`, `*.key`, `id_rsa*`) regardless of ignore files, with an explicit opt-in override flag for users who intentionally want them indexed (e.g., auditing their own secrets hygiene).
- Regex + entropy-based scrubbing of detected high-entropy strings (API keys, tokens) **before** persisting `Symbol.body`/embeddings — configurable on/off, on by default.
- Binary/generated-code detection (lockfiles, minified bundles, `dist/`/`build/` output) to avoid indexing noise — extend the existing `IGNORE_DIRS`/`IGNORE_EXTENSIONS` sets in `indexer/engine.py`, which already do a version of this for build artifacts but not for secrets/lockfiles specifically.

---

## 17. Reliability Architecture

- **Idempotency:** guaranteed by the upsert-by-natural-key design in §8/§9 — re-running any job step twice is safe.
- **Transactional boundaries:** one DB transaction per snapshot-finalization step (§8); lexical/vector index writes use the write-new-segment-then-flip-pointer pattern for crash safety.
- **Partial/failed indexing:** jobs have explicit states; a crashed worker leaves a `running` job, detected via a heartbeat timestamp and reclaimed (marked `failed`, eligible for retry) by a lightweight reaper — no distributed lock manager needed at single-server scale.
- **Corrupted/malformed source:** parser failures are caught per-file (already true in `engine.py`'s try/except around each file) and logged, not fatal to the whole job — this behavior is already correct and should be preserved.
- **Embedding model unavailable:** must **fail the job explicitly** rather than silently falling back to hash-based pseudo-embeddings (a direct fix to the audit's AI-quality finding) — if the real model can't load, the job is marked `failed` with a clear error, and lexical-only search remains available in the meantime via a documented "degraded mode."
- **Database/index unavailable:** health checks (§19) surface this before it causes silent query failures; search endpoint falls back to lexical-only if vector index is unreachable, returning a `degraded: true` flag rather than a bare error.
- **Duplicate jobs:** an advisory "one active job per repository" lock (§8) prevents concurrent conflicting indexing runs.

---

## 18. Observability Architecture

**Metrics (Prometheus-style counters/histograms — actually wire the already-declared dependency):** `search_requests_total{search_type}`, `search_latency_seconds` (histogram), `indexing_jobs_total{status}`, `indexing_duration_seconds`, `files_indexed_total`, `symbols_indexed_total`, `embedding_duration_seconds`, `job_queue_depth`, `api_errors_total{endpoint,status_code}`, `search_zero_result_total`.

**Logs:** structured (JSON via `structlog`, also already declared but unused) with consistent fields: `request_id`, `repository_id`, `job_id`, `api_key_id`, `operation`, `duration_ms`, `error_type` — every log line carries enough to reconstruct a request's full path without needing distributed tracing infrastructure at this scale.

**Health endpoints:** `/health/live` (process is up), `/health/ready` (DB reachable, embedding model loaded, index storage writable) — separated because a "ready" failure should stop new traffic/jobs but not restart the process.

---

## 19. Performance Strategy

Reject the original doc's untested `<150ms p95 @ 1M files` claim; replace with a **measure-first** plan.

| Repo size | Target initial index time (budget, not guarantee until measured) | Target incremental index time | Target hybrid search p95 |
|---|---|---|---|
| ~1K files | < 60s | < 5s for a typical single-file commit | < 100ms |
| ~10K files | < 10 min | < 15s | < 200ms |
| ~100K+ files | Hours (background, not blocking) | < 60s for typical commit | < 400ms (may require sharding/pagination of candidate search) |

These are **budgets to validate with the benchmark harness in §26**, not commitments — the document explicitly avoids repeating the previous unvalidated numbers as fact.

---

## 20. Testing Strategy

- **Unit:** parser, tokenizer, BM25, vector, ranker, chunker (mostly already exist and pass — preserve).
- **Integration:** indexer ↔ storage (upsert correctness, idempotency), API ↔ indexer (all 12+ endpoints, not the current 3).
- **Contract tests:** JSON schema validation for every API/MCP response shape — prevents silent breaking changes for agent consumers who parse structured fields.
- **End-to-end:** fixture repo → index → search → assert expected symbols appear.
- **Restart tests:** index → kill process → restart → search must return the same results (directly tests the §8/§9 durability fixes).
- **Failure tests:** kill indexing mid-job → assert prior snapshot still serves reads, job marked `failed`, retry succeeds.
- **Security tests:** unauthenticated request rejected; path traversal in ingestion path rejected; SSRF URL rejected; oversized repo rejected.
- **Performance tests:** run the §19 benchmark harness against fixture repos of each size tier in CI on a schedule (not every PR — too slow).
- **Relevance evaluation:** curate ~100–300 (query, expected-symbol) pairs from CodePulse's own repository as a starting relevance dataset; track Recall@10, MRR as a regression gate on ranking changes.

---

## 21. API Strategy

Stable `/v1/` namespace, versioned explicitly so future breaking changes go to `/v2/`. Every endpoint requires an API key (§15) except `/health/*`. Pagination (`limit`/`offset` or cursor) added to all list-returning endpoints (currently missing entirely). Errors follow a single shape: `{"error": {"code": "...", "message": "...", "details": {...}}}`. **No endpoint may return a metric it did not actually measure** (direct fix for §3's fabricated-values finding — this is now an explicit API design rule, not just a bugfix).

Endpoint groups: `repositories`, `jobs` (new — real job status, replacing the fake job_id), `search`, `symbols`, `references` (new), `graph` (callers/callees/impact), `similar`, `stats` (real only), `health`, `feedback` (real persistence — §32), `agent/context` (§12).

---

## 22. CLI Strategy

```
codepulse init                    # create local config, no infra required
codepulse index <path>            # full index
codepulse update [<path>]         # incremental index (git-diff based when available)
codepulse search "<query>"        # hybrid search, human table output by default, --json for automation
codepulse symbol <name>
codepulse refs <name>
codepulse callers <name>
codepulse callees <name>
codepulse impact <name> [--depth N]
codepulse similar <name>
codepulse status                  # index freshness, job history
codepulse serve                   # start local API/MCP server
codepulse doctor                  # environment/config diagnostics (embedding model loaded? DB reachable? ignore rules active?)
```

Configuration precedence: CLI flags > environment variables > `codepulse.toml` config file > built-in defaults — a real config system (currently entirely absent, per §3.4) is Phase 0 work, not a later nicety, because every other CLI/server feature depends on it existing.

---

## 23. IDE / VS Code Strategy

**Do not build yet.** First, the `/v1/` API and MCP server must be stable (Phase 4–5). Once stable: command-palette search, go-to-symbol, "find references," "impact analysis" panel, "similar code" panel, and a status-bar indexing indicator. Everything else (ranking, graph traversal, context assembly) stays in the backend — the extension is a thin presentation layer, never reimplementing retrieval logic client-side.

---

## 24. Web UI Strategy

**Marginal near-term value — build only after the API is stable and only a minimal MVP:** repository dashboard (indexing status/freshness), a search box with results linking to source, a symbol detail view showing callers/callees, and a dependency-graph visualization (this last one is CodePulse's most visually differentiated feature and worth prioritizing within the Web UI once built). Do not build a generic "developer portal" — every UI element should expose retrieval/graph intelligence the CLI/API already has, nothing new.

---

## 25. Local Deployment Strategy

`pip install codepulse` (or a single binary later) → `codepulse init` → `codepulse index .` → `codepulse serve`. No Docker, no Postgres, no Redis. SQLite + embedded lexical/vector index (§7, Option D local mode) entirely inside a `.codepulse/` directory in the repo or a user-level cache directory. This is the default and primary experience — everything else is additive.

---

## 26. Team / Server Deployment Strategy

Docker Compose for a small team: one `codepulse-server` container (API + indexing worker in-process, single-node — no separate worker deployment until proven necessary), Postgres for metadata (+ optional pgvector), a persistent volume for the embedded lexical/vector index segments. Kubernetes manifests are **explicitly deferred** (§38) — nothing in the near-term roadmap requires horizontal scaling of the indexing workload. Backups: standard Postgres dump + volume snapshot for the index segments (documented restore procedure, not automated yet). Migrations: Alembic (already declared, currently unused — actually wire it in Phase 0/1). Upgrades: version the on-disk index-segment format so a version mismatch triggers a safe rebuild rather than silent corruption.

---

## 27. AI (LLM) Features — Explicit Boundary

**Deterministic (source of truth, no LLM):** search ranking, symbol extraction, call graph, impact analysis, similarity via embeddings (the embedding model is "AI" but produces a deterministic-enough numeric signal fused into an otherwise deterministic pipeline — not a generative step).

**Legitimate future LLM-assisted features (additive, never replacing the above):** natural-language repository/architecture explanation *generated from* the deterministic module-cluster graph (§11); symbol-level plain-English explanation generated from signature+docstring+callers (again, generated *from* structured data, not instead of it); query expansion (turning "auth stuff" into better search terms) — genuinely useful, low-risk, and easy to make optional/toggleable. **Not planned:** LLM-based reranking (adds latency/cost/nondeterminism for a problem RRF/weighted fusion already handles reasonably) and LLM-generated code summaries as the primary search result (violates Persona 3's determinism requirement).

---

## 28. Product Differentiation — Ranked

1. **Graph + semantic + lexical retrieval, unified** (hardest to replicate quickly; already partially built).
2. **Agent-native, token-efficient structured retrieval** (few competitors treat this as first-class rather than an afterthought API wrapper).
3. **Local-first / self-hosted privacy** (real differentiator vs. GitHub/cloud code search, though not unique — several local-first tools exist).
4. Incremental repository intelligence — valuable but an *implementation quality* bar, not a standalone moat.
5. Context-efficient retrieval — overlaps heavily with #2; treated as part of it.
6. Developer workflow integration (IDE/CLI) — necessary but table-stakes, not differentiating on its own.

**Roadmap should reinforce #1 and #2 above all else** — everything in the Phase 0–5 plan below is sequenced to get those two solid before investing in #3's full server/multi-tenant story or #6's IDE/UI polish.

---

## 29. Data Model

Only entities justified by the above sections — several from the original prompt's suggested list are deliberately **not** introduced yet (see rationale column).

| Entity | Introduce now? | Why |
|---|---|---|
| `Repository` | Yes (exists) | Core |
| `RepositorySnapshot` | Yes (new) | Required for incremental indexing (§9) and crash-safe reads (§17) |
| `File` | Yes (exists, needs `content_hash`) | Change detection |
| `Symbol` | Yes (exists, needs upsert key + base-class field) | Core |
| `Dependency`/`Import` | Yes (exists) | Core |
| `CallEdge` | Yes (rename from `DependencyModel`, add `confidence`) | §11 |
| `Embedding` | Yes (exists, must actually be written to) | §3/§9 |
| `IndexJob` | Yes (new) | Real job tracking (§8, replaces fake `job_id`) |
| `APIKey` | Yes (new) | §15 |
| `AuditEvent` | Yes, server mode only (new) | §15 |
| `SearchFeedback` | Yes (new, minimal) | §32 |
| `User`/`Organization` | **Not yet** | No multi-tenant server deployment exists yet to justify it; API-key-per-repository is sufficient until Phase 7 team features are actually being built |
| `RepositoryBranch` | **Not yet** | Single-branch-at-a-time is sufficient until a real team-server use case demands branch comparison |
| `SearchDocument` | **Not yet** | The lexical/vector index segments themselves are the "document store" — no need for a separate relational mirror of that data |
| `IndexEvent` | **Not yet** | `IndexJob` status transitions plus structured logs (§18) cover this without a dedicated table |
| `SearchQuery` (full log) | **Not yet** | `SearchFeedback` (§32) captures what's needed initially; full query logging can be added later if analytics demand it |

---

## 30. Multi-Tenancy

For the timeframe this roadmap covers (through Phase 7), "tenancy" = **repository-scoped API keys**, not a full `Organization → User` hierarchy (deliberately deferred per §29). Authorization boundary: every request resolves to a set of `repository_id`s the caller's key is permitted to touch; every query (search, symbol, graph) filters on that set at the same central authorization checkpoint (§15) — this is the one piece of the whole system that must never have a second code path, because that's exactly how the current `repository_id`-ignoring bug happened.

---

## 31. Relevance & Feedback Loop

Replace the mocked `/v1/search/feedback` with real persistence: `SearchFeedback(query, result_symbol_id, signal, created_at)` where `signal ∈ {clicked, accepted, rejected, explicit_relevant, explicit_irrelevant}`. **Phase 1 use:** analytics only — dashboards showing zero-result-rate, click-through by search_type. **Do not build adaptive/learned ranking** until this table has meaningfully many judged examples (hundreds+) — until then, any "personalized ranking" claim would be fabricated in the same spirit as the metrics bug this whole plan is fixing (Rule 9, §47).

---

## 32. Documentation Strategy

```
README.md        — accurate, current-state only, links to ARCHITECTURE.md for the vision
ARCHITECTURE.md   — this document's §6–14, kept current as implementation lands
DEVELOPMENT.md    — local setup, actually-required dependencies only
API.md            — generated from OpenAPI schema, not hand-maintained prose
CLI.md            — command reference
DEPLOYMENT.md     — local mode + server mode, explicitly separated
SECURITY.md       — auth model, ingestion safety, responsible disclosure
CONTRIBUTING.md
ADR/              — one file per decision in §33 below
```

Every doc page must carry a status marker per feature it discusses: **Implemented / Experimental / Planned** — no exceptions, directly closing the audit's documentation-mismatch finding.

---

## 33. Architectural Decision Records (to write)

1. **Storage architecture** — Option D (local-first embedded, server mode adds Postgres) vs. A/B/C. *Decision:* D. *Rationale:* §7.
2. **Local vs. server mode as one codebase** — pluggable storage interface vs. a fork. *Decision:* one codebase, interface-based. *Rationale:* avoids maintaining two products.
3. **Vector index** — embedded HNSW (`hnswlib`/`sqlite-vec`) locally, optional pgvector in server mode. *Decision:* both, behind an interface.
4. **Lexical index** — Tantivy (embedded), replacing the current linear-scan `BM25Index`. *Decision:* adopt Tantivy for real; it was already the right choice, just unimplemented.
5. **Repository isolation model** — per-request authorization filter vs. per-index physical separation. *Decision:* logical filter (simpler, sufficient at target scale) with physically separate lexical/vector index segments per repo (cheap insurance against filter bugs).
6. **Incremental indexing strategy** — git-diff-based vs. hash-everything. *Decision:* git-diff when `.git` is available, hash-based fallback for non-git local paths.
7. **Call graph representation & confidence** — bare-name heuristic with confidence scoring vs. investing in full type resolution now. *Decision:* confidence scoring now; defer real type-aware resolution (a much larger effort) until user feedback shows it's the binding constraint.
8. **Embedding model** — keep `all-MiniLM-L6-v2` as default, defer CodeBERT evaluation until the relevance-evaluation harness (§20) exists to actually compare them.
9. **Agent protocol** — MCP as a thin adapter over the same internal API (§13), not a bespoke protocol.
10. **Authentication** — API keys as default, JWT only for future Web UI sessions.
11. **Queue architecture** — in-process job queue (single server) now; explicitly defer Redis/distributed queue until proven necessary (§38).

---

## 34. Migration From Current Prototype

| Component | Disposition | Rationale |
|---|---|---|
| `LanguageParser` ABC, `PythonParser`, `JavaScriptParser` | **Keep** | Correct, tested, extensible — no changes needed to add Go/Java later |
| `codepulse/models.py` (Pydantic models) | **Keep, extend** | Add `content_hash`, base-class list, `confidence` fields; no breaking redesign |
| `codepulse/embeddings/chunker.py` | **Keep** | Sound chunking strategy |
| `codepulse/embeddings/embedder.py` | **Refactor** | Remove silent hash-fallback; make model-load failure explicit and job-failing |
| `codepulse/search/bm25.py` (`BM25Index`) | **Replace** | Correct math, wrong data structure — swap for Tantivy-backed index behind the same conceptual interface |
| `codepulse/search/vector.py` (`VectorIndex`) | **Replace** | Same reasoning — swap brute-force NumPy for an embedded ANN structure, keep the `HybridRanker` interface it feeds |
| `codepulse/search/ranker.py` (`HybridRanker`) | **Keep** | Fusion math is correct; just needs to be fed repo-scoped, durable candidates instead of ephemeral global ones |
| `codepulse/parser/graph.py` (`CallGraphBuilder`) | **Refactor** | Keep the BFS traversal logic; add confidence scoring and persistence-backed (not memory-only) storage |
| `codepulse/indexer/engine.py` (`IndexingEngine`) | **Replace the state model, keep the pipeline steps** | The step-by-step logic (discover → parse → chunk → embed → persist) is the right shape; the *global singleton, no-reset, no-repo-scoping* state model is what must go — rebuild around per-repository, snapshot-based state |
| `codepulse/storage/db.py` (`StorageManager`, models) | **Refactor** | Add `RepositorySnapshot`, `IndexJob`, `APIKey`, `content_hash`/`confidence` columns; wire up Alembic migrations instead of `create_all()` |
| `codepulse/api/app.py` | **Replace incrementally, endpoint by endpoint** | Keep the 12-endpoint shape as a starting contract, but rebuild each handler against the new repo-scoped, durable engine rather than the global singleton; remove all hardcoded metric values |
| `codepulse/cli.py` | **Extend** | Keep Rich-based UX; add the missing commands from §22; wire to config system |
| `pyproject.toml` | **Trim + fix** | Remove `redis`, `pgvector`, `psycopg[binary]`, `alembic`-if-unused-still (actually use it instead), keep `tantivy` (finally use it), add `rich` |
| Shipped `codepulse.db` | **Delete from repo** | Should never have been committed; add a clean `.gitignore`-respecting first run |
| No `.git` in project | **Initialize immediately** | Prerequisite for git-diff incremental indexing and for any real collaboration on the project itself |

---

## 35. Recommended Final Architecture

```
                     ┌────────────────────────────┐
                     │  CLI  │  MCP  │  REST API   │   (thin adapters, same internal calls)
                     └──────────────┬──────────────┘
                                    │
                          ┌─────────▼─────────┐
                          │   API Gateway      │
                          │ Auth (API keys)    │
                          │ Rate limits        │
                          │ Central authz      │  ← single repository-scoping checkpoint
                          └─────────┬──────────┘
                                    │
              ┌─────────────────────▼─────────────────────┐
              │         CodePulse Intelligence Core        │
              │  (pure library — no HTTP/MCP awareness)    │
              └───────┬─────────────────────────┬──────────┘
                      │                         │
              ┌───────▼──────┐          ┌───────▼───────────┐
              │ Retrieval    │          │ Graph Engine       │
              │ lexical      │          │ symbols/refs       │
              │ (Tantivy)    │          │ callers/callees    │
              │ vector       │          │ confidence scoring │
              │ (embedded    │          │ impact analysis    │
              │  HNSW)       │          │ module clustering  │
              └───────┬──────┘          └───────┬───────────┘
                      │                         │
                      └───────────┬─────────────┘
                                  │
                     ┌────────────▼────────────┐
                     │  Repository Store       │
                     │  metadata (SQLite/PG)   │
                     │  snapshots              │
                     │  symbols / call edges   │
                     │  embeddings (durable)   │
                     └────────────┬────────────┘
                                  │
                     ┌────────────▼────────────┐
                     │  Indexing Pipeline      │
                     │  acquisition             │
                     │  git-diff / hash-diff    │
                     │  parsing (tree-sitter)   │
                     │  chunking                │
                     │  embedding                │
                     │  lexical + vector upsert │
                     │  job state machine       │
                     └──────────────────────────┘

Local mode: everything above in one process, SQLite + local index files, zero external services.
Server mode: same code, Postgres swapped in for metadata, index files on a shared volume, one server
process handling both API and indexing jobs until real load requires splitting them out.
```

---

## 36. Phased Roadmap

### Phase 0 — Foundation / Correctness
**Objective:** make the existing system honest and durable before adding anything.
Tasks: config system; Alembic migrations; repository/file/symbol upsert (idempotency); embedding persistence + load-on-startup; repo-scoped search engine (no more global singleton); remove fabricated metric values; `.gitignore`-respecting file discovery; fix CORS; trim/fix `pyproject.toml`; delete shipped `codepulse.db`; initialize `.git`.
Files affected: `storage/db.py`, `indexer/engine.py`, `api/app.py`, new `config.py`, new `alembic/`.
DB changes: add `content_hash` to `File`, add `RepositorySnapshot`, unique constraints for upsert keys.
Tests: idempotency tests, restart-survivability test, config-loading tests.
Definition of Done: indexing the same repo twice produces zero duplicate rows; killing and restarting the server preserves semantic search results; no endpoint returns a hardcoded number.
Complexity: **L**. Dependencies: none — this is the starting point.

### Phase 1 — Production Core
**Objective:** make the system safe to expose beyond a single trusted local user.
Tasks: API keys + central authorization checkpoint; real `IndexJob` table/state machine (replacing fake job IDs); rate limiting (in-process token bucket, no Redis yet); ingestion safety (path allow-listing, SSRF checks on git URLs, size caps); resource limits; proper structured error responses.
DB changes: `APIKey`, `IndexJob`.
API changes: auth on every non-health endpoint; `/v1/jobs/{id}` for real status polling.
Tests: security tests (unauth rejected, path traversal rejected, SSRF rejected), job lifecycle tests.
Definition of Done: no endpoint is callable without a key; a client can poll a job to real completion; an oversized/malicious ingestion request is rejected before doing work.
Complexity: **L**. Dependencies: Phase 0 (repo scoping and idempotency must exist first).

### Phase 2 — Search Engine at Scale
**Objective:** replace brute-force search internals without changing the ranking contract.
Tasks: swap `BM25Index` for a Tantivy-backed embedded lexical index; swap `VectorIndex` for an embedded ANN structure; keep `HybridRanker`'s interface unchanged; wire the already-implemented (but unused) RRF fusion path to be selectable via the API; add the relevance-evaluation harness and baseline dataset (§20).
Tests: benchmark suite against the three repo-size tiers (§19); relevance-regression tests using the curated dataset.
Definition of Done: measured p50/p95 latency numbers exist and are documented (replacing the old unverified target); Recall@10/MRR baseline established and gated in CI for future ranking changes.
Complexity: **L**. Dependencies: Phase 0 (durable, repo-scoped storage is a prerequisite for a real index to sit on top of).

### Phase 3 — Incremental Intelligence
**Objective:** stop paying full-reindex cost on every change.
Tasks: git-diff-based change detection; rename/move detection; symbol-level body-hash comparison to skip unchanged embeddings; call-graph confidence scoring; local BFS-based incremental graph updates.
DB changes: `CallEdge.confidence`; snapshot/commit tracking fields.
Definition of Done: a single-file commit to a 10K-file repo re-indexes in the §19 incremental budget, not the full-index budget; renamed files retain their symbol/embedding history.
Complexity: **L**. Dependencies: Phases 0–2.

### Phase 4 — Code Intelligence Depth
**Objective:** make impact analysis and symbol intelligence genuinely trustworthy.
Tasks: base-class/inheritance extraction; directly-vs-transitively-affected bucketing in impact analysis; deterministic module-clustering ("architecture discovery") via the import graph; references endpoint.
Definition of Done: impact analysis output clearly separates confidence tiers and depth buckets; a "explain this repo's structure" call returns real module clusters with no LLM involved.
Complexity: **M**. Dependencies: Phase 3 (confidence scoring feeds directly into this).

### Phase 5 — Agent Platform
**Objective:** make CodePulse genuinely excellent for AI agents, not just accessible to them.
Tasks: `/v1/agent/context` structured bundle endpoint (§12); MCP server exposing the same tools (§13); token-budget-aware truncation; snapshot/commit freshness metadata on every response.
Definition of Done: an agent can retrieve a complete, correctly-scoped context bundle for a symbol in one call, with explicit confidence and truncation flags.
Complexity: **M**. Dependencies: Phase 4 (needs real confidence scoring and impact analysis to assemble a trustworthy bundle).

### Phase 6 — Developer Experience
**Objective:** close the gap between "API exists" and "developers actually use this daily."
Tasks: full CLI command set (§22); VS Code extension MVP (§23, built only now that the API is stable); minimal Web UI (§24).
Complexity: **L**. Dependencies: Phase 5 (API/MCP contract must be stable before client UIs are built against it).

### Phase 7 — Team / Production
**Objective:** support a small engineering team, not just individuals.
Tasks: Postgres-backed server mode; RBAC beyond single-key-per-repo; Docker Compose packaging; observability wiring (metrics/logs/health, §18–19 finally turned on in code); backup/restore documentation; CI hook for commit-triggered incremental indexing.
Complexity: **XL**. Dependencies: everything above — team features should not be built on an unstable core.

---

## 37. Sprint-Level Milestones

```
Phase 0
  0.1 Config system (file + env + CLI precedence)
  0.2 Alembic migration setup, initial migration matching current schema
  0.3 Repository/File upsert with content_hash
  0.4 Symbol upsert by (file_id, name, start_line)
  0.5 Embedding persistence + load-on-startup hydration
  0.6 Repository-scoped BM25Index/VectorIndex/CallGraphBuilder (remove global singleton)
  0.7 Remove all hardcoded metric values from API responses
  0.8 .gitignore-respecting file discovery + .codepulseignore support
  0.9 Fix CORS policy; trim pyproject.toml; add rich; delete committed codepulse.db; git init
  0.10 Restart-survivability + idempotency test suite

Phase 1
  1.1 APIKey model + hashing + creation flow
  1.2 Central authorization dependency (FastAPI dependency injected into every route)
  1.3 IndexJob state machine + /v1/jobs/{id}
  1.4 In-process token-bucket rate limiter
  1.5 Ingestion safety: path allow-list, SSRF check, size caps
  1.6 Structured error response format across all endpoints
  1.7 Security test suite

Phase 2
  2.1 Tantivy-backed lexical index adapter behind existing BM25Index call sites
  2.2 Embedded ANN vector index adapter behind existing VectorIndex call sites
  2.3 Wire RRF fusion path to be selectable via API `search_type`/config
  2.4 Curated relevance dataset (100–300 query/symbol pairs) + eval harness
  2.5 Benchmark harness across 3 repo-size tiers; publish measured numbers

Phase 3
  3.1 Git-diff change detection (added/modified/deleted)
  3.2 Rename/move detection + symbol re-keying
  3.3 Symbol-level body_hash comparison to skip unchanged embeddings
  3.4 CallEdge.confidence scoring
  3.5 Local BFS incremental graph update

Phase 4
  4.1 Base-class/inheritance extraction (Python + JS/TS)
  4.2 Impact analysis: direct vs. transitive bucketing
  4.3 Deterministic module-clustering via import graph
  4.4 References endpoint

Phase 5
  5.1 Context-bundle assembly function (shared by REST + MCP)
  5.2 /v1/agent/context endpoint
  5.3 MCP server wrapping the same tools
  5.4 Token-budget truncation logic + snapshot freshness metadata

Phase 6
  6.1 Full CLI command set (update, refs, callers, callees, status, doctor)
  6.2 VS Code extension MVP
  6.3 Web UI MVP (dashboard, search, symbol view, dependency graph)

Phase 7
  7.1 Postgres storage backend (behind existing storage interface)
  7.2 RBAC roles + repo permission model
  7.3 Docker Compose packaging
  7.4 Metrics/logs/health wiring (activate structlog/prometheus-client)
  7.5 Backup/restore documentation + CI incremental-index hook
```

---

## 38. Dependency Graph

```
Config system
   ↓
Data model + migrations
   ↓
Persistence (upsert, embedding durability)
   ↓
Repository isolation (scoped engine)
   ↓
Auth + central authorization
   ↓
Real job system
   ↓
Scalable retrieval (Tantivy + ANN)
   ↓
Incremental indexing (git-diff, confidence scoring)
   ↓
Code intelligence depth (impact buckets, module clustering)
   ↓
Agent API / MCP
   ↓
CLI completeness / IDE / Web UI
   ↓
Team/server production hardening
```

(Auth is pulled earlier than the original prompt's suggested ordering — placed right after repository isolation rather than after search scaling — because ingestion-endpoint safety is a P0 risk the moment the server is reachable by anyone other than its author, and shouldn't wait behind search-performance work.)

---

## 39. Explicitly Defer

| Deferred | Why wait |
|---|---|
| **Kubernetes** | No workload here needs horizontal pod scaling yet; a single server process comfortably serves the target team scale. Revisit only if a specific customer needs multi-node. |
| **Redis (cache/queue)** | An in-process job queue and no query cache are sufficient until there's a real multi-worker deployment; adding Redis now is infrastructure for a problem that doesn't exist yet (Rule 4). |
| **Distributed workers** | Same reasoning — single-process indexing is fast enough at the target repo sizes once Phase 2/3 land. |
| **LLM-generated explanations as a primary feature** | Deterministic graph/search must be solid and trusted first (Rule 7); LLM narrative features are additive polish, not core value. |
| **Many programming languages beyond the §14 top few** | Depth (confidence scoring, incremental indexing, agent API) matters more than breadth right now; each new language is real ongoing maintenance. |
| **Sophisticated learned ranking** | No relevance-feedback dataset exists yet to train against (§32); building this now would be guesswork. |
| **Enterprise SSO** | No multi-org/team-of-teams customer exists yet to justify it; API keys + basic RBAC cover the target near-term users. |
| **Complex frontend framework/SPA** | The Web UI MVP (§24) is intentionally small; a heavier frontend investment should wait until usage data shows what people actually want from it. |
| **Microservices split** | The "one process, pluggable storage" design (§35) scales far enough for the roadmap's horizon; splitting API/worker/indexer into separate services adds operational cost with no current benefit. |

---

## 40. MVP Definitions

**Developer MVP:** `codepulse index .` works with zero external infrastructure; `search`, `symbol`, `impact`, `similar` all return correct, restart-durable results; setup takes under 5 minutes. *Acceptance:* a developer can clone a real repo, index it, and find a function by intent faster than `grep -r`.

**Team MVP:** the above, plus API-key auth, a shared server (Docker Compose), multi-repo isolation verified by test, and incremental indexing so the shared index doesn't require full re-index per push. *Acceptance:* two engineers with different API keys scoped to different repos can never see each other's search results; a push to a repo updates the shared index within the incremental-indexing time budget.

**Agent MVP:** `/v1/agent/context` and the MCP server both return a deterministic, confidence-annotated, token-bounded bundle for a given symbol, with snapshot freshness metadata. *Acceptance:* an agent can retrieve "definition + callers + callees + related symbols" for a symbol in one call, under a defined token budget, with no non-deterministic output across repeated calls against the same snapshot.

---

## 41. Product KPIs

**Search quality:** zero-result rate, result click-through rate (from real feedback logging, §32), Recall@10/MRR against the curated evaluation set.
**Developer productivity (proxy metrics, not directly instrumentable without user studies):** median time-to-first-result per session, proportion of sessions using `impact`/`similar` (signals deeper trust beyond basic search).
**Agent quality:** context-acceptance rate (agent didn't need a follow-up query), irrelevant-context rate (from feedback signal), token efficiency (bundle size vs. a naive "whole file" baseline), tool-call success rate (MCP calls that don't error).
**System health:** indexing success rate, p50/p95/p99 indexing and search latency (§19), API error rate, memory/disk usage per repo size tier.

---

## 42. Risk Register (Expanded)

| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| Cross-repository data leakage | High (if unaddressed) | Critical | Central authorization checkpoint (§15/§30); physically separate lexical/vector index segments per repo as defense-in-depth |
| Embedding quality poor / silently degraded | Medium | High | Fail-fast on model-load failure (§17), not silent hash fallback; relevance evaluation harness (§20) |
| Large repository indexing too slow | High | High | Incremental indexing (Phase 3); measured benchmarks before promising numbers (§19) |
| Call graph false positives | Medium | Medium | Confidence scoring surfaced to callers (§11), not hidden |
| Search relevance poor | Medium | High | Curated relevance dataset + Recall/MRR regression gate (§20) |
| Infrastructure too complex for target user | Medium | High | Local-first architecture as the default (§7, §25); server infra strictly opt-in |
| LLM dependency becomes expensive/unreliable | Medium | Medium | Deterministic core is the source of truth (§27); LLM features are optional/toggleable |
| Ingestion endpoint used for SSRF/path traversal | Medium | Critical | Explicit URL/path validation, allow-listing, size caps (§15, Phase 1) |
| Fabricated metrics reintroduced under time pressure | Low (with the rule now explicit) | High (trust) | API design rule (§21): no endpoint may report an unmeasured value; contract tests enforce field provenance where feasible |
| Incremental indexing produces inconsistent state after crash | Medium | High | Snapshot-based reads (always serve last `ready` snapshot) + idempotent, transactional writes (§8/§17) |
| Dependency footprint scares off new contributors/users | Medium | Medium | Trim `pyproject.toml` to actually-used packages (Phase 0) |
| Team adoption blocked by lack of RBAC/isolation guarantees | Medium | High | Phase 1 auth + Phase 7 RBAC; isolation tested explicitly, not assumed |
| Rename/move detection incorrectly merges unrelated symbols | Low | Medium | Conservative matching heuristic (git rename detection + name/type match) with fallback to delete+recreate rather than a risky guess |

---

## 43. Migration From Current Prototype — Summary

See §34's full table. In one sentence: **keep the parsers, chunker, and ranking math; replace the state-management and storage-backing of search/embeddings/call-graph; add the entire control plane (auth, jobs, config) that currently doesn't exist at all.**

---

## 44. If I Were Taking Ownership Today: The Real 6-Month Plan

Prioritizing **shipping usable increments**, not infrastructure for its own sake.

**Month 1 — Make it honest and durable (Phase 0 + start of Phase 1).**
Fix idempotency, embedding persistence, repo-scoped search, fabricated metrics, config system, `.gitignore`-respecting discovery, dependency cleanup, git-init the project itself. Ship: a `pip install`-able local CLI that indexes a repo and gives correct, restart-durable search — this alone is already more trustworthy than what exists today, and it's the foundation everything else needs.

**Month 2 — Lock down the API and make search fast (rest of Phase 1 + start of Phase 2).**
API keys, real job tracking, ingestion safety, rate limiting. Swap in Tantivy for lexical search (it's a drop-in for the existing interface, high leverage). Ship: a server that's safe to run somewhere other than localhost, with search that's noticeably faster on real repos.

**Month 3 — Finish scalable search, start incremental indexing (rest of Phase 2 + start of Phase 3).**
Embedded ANN vector index; relevance evaluation harness with real Recall/MRR numbers; git-diff-based incremental indexing for the common case (single/few-file commits). Ship: `codepulse update` that re-indexes a commit in seconds, not minutes, plus published, honest performance numbers.

**Month 4 — Code intelligence depth (Phase 4).**
Call-graph confidence scoring, direct-vs-transitive impact analysis, base-class extraction, deterministic module clustering. Ship: an `impact` command developers actually trust because it tells them *how confident* it is, and a first version of "explain this repo's structure" with zero LLM dependency.

**Month 5 — Agent platform (Phase 5).**
`/v1/agent/context` structured bundle endpoint + MCP server as a thin adapter over it. Ship: CodePulse is now usable by an AI coding agent (Claude Code, Cursor, etc.) as a retrieval tool, with deterministic, confidence-annotated, token-bounded context — this is the differentiator that separates CodePulse from "yet another code search tool."

**Month 6 — Package for a team, not just an individual (start of Phase 7, selectively).**
Docker Compose server mode with Postgres swap-in, minimal RBAC (viewer/indexer/admin), CI hook for commit-triggered incremental indexing, and the observability wiring (metrics/health/logs) turned on for real. Deliberately **skip** Kubernetes, Redis, and the Web UI/VS Code extension in this window — those are valuable but not what makes the next 10 users successful.

**End state after 6 months:** a developer can `pip install`, index a real repository in under a minute, search it faster and more accurately than grep, run a trustworthy impact analysis before a risky change, and point an AI coding agent at the same index via MCP for structured, token-efficient retrieval — all without touching Docker, Postgres, or Kubernetes unless they're setting it up for a team.

---

## 45. Final Prioritized Backlog

| ID | Phase | Feature | User Value | Technical Scope | Dependencies | Priority | Complexity | Definition of Done |
|---|---|---|---|---|---|---|---|---|
| B1 | 0 | Config system | Enables any real deployment mode | New `config.py`, env/file/flag precedence | None | P0 | S | CLI/API read all settings from config, not hardcoded values |
| B2 | 0 | Idempotent repo/file/symbol upsert | No data duplication on re-index | `storage/db.py`, `indexer/engine.py` | B1 | P0 | M | Re-indexing unchanged repo produces zero net writes |
| B3 | 0 | Embedding persistence + hydration | Semantic search survives restart | `storage/db.py`, `indexer/engine.py` | B2 | P0 | M | Restart test passes: same query, same results |
| B4 | 0 | Repo-scoped search engine (remove global singleton) | No cross-repo data mixing | `indexer/engine.py`, `search/*` | B2 | P0 | L | `repository_id` actually filters every search |
| B5 | 0 | Remove fabricated metrics | Trustworthy API responses | `api/app.py` | None | P0 | XS | No hardcoded numeric field remains |
| B6 | 0 | `.gitignore`/`.codepulseignore` respecting discovery | Avoid indexing secrets/noise | `indexer/engine.py` | B1 | P0 | S | Ignored files never appear in index |
| B7 | 0 | Dependency cleanup + git init | Working clean-install experience | `pyproject.toml`, repo root | None | P0 | XS | `pip install -e .` succeeds without unused heavy deps; `.git` exists |
| B8 | 1 | API key auth + central authorization | Safe to expose beyond localhost | New `APIKey` model, FastAPI dependency | B4 | P0 | M | No endpoint callable without a valid, scoped key |
| B9 | 1 | Real IndexJob tracking | Trustworthy job status | New `IndexJob` model, `/v1/jobs/{id}` | B2 | P0 | M | Returned `job_id` is pollable to real completion |
| B10 | 1 | Ingestion safety (path/SSRF/size limits) | Prevents abuse of the dangerous endpoint | `api/app.py` ingestion handler | B8 | P0 | M | Path traversal / SSRF / oversized-repo attempts rejected with tests proving it |
| B11 | 1 | Rate limiting (in-process) | Prevents resource-exhaustion abuse | New middleware | B8 | P1 | S | Excess requests receive 429 with retry-after |
| B12 | 2 | Tantivy-backed lexical index | Real scalability for keyword search | `search/bm25.py` replacement | B4 | P1 | L | Benchmarked latency at 10K/100K-file tiers documented |
| B13 | 2 | Embedded ANN vector index | Real scalability for semantic search | `search/vector.py` replacement | B4 | P1 | L | Same as above for semantic search |
| B14 | 2 | Relevance evaluation harness | Prevents ranking regressions | New eval dataset + CI gate | B12, B13 | P1 | M | Recall@10/MRR tracked and gated in CI |
| B15 | 3 | Git-diff incremental indexing | Fast updates, not full re-index | `indexer/engine.py`, new snapshot logic | B2, B12, B13 | P1 | L | Single-file commit re-indexes within budget in §19 |
| B16 | 3 | Call-graph confidence scoring | Trustworthy impact/call-graph output | `parser/graph.py` | B4 | P1 | M | Every `CallEdge` carries a confidence tier |
| B17 | 4 | Impact analysis depth (direct/transitive) | Actionable "what breaks" answers | `api/app.py`, `parser/graph.py` | B16 | P2 | M | Response distinguishes depth-1 vs. depth-2+ affected symbols |
| B18 | 4 | Deterministic module clustering | "Explain this repo" without an LLM | New graph-analysis module | B15 | P2 | M | Returns coherent module clusters on CodePulse's own repo as a test case |
| B19 | 5 | Agent context-bundle endpoint | Core agent-facing value | `api/app.py`, shared assembly function | B16, B17 | P1 | M | Bundle includes confidence, truncation flag, snapshot commit |
| B20 | 5 | MCP server | Native agent integration | New `mcp/` module wrapping B19's function | B19 | P1 | M | Same tool set/output as REST, verified via contract tests |
| B21 | 6 | Full CLI command set | Daily-usable developer workflow | `cli.py` | B4, B9 | P2 | M | All §22 commands implemented with `--json` output |
| B22 | 6 | VS Code extension MVP | Reach developers where they work | New extension repo/package | B19 (stable API) | P3 | L | Search/symbol/impact panels functional against a real server |
| B23 | 6 | Web UI MVP | Visualize dependency graph, dashboard | New frontend | B12, B13, B17 | P3 | L | Dashboard + search + symbol view + dependency graph render correctly |
| B24 | 7 | Postgres storage backend | Team-scale durability | `storage/db.py` behind interface | B2 | P2 | L | Same test suite passes against Postgres backend |
| B25 | 7 | RBAC roles | Safe team collaboration | `APIKey`/authorization extension | B8 | P2 | M | Viewer/indexer/admin permission boundaries tested |
| B26 | 7 | Docker Compose packaging | Easy team deployment | New `docker-compose.yml`, Dockerfile | B24 | P2 | M | `docker compose up` yields a working server against a fresh repo |
| B27 | 7 | Observability wiring (metrics/logs/health) | Operable in production | Activate `structlog`/`prometheus-client` | B9 | P2 | M | `/health/ready`, `/metrics` endpoints live and accurate |
| B28 | 1 | Real search feedback persistence | Foundation for future relevance work | New `SearchFeedback` model | B8 | P2 | S | Feedback endpoint writes real rows, no fabricated "weights updated" message |
| B29 | Defer | Redis cache/queue | Not needed at current scale | — | — | P4 | — | Revisit only when a multi-worker deployment is actually being built |
| B30 | Defer | Kubernetes manifests | Not needed at current scale | — | — | P4 | — | Revisit only if a specific customer needs multi-node scaling |

---

*End of strategy document. Next step, on your go-ahead: begin Phase 0, starting with B1 (config system) and B2 (idempotent upsert), since nearly everything else in the backlog depends on them.*
