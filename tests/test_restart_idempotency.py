"""
Phase 0.10 — Restart-survivability + idempotency test suite.

Existing coverage audited before writing this file (see CODEPULSE_PROGRESS.md
for the full audit): repository/file upsert idempotency (test_repository_
file_upsert.py, Phase 0.3), symbol upsert idempotency (test_symbol_upsert.py,
Phase 0.4), embedding persistence + hydration + no-pseudo-embedding-fallback
(test_embedding_persistence.py, Phase 0.5), repository-scoped search within
one continuous engine session (test_repository_scoped_search.py, Phase 0.6),
and Alembic migration survivability across a fresh database (test_migrations.
py, all phases). All of that is real, thorough, and NOT duplicated here.

This file adds exactly the gaps that audit found:

  1. A single combined scenario proving repository + file + symbol +
     embedding idempotency AND identifier stability TOGETHER, across more
     than two re-indexing passes (the existing tests prove each entity
     type in isolation, mostly across exactly two indexing calls).
  2. An explicit test of what a "restart" actually restores: semantic
     search (durable, hydrated) vs. lexical/call-graph state (not
     durable -- no backing DB table for either). No existing test asserts
     the *absence* of lexical/graph state after a restart; only the
     *presence* of hydrated vector state was previously tested.
  3. A test proving hydration itself never calls the embedding model
     (spied, not merely "doesn't crash" -- hydrate_all()'s own try/except
     would silently swallow a real call failure, so "no crash" alone
     wouldn't prove non-invocation).
  4. Repository isolation for semantic search specifically AFTER an
     explicit restart (existing coverage only tests isolation within one
     continuous engine session).

RESTART MODEL: every "restart" in this file means "dispose the first
StorageManager's SQLAlchemy engine (closing its DB connection pool), then
construct a brand-new StorageManager + IndexingEngine against the same
on-disk SQLite file." This is Restart Model B from the milestone's own
classification (closing the database/session and creating a fresh engine),
NOT a real OS-level process kill/restart (Model C) -- no subprocess
infrastructure is introduced here, consistent with the roadmap not
requiring literal process-level restart testing and the existing
architecture (a single Python library, not a client/server split) making
Model B the correct and sufficient level to prove the persistence
contract. Do not read "restart" in this file as "process restart."

All tests use temporary, disposable SQLite databases and temporary
repository directories under `tmp_path` -- never the production
codepulse.db.
"""
from __future__ import annotations

import os

import pytest

