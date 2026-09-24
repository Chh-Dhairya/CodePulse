"""
Backend-hardening addition (not a roadmap sprint item) -- SQLite
`PRAGMA foreign_keys` enforcement (`codepulse/storage/db.py::StorageManager.
__init__`, via a `connect`-event listener scoped to that instance's own
`self.engine`).

Background: SQLite disables foreign-key enforcement PER CONNECTION by
default -- declaring `ForeignKey(..., ondelete="CASCADE")` on a column
does nothing on its own unless `PRAGMA foreign_keys=ON` is run on every
connection. This was a real, previously-disclosed gap (see
CODEPULSE_PROGRESS.md's "Known technical debt"): every `ON DELETE
CASCADE` in this schema was silently inert.

What this fixes, concretely: `RepositoryModel.files` (and transitively
`FileModel.symbols`/`SymbolModel.embeddings`) already cascaded correctly
via SQLAlchemy's own ORM-level `cascade="all, delete-orphan"` on those
`relationship()` declarations, independent of any DB-level pragma --
unaffected by this change either way. But `CallEdge.repo_id`,
`APIKeyRepositoryModel.api_key_id`/`repo_id`, `IndexJobModel.repo_id`,
and `RepositorySnapshotModel.repo_id` have ONLY the DB-level
`ondelete="CASCADE"` declared, with no equivalent ORM relationship
providing the same cascade -- these rows used to silently survive
(orphaned) a parent row's ORM-level deletion. This file proves the
pragma is actually active, that it actually rejects an invalid
reference, and that a real `ON DELETE CASCADE` (`CallEdge.repo_id`) now
correctly fires at the database level.

Alembic's own migration-execution connection (`alembic/env.py`'s
separate `engine_from_config(...)`) is deliberately NOT touched by this
change -- verified indirectly here by confirming the full migration
test suite (tests/test_migrations.py) still passes unmodified.

All tests use temporary, disposable SQLite databases -- never the
production codepulse.db.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from codepulse.storage.db import (
    CallEdge,
    FileModel,
    RepositoryModel,
    StorageManager,
    SymbolModel,
)


@pytest.fixture
def storage(tmp_path):
    db_path = tmp_path / "test_fk_enforcement.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"
    return StorageManager(db_url=db_url)


def test_pragma_foreign_keys_is_on_for_every_connection(storage):
    session = storage.get_session()
    try:
        value = session.execute(text("PRAGMA foreign_keys")).scalar()
        assert value == 1
    finally:
        session.close()


def test_inserting_a_file_with_a_nonexistent_repo_id_is_rejected(storage):
    session = storage.get_session()
    try:
        f = FileModel(repo_id=999999, path="orphan.py", language="python")
        session.add(f)
        with pytest.raises(IntegrityError):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_inserting_a_symbol_with_a_nonexistent_file_id_is_rejected(storage):
    session = storage.get_session()
    try:
        s = SymbolModel(
            file_id=999999, name="orphan_fn", type="function",
            start_line=1, end_line=2, language="python",
        )
        session.add(s)
        with pytest.raises(IntegrityError):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_deleting_a_repository_cascades_to_call_edges_via_the_database_itself(storage):
    """
    The exact, real, previously-inert gap this milestone closes:
    `CallEdge.repo_id` has `ondelete="CASCADE"` but no equivalent ORM
    `relationship()`-level cascade -- deleting a `RepositoryModel` row
    only ever cleaned up `FileModel`/`SymbolModel`/`EmbeddingModel` rows
    (via `RepositoryModel.files`'s ORM cascade), never `CallEdge` rows,
    which used to survive as silent orphans. With `PRAGMA foreign_keys`
    now enforced, SQLite's own `ON DELETE CASCADE` fires at the
    database level and cleans these up too -- proven here by deleting
    the repository via the raw ORM (no manual CallEdge cleanup at all)
    and confirming the CallEdge row is genuinely gone afterward.
    """
    session = storage.get_session()
    try:
        repo = RepositoryModel(name="r1", url="file:///r1")
        session.add(repo)
        session.commit()
        repo_id = repo.id

        edge = CallEdge(
            repo_id=repo_id, source_symbol_name="a", source_file="a.py",
            target_symbol_name="b", target_file="b.py",
        )
        session.add(edge)
        session.commit()
        edge_id = edge.id

        # Delete ONLY the repository -- no manual CallEdge cleanup.
        session.delete(repo)
        session.commit()

        remaining = session.query(CallEdge).filter(CallEdge.id == edge_id).first()
        assert remaining is None, (
            "CallEdge.repo_id's ON DELETE CASCADE must actually fire now that "
            "PRAGMA foreign_keys is enforced -- this row must be gone, not orphaned"
        )
    finally:
        session.close()


def test_deleting_a_file_still_cascades_to_symbols_via_the_existing_orm_relationship(storage):
    """
    Unaffected-by-this-change sanity check: `FileModel.symbols`' own
    `cascade="all, delete-orphan"` ORM relationship already correctly
    cascaded deletes at the SQLAlchemy level, independent of the SQLite
    pragma either way -- confirms enabling the pragma didn't somehow
    interfere with (or duplicate-fail) an already-working cascade path.
    """
    session = storage.get_session()
    try:
        repo = RepositoryModel(name="r2", url="file:///r2")
        session.add(repo)
        session.commit()

        f = FileModel(repo_id=repo.id, path="a.py", language="python")
        session.add(f)
        session.commit()

        s = SymbolModel(
            file_id=f.id, name="fn", type="function", start_line=1, end_line=2, language="python",
        )
        session.add(s)
        session.commit()
        symbol_id = s.id

        session.delete(f)
        session.commit()

        remaining = session.query(SymbolModel).filter(SymbolModel.id == symbol_id).first()
        assert remaining is None
    finally:
        session.close()


def test_alembic_migrations_are_unaffected_by_the_storagemanager_only_pragma(tmp_path):
    """
    Confirms Alembic's own, entirely separate migration-execution
    connection (alembic/env.py's own engine_from_config(...), never a
    StorageManager instance) is genuinely untouched by this change --
    a real upgrade/downgrade round trip against a fresh database, not
    merely an assertion about which code path the listener is attached to.
    """
    from codepulse.storage.migrations import downgrade_migrations, get_current_revision, run_migrations

    db_path = tmp_path / "fk_migration_check.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"

    run_migrations(db_url=db_url, revision="head")
    assert get_current_revision(db_url) is not None

    downgrade_migrations(db_url=db_url, revision="base")
    assert get_current_revision(db_url) is None
