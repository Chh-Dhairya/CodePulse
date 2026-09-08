"""
Phase 0.8 — .gitignore/.codepulseignore discovery and ignore-rule handling.

These tests exercise `IndexingEngine.index_directory()` end-to-end (not
just `RepositoryIgnoreRules` in isolation) against temporary, disposable
repositories and SQLite databases -- never the production codepulse.db --
to verify that files excluded by the applicable ignore rules never enter
the indexing pipeline at all: no FileModel row, no SymbolModel row, no
BM25/vector-index entry.

Semantics under test (see codepulse/indexer/ignore.py's module docstring
for the full rationale):
  - `.gitignore` uses standard gitignore pattern syntax, including
    negation.
  - `.codepulseignore` uses the same syntax, ADDITIVE to `.gitignore`
    ("beyond git's own" -- roadmap §16), evaluated independently.
  - Only repository-root ignore files are read (no nested/per-directory
    support).
  - A malformed ignore file is skipped (with a warning), not fatal.
"""
from __future__ import annotations

import os

import pytest

from codepulse.config import reset_settings
from codepulse.embeddings.embedder import CodeEmbedder
from codepulse.indexer.engine import IndexingEngine
from codepulse.indexer.ignore import RepositoryIgnoreRules
from codepulse.storage.db import FileModel, StorageManager, SymbolModel


@pytest.fixture(autouse=True)
def cleanup_settings():
    reset_settings()
    yield
    reset_settings()


@pytest.fixture(scope="module")
def shared_embedder():
    """One real CodeEmbedder instance reused across tests in this module,
    so the underlying SentenceTransformer model is loaded only once."""
    return CodeEmbedder()


def _write_repo(tmp_path, name: str, files: dict[str, str]) -> str:
    repo_dir = tmp_path / name
    repo_dir.mkdir()
    for rel_path, content in files.items():
        full = repo_dir / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
    return str(repo_dir)


def _new_engine(db_url: str, embedder: CodeEmbedder) -> IndexingEngine:
    storage = StorageManager(db_url=db_url)
    return IndexingEngine(storage=storage, embedder=embedder)


def _indexed_paths(engine: IndexingEngine, repo_id: int) -> set[str]:
    """Real, persisted file paths for this repository, read from the DB."""
    session = engine.storage.get_session()
    try:
        return {f.path for f in session.query(FileModel).filter(FileModel.repo_id == repo_id).all()}
    finally:
        session.close()


def _indexed_symbol_names(engine: IndexingEngine, repo_id: int) -> set[str]:
    session = engine.storage.get_session()
    try:
        return {
            s.name
            for s in session.query(SymbolModel)
            .join(FileModel, SymbolModel.file_id == FileModel.id)
            .filter(FileModel.repo_id == repo_id)
            .all()
        }
    finally:
        session.close()


PY_A = 'def func_kept():\n    """A function that should remain indexed."""\n    return 1\n'
PY_B = 'def func_ignored():\n    """A function that must never be indexed."""\n    return 2\n'


# --- 1. No ignore files: baseline ------------------------------------------

def test_no_ignore_files_indexes_everything(tmp_path, shared_embedder):
    repo_path = _write_repo(tmp_path, "repo1", {"main.py": PY_A, "utils.py": PY_B})
    db_url = f"sqlite:///{str(tmp_path / 't1.db').replace(os.sep, '/')}"
    engine = _new_engine(db_url, shared_embedder)

    result = engine.index_directory(repo_path, repo_name="repo1", repo_url=repo_path)
    repo_id = result["repository_id"]

    assert _indexed_paths(engine, repo_id) == {"main.py", "utils.py"}


# --- 2. .gitignore ignored file ---------------------------------------------

def test_gitignore_excludes_a_file(tmp_path, shared_embedder):
    repo_path = _write_repo(
        tmp_path,
        "repo2",
        {
            "main.py": PY_A,
            "secret.py": PY_B,
            ".gitignore": "secret.py\n",
        },
    )
    db_url = f"sqlite:///{str(tmp_path / 't2.db').replace(os.sep, '/')}"
    engine = _new_engine(db_url, shared_embedder)

    result = engine.index_directory(repo_path, repo_name="repo2", repo_url=repo_path)
    repo_id = result["repository_id"]

    assert _indexed_paths(engine, repo_id) == {"main.py"}
    assert "func_ignored" not in _indexed_symbol_names(engine, repo_id)


# --- 3. .gitignore ignored directory (with a nested file) ------------------

