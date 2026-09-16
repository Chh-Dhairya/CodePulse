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

Also covers the repository-isolation fix that followed Phase 3.4 (not
a named roadmap sprint item -- a real, confirmed cross-repository data
leak found by a Phase 3.4 pre-commit audit, fixed on explicit
instruction before Phase 3.5): `CallEdge.repo_id` (migration
`2cd38eb12a7e`) plus the corresponding query changes in
`GET /v1/symbols/{id}` and `POST /v1/symbols/{id}/impact-analysis`.
These tests directly reproduce the exact two-repository, colliding-
symbol-name scenario the audit used to confirm the leak, proving it no
longer reproduces.

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
        # `repo_id` IS set here (unlike a genuinely pre-isolation-fix
        # row) specifically so this test isolates the concern it's
        # actually about (a missing `confidence` surfacing as `None`,
        # not fabricated) from the separate repo_id-scoping behavior
        # covered by its own dedicated tests below.
        ce = CallEdge(repo_id=repo.id, source_symbol_name="caller", source_file="main.py", target_symbol_name="target", target_file="main.py")
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


# --- Repository-isolation fix (post-Phase-3.4, pre-Phase-3.5) --------------

def _make_two_repos_with_colliding_symbol_names(tmp_path):
    """The exact fixture shape used to confirm the leak during the
    Phase 3.4 pre-commit audit: two unrelated repositories that each
    define their own, unrelated `query()` symbol -- repo A's `query()`
    is called by repo A's own `helper()`; repo B's `query()` has no
    callers at all."""
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    _write(
        repo_a, "service.py",
        'def helper():\n    """Repo A internal helper."""\n    return query()\n\n\n'
        'def query():\n    """Repo A query fn."""\n    return 1\n',
    )
    _write(repo_b, "other.py", 'def query():\n    """Repo B unrelated query fn."""\n    return 2\n')
    return str(repo_a), str(repo_b)


