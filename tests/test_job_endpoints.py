"""
Phase 1.3 — IndexJob state machine + /v1/jobs/{id} (roadmap §37 item 1.3;
backlog B9: "Returned job_id is pollable to real completion").

Exercises the real HTTP surface end-to-end: POST /v1/repositories/index now
creates a real, DB-backed IndexJob row (replacing the previous fake
`job_id`), and GET /v1/jobs/{job_id} polls it through to actual completion.
Also covers the roadmap §8 "one active job per repository (advisory lock)"
concurrency check, and that the new job endpoint is authenticated/
authorized exactly like every other endpoint since Phase 1.2 (401/403/404
paths).

All tests use the shared `isolated_app` fixture (tests/conftest.py): a
temporary, disposable SQLite database, never the real codepulse.db.
codepulse.jobs's own state-machine logic is tested directly (not via HTTP)
in tests/test_index_jobs.py.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from codepulse.api.app import app
from codepulse.jobs import create_job
from codepulse.storage.db import RepositoryModel
from tests.conftest import make_api_key_headers, make_creator_headers


@pytest.fixture
def client():
    return TestClient(app)


def _make_repo_dir(tmp_path, name: str, files: dict[str, str] | None = None):
    repo_dir = tmp_path / name
    repo_dir.mkdir()
    for rel_path, content in (files or {}).items():
        (repo_dir / rel_path).write_text(content, encoding="utf-8")
    return repo_dir


# --- POST /v1/repositories/index now returns a real, pollable job -----------

def test_index_repository_returns_a_real_job_id_and_repository_id(client, isolated_app, tmp_path):
    repo_dir = _make_repo_dir(tmp_path, "repo1")
    headers = make_creator_headers(isolated_app)

    response = client.post("/v1/repositories/index", json={"url": str(repo_dir)}, headers=headers)
    assert response.status_code == 202
    data = response.json()

    assert data["job_id"].startswith("job_")
    assert isinstance(data["repository_id"], int)
    assert data["status"] == "queued"
    assert data["url"] == str(repo_dir)

    # The repository row must already exist (resolved up front, before
    # indexing runs) -- not just claimed in the response.
    session = isolated_app.get_session()
    try:
        repo = session.query(RepositoryModel).filter(RepositoryModel.id == data["repository_id"]).first()
        assert repo is not None
    finally:
        session.close()


def test_job_id_from_index_response_is_pollable_to_real_completion(client, isolated_app, tmp_path):
    """
    Core Phase 1.3 Definition of Done (backlog B9): "Returned job_id is
    pollable to real completion." FastAPI's TestClient runs BackgroundTasks
    synchronously before returning the response, so by the time this test
    polls, the job has already run to completion for real.
    """
    repo_dir = _make_repo_dir(tmp_path, "repo1", {"a.py": "def foo():\n    return 1\n"})
    headers = make_creator_headers(isolated_app)

    post_response = client.post("/v1/repositories/index", json={"url": str(repo_dir)}, headers=headers)
    job_id = post_response.json()["job_id"]
    repo_id = post_response.json()["repository_id"]

    # This specific key was created before the job existed, so it isn't
    # scoped to the (only just resolved) repository yet -- mint a new one
    # now that we know the real repo_id.
    scoped_headers = make_api_key_headers(isolated_app, repository_ids=[repo_id])

    get_response = client.get(f"/v1/jobs/{job_id}", headers=scoped_headers)
    assert get_response.status_code == 200
    data = get_response.json()

    assert data["job_id"] == job_id
    assert data["repository_id"] == repo_id
    assert data["status"] == "succeeded"
    assert data["started_at"] is not None
    assert data["finished_at"] is not None
    assert data["error"] is None
    assert data["result"] is not None
    assert data["result"]["total_files"] == 1
    assert data["result"]["repository_id"] == repo_id


def test_job_status_reflects_a_real_failure(client, isolated_app, tmp_path, monkeypatch):
    """
    A genuine failure inside index_directory() must transition the job to
    `failed` with the real error message -- not leave it stuck `running`
    or silently report success.
    """
    import codepulse.api.app as app_module

    def boom(*args, **kwargs):
        raise RuntimeError("simulated indexing failure")

    monkeypatch.setattr(app_module.indexing_engine, "index_directory", boom)

    repo_dir = _make_repo_dir(tmp_path, "repo1")
    headers = make_creator_headers(isolated_app)

    post_response = client.post("/v1/repositories/index", json={"url": str(repo_dir)}, headers=headers)
    job_id = post_response.json()["job_id"]
    repo_id = post_response.json()["repository_id"]

    scoped_headers = make_api_key_headers(isolated_app, repository_ids=[repo_id])
    get_response = client.get(f"/v1/jobs/{job_id}", headers=scoped_headers)
    assert get_response.status_code == 200
    data = get_response.json()

    assert data["status"] == "failed"
    assert "simulated indexing failure" in data["error"]
    assert data["result"] is None


# --- Advisory one-active-job-per-repository lock -----------------------------

def test_index_repository_rejects_a_second_request_while_a_job_is_active(client, isolated_app, tmp_path):
    """
    TestClient runs BackgroundTasks synchronously, so a job created via a
    real POST is already resolved by the time a second POST could arrive
    -- this simulates the in-flight state a real async deployment would
    have while a job is still running, by inserting the active job
    directly.
    """
    repo_dir = _make_repo_dir(tmp_path, "repo1")
    url = str(repo_dir)

    session = isolated_app.get_session()
    try:
        repo = RepositoryModel(name="repo1", url=url)
        session.add(repo)
        session.commit()
        repo_id = repo.id
    finally:
        session.close()

    create_job(isolated_app, repo_id)  # leaves it in "queued" -- still active

    # Phase 7.2 (RBAC): re-indexing a repository that ALREADY EXISTS now
    # requires at least "indexer" role scoped to it (§15) -- this test
    # is about the active-job conflict check specifically, so the key
    # is explicitly granted that role for the repo it's testing against
    # (a real operator's key would already be scoped this way before
    # attempting to re-index anything), not left unscoped as it was
    # before RBAC existed.
    headers = make_api_key_headers(isolated_app, repository_ids=[repo_id], role="indexer")
    response = client.post("/v1/repositories/index", json={"url": url}, headers=headers)
    assert response.status_code == 409


def test_index_repository_succeeds_again_once_the_prior_job_is_terminal(client, isolated_app, tmp_path):
    repo_dir = _make_repo_dir(tmp_path, "repo1")
    url = str(repo_dir)

    # Phase 7.2 (RBAC): the FIRST call below creates a genuinely NEW
    # repository (open to any authenticated key, unchanged) -- but the
    # SECOND call re-indexes that now-EXISTING repository, which
    # requires "indexer" role. Pre-creating the repository row and
    # granting the key that role up front keeps this test's own focus
    # (the job-terminal-state logic) unaffected by RBAC, matching a
    # real operator's actual key-provisioning order (scoped BEFORE
    # indexing, not granted implicitly by having created it).
    session = isolated_app.get_session()
    try:
        repo = RepositoryModel(name="repo1", url=url)
        session.add(repo)
        session.commit()
        repo_id = repo.id
    finally:
        session.close()

    headers = make_api_key_headers(isolated_app, repository_ids=[repo_id], role="indexer")

    first = client.post("/v1/repositories/index", json={"url": url}, headers=headers)
    assert first.status_code == 202
    # TestClient's synchronous BackgroundTasks execution means the first
    # job has already reached a terminal state (succeeded/failed) by now.

    second = client.post("/v1/repositories/index", json={"url": url}, headers=headers)
    assert second.status_code == 202
    assert second.json()["job_id"] != first.json()["job_id"]


# --- Authentication / authorization on GET /v1/jobs/{job_id} ----------------

def test_get_job_status_rejected_without_any_key(client, isolated_app, tmp_path):
    repo_dir = _make_repo_dir(tmp_path, "repo1")
    headers = make_creator_headers(isolated_app)
    post_response = client.post("/v1/repositories/index", json={"url": str(repo_dir)}, headers=headers)
    job_id = post_response.json()["job_id"]

    response = client.get(f"/v1/jobs/{job_id}")
    assert response.status_code == 401


def test_get_job_status_forbidden_for_a_key_scoped_to_a_different_repository(client, isolated_app, tmp_path):
    repo_a = _make_repo_dir(tmp_path, "repo-a")
    repo_b = _make_repo_dir(tmp_path, "repo-b")

    headers = make_creator_headers(isolated_app)
    post_a = client.post("/v1/repositories/index", json={"url": str(repo_a)}, headers=headers)
    job_id_a = post_a.json()["job_id"]

    post_b = client.post("/v1/repositories/index", json={"url": str(repo_b)}, headers=headers)
    repo_b_id = post_b.json()["repository_id"]

    # A key scoped only to repo-b must not be able to see repo-a's job.
    scoped_to_b = make_api_key_headers(isolated_app, repository_ids=[repo_b_id])
    response = client.get(f"/v1/jobs/{job_id_a}", headers=scoped_to_b)
    assert response.status_code == 403


def test_get_job_status_succeeds_for_a_correctly_scoped_key(client, isolated_app, tmp_path):
    repo_dir = _make_repo_dir(tmp_path, "repo1")
    headers = make_creator_headers(isolated_app)
    post_response = client.post("/v1/repositories/index", json={"url": str(repo_dir)}, headers=headers)
    job_id = post_response.json()["job_id"]
    repo_id = post_response.json()["repository_id"]

    scoped_headers = make_api_key_headers(isolated_app, repository_ids=[repo_id])
    response = client.get(f"/v1/jobs/{job_id}", headers=scoped_headers)
    assert response.status_code == 200


def test_get_job_status_returns_404_for_a_nonexistent_job(client, isolated_app):
    headers = make_api_key_headers(isolated_app)
    response = client.get("/v1/jobs/job_999999", headers=headers)
    assert response.status_code == 404


def test_get_job_status_returns_404_for_a_malformed_job_id(client, isolated_app):
    headers = make_api_key_headers(isolated_app)
    response = client.get("/v1/jobs/not-a-real-id", headers=headers)
    assert response.status_code == 404


def test_get_job_status_accepts_a_bare_integer_id(client, isolated_app, tmp_path):
    """GET /v1/jobs/{job_id} accepts both the "job_<n>" form and a bare integer."""
    repo_dir = _make_repo_dir(tmp_path, "repo1")
    headers = make_creator_headers(isolated_app)
    post_response = client.post("/v1/repositories/index", json={"url": str(repo_dir)}, headers=headers)
    job_id = post_response.json()["job_id"]
    repo_id = post_response.json()["repository_id"]
    bare_id = job_id.removeprefix("job_")

    scoped_headers = make_api_key_headers(isolated_app, repository_ids=[repo_id])
    response = client.get(f"/v1/jobs/{bare_id}", headers=scoped_headers)
    assert response.status_code == 200
    assert response.json()["job_id"] == job_id
