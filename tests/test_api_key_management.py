"""
Backend-hardening addition -- API key management REST endpoints (not a
roadmap sprint item). Gives §15's `admin` role ("all + manage keys/
repos") its first real REST surface: before this, `create_api_key`/
`revoke_api_key` (codepulse.auth) were Python-function-only operations,
a disclosed gap since Phase 7.2 (CODEPULSE_PROGRESS.md's own "Known
Issues").

Endpoints under test:
  - POST   /v1/admin/keys        -- create a key
  - GET    /v1/admin/keys        -- list keys visible to the caller
  - DELETE /v1/admin/keys/{id}   -- revoke a key

Authorization model (see codepulse/api/app.py's own comment above these
routes for the full rationale): creating a key scoped to specific
repositories, or revoking a key holding any grant, both require the
caller to hold "admin" role on EVERY repository involved. An unscoped
key (no repository_ids) instead requires the caller to hold "admin" on
at least one repository somewhere (proving it is genuinely an
admin-role key).

Mirrors tests/test_rbac.py's own fixtures/style.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from codepulse.api.app import app
from codepulse.auth import verify_api_key
from codepulse.storage.db import RepositoryModel
from tests.conftest import make_api_key_headers


@pytest.fixture
def client():
    return TestClient(app)


def _make_repo(isolated_app, name: str) -> int:
    session = isolated_app.get_session()
    try:
        repo = RepositoryModel(name=name, url=f"file:///{name}")
        session.add(repo)
        session.commit()
        return repo.id
    finally:
        session.close()


# --- POST /v1/admin/keys -----------------------------------------------------

def test_admin_role_key_can_create_a_key_scoped_to_its_own_repository(client, isolated_app):
    repo_id = _make_repo(isolated_app, "repo_create_1")
    admin_headers = make_api_key_headers(isolated_app, repository_ids=[repo_id], role="admin")

    response = client.post(
        "/v1/admin/keys",
        json={"name": "new-key", "repository_ids": [repo_id], "role": "viewer"},
        headers=admin_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "new-key"
    assert body["repository_ids"] == [repo_id]
    assert body["role"] == "viewer"
    assert body["api_key"].startswith("cpk_")
    assert "key_hash" not in body

    # The returned raw key is real and usable.
    assert verify_api_key(isolated_app, body["api_key"]) == body["id"]


def test_viewer_role_key_cannot_create_a_key_on_its_repository(client, isolated_app):
    repo_id = _make_repo(isolated_app, "repo_create_2")
    viewer_headers = make_api_key_headers(isolated_app, repository_ids=[repo_id], role="viewer")

    response = client.post(
        "/v1/admin/keys",
        json={"name": "new-key", "repository_ids": [repo_id]},
        headers=viewer_headers,
    )
    assert response.status_code == 403


def test_indexer_role_key_cannot_create_a_key_on_its_repository(client, isolated_app):
    repo_id = _make_repo(isolated_app, "repo_create_3")
    indexer_headers = make_api_key_headers(isolated_app, repository_ids=[repo_id], role="indexer")

    response = client.post(
        "/v1/admin/keys",
        json={"name": "new-key", "repository_ids": [repo_id]},
        headers=indexer_headers,
    )
    assert response.status_code == 403


def test_cannot_create_a_key_scoped_to_a_repository_the_caller_has_no_grant_on(client, isolated_app):
    repo_id = _make_repo(isolated_app, "repo_create_4")
    other_repo_id = _make_repo(isolated_app, "repo_create_4_other")
    admin_headers = make_api_key_headers(isolated_app, repository_ids=[repo_id], role="admin")

    response = client.post(
        "/v1/admin/keys",
        json={"name": "new-key", "repository_ids": [other_repo_id]},
        headers=admin_headers,
    )
    assert response.status_code == 403


def test_admin_role_key_can_create_an_unscoped_key(client, isolated_app):
    repo_id = _make_repo(isolated_app, "repo_create_5")
    admin_headers = make_api_key_headers(isolated_app, repository_ids=[repo_id], role="admin")

    response = client.post(
        "/v1/admin/keys", json={"name": "unscoped-key", "repository_ids": []}, headers=admin_headers
    )
    assert response.status_code == 201
    assert response.json()["repository_ids"] == []


def test_key_without_admin_anywhere_cannot_create_an_unscoped_key(client, isolated_app):
    repo_id = _make_repo(isolated_app, "repo_create_6")
    indexer_headers = make_api_key_headers(isolated_app, repository_ids=[repo_id], role="indexer")

    response = client.post(
        "/v1/admin/keys", json={"name": "unscoped-key", "repository_ids": []}, headers=indexer_headers
    )
    assert response.status_code == 403


def test_create_key_rejects_an_invalid_role(client, isolated_app):
    repo_id = _make_repo(isolated_app, "repo_create_7")
    admin_headers = make_api_key_headers(isolated_app, repository_ids=[repo_id], role="admin")

    response = client.post(
        "/v1/admin/keys",
        json={"name": "bad-role-key", "repository_ids": [repo_id], "role": "superuser"},
        headers=admin_headers,
    )
    assert response.status_code == 400


def test_create_key_requires_authentication(client):
    response = client.post("/v1/admin/keys", json={"name": "x", "repository_ids": []})
    assert response.status_code == 401


# --- GET /v1/admin/keys ------------------------------------------------------

def test_list_keys_only_shows_keys_visible_to_the_callers_administered_repos(client, isolated_app):
    repo_a_id = _make_repo(isolated_app, "repo_list_a")
    repo_b_id = _make_repo(isolated_app, "repo_list_b")

    admin_a_headers = make_api_key_headers(isolated_app, repository_ids=[repo_a_id], role="admin")
    # A key visible only via repo_b -- must NOT appear for the repo_a admin.
    make_api_key_headers(isolated_app, name="key-b", repository_ids=[repo_b_id], role="admin")
    # A key visible via repo_a -- must appear.
    created_a = client.post(
        "/v1/admin/keys",
        json={"name": "key-a-visible", "repository_ids": [repo_a_id], "role": "viewer"},
        headers=admin_a_headers,
    ).json()

    response = client.get("/v1/admin/keys", headers=admin_a_headers)
    assert response.status_code == 200
    keys = response.json()["keys"]
    key_names = {k["name"] for k in keys}
    assert "key-a-visible" in key_names
    assert "key-b" not in key_names

    listed = next(k for k in keys if k["id"] == created_a["id"])
    assert listed["grants"] == [{"repository_id": repo_a_id, "role": "viewer"}]
    assert "api_key" not in listed
    assert "key_hash" not in listed


def test_list_keys_never_discloses_a_visible_keys_grants_on_repos_the_caller_does_not_administer(
    client, isolated_app
):
    """The exact isolation property this endpoint exists to preserve: a
    key visible because it shares repo_a with the caller may ALSO be
    granted on repo_c, which the caller has no rights over -- that grant
    must never be disclosed to this caller."""
    repo_a_id = _make_repo(isolated_app, "repo_grants_a")
    repo_c_id = _make_repo(isolated_app, "repo_grants_c")

    admin_a_headers = make_api_key_headers(isolated_app, repository_ids=[repo_a_id], role="admin")
    admin_both_headers = make_api_key_headers(
        isolated_app, repository_ids=[repo_a_id, repo_c_id], role="admin"
    )
    created = client.post(
        "/v1/admin/keys",
        json={"name": "multi-repo-key", "repository_ids": [repo_a_id, repo_c_id], "role": "viewer"},
        headers=admin_both_headers,
    ).json()

    response = client.get("/v1/admin/keys", headers=admin_a_headers)
    listed = next(k for k in response.json()["keys"] if k["id"] == created["id"])
    assert listed["grants"] == [{"repository_id": repo_a_id, "role": "viewer"}], (
        "the repo_c grant must be filtered out for a caller who only administers repo_a"
    )


def test_list_keys_requires_admin_role_somewhere(client, isolated_app):
    repo_id = _make_repo(isolated_app, "repo_list_viewer")
    viewer_headers = make_api_key_headers(isolated_app, repository_ids=[repo_id], role="viewer")

    response = client.get("/v1/admin/keys", headers=viewer_headers)
    assert response.status_code == 403


def test_list_keys_requires_authentication(client):
    response = client.get("/v1/admin/keys")
    assert response.status_code == 401


# --- DELETE /v1/admin/keys/{id} ----------------------------------------------

def test_admin_role_key_can_revoke_a_key_on_its_own_repository(client, isolated_app):
    repo_id = _make_repo(isolated_app, "repo_revoke_1")
    admin_headers = make_api_key_headers(isolated_app, repository_ids=[repo_id], role="admin")
    created = client.post(
        "/v1/admin/keys",
        json={"name": "to-revoke", "repository_ids": [repo_id], "role": "viewer"},
        headers=admin_headers,
    ).json()

    response = client.delete(f"/v1/admin/keys/{created['id']}", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["revoked_at"] is not None

    # The revoked key must genuinely stop authenticating.
    assert verify_api_key(isolated_app, created["api_key"]) is None


def test_revoking_a_key_requires_admin_on_every_repository_it_is_granted_to(client, isolated_app):
    repo_a_id = _make_repo(isolated_app, "repo_revoke_a")
    repo_b_id = _make_repo(isolated_app, "repo_revoke_b")
    admin_both_headers = make_api_key_headers(
        isolated_app, repository_ids=[repo_a_id, repo_b_id], role="admin"
    )
    admin_a_only_headers = make_api_key_headers(
        isolated_app, name="admin-a-only", repository_ids=[repo_a_id], role="admin"
    )
    created = client.post(
        "/v1/admin/keys",
        json={"name": "spans-two-repos", "repository_ids": [repo_a_id, repo_b_id], "role": "viewer"},
        headers=admin_both_headers,
    ).json()

    # A caller who only administers repo_a must NOT be able to revoke a
    # key that also has access to repo_b -- would silently strip access
    # from a repository this caller has no rights over.
    response = client.delete(f"/v1/admin/keys/{created['id']}", headers=admin_a_only_headers)
    assert response.status_code == 403

    # The caller who administers BOTH repositories can.
    response = client.delete(f"/v1/admin/keys/{created['id']}", headers=admin_both_headers)
    assert response.status_code == 200


def test_revoke_unscoped_key_requires_admin_role_somewhere(client, isolated_app):
    repo_id = _make_repo(isolated_app, "repo_revoke_unscoped")
    admin_headers = make_api_key_headers(isolated_app, repository_ids=[repo_id], role="admin")
    indexer_headers = make_api_key_headers(
        isolated_app, name="indexer-key", repository_ids=[repo_id], role="indexer"
    )
    created = client.post(
        "/v1/admin/keys", json={"name": "unscoped-to-revoke", "repository_ids": []}, headers=admin_headers
    ).json()

    forbidden = client.delete(f"/v1/admin/keys/{created['id']}", headers=indexer_headers)
    assert forbidden.status_code == 403

    ok = client.delete(f"/v1/admin/keys/{created['id']}", headers=admin_headers)
    assert ok.status_code == 200


def test_revoke_returns_404_for_an_unknown_key(client, isolated_app):
    repo_id = _make_repo(isolated_app, "repo_revoke_404")
    admin_headers = make_api_key_headers(isolated_app, repository_ids=[repo_id], role="admin")

    response = client.delete("/v1/admin/keys/999999", headers=admin_headers)
    assert response.status_code == 404


def test_revoke_is_idempotent_at_the_rest_level(client, isolated_app):
    repo_id = _make_repo(isolated_app, "repo_revoke_idem")
    admin_headers = make_api_key_headers(isolated_app, repository_ids=[repo_id], role="admin")
    created = client.post(
        "/v1/admin/keys",
        json={"name": "revoke-twice", "repository_ids": [repo_id]},
        headers=admin_headers,
    ).json()

    first = client.delete(f"/v1/admin/keys/{created['id']}", headers=admin_headers)
    second = client.delete(f"/v1/admin/keys/{created['id']}", headers=admin_headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["revoked_at"] == second.json()["revoked_at"]


def test_revoke_requires_authentication(client):
    response = client.delete("/v1/admin/keys/1")
    assert response.status_code == 401
