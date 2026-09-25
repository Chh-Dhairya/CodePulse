# CodePulse — Client Strategy: VS Code Extension vs. Web UI

*A recommendation and implementation plan for both, written against verified checkpoint `c5e4cc5`. This is a planning document only — no implementation code was written or changed to produce it. Grounded in the actual current REST/CLI/MCP surfaces (verified directly against the code, not assumed) and the roadmap's own §23/§24/B22/B23 design intent, not invented from scratch.*

---

## TL;DR recommendation

**Build the VS Code extension first.** Both are genuinely worth building eventually — this isn't "one is right, one is wrong" — but the extension is the better *next* step, for four concrete reasons laid out below. A phased implementation plan for **both** follows, since that's what you asked for, plus a full list of what's left before the project overall is "complete" in any meaningful sense.

---

## 1. Why the VS Code extension first

### It matches what CodePulse actually is

CodePulse's own stated identity (from `CODEPULSE_PRODUCT_AND_ARCHITECTURE.md`, Part 1) is a **local-first, agent-native code intelligence engine** — something that runs on a developer's own machine, alongside code they're already looking at, with zero data leaving the machine. A VS Code extension *is* that story: it runs where the developer already is, reads the workspace that's already open, and talks to a CodePulse server running on the same machine. A web app requires a browser tab, a separate mental context, and — for a genuinely local-first tool — an awkward question of how a sandboxed browser is supposed to know which local repository you mean at all.

### The backend was already built for this shape of client, not a dashboard

Look at what actually exists today: symbol lookup, references, impact analysis, similar-code search, autocomplete, module structure — these are all "tell me about the specific piece of code I'm looking at right now" operations. That's the exact shape of editor features (hover cards, CodeLens annotations, a references panel, a quick-pick search) — not the shape of a web dashboard, which wants aggregate views, multi-repo browsing, and things worth a whole page rather than a hover tooltip. The REST API's own design (repository-scoped, symbol-by-id, one workspace at a time) already assumes an editor-like caller more than a browser-like one.

### It's smaller and buildable entirely on what's already tested

