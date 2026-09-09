"""
Phase 1.4 — In-process token-bucket rate limiter (roadmap §37 item 1.4;
§6 "Recommended Architecture": rate limits live in the control plane /
API Gateway layer, alongside auth and central authorization; §35's
architecture diagram places "Rate limits" directly beside "Auth (API
keys)" and "Central authz"; backlog B11).

Scope, precisely (do not infer requirements from later milestones):
  - A classic token-bucket algorithm (capacity + refill rate), entirely
    in-process (a plain in-memory dict) -- no Redis, no database
    persistence, no distributed state. State resets on process restart;
    this project runs as one process, matching "in-process" literally
    (roadmap §37/§45: "rate limiting (in-process token bucket, no Redis
    yet)").
  - One bucket per client identity: the raw `X-API-Key` header value if
    present (and non-empty), else the client's IP address as a fallback
    for unauthenticated requests -- so an unauthenticated flood doesn't
    get an unlimited exemption, but distinct callers don't share one
    bucket either. This does NOT verify the key (no DB lookup, no import
    of `codepulse.auth`/`codepulse.api.security` here) -- the bucket is
    keyed on the raw string alone, kept deliberately cheap and
    independent; validity checking remains exactly `require_api_key`'s
    job (Phase 1.2), unchanged and un-duplicated.
  - Exceeding the bucket's capacity is reported as HTTP 429 with a real
    `Retry-After` value (backlog B11's literal DoD: "Excess requests
    receive 429 with retry-after"), computed as the actual number of
    seconds until at least one token will be available -- never a
    fabricated/estimated value.

Deliberately NOT in scope for Phase 1.4 (do not infer these belong here):
  - Per-role/per-plan differentiated limits (roadmap §15's RBAC roles are
    Phase 7 / B25 scope, not modeled at all yet).
  - Distributed/shared rate limiting across multiple processes or
    workers (explicitly deferred -- "no Redis yet").
  - A structured `{"error": {...}}` response envelope for the 429 body --
    Phase 1.6. The middleware in `codepulse/api/app.py` that uses this
    module returns a plain `{"detail": "..."}` body, matching FastAPI's
    own default `HTTPException` shape and every other error path already
    in `api/app.py`.
  - Ingestion-specific safety limits (path allow-listing, SSRF checks,
    size caps) -- Phase 1.5 / B10, an entirely separate concern from
    request-rate limiting.

Default parameters (`CodePulseConfig.rate_limit_requests_per_second`,
`.rate_limit_burst`) are a reasonable *starting* configuration for
local-first, single-developer use -- NOT a benchmarked/measured figure,
matching roadmap §19's own caution against presenting unvalidated
numbers as fact. Tune via config for a given workload.

This module lives outside `codepulse/indexer/` and `codepulse/api/`
deliberately -- a top-level module, mirroring `codepulse/auth.py` and
`codepulse/jobs.py`'s placement, per the roadmap's own "control plane"
grouping (§6) of auth/rate-limits/job-tracking as one layer, reusable
identically by a future CLI or MCP surface (though today only the REST
app installs it, as middleware).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class TokenBucketLimiter:
    """
    A plain in-memory token-bucket rate limiter, one bucket per identity
    string. Thread-safe via a single lock guarding the bucket dict --
    cheap and sufficient for this project's single-process, in-process
    scale (no Redis, no multi-process coordination).
    """

    def __init__(self, requests_per_second: float, burst: int) -> None:
        self.requests_per_second = requests_per_second
        self.burst = burst
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def check(self, identity: str) -> tuple[bool, float]:
        """
        Attempts to consume one token for `identity`. Returns
        `(allowed, retry_after_seconds)`: `retry_after_seconds` is `0.0`
        when `allowed` is True, or the real number of seconds until at
        least one token will be available when it is False.
        """
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(identity)
            if bucket is None:
                bucket = _Bucket(tokens=float(self.burst), last_refill=now)
                self._buckets[identity] = bucket

            elapsed = max(0.0, now - bucket.last_refill)
            bucket.tokens = min(float(self.burst), bucket.tokens + elapsed * self.requests_per_second)
            bucket.last_refill = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0.0

            missing = 1.0 - bucket.tokens
            retry_after = missing / self.requests_per_second if self.requests_per_second > 0 else float("inf")
            return False, retry_after

    def reset(self) -> None:
        """Clears all bucket state -- primarily for test isolation."""
        with self._lock:
            self._buckets.clear()


_DEFAULT_LIMITER: TokenBucketLimiter | None = None


def get_limiter() -> TokenBucketLimiter:
    """
    Returns the process-wide default `TokenBucketLimiter`, constructed
    from the currently active `CodePulseConfig` on first use (and cached
    -- call `reset_limiter()` after changing rate-limit settings, e.g. in
    tests, matching the same pattern `codepulse.config.reset_settings()`
    already establishes for the settings singleton itself).
    """
    global _DEFAULT_LIMITER
    if _DEFAULT_LIMITER is None:
        from codepulse.config import get_settings

        settings = get_settings()
        _DEFAULT_LIMITER = TokenBucketLimiter(
            requests_per_second=settings.rate_limit_requests_per_second,
            burst=settings.rate_limit_burst,
        )
    return _DEFAULT_LIMITER


def reset_limiter() -> None:
    """Resets the module-level default limiter -- primarily for test isolation."""
    global _DEFAULT_LIMITER
    _DEFAULT_LIMITER = None
