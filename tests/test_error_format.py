"""
Phase 1.6 — Structured error response format across all endpoints
(roadmap §37 item 1.6; §21 API Strategy: "Errors follow a single shape:
`{"error": {"code": "...", "message": "...", "details": {...}}}`").

Exercises the real HTTP surface end-to-end, proving every distinct error
class this app can produce now uses the single §21 envelope:
  - 401 (missing/unknown/revoked key), 403 (wrong repository scope),
    404 (not found), 409 (concurrency lock), 429 (rate limit), 501
    (unsupported git ingestion), 400 (bad ingestion path/URL) -- all via
    the existing `HTTPException` raise sites, reshaped centrally.
  - 422 (FastAPI's own automatic request-validation failure) -- a
    framework-generated error, not a hand-raised `HTTPException`.
  - 500 (a genuinely unhandled exception) -- logged server-side, reported
    to the client with a generic, non-leaking message.
  - A success response (200/202) is NOT reshaped -- only error responses
    change shape.
  - `code` is derived from the HTTP status (not an invented string per
    endpoint) and `details` carries real information where it exists
    (the rate limiter's real `retry_after_seconds`, the real Pydantic
    validation error list) and is empty otherwise.

All tests use the shared `isolated_app` fixture (tests/conftest.py): a
temporary, disposable SQLite database, never the real codepulse.db.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from codepulse.api.app import app
from codepulse.config import CodePulseConfig, reset_settings, set_settings
from codepulse.rate_limit import reset_limiter
from tests.conftest import make_api_key_headers


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def client_no_reraise():
    """
    A TestClient that does NOT re-raise a genuinely unhandled server
    exception to the calling test -- Starlette's TestClient defaults to
    `raise_server_exceptions=True`, a testing convenience that re-raises
    the original exception even when the app's own exception handler
    already produced a clean response, specifically so bugs aren't
    masked by accident. This fixture is used ONLY for the 500 test below,
    which deliberately triggers a real exception to inspect the app's
    own response, not to hide it.
    """
    return TestClient(app, raise_server_exceptions=False)


def _make_repo_dir(tmp_path, name: str, files: dict[str, str] | None = None):
    repo_dir = tmp_path / name
    repo_dir.mkdir()
    for rel_path, content in (files or {}).items():
        (repo_dir / rel_path).write_text(content, encoding="utf-8")
    return repo_dir


# --- The envelope shape itself --------------------------------------------

def _assert_envelope_shape(body: dict) -> dict:
    assert set(body.keys()) == {"error"}
    error = body["error"]
    assert set(error.keys()) == {"code", "message", "details"}
    assert isinstance(error["code"], str) and error["code"]
    assert isinstance(error["message"], str) and error["message"]
    assert isinstance(error["details"], dict)
    return error


# --- 401 -----------------------------------------------------------------

def test_401_missing_key_uses_the_structured_envelope(client, isolated_app):
    response = client.get("/v1/repositories/1/index-status")
    assert response.status_code == 401
    error = _assert_envelope_shape(response.json())
    assert error["code"] == "unauthorized"
    assert error["details"] == {}


def test_401_unknown_key_uses_the_structured_envelope(client, isolated_app):
    response = client.get(
        "/v1/repositories/1/index-status", headers={"X-API-Key": "cpk_never-created"}
    )
    assert response.status_code == 401
    error = _assert_envelope_shape(response.json())
    assert error["code"] == "unauthorized"


# --- 403 -----------------------------------------------------------------

def test_403_wrong_scope_uses_the_structured_envelope(client, isolated_app, tmp_path):
    from codepulse.storage.db import RepositoryModel

    session = isolated_app.get_session()
    try:
        repo = RepositoryModel(name="r1", url="file:///r1")
        session.add(repo)
        session.commit()
        repo_id = repo.id
    finally:
        session.close()

    headers = make_api_key_headers(isolated_app)  # unscoped
    response = client.get(f"/v1/repositories/{repo_id}/index-status", headers=headers)
    assert response.status_code == 403
    error = _assert_envelope_shape(response.json())
    assert error["code"] == "forbidden"


# --- 404 -----------------------------------------------------------------

def test_404_not_found_uses_the_structured_envelope(client, isolated_app):
    headers = make_api_key_headers(isolated_app)
    response = client.get("/v1/symbols/999999", headers=headers)
    assert response.status_code == 404
    error = _assert_envelope_shape(response.json())
    assert error["code"] == "not_found"
    assert error["message"] == "Symbol not found"


# --- 409 (advisory concurrency lock) --------------------------------------

def test_409_conflict_uses_the_structured_envelope(client, isolated_app, tmp_path):
    from codepulse.jobs import create_job
    from codepulse.storage.db import RepositoryModel

    repo_dir = _make_repo_dir(tmp_path, "repo1")
    session = isolated_app.get_session()
    try:
        repo = RepositoryModel(name="repo1", url=str(repo_dir))
        session.add(repo)
        session.commit()
        repo_id = repo.id
    finally:
        session.close()
    create_job(isolated_app, repo_id)  # leaves an active ("queued") job

    headers = make_api_key_headers(isolated_app)
    response = client.post("/v1/repositories/index", json={"url": str(repo_dir)}, headers=headers)
    assert response.status_code == 409
    error = _assert_envelope_shape(response.json())
    assert error["code"] == "conflict"


# --- 429 (rate limit) ------------------------------------------------------

def test_429_rate_limit_uses_the_structured_envelope_with_real_retry_after(client, isolated_app):
    set_settings(CodePulseConfig(rate_limit_burst=1, rate_limit_requests_per_second=1.0))
    reset_limiter()
    try:
        headers = make_api_key_headers(isolated_app)
        assert client.get("/v1/autocomplete?prefix=x", headers=headers).status_code == 200

        response = client.get("/v1/autocomplete?prefix=x", headers=headers)
        assert response.status_code == 429
        error = _assert_envelope_shape(response.json())
        assert error["code"] == "too_many_requests"

        header_retry_after = int(response.headers["retry-after"])
        # The exact same real value backs both the header (required since
        # Phase 1.4) and the new structured `details` field -- not two
        # independently-computed/fabricated numbers.
        assert error["details"]["retry_after_seconds"] == header_retry_after
        assert header_retry_after > 0
    finally:
        reset_limiter()
        reset_settings()


# --- 501 (unsupported git ingestion) --------------------------------------

def test_501_not_implemented_uses_the_structured_envelope(client, isolated_app):
    headers = make_api_key_headers(isolated_app)
    response = client.post(
        "/v1/repositories/index",
        json={"source": "github", "url": "https://8.8.8.8/org/repo"},
        headers=headers,
    )
    assert response.status_code == 501
    error = _assert_envelope_shape(response.json())
    assert error["code"] == "not_implemented"


# --- 400 (bad ingestion path) ----------------------------------------------

def test_400_bad_request_uses_the_structured_envelope(client, isolated_app, tmp_path):
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    outside_repo = _make_repo_dir(tmp_path, "outside")

    set_settings(CodePulseConfig(ingestion_allowed_roots=[str(allowed_root)]))
    try:
        headers = make_api_key_headers(isolated_app)
        response = client.post(
            "/v1/repositories/index", json={"url": str(outside_repo)}, headers=headers
        )
        assert response.status_code == 400
        error = _assert_envelope_shape(response.json())
        assert error["code"] == "bad_request"
    finally:
        reset_settings()


# --- 422 (FastAPI's own automatic validation error) ------------------------

def test_422_validation_error_uses_the_structured_envelope(client, isolated_app):
    headers = make_api_key_headers(isolated_app)
    # 'query' is a required field on SearchRequest -- omitting it triggers
    # FastAPI's own automatic RequestValidationError, not a hand-raised
    # HTTPException.
    response = client.post("/v1/search", json={}, headers=headers)
    assert response.status_code == 422
    error = _assert_envelope_shape(response.json())
    assert error["code"] == "unprocessable_entity"
    assert error["message"] == "Request validation failed"
    # The real Pydantic error list is surfaced in `details`, not discarded.
    assert isinstance(error["details"]["errors"], list)
    assert len(error["details"]["errors"]) >= 1
    assert error["details"]["errors"][0]["loc"] == ["body", "query"]


# --- 500 (a genuinely unhandled exception) ---------------------------------

def test_500_unhandled_exception_uses_the_structured_envelope_without_leaking_internals(
    client_no_reraise, isolated_app, monkeypatch
):
    import codepulse.api.app as app_module

    class _BrokenSymbolsById:
        def items(self):
            raise RuntimeError("some internal detail that must never reach the client")

    monkeypatch.setattr(app_module.indexing_engine, "symbols_by_id", _BrokenSymbolsById())

    headers = make_api_key_headers(isolated_app)
    response = client_no_reraise.get("/v1/autocomplete?prefix=x", headers=headers)

    assert response.status_code == 500
    error = _assert_envelope_shape(response.json())
    assert error["code"] == "internal_server_error"
    assert error["message"] == "Internal server error"
    assert error["details"] == {}
    # The real exception text/traceback must never reach the client.
    assert "some internal detail" not in response.text


# --- Success responses are NOT reshaped ------------------------------------

def test_success_response_is_not_wrapped_in_the_error_envelope(client, isolated_app):
    headers = make_api_key_headers(isolated_app)
    response = client.get("/v1/autocomplete?prefix=x", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert "error" not in data
    assert "suggestions" in data
