"""
Who may create a repository, and what the creator gets
(CODEPULSE_FUTURE_CHANGES.md item 18; user-approved option (a), 2026-10-14).

Found by the real-VS-Code tests: `POST /v1/repositories/index` let ANY valid
key create a brand-new repository (the role check only ran when the repository
already existed), but `GET /v1/jobs/{id}` needs a grant on the job's
repository, which the creating key did not have. So the VS Code extension's
Index Workspace on a never-indexed folder ended with "lost track of indexing
job ... API key is not scoped to repository N", and the key could never see
the repository it had just created. Even a viewer-only key could create
repositories.

Rules (owner semantics):
  - Creating a NEW repository requires the `indexer` role (or higher) on at
    least one EXISTING repository. A viewer-only key, or a key with no grants
    at all, gets 403 and nothing is created.
  - The repository row and the creator's `admin` grant on it are written in
    ONE transaction, and only the call that actually created the row gets the
    grant: a second key indexing the same new URL does not inherit admin on
    someone else's repository (it gets the normal "existing repository" role
    check).
  - Authorization is decided BEFORE the path is validated, so a key that may
    not create repositories cannot probe which paths exist (403, never 400).
  - Indexing an EXISTING repository is unchanged.

Temporary databases only (the shared `isolated_app` fixture).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from codepulse.api.app import app
from codepulse.auth import create_repository_owned_by
from codepulse.storage.db import APIKeyRepositoryModel, RepositoryModel
from tests.conftest import make_api_key_headers


@pytest.fixture
def client():
    return TestClient(app)


def _seed_repo(storage, name):
    session = storage.get_session()
    try:
        repo = RepositoryModel(name=name, url=f"file:///seed/{name}")
        session.add(repo)
        session.commit()
        return repo.id
    finally:
        session.close()


def _repo_dir(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    (path / "mod.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    return path


def _repo_count(storage):
    session = storage.get_session()
    try:
        return session.query(RepositoryModel).count()
    finally:
        session.close()


def _grants(storage, key_id):
    session = storage.get_session()
    try:
        return {
            g.repo_id: g.role
            for g in session.query(APIKeyRepositoryModel).filter(APIKeyRepositoryModel.api_key_id == key_id)
        }
    finally:
        session.close()


def _key_id(storage, headers):
    from codepulse.auth import verify_api_key

    return verify_api_key(storage, headers["X-API-Key"])


# --- who may create ---------------------------------------------------------

@pytest.mark.parametrize("role", ["indexer", "admin"])
def test_a_key_with_indexer_or_higher_on_an_existing_repository_may_create_a_new_one(client, isolated_app, tmp_path, role):
    anchor = _seed_repo(isolated_app, "anchor")
    headers = make_api_key_headers(isolated_app, repository_ids=[anchor], role=role)
    repo_dir = _repo_dir(tmp_path, "brand_new")

    response = client.post("/v1/repositories/index", json={"url": str(repo_dir)}, headers=headers)

    assert response.status_code == 202, response.text


def test_a_viewer_only_key_cannot_create_a_repository_and_nothing_is_created(client, isolated_app, tmp_path):
    anchor = _seed_repo(isolated_app, "anchor")
    headers = make_api_key_headers(isolated_app, repository_ids=[anchor], role="viewer")
    repo_dir = _repo_dir(tmp_path, "brand_new")
    before = _repo_count(isolated_app)

    response = client.post("/v1/repositories/index", json={"url": str(repo_dir)}, headers=headers)

    assert response.status_code == 403
    assert "indexer" in response.json()["error"]["message"]
    assert _repo_count(isolated_app) == before


def test_a_key_with_no_grants_cannot_create_a_repository(client, isolated_app, tmp_path):
    headers = make_api_key_headers(isolated_app)  # unscoped: valid, but grants nothing
    repo_dir = _repo_dir(tmp_path, "brand_new")

    response = client.post("/v1/repositories/index", json={"url": str(repo_dir)}, headers=headers)

    assert response.status_code == 403
    assert _repo_count(isolated_app) == 0


# --- what the creator gets --------------------------------------------------

def test_the_creator_becomes_admin_of_the_new_repository_and_keeps_its_other_grants(client, isolated_app, tmp_path):
    anchor = _seed_repo(isolated_app, "anchor")
    headers = make_api_key_headers(isolated_app, repository_ids=[anchor], role="indexer")
    repo_dir = _repo_dir(tmp_path, "brand_new")

    body = client.post("/v1/repositories/index", json={"url": str(repo_dir)}, headers=headers).json()

    grants = _grants(isolated_app, _key_id(isolated_app, headers))
    assert grants == {anchor: "indexer", body["repository_id"]: "admin"}


def test_the_creating_key_can_follow_its_job_to_completion_and_then_see_the_repository(client, isolated_app, tmp_path):
    """The exact flow the VS Code extension runs for Index Workspace on a new folder."""
    anchor = _seed_repo(isolated_app, "anchor")
    headers = make_api_key_headers(isolated_app, repository_ids=[anchor], role="indexer")
    repo_dir = _repo_dir(tmp_path, "brand_new")

    job = client.post("/v1/repositories/index", json={"url": str(repo_dir)}, headers=headers).json()
    status = client.get(f"/v1/jobs/{job['job_id']}", headers=headers)

    assert status.status_code == 200, status.text
    assert status.json()["status"] == "succeeded"
    listed = client.get("/v1/repositories", headers=headers).json()["repositories"]
    assert job["repository_id"] in [r["repository_id"] for r in listed]


def test_the_creator_really_is_admin_it_can_mint_keys_for_the_new_repository(client, isolated_app, tmp_path):
    anchor = _seed_repo(isolated_app, "anchor")
    headers = make_api_key_headers(isolated_app, repository_ids=[anchor], role="indexer")
    repo_dir = _repo_dir(tmp_path, "brand_new")
    new_id = client.post("/v1/repositories/index", json={"url": str(repo_dir)}, headers=headers).json()["repository_id"]

    response = client.post(
        "/v1/admin/keys", json={"name": "for-new-repo", "repository_ids": [new_id], "role": "viewer"}, headers=headers
    )

    assert response.status_code == 201, response.text


# --- someone else's repository ---------------------------------------------

def test_a_second_key_indexing_the_same_new_url_does_not_inherit_admin(client, isolated_app, tmp_path):
    anchor_a = _seed_repo(isolated_app, "anchor_a")
    anchor_b = _seed_repo(isolated_app, "anchor_b")
    first = make_api_key_headers(isolated_app, repository_ids=[anchor_a], role="indexer", name="first")
    second = make_api_key_headers(isolated_app, repository_ids=[anchor_b], role="indexer", name="second")
    repo_dir = _repo_dir(tmp_path, "shared")
    new_id = client.post("/v1/repositories/index", json={"url": str(repo_dir)}, headers=first).json()["repository_id"]

    response = client.post("/v1/repositories/index", json={"url": str(repo_dir)}, headers=second)

    assert response.status_code == 403  # the normal existing-repository role check
    assert new_id not in _grants(isolated_app, _key_id(isolated_app, second))


def test_losing_a_creation_race_gives_no_grant(isolated_app):
    anchor_a = _seed_repo(isolated_app, "anchor_a")
    anchor_b = _seed_repo(isolated_app, "anchor_b")
    first = _key_id(isolated_app, make_api_key_headers(isolated_app, repository_ids=[anchor_a], role="indexer", name="first"))
    second = _key_id(isolated_app, make_api_key_headers(isolated_app, repository_ids=[anchor_b], role="indexer", name="second"))

    repo_id, created = create_repository_owned_by(isolated_app, "shared", "file:///shared", first)
    same_id, created_again = create_repository_owned_by(isolated_app, "shared", "file:///shared", second)

    assert created is True and created_again is False
    assert same_id == repo_id
    assert _grants(isolated_app, first)[repo_id] == "admin"
    assert repo_id not in _grants(isolated_app, second)


# --- ordering and unchanged behavior ---------------------------------------

def test_a_key_that_may_not_create_repositories_cannot_probe_which_paths_exist(client, isolated_app, tmp_path):
    anchor = _seed_repo(isolated_app, "anchor")
    headers = make_api_key_headers(isolated_app, repository_ids=[anchor], role="viewer")

    response = client.post("/v1/repositories/index", json={"url": str(tmp_path / "does_not_exist")}, headers=headers)

    assert response.status_code == 403, "must be 403, not a 400 that reveals the path does not exist"


def test_an_authorized_creator_still_gets_a_400_for_a_nonexistent_path_and_creates_nothing(client, isolated_app, tmp_path):
    anchor = _seed_repo(isolated_app, "anchor")
    headers = make_api_key_headers(isolated_app, repository_ids=[anchor], role="indexer")
    before = _repo_count(isolated_app)

    response = client.post("/v1/repositories/index", json={"url": str(tmp_path / "does_not_exist")}, headers=headers)

    assert response.status_code == 400
    assert _repo_count(isolated_app) == before


def test_reindexing_an_existing_repository_is_unchanged(client, isolated_app, tmp_path):
    repo_dir = _repo_dir(tmp_path, "existing")
    session = isolated_app.get_session()
    try:
        repo = RepositoryModel(name="existing", url=str(repo_dir))
        session.add(repo)
        session.commit()
        existing_id = repo.id
    finally:
        session.close()
    indexer = make_api_key_headers(isolated_app, repository_ids=[existing_id], role="indexer", name="indexer")
    viewer = make_api_key_headers(isolated_app, repository_ids=[existing_id], role="viewer", name="viewer")

    assert client.post("/v1/repositories/index", json={"url": str(repo_dir)}, headers=viewer).status_code == 403
    ok = client.post("/v1/repositories/index", json={"url": str(repo_dir)}, headers=indexer)
    assert ok.status_code == 202
    assert ok.json()["repository_id"] == existing_id
    assert _grants(isolated_app, _key_id(isolated_app, indexer)) == {existing_id: "indexer"}, "no grant change on re-index"
