"""
Schema migrations: the Alembic plumbing, plus the safe `codepulse migrate` logic
(CODEPULSE_FUTURE_CHANGES.md items 8 and 17 and finding K; user-approved 2026-10-14).

The migration scripts live INSIDE the package (`codepulse/storage/schema_migrations/`)
so they ship in a wheel, and the Alembic config is built in code instead of read from a
repository-root `alembic.ini`, so none of this depends on the checkout or the current
directory. (`alembic.ini` still exists for developers running `alembic upgrade head`
from the repo root; it points at the same scripts.) Building the config without an
ini file also means a migration run no longer applies the ini's logging section, which
used to disable every logger that already existed and reset the root handlers.

A database can be in one of these states (`get_schema_status`):
  fresh      no tables yet (or no file): nothing to upgrade, `migrate` creates it at head
  current    tracked by Alembic and at head
  behind     tracked, at an older revision this CodePulse knows: `migrate` upgrades it
  unstamped  has tables but was never tracked (created by `create_all`, as every
             database CodePulse made before this change) AND its schema matches the
             models, apart from tables it simply does not have yet (a database made
             before a table existed, not opened by newer code since): `migrate`
             creates those empty tables and stamps it at head
  diverged   unstamped and its schema DIFFERS from the models in any other way (a
             missing or extra column, an extra table -- e.g. a legacy table that a later
             migration renames): stamping it would claim a schema it does not have, so
             `migrate` refuses and says what differs
  unknown    tracked, but at a revision this CodePulse does not have (a database from
             a newer CodePulse): `migrate` refuses

`migrate_database` never changes a database it refuses, takes a consistent backup of a
SQLite file (the sqlite3 backup API, safe with a writer present) before it alters one,
and verifies the result. Looking at a database (`get_schema_status`, `check=True`) never
creates or changes it.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Optional

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError

from codepulse.config import get_settings
from codepulse.storage.db import Base

SCRIPT_LOCATION = Path(__file__).resolve().parent / "schema_migrations"

# Structural differences that make an unstamped database unsafe to stamp. Index, constraint, type and
# default differences between `create_all` and the migrations are deliberately NOT included: they do
# not change what data the tables can hold.
_STRUCTURAL_DIFFS = ("add_table", "remove_table", "add_column", "remove_column")


class MigrationRefused(Exception):
    """`migrate` will not change this database (diverged or from a newer CodePulse); nothing was touched."""


def get_alembic_config(db_url: str | None = None) -> Config:
    """
    An Alembic `Config` built in code: the scripts are the packaged ones and no `alembic.ini` is needed.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(SCRIPT_LOCATION))
    # configparser treats % as interpolation; a percent-encoded URL would otherwise break.
    cfg.set_main_option("sqlalchemy.url", (db_url or get_settings().db_url).replace("%", "%%"))
    return cfg


@lru_cache(maxsize=1)
def head_revision() -> str:
    """The newest revision this CodePulse knows (computed once per process)."""
    head = ScriptDirectory.from_config(get_alembic_config("sqlite://")).get_current_head()
    assert head, "no migration revisions found"
    return head


def run_migrations(db_url: str | None = None, revision: str = "head") -> None:
    """Applies Alembic migrations up to the specified revision (default: head)."""
    command.upgrade(get_alembic_config(db_url=db_url), revision)


def stamp_database(db_url: str | None = None, revision: str = "head") -> None:
    """
    Stamps the database at the specified revision without running migration scripts.
    Only correct for a database whose schema already matches that revision;
    `migrate_database` checks that before it calls this.
    """
    command.stamp(get_alembic_config(db_url=db_url), revision)


def downgrade_migrations(db_url: str | None = None, revision: str = "base") -> None:
    """Downgrades Alembic migrations to the specified revision (default: base)."""
    command.downgrade(get_alembic_config(db_url=db_url), revision)


def get_current_revision(db_url: str | None = None) -> Optional[str]:
    """The database's alembic_version revision, or None if it is not tracked (or does not exist)."""
    target_url = db_url or get_settings().db_url
    path = _sqlite_file(target_url)
    if path is not None and not path.exists():
        return None
    engine = create_engine(target_url)
    try:
        if "alembic_version" not in inspect(engine).get_table_names():
            return None
        with engine.connect() as conn:
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()