A VS Code extension MVP needs no new backend architecture decisions — no auth-session model, no multi-tenancy story, no hosting/deployment question. It's a thin TypeScript client over the REST API (or the MCP server) that already exists, already has 569 passing tests behind it, and already returns exactly the fields an extension UI would render. A web app MVP, even a minimal one, needs a framework decision, a build pipeline, and — per the roadmap's own §21 — a real answer to how browser sessions authenticate (the roadmap explicitly says JWT should wait for "team/server mode," which doesn't exist yet; a local-only web app is stuck reusing raw API keys in browser storage, a weaker security story than a VS Code extension using `SecretStorage`).

### The web UI's best feature needs the extension's absence to justify itself

The roadmap's own §24 calls the dependency-graph visualization "CodePulse's most visually differentiated feature" — and that's genuinely true, but it's *also* the one feature that doesn't fit naturally inside an editor extension (a force-directed graph wants real screen space, not a VS Code panel). That's a real, good reason to eventually build the web UI too — but it's a reason to build it as a **complement** to the extension (a bigger canvas for the one feature that needs it), not a reason to build it *instead*, and not a reason to build it *first*, since without a team/server backend it's mostly duplicating what the extension will already do better in-context.

**In short:** the extension is smaller, more buildable today, and more aligned with what CodePulse already is. The web UI is real, worthwhile, and should happen too — but as a second step, and it will be a better product once Phase 7.1 (Postgres/team mode) exists to give it an actual multi-user reason to exist as a separate surface, rather than a single-player browser wrapper around the same CLI.

---

## 2. Two small, real backend gaps found while planning this

Verified directly against the code before writing the plans below — these are not covered by the earlier backend-gap audit or its recheck, because they only became relevant once actually designing a client around the API's current shape.

1. **No "list all indexed repositories" endpoint.** Every repository-scoped route (`/v1/repositories/{id}/stats`, `/index-status`, `/modules`, etc.) needs a `repo_id` the caller already knows. There is no `GET /v1/repositories` to enumerate what's indexed. A web dashboard's very first screen ("here's what's indexed") needs this; a VS Code extension opening an unfamiliar workspace would benefit from it too (today it would have to call `POST /v1/repositories/index` — idempotent, but heavier than a plain lookup — just to resolve a workspace path to a `repo_id`).
2. **No "resolve symbol at file + line" capability.** `GET /v1/symbols/{id}` needs a numeric symbol id; `GET /v1/files/{file_id}/symbols` needs a numeric file id. Neither exists for "what symbol contains line 42 of `auth.py`" — which is exactly what a hover provider or CodeLens needs to answer for the cursor's current position. This isn't just a missing REST route; the underlying engine has no method for it either (`_resolve_symbol` in the CLI matches by *name*, not by *location*).

Both are small, self-contained, low-risk additions (a new read-only query each, no schema change) — flagged here for visibility, not yet built. They'd naturally land as Phase 0 of the VS Code extension plan below.

---

## 3. VS Code Extension — implementation plan

Scope per the roadmap's own §23: *"command-palette search, go-to-symbol, 'find references,' 'impact analysis' panel, 'similar code' panel, and a status-bar indexing indicator... the extension is a thin presentation layer, never reimplementing retrieval logic client-side."* B22's DoD: *"Search/symbol/impact panels functional against a real server."*

### Phase 0 — Backend prerequisites (small, in this repo)
- `GET /v1/repositories` — list indexed repositories (id, name, url, file/symbol counts, last-indexed time). Thin wrapper over an existing query pattern (`RepositoryModel`), gated by which repos the caller's key is scoped to.
- A "resolve symbol at location" capability — either `GET /v1/files/by-path?repo_id=&path=` (to get a `file_id`) composed with the existing `/v1/files/{file_id}/symbols`, or a single new `GET /v1/repositories/{id}/resolve?path=&line=` doing both steps server-side. The latter is fewer round trips for the extension and keeps "which symbol contains this line" logic in the shared core rather than duplicated client-side — consistent with CLAUDE.md's own "REST/CLI/MCP call the same underlying functions" rule.

### Phase 1 — Foundation (new repo: `codepulse-vscode`, TypeScript)
- Per the roadmap's own B22 framing ("New extension repo/package"), this lives outside `codepulse/` as its own package — a VS Code extension is a fundamentally different tech stack (TypeScript, the `vscode` extension API), not a few files bolted onto the Python backend.
- Activation: on workspace open, check for a running local CodePulse server (`GET /health/ready`); if none, offer to launch `codepulse serve` as a managed child process (the CLI already supports this — `codepulse serve --host --port`).
- Settings: `codepulse.serverUrl` (default `http://127.0.0.1:8000`), `codepulse.apiKey`.
- Auth: store the API key in VS Code's `SecretStorage` (never a plain settings file) — prompt for one on first use, or generate one via a command that shells out to `codepulse` CLI's existing key-creation path.
- Status bar item: indexing state for the current workspace (`GET /v1/repositories/{id}/index-status`, using Phase 0's new list/resolve endpoint to find `id` from the open folder's path).

### Phase 2 — Core navigation (every feature below is a thin call to an already-built, already-tested REST endpoint)
- **Command: "CodePulse: Index Workspace"** → `POST /v1/repositories/index`, progress notification polling `GET /v1/jobs/{id}`.
- **Command: "CodePulse: Search"** → quick-pick UI over `POST /v1/search` (hybrid by default), selecting a result jumps to `file:line`.
- **Hover provider** on symbols → Phase 0's resolve-by-location endpoint, then render signature/docstring from the result (mirrors `GET /v1/symbols/{id}`'s own fields).
- **CodeLens** above each function/class: "N callers · M callees" → `GET /v1/symbols/{id}/impact-analysis`; clicking opens a panel with the direct/transitive breakdown, confidence included.
- **Sidebar TreeView — References** → `GET /v1/symbols/{id}/references` (direct callers + subclasses).
- **Sidebar TreeView — Similar Code** → `POST /v1/symbols/{id}/similar`.
- **Command: "CodePulse: Module Structure"** → `GET /v1/repositories/{id}/modules`, rendered as a simple collapsible tree (the full graph visualization is the web UI's job, per §24 — the extension shows the same data in a lighter form).

### Phase 3 — Agent integration (beyond the roadmap's literal §23 scope, a genuine value-add worth calling out explicitly since it isn't part of B22's own DoD)
- Register CodePulse's existing MCP server as a discoverable MCP provider inside VS Code, so any MCP-aware assistant already running in the editor (Copilot Chat, Claude, Cline, etc.) gets CodePulse's 8 tools automatically, without separate configuration. This reuses `codepulse serve --mcp` exactly as it exists today — no new backend work, just extension-side wiring.

### Phase 4 — Polish and ship
- Marketplace packaging (icon, README, publisher registration), multi-root workspace support (multiple indexed repos open at once, using Phase 0's list endpoint to disambiguate), basic telemetry opt-in if wanted, error states for "server unreachable" / "not yet indexed."

---

## 4. Web UI — implementation plan

Scope per the roadmap's own §24: *"repository dashboard (indexing status/freshness), a search box with results linking to source, a symbol detail view showing callers/callees, and a dependency-graph visualization... Do not build a generic 'developer portal' — every UI element should expose retrieval/graph intelligence the CLI/API already has, nothing new."* B23's DoD: *"Dashboard + search + symbol view + dependency graph render correctly."*

### Phase 0 — Backend prerequisites (same as the extension's Phase 0)
- `GET /v1/repositories` — the dashboard's very first screen needs this list; without it there is no dashboard.
- No location-resolution endpoint is needed here (the web UI doesn't have a "cursor position" the way an editor does) — this is a case where the two clients genuinely need different small backend additions, not identical ones.

### Phase 1 — Foundation (new repo: `codepulse-web`)
- Framework choice deliberately left open here — the roadmap itself doesn't prescribe one, and per its own §33 risk register, "a heavier frontend investment should wait until usage data shows what people actually want" — start with the lightest reasonable choice (a small SPA, e.g. Vite + a minimal component library) rather than a large framework commitment.
- Local dev flow: the user runs `codepulse serve` (already built); the web app is a static bundle pointing at that server's URL, configurable via a settings screen.
- Auth: an API-key entry screen, stored in browser storage — explicitly a single-user, local-only auth story for now, matching the roadmap's own note that JWT sessions are a *team/server-mode* concern (§21), not something to build ahead of Phase 7.1.

### Phase 2 — Core pages (each a thin view over an existing REST endpoint)
- **Dashboard** → Phase 0's new `GET /v1/repositories` list, each entry linking to its own stats (`GET /v1/repositories/{id}/stats`) and index status.
- **Search page** → `POST /v1/search`, with a `search_type` toggle (hybrid/keyword/semantic/rrf — all four already exist), results linking to a symbol detail page.
- **Symbol detail page** → `GET /v1/symbols/{id}`, with tabs for references (`/references`), impact analysis (`/impact-analysis`), and similar code (`/similar`) — every tab is one more existing endpoint, no new backend logic per tab.
- **Architecture / dependency-graph page** → `GET /v1/repositories/{id}/modules`, rendered as an actual force-directed or similar graph visualization. This is the page the roadmap itself calls out as the web UI's most differentiated feature, and the one place a browser's screen space genuinely earns its keep over an editor panel — worth prioritizing within this plan, exactly as §24 says.
- **Indexing flow** → trigger via the dashboard, progress via job polling, same pattern as the extension.

### Phase 3 — Team-mode honesty check
- Be explicit with users at this point that without Phase 7.1 (Postgres backend, currently blocked in this environment — see below), the web UI is a **single-user, local-only** browser interface, not a shared team dashboard. That's a real, known limitation to disclose in the UI itself (e.g., in a settings/about screen), not just in this document — don't let the web app's own framing overclaim collaboration it can't yet deliver.

### Phase 4 — Polish and ship
- Decide how it's served: bundled as static files `codepulse serve` itself can host (simplest, keeps "local-first, one process" intact), or a genuinely separate dev server. The former fits this project's own architecture better; the latter is more conventional for web frontends. Worth a deliberate decision when this phase is actually reached, not assumed now.

---

## 5. What's left to fully "complete" the project

Everything from the original backend-gap audit and its follow-up recheck is done (10/10 findings resolved, most recently the dead-code cleanup). What genuinely remains, gathered in one place:

### Genuinely blocked roadmap items (unchanged since first identified)
- **Phase 6.2 VS Code extension** / **Phase 6.3 Web UI** — exactly what this document just planned. Both were blocked only because they need their own tech stack with no existing spec — that gap is what this document closes; actually building either still requires the work described above.
- **Phase 7.1 Postgres storage backend** — blocked on no available Postgres instance in this environment. Needed for genuine team/multi-user mode, and for the web UI to become more than a single-user local tool.
- **Phase 7.3 Docker Compose packaging** — blocked on no available Docker in this environment. Needed for an easy, reproducible team/server deployment.

### Smaller, already-disclosed gaps (from `CODEPULSE_PRODUCT_AND_ARCHITECTURE.md`'s Part 15, still accurate)
- No CI has ever actually run on real infrastructure — the workflow file exists and is locally verified, but this repo still has no remote (by your own earlier choice to keep it local).
- API key expiry and audit logging remain unbuilt (revocation itself was built earlier in this session).
- No repository-deletion endpoint exists (a real gap now that `PRAGMA foreign_keys` enforcement means deleting one would correctly cascade — but nothing calls it yet).
- The rate limiter and the advisory per-repository job lock are both in-memory/single-process only — fine for local-first, a real limitation the moment multiple server processes exist (which needs Phase 7.1/7.3 anyway).
- Search-index segments still have no literal on-disk persistence (mitigated, not solved, by the SQL-rehydration fix built earlier this session).
- `SymbolType.MODULE` looks unproduced by either parser (noticed during the recent dead-code cleanup, deliberately not acted on — a candidate for its own small, separate decision later).

### The two new gaps this document itself found
- `GET /v1/repositories` (list endpoint) — needed by both clients' Phase 0.
- A "resolve symbol by file+line" capability — needed by the VS Code extension's Phase 0 specifically.

None of the above has been started. This document is the plan; the next step, whenever you're ready, is picking a starting point (most likely the VS Code extension's Phase 0/1, per the recommendation above) and working through it the same one-milestone-at-a-time way the rest of this project has been built.
