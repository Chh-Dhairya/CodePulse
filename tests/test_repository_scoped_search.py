"""
Phase 0.6 — Repository-scoped search (remove global singleton).

These tests exercise `IndexingEngine.index_directory()` / `.search()` end-to-
end against a temporary, disposable SQLite database (never the production
codepulse.db) to verify that repository isolation is a real, enforced
property of the search implementation itself -- not a caller convention.

Two repositories are indexed into the SAME `IndexingEngine` instance (the
same shared `bm25_index`/`vector_index`/`call_graphs` the roadmap calls the
"global singleton" problem), deliberately containing an OVERLAPPING symbol:
both define a `process_data` function at the same relative path
("main.py"), with near-identical docstrings that differ only in which
repository they belong to. This is the adversarial case that would leak or
collide if repository scoping weren't real: a naive doc_id scheme keyed only
on (file_path, symbol_name) would conflate the two, and an unscoped/porous
filter would return the wrong repository's `process_data` for a scoped query.

All assertions check actual identity (doc_id's repo_id prefix, and the
distinguishing docstring content), never result counts alone.
"""
from __future__ import annotations

import os

import pytest

from codepulse.config import reset_settings
from codepulse.embeddings.embedder import CodeEmbedder
from codepulse.indexer.engine import IndexingEngine
from codepulse.storage.db import StorageManager


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


def _make_repo_dir(tmp_path, name: str, files: dict[str, str]) -> str:
    repo_dir = tmp_path / name
    repo_dir.mkdir()
    for rel_path, content in files.items():
        full = repo_dir / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
    return str(repo_dir)


REPO_A_FILES = {
    "main.py": (
        'def process_data(x):\n'
        '    """Process incoming data records for REPO_A specifically."""\n'
        '    return x + 1\n'
    ),
    "utils.py": (
        'def helper_a():\n'
        '    """A helper function that only exists in repository A."""\n'
        '    return "a"\n'
    ),
}

REPO_B_FILES = {
    "main.py": (
        'def process_data(x):\n'
        '    """Process incoming data records for REPO_B specifically."""\n'
        '    return x - 1\n'
    ),
}


@pytest.fixture
def two_repo_engine(tmp_path, shared_embedder):
    """
    Indexes REPO_A and REPO_B (deliberately overlapping: same relative
    path, same symbol name) into one shared IndexingEngine/database, and
    returns (engine, repo_a_id, repo_b_id).
    """
    db_path = tmp_path / "scoped_search.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"
    storage = StorageManager(db_url=db_url)
    engine = IndexingEngine(storage=storage, embedder=shared_embedder)

    repo_a_path = _make_repo_dir(tmp_path, "repo_a", REPO_A_FILES)
    repo_b_path = _make_repo_dir(tmp_path, "repo_b", REPO_B_FILES)

    result_a = engine.index_directory(repo_a_path, repo_name="repo_a", repo_url=repo_a_path)
    result_b = engine.index_directory(repo_b_path, repo_name="repo_b", repo_url=repo_b_path)

    return engine, result_a["repository_id"], result_b["repository_id"]


def _assert_belongs_to(result, repo_id: int):
    """Verifies actual identity: the result's doc_id is repo-qualified with
    exactly this repo_id (not merely that some result was returned)."""
    assert result.doc_id.startswith(f"{repo_id}::"), (
        f"expected doc_id to be prefixed with repo {repo_id}, got {result.doc_id!r}"
    )


# --- 1/2/5. Lexical (keyword) search respects repository scope -------------

def test_lexical_search_scoped_to_repo_a_returns_only_repo_a(two_repo_engine):
    engine, repo_a_id, repo_b_id = two_repo_engine

    results = engine.search(
        "process incoming data records", limit=10, search_type="keyword", repository_id=repo_a_id
    )

    assert len(results) >= 1
    for r in results:
        _assert_belongs_to(r, repo_a_id)
        assert "REPO_B" not in (r.docstring or ""), "must never return repo B's content"
    assert any("REPO_A" in (r.docstring or "") for r in results)


def test_lexical_search_scoped_to_repo_b_returns_only_repo_b(two_repo_engine):
    engine, repo_a_id, repo_b_id = two_repo_engine

    results = engine.search(
        "process incoming data records", limit=10, search_type="keyword", repository_id=repo_b_id
    )

    assert len(results) >= 1
    for r in results:
        _assert_belongs_to(r, repo_b_id)
        assert "REPO_A" not in (r.docstring or ""), "must never return repo A's content"
    assert any("REPO_B" in (r.docstring or "") for r in results)


# --- 6. Semantic search respects repository scope ---------------------------