from codepulse.config import reset_settings
from codepulse.embeddings.embedder import CodeEmbedder
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
    so the underlying SentenceTransformer model is loaded only once. This
    tests the real persistence/hydration contract, not a stand-in for the
    embedding provider -- see module docstring and CODEPULSE_PROGRESS.md
    for why the real model is used rather than a mock."""
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


def _restart(engine: IndexingEngine, db_url: str, embedder: CodeEmbedder) -> IndexingEngine:
    """Performs a Restart-Model-B restart: closes the given engine's DB
    connection pool, then constructs a brand-new StorageManager +
    IndexingEngine against the same on-disk database."""
    engine.storage.engine.dispose()
    return _new_engine(db_url, embedder)


# --- 1. Combined repository + file + symbol + embedding idempotency, -------
# --- with identifier stability, across more than two re-indexes ------------

def test_repeated_reindex_is_idempotent_and_identifiers_stay_stable(tmp_path, shared_embedder):
    repo_path = _make_repo_dir(
        tmp_path,
        "repo_idem",
        {
            "main.py": (
                'def alpha():\n    """First function."""\n    return 1\n\n\n'
                'def beta():\n    """Second function."""\n    return 2\n'
            ),
            "utils.py": 'def gamma():\n    """Third function."""\n    return 3\n',
        },
    )
    db_path = tmp_path / "idem.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"
    engine = _new_engine(db_url, shared_embedder)

    seen = {}  # populated on the first pass, compared against on every later pass

    for iteration in range(5):
        engine.index_directory(repo_path, repo_name="repo_idem", repo_url=repo_path)

        session = engine.storage.get_session()
        try:
            repos = session.query(RepositoryModel).all()
            assert len(repos) == 1, f"iteration {iteration}: expected exactly 1 repository row"
            repo_id = repos[0].id

            files = session.query(FileModel).filter(FileModel.repo_id == repo_id).all()
            assert len(files) == 2, f"iteration {iteration}: expected exactly 2 file rows"

            symbols = (
                session.query(SymbolModel)
                .join(FileModel, SymbolModel.file_id == FileModel.id)
                .filter(FileModel.repo_id == repo_id)
                .all()
            )
            assert len(symbols) == 3, f"iteration {iteration}: expected exactly 3 symbol rows"

            embeddings = (
                session.query(EmbeddingModel)
                .join(SymbolModel, EmbeddingModel.symbol_id == SymbolModel.id)
                .join(FileModel, SymbolModel.file_id == FileModel.id)
                .filter(FileModel.repo_id == repo_id)
                .all()
            )
            assert len(embeddings) >= 3, f"iteration {iteration}: expected at least one embedding per symbol"

            current = {
                "repo_id": repo_id,
                "file_ids": {f.id for f in files},
                "symbol_ids": {s.id for s in symbols},
                "embedding_ids": {e.id for e in embeddings},
                "counts": (len(files), len(symbols), len(embeddings)),
            }

            if iteration == 0:
                seen.update(current)
            else:
                assert current["repo_id"] == seen["repo_id"], f"iteration {iteration}: repository id must stay stable"
                assert current["file_ids"] == seen["file_ids"], f"iteration {iteration}: file ids must stay stable"
                assert current["symbol_ids"] == seen["symbol_ids"], f"iteration {iteration}: symbol ids must stay stable"
                assert current["embedding_ids"] == seen["embedding_ids"], f"iteration {iteration}: embedding ids must stay stable"
                assert current["counts"] == seen["counts"], f"iteration {iteration}: row counts must not grow"
        finally:
            session.close()

        # Phase 2.1: the Tantivy-backed BM25Index has no backing SQL table
        # (see test 2 below -- lexical state is intentionally not durable),
        # so its own idempotency must be checked directly against the
        # engine's live in-memory state, not via a session query like the
        # SQL-backed assertions above. Before Phase 2.1, `BM25Index` had no
        # doc_id-based replace logic at all, so this exact 5-iteration loop
        # would have silently accumulated 5x duplicate lexical documents
        # without any test ever catching it -- asserted explicitly here so
        # that regression can never again go unnoticed.
        assert engine.bm25_index.num_docs == 3, (
            f"iteration {iteration}: repeated re-indexing must not accumulate duplicate "
            f"lexical (Tantivy) documents -- expected exactly 3 (one per symbol), "
            f"got {engine.bm25_index.num_docs}"
        )

        # Backend-hardening fix (mirrors the bm25_index assertion just
        # above): before this fix, VectorIndex had no repository-replacement
        # call at all, so this exact 5-iteration full-reindex loop would
        # have silently accumulated 5x duplicate vector entries -- asserted
        # explicitly here, against the real IndexingEngine (not just the
        # VectorIndex unit tests in test_sqlite_vec_semantic_index.py), so
        # that regression can never again go unnoticed. Each symbol may
        # legitimately produce more than one chunk (SemanticChunker), so
        # this asserts non-growth across iterations rather than a fixed
        # count of exactly 3.
        if iteration == 0:
            seen["vector_doc_count"] = len(engine.vector_index.doc_ids)
            assert seen["vector_doc_count"] >= 3
        else:
            assert len(engine.vector_index.doc_ids) == seen["vector_doc_count"], (
                f"iteration {iteration}: repeated re-indexing must not accumulate duplicate "
                f"vector (sqlite-vec) documents -- expected exactly "
                f"{seen['vector_doc_count']} (stable since iteration 0), "
                f"got {len(engine.vector_index.doc_ids)}"
            )


# --- 2. Restart restores durable (semantic) state, not in-memory-only state -

def test_restart_restores_semantic_state_but_not_lexical_or_graph_state(tmp_path, shared_embedder):
    repo_path = _make_repo_dir(
        tmp_path,
        "repo_restart",
        {"main.py": 'def greet(name):\n    """Return a friendly greeting for the given name."""\n    return f"Hello, {name}!"\n'},
    )
    db_path = tmp_path / "restart.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"

    engine1 = _new_engine(db_url, shared_embedder)
    engine1.index_directory(repo_path, repo_name="repo_restart", repo_url=repo_path)

    # Sanity: before restart, all in-memory structures are populated.
    assert engine1.bm25_index.num_docs > 0
    assert len(engine1.call_graphs) > 0
    assert len(engine1.vector_index.doc_ids) > 0

    engine2 = _restart(engine1, db_url, shared_embedder)

    # Semantic search state IS durable -- hydrated automatically from the
    # database with no re-indexing (Phase 0.5).
    assert len(engine2.vector_index.doc_ids) > 0, "vector index must be hydrated after restart"
    semantic_results = engine2.search("friendly greeting for a name", limit=5, search_type="semantic")
    assert len(semantic_results) >= 1
    assert any(r.symbol_name == "greet" for r in semantic_results)

    # Lexical (BM25) and call-graph state are NOT durable -- there is no
    # backing DB table for either, so a fresh engine starts genuinely
    # empty until index_directory() is called again. This is the actual,
    # intended architecture (Phase 0.5 only made embeddings durable), not
    # a defect -- asserted explicitly here so it can never silently regress
    # into a false "everything survives restart" claim.
    assert engine2.bm25_index.num_docs == 0, "BM25 index has no backing table and must not survive restart"
    assert engine2.call_graphs == {}, "call graphs have no backing table and must not survive restart"
    assert engine2.symbols_by_id == {}, "in-memory symbol map has no backing table and must not survive restart"

    keyword_results = engine2.search("anything", limit=5, search_type="keyword")
    assert keyword_results == [], "keyword search must return nothing until re-indexed after restart"


# --- 3. Hydration never calls the embedding model ---------------------------

def test_hydration_never_calls_the_embedding_model(tmp_path, shared_embedder):
    repo_path = _make_repo_dir(
        tmp_path,
        "repo_no_model_call",
        {"main.py": 'def greet(name):\n    """Return a friendly greeting for the given name."""\n    return f"Hello, {name}!"\n'},
    )
    db_path = tmp_path / "no_model_call.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"

    engine1 = _new_engine(db_url, shared_embedder)
    engine1.index_directory(repo_path, repo_name="repo_no_model_call", repo_url=repo_path)
    engine1.storage.engine.dispose()

    # A deliberately broken embedder for the "restart" engine. hydrate_all()
    # catches and only logs any internal failure (see IndexingEngine.__init__),
    # so "engine construction doesn't crash" alone would NOT prove the model
    # was never called -- instead, spy on .encode() directly and assert it is
    # never invoked during hydration.
    broken_embedder = CodeEmbedder(model_name="definitely-not-a-real-model-xyz-789", dimension=384)
    call_count = {"n": 0}
    original_encode = broken_embedder.encode

    def _spy_encode(*args, **kwargs):
        call_count["n"] += 1
        return original_encode(*args, **kwargs)

    broken_embedder.encode = _spy_encode  # type: ignore[method-assign]

    storage2 = StorageManager(db_url=db_url)
    engine2 = IndexingEngine(storage=storage2, embedder=broken_embedder)

    assert call_count["n"] == 0, "hydration must never call the embedding model"
    # Hydration still worked despite the embedder being unusable -- proving
    # persistence/hydration is fully decoupled from any live model call.
    assert len(engine2.vector_index.doc_ids) >= 1


# --- 4. Repository isolation for semantic search holds after restart -------

def test_repository_isolation_holds_after_restart(tmp_path, shared_embedder):
    repo_a_path = _make_repo_dir(
        tmp_path,
        "repo_iso_a",
        {"main.py": 'def process_data(x):\n    """Process incoming data records for REPO_A specifically."""\n    return x + 1\n'},
    )
    repo_b_path = _make_repo_dir(
        tmp_path,
        "repo_iso_b",
        {"main.py": 'def process_data(x):\n    """Process incoming data records for REPO_B specifically."""\n    return x - 1\n'},
    )
    db_path = tmp_path / "iso_restart.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"

    engine1 = _new_engine(db_url, shared_embedder)
    result_a = engine1.index_directory(repo_a_path, repo_name="repo_iso_a", repo_url=repo_a_path)
    result_b = engine1.index_directory(repo_b_path, repo_name="repo_iso_b", repo_url=repo_b_path)
    repo_a_id = result_a["repository_id"]
    repo_b_id = result_b["repository_id"]

    engine2 = _restart(engine1, db_url, shared_embedder)

    # Note: semantic/vector results carry their distinguishing text in
    # `snippet` (populated from the chunk content, both for freshly
    # indexed and hydrated vectors), not `docstring` -- the vector-index
    # metadata pipeline (index_directory's step 4 and hydrate_repository
    # alike) has never populated a separate `docstring` key, only
    # `snippet`; that field is only populated for BM25/keyword results.
    # Verified directly against a real run before writing this assertion
    # (see CODEPULSE_PROGRESS.md) rather than assumed.
    results_a = engine2.search(
        "process incoming data records", limit=10, search_type="semantic", repository_id=repo_a_id
    )
    assert len(results_a) >= 1
    for r in results_a:
        assert r.doc_id.startswith(f"{repo_a_id}::"), f"result {r.doc_id} must belong to repo A"
    assert all("REPO_B" not in r.snippet for r in results_a)
    assert any("REPO_A" in r.snippet for r in results_a)

    results_b = engine2.search(
        "process incoming data records", limit=10, search_type="semantic", repository_id=repo_b_id
    )
    assert len(results_b) >= 1
    for r in results_b:
        assert r.doc_id.startswith(f"{repo_b_id}::"), f"result {r.doc_id} must belong to repo B"
    assert all("REPO_A" not in r.snippet for r in results_b)
    assert any("REPO_B" in r.snippet for r in results_b)
