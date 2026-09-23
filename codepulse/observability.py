"""
Phase 7.4 -- Observability wiring (roadmap §37 item 7.4; §18
Observability Architecture: "actually wire the already-declared
dependency" (`prometheus-client`, `structlog`) -- both were declared in
`pyproject.toml` since Phase 0.9 but never installed or imported by any
code until this milestone; both are now genuinely installed and used).

§18, quoted and implemented exactly:
  - **Metrics** (real Prometheus counters/histograms/gauge, named
    exactly as listed): `search_requests_total{search_type}`,
    `search_latency_seconds`, `indexing_jobs_total{status}`,
    `indexing_duration_seconds`, `files_indexed_total`,
    `symbols_indexed_total`, `embedding_duration_seconds`,
    `job_queue_depth`, `api_errors_total{endpoint,status_code}`,
    `search_zero_result_total`.
  - **Logs**: structured JSON via `structlog`, with the exact field
    set §18 names: `request_id`, `repository_id`, `job_id`,
    `api_key_id`, `operation`, `duration_ms`, `error_type`.
  - **Health endpoints** (`codepulse/api/app.py`): `/health/live`
    (process up) and `/health/ready` (DB reachable, embedding model
    loaded, index storage writable) -- built in `app.py` itself using
    this module's `check_readiness`, kept out of this module because
    they need live references to `storage_manager`/`indexing_engine`.

This module holds a DEDICATED `CollectorRegistry` (not
`prometheus_client`'s global default) so importing it repeatedly across
this project's own test suite (many isolated test modules, potentially
several `TestClient(app)` instances per test session) can never raise
"Duplicated timeseries in CollectorRegistry" -- module-level singletons
are created exactly once per Python process regardless of how many
tests import this module, which is what makes a dedicated registry
unnecessary in principle, but using one anyway is the standard,
recommended `prometheus_client` pattern for avoiding any accidental
collision with a DIFFERENT library's own use of the global default
registry in the same process.

Every counter/histogram below is incremented/observed from a REAL,
already-happening event (a request actually being served, a job
actually transitioning state, an embedding call actually completing) --
never a fabricated/synthetic value, per CLAUDE.md's "no fabricated
metrics" invariant applied to observability data too.
"""
from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest

REGISTRY = CollectorRegistry()

search_requests_total = Counter(
    "search_requests_total", "Total search requests served, by search_type", ["search_type"], registry=REGISTRY
)
search_latency_seconds = Histogram(
    "search_latency_seconds", "Search request latency in seconds", registry=REGISTRY
)
search_zero_result_total = Counter(
    "search_zero_result_total", "Total search requests that returned zero results", registry=REGISTRY
)
indexing_jobs_total = Counter(
    "indexing_jobs_total", "Total indexing jobs, by terminal/transition status", ["status"], registry=REGISTRY
)
indexing_duration_seconds = Histogram(
    "indexing_duration_seconds", "Wall-clock duration of one index_directory() run, in seconds", registry=REGISTRY
)
files_indexed_total = Counter(
    "files_indexed_total", "Total files processed across all indexing runs", registry=REGISTRY
)
symbols_indexed_total = Counter(
    "symbols_indexed_total", "Total symbols processed across all indexing runs", registry=REGISTRY
)
embedding_duration_seconds = Histogram(
    "embedding_duration_seconds", "Duration of one CodeEmbedder.encode() call, in seconds", registry=REGISTRY
)
job_queue_depth = Gauge(
    "job_queue_depth", "Current number of queued+running IndexJob rows", registry=REGISTRY
)
api_errors_total = Counter(
    "api_errors_total", "Total error responses, by endpoint path and status code", ["endpoint", "status_code"], registry=REGISTRY
)


def render_metrics() -> tuple[bytes, str]:
    """Returns `(body, content_type)` for the `/metrics` endpoint -- the real, current state of `REGISTRY`, generated fresh on every call (never cached/stale)."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


_logging_configured = False


def configure_logging() -> None:
    """
    Configures `structlog` for JSON-rendered structured log output, per
    §18: "structured (JSON via structlog)... every log line carries
    enough to reconstruct a request's full path." Idempotent -- safe to
    call more than once (e.g. once per test module importing
    `codepulse.api.app`), only actually reconfigures the first time.
    """
    global _logging_configured
    if _logging_configured:
        return
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )
    _logging_configured = True


def get_logger() -> Any:
    """
    Returns a structlog bound logger. §18's own exact field set
    (`request_id`, `repository_id`, `job_id`, `api_key_id`,
    `operation`, `duration_ms`, `error_type`) is bound by each CALL
    SITE via `.bind(...)`/keyword arguments -- this function does not
    hardcode which fields a given log line carries, since not every
    operation has all of them (e.g. a search request has no `job_id`).
    """
    configure_logging()
    return structlog.get_logger()