def test_get_symbol_details_does_not_leak_a_different_repositorys_call_edges(tmp_path, shared_embedder, monkeypatch):
    """Direct reproduction of the confirmed Phase 3.4 audit finding --
    proves it no longer reproduces."""
    repo_a_path, repo_b_path = _make_two_repos_with_colliding_symbol_names(tmp_path)
    storage = StorageManager(db_url=_db_url(tmp_path, "isolation.db"))
    engine = IndexingEngine(storage=storage, embedder=shared_embedder)
    r_a = engine.index_directory(repo_a_path, repo_name="repo_a", repo_url=repo_a_path)
    r_b = engine.index_directory(repo_b_path, repo_name="repo_b", repo_url=repo_b_path)

    monkeypatch.setattr(app_module, "storage_manager", storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    client = TestClient(app)
    # A key scoped ONLY to repo B -- must never see repo A's data.
    headers = make_api_key_headers(storage, repository_ids=[r_b["repository_id"]])

    from codepulse.storage.db import FileModel, SymbolModel
    session = storage.get_session()
    try:
        b_query_sym = session.query(SymbolModel).join(FileModel).filter(
            FileModel.repo_id == r_b["repository_id"], SymbolModel.name == "query"
        ).first()
        symbol_id = b_query_sym.id
    finally:
        session.close()

    response = client.get(f"/v1/symbols/{symbol_id}", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["file"] == "other.py"
    assert body["called_by"] == [], (
        "repo B's query() has no real callers -- if repo A's helper() shows up here, "
        "the cross-repository leak has reappeared"
    )


def test_get_symbol_details_still_returns_same_repository_call_edges_correctly(tmp_path, shared_embedder, monkeypatch):
    """The fix must not overcorrect into hiding a symbol's OWN
    repository's real edges -- the positive case, checked directly
    alongside the negative one above."""
    repo_a_path, repo_b_path = _make_two_repos_with_colliding_symbol_names(tmp_path)
    storage = StorageManager(db_url=_db_url(tmp_path, "isolation_positive.db"))
    engine = IndexingEngine(storage=storage, embedder=shared_embedder)
    r_a = engine.index_directory(repo_a_path, repo_name="repo_a", repo_url=repo_a_path)
    engine.index_directory(repo_b_path, repo_name="repo_b", repo_url=repo_b_path)

    monkeypatch.setattr(app_module, "storage_manager", storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    client = TestClient(app)
    headers = make_api_key_headers(storage, repository_ids=[r_a["repository_id"]])

    from codepulse.storage.db import FileModel, SymbolModel
    session = storage.get_session()
    try:
        a_query_sym = session.query(SymbolModel).join(FileModel).filter(
            FileModel.repo_id == r_a["repository_id"], SymbolModel.name == "query"
        ).first()
        symbol_id = a_query_sym.id
    finally:
        session.close()

    response = client.get(f"/v1/symbols/{symbol_id}", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert len(body["called_by"]) == 1
    assert body["called_by"][0]["symbol"] == "helper"
    assert body["called_by"][0]["file"] == "service.py"


def test_impact_analysis_does_not_leak_a_different_repositorys_call_graph(tmp_path, shared_embedder, monkeypatch):
    """Same reproduction, against the OTHER endpoint that had the
    related `indexing_engine.call_graph` (singular, last-indexed-only)
    scoping bug."""
    repo_a_path, repo_b_path = _make_two_repos_with_colliding_symbol_names(tmp_path)
    storage = StorageManager(db_url=_db_url(tmp_path, "isolation_impact.db"))
    engine = IndexingEngine(storage=storage, embedder=shared_embedder)
    r_a = engine.index_directory(repo_a_path, repo_name="repo_a", repo_url=repo_a_path)
    r_b = engine.index_directory(repo_b_path, repo_name="repo_b", repo_url=repo_b_path)

    monkeypatch.setattr(app_module, "storage_manager", storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    client = TestClient(app)
    headers = make_api_key_headers(storage, repository_ids=[r_b["repository_id"]])

    from codepulse.storage.db import FileModel, SymbolModel
    session = storage.get_session()
    try:
        b_query_sym = session.query(SymbolModel).join(FileModel).filter(
            FileModel.repo_id == r_b["repository_id"], SymbolModel.name == "query"
        ).first()
        symbol_id = b_query_sym.id
    finally:
        session.close()

    response = client.post(f"/v1/symbols/{symbol_id}/impact-analysis", json={}, headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["call_chain"] == [], (
        "repo A's helper() must never appear as a caller of repo B's unrelated query()"
    )
    assert body["direct_callers"] == 0


def test_a_call_edge_row_with_no_repo_id_is_invisible_to_every_repository(tmp_path, shared_embedder, monkeypatch):
    """A `CallEdge` row that couldn't be unambiguously backfilled (or
    was inserted with no `repo_id` at all) must never surface for ANY
    repository -- the deliberate, disclosed tradeoff (a one-time
    completeness gap for that specific edge) that makes the isolation
    fix safe by construction rather than by best-effort filtering."""
    repo_path = _make_repo(tmp_path)
    storage = StorageManager(db_url=_db_url(tmp_path, "orphaned_edge.db"))
    engine = IndexingEngine(storage=storage, embedder=shared_embedder)
    result = engine.index_directory(repo_path, repo_name="r", repo_url=repo_path)

    session = storage.get_session()
    try:
        # Simulate a row that could not be attributed to any repository
        # (e.g. an ambiguous or failed historical backfill).
        orphan = CallEdge(
            repo_id=None, source_symbol_name="ghost_caller", source_file="db.py",
            target_symbol_name="db_query", target_file="db.py", confidence="low",
        )
        session.add(orphan)
        session.commit()

        from codepulse.storage.db import SymbolModel
        db_query_sym = session.query(SymbolModel).filter(SymbolModel.name == "db_query").first()
        symbol_id = db_query_sym.id
    finally:
        session.close()

    monkeypatch.setattr(app_module, "storage_manager", storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    client = TestClient(app)
    headers = make_api_key_headers(storage, repository_ids=[result["repository_id"]])

    response = client.get(f"/v1/symbols/{symbol_id}", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert not any(c["symbol"] == "ghost_caller" for c in body["called_by"]), (
        "an orphaned (repo_id=NULL) CallEdge row must never surface for any repository, "
        "including the one its source_file happens to also belong to"
    )
