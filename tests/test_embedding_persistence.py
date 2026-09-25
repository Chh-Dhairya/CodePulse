"""
Phase 0.5 — Embedding Persistence + Hydration.

These tests exercise `IndexingEngine.index_directory()` / `hydrate_all()`
end-to-end against temporary, disposable SQLite databases (never the
production codepulse.db) to verify:

  - A real embedding is persisted when embedding generation succeeds.
  - A persisted embedding can be loaded after a fresh process/storage
    initialization (simulated here via a second, independently constructed
    IndexingEngine against the same database -- the closest equivalent to
    "restart the process" available within a single test process).
  - Hydrated embeddings populate the in-memory vector index correctly, and
    semantic retrieval still works after hydration alone (no re-indexing).
  - Re-indexing unchanged content does not create duplicate embedding rows.
  - Changed content updates the existing row rather than reusing a stale one.
  - Embedding dimension/model metadata survives a restart.
  - A real embedding-generation failure does NOT create any pseudo-embedding
    (no hash-based, random, or zero vector), and is reported explicitly
    rather than silently as success.

All tests here use the REAL configured embedding model (the project's
established testing strategy -- see tests/test_api.py and others, which
already call `index_directory`/`search` and thus the real embedder). A
single CodeEmbedder instance is shared across the "success" tests via a
module-scoped fixture purely to avoid repeatedly paying the ~10-20s model
load cost -- this has no bearing on the pseudo-embedding invariant, which
concerns what happens when the REAL model cannot be used, not which test
happens to reuse a loaded instance of it.
"""
from __future__ import annotations

import json
import os

import numpy as np
import pytest

from codepulse.config import reset_settings
from codepulse.embeddings.embedder import CodeEmbedder, EmbeddingUnavailableError
from codepulse.indexer.engine import IndexingEngine
from codepulse.storage.db import EmbeddingModel, FileModel, RepositoryModel, StorageManager, SymbolModel


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


# --- 1. Real embedding persisted on success ---------------------------------

def test_real_embedding_is_persisted_on_success(tmp_path, shared_embedder):
    db_path = tmp_path / "t1.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"
    repo_path = _make_repo_dir(tmp_path, "repo_a", SAMPLE_FILES)

    engine = _new_engine(db_url, shared_embedder)
    result = engine.index_directory(repo_path, repo_name="repo_a", repo_url=repo_path)

    assert result["embedding_status"] == "success"
    assert result["embedding_error"] is None
    assert result["degraded_mode"] is False

    session = engine.storage.get_session()
    try:
        rows = session.query(EmbeddingModel).all()
        assert len(rows) >= 1, "at least one embedding row must be persisted"
        for row in rows:
            vec = json.loads(row.embedding_json)
            assert len(vec) == shared_embedder.dimension
            assert row.embedding_dim == shared_embedder.dimension
            assert row.embedding_model == shared_embedder.model_name
            assert row.content_hash  # a real hash was recorded
            # A real embedding is not the all-zero / degenerate vector.
            assert any(abs(v) > 1e-6 for v in vec)
    finally:
        session.close()


# --- 2 & 3. Restart: fresh engine hydrates persisted embeddings -------------

def test_hydration_after_fresh_engine_populates_vector_index(tmp_path, shared_embedder):
    db_path = tmp_path / "t2.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"
    repo_path = _make_repo_dir(tmp_path, "repo_b", SAMPLE_FILES)

    engine1 = _new_engine(db_url, shared_embedder)
    engine1.index_directory(repo_path, repo_name="repo_b", repo_url=repo_path)

    session = engine1.storage.get_session()
    try:
        persisted_count = session.query(EmbeddingModel).count()
        assert persisted_count >= 1
    finally:
        session.close()

    # Simulate a process restart: a brand-new IndexingEngine, same database,
    # no call to index_directory() -- hydration happens automatically in
    # __init__.
    engine2 = _new_engine(db_url, shared_embedder)

    assert len(engine2.vector_index.doc_ids) == persisted_count, (
        "hydration must populate the vector index from durable storage alone"
    )
    assert engine2.vector_index.vectors.shape == (persisted_count, shared_embedder.dimension)


# --- 4. Semantic retrieval works after hydration alone ----------------------