def stamp_new_database(engine: Engine) -> None:
    """
    Records head as the revision of a database that `StorageManager` has JUST created from scratch with
    `create_all` (so its schema IS head). Before this, such a database was never tracked by Alembic and
    could not be upgraded later. Safe if two processes create the same new database at once.
    """
    head = head_revision()
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL, "
                "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
            ))
            if conn.execute(text("SELECT count(*) FROM alembic_version")).scalar() == 0:
                conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": head})
    except IntegrityError:
        pass  # the other process stamped it first


# --- status ------------------------------------------------------------------

@dataclass(frozen=True)
class SchemaStatus:
    state: str  # fresh | current | behind | unstamped | diverged | unknown
    current: Optional[str]
    head: str
    pending: list[str] = field(default_factory=list)  # revisions `migrate` would apply, oldest first
    differences: list[str] = field(default_factory=list)  # for diverged: what differs from the models
    missing_tables: list[str] = field(default_factory=list)  # for unstamped: tables `migrate` will create
    detail: str = ""

    @property
    def needs_action(self) -> bool:
        return self.state in ("behind", "unstamped", "diverged", "unknown")


def _sqlite_file(db_url: str) -> Optional[Path]:
    url = make_url(db_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        return Path(url.database)
    return None


def _pending(current: str) -> list[str]:
    script = ScriptDirectory.from_config(get_alembic_config("sqlite://"))
    revisions = [rev.revision for rev in script.iterate_revisions(head_revision(), current)]
    return [rev for rev in reversed(revisions) if rev != current]


def _structural_differences(engine: Engine) -> tuple[list[str], list[str]]:
    """
    `(differences, missing_tables)`: what in the database does NOT match the models, split into things that
    make it unsafe to stamp (`differences`) and tables that are merely absent (`missing_tables`). Creating an
    absent empty table cannot conflict with existing data, so those alone do not block.
    """
    with engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": False, "compare_server_default": False})
        diffs = compare_metadata(context, Base.metadata)
    out: list[str] = []
    missing: list[str] = []
    for diff in diffs:
        kind = diff[0]
        if kind == "add_table":
            missing.append(diff[1].name)
        elif kind == "remove_table":
            if diff[1].name != "alembic_version":
                out.append(f"table {diff[1].name} is not in the models")
        elif kind == "add_column":
            out.append(f"column {diff[2]}.{diff[3].name} is missing")
        elif kind == "remove_column":
            out.append(f"column {diff[2]}.{diff[3].name} is not in the models")
    return sorted(out), sorted(missing)


def get_schema_status(db_url: str | None = None) -> SchemaStatus:
    """Classifies the database (see the module docstring). Never creates or changes it."""
    target_url = db_url or get_settings().db_url
    head = head_revision()
    path = _sqlite_file(target_url)
    if path is not None and (not path.exists() or path.stat().st_size == 0):
        return SchemaStatus("fresh", None, head, detail="no database yet")

    engine = create_engine(target_url)
    try:
        tables = set(inspect(engine).get_table_names())
        user_tables = tables - {"alembic_version"}
        current = None
        if "alembic_version" in tables:
            with engine.connect() as conn:
                current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar() or None
        if not user_tables:
            return SchemaStatus("fresh", current, head, detail="no tables yet")
        if current is not None:
            if current == head:
                return SchemaStatus("current", current, head, detail=f"at {head}")
            known = {rev.revision for rev in ScriptDirectory.from_config(get_alembic_config("sqlite://")).walk_revisions()}
            if current not in known:
                return SchemaStatus(
                    "unknown", current, head,
                    detail=f"revision {current} is not known to this CodePulse (a database from a newer version?)",
                )
            pending = _pending(current)
            return SchemaStatus("behind", current, head, pending, detail=f"at {current}, {len(pending)} revision(s) behind {head}")
        differences, missing_tables = _structural_differences(engine)
        if differences:
            return SchemaStatus(
                "diverged", None, head, differences=differences,
                missing_tables=missing_tables,
                detail="not tracked by Alembic and its schema differs from the models: "
                + "; ".join(differences + [f"table {t} is missing" for t in missing_tables]),
            )
        if missing_tables:
            return SchemaStatus(
                "unstamped", None, head, missing_tables=missing_tables,
                detail=f"not tracked by Alembic; its schema matches the models except for missing table(s) {', '.join(missing_tables)}, which migrate will create",
            )
        return SchemaStatus("unstamped", None, head, detail="not tracked by Alembic, but its schema matches the models")
    finally:
        engine.dispose()


# --- migrate -----------------------------------------------------------------

@dataclass(frozen=True)
class MigrationResult:
    action: str  # created | up-to-date | upgraded | stamped | needs-upgrade | needs-stamp | blocked
    state: str
    from_revision: Optional[str]
    to_revision: Optional[str]
    backup_path: Optional[str] = None
    applied: list[str] = field(default_factory=list)
    detail: str = ""


def _backup_sqlite(path: Path, label: str) -> str:
    """A consistent copy of a live SQLite database (the sqlite3 backup API, safe with a writer present)."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(f"{path.name}.pre-{label}-{stamp}.bak")
    counter = 1
    while dest.exists():
        counter += 1
        dest = path.with_name(f"{path.name}.pre-{label}-{stamp}-{counter}.bak")
    source = sqlite3.connect(str(path))
    target = sqlite3.connect(str(dest))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return str(dest)


def _verify(db_url: str, backup_path: Optional[str]) -> None:
    if get_current_revision(db_url) != head_revision():
        raise RuntimeError(f"migration finished but the database is not at {head_revision()}" + _restore_hint(backup_path))
    path = _sqlite_file(db_url)
    if path is not None:
        conn = sqlite3.connect(str(path))
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
        if result != "ok":
            raise RuntimeError(f"integrity check failed after migrating: {result}" + _restore_hint(backup_path))


def _restore_hint(backup_path: Optional[str]) -> str:
    return f" (a backup of the previous state is at {backup_path})" if backup_path else ""


def migrate_database(db_url: str | None = None, *, backup: bool = True, check: bool = False) -> MigrationResult:
    """
    Brings the database to head, safely (see the module docstring for what each state does).

    `check=True` only reports what would happen and never changes anything. Otherwise a `diverged` or
    `unknown` database raises `MigrationRefused` and is left exactly as it was. A SQLite file is backed up
    (unless `backup=False`) before it is upgraded or stamped; a database that did not exist has nothing to
    back up.
    """
    target_url = db_url or get_settings().db_url
    status = get_schema_status(target_url)
    head = status.head
    path = _sqlite_file(target_url)

    if check:
        action = {
            "fresh": "up-to-date", "current": "up-to-date", "behind": "needs-upgrade",
            "unstamped": "needs-stamp", "diverged": "blocked", "unknown": "blocked",
        }[status.state]
        return MigrationResult(action, status.state, status.current, head, applied=list(status.pending), detail=status.detail)

    if status.state == "current":
        return MigrationResult("up-to-date", "current", status.current, head, detail=status.detail)
    if status.state in ("diverged", "unknown"):
        raise MigrationRefused(
            status.detail + ". Nothing was changed. "
            + ("Back the file up and rebuild it by re-indexing, or migrate it by hand." if status.state == "diverged"
               else "Upgrade CodePulse instead of migrating this database.")
        )

    backup_path: Optional[str] = None
    if status.state == "fresh":
        run_migrations(target_url)
        _verify(target_url, None)
        return MigrationResult("created", "fresh", None, head, applied=[head], detail="created the schema")

    if backup and path is not None:
        backup_path = _backup_sqlite(path, status.current or "unstamped")
    if status.state == "behind":
        run_migrations(target_url)
        _verify(target_url, backup_path)
        return MigrationResult("upgraded", "behind", status.current, head, backup_path, list(status.pending), f"upgraded {status.current} -> {head}")
    # unstamped and matching the models, apart from tables it does not have yet
    created: list[str] = []
    if status.missing_tables:
        engine = create_engine(target_url)
        try:
            Base.metadata.create_all(engine, tables=[Base.metadata.tables[name] for name in status.missing_tables])
        finally:
            engine.dispose()
        created = list(status.missing_tables)
    stamp_database(target_url)
    _verify(target_url, backup_path)
    detail = f"stamped at {head} (its schema matches the models)"
    if created:
        detail = f"created missing table(s) {', '.join(created)} and stamped at {head}"
    return MigrationResult("stamped", "unstamped", None, head, backup_path, [], detail)
