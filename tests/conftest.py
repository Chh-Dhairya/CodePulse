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
"""
from __future__ import annotations

import os

import pytest

import codepulse.api.app as app_module
from codepulse.auth import create_api_key
from codepulse.indexer.engine import IndexingEngine
from codepulse.storage.db import StorageManager


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
    storage: StorageManager, *, repository_ids: list[int] | None = None, name: str = "test-key"
) -> dict[str, str]:
    """Creates a real API key against `storage` and returns it as request headers."""
    created = create_api_key(storage, name=name, repository_ids=repository_ids)
    return {"X-API-Key": created.raw_key}