def test_gitignore_excludes_a_directory_and_its_contents(tmp_path, shared_embedder):
    repo_path = _write_repo(
        tmp_path,
        "repo3",
        {
            "main.py": PY_A,
            "build/generated.py": PY_B,
            "build/deep/nested.py": PY_B,
            ".gitignore": "build/\n",
        },
    )
    db_url = f"sqlite:///{str(tmp_path / 't3.db').replace(os.sep, '/')}"
    engine = _new_engine(db_url, shared_embedder)

    result = engine.index_directory(repo_path, repo_name="repo3", repo_url=repo_path)
    repo_id = result["repository_id"]

    assert _indexed_paths(engine, repo_id) == {"main.py"}


# --- 4/5. .codepulseignore ignored file and directory -----------------------

def test_codepulseignore_excludes_a_file_and_a_directory(tmp_path, shared_embedder):
    repo_path = _write_repo(
        tmp_path,
        "repo4",
        {
            "main.py": PY_A,
            "internal.py": PY_B,
            "vendor/third_party.py": PY_B,
            ".codepulseignore": "internal.py\nvendor/\n",
        },
    )
    db_url = f"sqlite:///{str(tmp_path / 't4.db').replace(os.sep, '/')}"
    engine = _new_engine(db_url, shared_embedder)

    result = engine.index_directory(repo_path, repo_name="repo4", repo_url=repo_path)
    repo_id = result["repository_id"]

    assert _indexed_paths(engine, repo_id) == {"main.py"}


# --- 6. Combined .gitignore + .codepulseignore: additive, not override -----

def test_gitignore_and_codepulseignore_combine_additively(tmp_path, shared_embedder):
    repo_path = _write_repo(
        tmp_path,
        "repo5",
        {
            "main.py": PY_A,
            "git_excluded.py": PY_B,
            "pulse_excluded.py": PY_B,
            ".gitignore": "git_excluded.py\n",
            ".codepulseignore": "pulse_excluded.py\n",
        },
    )
    db_url = f"sqlite:///{str(tmp_path / 't5.db').replace(os.sep, '/')}"
    engine = _new_engine(db_url, shared_embedder)

    result = engine.index_directory(repo_path, repo_name="repo5", repo_url=repo_path)
    repo_id = result["repository_id"]

    # Both files are excluded (union), even though each ignore file only
    # mentions one of them -- proving .codepulseignore is additive to
    # .gitignore rather than overriding/replacing it.
    assert _indexed_paths(engine, repo_id) == {"main.py"}


# --- 7/8. Repository-relative + nested-path pattern matching ---------------

def test_pattern_matches_nested_path_not_just_root(tmp_path, shared_embedder):
    repo_path = _write_repo(
        tmp_path,
        "repo6",
        {
            "main.py": PY_A,
            "pkg/sub/generated.py": PY_B,
            ".gitignore": "pkg/sub/generated.py\n",
        },
    )
    db_url = f"sqlite:///{str(tmp_path / 't6.db').replace(os.sep, '/')}"
    engine = _new_engine(db_url, shared_embedder)

    result = engine.index_directory(repo_path, repo_name="repo6", repo_url=repo_path)
    repo_id = result["repository_id"]

    assert _indexed_paths(engine, repo_id) == {"main.py"}


# --- 9. Negation pattern (standard gitignore syntax) ------------------------

def test_negation_pattern_re_includes_a_file(tmp_path, shared_embedder):
    repo_path = _write_repo(
        tmp_path,
        "repo7",
        {
            "main.py": PY_A,
            "debug.log.py": PY_B,  # matched by *.log.py below
            "keep.log.py": PY_A,   # re-included by the negation
            ".gitignore": "*.log.py\n!keep.log.py\n",
        },
    )
    db_url = f"sqlite:///{str(tmp_path / 't7.db').replace(os.sep, '/')}"
    engine = _new_engine(db_url, shared_embedder)

    result = engine.index_directory(repo_path, repo_name="repo7", repo_url=repo_path)
    repo_id = result["repository_id"]

    assert _indexed_paths(engine, repo_id) == {"main.py", "keep.log.py"}


# --- 10/11/12. Ignored files produce no DB rows and no search hits --------