def test_semantic_search_works_after_hydration_without_reindexing(tmp_path, shared_embedder):
    db_path = tmp_path / "t3.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"
    repo_path = _make_repo_dir(tmp_path, "repo_c", SAMPLE_FILES)

    engine1 = _new_engine(db_url, shared_embedder)
    engine1.index_directory(repo_path, repo_name="repo_c", repo_url=repo_path)

    # Fresh engine: hydrated from DB only, index_directory() is never called.
    engine2 = _new_engine(db_url, shared_embedder)
    results = engine2.search("friendly greeting for a name", limit=5, search_type="semantic")

    assert len(results) >= 1, "semantic search must return results after hydration alone"
    assert any(r.symbol_name == "greet" for r in results)


# --- Backend-hardening addition: lexical (BM25) hydration, not a roadmap ----
# --- sprint item -- mirrors the two vector-hydration tests above exactly, --
# --- proving `hydrate_repository`/`hydrate_all` now ALSO rebuild the -------
# --- lexical index from durable symbols/files rows, closing the gap -------
# --- test_restart_idempotency.py used to assert as permanent. -------------

def test_lexical_hydration_after_fresh_engine_populates_bm25_index(tmp_path, shared_embedder):
    db_path = tmp_path / "t2b.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"
    repo_path = _make_repo_dir(tmp_path, "repo_b_lex", SAMPLE_FILES)

    engine1 = _new_engine(db_url, shared_embedder)
    engine1.index_directory(repo_path, repo_name="repo_b_lex", repo_url=repo_path)
    symbol_count = engine1.bm25_index.num_docs
    assert symbol_count >= 1

    # Simulate a process restart: a brand-new IndexingEngine, same database,
    # no call to index_directory() -- hydration happens automatically in
    # __init__.
    engine2 = _new_engine(db_url, shared_embedder)

    assert engine2.bm25_index.num_docs == symbol_count, (
        "lexical hydration must populate bm25_index from durable symbols/files rows alone"
    )


def test_keyword_search_works_after_hydration_without_reindexing(tmp_path, shared_embedder):
    db_path = tmp_path / "t3b.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"
    repo_path = _make_repo_dir(tmp_path, "repo_c_lex", SAMPLE_FILES)

    engine1 = _new_engine(db_url, shared_embedder)
    engine1.index_directory(repo_path, repo_name="repo_c_lex", repo_url=repo_path)

    # Fresh engine: hydrated from DB only, index_directory() is never called.
    engine2 = _new_engine(db_url, shared_embedder)
    results = engine2.search("friendly greeting", limit=5, search_type="keyword")

    assert len(results) >= 1, "keyword search must return results after hydration alone"
    assert any(r.symbol_name == "greet" for r in results)


def test_lexical_hydration_does_not_leak_across_repositories(tmp_path, shared_embedder):
    db_path = tmp_path / "t2c.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"
    repo_a_path = _make_repo_dir(
        tmp_path, "repo_lex_a",
        {"main.py": 'def process_data(x):\n    """Process incoming data records for REPO_A specifically."""\n    return x + 1\n'},
    )
    repo_b_path = _make_repo_dir(
        tmp_path, "repo_lex_b",
        {"main.py": 'def process_data(x):\n    """Process incoming data records for REPO_B specifically."""\n    return x - 1\n'},
    )

    engine1 = _new_engine(db_url, shared_embedder)
    result_a = engine1.index_directory(repo_a_path, repo_name="repo_lex_a", repo_url=repo_a_path)
    result_b = engine1.index_directory(repo_b_path, repo_name="repo_lex_b", repo_url=repo_b_path)

    engine2 = _new_engine(db_url, shared_embedder)
    results_a = engine2.search(
        "process incoming data records", limit=10, search_type="keyword", repository_id=result_a["repository_id"]
    )
    assert len(results_a) >= 1
    assert all("REPO_A" in (r.docstring or "") for r in results_a), (
        "hydrated lexical search scoped to repo A must never surface repo B's own docstring text"
    )


