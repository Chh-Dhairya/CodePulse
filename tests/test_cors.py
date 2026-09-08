"""
Phase 0.9 — Fix CORS policy defaults.

Verifies actual HTTP/middleware behavior (real response headers for real
requests carrying an `Origin` header), not merely that a config constant
holds a particular value.

The real `codepulse.api.app` app object has its `CORSMiddleware` wired up
once, at module-import time, from whatever config was active then (the
built-in defaults, since no test sets a CORS-related env var before this
module gets imported anywhere in the session) -- so it's used directly to
verify default behavior. For *configured* origin/credentials behavior, a
lightweight standalone app is built per test, using the exact same
`CORSMiddleware` wiring `codepulse/api/app.py` uses (`allow_methods=["*"]`,
`allow_headers=["*"]`), parameterized by an explicit `CodePulseConfig` --
this avoids reconstructing the real, heavyweight app (which loads a real
StorageManager/IndexingEngine) just to exercise different CORS settings.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from pydantic import ValidationError

from codepulse.api.app import app as real_app
from codepulse.config import CodePulseConfig, reset_settings

real_client = TestClient(real_app)


@pytest.fixture(autouse=True)
def cleanup_settings():
    reset_settings()
    yield
    reset_settings()


def _build_app_with_cors(settings: CodePulseConfig) -> TestClient:
    test_app = FastAPI()
    test_app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @test_app.get("/ping")
    def ping():
        return {"ok": True}

    return TestClient(test_app)


# --- 1. Default origin behavior (the real, running app) --------------------

def test_default_cors_denies_all_origins():
    response = real_client.get(
        "/v1/autocomplete?prefix=x", headers={"Origin": "http://evil.example.com"}
    )
    assert response.status_code == 200
    # No origin is allowed by default -- the browser-enforced header must be
    # absent entirely (deny), not a wildcard.
    assert "access-control-allow-origin" not in response.headers


def test_default_cors_preflight_grants_nothing():
    response = real_client.options(
        "/v1/autocomplete",
        headers={"Origin": "http://evil.example.com", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in response.headers


def test_default_credentials_header_absent():
    response = real_client.get(
        "/v1/autocomplete?prefix=x", headers={"Origin": "http://example.com"}
    )
    assert "access-control-allow-credentials" not in response.headers


# --- 2. Allowed origin behavior (configured) --------------------------------

def test_configured_origin_is_allowed():
    settings = CodePulseConfig(
        cors_origins=["https://allowed.example.com"], cors_allow_credentials=False
    )
    client = _build_app_with_cors(settings)

    response = client.get("/ping", headers={"Origin": "https://allowed.example.com"})
    assert response.headers.get("access-control-allow-origin") == "https://allowed.example.com"


# --- 3. Disallowed origin behavior (configured, but a different origin) ----

def test_non_configured_origin_is_denied():
    settings = CodePulseConfig(
        cors_origins=["https://allowed.example.com"], cors_allow_credentials=False
    )
    client = _build_app_with_cors(settings)

    response = client.get("/ping", headers={"Origin": "https://attacker.example.com"})
    assert "access-control-allow-origin" not in response.headers


# --- 4. Credentials behavior -------------------------------------------------

def test_credentials_header_present_when_configured_true():
    settings = CodePulseConfig(
        cors_origins=["https://allowed.example.com"], cors_allow_credentials=True
    )
    client = _build_app_with_cors(settings)

    response = client.get("/ping", headers={"Origin": "https://allowed.example.com"})
    assert response.headers.get("access-control-allow-credentials") == "true"


def test_credentials_header_absent_when_configured_false():
    settings = CodePulseConfig(
        cors_origins=["https://allowed.example.com"], cors_allow_credentials=False
    )
    client = _build_app_with_cors(settings)

    response = client.get("/ping", headers={"Origin": "https://allowed.example.com"})
    assert "access-control-allow-credentials" not in response.headers


# --- 5. The invalid wildcard+credentials combination is rejected -----------

def test_wildcard_origin_with_credentials_is_rejected_at_config_load():
    with pytest.raises(ValidationError):
        CodePulseConfig(cors_origins=["*"], cors_allow_credentials=True)


def test_wildcard_origin_without_credentials_remains_valid():
    """A non-credentialed wildcard is still a legal (if permissive) config
    -- only the credentialed combination is rejected."""
    settings = CodePulseConfig(cors_origins=["*"], cors_allow_credentials=False)
    client = _build_app_with_cors(settings)

    response = client.get("/ping", headers={"Origin": "https://anything.example.com"})
    assert response.headers.get("access-control-allow-origin") == "*"
