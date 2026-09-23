"""
Phase 7.4 -- Observability wiring (roadmap §37 item 7.4; §18
Observability Architecture: "actually wire the already-declared
dependency" -- `prometheus-client` and `structlog`, both declared in
`pyproject.toml` since Phase 0.9, neither installed nor imported by any
code until this milestone).

Implemented under the user's explicit continuous-execution
authorization (see CODEPULSE_PROGRESS.md's standing-deviation notice).

`codepulse/observability.py` holds a single, MODULE-LEVEL (process-
lifetime) `CollectorRegistry` -- every metric in it accumulates across
the ENTIRE test session, not just this file's own tests (many other
test files also hit `/v1/search`/`/v1/repositories/index`). Every
assertion below therefore checks the DELTA a specific action causes
(before/after snapshot), never an absolute counter value -- the only
assertion style that is correct and order-independent for a real,
shared, monotonically-increasing Prometheus registry.

`/health/live`, `/health/ready`, `/metrics` all carry NO
`Depends(require_api_key)` -- verified directly (no headers sent at
all), per §21's own "every endpoint requires an API key except
`/health/*`" (extended here, for the same reason real Prometheus
scraping is never per-repository-scoped, to `/metrics` too).
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from codepulse.api.app import app
from codepulse.jobs import create_job
from codepulse.observability import REGISTRY
from codepulse.storage.db import RepositoryModel
from tests.conftest import make_api_key_headers


@pytest.fixture
def client():
    return TestClient(app)


def _make_repo_dir(tmp_path, name: str, files: dict[str, str] | None = None):
    repo_dir = tmp_path / name
    repo_dir.mkdir()
    for rel_path, content in (files or {"mod.py": "def helper():\n    return 1\n"}).items():
        (repo_dir / rel_path).write_text(content, encoding="utf-8")
    return repo_dir


def _metric_value(name: str, labels: dict[str, str] | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


# --- Health endpoints -------------------------------------------------------

def test_health_live_requires_no_api_key(client, isolated_app):
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "live"


def test_health_ready_requires_no_api_key_and_reports_real_checks(client, isolated_app):
    response = client.get("/health/ready")
    assert response.status_code in (200, 503)
    body = response.json()
    assert set(body["checks"].keys()) == {"database_reachable", "embedding_model_loaded", "index_storage_writable"}
    assert body["checks"]["database_reachable"] is True


def test_health_ready_reflects_the_embedding_model_actually_loading(client, isolated_app):
    """The embedding model is lazily loaded (Phase 0's own established
    pattern) -- /health/ready must honestly report `False` before any
    encode() call has happened, and `True` immediately after one does,
    never assumed true just because the process started successfully."""
    from codepulse.embeddings.embedder import CodeEmbedder
    embedder = CodeEmbedder()
    import codepulse.api.app as app_module
    app_module.indexing_engine.embedder = embedder

    before = client.get("/health/ready").json()
    assert before["checks"]["embedding_model_loaded"] is False

    embedder.encode("def f(): pass")

    after = client.get("/health/ready").json()
    assert after["checks"]["embedding_model_loaded"] is True


# --- /metrics endpoint -------------------------------------------------------

def test_metrics_endpoint_requires_no_api_key_and_returns_valid_prometheus_format(client, isolated_app):
    response = client.get("/metrics")
    assert response.status_code == 200
    # A real, valid Prometheus exposition-format parse -- not just "some
    # text" (this also proves generate_latest() didn't just silently
    # emit an empty/malformed body). The parser normalizes away Counter
    # metrics' own "_total" suffix at the FAMILY level (a well-known
    # Prometheus client convention), so family names are checked
    # without that suffix; the raw exposition text (checked separately
    # below) still carries the full "_total" name every scraper sees.
    families = list(text_string_to_metric_families(response.text))
    family_names = {f.name for f in families}
    assert "search_requests" in family_names
    assert "indexing_jobs" in family_names
    assert "job_queue_depth" in family_names
    assert "api_errors" in family_names
    assert "search_requests_total" in response.text
    assert "indexing_jobs_total" in response.text
    assert "api_errors_total" in response.text


def test_search_request_increments_search_requests_total_and_latency(client, isolated_app, tmp_path):
    repo_dir = _make_repo_dir(tmp_path, "repo_metrics_search")
    from codepulse.indexer.engine import IndexingEngine
    import codepulse.api.app as app_module
    result = app_module.indexing_engine.index_directory(str(repo_dir), repo_name="r", repo_url=str(repo_dir))
    headers = make_api_key_headers(isolated_app, repository_ids=[result["repository_id"]])

    before = _metric_value("search_requests_total", {"search_type": "keyword"})
    before_count = _metric_value("search_latency_seconds_count")

    response = client.post(
        "/v1/search",
        json={"repository_id": result["repository_id"], "query": "helper", "search_type": "keyword"},
        headers=headers,
    )
    assert response.status_code == 200

    after = _metric_value("search_requests_total", {"search_type": "keyword"})
    after_count = _metric_value("search_latency_seconds_count")
    assert after == before + 1
    assert after_count == before_count + 1


def test_search_with_zero_results_increments_search_zero_result_total(client, isolated_app, tmp_path):
    repo_dir = _make_repo_dir(tmp_path, "repo_metrics_zero")
    import codepulse.api.app as app_module
    result = app_module.indexing_engine.index_directory(str(repo_dir), repo_name="r", repo_url=str(repo_dir))
    headers = make_api_key_headers(isolated_app, repository_ids=[result["repository_id"]])

    before = _metric_value("search_zero_result_total")
    response = client.post(
        "/v1/search",
        json={"repository_id": result["repository_id"], "query": "zzz_definitely_absent_zzz", "search_type": "keyword"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["results"] == []
    after = _metric_value("search_zero_result_total")
    assert after == before + 1


def test_indexing_job_transitions_increment_indexing_jobs_total(client, isolated_app, tmp_path):
    repo_dir = _make_repo_dir(tmp_path, "repo_metrics_index")
    headers = make_api_key_headers(isolated_app)

    before_running = _metric_value("indexing_jobs_total", {"status": "running"})
    before_succeeded = _metric_value("indexing_jobs_total", {"status": "succeeded"})
    before_files = _metric_value("files_indexed_total")
    before_symbols = _metric_value("symbols_indexed_total")
    before_duration_count = _metric_value("indexing_duration_seconds_count")

    response = client.post("/v1/repositories/index", json={"url": str(repo_dir)}, headers=headers)
    assert response.status_code == 202

    after_running = _metric_value("indexing_jobs_total", {"status": "running"})
    after_succeeded = _metric_value("indexing_jobs_total", {"status": "succeeded"})
    after_files = _metric_value("files_indexed_total")
    after_symbols = _metric_value("symbols_indexed_total")
    after_duration_count = _metric_value("indexing_duration_seconds_count")

    assert after_running == before_running + 1
    assert after_succeeded == before_succeeded + 1
    assert after_files == before_files + 1  # exactly one file in this fixture repo
    assert after_symbols >= before_symbols + 1  # at least the one helper() symbol
    assert after_duration_count == before_duration_count + 1


def test_job_queue_depth_reflects_a_real_queued_job(client, isolated_app, tmp_path):
    repo_dir = _make_repo_dir(tmp_path, "repo_metrics_queue")
    session = isolated_app.get_session()
    try:
        repo = RepositoryModel(name="repo_metrics_queue", url=str(repo_dir))
        session.add(repo)
        session.commit()
        repo_id = repo.id
    finally:
        session.close()
    create_job(isolated_app, repo_id)  # leaves it "queued" -- never resolved

    response = client.get("/metrics")
    families = {f.name: f for f in text_string_to_metric_families(response.text)}
    depth_samples = families["job_queue_depth"].samples
    assert any(s.value >= 1 for s in depth_samples), "job_queue_depth must reflect the real queued job just created"


def test_a_404_response_increments_api_errors_total(client, isolated_app):
    headers = make_api_key_headers(isolated_app)
    before = _metric_value("api_errors_total", {"endpoint": "/v1/symbols/999999", "status_code": "404"})
    response = client.get("/v1/symbols/999999", headers=headers)
    assert response.status_code == 404
    after = _metric_value("api_errors_total", {"endpoint": "/v1/symbols/999999", "status_code": "404"})
    assert after == before + 1


# --- Structured logging (structlog) ----------------------------------------

def test_configure_logging_is_idempotent():
    from codepulse.observability import configure_logging
    configure_logging()
    configure_logging()  # must not raise or reconfigure a second time


def test_get_logger_returns_a_usable_structlog_logger(capsys):
    from codepulse.observability import get_logger
    logger = get_logger()
    logger.info("test_event", operation="unit_test", duration_ms=1)
    # structlog's PrintLoggerFactory writes to stdout -- confirm a real
    # JSON-shaped line was actually emitted, not just that no exception
    # was raised.
    out = capsys.readouterr().out
    assert "test_event" in out
    assert "operation" in out