def test_hydrate_repository_reports_hydrated_symbol_count(tmp_path, shared_embedder):
    """
    `hydrate_repository`'s own return value reports exactly how many
    symbols it rehydrated into `bm25_index`. Exercised directly on
    `engine1` (rather than via a second engine's automatic `__init__`
    hydration) after clearing its own just-added in-memory lexical
    entries via `remove_repo` first -- this isolates the method's own
    behavior without calling it twice on the same engine, which is NOT
    a supported/idempotent usage (neither `bm25_index.add_symbol` nor
    `vector_index.add_batch` dedup against an existing entry; this
    method is only ever called once, from `__init__`, in production).
    """
    db_path = tmp_path / "t2d.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"
    repo_path = _make_repo_dir(tmp_path, "repo_hydrate_count", SAMPLE_FILES)

    engine1 = _new_engine(db_url, shared_embedder)
    result = engine1.index_directory(repo_path, repo_name="repo_hydrate_count", repo_url=repo_path)
    symbol_count = engine1.bm25_index.num_docs
    assert symbol_count >= 1

    engine1.bm25_index.remove_repo(result["repository_id"])
    engine1.bm25_index.build()
    assert engine1.bm25_index.num_docs == 0

    hydration_result = engine1.hydrate_repository(repo_path)
    assert hydration_result["hydrated_symbols"] == symbol_count
    assert hydration_result["repo_id"] == result["repository_id"]
    assert engine1.bm25_index.num_docs == symbol_count


def test_lexical_hydration_skips_a_symbol_row_with_an_unrecognized_type(tmp_path, shared_embedder):
    """
    Backend-hardening cleanup (not a roadmap sprint item): `SymbolType.
    VARIABLE`/`INTERFACE` were removed from the enum as genuinely dead
    (produced by no parser, referenced nowhere, absent from the real
    codepulse.db). This test proves the ALREADY-EXISTING defensive
    fallback in `hydrate_repository` -- added when lexical hydration was
    first built, but never directly exercised by any test until now --
    correctly SKIPS (rather than crashes on) a symbol row whose `type`
    column doesn't parse as any current `SymbolType` member, exactly the
    shape a stray legacy/foreign value (like the now-removed "variable"/
    "interface") would take if one ever existed. `symbols.type` is a
    plain, DB-unconstrained `String(50)` -- nothing at the schema level
    prevents this, so the defensive code path is a real safety net, not
    dead code itself.
    """
    db_path = tmp_path / "t2e.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"
    repo_path = _make_repo_dir(tmp_path, "repo_bad_type", SAMPLE_FILES)

    engine1 = _new_engine(db_url, shared_embedder)
    engine1.index_directory(repo_path, repo_name="repo_bad_type", repo_url=repo_path)

    session = engine1.storage.get_session()
    try:
        sym = session.query(SymbolModel).one()
        sym.type = "variable"  # a value no current SymbolType member matches
        session.commit()
    finally:
        session.close()

    # Fresh engine, same database -- exercises hydrate_repository via the
    # real __init__ path, not a direct call, matching how this would
    # actually be hit in production.
    engine2 = _new_engine(db_url, shared_embedder)

    assert engine2.bm25_index.num_docs == 0, (
        "the sole symbol row has an unrecognized type and must be skipped, not hydrated"
    )


# --- 5. Re-indexing unchanged content: no duplicate embedding rows ---------

def test_reindexing_unchanged_content_reuses_embedding_row(tmp_path, shared_embedder):
    db_path = tmp_path / "t4.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"
    repo_path = _make_repo_dir(tmp_path, "repo_d", SAMPLE_FILES)

    engine = _new_engine(db_url, shared_embedder)
    engine.index_directory(repo_path, repo_name="repo_d", repo_url=repo_path)

    session = engine.storage.get_session()
    try:
        before = session.query(EmbeddingModel).all()
        before_ids = sorted(r.id for r in before)
        before_hashes = {r.id: r.content_hash for r in before}
    finally:
        session.close()

    result = engine.index_directory(repo_path, repo_name="repo_d", repo_url=repo_path)
    assert result["database_stats"]["new_embeddings"] == 0
    assert result["database_stats"]["unchanged_embeddings"] == len(before_ids)

    session = engine.storage.get_session()
    try:
        after = session.query(EmbeddingModel).all()
        after_ids = sorted(r.id for r in after)
        assert after_ids == before_ids, "unchanged content must not create new embedding rows"
        for r in after:
            assert r.content_hash == before_hashes[r.id]
    finally:
        session.close()


# --- 6. Changed content updates, not reuses, the embedding ------------------

