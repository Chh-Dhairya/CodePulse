"""
Phase 0.4 — Symbol upsert by (file_id, name, start_line).

These tests exercise `IndexingEngine.index_directory()` end-to-end against
temporary, disposable SQLite databases (never the production codepulse.db)
to verify:

  - Symbol rows are upserted by natural key `(file_id, name, start_line)`
    (no duplicates on re-index).
  - `body_hash` drives change detection: unchanged symbols reuse their
    existing row untouched, changed symbols update the existing row in
    place, new symbols get a new row.
  - The same symbol name/line in two different files stays isolated
    (distinguished by file_id).
  - The natural-key invariant is enforced at the database level, not only
    in application logic.

Dependency (call graph edge) rows are intentionally NOT covered for
idempotency here -- that remains Phase 3 scope.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy.exc import IntegrityError

from codepulse.config import reset_settings
from codepulse.indexer.engine import IndexingEngine
from codepulse.storage.db import FileModel, RepositoryModel, StorageManager, SymbolModel


@pytest.fixture(autouse=True)
def cleanup_settings():
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def temp_db_url(tmp_path):
    db_path = tmp_path / "test_symbol_upsert.db"
    return f"sqlite:///{str(db_path).replace(os.sep, '/')}"


def _make_repo_dir(tmp_path, name: str, files: dict[str, str]) -> str:
    repo_dir = tmp_path / name
    repo_dir.mkdir()
    for rel_path, content in files.items():
        full = repo_dir / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
    return str(repo_dir)


def _new_engine(db_url: str) -> IndexingEngine:
    storage = StorageManager(db_url=db_url)
    return IndexingEngine(storage=storage)


# --- new symbols are inserted -----------------------------------------------

def test_new_symbols_are_inserted(tmp_path, temp_db_url):
    repo_path = _make_repo_dir(
        tmp_path,
        "repo_a",
        {"main.py": "def hello():\n    return 'hi'\n\n\ndef add(a, b):\n    return a + b\n"},
    )

    engine = _new_engine(temp_db_url)
    engine.index_directory(repo_path, repo_name="repo_a", repo_url=repo_path)

    session = engine.storage.get_session()
    try:
        symbols = session.query(SymbolModel).all()
        names = sorted(s.name for s in symbols)
        assert names == ["add", "hello"]
        for s in symbols:
            assert s.body_hash  # a real hash was recorded
    finally:
        session.close()


# --- unchanged symbol is reused, not duplicated -----------------------------

def test_reindexing_unchanged_symbol_reuses_row(tmp_path, temp_db_url):
    repo_path = _make_repo_dir(tmp_path, "repo_b", {"main.py": "def f():\n    return 1\n"})

    engine = _new_engine(temp_db_url)
    engine.index_directory(repo_path, repo_name="repo_b", repo_url=repo_path)

    session = engine.storage.get_session()
    try:
        before = session.query(SymbolModel).filter(SymbolModel.name == "f").all()
        assert len(before) == 1
        symbol_id_before = before[0].id
        body_hash_before = before[0].body_hash
    finally:
        session.close()

    # Re-index the same, unmodified repository.
    engine.index_directory(repo_path, repo_name="repo_b", repo_url=repo_path)

    session = engine.storage.get_session()
    try:
        after = session.query(SymbolModel).filter(SymbolModel.name == "f").all()
        assert len(after) == 1, "unchanged symbol must not create another Symbol row"
        assert after[0].id == symbol_id_before, "the existing row must be reused"
        assert after[0].body_hash == body_hash_before
    finally:
        session.close()


# --- changed symbol body updates the existing row, doesn't duplicate it ----

def test_changed_symbol_body_updates_existing_row(tmp_path, temp_db_url):
    repo_dir = tmp_path / "repo_c"
    repo_dir.mkdir()
    file_path = repo_dir / "main.py"
    file_path.write_text("def f():\n    return 1\n", encoding="utf-8")
    repo_path = str(repo_dir)

    engine = _new_engine(temp_db_url)
    engine.index_directory(repo_path, repo_name="repo_c", repo_url=repo_path)

    session = engine.storage.get_session()
    try:
        before = session.query(SymbolModel).filter(SymbolModel.name == "f").all()
        assert len(before) == 1
        symbol_id_before = before[0].id
        body_hash_before = before[0].body_hash
    finally:
        session.close()

    # Same function name, same start line, different body -- must update in
    # place rather than insert a second row for the (file_id, name,
    # start_line) key.
    file_path.write_text("def f():\n    return 2\n", encoding="utf-8")
    engine.index_directory(repo_path, repo_name="repo_c", repo_url=repo_path)

    session = engine.storage.get_session()
    try:
        after = session.query(SymbolModel).filter(SymbolModel.name == "f").all()
        assert len(after) == 1, "changed body must update in place, not insert a new row"
        assert after[0].id == symbol_id_before
        assert after[0].body_hash != body_hash_before
    finally:
        session.close()


# --- same symbol name/line in two different files stays isolated -----------

def test_same_name_in_two_files_is_isolated(tmp_path, temp_db_url):
    repo_path = _make_repo_dir(
        tmp_path,
        "repo_d",
        {
            "a.py": "def run():\n    return 'a'\n",
            "b.py": "def run():\n    return 'b'\n",
        },
    )

    engine = _new_engine(temp_db_url)
    engine.index_directory(repo_path, repo_name="repo_d", repo_url=repo_path)

    session = engine.storage.get_session()
    try:
        symbols = session.query(SymbolModel).filter(SymbolModel.name == "run").all()
        assert len(symbols) == 2, "each file must have its own Symbol row for the same name"
        file_ids = {s.file_id for s in symbols}
        assert len(file_ids) == 2
    finally:
        session.close()


# --- database-level uniqueness prevents duplicate natural keys -------------

def test_db_rejects_duplicate_file_id_name_start_line(temp_db_url):
    storage = StorageManager(db_url=temp_db_url)
    session = storage.get_session()
    try:
        repo = RepositoryModel(name="r", url="file:///x")
        session.add(repo)
        session.flush()

        f = FileModel(repo_id=repo.id, path="a.py", language="python")
        session.add(f)
        session.flush()

        session.add(
            SymbolModel(
                file_id=f.id, name="f", type="function", start_line=1, end_line=2, language="python"
            )
        )
        session.commit()

        session.add(
            SymbolModel(
                file_id=f.id, name="f", type="function", start_line=1, end_line=2, language="python"
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
    finally:
        session.rollback()
        session.close()


# --- re-indexing an entirely unchanged repo now also stops duplicating -----
# symbols end-to-end (repository + files + symbols all idempotent together).

def test_full_reindex_of_unchanged_repo_produces_zero_new_symbol_rows(tmp_path, temp_db_url):
    repo_path = _make_repo_dir(
        tmp_path,
        "repo_e",
        {
            "main.py": "def one():\n    return 1\n\n\ndef two():\n    return 2\n",
            "utils.py": "def helper():\n    return 'h'\n",
        },
    )

    engine = _new_engine(temp_db_url)
    engine.index_directory(repo_path, repo_name="repo_e", repo_url=repo_path)

    session = engine.storage.get_session()
    try:
        count_before = session.query(SymbolModel).count()
        assert count_before == 3
    finally:
        session.close()

    for _ in range(3):
        engine.index_directory(repo_path, repo_name="repo_e", repo_url=repo_path)

    session = engine.storage.get_session()
    try:
        count_after = session.query(SymbolModel).count()
        assert count_after == 3, "repeated re-indexing of unchanged content must not grow Symbol rows"
    finally:
        session.close()
