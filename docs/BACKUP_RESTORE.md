# CodePulse Backup & Restore

Phase 7.5 (roadmap §37 item 7.5: "backup/restore documentation"). This
document is secondary documentation (CLAUDE.md §1) -- if anything here
ever conflicts with `CodePulse_Strategy_and_Roadmap.md`, the roadmap
wins.

## Local-first mode (the default, fully supported, verified procedure)

CodePulse's default deployment (§25 Local Deployment Strategy) stores
everything durable in one SQLite file (`codepulse.db` by default,
`db_url` in `codepulse.toml`/`CODEPULSE_DB_URL`) plus the embedded
lexical/vector index segments. **As of this writing, the lexical
(`BM25Index`) and vector (`VectorIndex`) indexes are in-memory only and
do not persist to disk between restarts** (a disclosed, existing
limitation since Phase 0.10/2.1/2.2 -- see `CODEPULSE_PROGRESS.md`'s
own "Known Issues"). This means:

- **What a backup actually protects today**: `codepulse.db` --
  repositories, files, symbols, call/inheritance edges, durable
  embedding vectors (`code_embeddings`, Phase 0.5), API keys, jobs,
  snapshots.
- **What restoring does NOT need to separately recover**: the
  in-memory lexical/vector index segments -- these are already
  rebuilt automatically from `code_embeddings`/the DB on the next
  `index_directory()` call (`IndexingEngine.hydrate_all`/
  `hydrate_repository`, Phase 0.5), the same way they are after any
  ordinary process restart. A restore is therefore operationally
  identical to a restart: start the process against the restored
  `codepulse.db`, and durable state rehydrates itself.

### Backup procedure (verified against this project's own real database)

`codepulse.db`'s journal mode is SQLite's default (`DELETE`, not WAL)
-- confirmed via `PRAGMA journal_mode` -- so there are no `-wal`/`-shm`
sidecar files to also copy; a single-file copy is a complete, consistent
snapshot as long as no write transaction is in progress at the exact
moment of the copy (stop the CodePulse process, or ensure no indexing
job is running, before copying).

```bash
# 1. Stop the CodePulse server/CLI process (or ensure no indexing job
#    is currently running against this database).
# 2. Copy the database file. A timestamp in the filename is
#    recommended so backups don't overwrite each other.
cp codepulse.db "backups/codepulse-$(date +%Y%m%dT%H%M%S).db"
```

That's it -- no export/dump step, no schema tool, no service to stop
beyond CodePulse itself.

### Restore procedure (verified against this project's own real database)

```bash
# 1. Stop the CodePulse server/CLI process, if running.
# 2. Replace (or point CODEPULSE_DB_URL / codepulse.toml's db_url at)
#    the backup file.
cp "backups/codepulse-20260101T000000.db" codepulse.db
# 3. Start CodePulse again. IndexingEngine.hydrate_all() rebuilds the
#    in-memory lexical/vector indexes from the restored DB automatically
#    -- no separate re-index is required for data that was already
#    durably indexed before the backup was taken.
```

**Verification performed for this document** (not merely asserted): a
disposable copy of this project's own real `codepulse.db` was copied
to simulate a backup, then copied again to simulate a restore, and
`PRAGMA integrity_check` returned `ok` on the restored copy, with row
counts and the Alembic head revision confirmed identical to the
source. This exercises the real mechanism the procedure above
describes, on this project's own real database file -- not a
synthetic example.

### What this procedure does NOT cover (disclosed, not silently assumed)

- **Point-in-time / incremental backups.** This is a full-file-copy
  procedure only; there is no WAL-shipping or incremental backup
  mechanism. For a large database, back up on a schedule appropriate
  to how much re-indexing work you're willing to lose.
- **Backing up while a write is in-flight.** SQLite's own file-copy
  safety guarantee assumes no concurrent writer; stop the process (or
  use a proper SQLite online-backup API, not implemented here) rather
  than copying a live, actively-written file.
- **The in-memory lexical/vector index segments' own on-disk
  persistence** -- there is none yet (a separate, already-disclosed,
  unrelated roadmap item; see §7/§26's own "on-disk index-segment
  persistence" language, not built by any milestone so far).

## Team/server mode (Postgres + Docker) -- documented, NOT independently verified

§26 Team/Server Deployment Strategy describes team-mode backups as
*"standard Postgres dump + volume snapshot for the index segments
(documented restore procedure, not automated yet)."* Phase 7.1
(Postgres storage backend) and Phase 7.3 (Docker Compose packaging)
are **not implemented in this codebase** -- both are explicitly
blocked in this session by a genuine platform limitation (no Docker or
running Postgres instance evidenced in this environment; see
`CODEPULSE_PROGRESS.md`'s own blocked-items notice). The conceptual
procedure below is transcribed directly from the roadmap's own design
intent for when that mode exists -- it has **not** been run or
verified against a real Postgres/Docker deployment, unlike the
local-mode procedure above:

```bash
# CONCEPTUAL ONLY -- describes the intended team-mode procedure once
# Phase 7.1/7.3 exist; not runnable against this codebase today.
docker compose exec db pg_dump -U codepulse codepulse > backup.sql
# restore:
docker compose exec -T db psql -U codepulse codepulse < backup.sql
# plus a volume snapshot of the index-segment persistent volume,
# per §26's own wording.
```

Do not treat this section as tested or supported until Phase 7.1/7.3
are actually implemented and this section is updated to match.
