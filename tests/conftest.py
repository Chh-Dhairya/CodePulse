"""
Shared pytest fixtures for API-level tests.

Phase 1.2 requires a valid `X-API-Key` header on every endpoint of the real
`codepulse.api.app` FastAPI app (see `codepulse/api/security.py`). Tests
that exercise that real `app` object must never authenticate against the
real `codepulse.db` (CLAUDE.md §7: never use the real database as a
disposable test database) -- so every such test swaps `codepulse.api.app`'s
module-level `storage_manager`/`indexing_engine` for a temporary, isolated
SQLite database first (the pattern already established in
`tests/test_no_fabricated_metrics.py`'s ingestion test since Phase 0.7),
then mints a real API key against that isolated database via
`codepulse.auth.create_api_key`.

Phase 1.4 adds an autouse fixture resetting the in-process rate limiter's
bucket state before and after every test in the whole suite (not just
API-level ones -- it's a cheap no-op for tests that never touch the API).
Without this, bucket state for shared identities -- most notably the
`request.client.host` fallback bucket every *unauthenticated* request
across the whole suite shares under Starlette's `TestClient` (which
reports a fixed synthetic client host) -- would persist for the entire
pytest process, risking a later test's "no key -> 401" assertion
occasionally observing a 429 instead, depending on exactly how many such
requests happened to run beforehand. Resetting between tests removes that
class of flakiness entirely rather than relying on wall-clock refill
timing to avoid it.
"""
from __future__ import annotations

import os

import pytest

import codepulse.api.app as app_module
from codepulse.auth import create_api_key
from codepulse.indexer.engine import IndexingEngine
from codepulse.rate_limit import reset_limiter
from codepulse.storage.db import RepositoryModel, StorageManager


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    reset_limiter()
    yield
    reset_limiter()


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    """
    Swaps the real app's `storage_manager`/`indexing_engine` for a
    temporary, isolated database and engine, so API-level tests never read
    or write the real `codepulse.db`. Returns the isolated `StorageManager`
    -- pass it to `make_api_key_headers` to mint a real API key against it.
    """
    db_path = tmp_path / "isolated_api_test.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"
    storage = StorageManager(db_url=db_url)
    engine = IndexingEngine(storage=storage)
    monkeypatch.setattr(app_module, "storage_manager", storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    return storage


def make_api_key_headers(
    storage: StorageManager, *, repository_ids: list[int] | None = None, name: str = "test-key",
    role: str = "admin",
) -> dict[str, str]:
    """
    Creates a real API key against `storage` and returns it as request
    headers.

    `role="admin"` by default -- Phase 7.2 (roadmap §37 item 7.2) added
    role-based grants with a SAFER `"viewer"` default on
    `codepulse.auth.create_api_key` itself, but every test written
    before Phase 7.2 (and most written since, since role-boundary
    testing is not most tests' actual concern) implicitly assumes the
    unrestricted, pre-RBAC behavior every key had before this milestone
    -- `role="admin"` here preserves that for the whole existing test
    suite. Tests that specifically exercise role boundaries pass
    `role="viewer"`/`role="indexer"` explicitly.
    """
    created = create_api_key(storage, name=name, repository_ids=repository_ids, role=role)
    return {"X-API-Key": created.raw_key}


def make_creator_headers(storage: StorageManager, *, name: str = "creator-key") -> dict[str, str]:
    """
    Headers for a key that MAY create new repositories: `indexer` on an
    existing (seeded, never indexed) anchor repository.

    Creating a brand-new repository through `POST /v1/repositories/index` used
    to be open to any key, including one with no grants. Since
    FUTURE_CHANGES item 18 (2026-10-14) it needs `indexer` (or higher) on at
    least one existing repository, and the creator becomes `admin` of what it
    creates. Tests that merely need to create a repository use this instead
    of an unscoped key; tests about the rule itself are in
    test_repository_creation_authorization.py.
    """
    import uuid

    session = storage.get_session()
    try:
        anchor = RepositoryModel(name="anchor", url=f"file:///anchor/{uuid.uuid4().hex}")
        session.add(anchor)
        session.commit()
        anchor_id = anchor.id
    finally:
        session.close()
    return make_api_key_headers(storage, repository_ids=[anchor_id], name=name, role="indexer")

