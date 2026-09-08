"""
Phase 1.2 note: every one of these endpoints now requires a valid `X-API-Key`
header (see `codepulse/api/security.py`), so each test uses the shared
`isolated_app` fixture (tests/conftest.py) instead of the plain
module-level `TestClient(app)` this file used before Phase 1.2 -- that
avoids ever authenticating against the real `codepulse.db`. The original
assertions are unchanged; only the request setup (isolated DB + a real
scoped/unscoped API key) was added.
"""
from fastapi.testclient import TestClient

from codepulse.api.app import app
from codepulse.storage.db import RepositoryModel
from tests.conftest import make_api_key_headers


def test_api_search_endpoint(isolated_app):
    # /v1/search's SearchRequest.repository_id defaults to 1 when omitted
    # from the request body (as below) -- create a real repository so a key
    # can legitimately be scoped to that id.
    session = isolated_app.get_session()
    try:
        repo = RepositoryModel(name="r1", url="file:///r1")
        session.add(repo)
        session.commit()
        repo_id = repo.id
    finally:
        session.close()

    client = TestClient(app)
    headers = make_api_key_headers(isolated_app, repository_ids=[repo_id])

    response = client.post(
        "/v1/search",
        json={"query": "test query", "limit": 5, "search_type": "hybrid", "repository_id": repo_id},
        headers=headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert "results" in data
    assert "execution_time_ms" in data
    assert data["query"] == "test query"


def test_api_autocomplete_endpoint(isolated_app):
    client = TestClient(app)
    headers = make_api_key_headers(isolated_app)  # autocomplete needs auth only, no repo scope

    response = client.get("/v1/autocomplete?prefix=get&limit=5", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert "suggestions" in data


def test_api_feedback_endpoint(isolated_app):
    """
    Phase 0.7: the endpoint no longer claims "recorded" / "weights
    updated" -- it doesn't persist anything or touch ranking weights, so
    its response must not claim otherwise. Updated from the pre-0.7
    assertion (`status == "recorded"`), which encoded exactly the
    fabricated success claim this milestone removes.
    """
    client = TestClient(app)
    headers = make_api_key_headers(isolated_app)  # feedback needs auth only, no repo scope

    response = client.post(
        "/v1/search/feedback",
        json={"search_id": "test_search_1", "result_id": 1, "feedback": "relevant"},
        headers=headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "received"
    assert "weights updated" not in data["message"].lower()
