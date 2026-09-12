"""
Phase 1.2 — Central authorization dependency (roadmap §37 item 1.2; B8 DoD:
"No endpoint callable without a valid, scoped key").

Exercises `codepulse.api.security.require_api_key`/`AuthorizedKey` against
the real FastAPI `app`, end-to-end via real HTTP requests (not by
inspecting source), covering every distinct outcome the roadmap's
Definition of Done requires:
  - missing / unknown / revoked key -> 401
  - valid key, but not scoped to the target repository -> 403
  - valid key, correctly scoped -> 200 (the success path is preserved, not
    just the rejection path)
  - a None/omitted repository_id on a search-style endpoint can never be
    authorized against a scoped key -> 403 (closes the exact
    "search ignores repository_id" cross-repo leakage class §30 names as
    this milestone's reason to exist)
  - repository isolation is not weakened: a key scoped to repository A gets
    403 -- never repository B's content -- when it targets repository B
  - endpoints with no repository concept (autocomplete, feedback, the
    ingestion endpoint) succeed with an unscoped key -- proving they don't
    spuriously require a scope that doesn't apply to them, matching the
    documented rationale in codepulse/api/app.py for each

All tests use the shared `isolated_app` fixture (tests/conftest.py): a
temporary, disposable SQLite database, never the real codepulse.db.

Phase 1.7 (roadmap §37 item 1.7, "Security test suite"; §20 Testing
Strategy's "unauthenticated request rejected" category) added the final
section below, closing the one remaining gap: nine endpoints that were
already correctly rejecting a keyless request (via the same
`Depends(require_api_key)` every route already carries) had no
endpoint-specific test proving it. No production behavior changed --
this only adds the missing proof. The other three §20 security-test
categories (path traversal / SSRF / oversized-repo rejected) were already
fully covered by Phase 1.5's `tests/test_ingestion_safety.py` and
`tests/test_ingestion_safety_endpoint.py`, so Phase 1.7 does not duplicate
them here.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from codepulse.api.app import app
from codepulse.auth import create_api_key
from codepulse.storage.db import APIKeyModel, FileModel, RepositoryModel, SymbolModel
from tests.conftest import make_api_key_headers


@pytest.fixture
def client():
    return TestClient(app)


def _seed_repo_with_symbol(isolated_app, repo_name: str) -> tuple[int, int, int]:
    """Seeds one Repository + one File + one Symbol; returns (repo_id, file_id, symbol_id)."""
    session = isolated_app.get_session()
    try:
        repo = RepositoryModel(name=repo_name, url=f"file:///{repo_name}")
        session.add(repo)
        session.flush()

        f = FileModel(repo_id=repo.id, path=f"{repo_name}/main.py", language="python")
        session.add(f)
        session.flush()

        sym = SymbolModel(
            file_id=f.id,
            name="hello",
            type="function",
            start_line=1,
            end_line=2,
            signature="def hello():",
            language="python",
        )
        session.add(sym)
        session.commit()
        return repo.id, f.id, sym.id
    finally:
        session.close()


# --- Missing / unknown / revoked key -> 401 ----------------------------------

def test_missing_api_key_is_rejected(client, isolated_app):
    response = client.get("/v1/repositories/1/index-status")
    assert response.status_code == 401


def test_empty_api_key_header_is_rejected(client, isolated_app):
    response = client.get("/v1/repositories/1/index-status", headers={"X-API-Key": ""})
    assert response.status_code == 401


def test_unknown_api_key_is_rejected(client, isolated_app):
    response = client.get(
        "/v1/repositories/1/index-status", headers={"X-API-Key": "cpk_this-was-never-created"}
    )
    assert response.status_code == 401


def test_revoked_api_key_is_rejected(client, isolated_app):
    repo_id, _, _ = _seed_repo_with_symbol(isolated_app, "r1")
    created = create_api_key(isolated_app, name="to-revoke", repository_ids=[repo_id])

    session = isolated_app.get_session()
    try:
        row = session.query(APIKeyModel).filter(APIKeyModel.id == created.id).first()
        row.revoked_at = datetime.utcnow()
        session.commit()
    finally:
        session.close()

    response = client.get(
        f"/v1/repositories/{repo_id}/index-status", headers={"X-API-Key": created.raw_key}
    )
    assert response.status_code == 401


# --- Valid key, wrong/no scope -> 403 ----------------------------------------

def test_valid_key_without_any_scope_gets_403_on_repository_endpoint(client, isolated_app):
    repo_id, _, _ = _seed_repo_with_symbol(isolated_app, "r1")
    headers = make_api_key_headers(isolated_app)  # unscoped: no repository grants at all

    response = client.get(f"/v1/repositories/{repo_id}/index-status", headers=headers)
    assert response.status_code == 403


def test_key_scoped_to_a_different_repository_gets_403_on_index_status(client, isolated_app):
    repo_a, _, _ = _seed_repo_with_symbol(isolated_app, "repo-a")
    repo_b, _, _ = _seed_repo_with_symbol(isolated_app, "repo-b")
    headers = make_api_key_headers(isolated_app, repository_ids=[repo_a])

    response = client.get(f"/v1/repositories/{repo_b}/index-status", headers=headers)
    assert response.status_code == 403


def test_key_scoped_to_a_different_repository_gets_403_on_stats(client, isolated_app):
    repo_a, _, _ = _seed_repo_with_symbol(isolated_app, "repo-a")
    repo_b, _, _ = _seed_repo_with_symbol(isolated_app, "repo-b")
    headers = make_api_key_headers(isolated_app, repository_ids=[repo_a])

    response = client.get(f"/v1/repositories/{repo_b}/stats", headers=headers)
    assert response.status_code == 403


# --- Valid, correctly-scoped key -> success path preserved ------------------

def test_correctly_scoped_key_succeeds_on_index_status(client, isolated_app):
    repo_id, _, _ = _seed_repo_with_symbol(isolated_app, "r1")
    headers = make_api_key_headers(isolated_app, repository_ids=[repo_id])

    response = client.get(f"/v1/repositories/{repo_id}/index-status", headers=headers)
    assert response.status_code == 200
    assert response.json()["repository_id"] == repo_id


def test_correctly_scoped_key_succeeds_on_stats(client, isolated_app):
    repo_id, _, _ = _seed_repo_with_symbol(isolated_app, "r1")
    headers = make_api_key_headers(isolated_app, repository_ids=[repo_id])

    response = client.get(f"/v1/repositories/{repo_id}/stats", headers=headers)
    assert response.status_code == 200


# --- Symbol/file endpoints resolve their owning repository and enforce scope

def test_symbol_details_authorized_for_scoped_repository(client, isolated_app):
    repo_id, _, symbol_id = _seed_repo_with_symbol(isolated_app, "r1")
    headers = make_api_key_headers(isolated_app, repository_ids=[repo_id])

    response = client.get(f"/v1/symbols/{symbol_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["name"] == "hello"


def test_symbol_details_forbidden_when_key_scoped_to_a_different_repository(client, isolated_app):
    repo_a, _, symbol_id = _seed_repo_with_symbol(isolated_app, "repo-a")
    repo_b, _, _ = _seed_repo_with_symbol(isolated_app, "repo-b")
    headers = make_api_key_headers(isolated_app, repository_ids=[repo_b])  # scoped to the OTHER repo

    response = client.get(f"/v1/symbols/{symbol_id}", headers=headers)
    assert response.status_code == 403


def test_symbol_not_found_returns_404_even_for_an_unscoped_key(client, isolated_app):
    """A genuinely nonexistent symbol is a 404 before any scope check is even possible."""
    headers = make_api_key_headers(isolated_app)
    response = client.get("/v1/symbols/999999", headers=headers)
    assert response.status_code == 404


def test_file_symbols_authorized_for_scoped_repository(client, isolated_app):
    repo_id, file_id, _ = _seed_repo_with_symbol(isolated_app, "r1")
    headers = make_api_key_headers(isolated_app, repository_ids=[repo_id])

    response = client.get(f"/v1/files/{file_id}/symbols", headers=headers)
    assert response.status_code == 200


def test_file_symbols_forbidden_when_key_scoped_to_a_different_repository(client, isolated_app):
    repo_a, file_id, _ = _seed_repo_with_symbol(isolated_app, "repo-a")
    repo_b, _, _ = _seed_repo_with_symbol(isolated_app, "repo-b")
    headers = make_api_key_headers(isolated_app, repository_ids=[repo_b])

    response = client.get(f"/v1/files/{file_id}/symbols", headers=headers)
    assert response.status_code == 403


def test_symbol_similar_forbidden_when_key_scoped_to_a_different_repository(client, isolated_app):
    """
    Proves the authorization check runs -- and rejects -- before any real
    embedding-model work happens inside find_similar_code: this failure
    path never needs to load the (slow) real embedder.
    """
    repo_a, _, symbol_id = _seed_repo_with_symbol(isolated_app, "repo-a")
    repo_b, _, _ = _seed_repo_with_symbol(isolated_app, "repo-b")
    headers = make_api_key_headers(isolated_app, repository_ids=[repo_b])

    response = client.post(f"/v1/symbols/{symbol_id}/similar", json={}, headers=headers)
    assert response.status_code == 403


def test_symbol_impact_analysis_forbidden_when_key_scoped_to_a_different_repository(client, isolated_app):
    repo_a, _, symbol_id = _seed_repo_with_symbol(isolated_app, "repo-a")
    repo_b, _, _ = _seed_repo_with_symbol(isolated_app, "repo-b")
    headers = make_api_key_headers(isolated_app, repository_ids=[repo_b])

    response = client.post(f"/v1/symbols/{symbol_id}/impact-analysis", json={}, headers=headers)
    assert response.status_code == 403


def test_symbol_within_search_forbidden_when_key_scoped_to_a_different_repository(client, isolated_app):
    repo_a, _, symbol_id = _seed_repo_with_symbol(isolated_app, "repo-a")
    repo_b, _, _ = _seed_repo_with_symbol(isolated_app, "repo-b")
    headers = make_api_key_headers(isolated_app, repository_ids=[repo_b])

    response = client.post(f"/v1/symbols/{symbol_id}/search?query=hello", headers=headers)
    assert response.status_code == 403


def test_symbol_within_search_succeeds_for_scoped_repository(client, isolated_app):
    repo_id, _, symbol_id = _seed_repo_with_symbol(isolated_app, "r1")
    headers = make_api_key_headers(isolated_app, repository_ids=[repo_id])

    response = client.post(f"/v1/symbols/{symbol_id}/search?query=hello", headers=headers)
    assert response.status_code == 200
    assert response.json()["found"] is True


# --- Search: an omitted/None repository_id can never be authorized ----------

def test_search_with_explicit_null_repository_id_is_forbidden(client, isolated_app):
    repo_id, _, _ = _seed_repo_with_symbol(isolated_app, "r1")
    headers = make_api_key_headers(isolated_app, repository_ids=[repo_id])

    response = client.post(
        "/v1/search", json={"query": "hello", "repository_id": None}, headers=headers
    )
    assert response.status_code == 403


def test_ide_search_with_explicit_null_repository_id_is_forbidden(client, isolated_app):
    repo_id, _, _ = _seed_repo_with_symbol(isolated_app, "r1")
    headers = make_api_key_headers(isolated_app, repository_ids=[repo_id])

    response = client.post(
        "/v1/ide-search",
        json={"selected_text": "hello", "repository_id": None},
        headers=headers,
    )
    assert response.status_code == 403


def test_search_forbidden_for_a_repository_outside_the_keys_scope(client, isolated_app):
    repo_a, _, _ = _seed_repo_with_symbol(isolated_app, "repo-a")
    repo_b, _, _ = _seed_repo_with_symbol(isolated_app, "repo-b")
    headers = make_api_key_headers(isolated_app, repository_ids=[repo_a])

    response = client.post(
        "/v1/search", json={"query": "hello", "repository_id": repo_b}, headers=headers
    )
    assert response.status_code == 403


def test_search_scoped_correctly_succeeds(client, isolated_app):
    repo_id, _, _ = _seed_repo_with_symbol(isolated_app, "r1")
    headers = make_api_key_headers(isolated_app, repository_ids=[repo_id])

    response = client.post(
        "/v1/search", json={"query": "hello", "repository_id": repo_id}, headers=headers
    )
    assert response.status_code == 200


# --- Endpoints with no repository concept: auth-only, unscoped key works ---

def test_autocomplete_succeeds_with_an_unscoped_key(client, isolated_app):
    headers = make_api_key_headers(isolated_app)
    response = client.get("/v1/autocomplete?prefix=h", headers=headers)
    assert response.status_code == 200


def test_autocomplete_rejected_without_any_key(client, isolated_app):
    response = client.get("/v1/autocomplete?prefix=h")
    assert response.status_code == 401


def test_feedback_succeeds_with_an_unscoped_key(client, isolated_app):
    headers = make_api_key_headers(isolated_app)
    response = client.post(
        "/v1/search/feedback",
        json={"search_id": "s1", "result_id": 1, "feedback": "relevant"},
        headers=headers,
    )
    assert response.status_code == 200


def test_index_repository_succeeds_with_an_unscoped_key(client, isolated_app, tmp_path):
    headers = make_api_key_headers(isolated_app)
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    response = client.post(
        "/v1/repositories/index", json={"url": str(empty_dir)}, headers=headers
    )
    assert response.status_code == 202


def test_index_repository_rejected_without_any_key(client, isolated_app, tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    response = client.post("/v1/repositories/index", json={"url": str(empty_dir)})
    assert response.status_code == 401


# --- Phase 1.7 / roadmap §20, §36: "unauthenticated request rejected" --------
# closes the remaining endpoint-level gap -- every endpoint above this
# section already had at least one no-key test tied to its own scope/
# success coverage; these nine did not yet have one tied specifically to
# the no-key case, even though `Depends(require_api_key)` is already wired
# into every route (Phase 1.2) and already exhaustively unit-tested on its
# own (the "Missing / unknown / revoked key" section at the top of this
# file). This section proves it holds for every remaining route too --
# not a new behavior, a missing endpoint-level proof of an existing one.
# Requests use syntactically valid bodies (what a real, correctly-shaped
# call would send) so the 401 is unambiguously attributable to the missing
# key alone, not to some other validation failure.

def test_search_rejected_without_any_key(client, isolated_app):
    response = client.post("/v1/search", json={"query": "hello"})
    assert response.status_code == 401


def test_symbol_details_rejected_without_any_key(client, isolated_app):
    response = client.get("/v1/symbols/1")
    assert response.status_code == 401


def test_symbol_similar_rejected_without_any_key(client, isolated_app):
    response = client.post("/v1/symbols/1/similar", json={})
    assert response.status_code == 401


def test_symbol_impact_analysis_rejected_without_any_key(client, isolated_app):
    response = client.post("/v1/symbols/1/impact-analysis", json={})
    assert response.status_code == 401


def test_symbol_within_search_rejected_without_any_key(client, isolated_app):
    response = client.post("/v1/symbols/1/search?query=hello")
    assert response.status_code == 401


def test_file_symbols_rejected_without_any_key(client, isolated_app):
    response = client.get("/v1/files/1/symbols")
    assert response.status_code == 401


def test_repository_stats_rejected_without_any_key(client, isolated_app):
    response = client.get("/v1/repositories/1/stats")
    assert response.status_code == 401


def test_ide_search_rejected_without_any_key(client, isolated_app):
    response = client.post("/v1/ide-search", json={"selected_text": "hello"})
    assert response.status_code == 401


def test_feedback_rejected_without_any_key(client, isolated_app):
    response = client.post(
        "/v1/search/feedback",
        json={"search_id": "s1", "result_id": 1, "feedback": "relevant"},
    )
    assert response.status_code == 401
