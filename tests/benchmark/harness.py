"""
Phase 2.5 — Benchmark harness across 3 repo-size tiers (roadmap §37 item
2.5: "Benchmark harness across 3 repo-size tiers; publish measured
numbers"; §36 Phase 2 DoD: "measured p50/p95 latency numbers exist and
are documented (replacing the old unverified target)"; §19 Performance
Strategy: defines the three tiers (~1K / ~10K / ~100K+ files) and their
budget table, explicitly "budgets to validate... not commitments"; §20
Testing Strategy: "Performance tests: run the §19 benchmark harness
against fixture repos of each size tier in CI on a schedule (not every
PR -- too slow)").

This module holds the harness's pure, reusable logic: fixture-repo
generation, timing helpers, and percentile computation. It has ZERO
dependency on pytest and is deliberately NOT named test_*.py so pytest's
own `testpaths = ["tests"]` collection does not sweep a multi-minute
benchmark run into the routine `pytest -q` invocation every other
milestone in this project has used as its fast, full-suite regression
check -- matching §20's own explicit "not every PR -- too slow" framing.
`tests/test_benchmark_harness.py` unit-tests this module's own pure
functions (fixture generation validity, percentile math) with fast,
tiny synthetic inputs; the actual expensive benchmark RUN across the
three real tiers is `tests/benchmark/run_benchmark.py`, a standalone
script invoked on demand, not part of the routine test suite.
"""
from __future__ import annotations

import math
import random
import string
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# A small, realistic vocabulary used to generate varied, meaningfully
# searchable (not degenerately identical) function names/bodies across a
# large number of fixture files -- real lexical diversity for BM25/vector
# search to operate on, not literally repeated text.
_VERBS = [
    "get", "set", "compute", "validate", "process", "fetch", "parse",
    "build", "load", "save", "update", "delete", "resolve", "normalize",
    "serialize", "deserialize", "encode", "decode", "filter", "sort",
]
_NOUNS = [
    "user", "data", "item", "record", "config", "session", "token",
    "file", "request", "result", "response", "payload", "message",
    "account", "profile", "document", "entry", "batch", "cache", "queue",
]


def _make_identifier(rng: random.Random, index: int) -> str:
    verb = rng.choice(_VERBS)
    noun = rng.choice(_NOUNS)
    return f"{verb}_{noun}_{index}"


def generate_fixture_file(index: int, symbols_per_file: int, rng: random.Random) -> str:
    """
    Generates one syntactically valid, parseable Python source file
    containing `symbols_per_file` small functions with varied, realistic
    (not degenerately repeated) names/docstrings/bodies.
    """
    lines: list[str] = []
    for j in range(symbols_per_file):
        name = _make_identifier(rng, index * symbols_per_file + j)
        verb, noun = name.split("_")[0], name.split("_")[1]
        lines.append(f"def {name}(value):")
        lines.append(f'    """{verb.capitalize()}s the given {noun} and returns it."""')
        lines.append(f"    result = value")
        lines.append(f"    return result")
        lines.append("")
        lines.append("")
    return "\n".join(lines)


def generate_fixture_repo(root: Path, num_files: int, symbols_per_file: int = 2, seed: int = 42) -> None:
    """
    Populates `root` (must already exist) with `num_files` real, valid
    Python source files, each containing `symbols_per_file` small
    functions -- deterministic given the same `seed`, so a benchmark run
    is reproducible.
    """
    rng = random.Random(seed)
    for i in range(num_files):
        content = generate_fixture_file(i, symbols_per_file, rng)
        (root / f"module_{i:06d}.py").write_text(content, encoding="utf-8")


def sample_queries(num_queries: int, seed: int = 7) -> list[str]:
    """
    Generates a fixed, reproducible set of natural-ish two-word search
    queries drawn from the same vocabulary `generate_fixture_repo` uses,
    so a meaningful (non-trivial, non-empty) fraction of them have real
    matches in the generated fixture corpus -- representative of a real
    keyword/semantic search workload, not an arbitrary/degenerate one.
    """
    rng = random.Random(seed)
    queries = []
    for _ in range(num_queries):
        verb = rng.choice(_VERBS)
        noun = rng.choice(_NOUNS)
        queries.append(f"{verb} {noun}")
    return queries


def percentile(values: list[float], pct: float) -> float:
    """
    Real, deterministic percentile computation (nearest-rank method,
    linear interpolation between ranks) over a real list of measured
    values -- no library dependency, no fabricated/estimated number.
    """
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (pct / 100.0) * (len(sorted_vals) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_vals[int(rank)]
    frac = rank - lower
    return sorted_vals[lower] + (sorted_vals[upper] - sorted_vals[lower]) * frac


@dataclass
class LatencyStats:
    p50_ms: float
    p95_ms: float
    min_ms: float
    max_ms: float
    num_samples: int
    raw_ms: list[float] = field(default_factory=list)


def measure_search_latency(
    search_fn: Callable[[str], Any],
    queries: list[str],
    repeats: int = 3,
) -> LatencyStats:
    """
    Calls `search_fn(query)` once per query per repeat, timing each real
    call with `time.perf_counter()` -- real wall-clock measurements, not
    estimated. `repeats` > 1 lets a small query set still produce a
    reasonably-sized, real latency distribution to compute p50/p95 from.
    """
    samples_ms: list[float] = []
    for _ in range(repeats):
        for q in queries:
            t0 = time.perf_counter()
            search_fn(q)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            samples_ms.append(elapsed_ms)

    return LatencyStats(
        p50_ms=percentile(samples_ms, 50),
        p95_ms=percentile(samples_ms, 95),
        min_ms=min(samples_ms) if samples_ms else 0.0,
        max_ms=max(samples_ms) if samples_ms else 0.0,
        num_samples=len(samples_ms),
        raw_ms=samples_ms,
    )


@dataclass
class TierBenchmarkResult:
    tier_name: str
    num_files: int
    num_symbols: int
    index_time_seconds: float
    search_type: str
    latency: LatencyStats
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier_name": self.tier_name,
            "num_files": self.num_files,
            "num_symbols": self.num_symbols,
            "index_time_seconds": round(self.index_time_seconds, 3),
            "search_type": self.search_type,
            "search_p50_ms": round(self.latency.p50_ms, 2),
            "search_p95_ms": round(self.latency.p95_ms, 2),
            "search_min_ms": round(self.latency.min_ms, 2),
            "search_max_ms": round(self.latency.max_ms, 2),
            "num_latency_samples": self.latency.num_samples,
            "notes": self.notes,
        }
