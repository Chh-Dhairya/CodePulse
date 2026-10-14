"""
Phase 7.2 -- RBAC roles + repo permission model (roadmap §37 item 7.2;
§15's literal minimal-viable model: "roles `viewer` (search/read),
`indexer` (viewer + trigger indexing), `admin` (all + manage keys/
repos). Enforced at the repository-permission level, not globally.").

Implemented under the user's explicit continuous-execution
authorization (see CODEPULSE_PROGRESS.md's standing-deviation notice).

Architecture:
  - `role` lives on `APIKeyRepositoryModel` (the per-(key, repository)
    grant row), not on `APIKeyModel` -- a single key CAN hold different
    roles for different repositories, matching "not globally" literally.
  - `codepulse.auth.create_api_key(..., role="viewer")` -- the SAFE,
    least-privilege default for newly created grants.
  - `AuthorizedKey.authorize_role(repo_id, required_role)` -- the
    role-aware counterpart to the existing `authorize_repository`,
    reusing it for the membership check before applying the role
    ordering (`viewer` < `indexer` < `admin`).
  - The one existing endpoint where a role distinction is actually
    meaningful today: `POST /v1/repositories/index` now requires
    "indexer" (or higher) to RE-index an ALREADY-KNOWN repository (a
    key scoped there only as "viewer" gets 403); a genuinely NEW
    repository (no existing grant for anyone to check against) remains
    open to any authenticated key, unchanged from before this
    milestone -- there is nothing to check a not-yet-indexed path
    against, exactly the same rationale this endpoint's own Phase 1.2
    comment already documents for skipping `authorize_repository`
    there.
  - `tests/conftest.py::make_api_key_headers` defaults to `role="admin"`
    (NOT `create_api_key`'s own `"viewer"` default) specifically to
    preserve every pre-existing test's implicit unrestricted-key
    assumption -- only tests that actually exercise role boundaries
    (this file) pass `role=` explicitly.

Migration `b9014c049e33` (adds `api_key_repositories.role`, backfills
every existing grant row to `"admin"` -- the historically accurate
value, since no role concept existed to restrict any grant before this
migration) was verified via upgrade -> downgrade -> re-upgrade on a
disposable copy of the real `codepulse.db` before being applied there
(zero `api_keys`/`api_key_repositories` rows existed in the real
database, so the backfill had no real rows to affect) -- not
re-verified as a second automated test here, matching this project's
own established precedent of trusting a manually-verified migration
once applied, rather than duplicating that proof in the suite.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from codepulse.api.app import app
from codepulse.api.security import AuthorizedKey
from codepulse.auth import create_api_key
from codepulse.storage.db import APIKeyRepositoryModel, FileModel, RepositoryModel, SymbolModel
from tests.conftest import make_api_key_headers


@pytest.fixture
def client():
    return TestClient(app)


def _seed_repo_with_symbol(isolated_app, repo_name: str) -> tuple[int, int, int]:
    session = isolated_app.get_session()
    try:
        repo = RepositoryModel(name=repo_name, url=f"file:///{repo_name}")
        session.add(repo)
        session.flush()
        f = FileModel(repo_id=repo.id, path=f"{repo_name}/main.py", language="python")
        session.add(f)
        session.flush()
        sym = SymbolModel(
            file_id=f.id, name="hello", type="function", start_line=1, end_line=2,
            signature="def hello():", language="python",
        )
        session.add(sym)
        session.commit()
        return repo.id, f.id, sym.id
    finally:
        session.close()


# --- create_api_key / role validation --------------------------------------

def test_create_api_key_rejects_an_invalid_role(isolated_app):
    with pytest.raises(ValueError):
        create_api_key(isolated_app, name="k", repository_ids=[], role="superuser")


def test_create_api_key_defaults_to_viewer_role(isolated_app):
    repo_id, _, _ = _seed_repo_with_symbol(isolated_app, "repo_default_role")
    created = create_api_key(isolated_app, name="k", repository_ids=[repo_id])

    session = isolated_app.get_session()
    try:
        grant = session.query(APIKeyRepositoryModel).filter(
            APIKeyRepositoryModel.api_key_id == created.id
        ).first()
        assert grant.role == "viewer"
    finally:
        session.close()


def test_create_api_key_applies_the_given_role_to_every_granted_repository(isolated_app):
    repo_a, _, _ = _seed_repo_with_symbol(isolated_app, "repo_role_a")
    repo_b, _, _ = _seed_repo_with_symbol(isolated_app, "repo_role_b")
    created = create_api_key(isolated_app, name="k", repository_ids=[repo_a, repo_b], role="indexer")

    session = isolated_app.get_session()
    try:
        roles = {
            g.repo_id: g.role
            for g in session.query(APIKeyRepositoryModel).filter(
                APIKeyRepositoryModel.api_key_id == created.id
            ).all()
        }
        assert roles == {repo_a: "indexer", repo_b: "indexer"}
    finally:
        session.close()


# --- AuthorizedKey.authorize_role -------------------------------------------

def test_authorize_role_hierarchy_viewer_indexer_admin():
    key = AuthorizedKey(api_key_id=1, repository_ids=frozenset([1]), repository_roles={1: "indexer"})
    key.authorize_role(1, "viewer")    # indexer >= viewer -- must pass
    key.authorize_role(1, "indexer")   # indexer >= indexer -- must pass
    with pytest.raises(Exception):
        key.authorize_role(1, "admin")  # indexer < admin -- must reject


def test_authorize_role_rejects_a_repository_the_key_is_not_scoped_to_at_all():
    key = AuthorizedKey(api_key_id=1, repository_ids=frozenset(), repository_roles={})
    with pytest.raises(Exception):
        key.authorize_role(1, "viewer")


def test_authorize_role_is_independent_per_repository():
    """§15's own literal 'not globally' -- one key, two repositories, two different roles."""
    key = AuthorizedKey(
        api_key_id=1, repository_ids=frozenset([1, 2]),
        repository_roles={1: "viewer", 2: "admin"},
    )
    with pytest.raises(Exception):
        key.authorize_role(1, "indexer")
    key.authorize_role(2, "indexer")  # admin satisfies indexer on repo 2


