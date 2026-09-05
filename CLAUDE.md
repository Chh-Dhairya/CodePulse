# CodePulse Claude Code Instructions

## 1. Project Authority

`CodePulse_Strategy_and_Roadmap.md` is the authoritative source of truth for product strategy, architecture, roadmap, milestone ordering, scope, and permanent invariants.

`Implementation.md` and `README.md` are secondary documentation only.

If any conflict exists between the roadmap and anything else — including this file, `CODEPULSE_PROGRESS.md`, prior agent claims, or prior conversation context — **the roadmap wins.**

## 2. Product Direction

CodePulse is a **local-first, agent-native code intelligence engine**.

Core loop (do not reinterpret):

```
Index → Retrieve → Explain (structure) → Navigate → Decide (impact) → Act
```

Stated differentiation (do not reinterpret):

- Unified lexical + semantic + structural/graph retrieval.
- Agent-native, token-efficient structured retrieval.
- Local-first / self-hosted operation.

## 3. Architecture Rules

Intended architecture:

```
CLI / MCP / REST API
        ↓
API Gateway / authorization (where applicable)
        ↓
CodePulse Intelligence Core
        ↓
Retrieval + Graph Engine
        ↓
Repository Store
        ↓
Indexing Pipeline
```

- The intelligence core must remain independent of HTTP/MCP concerns.
- REST, CLI, and MCP must call the same underlying intelligence functions — never separate implementations.
- Local mode is the default.
- SQLite plus embedded/local indexes are the default local architecture.

Do not introduce architectural complexity without explicit roadmap justification.

## 4. Permanent Invariants

Non-negotiable unless the authoritative roadmap itself is changed:

- Repository isolation
- Idempotent indexing
- Durable embeddings
- Safe snapshots
- Deterministic retrieval for the same query/snapshot
- Call-graph uncertainty represented with confidence
- Explicit truncation/omission in agent responses
- No fabricated metrics
- No silent pseudo-embedding fallback
- REST/CLI/MCP shared intelligence core
- Local-first operation by default

## 5. Roadmap Discipline

Implement one roadmap milestone at a time. Never skip ahead. Never silently combine multiple milestones.

Before implementation:

1. Read the relevant roadmap section.
2. Inspect the current implementation.
3. Inspect relevant tests.
4. Inspect database/schema state if applicable.
5. Identify exact files affected.
6. Produce an implementation plan.
7. Implement only the requested milestone.
8. Run tests.
9. Inspect the Git diff.
10. Verify against the roadmap.
11. Update `CODEPULSE_PROGRESS.md`.
12. Create a Git checkpoint when the milestone is complete.

## 6. Deferred Scope

Do not prematurely introduce:

- Kubernetes
- Redis
- Distributed workers
- Microservices
- Enterprise SSO
- Full organization/user tenancy
- Learned/adaptive ranking
- LLM reranking
- LLM-generated explanations as source of truth
- Unnecessary frontend complexity
- Unnecessary language expansion

Only introduce deferred functionality when the roadmap explicitly reaches that milestone.

## 7. Database Safety

`codepulse.db` contains real development data.

- Never casually delete, recreate, reset, truncate, or overwrite it.
- Use Alembic for schema evolution.
- Migrations must be non-destructive unless the roadmap explicitly requires otherwise.
- For migration testing, prefer temporary databases or copies.
- Never use the real development database as a disposable test database.

## 8. Git Discipline

Git is the durable implementation history.

- Never blindly reset or revert the repository.
- Before changing anything after an interruption: run `git status`, `git diff`, `git diff --cached`, `git log`, and inspect relevant files and tests.
- Every meaningful completed roadmap milestone should receive a clear Git commit.
- Never claim work is complete based solely on a previous agent report.
- The repository is the source of truth.

## 9. Testing Discipline

- Never weaken or delete tests merely to make implementation pass.
- Do not skip tests without explicitly reporting why.
- New functionality requires focused tests.
- Existing tests must continue passing unless a roadmap-approved behavior change intentionally changes them.
- Always report exact test results.

## 10. Context Window / Session Exhaustion Protocol

This section is CRITICAL.

If the context window is becoming limited, do not leave significant implementation work undocumented.

Before context exhaustion or ending a long implementation task:

1. Finish the smallest safe unit of work possible.
2. Run relevant tests.
3. Inspect git status.
4. Inspect git diff.
5. Update `CODEPULSE_PROGRESS.md` with: current milestone, completed work, incomplete work, files changed, tests run, test results, database state if relevant, known problems, exact next action.
6. If the milestone is complete, create the Git checkpoint.
7. If the milestone is incomplete, clearly mark it PARTIAL.
8. Never claim COMPLETE when work remains.
9. Never leave an undocumented half-finished migration.
10. Never assume the next context remembers the conversation.

When starting a fresh context:

1. Read `CLAUDE.md`.
2. Read `CODEPULSE_PROGRESS.md`.
3. Read `CodePulse_Strategy_and_Roadmap.md`.
4. Run `git status`.
5. Run `git log`.
6. Inspect the current working tree.
7. Run relevant tests.
8. Verify the progress document against the actual repository.
9. Only then continue implementation.

The filesystem, Git history, tests, database state, roadmap, `CLAUDE.md`, and `CODEPULSE_PROGRESS.md` are the durable project state.

**The conversation is NOT the durable project state.**

## 11. Progress File Rules

`CODEPULSE_PROGRESS.md` must be updated after every meaningful milestone or interrupted implementation.

- Never fabricate progress.
- Never mark work complete merely because code exists.
- Record actual verification.

## 12. Change Scope

Do not make opportunistic cleanup during a focused milestone.

Do not fix unrelated technical debt unless:

- it is required by the current milestone, or
- the roadmap explicitly requires it.

If an unrelated problem is discovered, record it in `CODEPULSE_PROGRESS.md` rather than silently expanding scope.

## 13. Agent Handoff Rule

If another agent worked on CodePulse previously, reconstruct the actual state from: Git, files, tests, database, migrations, roadmap, and the progress document.

Do not trust previous summaries without verification.
