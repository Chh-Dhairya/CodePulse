"""
Phase 2.2 — Embedded ANN vector index (roadmap §36/§37 item 2.2; §45
backlog B13: "Embedded ANN vector index... `search/vector.py`
replacement... Same as above for semantic search" [B12's benchmark DoD --
Phase 2.5's job, not this milestone's]; §7 Storage Decision: "a local
vector index (e.g. `sqlite-vec` or a persisted HNSW structure via
`hnswlib`)").

UNIT tests against `VectorIndex` directly (no `IndexingEngine`), mirroring
`tests/test_bm25_vector.py`'s own style -- proving the class's external
contract (constructor, `.dimension`, `.doc_ids`, `.vectors`, `add`,
`add_batch`, `search`'s return shape) is preserved exactly across the
sqlite-vec-backed swap, per the same "ranking contract" discipline Phase
2.1 established for `BM25Index`.

Repository-isolation coverage (overlapping symbols across two repos,
unscoped vs. scoped semantic search, restart) is NOT duplicated here --
`tests/test_repository_scoped_search.py` and
`tests/test_restart_idempotency.py` already exercise that through the
real `IndexingEngine`, unmodified, and continue to pass against this
adapter (verified separately, see CODEPULSE_PROGRESS.md).

All tests use temporary, disposable state -- never the production
codepulse.db.
"""
from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from codepulse.search.vector import VectorIndex


# =============================================================================
# UNIT TESTS -- VectorIndex direct contract
# =============================================================================

def test_empty_index_search_returns_empty_list():
    index = VectorIndex(dimension=4)
    query = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    assert index.search(query) == []


def test_single_document_search_matches():
    index = VectorIndex(dimension=4)
    v = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    index.add("doc1", v, {"name": "doc1"})

    results = index.search(v)
    assert len(results) == 1
    assert results[0]["doc_id"] == "doc1"
    assert round(results[0]["similarity"], 4) == 1.0


def test_multiple_documents_reproduce_existing_fixture_ordering():
    """
    Direct regression proof, using the EXACT fixture and assertions from
    the pre-Phase-2.2 `tests/test_bm25_vector.py::test_vector_index_search`
    -- proves the sqlite-vec-backed engine still ranks these three vectors
    identically to before the swap, even though the underlying computation
    now runs inside a compiled SQLite extension instead of NumPy.
    """
    vec_index = VectorIndex(dimension=4)

    v1 = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    v2 = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
    v3 = np.array([0.7, 0.7, 0.0, 0.0], dtype=np.float32)

    vec_index.add("doc1", v1, {"name": "doc1"})
    vec_index.add("doc2", v2, {"name": "doc2"})
    vec_index.add("doc3", v3, {"name": "doc3"})

    query = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    results = vec_index.search(query, top_k=3)

    assert len(results) == 3
    assert results[0]["doc_id"] == "doc1"
    assert round(results[0]["similarity"], 2) == 1.0
    assert results[1]["doc_id"] == "doc3"


def test_similarity_relevance_and_distance_are_real_floats_in_range():
    index = VectorIndex(dimension=4)
    index.add("same", np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
    index.add("orthogonal", np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32))
    index.add("opposite", np.array([-1.0, 0.0, 0.0, 0.0], dtype=np.float32))

    results = index.search(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32), top_k=3)
    by_id = {r["doc_id"]: r for r in results}

    assert isinstance(by_id["same"]["similarity"], float)
    assert round(by_id["same"]["similarity"], 4) == 1.0
    assert round(by_id["same"]["relevance_score"], 4) == 1.0
    assert round(by_id["same"]["embedding_distance"], 4) == 0.0

    assert round(by_id["orthogonal"]["similarity"], 4) == 0.0
    assert round(by_id["orthogonal"]["relevance_score"], 4) == 0.5

    assert round(by_id["opposite"]["similarity"], 4) == -1.0
    assert round(by_id["opposite"]["relevance_score"], 4) == 0.0
    assert round(by_id["opposite"]["embedding_distance"], 4) == 2.0

    for r in results:
        assert 0.0 <= r["relevance_score"] <= 1.0
        assert -1.0 <= r["similarity"] <= 1.0
        assert 0.0 <= r["embedding_distance"] <= 2.0


