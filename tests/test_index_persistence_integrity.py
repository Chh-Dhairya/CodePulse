"""
Indexing must never report success for data it did not save (found by the final
publication audit, 2026-10-15).

Two defects met here:

1. A regression from the module-edges work: `IndexingEngine._module_edges_table_exists`
   checked for the table through a SEPARATE database connection while the main
   connection was in the middle of a write transaction. Once that transaction grew
   past SQLite's page cache and spilled to disk, the second connection was refused
   ("database is locked") and the whole save failed. Every test indexed a tiny
   repository, so none ever got that far.

2. An older design flaw that turned (1) into silent data loss: `_persist_to_db`
   swallowed every exception and returned `{"error": ...}`; `index_directory` ignored
   it, recorded a snapshot and returned its normal success numbers. A 46-file index
   reported success and left the database with a repository row and zero files,
   symbols or embeddings (the relevance test could not notice: it only queries the
   in-memory indexes).

Rules pinned down here:
  - a large repository is saved completely (files, symbols, embeddings, edges);
  - a failed save FAILS the run: `index_directory` raises, no snapshot is recorded,
    and the in-memory indexes are put back in line with what is actually in the
    database, so a running server never serves content that a restart would lose;
  - the failure reaches REST as a `failed` job with the real error message.

A stand-in embedder (fast, deterministic) is used for volume; the real model is not
needed to exercise persistence.
"""
from __future__ import annotations

import os

import numpy as np
import pytest
from fastapi.testclient import TestClient

from codepulse.api.app import app
from codepulse.embeddings.embedder import CodeEmbedder
from codepulse.indexer.engine import IndexingEngine, IndexingPersistenceError
from codepulse.storage.db import (
    CallEdge,
    EmbeddingModel,
    FileModel,
    ModuleEdge,
    RepositorySnapshotModel,
    StorageManager,
    SymbolModel,
)
from tests.conftest import make_creator_headers


class _StubModel:
    """Deterministic 384-d vectors, no model download; one vector per input text."""

    def encode(self, texts, batch_size=32, show_progress_bar=False, convert_to_numpy=True, normalize_embeddings=True):
        vectors = []
        for text in texts:
            rng = np.random.default_rng(abs(hash(text)) % (2**32))
            v = rng.standard_normal(384).astype(np.float32)
            vectors.append(v / np.linalg.norm(v))
        return np.vstack(vectors)


def _stub_embedder() -> CodeEmbedder:
    embedder = CodeEmbedder()
    embedder._model = _StubModel()
    return embedder


def _make_big_repo(tmp_path, functions_per_file=250, files=3):
    """~750 functions -> ~1,500 chunks (two per function), enough to spill SQLite's default 2 MB page cache."""
    repo = tmp_path / "big"
    repo.mkdir()
    for f in range(files):
        body = "".join(
            f'def fn_{f}_{i}(x):\n    """Docstring number {i} of module {f}."""\n    y = x + {i}\n    return y * {f + 1}\n\n\n'
            for i in range(functions_per_file)
        )
        (repo / f"mod_{f}.py").write_text(body, encoding="utf-8")
    return repo


def _new_engine(tmp_path, name="p.db"):
    return IndexingEngine(
        storage=StorageManager(db_url=f"sqlite:///{(tmp_path / name).as_posix()}"), embedder=_stub_embedder()
    )


def _counts(engine, repo_id):
    session = engine.storage.get_session()
    try:
        files = session.query(FileModel).filter(FileModel.repo_id == repo_id).count()
        symbols = session.query(SymbolModel).join(FileModel).filter(FileModel.repo_id == repo_id).count()
        embeddings = (
            session.query(EmbeddingModel)
            .join(SymbolModel, EmbeddingModel.symbol_id == SymbolModel.id)
            .join(FileModel, SymbolModel.file_id == FileModel.id)
            .filter(FileModel.repo_id == repo_id)
            .count()
        )
        call_edges = session.query(CallEdge).filter(CallEdge.repo_id == repo_id).count()
        module_edges = session.query(ModuleEdge).filter(ModuleEdge.repo_id == repo_id).count()
        snapshots = session.query(RepositorySnapshotModel).filter(RepositorySnapshotModel.repo_id == repo_id).count()
        return {"files": files, "symbols": symbols, "embeddings": embeddings, "call_edges": call_edges,
                "module_edges": module_edges, "snapshots": snapshots}
    finally:
        session.close()


# --- (1) the lock --------------------------------------------------------------

