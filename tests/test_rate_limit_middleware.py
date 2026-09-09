"""
Phase 1.4 — In-process token-bucket rate limiter, HTTP layer (roadmap §37
item 1.4; backlog B11 DoD: "Excess requests receive 429 with retry-after").

Exercises the real `_rate_limit_middleware` in `codepulse.api.app` end-to-
end via real HTTP requests:
  - requests within the configured burst succeed normally
  - exceeding the burst returns HTTP 429 with a real, positive
    `Retry-After` header
  - distinct API keys get independent limits (one key being throttled
    does not affect another)
  - unauthenticated requests are rate-limited too (by client IP), so an
    unauthenticated flood doesn't get an unlimited exemption
  - the limiter can be disabled entirely via config
  - a 429 rejection still passes through `CORSMiddleware` correctly (the
    same middleware-ordering guarantee 401/403 auth rejections already
    have), verified against a minimal, explicitly-CORS-configured app
    mirroring `tests/test_cors.py`'s own established pattern -- the real
    global app's CORS config defaults to deny-all and is frozen at first
    import, so it cannot itself demonstrate an *allowed* origin surviving
    a 429 the way this dedicated check can.

All tests use the shared `isolated_app` fixture (tests/conftest.py) plus a
`tight_rate_limit` fixture (below) that reconfigures the rate limiter to a
tiny, deterministic burst -- never the real codepulse.db, never relying on
wall-clock timing to trigger a 429.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from codepulse.api.app import app
from codepulse.config import CodePulseConfig, reset_settings, set_settings
from codepulse.rate_limit import TokenBucketLimiter, reset_limiter
from tests.conftest import make_api_key_headers


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def tight_rate_limit():
    """Reconfigures the rate limiter to a tiny, deterministic burst (3 requests, ~1/s refill)."""
    set_settings(CodePulseConfig(rate_limit_burst=3, rate_limit_requests_per_second=1.0))
    reset_limiter()
    yield
    reset_limiter()
    reset_settings()


@pytest.fixture
def disabled_rate_limit():
    set_settings(CodePulseConfig(rate_limit_enabled=False))
    reset_limiter()
    yield
    reset_limiter()
    reset_settings()


# --- Requests within burst succeed --------------------------------------------

def test_requests_within_burst_all_succeed(client, isolated_app, tight_rate_limit):
    headers = make_api_key_headers(isolated_app)
    for _ in range(3):
        response = client.get("/v1/autocomplete?prefix=x", headers=headers)
        assert response.status_code == 200


# --- Exceeding burst returns 429 with a real Retry-After ----------------------

def test_exceeding_burst_returns_429_with_retry_after(client, isolated_app, tight_rate_limit):
    headers = make_api_key_headers(isolated_app)
    for _ in range(3):
        assert client.get("/v1/autocomplete?prefix=x", headers=headers).status_code == 200

    response = client.get("/v1/autocomplete?prefix=x", headers=headers)
    assert response.status_code == 429
    retry_after = response.headers.get("retry-after")
    assert retry_after is not None
    assert int(retry_after) > 0
    assert "rate limit" in response.json()["detail"].lower()


# --- Independent limits per identity -------------------------------------------

def test_different_api_keys_have_independent_limits(client, isolated_app, tight_rate_limit):
    headers_a = make_api_key_headers(isolated_app, name="key-a")
    headers_b = make_api_key_headers(isolated_app, name="key-b")

    for _ in range(3):
        assert client.get("/v1/autocomplete?prefix=x", headers=headers_a).status_code == 200
    # key-a is now exhausted.
    assert client.get("/v1/autocomplete?prefix=x", headers=headers_a).status_code == 429

    # key-b's own bucket is untouched by key-a's usage.
    assert client.get("/v1/autocomplete?prefix=x", headers=headers_b).status_code == 200


# --- Unauthenticated requests are rate-limited by IP ---------------------------

def test_unauthenticated_requests_are_rate_limited_too(client, isolated_app, tight_rate_limit):
    """
    No API key at all still consumes the IP-fallback bucket -- exceeding
    it returns 429, not endless 401s, so an unauthenticated flood cannot
    bypass the limiter entirely.
    """
    for _ in range(3):
        response = client.get("/v1/autocomplete?prefix=x")
        assert response.status_code == 401  # rate limit not yet exceeded; auth still runs and rejects

    response = client.get("/v1/autocomplete?prefix=x")
    assert response.status_code == 429


# --- Config-driven enable/disable ----------------------------------------------

def test_rate_limiting_can_be_disabled_via_config(client, isolated_app, disabled_rate_limit):
    headers = make_api_key_headers(isolated_app)
    for _ in range(50):  # far beyond the default burst of 20
        response = client.get("/v1/autocomplete?prefix=x", headers=headers)
        assert response.status_code == 200, "disabled rate limiting must never return 429"


# --- Default (non-tight) settings do not interfere with normal test traffic --

def test_default_rate_limit_settings_do_not_disrupt_a_handful_of_requests(client, isolated_app):
    """No `tight_rate_limit` override here -- proves the real default
    (burst=20, 5 req/s) comfortably tolerates ordinary test-sized traffic."""
    headers = make_api_key_headers(isolated_app)
    for _ in range(5):
        assert client.get("/v1/autocomplete?prefix=x", headers=headers).status_code == 200


# --- A 429 rejection still passes through CORSMiddleware correctly -----------

def _build_app_with_rate_limit_and_cors() -> TestClient:
    """
    Mirrors tests/test_cors.py's own `_build_app_with_cors` pattern: a
    minimal app combining the exact rate-limit middleware logic with a
    CORSMiddleware explicitly configured to ALLOW a specific origin --
    needed because the real global app's CORS config defaults to deny-all
    and is frozen at first import (same reason test_cors.py tests
    *configured* CORS behavior against a separate lightweight app, not
    the real one).
    """
    test_app = FastAPI()
    limiter = TokenBucketLimiter(requests_per_second=0.001, burst=1)

    @test_app.middleware("http")
    async def rate_limit(request: Request, call_next):
        allowed, retry_after = limiter.check("probe")
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded"},
                headers={"Retry-After": "5"},
            )
        return await call_next(request)

    test_app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://allowed.example.com"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @test_app.get("/ping")
    def ping():
        return {"ok": True}

    return TestClient(test_app)


def test_429_response_still_carries_correct_cors_headers():
    client = _build_app_with_rate_limit_and_cors()
    headers = {"Origin": "https://allowed.example.com"}

    first = client.get("/ping", headers=headers)
    assert first.status_code == 200
    assert first.headers.get("access-control-allow-origin") == "https://allowed.example.com"

    second = client.get("/ping", headers=headers)
    assert second.status_code == 429
    assert second.headers.get("access-control-allow-origin") == "https://allowed.example.com", (
        "a 429 rate-limit rejection must still carry CORS headers, exactly like a 401/403 "
        "auth rejection already does -- this requires the rate-limit middleware to be "
        "registered BEFORE CORSMiddleware (see codepulse/api/app.py's comment on this)"
    )