def test_metadata_round_trips_exactly():
    index = VectorIndex(dimension=4)
    meta = {
        "repo_id": 7,
        "symbol_name": "greet",
        "file_path": "a.py",
        "chunk_type": "signature_doc",
        "start_line": 1,
        "end_line": 5,
        "snippet": "def greet(name): ...",
    }
    index.add("42::a.py::greet::0", np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32), meta)

    results = index.search(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
    assert results[0]["metadata"] == meta


def test_metadata_defaults_to_empty_dict_when_not_given():
    index = VectorIndex(dimension=4)
    index.add("doc1", np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
    results = index.search(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
    assert results[0]["metadata"] == {}


def test_doc_id_round_trips_exactly():
    index = VectorIndex(dimension=4)
    index.add("7::pkg/mod.py::do_thing::0", np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
    results = index.search(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
    assert results[0]["doc_id"] == "7::pkg/mod.py::do_thing::0"


def test_deterministic_repeated_query_results():
    index = VectorIndex(dimension=4)
    index.add("doc1", np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
    index.add("doc2", np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32))
    index.add("doc3", np.array([0.7, 0.7, 0.0, 0.0], dtype=np.float32))

    query = np.array([1.0, 0.3, 0.0, 0.0], dtype=np.float32)
    run1 = index.search(query, top_k=3)
    run2 = index.search(query, top_k=3)
    assert [r["doc_id"] for r in run1] == [r["doc_id"] for r in run2]
    assert [r["similarity"] for r in run1] == [r["similarity"] for r in run2]


def test_top_k_truncates_result_count():
    index = VectorIndex(dimension=4)
    for i in range(10):
        index.add(f"doc{i}", np.array([1.0, float(i) * 0.01, 0.0, 0.0], dtype=np.float32))

    all_results = index.search(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32), top_k=100)
    assert len(all_results) == 10

    limited = index.search(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32), top_k=3)
    assert len(limited) == 3
    assert [r["doc_id"] for r in limited] == [r["doc_id"] for r in all_results[:3]]


def test_vectors_attribute_reflects_real_shape():
    index = VectorIndex(dimension=4)
    assert index.vectors.shape == (0, 4)

    index.add("doc1", np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
    assert index.vectors.shape == (1, 4)

    index.add_batch(
        ["doc2", "doc3"],
        np.array([[0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0]], dtype=np.float32),
    )
    assert index.vectors.shape == (3, 4)
    assert index.vectors.dtype == np.float32


def test_doc_ids_and_dimension_attributes():
    index = VectorIndex(dimension=8)
    assert index.dimension == 8
    assert index.doc_ids == []
    index.add("doc1", np.zeros(8, dtype=np.float32) + 1.0)
    assert index.doc_ids == ["doc1"]


def test_add_batch_with_unnormalized_vectors_is_normalized_correctly():
    """A vector far outside unit length must still be normalized before
    indexing -- an exact match direction should still score similarity 1.0
    regardless of the original vector's magnitude."""
    index = VectorIndex(dimension=4)
    huge = np.array([100.0, 0.0, 0.0, 0.0], dtype=np.float32)
    index.add("doc1", huge)

    results = index.search(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
    assert round(results[0]["similarity"], 4) == 1.0
    # The stored vector itself is unit-length, not the original magnitude.
    assert round(float(np.linalg.norm(index.vectors[0])), 4) == 1.0


def test_all_zero_query_vector_does_not_crash():
    """A degenerate all-zero query (never produced by the real embedder --
    CLAUDE.md's no-pseudo-embedding invariant -- but defensively handled
    rather than left to crash)."""
    index = VectorIndex(dimension=4)
    index.add("doc1", np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))

    results = index.search(np.array([0.0, 0.0, 0.0, 0.0], dtype=np.float32))
    assert len(results) == 1
    assert results[0]["similarity"] == 0.0


def test_add_batch_empty_list_is_a_safe_no_op():
    index = VectorIndex(dimension=4)
    index.add_batch([], np.empty((0, 4), dtype=np.float32))
    assert index.doc_ids == []
    assert index.vectors.shape == (0, 4)


# =============================================================================
# DISCLOSED KNOWN LIMITATION -- explicitly documented, not silently hidden
# =============================================================================

def test_known_limitation_repeated_add_of_the_same_doc_id_accumulates_duplicates():
    """
    Documents, deliberately and explicitly, a REAL, PRE-EXISTING
    characteristic this milestone does NOT change: `VectorIndex` (both
    before and after the Phase 2.2 sqlite-vec swap) has no doc_id-based
    replace/upsert logic -- `add`/`add_batch` are pure-append operations.
    Unlike Phase 2.1's `BM25Index.remove_repo`, no repository-replacement
    idempotency was implemented for `VectorIndex` in this milestone (see
    codepulse/search/vector.py's module docstring and
    CODEPULSE_PROGRESS.md's Phase 2.2 section for the full disclosed
    reasoning). This test exists so that fact can never silently change
    (in either direction) without a test noticing -- mirroring this
    project's own established practice (e.g.
    test_restart_idempotency.py's explicit "does NOT survive restart"
    assertions) of asserting known characteristics explicitly rather than
    leaving them undocumented.
    """
    index = VectorIndex(dimension=4)
    v = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)

    index.add("42::a.py::foo", v, {"repo_id": 42})
    index.add("42::a.py::foo", v, {"repo_id": 42})  # same doc_id, re-"indexed"

    assert index.doc_ids.count("42::a.py::foo") == 2, (
        "this is CURRENT, DISCLOSED behavior, not a regression -- if this "
        "assertion starts failing because duplicates are now prevented, "
        "update this test and CODEPULSE_PROGRESS.md's Phase 2.2 section "
        "together, deliberately, not by accident"
    )
    results = index.search(v, top_k=10)
    assert sum(1 for r in results if r["doc_id"] == "42::a.py::foo") == 2


# =============================================================================
# FAILURE PROPAGATION -- a sqlite-vec-layer failure surfaces through the
# existing Phase 1.6 structured error architecture, not a hand-rolled shape.
# =============================================================================

def test_semantic_search_failure_propagates_through_existing_500_handling(isolated_app, monkeypatch):
    """Mirrors the equivalent Phase 2.1 test for the lexical layer -- see
    tests/test_tantivy_lexical_index.py's own docstring for the full
    reasoning. Only proves propagation, not the envelope's own shape
    (tests/test_error_format.py already owns that)."""
    import codepulse.api.app as app_module
    from codepulse.storage.db import RepositoryModel
    from tests.conftest import make_api_key_headers

    client = TestClient(app_module.app, raise_server_exceptions=False)

    session = isolated_app.get_session()
    try:
        repo = RepositoryModel(name="r1", url="file:///r1")
        session.add(repo)
        session.commit()
        repo_id = repo.id
    finally:
        session.close()

    def _broken_search(*args, **kwargs):
        raise RuntimeError("a genuine sqlite-vec-layer failure, must never leak to the client")

    monkeypatch.setattr(app_module.indexing_engine.vector_index, "search", _broken_search)

    headers = make_api_key_headers(isolated_app, repository_ids=[repo_id])
    response = client.post(
        "/v1/search",
        json={"query": "anything", "search_type": "semantic", "repository_id": repo_id},
        headers=headers,
    )

    assert response.status_code == 500
    assert "sqlite-vec-layer failure" not in response.text