# --- REST-level: indexer role required to re-index an EXISTING repository --

def test_viewer_role_key_is_forbidden_from_reindexing_an_existing_repository(client, isolated_app, tmp_path):
    repo_path = str(tmp_path / "existing_repo")
    os.makedirs(repo_path, exist_ok=True)
    session = isolated_app.get_session()
    try:
        repo = RepositoryModel(name="existing_repo", url=repo_path, file_count=0, symbol_count=0)
        session.add(repo)
        session.commit()
        repo_id = repo.id
    finally:
        session.close()

    viewer_headers = make_api_key_headers(isolated_app, repository_ids=[repo_id], role="viewer")
    response = client.post("/v1/repositories/index", json={"url": repo_path}, headers=viewer_headers)
    assert response.status_code == 403


def test_indexer_role_key_can_reindex_an_existing_repository(client, isolated_app, tmp_path):
    repo_path = str(tmp_path / "existing_repo_2")
    os.makedirs(repo_path, exist_ok=True)
    session = isolated_app.get_session()
    try:
        repo = RepositoryModel(name="existing_repo_2", url=repo_path, file_count=0, symbol_count=0)
        session.add(repo)
        session.commit()
        repo_id = repo.id
    finally:
        session.close()

    indexer_headers = make_api_key_headers(isolated_app, repository_ids=[repo_id], role="indexer")
    response = client.post("/v1/repositories/index", json={"url": repo_path}, headers=indexer_headers)
    assert response.status_code == 202


def test_a_key_with_no_grants_can_no_longer_create_a_brand_new_repository(client, isolated_app, tmp_path):
    """2026-10-14 (FUTURE_CHANGES item 18, user-approved): this test used to
    be `test_any_authenticated_key_can_still_create_a_brand_new_repository`
    and asserted that creating a repository stayed open to ANY authenticated
    key (Phase 1.2's rationale: no grant existed to check a not-yet-indexed
    path against). That left the creating key unable to poll its own job or
    see the repository, and let a viewer-only key create repositories.
    Creating one now needs `indexer` (or higher) on an existing repository;
    the full rule is tested in test_repository_creation_authorization.py."""
    repo_path = str(tmp_path / "brand_new_repo")
    os.makedirs(repo_path, exist_ok=True)
    headers = make_api_key_headers(isolated_app, repository_ids=[])
    response = client.post("/v1/repositories/index", json={"url": repo_path}, headers=headers)
    assert response.status_code == 403


def test_viewer_role_key_can_still_perform_read_operations(client, isolated_app):
    """A restricted "viewer" role must not accidentally break ordinary
    reads -- authorize_repository (used by read endpoints) is
    membership-only and every role satisfies it."""
    repo_id, file_id, symbol_id = _seed_repo_with_symbol(isolated_app, "repo_viewer_read")
    viewer_headers = make_api_key_headers(isolated_app, repository_ids=[repo_id], role="viewer")

    response = client.get(f"/v1/symbols/{symbol_id}", headers=viewer_headers)
    assert response.status_code == 200
