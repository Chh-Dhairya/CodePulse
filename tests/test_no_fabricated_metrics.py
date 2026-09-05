"""
Phase 0.7 — Remove all hardcoded metric values from API responses.

These tests prove, by calling the actual FastAPI endpoints and inspecting
the real JSON responses, that the previously fabricated fields
(`index_size_mb`, `total_queries`, `avg_search_latency_ms`,
`estimated_time_seconds`, and the false "recorded"/"weights updated"
feedback claim) are genuinely gone from what the API serves -- not merely
absent from a source-code search -- while legitimate, actually-measured/
computed fields remain present and correct.

Read-only endpoints (index-status, stats) are exercised against the app's
existing global TestClient without touching real data: a deliberately
nonexistent `repository_id` is used, since these endpoints perform no
writes regardless of whether the repository exists, so this is safe
against the real codepulse.db and doesn't depend on its specific content.

`/v1/repositories/index` is the one endpoint that WOULD write to the real
codepulse.db if called against the app's default global storage_manager
(FastAPI's TestClient runs BackgroundTasks synchronously before returning
the response, so index_directory() would actually run). That test
monkeypatches the app module's storage_manager/indexing_engine to a
temporary, disposable database first, per this project's DB-safety rule:
never write to the production codepulse.db from a test.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

import codepulse.api.app as app_module
from codepulse.api.app import app
from codepulse.config import reset_settings
from codepulse.indexer.engine import IndexingEngine
from codepulse.storage.db import RepositoryModel, StorageManager

client = TestClient(app)


@pytest.fixture(autouse=True)
def cleanup_settings():
    reset_settings()
    yield
    reset_settings()


# --- /v1/repositories/{id}/index-status -------------------------------------

def test_index_status_has_no_fabricated_index_size():
    response = client.get("/v1/repositories/999999999/index-status")
    assert response.status_code == 200
    data = response.json()

    assert "index_size_mb" not in data, "index_size_mb was never measured and must not be returned"

    # Legitimate fields (real counts / a configured value) must remain.
    assert data["repository_id"] == 999999999
    assert "indexed_files" in data
    assert "indexed_symbols" in data
    assert data["embedding_model"]  # configured value, not a metric


# --- /v1/repositories/{id}/stats --------------------------------------------

def test_repository_stats_has_no_fabricated_query_metrics():
    response = client.get("/v1/repositories/999999999/stats")
    assert response.status_code == 200
    data = response.json()

    assert "total_queries" not in data, "total_queries was never measured (no query log exists) and must not be returned"
    assert "avg_search_latency_ms" not in data, "avg_search_latency_ms was never measured and must not be returned"

    # Legitimate fields (real counts) must remain.
    assert "total_files" in data
    assert "total_symbols" in data
    assert "languages" in data


# --- /v1/repositories/index --------------------------------------------------

def test_index_repository_response_has_no_fabricated_duration_estimate(tmp_path, monkeypatch):
    """
    FastAPI's TestClient runs BackgroundTasks synchronously, so posting to
    this endpoint actually calls index_directory() against whatever
    storage_manager/indexing_engine the app module currently holds. This
    test swaps those for a temporary, disposable database first so no
    write ever reaches the real codepulse.db, then confirms the swap
    actually took effect (the write landed in the temp DB).
    """
    db_path = tmp_path / "isolated.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"
    storage = StorageManager(db_url=db_url)
    engine = IndexingEngine(storage=storage)
    monkeypatch.setattr(app_module, "storage_manager", storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)

    empty_repo_dir = tmp_path / "empty_repo"
    empty_repo_dir.mkdir()

    response = client.post("/v1/repositories/index", json={"url": str(empty_repo_dir)})
    assert response.status_code == 202
    data = response.json()

    assert "estimated_time_seconds" not in data, "duration was never measured/predicted and must not be returned"

    # Legitimate fields must remain.
    assert data["status"] == "queued"
    assert "job_id" in data
    assert data["url"] == str(empty_repo_dir)

    # Confirm the write landed in the ISOLATED temp DB (proving the swap
    # worked, and that this test never touched the real codepulse.db).
    session = storage.get_session()
    try:
        repo = session.query(RepositoryModel).filter(RepositoryModel.url == str(empty_repo_dir)).first()
        assert repo is not None
    finally:
        session.close()


# --- /v1/search/feedback -----------------------------------------------------

def test_feedback_endpoint_does_not_claim_persistence_or_ranking_effect():
    response = client.post(
        "/v1/search/feedback",
        json={"search_id": "s1", "result_id": 1, "feedback": "relevant"},
    )
    assert response.status_code == 200
    data = response.json()

    assert data["status"] != "recorded", "nothing is persisted -- status must not claim it was"
    assert "weights updated" not in data["message"].lower(), "no ranking weights are ever touched by this endpoint"
    assert "not yet persisted" in data["message"].lower()


# --- search endpoint's execution_time_ms is real, not fabricated -----------

def test_search_execution_time_is_a_real_measurement_not_removed():
    """
    Not a "remove this field" case -- execution_time_ms is a legitimate,
    actually-measured value (a real time.time() delta around the actual
    search() call), unlike the fabricated fields above. This test locks in
    that classification: it must still be present and be a plausible
    non-negative duration.
    """
    response = client.post(
        "/v1/search",
        json={"query": "anything", "limit": 5, "search_type": "hybrid"},
    )
    assert response.status_code == 200
    data = response.json()

    assert "execution_time_ms" in data
    assert isinstance(data["execution_time_ms"], int)
    assert data["execution_time_ms"] >= 0
