"""
Phase 3.4 — CallEdge.confidence scoring (roadmap §37 item 3.4; §11 "Call
graph confidence"; §29 Data Model: "CallEdge | Yes (rename from
DependencyModel, add confidence) | §11").

Unit-level coverage of `CallGraphBuilder._build_graph`'s own confidence
computation (the "high"/"low"/"unresolved" decision logic itself, in
isolation) already exists and is NOT duplicated here: tests/test_graph.py.

This file exercises the two things that only make sense end-to-end:
  1. `IndexingEngine.index_directory()` -> `_persist_to_db()` actually
     persists each in-memory `CallGraphEdge`'s `confidence` onto its
     durable `CallEdge` row (roundtrip fidelity through the whole
     pipeline, not just the in-memory builder).
  2. `GET /v1/symbols/{id}` surfaces that persisted `confidence` in its
     `called_by`/`calls` response fields (§11/§42: "confidence scoring
     surfaced to callers, not hidden").

All tests use temporary, disposable SQLite databases and temporary
repository directories under `tmp_path` -- never the production
codepulse.db.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

import codepulse.api.app as app_module
from codepulse.api.app import app
from codepulse.embeddings.embedder import CodeEmbedder
from codepulse.indexer.engine import IndexingEngine
from codepulse.storage.db import CallEdge, StorageManager
from tests.conftest import make_api_key_headers


@pytest.fixture(scope="module")
def shared_embedder():
    """One real CodeEmbedder instance reused across tests in this module
    (same rationale as other engine-level test files this session)."""
    return CodeEmbedder()


def _write(repo_path, rel_path: str, content: str) -> None:
    full = repo_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")


def _db_url(tmp_path, name: str) -> str:
    db_path = tmp_path / name
    return f"sqlite:///{str(db_path).replace(os.sep, '/')}"


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    _write(
        repo,
        "db.py",
        'def db_connect():\n    """Open a connection."""\n    return "conn"\n\n\n'
        'def db_query(q):\n    """Run a query."""\n    conn = db_connect()\n    return conn\n',
    )
    _write(
        repo,
        "service.py",
        'from db import db_query\n\n\n'
        'def get_user_profile(user_id):\n    """Look up a user profile."""\n    return db_query("SELECT *")\n',
    )
    return str(repo)


def test_persisted_call_edge_rows_carry_the_computed_confidence(tmp_path, shared_embedder):
    repo_path = _make_repo(tmp_path)
    storage = StorageManager(db_url=_db_url(tmp_path, "confidence.db"))
    engine = IndexingEngine(storage=storage, embedder=shared_embedder)

    engine.index_directory(repo_path, repo_name="r", repo_url=repo_path)

    session = storage.get_session()
    try:
        rows = session.query(CallEdge).all()
        assert len(rows) >= 2

        high = [r for r in rows if r.source_symbol_name == "get_user_profile" and r.target_symbol_name == "db_query"]
        assert len(high) == 1
        assert high[0].confidence == "high"
        assert high[0].target_file == "db.py"

        low = [r for r in rows if r.source_symbol_name == "db_query" and r.target_symbol_name == "db_connect"]
        assert len(low) == 1
        assert low[0].confidence == "low"

        # Every persisted row has SOME real confidence value from this
        # fresh index -- never a silently-missing/None value for a row
        # produced by this run (None is only for pre-migration historical
        # rows, not for anything this pipeline itself writes).
        assert all(r.confidence in ("high", "low", "unresolved") for r in rows)
    finally:
        session.close()


def test_get_symbol_details_surfaces_confidence_in_called_by_and_calls(tmp_path, shared_embedder, monkeypatch):
    repo_path = _make_repo(tmp_path)
    storage = StorageManager(db_url=_db_url(tmp_path, "confidence_api.db"))
    engine = IndexingEngine(storage=storage, embedder=shared_embedder)
    result = engine.index_directory(repo_path, repo_name="r", repo_url=repo_path)

    monkeypatch.setattr(app_module, "storage_manager", storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    client = TestClient(app)
    headers = make_api_key_headers(storage, repository_ids=[result["repository_id"]])

    session = storage.get_session()
    try:
        from codepulse.storage.db import SymbolModel
        db_query_sym = session.query(SymbolModel).filter(SymbolModel.name == "db_query").first()
        symbol_id = db_query_sym.id
    finally:
        session.close()

    response = client.get(f"/v1/symbols/{symbol_id}", headers=headers)
    assert response.status_code == 200
    body = response.json()

    assert body["name"] == "db_query"
    assert len(body["called_by"]) >= 1
    caller = next(c for c in body["called_by"] if c["symbol"] == "get_user_profile")
    assert caller["confidence"] == "high"

    assert len(body["calls"]) >= 1
    callee = next(c for c in body["calls"] if c["symbol"] == "db_connect")
    assert callee["confidence"] == "low"


def test_pre_migration_style_row_with_no_confidence_surfaces_as_null_not_fabricated(tmp_path, monkeypatch):
    """A row inserted without a `confidence` value (standing in for one
    persisted before this migration existed) must surface as `None` in
    the API response, never silently defaulted to some fabricated tier."""
    storage = StorageManager(db_url=_db_url(tmp_path, "legacy_row.db"))
    from codepulse.storage.db import FileModel, RepositoryModel, SymbolModel

    session = storage.get_session()
    try:
        repo = RepositoryModel(name="r", url="file:///r")
        session.add(repo)
        session.flush()
        fm = FileModel(repo_id=repo.id, path="main.py", language="python")
        session.add(fm)
        session.flush()
        sym = SymbolModel(file_id=fm.id, name="target", type="function", start_line=1, end_line=2, language="python")
        session.add(sym)
        session.flush()
        # No `confidence` kwarg at all -- exactly what a row inserted
        # before this migration's column existed would look like.
        ce = CallEdge(source_symbol_name="caller", source_file="main.py", target_symbol_name="target", target_file="main.py")
        session.add(ce)
        session.commit()
        symbol_id = sym.id
        repo_id = repo.id
    finally:
        session.close()

    monkeypatch.setattr(app_module, "storage_manager", storage)
    monkeypatch.setattr(app_module, "indexing_engine", IndexingEngine(storage=storage))
    client = TestClient(app)
    headers = make_api_key_headers(storage, repository_ids=[repo_id])
    response = client.get(f"/v1/symbols/{symbol_id}", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["called_by"][0]["confidence"] is None