def test_a_large_repository_is_saved_completely(tmp_path):
    repo = _make_big_repo(tmp_path)
    engine = _new_engine(tmp_path)

    result = engine.index_directory(str(repo), repo_name="big", repo_url=str(repo))

    assert "error" not in result["database_stats"], result["database_stats"]
    counts = _counts(engine, result["repository_id"])
    assert counts["files"] == 3
    assert counts["symbols"] == result["total_symbols"] == 750
    assert counts["embeddings"] == result["total_chunks"] > 1000, "the volume must be large enough to exercise the spill"
    assert counts["module_edges"] >= 3, "module rows are written in the same transaction"
    assert counts["snapshots"] == 1


def test_a_large_repository_survives_a_restart_with_every_index_restored(tmp_path):
    repo = _make_big_repo(tmp_path)
    engine = _new_engine(tmp_path)
    repo_id = engine.index_directory(str(repo), repo_name="big", repo_url=str(repo))["repository_id"]
    engine.storage.engine.dispose()

    restarted = _new_engine(tmp_path)

    assert len(restarted.vector_index.doc_ids) > 1000
    assert restarted.bm25_index.num_docs == 750
    assert restarted.search("Docstring number 7 of module 1", limit=5, search_type="keyword", repository_id=repo_id)


# --- (2) a failed save is a failed run -----------------------------------------

class _Boom(RuntimeError):
    pass


def test_a_failed_save_raises_instead_of_reporting_success(tmp_path, monkeypatch):
    repo = _make_big_repo(tmp_path, functions_per_file=20, files=1)
    engine = _new_engine(tmp_path)
    monkeypatch.setattr(engine, "_persist_call_graph_edges", lambda *a, **k: (_ for _ in ()).throw(_Boom("disk full")))

    with pytest.raises(IndexingPersistenceError) as excinfo:
        engine.index_directory(str(repo), repo_name="big", repo_url=str(repo))

    assert "disk full" in str(excinfo.value)


def test_a_failed_save_records_no_snapshot_and_saves_nothing(tmp_path, monkeypatch):
    repo = _make_big_repo(tmp_path, functions_per_file=20, files=1)
    engine = _new_engine(tmp_path)
    monkeypatch.setattr(engine, "_persist_call_graph_edges", lambda *a, **k: (_ for _ in ()).throw(_Boom("disk full")))

    with pytest.raises(IndexingPersistenceError):
        engine.index_directory(str(repo), repo_name="big", repo_url=str(repo))

    session = engine.storage.get_session()
    try:
        repo_id = session.query(__import__("codepulse.storage.db", fromlist=["RepositoryModel"]).RepositoryModel).one().id
    finally:
        session.close()
    counts = _counts(engine, repo_id)
    assert counts == {"files": 0, "symbols": 0, "embeddings": 0, "call_edges": 0, "module_edges": 0, "snapshots": 0}


def test_after_a_failed_save_the_memory_matches_the_database_not_the_failed_run(tmp_path, monkeypatch):
    """The first run is saved; a second run (with new content) fails to save. The running engine must keep
    serving the saved state, exactly what a restart would restore, not the new content that was lost."""
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "a.py").write_text('def saved_function():\n    """Saved."""\n    return 1\n', encoding="utf-8")
    engine = _new_engine(tmp_path)
    repo_id = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))["repository_id"]
    assert [r.symbol_name for r in engine.search("saved_function", limit=5, search_type="keyword", repository_id=repo_id)] == ["saved_function"]

    (repo / "b.py").write_text('def lost_function():\n    """Lost."""\n    return 2\n', encoding="utf-8")
    monkeypatch.setattr(engine, "_persist_call_graph_edges", lambda *a, **k: (_ for _ in ()).throw(_Boom("disk full")))
    with pytest.raises(IndexingPersistenceError):
        engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    monkeypatch.undo()

    assert "lost_function" not in [r.symbol_name for r in engine.search("lost_function", limit=5, search_type="keyword", repository_id=repo_id)]
    assert [r.symbol_name for r in engine.search("saved_function", limit=5, search_type="keyword", repository_id=repo_id)] == ["saved_function"]
    assert f"{repo_id}::b.py::lost_function" not in engine.symbols_by_id
    assert _counts(engine, repo_id)["files"] == 1


def test_a_rest_job_whose_save_fails_is_failed_with_the_real_message(isolated_app, tmp_path, monkeypatch):
    import codepulse.api.app as app_module

    repo = _make_big_repo(tmp_path, functions_per_file=10, files=1)
    engine = IndexingEngine(storage=isolated_app, embedder=_stub_embedder())
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    monkeypatch.setattr(engine, "_persist_call_graph_edges", lambda *a, **k: (_ for _ in ()).throw(_Boom("disk full")))
    client = TestClient(app)
    headers = make_creator_headers(isolated_app)

    job = client.post("/v1/repositories/index", json={"url": str(repo)}, headers=headers).json()
    status = client.get(f"/v1/jobs/{job['job_id']}", headers=headers).json()

    assert status["status"] == "failed"
    assert "disk full" in status["error"]
