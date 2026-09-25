"""
Backend-hardening addition (not a roadmap sprint item) -- query-side
embedding-failure degradation, found by a fresh backend-gap recheck.

Background: `index_directory()` has always handled an unavailable
embedding model gracefully (`EmbeddingUnavailableError` -> lexical-only
"degraded mode," `tests/test_embedding_persistence.py`'s own
`test_embedding_failure_does_not_create_pseudo_embedding`). Query-side
search had no equivalent: `IndexingEngine.search()`'s `self.embedder.
encode(query)` call for `"semantic"`/`"rrf"`/`"hybrid"` had no
try/except at all, so a failed model load propagated all the way up
through `POST /v1/search`, which also had no handler -- landing as a
generic, opaque 500.

Fix:
  - `"hybrid"`/`"rrf"` now gracefully fall back to keyword-only results
    inside `IndexingEngine.search()` itself (they have a fallback
    candidate source -- BM25 -- so silently degrading to it, with a
    logged warning, is the honest, useful behavior).
  - `"semantic"` (no fallback candidate source at all) deliberately
    still raises `EmbeddingUnavailableError` uncaught -- returning an
    empty list would misrepresent "the model is broken" as "nothing
    matched." `POST /v1/search` catches it there and returns a clear,
    honest 503 instead of an opaque 500.

Uses the same genuinely-broken-embedder trick already established in
`tests/test_embedding_persistence.py` (an unresolvable model name,
which really does raise `EmbeddingUnavailableError` on load -- not a
mock/stub standing in for the real failure mode).
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

import codepulse.api.app as app_module
from codepulse.api.app import app
from codepulse.config import reset_settings
from codepulse.embeddings.embedder import CodeEmbedder, EmbeddingUnavailableError
from codepulse.indexer.engine import IndexingEngine
from codepulse.storage.db import StorageManager
from tests.conftest import make_api_key_headers


@pytest.fixture(autouse=True)
def cleanup_settings():
    reset_settings()
    yield
    reset_settings()


@pytest.fixture(scope="module")
def shared_embedder():
    return CodeEmbedder()


def _make_repo_dir(tmp_path, name: str, files: dict[str, str]) -> str:
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


SAMPLE_FILES = {
    "main.py": (
        'def greet(name):\n'
        '    """Return a friendly greeting for the given name."""\n'
        '    return f"Hello, {name}!"\n'
    ),
}


@pytest.fixture
def indexed_repo_with_a_broken_embedder(tmp_path, shared_embedder):
    """Indexes a real repository with the REAL embedder (so bm25_index
    has real content), then constructs a SECOND engine -- with a
    genuinely broken embedder -- against the SAME database, mirroring a
    restart. The second engine's bm25_index is correctly rehydrated
    from durable symbols/files rows (the "restart-survivable lexical
    search" fix); only its embedder is broken."""
    repo_path = _make_repo_dir(tmp_path, "repo_degrade", SAMPLE_FILES)
    db_path = tmp_path / "degrade.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"

    engine1 = _new_engine(db_url, shared_embedder)
    result = engine1.index_directory(repo_path, repo_name="repo_degrade", repo_url=repo_path)

    broken_embedder = CodeEmbedder(model_name="definitely-not-a-real-model-xyz-789", dimension=384)
    with pytest.raises(EmbeddingUnavailableError):
        broken_embedder.encode("sanity check: this embedder is genuinely broken")

    engine2 = _new_engine(db_url, broken_embedder)
    return engine2, result["repository_id"]


# --- Engine-level: hybrid/rrf gracefully degrade -----------------------

def test_hybrid_search_degrades_to_keyword_only_when_embedding_is_unavailable(
    indexed_repo_with_a_broken_embedder,
):
    engine, repo_id = indexed_repo_with_a_broken_embedder

    results = engine.search("friendly greeting", limit=5, search_type="hybrid", repository_id=repo_id)

    assert len(results) >= 1, "hybrid search must still return real, keyword-derived results"
    assert any(r.symbol_name == "greet" for r in results)


def test_rrf_search_degrades_to_keyword_only_when_embedding_is_unavailable(
    indexed_repo_with_a_broken_embedder,
):
    engine, repo_id = indexed_repo_with_a_broken_embedder

    results = engine.search("friendly greeting", limit=5, search_type="rrf", repository_id=repo_id)

    assert len(results) >= 1
    assert any(r.symbol_name == "greet" for r in results)


def test_semantic_search_still_raises_when_embedding_is_unavailable(
    indexed_repo_with_a_broken_embedder,
):
    """Deliberately NOT degraded -- semantic has no fallback candidate
    source, so silently returning an empty list would misrepresent a
    broken model as a genuine zero-result search. Unchanged behavior,
    explicitly locked in by this test."""
    engine, repo_id = indexed_repo_with_a_broken_embedder

    with pytest.raises(EmbeddingUnavailableError):
        engine.search("friendly greeting", limit=5, search_type="semantic", repository_id=repo_id)


def test_keyword_search_is_entirely_unaffected_by_a_broken_embedder(
    indexed_repo_with_a_broken_embedder,
):
    """Sanity: keyword search never touches the embedder at all, broken or not."""
    engine, repo_id = indexed_repo_with_a_broken_embedder

    results = engine.search("friendly greeting", limit=5, search_type="keyword", repository_id=repo_id)
    assert len(results) >= 1


# --- REST-level: POST /v1/search ----------------------------------------

@pytest.fixture
def client_with_broken_embedder(indexed_repo_with_a_broken_embedder, monkeypatch):
    engine, repo_id = indexed_repo_with_a_broken_embedder
    monkeypatch.setattr(app_module, "storage_manager", engine.storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    return TestClient(app), engine, repo_id


def test_post_search_semantic_returns_a_clear_503_not_an_opaque_500(client_with_broken_embedder):
    client, engine, repo_id = client_with_broken_embedder
    headers = make_api_key_headers(engine.storage, repository_ids=[repo_id])

    response = client.post(
        "/v1/search",
        json={"query": "friendly greeting", "repository_id": repo_id, "search_type": "semantic"},
        headers=headers,
    )

    assert response.status_code == 503
    body = response.json()
    assert "error" in body
    assert "embedding model" in body["error"]["message"].lower()


def test_post_search_hybrid_still_returns_200_with_real_results(client_with_broken_embedder):
    client, engine, repo_id = client_with_broken_embedder
    headers = make_api_key_headers(engine.storage, repository_ids=[repo_id])

    response = client.post(
        "/v1/search",
        json={"query": "friendly greeting", "repository_id": repo_id, "search_type": "hybrid"},
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) >= 1
    assert any(r["symbol_name"] == "greet" for r in body["results"])


def test_post_search_rrf_still_returns_200_with_real_results(client_with_broken_embedder):
    client, engine, repo_id = client_with_broken_embedder
    headers = make_api_key_headers(engine.storage, repository_ids=[repo_id])

    response = client.post(
        "/v1/search",
        json={"query": "friendly greeting", "repository_id": repo_id, "search_type": "rrf"},
        headers=headers,
    )

    assert response.status_code == 200
    assert len(response.json()["results"]) >= 1


def test_post_search_keyword_is_entirely_unaffected(client_with_broken_embedder):
    client, engine, repo_id = client_with_broken_embedder
    headers = make_api_key_headers(engine.storage, repository_ids=[repo_id])

    response = client.post(
        "/v1/search",
        json={"query": "friendly greeting", "repository_id": repo_id, "search_type": "keyword"},
        headers=headers,
    )

    assert response.status_code == 200
    assert len(response.json()["results"]) >= 1
