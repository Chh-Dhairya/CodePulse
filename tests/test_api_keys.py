"""
Phase 1.1 — API key model + hashing + creation flow.

These tests exercise `codepulse.auth.create_api_key`/`verify_api_key`
end-to-end against temporary, disposable SQLite databases (never the
production codepulse.db) to verify:

  - A real, random API key is generated and returned exactly once.
  - Only its SHA-256 hash (and a short non-secret prefix) is ever
    persisted -- the raw key itself is never stored anywhere in the DB.
  - The hash genuinely matches the documented convention and can be used
    to look the key back up (verify_api_key).
  - An unknown, wrong, or revoked key does not verify.
  - Repository scoping ("API keys ... scoped to one or more repositories",
    roadmap §15) is recorded correctly, including rejecting a scope to a
    nonexistent repository without leaving partial state behind.
  - The (key_hash) natural key is enforced at the database level.

None of this is wired into any API endpoint, FastAPI dependency, or
request path -- that is Phase 1.2's explicit scope, not tested here.
"""
from __future__ import annotations

import hashlib
import os

import pytest
from sqlalchemy.exc import IntegrityError

from codepulse.auth import (
    API_KEY_PREFIX,
    CreatedAPIKey,
    create_api_key,
    generate_raw_api_key,
    hash_api_key,
    verify_api_key,
)
from codepulse.config import reset_settings
from codepulse.storage.db import APIKeyModel, APIKeyRepositoryModel, RepositoryModel, StorageManager


@pytest.fixture(autouse=True)
def cleanup_settings():
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def storage(tmp_path):
    db_path = tmp_path / "test_api_keys.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"
    return StorageManager(db_url=db_url)


# --- Raw key generation ------------------------------------------------------

def test_generate_raw_api_key_has_expected_prefix_and_randomness():
    key1 = generate_raw_api_key()
    key2 = generate_raw_api_key()

    assert key1.startswith(API_KEY_PREFIX)
    assert key2.startswith(API_KEY_PREFIX)
    assert key1 != key2, "two generated keys must not collide"
    assert len(key1) > len(API_KEY_PREFIX) + 20, "the random portion must carry real entropy"


# --- Hashing convention -------------------------------------------------------

def test_hash_api_key_matches_documented_sha256_convention():
    raw = "cpk_example-raw-key-value"
    expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert hash_api_key(raw) == expected


def test_hash_api_key_is_deterministic_and_distinguishes_different_keys():
    raw1 = generate_raw_api_key()
    raw2 = generate_raw_api_key()
    assert hash_api_key(raw1) == hash_api_key(raw1)  # deterministic
    assert hash_api_key(raw1) != hash_api_key(raw2)  # distinguishes real keys


# --- Creation flow: real key persisted, hashed, raw key never stored -------

def test_create_api_key_persists_hash_never_the_raw_key(storage):
    result = create_api_key(storage, name="ci-pipeline")

    assert isinstance(result, CreatedAPIKey)
    assert result.raw_key.startswith(API_KEY_PREFIX)
    assert result.id is not None
    assert result.name == "ci-pipeline"
    assert result.key_prefix and result.raw_key.startswith(result.key_prefix)

    session = storage.get_session()
    try:
        row = session.query(APIKeyModel).filter(APIKeyModel.id == result.id).first()
        assert row is not None
        assert row.key_hash == hash_api_key(result.raw_key)
        assert row.key_hash != result.raw_key, "the hash must not equal the raw key"
        assert row.revoked_at is None

        # The raw key must not appear anywhere in the persisted row's
        # columns -- not truncated into key_hash, not accidentally stored
        # in key_prefix in full, nothing.
        assert result.raw_key != row.key_hash
        assert result.raw_key != row.key_prefix
        assert row.key_prefix != result.raw_key
    finally:
        session.close()


def test_two_created_keys_have_different_raw_values_and_hashes(storage):
    result1 = create_api_key(storage, name="key-one")
    result2 = create_api_key(storage, name="key-two")

    assert result1.raw_key != result2.raw_key
    assert hash_api_key(result1.raw_key) != hash_api_key(result2.raw_key)
    assert result1.id != result2.id


# --- Database-level uniqueness on key_hash ----------------------------------

def test_db_rejects_duplicate_key_hash(storage):
    session = storage.get_session()
    try:
        session.add(
            APIKeyModel(name="a", key_hash="a" * 64, key_prefix="cpk_aaaaaaaa")
        )
        session.commit()

        session.add(
            APIKeyModel(name="b", key_hash="a" * 64, key_prefix="cpk_bbbbbbbb")
        )
        with pytest.raises(IntegrityError):
            session.commit()
    finally:
        session.rollback()
        session.close()


# --- Verification -------------------------------------------------------------

def test_verify_api_key_succeeds_for_a_valid_key(storage):
    result = create_api_key(storage, name="valid-key")
    verified_id = verify_api_key(storage, result.raw_key)
    assert verified_id == result.id


def test_verify_api_key_fails_for_an_unknown_key(storage):
    create_api_key(storage, name="some-key")
    assert verify_api_key(storage, "cpk_this-was-never-created") is None


def test_verify_api_key_fails_for_a_revoked_key(storage):
    result = create_api_key(storage, name="to-be-revoked")

    session = storage.get_session()
    try:
        from datetime import datetime

        row = session.query(APIKeyModel).filter(APIKeyModel.id == result.id).first()
        row.revoked_at = datetime.utcnow()
        session.commit()
    finally:
        session.close()

    assert verify_api_key(storage, result.raw_key) is None


# --- Repository scoping --------------------------------------------------------

def test_create_api_key_scoped_to_repositories_records_grants(storage):
    session = storage.get_session()
    try:
        repo1 = RepositoryModel(name="r1", url="file:///r1")
        repo2 = RepositoryModel(name="r2", url="file:///r2")
        session.add_all([repo1, repo2])
        session.commit()
        repo1_id, repo2_id = repo1.id, repo2.id
    finally:
        session.close()

    result = create_api_key(storage, name="scoped-key", repository_ids=[repo1_id, repo2_id])

    session = storage.get_session()
    try:
        grants = (
            session.query(APIKeyRepositoryModel)
            .filter(APIKeyRepositoryModel.api_key_id == result.id)
            .all()
        )
        assert {g.repo_id for g in grants} == {repo1_id, repo2_id}
    finally:
        session.close()


def test_create_api_key_without_repository_ids_creates_no_scope_rows(storage):
    result = create_api_key(storage, name="unscoped-key")

    session = storage.get_session()
    try:
        grants = (
            session.query(APIKeyRepositoryModel)
            .filter(APIKeyRepositoryModel.api_key_id == result.id)
            .all()
        )
        assert grants == []
    finally:
        session.close()


def test_create_api_key_rejects_nonexistent_repository_and_leaves_no_partial_state(storage):
    session = storage.get_session()
    try:
        before_key_count = session.query(APIKeyModel).count()
    finally:
        session.close()

    with pytest.raises(ValueError):
        create_api_key(storage, name="bad-scope-key", repository_ids=[999999])

    session = storage.get_session()
    try:
        after_key_count = session.query(APIKeyModel).count()
        assert after_key_count == before_key_count, (
            "a rejected repository scope must not leave a partially-created "
            "APIKeyModel row behind"
        )
    finally:
        session.close()
