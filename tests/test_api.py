import pytest
from fastapi.testclient import TestClient
from codepulse.api.app import app

client = TestClient(app)


def test_api_search_endpoint():
    response = client.post(
        "/v1/search",
        json={"query": "test query", "limit": 5, "search_type": "hybrid"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "results" in data
    assert "execution_time_ms" in data
    assert data["query"] == "test query"


def test_api_autocomplete_endpoint():
    response = client.get("/v1/autocomplete?prefix=get&limit=5")
    assert response.status_code == 200
    data = response.json()
    assert "suggestions" in data


def test_api_feedback_endpoint():
    """
    Phase 0.7: the endpoint no longer claims "recorded" / "weights
    updated" -- it doesn't persist anything or touch ranking weights, so
    its response must not claim otherwise. Updated from the pre-0.7
    assertion (`status == "recorded"`), which encoded exactly the
    fabricated success claim this milestone removes.
    """
    response = client.post(
        "/v1/search/feedback",
        json={"search_id": "test_search_1", "result_id": 1, "feedback": "relevant"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "received"
    assert "weights updated" not in data["message"].lower()