def test_changed_content_does_not_reuse_stale_embedding(tmp_path, shared_embedder):
    repo_dir = tmp_path / "repo_e"
    repo_dir.mkdir()
    file_path = repo_dir / "main.py"
    file_path.write_text(SAMPLE_FILES["main.py"], encoding="utf-8")
    repo_path = str(repo_dir)

    db_path = tmp_path / "t5.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"

    engine = _new_engine(db_url, shared_embedder)
    engine.index_directory(repo_path, repo_name="repo_e", repo_url=repo_path)

    session = engine.storage.get_session()
    try:
        before = {(r.symbol_id, r.chunk_type, r.chunk_index): r.content_hash for r in session.query(EmbeddingModel).all()}
        before_count = len(before)
    finally:
        session.close()

    # Change the docstring -- same symbol name/start_line, different content.
    file_path.write_text(
        'def greet(name):\n'
        '    """Return a COMPLETELY DIFFERENT greeting message for testing."""\n'
        '    return f"Hey there, {name}!"\n',
        encoding="utf-8",
    )
    result = engine.index_directory(repo_path, repo_name="repo_e", repo_url=repo_path)
    assert result["database_stats"]["updated_embeddings"] >= 1

    session = engine.storage.get_session()
    try:
        after = session.query(EmbeddingModel).all()
        assert len(after) == before_count, "changed content must update in place, not insert a new row"
        after_map = {(r.symbol_id, r.chunk_type, r.chunk_index): r.content_hash for r in after}
        assert after_map.keys() == before.keys()
        # At least the signature_doc chunk (which includes the docstring)
        # must have a different content_hash now.
        changed = [k for k in before if before[k] != after_map[k]]
        assert changed, "at least one embedding's content_hash must have changed"
    finally:
        session.close()


# --- 7. Dimension/model metadata remains valid after restart ---------------

def test_embedding_metadata_valid_after_restart(tmp_path, shared_embedder):
    db_path = tmp_path / "t6.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"
    repo_path = _make_repo_dir(tmp_path, "repo_f", SAMPLE_FILES)

    engine1 = _new_engine(db_url, shared_embedder)
    engine1.index_directory(repo_path, repo_name="repo_f", repo_url=repo_path)

    engine2 = _new_engine(db_url, shared_embedder)  # simulated restart
    session = engine2.storage.get_session()
    try:
        rows = session.query(EmbeddingModel).all()
        assert len(rows) >= 1
        for r in rows:
            assert r.embedding_dim == shared_embedder.dimension
            assert r.embedding_model == shared_embedder.model_name
            vec = np.array(json.loads(r.embedding_json), dtype=np.float32)
            assert vec.shape == (shared_embedder.dimension,)
    finally:
        session.close()

    assert engine2.vector_index.dimension == shared_embedder.dimension
    assert engine2.vector_index.vectors.shape[1] == shared_embedder.dimension


# --- 8. Embedding generation failure: no pseudo-embedding, explicit status -

def test_embedding_failure_does_not_create_pseudo_embedding(tmp_path):
    """
    Forces a REAL embedding-model load failure (an unresolvable model name)
    and verifies:
      - IndexingEngine.index_directory() does not raise/crash.
      - No Embedding row is created for the failed run.
      - The failure is reported explicitly in the returned stats, not
        silently reported as success.
      - Files/Symbols still persist (lexical-only "degraded mode").
    """
    db_path = tmp_path / "t7.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"
    repo_path = _make_repo_dir(tmp_path, "repo_g", SAMPLE_FILES)

    broken_embedder = CodeEmbedder(model_name="definitely-not-a-real-model-xyz-123", dimension=384)

    # The embedder itself must raise explicitly, never fabricate a vector.
    with pytest.raises(EmbeddingUnavailableError):
        broken_embedder.encode(["def foo(): pass"])

    engine = _new_engine(db_url, broken_embedder)
    result = engine.index_directory(repo_path, repo_name="repo_g", repo_url=repo_path)

    assert result["embedding_status"] == "failed"
    assert result["degraded_mode"] is True
    assert result["embedding_error"]  # a non-empty, explicit error message

    # Degraded mode: files/symbols still persist despite the embedding failure.
    assert result["total_files"] == 1
    assert result["total_symbols"] == 1
    assert result["database_stats"]["saved_files"] == 1
    assert result["database_stats"]["saved_symbols"] == 1

    session = engine.storage.get_session()
    try:
        embedding_rows = session.query(EmbeddingModel).all()
        assert len(embedding_rows) == 0, "a failed embedding generation must not persist any row"

        symbol_rows = session.query(SymbolModel).all()
        assert len(symbol_rows) == 1, "symbols must still be persisted in degraded mode"
    finally:
        session.close()

    # And the in-memory vector index stays empty -- nothing fake was added.
    assert len(engine.vector_index.doc_ids) == 0
