"""
Phase 0.7 — Remove all hardcoded metric values from API responses.

These tests prove, by calling the actual FastAPI endpoints and inspecting
the real JSON responses, that the previously fabricated fields
(`index_size_mb`, `total_queries`, `avg_search_latency_ms`,
`estimated_time_seconds`, and the false "recorded"/"weights updated"
feedback claim) are genuinely gone from what the API serves -- not merely
absent from a source-code search -- while legitimate, actually-measured/
computed fields remain present and correct.

Phase 1.2 note: every endpoint now requires a valid, and where applicable
correctly *scoped*, `X-API-Key` header (see `codepulse/api/security.py`).
Every test below therefore uses the shared `isolated_app` fixture
(tests/conftest.py) to swap the app's storage for a temporary, disposable
database and mint a real key against it, per this project's DB-safety rule
(never write to or authenticate against the real `codepulse.db` from a
test).

`test_index_status_has_no_fabricated_index_size` and
`test_repository_stats_has_no_fabricated_query_metrics` previously used a
deliberately nonexistent `repository_id` (999999999) to exercise
`get_index_status`/`get_repository_stats`'s "repository not found" fallback
branch without touching real data. That specific scenario is no longer
reachable through an authenticated request: `codepulse.auth.create_api_key`
(Phase 1.1) validates that every repository id a key is scoped to actually
exists, so a key can never be scoped to a phantom id, and `authorize_repository`
(Phase 1.2) rejects any repository_id the key isn't scoped to before the
handler's own "repository not found" branch is ever reached. Both tests
were adapted to authorize against a real, seeded repository instead --
this exercises the "repository found" branch rather than the fallback one,
but verifies the exact same assertion (no fabricated field in the
response), which is unaffected by which of the two branches produced it.

`/v1/repositories/index` is the one endpoint that WOULD write to whatever
database `storage_manager` currently points at (FastAPI's TestClient runs
BackgroundTasks synchronously before returning the response, so
index_directory() actually runs) -- `isolated_app` already points it at a
disposable database, so this remains safe.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from codepulse.api.app import app
from codepulse.config import reset_settings
from codepulse.storage.db import RepositoryModel
from tests.conftest import make_api_key_headers, make_creator_headers


@pytest.fixture(autouse=True)
def cleanup_settings():
    reset_settings()
    yield
    reset_settings()


def _seed_repository(isolated_app, name: str = "seeded-repo") -> int:
    session = isolated_app.get_session()
    try:
        repo = RepositoryModel(name=name, url=f"file:///{name}")
        session.add(repo)
        session.commit()
        return repo.id
    finally:
        session.close()


# --- /v1/repositories/{id}/index-status -------------------------------------

def test_index_status_has_no_fabricated_index_size(isolated_app):
    repo_id = _seed_repository(isolated_app)
    client = TestClient(app)
    headers = make_api_key_headers(isolated_app, repository_ids=[repo_id])

    response = client.get(f"/v1/repositories/{repo_id}/index-status", headers=headers)
    assert response.status_code == 200
    data = response.json()

    assert "index_size_mb" not in data, "index_size_mb was never measured and must not be returned"

    # Legitimate fields (real counts / a configured value) must remain.
    assert data["repository_id"] == repo_id
    assert "indexed_files" in data
    assert "indexed_symbols" in data
    assert data["embedding_model"]  # configured value, not a metric


# --- /v1/repositories/{id}/stats --------------------------------------------

def test_repository_stats_has_no_fabricated_query_metrics(isolated_app):
    repo_id = _seed_repository(isolated_app)
    client = TestClient(app)
    headers = make_api_key_headers(isolated_app, repository_ids=[repo_id])

    response = client.get(f"/v1/repositories/{repo_id}/stats", headers=headers)
    assert response.status_code == 200
    data = response.json()

    assert "total_queries" not in data, "total_queries was never measured (no query log exists) and must not be returned"
    assert "avg_search_latency_ms" not in data, "avg_search_latency_ms was never measured and must not be returned"

    # Legitimate fields (real counts) must remain.
    assert "total_files" in data
    assert "total_symbols" in data
    assert "languages" in data


# --- /v1/repositories/index --------------------------------------------------

def test_index_repository_response_has_no_fabricated_duration_estimate(isolated_app, tmp_path):
    """
    /v1/repositories/index requires authentication only (Phase 1.2 does not
    scope this endpoint -- see its docstring in codepulse/api/app.py), so an
    unscoped key suffices.
    """
    client = TestClient(app)
    headers = make_creator_headers(isolated_app)

    empty_repo_dir = tmp_path / "empty_repo"
    empty_repo_dir.mkdir()

    response = client.post("/v1/repositories/index", json={"url": str(empty_repo_dir)}, headers=headers)
    assert response.status_code == 202
    data = response.json()

    assert "estimated_time_seconds" not in data, "duration was never measured/predicted and must not be returned"

    # Legitimate fields must remain.
    assert data["status"] == "queued"
    assert "job_id" in data
    assert data["url"] == str(empty_repo_dir)

    # Confirm the write landed in the ISOLATED temp DB (proving no write
    # ever reached the real codepulse.db).
    session = isolated_app.get_session()
    try:
        repo = session.query(RepositoryModel).filter(RepositoryModel.url == str(empty_repo_dir)).first()
        assert repo is not None
    finally:
        session.close()


# --- /v1/search/feedback -----------------------------------------------------

def test_feedback_endpoint_does_not_claim_persistence_or_ranking_effect(isolated_app):
    client = TestClient(app)
    headers = make_api_key_headers(isolated_app)  # feedback needs auth only, no repo scope

    response = client.post(
        "/v1/search/feedback",
        json={"search_id": "s1", "result_id": 1, "feedback": "relevant"},
        headers=headers,
    )
    assert response.status_code == 200
    data = response.json()

    assert data["status"] != "recorded", "nothing is persisted -- status must not claim it was"
    assert "weights updated" not in data["message"].lower(), "no ranking weights are ever touched by this endpoint"
    assert "not yet persisted" in data["message"].lower()


# --- search endpoint's execution_time_ms is real, not fabricated -----------

def test_search_execution_time_is_a_real_measurement_not_removed(isolated_app):
    """
    Not a "remove this field" case -- execution_time_ms is a legitimate,
    actually-measured value (a real time.time() delta around the actual
    search() call), unlike the fabricated fields above. This test locks in
    that classification: it must still be present and be a plausible
    non-negative duration.
    """
    repo_id = _seed_repository(isolated_app)
    client = TestClient(app)
    headers = make_api_key_headers(isolated_app, repository_ids=[repo_id])

    response = client.post(
        "/v1/search",
        json={"query": "anything", "limit": 5, "search_type": "hybrid", "repository_id": repo_id},
        headers=headers,
    )
    assert response.status_code == 200
    data = response.json()

    assert "execution_time_ms" in data
    assert isinstance(data["execution_time_ms"], int)
    assert data["execution_time_ms"] >= 0
