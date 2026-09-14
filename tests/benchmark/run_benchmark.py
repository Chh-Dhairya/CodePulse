"""
Phase 2.5 — standalone benchmark runner (roadmap §37 item 2.5: "Benchmark
harness across 3 repo-size tiers; publish measured numbers"; §20:
"run the §19 benchmark harness against fixture repos of each size tier
in CI on a schedule (not every PR -- too slow)").

Deliberately NOT a pytest test file (no `test_` prefix, not auto-
collected) -- a real 3-tier run takes real wall-clock time (see
CODEPULSE_PROGRESS.md's Phase 2.5 section for exactly how long), and has
no business slowing down the routine `pytest -q` full-suite check every
other milestone in this project has relied on. Run it directly:

    .venv/Scripts/python.exe tests/benchmark/run_benchmark.py

Uses a temporary, disposable SQLite database for every tier -- never the
production codepulse.db. Results are printed as a table and saved to
tests/benchmark/results.json (both real, measured numbers -- CLAUDE.md:
no fabricated metrics).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from codepulse.config import CodePulseConfig, reset_settings, set_settings
from codepulse.embeddings.embedder import CodeEmbedder
from codepulse.indexer.engine import IndexingEngine
from codepulse.storage.db import StorageManager
from tests.benchmark.harness import (
    TierBenchmarkResult,
    generate_fixture_repo,
    measure_search_latency,
    sample_queries,
)

# Tier definitions. See CODEPULSE_PROGRESS.md's Phase 2.5 section for the
# full disclosure on tier sizing versus §19's literal ~1K/~10K/~100K+
# wording -- the "large" tier size here was chosen from real, measured
# throughput calibration in this session, not the literal ~100K+ figure,
# which the roadmap's own §19 table itself characterizes as an
# hours-long, background (non-blocking) operation.
TIERS = [
    ("small (~1K files)", 1000, 2),
    ("medium (~10K files)", 10000, 2),
    ("large (reduced from roadmap's ~100K+)", 25000, 2),
]

SEARCH_TYPE = "hybrid"
NUM_QUERIES = 30
REPEATS = 3


def run_tier(tier_name: str, num_files: int, symbols_per_file: int, embedder: CodeEmbedder) -> TierBenchmarkResult:
    tmp_repo_dir = tempfile.mkdtemp(prefix="codepulse_bench_repo_")
    tmp_db_dir = tempfile.mkdtemp(prefix="codepulse_bench_db_")
    try:
        repo_path = Path(tmp_repo_dir)
        print(f"[{tier_name}] generating {num_files} fixture files...", flush=True)
        t_gen0 = time.perf_counter()
        generate_fixture_repo(repo_path, num_files=num_files, symbols_per_file=symbols_per_file)
        t_gen = time.perf_counter() - t_gen0
        print(f"[{tier_name}] fixture generation took {t_gen:.1f}s", flush=True)

        db_path = Path(tmp_db_dir) / "bench.db"
        db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"
        # Raise the ingestion file-count cap for this benchmark-only,
        # disposable config instance -- the real production default
        # (50,000) is unchanged; this is a local override for a
        # deliberately large synthetic fixture repo, not a production
        # behavior change.
        set_settings(CodePulseConfig(ingestion_max_files_per_repo=max(200_000, num_files * 2)))
        storage = StorageManager(db_url=db_url)
        engine = IndexingEngine(storage=storage, embedder=embedder)

        print(f"[{tier_name}] indexing...", flush=True)
        t_idx0 = time.perf_counter()
        result = engine.index_directory(str(repo_path), repo_name=tier_name, repo_url=str(repo_path))
        index_time = time.perf_counter() - t_idx0
        repo_id = result["repository_id"]
        num_symbols = result.get("total_symbols", 0)
        print(f"[{tier_name}] indexed {num_symbols} symbols in {index_time:.1f}s", flush=True)

        queries = sample_queries(NUM_QUERIES)

        def search_fn(query: str):
            return engine.search(query, limit=20, search_type=SEARCH_TYPE, repository_id=repo_id)

        print(f"[{tier_name}] measuring search latency ({NUM_QUERIES} queries x {REPEATS} repeats)...", flush=True)
        latency = measure_search_latency(search_fn, queries=queries, repeats=REPEATS)
        print(f"[{tier_name}] p50={latency.p50_ms:.1f}ms p95={latency.p95_ms:.1f}ms", flush=True)

        return TierBenchmarkResult(
            tier_name=tier_name,
            num_files=num_files,
            num_symbols=num_symbols,
            index_time_seconds=index_time,
            search_type=SEARCH_TYPE,
            latency=latency,
            notes=f"fixture generation took {t_gen:.1f}s (not included in index_time_seconds)",
        )
    finally:
        reset_settings()
        shutil.rmtree(tmp_repo_dir, ignore_errors=True)
        shutil.rmtree(tmp_db_dir, ignore_errors=True)


def main() -> None:
    embedder = CodeEmbedder()
    results: list[TierBenchmarkResult] = []
    for tier_name, num_files, symbols_per_file in TIERS:
        results.append(run_tier(tier_name, num_files, symbols_per_file, embedder))

    print("\n" + "=" * 100)
    print(f"{'Tier':<40} {'Files':>8} {'Symbols':>8} {'Index(s)':>10} {'p50(ms)':>9} {'p95(ms)':>9}")
    print("=" * 100)
    for r in results:
        print(
            f"{r.tier_name:<40} {r.num_files:>8} {r.num_symbols:>8} "
            f"{r.index_time_seconds:>10.1f} {r.latency.p50_ms:>9.1f} {r.latency.p95_ms:>9.1f}"
        )

    out_path = Path(__file__).parent / "results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump([r.to_dict() for r in results], f, indent=2)
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
