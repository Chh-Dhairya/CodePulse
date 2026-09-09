"""
Phase 1.4 — In-process token-bucket rate limiter.

These tests exercise `codepulse.rate_limit.TokenBucketLimiter` directly
(no HTTP, no database) to verify the token-bucket algorithm itself:
initial burst capacity, real denial once exhausted, a real (not
fabricated) `retry_after` value, refill over real elapsed time, per-
identity isolation, and `reset()`.

HTTP-level behavior (the actual FastAPI middleware, 429 responses,
Retry-After headers, per-API-key/per-IP identity extraction, the
config-driven enable/disable switch, and interaction with CORS) is
covered separately in tests/test_rate_limit_middleware.py.
"""
from __future__ import annotations

import time

from codepulse.rate_limit import TokenBucketLimiter


def test_first_request_is_allowed():
    limiter = TokenBucketLimiter(requests_per_second=1.0, burst=5)
    allowed, retry_after = limiter.check("alice")
    assert allowed is True
    assert retry_after == 0.0


def test_requests_up_to_burst_capacity_are_all_allowed():
    limiter = TokenBucketLimiter(requests_per_second=0.001, burst=5)
    for _ in range(5):
        allowed, _ = limiter.check("alice")
        assert allowed is True


def test_request_beyond_burst_capacity_is_denied():
    limiter = TokenBucketLimiter(requests_per_second=0.001, burst=3)
    for _ in range(3):
        allowed, _ = limiter.check("alice")
        assert allowed is True

    allowed, retry_after = limiter.check("alice")
    assert allowed is False
    assert retry_after > 0.0


def test_denied_request_reports_a_real_positive_retry_after_not_zero_or_fabricated():
    limiter = TokenBucketLimiter(requests_per_second=2.0, burst=1)
    limiter.check("alice")  # consumes the only token

    allowed, retry_after = limiter.check("alice")
    assert allowed is False
    # With 1 missing token and a refill rate of 2/s, real wait time is ~0.5s.
    assert 0.0 < retry_after <= 0.6


def test_tokens_refill_over_real_elapsed_time():
    """
    Uses a fast refill rate + a short real sleep (rather than mocking the
    clock) so this stays a genuine behavioral test without slowing the
    suite down, matching this project's established preference for
    verifying real behavior over assuming it.
    """
    limiter = TokenBucketLimiter(requests_per_second=1000.0, burst=1)
    allowed, _ = limiter.check("alice")
    assert allowed is True

    denied, _ = limiter.check("alice")
    assert denied is False

    time.sleep(0.05)  # 1000 tokens/s * 0.05s = ~50 tokens refilled, capped at burst=1

    allowed_again, _ = limiter.check("alice")
    assert allowed_again is True


def test_different_identities_have_independent_buckets():
    limiter = TokenBucketLimiter(requests_per_second=0.001, burst=1)
    allowed_alice, _ = limiter.check("alice")
    allowed_bob, _ = limiter.check("bob")
    assert allowed_alice is True
    assert allowed_bob is True

    # Alice's bucket is now empty; Bob's is untouched by Alice's usage.
    denied_alice, _ = limiter.check("alice")
    assert denied_alice is False


def test_reset_clears_all_bucket_state():
    limiter = TokenBucketLimiter(requests_per_second=0.001, burst=1)
    limiter.check("alice")
    denied, _ = limiter.check("alice")
    assert denied is False

    limiter.reset()

    allowed_after_reset, _ = limiter.check("alice")
    assert allowed_after_reset is True
