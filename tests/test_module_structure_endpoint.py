"""
Backend-hardening addition (not a roadmap sprint item) -- REST query
surface for Phase 4.3's module clustering: `GET /v1/repositories/{repo_id}/modules`.

`IndexingEngine.analyze_module_structure`/`ModuleGraphBuilder` are
already exhaustively tested engine-level in `tests/test_module_graph.py`
(clustering algorithm, degree computation, repository isolation,
determinism, restart/cold-cache behavior) -- this file proves only the
thin REST wrapper itself: correct shape, authorization (401/403), and
that its output matches a direct `IndexingEngine.analyze_module_structure`
call exactly (no re-derived logic in the route).

Mirrors tests/test_references.py's own REST-section style/fixtures.
"""
from __future__ import annotations

import os
import subprocess

import pytest
from fastapi.testclient import TestClient

import codepulse.api.app as app_module
from codepulse.api.app import app
from codepulse.config import reset_settings
from codepulse.embeddings.embedder import CodeEmbedder
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


def _git(repo_path, *args) -> None:
    subprocess.run(["git", *args], cwd=str(repo_path), check=True, capture_output=True, text=True)


def _init_repo(repo_path) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    _git(repo_path, "init", "-q")
    _git(repo_path, "config", "user.email", "test@codepulse.local")
    _git(repo_path, "config", "user.name", "CodePulse Test")


def _commit_all(repo_path, message: str) -> None:
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-q", "-m", message)


def _write(repo_path, rel_path: str, content: str) -> None:
    full = repo_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")


def _db_url(tmp_path, name: str) -> str:
    db_path = tmp_path / name
    return f"sqlite:///{str(db_path).replace(os.sep, '/')}"


def _new_engine(db_url: str, embedder: CodeEmbedder) -> IndexingEngine:
    storage = StorageManager(db_url=db_url)
    return IndexingEngine(storage=storage, embedder=embedder)


MODULE_A = "from mod_b import fb\n\n\ndef fa():\n    return fb()\n"
MODULE_B = "def fb():\n    return 1\n"


def _index_module_repo(tmp_path, dirname, embedder, db_name_or_engine):
    repo = tmp_path / dirname
    _init_repo(repo)
    _write(repo, "mod_a.py", MODULE_A)
    _write(repo, "mod_b.py", MODULE_B)
    _commit_all(repo, "initial")
    engine = db_name_or_engine if isinstance(db_name_or_engine, IndexingEngine) else _new_engine(
        _db_url(tmp_path, db_name_or_engine), embedder
    )
    result = engine.index_directory(str(repo), repo_name=dirname, repo_url=str(repo))
    return engine, result


def test_get_repository_modules_matches_engine_result_exactly(tmp_path, shared_embedder, monkeypatch):
    engine, result = _index_module_repo(tmp_path, "repo_modules_rest", shared_embedder, "modules_rest.db")
    repo_id = result["repository_id"]
    module_graph = engine.analyze_module_structure(repo_id)

    monkeypatch.setattr(app_module, "storage_manager", engine.storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    client = TestClient(app)
    headers = make_api_key_headers(engine.storage, repository_ids=[repo_id])

    response = client.get(f"/v1/repositories/{repo_id}/modules", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["repository_id"] == repo_id
    assert body["clusters"] == module_graph.clusters
    assert body["ranked_by_inbound"] == module_graph.rank_by_inbound()
    assert body["ranked_by_outbound"] == module_graph.rank_by_outbound()

    # Sanity on real content, not just shape equality against itself.
    all_files = {f for cluster in body["clusters"] for f in cluster["files"]}
    assert all_files == {"mod_a.py", "mod_b.py"}
    inbound_by_file = {row["file"]: row["in_degree"] for row in body["ranked_by_inbound"]}
    assert inbound_by_file["mod_b.py"] == 1
    assert inbound_by_file["mod_a.py"] == 0


def test_get_repository_modules_forbidden_for_a_different_repository(tmp_path, shared_embedder, monkeypatch):
    engine, r_a = _index_module_repo(tmp_path, "repo_a_modules_rest", shared_embedder, "modules_rest_iso.db")
    engine, r_b = _index_module_repo(tmp_path, "repo_b_modules_rest", shared_embedder, engine)

    monkeypatch.setattr(app_module, "storage_manager", engine.storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    client = TestClient(app)
    headers = make_api_key_headers(engine.storage, repository_ids=[r_a["repository_id"]])

    response = client.get(f"/v1/repositories/{r_b['repository_id']}/modules", headers=headers)
    assert response.status_code == 403


def test_get_repository_modules_rejected_without_any_key(tmp_path, shared_embedder, monkeypatch):
    engine, result = _index_module_repo(tmp_path, "repo_modules_noauth", shared_embedder, "modules_noauth.db")
    monkeypatch.setattr(app_module, "storage_manager", engine.storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    client = TestClient(app)

    response = client.get(f"/v1/repositories/{result['repository_id']}/modules")
    assert response.status_code == 401


def test_get_repository_modules_returns_empty_graph_for_a_cold_cache(tmp_path, shared_embedder, monkeypatch):
    """Restart-like behavior: a repository this process has no cached
    parse_results for yet must degrade to an empty graph, never a 500."""
    engine, result = _index_module_repo(tmp_path, "repo_modules_cold", shared_embedder, "modules_cold.db")
    repo_id = result["repository_id"]

    # A key scoped to a repo_id that this fresh engine instance has never
    # parsed -- simulating a restart without actually restarting.
    fresh_engine = _new_engine(_db_url(tmp_path, "modules_cold.db"), shared_embedder)
    monkeypatch.setattr(app_module, "storage_manager", fresh_engine.storage)
    monkeypatch.setattr(app_module, "indexing_engine", fresh_engine)
    client = TestClient(app)
    headers = make_api_key_headers(fresh_engine.storage, repository_ids=[repo_id])

    response = client.get(f"/v1/repositories/{repo_id}/modules", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "repository_id": repo_id,
        "clusters": [],
        "ranked_by_inbound": [],
        "ranked_by_outbound": [],
    }
