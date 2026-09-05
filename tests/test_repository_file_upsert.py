"""
Phase 0.3 — Repository/File upsert with content_hash.

These tests exercise `IndexingEngine.index_directory()` end-to-end against
temporary, disposable SQLite databases (never the production codepulse.db)
to verify:

  - Repository rows are upserted by natural key `url` (no duplicates).
  - File rows are upserted by natural key `(repo_id, path)`, using
    content_hash for change detection (no duplicates; changed content
    updates the existing row in place; unchanged content reuses it as-is).
  - The same relative path in two different repositories stays isolated.
  - The natural-key invariants are enforced at the database level, not only
    in application logic.

Dependency (call graph edge) rows are intentionally NOT covered for
idempotency here -- that remains non-idempotent until Phase 3 (call-graph
confidence scoring), per the roadmap's scope. Symbol upsert is covered by
tests/test_symbol_upsert.py as of Phase 0.4.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy.exc import IntegrityError

from codepulse.config import reset_settings
from codepulse.indexer.engine import IndexingEngine
from codepulse.models import compute_content_hash
from codepulse.storage.db import FileModel, RepositoryModel, StorageManager


@pytest.fixture(autouse=True)
def cleanup_settings():
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def temp_db_url(tmp_path):
    db_path = tmp_path / "test_upsert.db"
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


# --- 1 & 2. New repository / new files are inserted ------------------------

def test_new_repository_and_files_are_inserted(tmp_path, temp_db_url):
    repo_path = _make_repo_dir(
        tmp_path,
        "repo_a",
        {
            "main.py": "def hello():\n    return 'hi'\n",
            "utils.py": "def add(a, b):\n    return a + b\n",
        },
    )

    engine = _new_engine(temp_db_url)
    engine.index_directory(repo_path, repo_name="repo_a", repo_url=repo_path)

    session = engine.storage.get_session()
    try:
        repos = session.query(RepositoryModel).all()
        assert len(repos) == 1
        assert repos[0].url == repo_path

        files = session.query(FileModel).filter(FileModel.repo_id == repos[0].id).all()
        assert len(files) == 2
        for f in files:
            assert f.content_hash  # a real, non-empty hash was computed
    finally:
        session.close()


# --- 3. Existing repository is reused, never duplicated --------------------

def test_reindexing_reuses_existing_repository_row(tmp_path, temp_db_url):
    repo_path = _make_repo_dir(tmp_path, "repo_b", {"main.py": "def f():\n    return 1\n"})

    engine = _new_engine(temp_db_url)
    engine.index_directory(repo_path, repo_name="repo_b", repo_url=repo_path)
    engine.index_directory(repo_path, repo_name="repo_b", repo_url=repo_path)

    session = engine.storage.get_session()
    try:
        repos = session.query(RepositoryModel).all()
        assert len(repos) == 1
    finally:
        session.close()


# --- 4 & 5. Existing file reused; unchanged content -> no duplicate row ----

def test_reindexing_unchanged_content_reuses_file_row(tmp_path, temp_db_url):
    repo_path = _make_repo_dir(tmp_path, "repo_c", {"main.py": "def f():\n    return 1\n"})

    engine = _new_engine(temp_db_url)
    engine.index_directory(repo_path, repo_name="repo_c", repo_url=repo_path)

    session = engine.storage.get_session()
    try:
        files_before = session.query(FileModel).all()
        assert len(files_before) == 1
        file_id_before = files_before[0].id
        hash_before = files_before[0].content_hash
    finally:
        session.close()

    # Re-index the same, unmodified repository.
    engine.index_directory(repo_path, repo_name="repo_c", repo_url=repo_path)

    session = engine.storage.get_session()
    try:
        files_after = session.query(FileModel).all()
        assert len(files_after) == 1, "unchanged content must not create another File row"
        assert files_after[0].id == file_id_before, "the existing row must be reused"
        assert files_after[0].content_hash == hash_before
    finally:
        session.close()


# --- 6. Changed content updates the existing row, doesn't duplicate it -----

def test_changed_file_content_updates_existing_row(tmp_path, temp_db_url):
    repo_dir = tmp_path / "repo_d"
    repo_dir.mkdir()
    file_path = repo_dir / "main.py"
    file_path.write_text("def f():\n    return 1\n", encoding="utf-8")
    repo_path = str(repo_dir)

    engine = _new_engine(temp_db_url)
    engine.index_directory(repo_path, repo_name="repo_d", repo_url=repo_path)

    session = engine.storage.get_session()
    try:
        files_before = session.query(FileModel).all()
        assert len(files_before) == 1
        file_id_before = files_before[0].id
        hash_before = files_before[0].content_hash
    finally:
        session.close()

    new_content = "def f():\n    return 2\n"
    file_path.write_text(new_content, encoding="utf-8")
    engine.index_directory(repo_path, repo_name="repo_d", repo_url=repo_path)

    session = engine.storage.get_session()
    try:
        files_after = session.query(FileModel).all()
        assert len(files_after) == 1, "changed content must update in place, not insert a new row"
        assert files_after[0].id == file_id_before
        assert files_after[0].content_hash != hash_before
        assert files_after[0].content_hash == compute_content_hash(new_content)
    finally:
        session.close()


# --- 7. Same relative path in two different repos stays isolated -----------

def test_same_relative_path_in_two_repos_is_isolated(tmp_path, temp_db_url):
    repo1 = _make_repo_dir(tmp_path, "repo_e1", {"app.py": "def one():\n    return 1\n"})
    repo2 = _make_repo_dir(tmp_path, "repo_e2", {"app.py": "def two():\n    return 2\n"})

    engine = _new_engine(temp_db_url)
    engine.index_directory(repo1, repo_name="repo_e1", repo_url=repo1)
    engine.index_directory(repo2, repo_name="repo_e2", repo_url=repo2)

    session = engine.storage.get_session()
    try:
        repos = session.query(RepositoryModel).all()
        assert len(repos) == 2

        files = session.query(FileModel).filter(FileModel.path == "app.py").all()
        assert len(files) == 2, "each repository must have its own File row for the same path"
        repo_ids = {f.repo_id for f in files}
        assert len(repo_ids) == 2
    finally:
        session.close()


# --- 8. Database-level uniqueness prevents duplicate natural keys ----------

def test_db_rejects_duplicate_repo_id_path(temp_db_url):
    storage = StorageManager(db_url=temp_db_url)
    session = storage.get_session()
    try:
        repo = RepositoryModel(name="r", url="file:///x")
        session.add(repo)
        session.flush()

        session.add(FileModel(repo_id=repo.id, path="a.py", language="python"))
        session.commit()

        session.add(FileModel(repo_id=repo.id, path="a.py", language="python"))
        with pytest.raises(IntegrityError):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_db_rejects_duplicate_repository_url(temp_db_url):
    storage = StorageManager(db_url=temp_db_url)
    session = storage.get_session()
    try:
        session.add(RepositoryModel(name="r1", url="file:///same"))
        session.commit()

        session.add(RepositoryModel(name="r2", url="file:///same"))
        with pytest.raises(IntegrityError):
            session.commit()
    finally:
        session.rollback()
        session.close()


# --- content_hash correctness -----------------------------------------------

def test_content_hash_matches_shared_convention(tmp_path, temp_db_url):
    content = "def f():\n    return 42\n"
    repo_path = _make_repo_dir(tmp_path, "repo_f", {"main.py": content})

    engine = _new_engine(temp_db_url)
    engine.index_directory(repo_path, repo_name="repo_f", repo_url=repo_path)

    session = engine.storage.get_session()
    try:
        f = session.query(FileModel).filter(FileModel.path == "main.py").first()
        assert f is not None
        assert f.content_hash == compute_content_hash(content)
    finally:
        session.close()


# --- symbols are now upserted too, as of Phase 0.4 -------------------------

def test_symbols_are_deduplicated_on_reindex_phase_0_4(tmp_path, temp_db_url):
    """
    Updated for Phase 0.4 (Symbol upsert by (file_id, name, start_line)):
    re-indexing an unchanged repository must no longer duplicate Symbol
    rows either. See tests/test_symbol_upsert.py for the full, focused
    Phase 0.4 test coverage; this test stays here as the same end-to-end
    regression check its Phase 0.3 predecessor was, now updated to match
    the roadmap-approved behavior change Phase 0.4 introduced.
    """
    from codepulse.storage.db import SymbolModel

    repo_path = _make_repo_dir(tmp_path, "repo_g", {"main.py": "def f():\n    return 1\n"})

    engine = _new_engine(temp_db_url)
    engine.index_directory(repo_path, repo_name="repo_g", repo_url=repo_path)
    engine.index_directory(repo_path, repo_name="repo_g", repo_url=repo_path)

    session = engine.storage.get_session()
    try:
        symbols = session.query(SymbolModel).filter(SymbolModel.name == "f").all()
        assert len(symbols) == 1, "unchanged symbol must not be duplicated on re-index"
    finally:
        session.close()
