"""
Backend-hardening addition (not a roadmap sprint item) -- Phase 0
prerequisites from `CODEPULSE_CLIENT_STRATEGY.md`'s VS Code extension
(and Web UI) implementation plans:

  - `GET /v1/repositories`: lists the repositories the CALLING KEY
    holds a grant for. Previously no route could enumerate what's
    indexed at all -- every repository-scoped route needed a `repo_id`
    the caller already knew.
  - `GET /v1/repositories/{repo_id}/resolve`: resolves the symbol
    containing a given (file path, line) -- needed for a hover
    provider/CodeLens, which only knows the open file's path and the
    cursor's line, not any internal symbol/file id. Prefers the
    NARROWEST matching symbol when a line falls inside more than one
    (e.g. a method nested inside its enclosing class).

Mirrors tests/test_module_structure_endpoint.py's own REST-section
style/fixtures (real temporary git repos, a shared real embedder,
monkeypatched app_module state, TestClient + make_api_key_headers).
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
from codepulse.storage.db import StorageManager, SymbolModel
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


def _index_repo(tmp_path, dirname, embedder, db_name_or_engine, content: str):
    repo = tmp_path / dirname
    _init_repo(repo)
    _write(repo, "main.py", content)
    _commit_all(repo, "initial")
    engine = db_name_or_engine if isinstance(db_name_or_engine, IndexingEngine) else _new_engine(
        _db_url(tmp_path, db_name_or_engine), embedder
    )
    result = engine.index_directory(str(repo), repo_name=dirname, repo_url=str(repo))
    return engine, result


SIMPLE_FILE = 'def greet(name):\n    """Say hello."""\n    return f"Hello, {name}!"\n'


# --- GET /v1/repositories ----------------------------------------------

def test_list_repositories_returns_only_repos_the_key_is_scoped_to(tmp_path, shared_embedder, monkeypatch):
    engine, r_a = _index_repo(tmp_path, "repo_list_a", shared_embedder, "list.db", SIMPLE_FILE)
    engine, r_b = _index_repo(tmp_path, "repo_list_b", shared_embedder, engine, SIMPLE_FILE)

    monkeypatch.setattr(app_module, "storage_manager", engine.storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    client = TestClient(app)

    headers = make_api_key_headers(engine.storage, repository_ids=[r_a["repository_id"]])
    response = client.get("/v1/repositories", headers=headers)

    assert response.status_code == 200
    body = response.json()
    repo_ids = {r["repository_id"] for r in body["repositories"]}
    assert repo_ids == {r_a["repository_id"]}
    assert r_b["repository_id"] not in repo_ids


def test_list_repositories_response_shape(tmp_path, shared_embedder, monkeypatch):
    engine, result = _index_repo(tmp_path, "repo_list_shape", shared_embedder, "list_shape.db", SIMPLE_FILE)

    monkeypatch.setattr(app_module, "storage_manager", engine.storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    client = TestClient(app)

    headers = make_api_key_headers(engine.storage, repository_ids=[result["repository_id"]])
    response = client.get("/v1/repositories", headers=headers)

    assert response.status_code == 200
    entries = response.json()["repositories"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["repository_id"] == result["repository_id"]
    assert entry["name"] == "repo_list_shape"
    assert entry["file_count"] == 1
    assert entry["symbol_count"] == 1
    assert entry["last_sync"] is not None
    assert "url" in entry and "branch" in entry


def test_list_repositories_with_an_unscoped_key_returns_an_empty_list(tmp_path, shared_embedder, monkeypatch):
    engine, result = _index_repo(tmp_path, "repo_list_unscoped", shared_embedder, "list_unscoped.db", SIMPLE_FILE)

    monkeypatch.setattr(app_module, "storage_manager", engine.storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    client = TestClient(app)

    headers = make_api_key_headers(engine.storage, repository_ids=[])
    response = client.get("/v1/repositories", headers=headers)

    assert response.status_code == 200
    assert response.json()["repositories"] == []


def test_list_repositories_requires_authentication(tmp_path, shared_embedder, monkeypatch):
    engine, _ = _index_repo(tmp_path, "repo_list_noauth", shared_embedder, "list_noauth.db", SIMPLE_FILE)

    monkeypatch.setattr(app_module, "storage_manager", engine.storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    client = TestClient(app)

    response = client.get("/v1/repositories")
    assert response.status_code == 401


# --- GET /v1/repositories/{repo_id}/resolve ------------------------------

NESTED_FILE = (
    'class Greeter:\n'
    '    """A greeter class."""\n'
    '\n'
    '    def greet(self, name):\n'
    '        """Say hello."""\n'
    '        return f"Hello, {name}!"\n'
)


@pytest.fixture
def nested_symbol_repo(tmp_path, shared_embedder, monkeypatch):
    engine, result = _index_repo(tmp_path, "repo_resolve", shared_embedder, "resolve.db", NESTED_FILE)
    repo_id = result["repository_id"]

    session = engine.storage.get_session()
    try:
        cls_sym = session.query(SymbolModel).filter(SymbolModel.name == "Greeter").one()
        method_sym = session.query(SymbolModel).filter(SymbolModel.name == "greet").one()
        cls_range = (cls_sym.start_line, cls_sym.end_line)
        method_range = (method_sym.start_line, method_sym.end_line)
    finally:
        session.close()

    monkeypatch.setattr(app_module, "storage_manager", engine.storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    client = TestClient(app)
    headers = make_api_key_headers(engine.storage, repository_ids=[repo_id])

    return {
        "client": client,
        "engine": engine,
        "repo_id": repo_id,
        "headers": headers,
        "cls_range": cls_range,
        "method_range": method_range,
    }


def test_resolve_prefers_the_narrowest_symbol_at_a_line_inside_both(nested_symbol_repo):
    d = nested_symbol_repo
    line = d["method_range"][0]  # a line that is inside BOTH Greeter's and greet's range
    assert d["cls_range"][0] <= line <= d["cls_range"][1], "sanity: this line must also be inside Greeter's range"

    response = d["client"].get(
        f"/v1/repositories/{d['repo_id']}/resolve?path=main.py&line={line}", headers=d["headers"]
    )
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] is not None
    assert body["symbol"]["name"] == "greet", "the narrowest (method) match must win over the enclosing class"


def test_resolve_finds_the_class_at_a_line_only_inside_the_class(nested_symbol_repo):
    d = nested_symbol_repo
    class_only_line = d["cls_range"][0]  # the class's own `class Greeter:` line -- not inside greet's range
    assert not (d["method_range"][0] <= class_only_line <= d["method_range"][1]), (
        "sanity: this line must NOT be inside greet's own range"
    )

    response = d["client"].get(
        f"/v1/repositories/{d['repo_id']}/resolve?path=main.py&line={class_only_line}", headers=d["headers"]
    )
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"]["name"] == "Greeter"


def test_resolve_returns_null_symbol_for_a_line_with_no_symbol(nested_symbol_repo):
    d = nested_symbol_repo
    # The class's own end_line covers its full body (including blank lines
    # between members), confirmed by test_resolve_prefers_the_narrowest_
    # symbol_at_a_line_inside_both/test_resolve_finds_the_class_at_a_line_
    # only_inside_the_class -- so a genuinely symbol-free line needs to be
    # past every symbol's range entirely, not merely "blank."
    no_symbol_line = max(d["cls_range"][1], d["method_range"][1]) + 100
    response = d["client"].get(
        f"/v1/repositories/{d['repo_id']}/resolve?path=main.py&line={no_symbol_line}", headers=d["headers"]
    )
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] is None
    assert body["file"] == "main.py"
    assert body["line"] == no_symbol_line


def test_resolve_returns_404_for_an_unknown_file_path(nested_symbol_repo):
    d = nested_symbol_repo
    response = d["client"].get(
        f"/v1/repositories/{d['repo_id']}/resolve?path=does_not_exist.py&line=1", headers=d["headers"]
    )
    assert response.status_code == 404


def test_resolve_is_forbidden_for_a_repository_the_key_is_not_scoped_to(tmp_path, shared_embedder, nested_symbol_repo):
    engine = nested_symbol_repo["engine"]
    _, other_result = _index_repo(tmp_path, "repo_resolve_other", shared_embedder, engine, SIMPLE_FILE)

    response = nested_symbol_repo["client"].get(
        f"/v1/repositories/{other_result['repository_id']}/resolve?path=main.py&line=1",
        headers=nested_symbol_repo["headers"],
    )
    assert response.status_code == 403


def test_resolve_requires_authentication(nested_symbol_repo):
    response = nested_symbol_repo["client"].get(
        f"/v1/repositories/{nested_symbol_repo['repo_id']}/resolve?path=main.py&line=1"
    )
    assert response.status_code == 401


# --- GET /v1/repositories/{repo_id}/file-symbols --------------------------
# Added alongside the VS Code extension's Phase 2 CodeLens provider: it
# needs every symbol in an open file (to place a lens above each one),
# not just the one at a specific line, and only knows the file's
# workspace-relative path -- not any internal file_id.

def test_file_symbols_lists_every_symbol_in_the_file_ordered_by_start_line(nested_symbol_repo):
    d = nested_symbol_repo
    response = d["client"].get(
        f"/v1/repositories/{d['repo_id']}/file-symbols?path=main.py", headers=d["headers"]
    )
    assert response.status_code == 200
    body = response.json()
    assert body["repository_id"] == d["repo_id"]
    assert body["file"] == "main.py"
    names = [s["name"] for s in body["symbols"]]
    assert names == ["Greeter", "greet"], "class must precede its own method (ordered by start_line)"

    greeter = body["symbols"][0]
    assert greeter["line_start"] == d["cls_range"][0]
    assert greeter["line_end"] == d["cls_range"][1]
    assert "symbol_id" in greeter and "type" in greeter and "complexity" in greeter


def test_file_symbols_returns_404_for_an_unknown_file_path(nested_symbol_repo):
    d = nested_symbol_repo
    response = d["client"].get(
        f"/v1/repositories/{d['repo_id']}/file-symbols?path=does_not_exist.py", headers=d["headers"]
    )
    assert response.status_code == 404


def test_file_symbols_is_forbidden_for_a_repository_the_key_is_not_scoped_to(
    tmp_path, shared_embedder, nested_symbol_repo
):
    engine = nested_symbol_repo["engine"]
    _, other_result = _index_repo(tmp_path, "repo_file_symbols_other", shared_embedder, engine, SIMPLE_FILE)

    response = nested_symbol_repo["client"].get(
        f"/v1/repositories/{other_result['repository_id']}/file-symbols?path=main.py",
        headers=nested_symbol_repo["headers"],
    )
    assert response.status_code == 403


def test_file_symbols_requires_authentication(nested_symbol_repo):
    response = nested_symbol_repo["client"].get(
        f"/v1/repositories/{nested_symbol_repo['repo_id']}/file-symbols?path=main.py"
    )
    assert response.status_code == 401
