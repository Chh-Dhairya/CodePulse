"""
Backend-hardening addition (not a roadmap sprint item) -- fixes two real,
previously-undisclosed cross-repository data leaks found during a fresh
repo-gap recheck (the same class of bug Phase 3.4 and Phase 5.1 each
found and fixed for other endpoints):

  - `GET /v1/autocomplete`: a key scoped to specific repositories used to
    get suggestions blended from EVERY repository currently in memory,
    not just the ones it holds a grant for. Fixed by filtering on
    `auth.repository_ids` -- but ONLY when the key actually has grants.
    An UNSCOPED key's behavior is deliberately UNCHANGED (still blended
    across every repository) -- this endpoint has been a documented,
    tested "no repository concept" exemption since Phase 1.2
    (`tests/test_api_authorization.py`'s own module docstring), and
    reversing that for unscoped keys was an explicit, separate decision
    the user did not ask for this pass.
  - `GET /v1/repositories/{repo_id}/stats`: the route itself was already
    correctly gated (`auth.authorize_repository(repo_id)`), but its
    `languages` breakdown iterated `indexing_engine.parse_results`
    unfiltered -- a caller authorized for repo A, asking for repo A's
    own stats, got file/symbol/language counts blended with every OTHER
    repository this engine has ever parsed in-process. Fixed by
    filtering to the same `"{repo_id}::"` key-prefix scoping
    `IndexingEngine.analyze_module_structure` already established for
    this exact dict.

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


def _index_repo(tmp_path, dirname, embedder, db_name_or_engine, symbol_name: str):
    repo = tmp_path / dirname
    _init_repo(repo)
    _write(repo, "main.py", f'def {symbol_name}():\n    """A symbol unique to {dirname}."""\n    return 1\n')
    _commit_all(repo, "initial")
    engine = db_name_or_engine if isinstance(db_name_or_engine, IndexingEngine) else _new_engine(
        _db_url(tmp_path, db_name_or_engine), embedder
    )
    result = engine.index_directory(str(repo), repo_name=dirname, repo_url=str(repo))
    return engine, result


@pytest.fixture
def two_repos(tmp_path, shared_embedder):
    engine, r_a = _index_repo(tmp_path, "repo_a_iso", shared_embedder, "iso.db", "alpha_only_in_a")
    engine, r_b = _index_repo(tmp_path, "repo_b_iso", shared_embedder, engine, "alpha_only_in_b")

    app_module_storage = engine.storage
    return {
        "engine": engine,
        "repo_a_id": r_a["repository_id"],
        "repo_b_id": r_b["repository_id"],
        "storage": app_module_storage,
    }


@pytest.fixture
def client(two_repos, monkeypatch):
    monkeypatch.setattr(app_module, "storage_manager", two_repos["storage"])
    monkeypatch.setattr(app_module, "indexing_engine", two_repos["engine"])
    return TestClient(app)


# --- GET /v1/autocomplete -----------------------------------------------

def test_autocomplete_scoped_key_never_sees_a_different_repositorys_symbols(client, two_repos):
    headers = make_api_key_headers(two_repos["storage"], repository_ids=[two_repos["repo_a_id"]])

    response = client.get("/v1/autocomplete?prefix=alpha&limit=20", headers=headers)
    assert response.status_code == 200
    names = {s["name"] for s in response.json()["suggestions"]}

    assert "alpha_only_in_a" in names
    assert "alpha_only_in_b" not in names, (
        "a key scoped only to repo A must never see repo B's symbols in autocomplete suggestions"
    )


def test_autocomplete_scoped_key_sees_its_own_repositorys_symbols(client, two_repos):
    headers = make_api_key_headers(two_repos["storage"], repository_ids=[two_repos["repo_b_id"]])

    response = client.get("/v1/autocomplete?prefix=alpha&limit=20", headers=headers)
    assert response.status_code == 200
    names = {s["name"] for s in response.json()["suggestions"]}

    assert "alpha_only_in_b" in names
    assert "alpha_only_in_a" not in names


def test_autocomplete_key_scoped_to_both_repositories_sees_both(client, two_repos):
    headers = make_api_key_headers(
        two_repos["storage"], repository_ids=[two_repos["repo_a_id"], two_repos["repo_b_id"]]
    )

    response = client.get("/v1/autocomplete?prefix=alpha&limit=20", headers=headers)
    names = {s["name"] for s in response.json()["suggestions"]}
    assert names == {"alpha_only_in_a", "alpha_only_in_b"}


def test_autocomplete_unscoped_key_behavior_is_unchanged_blends_all_repos(client, two_repos):
    """Deliberately preserved, tested Phase 1.2 behavior -- an unscoped
    key is still treated as having "no repository concept" for this
    endpoint specifically, not a regression of the fix above."""
    headers = make_api_key_headers(two_repos["storage"], repository_ids=[])

    response = client.get("/v1/autocomplete?prefix=alpha&limit=20", headers=headers)
    assert response.status_code == 200
    names = {s["name"] for s in response.json()["suggestions"]}
    assert names == {"alpha_only_in_a", "alpha_only_in_b"}


# --- GET /v1/repositories/{repo_id}/stats --------------------------------

def test_repository_stats_languages_breakdown_never_leaks_another_repository(client, two_repos, tmp_path, shared_embedder):
    """The concrete leak: index a THIRD repository in a different
    language into the SAME shared engine, then confirm repo A's own
    stats response contains no trace of it."""
    engine = two_repos["engine"]
    repo_c = tmp_path / "repo_c_js_iso"
    _init_repo(repo_c)
    _write(repo_c, "main.js", 'function js_only_symbol() {\n  return 1;\n}\n')
    _commit_all(repo_c, "initial")
    engine.index_directory(str(repo_c), repo_name="repo_c_js_iso", repo_url=str(repo_c))

    headers = make_api_key_headers(two_repos["storage"], repository_ids=[two_repos["repo_a_id"]])
    response = client.get(f"/v1/repositories/{two_repos['repo_a_id']}/stats", headers=headers)
    assert response.status_code == 200
    body = response.json()

    assert body["repository_id"] == two_repos["repo_a_id"]
    assert set(body["languages"].keys()) == {"python"}, (
        "repo A's stats must never include repo C's javascript language entry"
    )
    assert body["languages"]["python"]["files"] == 1
    assert body["languages"]["python"]["symbols"] == 1


def test_repository_stats_languages_breakdown_is_correct_for_each_repository(client, two_repos):
    for key, repo_id in (("repo_a_id", two_repos["repo_a_id"]), ("repo_b_id", two_repos["repo_b_id"])):
        headers = make_api_key_headers(two_repos["storage"], repository_ids=[repo_id])
        response = client.get(f"/v1/repositories/{repo_id}/stats", headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["languages"] == {"python": {"files": 1, "symbols": 1}}


def test_repository_stats_still_requires_authorization_for_the_requested_repo(client, two_repos):
    headers = make_api_key_headers(two_repos["storage"], repository_ids=[two_repos["repo_a_id"]])
    response = client.get(f"/v1/repositories/{two_repos['repo_b_id']}/stats", headers=headers)
    assert response.status_code == 403