def test_semantic_search_scoped_returns_only_that_repo(two_repo_engine):
    engine, repo_a_id, repo_b_id = two_repo_engine

    results_a = engine.search(
        "process incoming data records", limit=10, search_type="semantic", repository_id=repo_a_id
    )
    assert len(results_a) >= 1
    for r in results_a:
        _assert_belongs_to(r, repo_a_id)

    results_b = engine.search(
        "process incoming data records", limit=10, search_type="semantic", repository_id=repo_b_id
    )
    assert len(results_b) >= 1
    for r in results_b:
        _assert_belongs_to(r, repo_b_id)


# --- 7. Hybrid search respects repository scope -----------------------------

def test_hybrid_search_scoped_returns_only_that_repo(two_repo_engine):
    engine, repo_a_id, repo_b_id = two_repo_engine

    results_a = engine.search(
        "process incoming data records", limit=10, search_type="hybrid", repository_id=repo_a_id
    )
    assert len(results_a) >= 1
    for r in results_a:
        _assert_belongs_to(r, repo_a_id)
        assert "REPO_B" not in (r.docstring or "")

    results_b = engine.search(
        "process incoming data records", limit=10, search_type="hybrid", repository_id=repo_b_id
    )
    assert len(results_b) >= 1
    for r in results_b:
        _assert_belongs_to(r, repo_b_id)
        assert "REPO_A" not in (r.docstring or "")


# --- 3/4. Unscoped behavior unchanged; scoping actually changes the result -

def test_unscoped_search_mixes_both_repositories_as_before(two_repo_engine):
    engine, repo_a_id, repo_b_id = two_repo_engine

    results = engine.search("process incoming data records", limit=10, search_type="hybrid")

    docstrings = " ".join(r.docstring or "" for r in results)
    assert "REPO_A" in docstrings
    assert "REPO_B" in docstrings, "unscoped search must still see both repositories, unchanged"


# --- Multiple files/symbols within the same repository ---------------------

def test_scoped_search_finds_symbol_from_a_different_file_in_same_repo(two_repo_engine):
    engine, repo_a_id, repo_b_id = two_repo_engine

    results = engine.search("helper function repository A", limit=10, search_type="hybrid", repository_id=repo_a_id)

    assert any(r.symbol_name == "helper_a" for r in results)
    for r in results:
        _assert_belongs_to(r, repo_a_id)


def test_no_leak_when_querying_for_content_unique_to_the_other_repo(two_repo_engine):
    """`helper_a` only exists in repo A -- scoping to repo B must never
    surface it, proving the filter excludes rather than merely reorders."""
    engine, repo_a_id, repo_b_id = two_repo_engine

    results = engine.search("helper function repository A", limit=10, search_type="hybrid", repository_id=repo_b_id)

    assert all(r.symbol_name != "helper_a" for r in results)
    for r in results:
        _assert_belongs_to(r, repo_b_id)


# --- Nonexistent repository / repository with no content -------------------

def test_nonexistent_repository_id_returns_no_results(two_repo_engine):
    engine, repo_a_id, repo_b_id = two_repo_engine
    bogus_id = max(repo_a_id, repo_b_id) + 999

    for search_type in ("keyword", "semantic", "hybrid"):
        results = engine.search("process incoming data records", limit=10, search_type=search_type, repository_id=bogus_id)
        assert results == [], f"{search_type} search for a nonexistent repository must return []"


def test_repository_with_no_indexed_content_returns_empty(tmp_path, shared_embedder):
    db_path = tmp_path / "empty_repo.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"
    storage = StorageManager(db_url=db_url)
    engine = IndexingEngine(storage=storage, embedder=shared_embedder)

    empty_repo_path = _make_repo_dir(tmp_path, "repo_empty", {})
    result = engine.index_directory(empty_repo_path, repo_name="repo_empty", repo_url=empty_repo_path)
    empty_repo_id = result["repository_id"]

    results = engine.search("anything at all", limit=10, search_type="hybrid", repository_id=empty_repo_id)
    assert results == []


# --- Empty query: follow the existing (pre-0.6) contract, don't invent one -

def test_empty_query_scoped_does_not_crash(two_repo_engine):
    engine, repo_a_id, repo_b_id = two_repo_engine

    for search_type in ("keyword", "semantic", "hybrid"):
        results = engine.search("", limit=10, search_type=search_type, repository_id=repo_a_id)
        assert isinstance(results, list)
        # Whatever the pre-existing empty-query contract returns, scoping
        # must not violate it: every returned item (if any) still belongs
        # to the requested repository.
        for r in results:
            _assert_belongs_to(r, repo_a_id)


# --- Determinism -------------------------------------------------------------

def test_scoped_search_is_deterministic_across_repeated_calls(two_repo_engine):
    engine, repo_a_id, repo_b_id = two_repo_engine

    run1 = engine.search("process incoming data records", limit=10, search_type="hybrid", repository_id=repo_a_id)
    run2 = engine.search("process incoming data records", limit=10, search_type="hybrid", repository_id=repo_a_id)

    assert [r.doc_id for r in run1] == [r.doc_id for r in run2]
    assert [r.final_score for r in run1] == [r.final_score for r in run2]
