"""
Phase 2.5 — Benchmark harness unit tests (roadmap §37 item 2.5).

Fast, pytest-collected tests against `tests/benchmark/harness.py`'s own
pure logic (fixture generation validity, percentile math, latency-stats
aggregation) using tiny synthetic inputs -- no real large-scale indexing
or embedding here. The actual expensive 3-tier benchmark run is
`tests/benchmark/run_benchmark.py`, a standalone script, deliberately
NOT part of this file or the routine test suite (see harness.py's own
module docstring for why).
"""
from __future__ import annotations

import time

from codepulse.parser.factory import get_parser_for_file
from tests.benchmark.harness import (
    LatencyStats,
    generate_fixture_file,
    generate_fixture_repo,
    measure_search_latency,
    percentile,
    sample_queries,
)


# =============================================================================
# Fixture generation
# =============================================================================

def test_generate_fixture_file_is_valid_parseable_python(tmp_path):
    import random

    content = generate_fixture_file(index=0, symbols_per_file=3, rng=random.Random(1))
    f = tmp_path / "mod.py"
    f.write_text(content, encoding="utf-8")

    parser = get_parser_for_file(str(f))
    assert parser is not None
    result = parser.parse_file("mod.py", content)
    assert len(result.symbols) == 3
    for sym in result.symbols:
        assert sym.docstring  # every generated function has a real docstring


def test_generate_fixture_repo_creates_the_requested_file_count(tmp_path):
    generate_fixture_repo(tmp_path, num_files=25, symbols_per_file=2)
    py_files = list(tmp_path.glob("*.py"))
    assert len(py_files) == 25


def test_generate_fixture_repo_is_deterministic_given_the_same_seed(tmp_path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    generate_fixture_repo(root_a, num_files=10, symbols_per_file=2, seed=99)
    generate_fixture_repo(root_b, num_files=10, symbols_per_file=2, seed=99)

    for i in range(10):
        content_a = (root_a / f"module_{i:06d}.py").read_text(encoding="utf-8")
        content_b = (root_b / f"module_{i:06d}.py").read_text(encoding="utf-8")
        assert content_a == content_b


def test_generate_fixture_repo_files_are_not_degenerately_identical(tmp_path):
    """Real lexical diversity is required for a representative benchmark
    -- files must not all contain byte-identical content."""
    generate_fixture_repo(tmp_path, num_files=20, symbols_per_file=2)
    contents = {(tmp_path / f"module_{i:06d}.py").read_text(encoding="utf-8") for i in range(20)}
    assert len(contents) > 1, "generated fixture files must have varied content"


def test_sample_queries_returns_the_requested_count_and_is_reproducible():
    q1 = sample_queries(30)
    q2 = sample_queries(30)
    assert len(q1) == 30
    assert q1 == q2  # same seed -> same queries
    assert all(isinstance(q, str) and q.strip() for q in q1)


# =============================================================================
# Percentile computation
# =============================================================================

def test_percentile_of_empty_list_is_zero():
    assert percentile([], 50) == 0.0


def test_percentile_of_single_value():
    assert percentile([42.0], 50) == 42.0
    assert percentile([42.0], 95) == 42.0


def test_percentile_p50_of_known_distribution():
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(values, 50) == 30.0  # exact median, odd count


def test_percentile_p95_is_close_to_the_maximum_for_a_small_sample():
    values = list(range(1, 101))  # 1..100
    p95 = percentile([float(v) for v in values], 95)
    assert 90.0 <= p95 <= 100.0


def test_percentile_is_monotonic_across_increasing_percentiles():
    values = [5.0, 1.0, 9.0, 3.0, 7.0]
    p50 = percentile(values, 50)
    p95 = percentile(values, 95)
    assert p95 >= p50


# =============================================================================
# Latency measurement
# =============================================================================

def test_measure_search_latency_produces_real_positive_timings():
    def fake_search(query: str):
        time.sleep(0.001)
        return [query]

    stats = measure_search_latency(fake_search, queries=["a", "b", "c"], repeats=2)

    assert isinstance(stats, LatencyStats)
    assert stats.num_samples == 6  # 3 queries * 2 repeats
    assert stats.p50_ms > 0.0
    assert stats.p95_ms >= stats.p50_ms
    assert stats.min_ms <= stats.p50_ms <= stats.max_ms
    assert len(stats.raw_ms) == 6


def test_measure_search_latency_actually_calls_search_fn_the_right_number_of_times():
    call_count = {"n": 0}

    def counting_search(query: str):
        call_count["n"] += 1

    measure_search_latency(counting_search, queries=["x", "y"], repeats=4)
    assert call_count["n"] == 8  # 2 queries * 4 repeats