def test_ignored_file_produces_no_symbols_and_no_search_hit(tmp_path, shared_embedder):
    repo_path = _write_repo(
        tmp_path,
        "repo8",
        {
            "main.py": PY_A,
            "secret.py": PY_B,
            ".gitignore": "secret.py\n",
        },
    )
    db_url = f"sqlite:///{str(tmp_path / 't8.db').replace(os.sep, '/')}"
    engine = _new_engine(db_url, shared_embedder)

    result = engine.index_directory(repo_path, repo_name="repo8", repo_url=repo_path)
    repo_id = result["repository_id"]

    # No FileModel row for the ignored file.
    assert "secret.py" not in _indexed_paths(engine, repo_id)
    # No SymbolModel row derived from it.
    assert "func_ignored" not in _indexed_symbol_names(engine, repo_id)
    # It never entered the in-memory search structures either.
    assert not any("secret.py" in doc_id for doc_id in engine.bm25_index.doc_ids)
    assert not any("secret.py" in doc_id for doc_id in engine.vector_index.doc_ids)

    results = engine.search(
        "function that must never be indexed", limit=10, search_type="hybrid", repository_id=repo_id
    )
    assert all(r.symbol_name != "func_ignored" for r in results)


# --- 13. Two repositories, different ignore rules, no shared state ---------

def test_two_repositories_have_independent_ignore_rules(tmp_path, shared_embedder):
    repo_a_path = _write_repo(
        tmp_path,
        "repo9a",
        {"shared_name.py": PY_A, "only_in_a.py": PY_A, ".gitignore": "only_in_a.py\n"},
    )
    repo_b_path = _write_repo(
        tmp_path,
        "repo9b",
        {"shared_name.py": PY_A, "only_in_a.py": PY_A},  # repo B has NO .gitignore at all
    )
    db_url = f"sqlite:///{str(tmp_path / 't9.db').replace(os.sep, '/')}"
    engine = _new_engine(db_url, shared_embedder)

    result_a = engine.index_directory(repo_a_path, repo_name="repo9a", repo_url=repo_a_path)
    result_b = engine.index_directory(repo_b_path, repo_name="repo9b", repo_url=repo_b_path)

    # Repo A's ignore rule excludes only_in_a.py...
    assert _indexed_paths(engine, result_a["repository_id"]) == {"shared_name.py"}
    # ...but repo B has no such rule, so its own copy of the same filename
    # is indexed -- proving no global/shared ignore state leaked between
    # repositories indexed by the same engine instance.
    assert _indexed_paths(engine, result_b["repository_id"]) == {"shared_name.py", "only_in_a.py"}


# --- 14. The ignore files themselves are never indexed ---------------------

def test_ignore_files_themselves_are_never_indexed(tmp_path, shared_embedder):
    repo_path = _write_repo(
        tmp_path,
        "repo10",
        {
            "main.py": PY_A,
            ".gitignore": "*.tmp\n",
            ".codepulseignore": "*.cache\n",
        },
    )
    db_url = f"sqlite:///{str(tmp_path / 't10.db').replace(os.sep, '/')}"
    engine = _new_engine(db_url, shared_embedder)

    result = engine.index_directory(repo_path, repo_name="repo10", repo_url=repo_path)
    repo_id = result["repository_id"]

    paths = _indexed_paths(engine, repo_id)
    assert paths == {"main.py"}
    assert ".gitignore" not in paths
    assert ".codepulseignore" not in paths


# --- 15. Malformed ignore file is skipped, not fatal ------------------------

def test_malformed_gitignore_does_not_crash_indexing(tmp_path, shared_embedder):
    repo_path = _write_repo(
        tmp_path,
        "repo11",
        {
            "main.py": PY_A,
            "other.py": PY_A,
            # "[z-a]" is an invalid regex character range once translated --
            # a genuinely unparseable gitwildmatch pattern (verified
            # directly against pathspec, not assumed).
            ".gitignore": "[z-a]\n",
        },
    )
    db_url = f"sqlite:///{str(tmp_path / 't11.db').replace(os.sep, '/')}"
    engine = _new_engine(db_url, shared_embedder)

    # Must not raise.
    result = engine.index_directory(repo_path, repo_name="repo11", repo_url=repo_path)
    repo_id = result["repository_id"]

    # The malformed file contributes no rules -- both real files are still
    # indexed rather than the whole run failing or over-excluding.
    assert _indexed_paths(engine, repo_id) == {"main.py", "other.py"}


# --- RepositoryIgnoreRules unit-level checks (in addition to the above) ---

def test_repository_ignore_rules_absent_files_means_nothing_ignored(tmp_path):
    repo_path = _write_repo(tmp_path, "repo12", {"main.py": PY_A})
    rules = RepositoryIgnoreRules(repo_path)
    assert rules.is_ignored("main.py") is False
    assert rules.is_ignored("anything/else.py") is False
