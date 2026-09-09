"""
Phase 1.3 — IndexJob state machine + /v1/jobs/{id}.

These tests exercise `codepulse.jobs` directly against a temporary,
disposable SQLite database (never the production codepulse.db) to verify:

  - A new job starts in the `queued` state.
  - `mark_running`/`mark_succeeded`/`mark_failed` transition it correctly
    and record real timestamps.
  - `result_summary` round-trips a real result dict through JSON.
  - `get_active_job` implements the roadmap §8 "one active job per
    repository (advisory lock)" check: it finds a queued/running job but
    never a terminal (succeeded/failed/cancelled) one.
  - `get_job` returns None for a nonexistent id.

HTTP-level behavior (the /v1/repositories/index and /v1/jobs/{id}
endpoints, authorization, the 409 concurrency response) is covered
separately in tests/test_job_endpoints.py.
"""
from __future__ import annotations

import os

import pytest

from codepulse.jobs import (
    create_job,
    get_active_job,
    get_job,
    mark_failed,
    mark_running,
    mark_succeeded,
)
from codepulse.storage.db import RepositoryModel, StorageManager


@pytest.fixture
def storage(tmp_path):
    db_path = tmp_path / "test_index_jobs.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"
    return StorageManager(db_url=db_url)


def _seed_repo(storage: StorageManager, name: str = "r1") -> int:
    session = storage.get_session()
    try:
        repo = RepositoryModel(name=name, url=f"file:///{name}")
        session.add(repo)
        session.commit()
        return repo.id
    finally:
        session.close()


# --- Creation and initial state ----------------------------------------------

def test_create_job_starts_in_queued_state(storage):
    repo_id = _seed_repo(storage)
    job = create_job(storage, repo_id)

    assert job.repo_id == repo_id
    assert job.status == "queued"
    assert job.created_at is not None
    assert job.started_at is None
    assert job.finished_at is None
    assert job.error_message is None
    assert job.result is None


def test_two_jobs_for_the_same_repository_get_distinct_ids(storage):
    repo_id = _seed_repo(storage)
    job1 = create_job(storage, repo_id)
    job2 = create_job(storage, repo_id)
    assert job1.id != job2.id


# --- State transitions --------------------------------------------------------

def test_mark_running_transitions_and_sets_started_at(storage):
    repo_id = _seed_repo(storage)
    job = create_job(storage, repo_id)

    mark_running(storage, job.id)

    updated = get_job(storage, job.id)
    assert updated.status == "running"
    assert updated.started_at is not None
    assert updated.finished_at is None


def test_mark_succeeded_transitions_and_stores_real_result(storage):
    repo_id = _seed_repo(storage)
    job = create_job(storage, repo_id)
    mark_running(storage, job.id)

    real_result = {
        "repository_name": "r1",
        "repository_id": repo_id,
        "total_files": 3,
        "total_symbols": 12,
        "embedding_status": "success",
        "degraded_mode": False,
    }
    mark_succeeded(storage, job.id, real_result)

    updated = get_job(storage, job.id)
    assert updated.status == "succeeded"
    assert updated.finished_at is not None
    assert updated.error_message is None
    assert updated.result == real_result, "result must round-trip through JSON unchanged"


def test_mark_failed_transitions_and_stores_error(storage):
    repo_id = _seed_repo(storage)
    job = create_job(storage, repo_id)
    mark_running(storage, job.id)

    mark_failed(storage, job.id, "boom: disk full")

    updated = get_job(storage, job.id)
    assert updated.status == "failed"
    assert updated.finished_at is not None
    assert updated.error_message == "boom: disk full"
    assert updated.result is None


def test_mark_running_on_a_nonexistent_job_does_not_raise(storage):
    """Defensive no-op, matching the same defensive pattern used throughout this module."""
    mark_running(storage, 999999)  # must not raise
    mark_succeeded(storage, 999999, {"x": 1})  # must not raise
    mark_failed(storage, 999999, "err")  # must not raise


# --- get_job ------------------------------------------------------------------

def test_get_job_returns_none_for_nonexistent_id(storage):
    assert get_job(storage, 999999) is None


def test_get_job_returns_current_state(storage):
    repo_id = _seed_repo(storage)
    job = create_job(storage, repo_id)
    fetched = get_job(storage, job.id)
    assert fetched.id == job.id
    assert fetched.repo_id == repo_id
    assert fetched.status == "queued"


# --- get_active_job / advisory lock -------------------------------------------

def test_get_active_job_returns_none_when_no_job_exists(storage):
    repo_id = _seed_repo(storage)
    assert get_active_job(storage, repo_id) is None


def test_get_active_job_finds_a_queued_job(storage):
    repo_id = _seed_repo(storage)
    job = create_job(storage, repo_id)
    active = get_active_job(storage, repo_id)
    assert active is not None
    assert active.id == job.id


def test_get_active_job_finds_a_running_job(storage):
    repo_id = _seed_repo(storage)
    job = create_job(storage, repo_id)
    mark_running(storage, job.id)
    active = get_active_job(storage, repo_id)
    assert active is not None
    assert active.status == "running"


def test_get_active_job_ignores_succeeded_jobs(storage):
    repo_id = _seed_repo(storage)
    job = create_job(storage, repo_id)
    mark_running(storage, job.id)
    mark_succeeded(storage, job.id, {"total_files": 1})

    assert get_active_job(storage, repo_id) is None


def test_get_active_job_ignores_failed_jobs(storage):
    repo_id = _seed_repo(storage)
    job = create_job(storage, repo_id)
    mark_running(storage, job.id)
    mark_failed(storage, job.id, "err")

    assert get_active_job(storage, repo_id) is None


def test_get_active_job_is_scoped_to_its_own_repository(storage):
    repo_a = _seed_repo(storage, "repo-a")
    repo_b = _seed_repo(storage, "repo-b")
    create_job(storage, repo_a)

    assert get_active_job(storage, repo_a) is not None
    assert get_active_job(storage, repo_b) is None


