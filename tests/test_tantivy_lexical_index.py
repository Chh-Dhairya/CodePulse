"""
Phase 2.1 — Tantivy-backed embedded lexical index (roadmap §36/§37 item 2.1;
§45 backlog B12: "Tantivy-backed lexical index... `search/bm25.py`
replacement... Benchmarked latency at 10K/100K-file tiers documented" —
note: the *benchmark* half of B12's DoD is explicitly Phase 2.5's job, not
this milestone's; see CODEPULSE_PROGRESS.md).

Two groups of tests:

  1. UNIT tests against `BM25Index` directly (no `IndexingEngine`), mirroring
     `tests/test_bm25_vector.py`'s own style -- proving the class's external
     contract (constructor, `.doc_ids`, `.num_docs`, `add_symbol`,
     `add_document`, `build`, `search`'s return shape) is preserved exactly
     across the Tantivy-backed swap, per the Phase 2.1 architecture gate's
     Section 3 ("ranking contract").

  2. INTEGRATION tests through the real `IndexingEngine`, proving the new
     repository-replacement (delete-by-repo-then-add) idempotency semantics
     required by this milestone -- the exact CASE A-F scenarios from the
     Phase 2.1 implementation authorization -- without duplicating
     `tests/test_repository_scoped_search.py`'s own existing isolation
     coverage (overlapping-symbol adversarial scoping, unscoped search,
     nonexistent-repository handling, determinism) or
     `tests/test_restart_idempotency.py`'s existing restart-contract
     coverage, both of which already pass unmodified against the new
     adapter (verified separately).

All integration tests use temporary, disposable SQLite databases and
temporary repository directories under `tmp_path` -- never the production
codepulse.db.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from codepulse.config import reset_settings
from codepulse.embeddings.embedder import CodeEmbedder
from codepulse.indexer.engine import IndexingEngine
from codepulse.models import Symbol, SymbolType
from codepulse.search.bm25 import BM25Index
from codepulse.storage.db import StorageManager


@pytest.fixture(autouse=True)
def cleanup_settings():
    reset_settings()
    yield
    reset_settings()


# =============================================================================
# UNIT TESTS -- BM25Index direct contract
# =============================================================================

def _sym(name: str, file_path: str, docstring: str, body: str = "") -> Symbol:
    return Symbol(
        name=name,
        type=SymbolType.FUNCTION,
        file_path=file_path,
        start_line=1,
        end_line=10,
        signature=f"def {name}():",
        docstring=docstring,
        body=body,
        language="python",
    )


def test_empty_index_search_returns_empty_list():
    index = BM25Index()
    assert index.search("anything") == []


def test_single_document_search_matches():
    index = BM25Index()
    index.add_symbol("sym1", _sym("greet", "a.py", "Return a friendly greeting for the given name."))
    index.build()

    results = index.search("friendly greeting")
    assert len(results) == 1
    assert results[0]["doc_id"] == "sym1"


def test_multiple_documents_reproduce_existing_bm25_fixture_ordering():
    """
    Direct regression proof, using the EXACT fixture and assertions from the
    pre-Tantivy `tests/test_bm25_vector.py::test_bm25_search` -- proves the
    Tantivy-backed engine still ranks these two adversarial-ish documents
    identically to before the swap, even though the underlying scoring
    arithmetic is now genuinely different (Phase 2.1 architecture gate,
    Section 3.A: candidate-generation numerics are allowed to change; B/C:
    which document wins for a clearly-relevant query must not).
    """
    index = BM25Index()

    sym1 = Symbol(
        name="create_db_connection",
        type=SymbolType.FUNCTION,
        file_path="db/connection.py",
        start_line=1,
        end_line=10,
        signature="def create_db_connection(host, port):",
        docstring="Creates a PostgreSQL database connection with pooling.",
        body="return psycopg2.pool.SimpleConnectionPool(minconn=1, maxconn=10)",
        language="python",
    )
    sym2 = Symbol(
        name="authenticate_user",
        type=SymbolType.FUNCTION,
        file_path="auth/jwt.py",
        start_line=1,
        end_line=15,
        signature="def authenticate_user(username, password):",
        docstring="Validates user credentials against password hash.",
        body="return verify_password(password, user.hash)",
        language="python",
    )

    index.add_symbol("sym1", sym1)
    index.add_symbol("sym2", sym2)
    index.build()

    results = index.search("database connection pooling")
    assert len(results) > 0
    assert results[0]["doc_id"] == "sym1"
    assert results[0]["normalized_score"] > 0.0

    results_auth = index.search("user password verification")
    assert len(results_auth) > 0
    assert results_auth[0]["doc_id"] == "sym2"


def test_raw_score_is_a_real_float():
    index = BM25Index()
    index.add_symbol("sym1", _sym("greet", "a.py", "Return a friendly greeting."))
    index.build()

    results = index.search("friendly greeting")
    assert isinstance(results[0]["raw_score"], float)
    assert results[0]["raw_score"] > 0.0


def test_normalized_score_is_a_float_in_zero_one_range():
    index = BM25Index()
    index.add_symbol("sym1", _sym("greet", "a.py", "Return a friendly greeting for a name."))
    index.add_symbol("sym2", _sym("farewell", "b.py", "Return a friendly farewell message."))
    index.build()

    results = index.search("friendly greeting farewell")
    assert len(results) >= 1
    for r in results:
        assert isinstance(r["normalized_score"], float)
        assert 0.0 <= r["normalized_score"] <= 1.0
    # The top result of its own result set is always fully normalized.
    assert results[0]["normalized_score"] == 1.0


def test_metadata_round_trips_every_required_key():
    """
    HybridRanker/SearchResultItem read exactly these metadata keys
    (Phase 2.1 architecture gate, Section 3.B) -- every one must survive
    the add -> Tantivy JSON field -> search round trip unchanged.
    """
    index = BM25Index()
    sym = _sym("greet", "a.py", "Return a friendly greeting for a name.", body="return f'hi {name}'")
    index.add_symbol("sym1", sym, repo_id=42)
    index.build()

    results = index.search("friendly greeting")
    assert len(results) == 1
    meta = results[0]["metadata"]
    assert meta["symbol_name"] == "greet"
    assert meta["file_path"] == "a.py"
    assert meta["type"] == "function"
    assert meta["start_line"] == 1
    assert meta["end_line"] == 10
    assert meta["signature"] == "def greet():"
    assert meta["docstring"] == "Return a friendly greeting for a name."
    assert meta["language"] == "python"
    assert meta["repo_id"] == 42


def test_metadata_repo_id_is_none_when_not_given():
    """Matches the pre-Tantivy dict-based contract exactly: an
    unscoped/direct caller that never passes `repo_id` gets back `None`,
    not a missing key or a fabricated default."""
    index = BM25Index()
    index.add_symbol("sym1", _sym("greet", "a.py", "Return a friendly greeting."))
    index.build()

    results = index.search("friendly greeting")
    assert results[0]["metadata"]["repo_id"] is None


def test_doc_id_round_trips_exactly():
    index = BM25Index()
    index.add_symbol("42::pkg/mod.py::do_thing", _sym("do_thing", "pkg/mod.py", "Does the thing reliably."))
    index.build()

    results = index.search("does the thing")
    assert results[0]["doc_id"] == "42::pkg/mod.py::do_thing"


def test_deterministic_repeated_query_results():
    index = BM25Index()
    index.add_symbol("sym1", _sym("greet", "a.py", "Return a friendly greeting for a name."))
    index.add_symbol("sym2", _sym("farewell", "b.py", "Return a friendly farewell message."))
    index.build()

    run1 = index.search("friendly message")
    run2 = index.search("friendly message")
    assert [r["doc_id"] for r in run1] == [r["doc_id"] for r in run2]
    assert [r["raw_score"] for r in run1] == [r["raw_score"] for r in run2]
    assert [r["normalized_score"] for r in run1] == [r["normalized_score"] for r in run2]


def test_top_k_truncates_result_count():
    index = BM25Index()
    for i in range(10):
        index.add_symbol(f"sym{i}", _sym(f"func{i}", f"f{i}.py", "A common shared searchable phrase."))
    index.build()

    all_results = index.search("common shared searchable phrase", top_k=100)
    assert len(all_results) == 10

    limited = index.search("common shared searchable phrase", top_k=3)
    assert len(limited) == 3
    # The top 3 of the truncated call match the top 3 of the untruncated one.
    assert [r["doc_id"] for r in limited] == [r["doc_id"] for r in all_results[:3]]


def test_no_non_matching_documents_appear_in_results():
    """Only in this milestone's contract: a document sharing zero query
    tokens with the query must never appear in `search()`'s output --
    preserved from the pre-Tantivy implementation's `score > 0` filter,
    naturally guaranteed here by Tantivy's own boolean-OR match semantics
    (a non-matching document is never a search hit at all)."""
    index = BM25Index()
    index.add_symbol("sym1", _sym("greet", "a.py", "Return a friendly greeting."))
    index.add_symbol("sym2", _sym("unrelated_thing", "b.py", "Completely different subject matter entirely."))
    index.build()

    results = index.search("friendly greeting")
    assert len(results) == 1
    assert results[0]["doc_id"] == "sym1"


def test_doc_ids_attribute_reflects_current_indexed_set():
    index = BM25Index()
    assert index.doc_ids == []
    index.add_symbol("sym1", _sym("greet", "a.py", "hi"))
    assert index.doc_ids == ["sym1"]
    assert index.num_docs == 1


def test_k1_and_b_attributes_are_settable_and_readable():
    index = BM25Index(k1=1.9, b=0.55)
    assert index.k1 == 1.9
    assert index.b == 0.55


def test_remove_repo_on_never_indexed_repo_is_a_safe_no_op():
    index = BM25Index()
    index.add_symbol("sym1", _sym("greet", "a.py", "hi"), repo_id=1)
    index.build()

    index.remove_repo(999)  # never indexed
    index.build()

    assert index.num_docs == 1
    assert index.doc_ids == ["sym1"]


# =============================================================================
# INTEGRATION TESTS -- repository-replacement idempotency through IndexingEngine
# =============================================================================

def _make_repo_dir(tmp_path, name: str, files: dict[str, str]) -> str:
    repo_dir = tmp_path / name
    repo_dir.mkdir()
    for rel_path, content in files.items():
        full = repo_dir / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
    return str(repo_dir)


@pytest.fixture(scope="module")
def shared_embedder():
    """One real CodeEmbedder instance reused across tests in this module,
    matching the established pattern in test_repository_scoped_search.py /
    test_restart_idempotency.py, so the underlying model loads once."""
    return CodeEmbedder()


def _new_engine(tmp_path, name, embedder) -> IndexingEngine:
    db_path = tmp_path / f"{name}.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"
    storage = StorageManager(db_url=db_url)
    return IndexingEngine(storage=storage, embedder=embedder)


REPO_A_V1 = {
    "main.py": (
        'def foo():\n    """Returns the unique alpha marker value."""\n    return "alpha"\n\n\n'
        'def bar():\n    """Returns the unique bravo marker value."""\n    return "bravo"\n'
    ),
}


# --- CASE A: initial index ---------------------------------------------------

def test_case_a_initial_index_search_returns_both_symbols(tmp_path, shared_embedder):
    engine = _new_engine(tmp_path, "case_a", shared_embedder)
    repo_path = _make_repo_dir(tmp_path, "repo_a", REPO_A_V1)
    result = engine.index_directory(repo_path, repo_name="repo_a", repo_url=repo_path)
    repo_id = result["repository_id"]

    foo_results = engine.search("unique alpha marker", limit=10, search_type="keyword", repository_id=repo_id)
    bar_results = engine.search("unique bravo marker", limit=10, search_type="keyword", repository_id=repo_id)
    assert any(r.symbol_name == "foo" for r in foo_results)
    assert any(r.symbol_name == "bar" for r in bar_results)


# --- CASE B: repeat unchanged indexing does not accumulate duplicates -------

def test_case_b_repeated_unchanged_indexing_does_not_duplicate_lexical_docs(tmp_path, shared_embedder):
    engine = _new_engine(tmp_path, "case_b", shared_embedder)
    repo_path = _make_repo_dir(tmp_path, "repo_b", REPO_A_V1)

    result1 = engine.index_directory(repo_path, repo_name="repo_b", repo_url=repo_path)
    repo_id = result1["repository_id"]
    docs_after_first = engine.bm25_index.num_docs

    for _ in range(4):
        engine.index_directory(repo_path, repo_name="repo_b", repo_url=repo_path)

    assert engine.bm25_index.num_docs == docs_after_first, (
        "re-indexing an unchanged repository must not accumulate duplicate lexical documents"
    )

    results = engine.search("unique alpha marker", limit=10, search_type="keyword", repository_id=repo_id)
    matching = [r for r in results if r.symbol_name == "foo"]
    assert len(matching) == 1, "exactly one (not five) lexical hits for the unchanged symbol"


# --- CASE C: changed symbol replaces its old lexical representation --------

def test_case_c_changed_symbol_replaces_old_lexical_content(tmp_path, shared_embedder):
    engine = _new_engine(tmp_path, "case_c", shared_embedder)
    repo_dir = tmp_path / "repo_c"
    repo_dir.mkdir()
    main_py = repo_dir / "main.py"
    main_py.write_text(
        'def foo():\n    """Returns the original unique zulu marker value."""\n    return 1\n',
        encoding="utf-8",
    )
    result = engine.index_directory(str(repo_dir), repo_name="repo_c", repo_url=str(repo_dir))
    repo_id = result["repository_id"]

    # Query on the word that is UNIQUE to the old docstring only (not shared
    # with the new docstring below) -- "original"/"unique"/"marker" are
    # deliberately reused in both versions and would still legitimately
    # match after the change, so they would not actually prove replacement.
    old_results = engine.search("zulu", limit=10, search_type="keyword", repository_id=repo_id)
    assert any(r.symbol_name == "foo" for r in old_results)

    # Change the docstring -- re-index.
    main_py.write_text(
        'def foo():\n    """Returns the brand new unique yankee marker value."""\n    return 1\n',
        encoding="utf-8",
    )
    engine.index_directory(str(repo_dir), repo_name="repo_c", repo_url=str(repo_dir))

    stale_results = engine.search("zulu", limit=10, search_type="keyword", repository_id=repo_id)
    assert not any(r.symbol_name == "foo" for r in stale_results), (
        "the OLD unique text ('zulu') must no longer be searchable after re-indexing"
    )

    fresh_results = engine.search("yankee", limit=10, search_type="keyword", repository_id=repo_id)
    assert any(r.symbol_name == "foo" for r in fresh_results), (
        "the NEW unique text ('yankee') must be searchable after re-indexing"
    )


# --- CASE D: removed file/symbol disappears from lexical search ------------

def test_case_d_removed_file_disappears_from_lexical_search(tmp_path, shared_embedder):
    engine = _new_engine(tmp_path, "case_d", shared_embedder)
    repo_dir = tmp_path / "repo_d"
    repo_dir.mkdir()
    (repo_dir / "keep.py").write_text(
        'def keeper():\n    """A symbol that stays present across re-indexing."""\n    return 1\n',
        encoding="utf-8",
    )
    doomed_file = repo_dir / "doomed.py"
    doomed_file.write_text(
        'def doomed_func():\n    """A uniquely-named quebec marker symbol slated for removal."""\n    return 1\n',
        encoding="utf-8",
    )
    result = engine.index_directory(str(repo_dir), repo_name="repo_d", repo_url=str(repo_dir))
    repo_id = result["repository_id"]

    before = engine.search("uniquely quebec marker", limit=10, search_type="keyword", repository_id=repo_id)
    assert any(r.symbol_name == "doomed_func" for r in before)

    doomed_file.unlink()
    engine.index_directory(str(repo_dir), repo_name="repo_d", repo_url=str(repo_dir))

    after = engine.search("uniquely quebec marker", limit=10, search_type="keyword", repository_id=repo_id)
    assert not any(r.symbol_name == "doomed_func" for r in after), (
        "a removed file's symbol must no longer be returned by lexical search"
    )
    still_there = engine.search("stays present across", limit=10, search_type="keyword", repository_id=repo_id)
    assert any(r.symbol_name == "keeper" for r in still_there), "an unrelated, still-present symbol must be unaffected"


# --- CASE E: repository A re-index does not damage repository B ------------

def test_case_e_reindexing_repo_a_leaves_repo_b_lexical_docs_intact(tmp_path, shared_embedder):
    engine = _new_engine(tmp_path, "case_e", shared_embedder)
    repo_a_path = _make_repo_dir(tmp_path, "repo_e_a", REPO_A_V1)
    repo_b_path = _make_repo_dir(
        tmp_path,
        "repo_e_b",
        {"main.py": 'def baz():\n    """Returns the unique charlie marker value for repo B."""\n    return "charlie"\n'},
    )

    result_a = engine.index_directory(repo_a_path, repo_name="repo_e_a", repo_url=repo_a_path)
    result_b = engine.index_directory(repo_b_path, repo_name="repo_e_b", repo_url=repo_b_path)
    repo_a_id = result_a["repository_id"]
    repo_b_id = result_b["repository_id"]

    docs_before = engine.bm25_index.num_docs

    # Re-index A several times.
    for _ in range(3):
        engine.index_directory(repo_a_path, repo_name="repo_e_a", repo_url=repo_a_path)

    assert engine.bm25_index.num_docs == docs_before, "repo A's re-indexing must not change the total doc count"

    b_results = engine.search("unique charlie marker", limit=10, search_type="keyword", repository_id=repo_b_id)
    assert any(r.symbol_name == "baz" for r in b_results), "repo B's lexical documents must survive repo A's re-indexing"
    for r in b_results:
        assert r.doc_id.startswith(f"{repo_b_id}::")

    a_results = engine.search("unique alpha marker", limit=10, search_type="keyword", repository_id=repo_a_id)
    assert any(r.symbol_name == "foo" for r in a_results)


# --- CASE F: empty repository must not delete another repository's docs ----

def test_case_f_indexing_an_empty_repository_does_not_delete_another_repos_docs(tmp_path, shared_embedder):
    engine = _new_engine(tmp_path, "case_f", shared_embedder)
    repo_a_path = _make_repo_dir(tmp_path, "repo_f_a", REPO_A_V1)
    result_a = engine.index_directory(repo_a_path, repo_name="repo_f_a", repo_url=repo_a_path)
    repo_a_id = result_a["repository_id"]
    docs_before = engine.bm25_index.num_docs
    assert docs_before > 0

    empty_repo_path = _make_repo_dir(tmp_path, "repo_f_empty", {})
    engine.index_directory(empty_repo_path, repo_name="repo_f_empty", repo_url=empty_repo_path)

    assert engine.bm25_index.num_docs == docs_before, (
        "indexing an empty repository must not remove another repository's lexical documents"
    )
    a_results = engine.search("unique alpha marker", limit=10, search_type="keyword", repository_id=repo_a_id)
    assert any(r.symbol_name == "foo" for r in a_results)


# =============================================================================
# FAILURE PROPAGATION -- a Tantivy-layer failure surfaces through the
# existing Phase 1.6 structured error architecture, not a hand-rolled shape.
# =============================================================================

def test_lexical_search_failure_propagates_through_existing_500_handling(isolated_app, monkeypatch):
    """
    Mirrors tests/test_error_format.py's own established pattern for
    proving an unhandled exception anywhere in the search path surfaces as
    the existing structured 500 envelope -- this test only proves
    PROPAGATION through the existing architecture (no hand-rolled response
    shape introduced by the lexical layer), not the envelope's own shape,
    which tests/test_error_format.py already owns verifying in full.
    """
    import codepulse.api.app as app_module
    from codepulse.storage.db import RepositoryModel
    from tests.conftest import make_api_key_headers

    client = TestClient(app_module.app, raise_server_exceptions=False)

    # /v1/search requires a repository-scoped request (Phase 1.2:
    # unscoped/global search can never be authorized) -- create a real
    # repository and a key scoped to it, exactly like the Phase 1.6 error
    # tests that need repository-specific requests already do.
    session = isolated_app.get_session()
    try:
        repo = RepositoryModel(name="r1", url="file:///r1")
        session.add(repo)
        session.commit()
        repo_id = repo.id
    finally:
        session.close()

    def _broken_search(*args, **kwargs):
        raise RuntimeError("a genuine Tantivy-layer failure, must never leak to the client")

    monkeypatch.setattr(app_module.indexing_engine.bm25_index, "search", _broken_search)

    headers = make_api_key_headers(isolated_app, repository_ids=[repo_id])
    response = client.post(
        "/v1/search",
        json={"query": "anything", "search_type": "keyword", "repository_id": repo_id},
        headers=headers,
    )

    assert response.status_code == 500
    assert "Tantivy-layer failure" not in response.text
