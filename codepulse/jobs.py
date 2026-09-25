"""
Phase 1.3 — IndexJob state machine + /v1/jobs/{id} (roadmap §37 item 1.3;
§8 "Concurrency/failure handling"; §21; backlog B9).

Scope, precisely (do not infer requirements from later milestones):
  - Create a real, DB-backed job row for one indexing run, in the initial
    `queued` state.
  - Transition it through the roadmap §8 state machine: `queued ->
    running -> succeeded|failed`. `cancelled` is a valid schema value
    (see `IndexJobModel`) but this module exposes no way to reach it --
    no cancel endpoint/flow exists yet.
  - An "advisory lock" (roadmap §8's own wording) preventing a second
    concurrent indexing run for the same repository: `get_active_job`
    lets a caller check for -- and refuse to start alongside -- an
    existing `queued`/`running` job for that repository. This is an
    application-level check, not a hard database constraint: this
    project runs indexing as in-process background tasks, not multiple
    concurrent worker processes, so a stronger lock is not needed at this
    scale, and the roadmap itself only calls for "advisory."
  - A read path (`get_job`) returning everything `GET /v1/jobs/{job_id}`
    needs to report real, measured status -- never a fabricated/estimated
    one.

Deliberately NOT in scope for Phase 1.3 (reserved for later milestones --
do not infer these belong here):
  - A heartbeat-based crash reaper reclaiming a `running` job whose worker
    died (roadmap §17 mentions this as a *reliability* mechanism, but no
    sprint item assigns it a specific milestone; this project has no
    subprocess/worker infrastructure for a "crashed worker" to even be a
    distinct case from "this process's background task raised" -- the
    latter IS handled here, via `mark_failed`, since `run_indexing`'s own
    try/except in `api/app.py` calls it on any exception). Backend-hardening
    addition (not a roadmap sprint item): `reap_stale_jobs` below IS that
    reaper, added later -- see its own docstring for why a startup-time
    reconciliation, not a heartbeat/timeout, is the correct mechanism for
    this project's actual single-process architecture.
  - `RepositorySnapshot` / git commit tracking / incremental indexing
    (roadmap §9, explicitly Phase 3 sprint items 3.1-3.5) -- this module
    has no concept of a snapshot; `IndexingEngine.index_directory()` is
    still a full re-index every time, unchanged by this milestone.
  - A cancel endpoint/flow, job expiry/cleanup, rate limiting (Phase 1.4),
    ingestion safety (Phase 1.5), a structured error envelope (Phase 1.6).

This module lives outside `codepulse/indexer/` deliberately, mirroring
`codepulse/auth.py`'s placement: `IndexingEngine.index_directory()` has no
knowledge of jobs at all and is not modified by this milestone -- the
caller (currently `codepulse/api/app.py`'s background task) is entirely
responsible for calling `mark_running`/`mark_succeeded`/`mark_failed`
around its own call to `index_directory()`. It is a top-level module
(not under `codepulse/api/`) so a future CLI or MCP surface that also
triggers indexing (roadmap: "REST, CLI, and MCP must call the same
underlying intelligence functions") can reuse it identically, exactly as
`codepulse/auth.py` is already structured for reuse beyond REST alone.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from codepulse.storage.db import IndexJobModel, StorageManager

ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")


@dataclass(frozen=True)
class JobInfo:
    """
    A snapshot of one IndexJob row's state, safe to hold after its
    originating session has closed (see `StorageManager.SessionLocal`'s
    `expire_on_commit=True` default -- the same reason
    `codepulse.auth.CreatedAPIKey` is a frozen dataclass rather than a raw
    ORM object, established in Phase 1.1).
    """

    id: int
    repo_id: int
    status: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    result: dict[str, Any] | None


def _to_job_info(job: IndexJobModel) -> JobInfo:
    return JobInfo(
        id=job.id,
        repo_id=job.repo_id,
        status=job.status,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        error_message=job.error_message,
        result=json.loads(job.result_summary) if job.result_summary else None,
    )


def get_active_job(storage: StorageManager, repo_id: int) -> JobInfo | None:
    """
    Roadmap §8: "one active job per repository (advisory lock on
    repo_id)". Returns the most recent `queued`/`running` job for
    `repo_id`, if any -- callers use this to refuse starting a second
    concurrent indexing run for the same repository, rather than letting
    two runs write conflicting data at once.
    """
    session = storage.get_session()
    try:
        job = (
            session.query(IndexJobModel)
            .filter(IndexJobModel.repo_id == repo_id, IndexJobModel.status.in_(ACTIVE_STATUSES))
            .order_by(IndexJobModel.id.desc())
            .first()
        )
        return _to_job_info(job) if job else None
    finally:
        session.close()


def create_job(storage: StorageManager, repo_id: int) -> JobInfo:
    """Creates a new IndexJob row in the initial `queued` state."""
    session = storage.get_session()
    try:
        job = IndexJobModel(repo_id=repo_id, status="queued", created_at=datetime.utcnow())
        session.add(job)
        session.commit()
        return _to_job_info(job)
    finally:
        session.close()


def mark_running(storage: StorageManager, job_id: int) -> None:
    """Transitions a job `queued -> running`, recording `started_at`."""
    session = storage.get_session()
    try:
        job = session.query(IndexJobModel).filter(IndexJobModel.id == job_id).first()
        if job is not None:
            job.status = "running"
            job.started_at = datetime.utcnow()
            session.commit()
    finally:
        session.close()
    # Phase 7.4 (roadmap §37 item 7.4; §18: "indexing_jobs_total{status}"):
    # real state-transition counter, incremented at the exact moment
    # this transition actually happens -- not a synthetic/estimated value.
    from codepulse.observability import indexing_jobs_total
    indexing_jobs_total.labels(status="running").inc()


def mark_succeeded(storage: StorageManager, job_id: int, result: dict[str, Any]) -> None:
    """
    Transitions a job `running -> succeeded`, recording `finished_at` and
    `result` -- `result` must be the real, measured return value of
    `IndexingEngine.index_directory()` (never an estimated/fabricated
    summary), per this project's "no fabricated metrics" invariant.
    """
    session = storage.get_session()
    try:
        job = session.query(IndexJobModel).filter(IndexJobModel.id == job_id).first()
        if job is not None:
            job.status = "succeeded"
            job.finished_at = datetime.utcnow()
            job.result_summary = json.dumps(result)
            session.commit()
    finally:
        session.close()
    from codepulse.observability import indexing_jobs_total
    indexing_jobs_total.labels(status="succeeded").inc()


def mark_failed(storage: StorageManager, job_id: int, error: str) -> None:
    """Transitions a job `running -> failed`, recording `finished_at` and `error`."""
    session = storage.get_session()
    try:
        job = session.query(IndexJobModel).filter(IndexJobModel.id == job_id).first()
        if job is not None:
            job.status = "failed"
            job.finished_at = datetime.utcnow()
            job.error_message = error
            session.commit()
    finally:
        session.close()
    from codepulse.observability import indexing_jobs_total
    indexing_jobs_total.labels(status="failed").inc()


def reap_stale_jobs(storage: StorageManager) -> list[int]:
    """
    Backend-hardening addition (not a roadmap sprint item) -- closes the
    one remaining gap this module's own docstring has disclosed since
    Phase 1.3: a `running` job whose background task is killed by a real
    process crash used to stay `running` forever, permanently blocking
    `get_active_job`'s advisory lock for that repository (every future
    indexing attempt would see an "active" job that can never finish and
    refuse to start).

    Deliberately a STARTUP-TIME reconciliation, not a heartbeat/timeout
    mechanism: this project runs indexing as in-process `BackgroundTasks`
    coroutines (roadmap §8's own "advisory lock... this project runs
    indexing as in-process background tasks, not multiple concurrent
    worker processes" framing, already established for `get_active_job`),
    with no persisted "which process owns this job" tracking and no
    heartbeat column anywhere in the schema. In that architecture, a job
    still `queued`/`running` at the moment a NEW `IndexingEngine` is
    constructed can only be a leftover from a PREVIOUS process that no
    longer exists -- the current process could not have created it (it
    just started), and no other process could be concurrently progressing
    it (this project has no distributed worker pool). A timeout-based
    heuristic would need a new column, a migration, and an arbitrary
    staleness threshold that either reaps a genuinely slow-but-healthy
    job too early or a genuinely crashed one too late; this mechanism
    needs neither and cannot produce a false positive for THIS
    architecture's actual failure mode.

    Reuses `mark_failed` (not a hand-rolled column update) so this stays
    the single source of truth for what a `-> failed` transition means,
    including its own existing `indexing_jobs_total{status="failed"}`
    metric increment -- a reaped job is counted exactly like any other
    real failure, not a separate, uncounted code path.

    Called once from `IndexingEngine.__init__`, mirroring `hydrate_all`'s
    own "reconcile whatever happened before this process began"
    philosophy and timing. Returns the ids of every job reaped (empty if
    none were stale) -- primarily for logging/testing, not a public API.
    """
    session = storage.get_session()
    try:
        stale_ids = [
            job.id
            for job in session.query(IndexJobModel)
            .filter(IndexJobModel.status.in_(ACTIVE_STATUSES))
            .all()
        ]
    finally:
        session.close()

    for job_id in stale_ids:
        mark_failed(
            storage,
            job_id,
            "Reaped at startup: this job was still queued/running when the "
            "previous process ended (crash, restart, or forced shutdown) "
            "and can never complete.",
        )
    return stale_ids


def get_job(storage: StorageManager, job_id: int) -> JobInfo | None:
    """Returns the current state of one job, or None if no such job exists."""
    session = storage.get_session()
    try:
        job = session.query(IndexJobModel).filter(IndexJobModel.id == job_id).first()
        return _to_job_info(job) if job else None
    finally:
        session.close()
