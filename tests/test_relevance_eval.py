"""
Phase 2.4 — Relevance evaluation dataset + harness (roadmap §36/§37 item
2.4; §20 Testing Strategy; §45 backlog B14).

Two groups of tests:

  1. UNIT tests against `tests/relevance/eval_harness.py` directly, using
     small synthetic result stubs (fast, no real indexing/embedding) --
     proving `reciprocal_rank`/`recall_at_k`/`evaluate`'s own arithmetic
     is correct, independent of any real search engine.
  2. The dataset's own structural health (loads, real size band, every
     pair references content, no duplicate/empty entries).
  3. The INTEGRATION regression gate itself: indexes CodePulse's own
     `codepulse/` package (this repository's real source, per §20's
     explicit "from CodePulse's own repository" wording) into a real,
     disposable `IndexingEngine`, runs the full curated dataset through
     real hybrid search, and asserts Recall@10/MRR against a measured
     baseline -- the actual "regression gate on ranking changes" §20
     describes. See eval_harness.py's own module docstring for why this
     is expressed as a pytest assertion rather than a literal CI-config
     file (no CI system exists anywhere in this repository yet).

All tests use a temporary, disposable SQLite database -- never the
production codepulse.db.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from codepulse.config import reset_settings
from codepulse.embeddings.embedder import CodeEmbedder
from codepulse.indexer.engine import IndexingEngine
from codepulse.storage.db import StorageManager
from tests.relevance.eval_harness import (
    DEFAULT_DATASET_PATH,
    EvaluationResult,
    PerQueryResult,
    RelevancePair,
    evaluate,
    load_relevance_dataset,
    reciprocal_rank,
    recall_at_k,
)


@pytest.fixture(autouse=True)
def cleanup_settings():
    reset_settings()
    yield
    reset_settings()


class _StubResult:
    def __init__(self, file_path: str, symbol_name: str):
        self.file_path = file_path
        self.symbol_name = symbol_name


# =============================================================================
# UNIT TESTS -- metric computation (synthetic data, no real search engine)
# =============================================================================

def test_reciprocal_rank_found_at_first_position():
    pair = RelevancePair(query="q", expected_file_path="a.py", expected_symbol_name="foo")
    results = [_StubResult("a.py", "foo"), _StubResult("b.py", "bar")]
    assert reciprocal_rank(results, pair) == 1.0


def test_reciprocal_rank_found_at_third_position():
    pair = RelevancePair(query="q", expected_file_path="a.py", expected_symbol_name="foo")
    results = [_StubResult("x.py", "x"), _StubResult("y.py", "y"), _StubResult("a.py", "foo")]
    assert reciprocal_rank(results, pair) == pytest.approx(1.0 / 3)


def test_reciprocal_rank_not_found_is_zero():
    pair = RelevancePair(query="q", expected_file_path="a.py", expected_symbol_name="foo")
    results = [_StubResult("x.py", "x"), _StubResult("y.py", "y")]
    assert reciprocal_rank(results, pair) == 0.0


def test_reciprocal_rank_same_symbol_name_different_file_does_not_match():
    """A symbol_name match alone must not count -- file_path must match too,
    exactly the disambiguation the real dataset relies on (e.g. `search`
    exists as a method in three different files in this codebase)."""
    pair = RelevancePair(query="q", expected_file_path="a.py", expected_symbol_name="search")
    results = [_StubResult("b.py", "search")]
    assert reciprocal_rank(results, pair) == 0.0


def test_recall_at_k_true_within_cutoff():
    pair = RelevancePair(query="q", expected_file_path="a.py", expected_symbol_name="foo")
    results = [_StubResult("x.py", "x"), _StubResult("a.py", "foo")]
    assert recall_at_k(results, pair, k=10) is True
    assert recall_at_k(results, pair, k=1) is False


def test_evaluate_computes_correct_aggregate_recall_and_mrr():
    pairs = [
        RelevancePair(query="q1", expected_file_path="a.py", expected_symbol_name="foo"),
        RelevancePair(query="q2", expected_file_path="b.py", expected_symbol_name="bar"),
        RelevancePair(query="q3", expected_file_path="c.py", expected_symbol_name="baz"),
    ]

    def fake_search(query: str, limit: int):
        if query == "q1":
            return [_StubResult("a.py", "foo")]  # rank 1
        if query == "q2":
            return [_StubResult("x.py", "x"), _StubResult("b.py", "bar")]  # rank 2
        return [_StubResult("x.py", "x")]  # not found

    result = evaluate(fake_search, pairs=pairs, k=10, search_limit=20)

    assert isinstance(result, EvaluationResult)
    assert result.num_queries == 3
    # Recall@10: 2 of 3 queries found within top 10 (q1, q2; q3 missed).
    assert result.recall_at_10 == pytest.approx(2 / 3)
    # MRR: (1/1 + 1/2 + 0) / 3
    assert result.mrr == pytest.approx((1.0 + 0.5 + 0.0) / 3)
    assert len(result.per_query) == 3
    assert all(isinstance(pq, PerQueryResult) for pq in result.per_query)


def test_evaluate_misses_returns_only_the_unfound_queries():
    pairs = [
        RelevancePair(query="q1", expected_file_path="a.py", expected_symbol_name="foo"),
        RelevancePair(query="q2", expected_file_path="b.py", expected_symbol_name="bar"),
    ]

    def fake_search(query: str, limit: int):
        if query == "q1":
            return [_StubResult("a.py", "foo")]
        return []

    result = evaluate(fake_search, pairs=pairs)
    misses = result.misses()
    assert len(misses) == 1
    assert misses[0].pair.query == "q2"


def test_evaluate_search_limit_must_be_at_least_k():
    with pytest.raises(ValueError):
        evaluate(lambda q, limit: [], pairs=[], k=10, search_limit=5)


# =============================================================================
# DATASET STRUCTURAL HEALTH
# =============================================================================

def test_dataset_file_exists_at_the_expected_location():
    assert DEFAULT_DATASET_PATH.exists(), f"expected dataset at {DEFAULT_DATASET_PATH}"


def test_dataset_loads_and_is_within_the_roadmap_size_band():
    """§20: '~100-300 (query, expected-symbol) pairs'."""
    pairs = load_relevance_dataset()
    assert 100 <= len(pairs) <= 300


def test_dataset_every_pair_has_real_content():
    pairs = load_relevance_dataset()
    for p in pairs:
        assert p.query.strip(), f"empty query for {p.expected_file_path}::{p.expected_symbol_name}"
        assert p.expected_file_path.strip()
        assert p.expected_symbol_name.strip()
        # This dataset's own convention (tests/relevance/dataset.json's
        # documented expected_file_path_convention): relative to the
        # codepulse/ package directory, never repository-root-relative.
        assert not p.expected_file_path.startswith("codepulse/")


def test_dataset_has_no_duplicate_queries():
    pairs = load_relevance_dataset()
    queries = [p.query for p in pairs]
    assert len(queries) == len(set(queries)), "duplicate query text found in the curated dataset"


def test_dataset_every_expected_symbol_actually_exists_in_the_current_codebase():
    """
    Proves every (expected_file_path, expected_symbol_name) pair in the
    curated dataset is real -- not fabricated, not stale against the
    current source tree -- by actually parsing codepulse/ with this
    project's own parser and checking each target is present. If this
    ever fails, the dataset (not the search engine) needs curating.
    """
    from codepulse.parser.factory import get_parser_for_file

    codepulse_root = Path(__file__).parent.parent / "codepulse"
    real_symbols: set[tuple[str, str]] = set()
    for dirpath, dirnames, filenames in os.walk(codepulse_root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            full_path = os.path.join(dirpath, fn)
            rel_path = os.path.relpath(full_path, codepulse_root).replace(os.sep, "/")
            parser = get_parser_for_file(full_path)
            if not parser:
                continue
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
            result = parser.parse_file(rel_path, source)
            for sym in result.symbols:
                real_symbols.add((rel_path, sym.name))

    pairs = load_relevance_dataset()
    missing = [
        (p.expected_file_path, p.expected_symbol_name)
        for p in pairs
        if (p.expected_file_path, p.expected_symbol_name) not in real_symbols
    ]
    assert missing == [], f"dataset references symbols that no longer exist: {missing}"


# =============================================================================
# INTEGRATION -- the actual regression gate, against CodePulse's own repo
# =============================================================================

@pytest.fixture(scope="module")
def shared_embedder():
    return CodeEmbedder()


@pytest.fixture(scope="module")
def indexed_codepulse_engine(tmp_path_factory, shared_embedder):
    """
    Indexes CodePulse's own codepulse/ package -- this repository's real
    application source, exactly the corpus §20's "from CodePulse's own
    repository" wording describes -- into one disposable IndexingEngine,
    reused (module-scoped) across every query in the dataset so the
    (real, non-trivial) indexing + embedding cost is paid exactly once
    per test session, not once per one of the 141 dataset queries.
    """
    codepulse_root = Path(__file__).parent.parent / "codepulse"
    db_path = tmp_path_factory.mktemp("relevance_eval") / "eval.db"
    db_url = f"sqlite:///{str(db_path).replace(os.sep, '/')}"
    storage = StorageManager(db_url=db_url)
    engine = IndexingEngine(storage=storage, embedder=shared_embedder)
    result = engine.index_directory(str(codepulse_root), repo_name="codepulse_eval", repo_url=str(codepulse_root))
    return engine, result["repository_id"]


def test_hybrid_search_relevance_meets_the_measured_baseline(indexed_codepulse_engine):
    """
    THE regression gate: real, measured Recall@10/MRR for hybrid search
    against CodePulse's own indexed source, checked against a baseline
    established by actually running this evaluation (not guessed) --
    §36's DoD: "Recall@10/MRR baseline established... for future ranking
    changes"; §45/B14's DoD: "Recall@10/MRR tracked and gated."

    Measured baseline (this milestone, real run against the codebase as
    it existed at Phase 2.4's own commit -- see CODEPULSE_PROGRESS.md's
    Phase 2.4 section for the full per-search_type breakdown):
    hybrid Recall@10 = 0.9007 (127/141), MRR = 0.7129. Thresholds below
    are set with a real safety margin under those measured numbers --
    not at 1.0 (some dataset queries are deliberately harder: short
    docstring fragments using common English words are not always
    uniquely identifying against a ~195-symbol corpus) and not exactly
    at the measured value (so a run-to-run rounding difference alone,
    should one ever appear, doesn't trip the gate) -- so this test fails
    loudly, with per-query miss detail, only on a genuine relevance
    regression.
    """
    engine, repo_id = indexed_codepulse_engine

    def search_fn(query: str, limit: int):
        return engine.search(query, limit=limit, search_type="hybrid", repository_id=repo_id)

    result = evaluate(search_fn, k=10, search_limit=20)

    misses = result.misses()
    miss_summary = "\n".join(
        f"  - {pq.pair.query!r} -> expected {pq.pair.expected_file_path}::{pq.pair.expected_symbol_name}"
        for pq in misses[:15]
    )

    # Ratcheted on 2026-10-18 (FUTURE_CHANGES item 20): measured hybrid is now
    # Recall@10 0.957 / MRR 0.874 (it was 0.872 / 0.683 before results were folded
    # per symbol and the semantic score was put on the lexical score's scale).
    assert result.recall_at_10 >= 0.92, (
        f"Recall@10 regressed to {result.recall_at_10:.3f} (measured 2026-10-18: 0.957; Phase 2.4 baseline: 0.9007). "
        f"{len(misses)}/{result.num_queries} queries missed:\n{miss_summary}"
    )
    assert result.mrr >= 0.80, f"MRR regressed to {result.mrr:.3f} (measured 2026-10-18: 0.874; Phase 2.4 baseline: 0.7129)"


def test_hybrid_search_meets_the_floor_on_the_hand_written_paraphrases_alone(indexed_codepulse_engine):
    """100 of the 141 queries are the symbol's own docstring, which keyword search answers perfectly, so the
    headline gate can stay green while search on realistic queries gets worse (it did: hybrid trailed semantic
    search alone on these 41). Gate the paraphrase subset by itself.
    Measured 2026-10-18: Recall@10 0.854 / MRR 0.644 (before the fix: 0.659 / 0.477)."""
    engine, repo_id = indexed_codepulse_engine
    paraphrases = [p for p in load_relevance_dataset() if p.source == "hand_curated"]
    assert len(paraphrases) == 41

    result = evaluate(
        lambda q, limit: engine.search(q, limit=limit, search_type="hybrid", repository_id=repo_id),
        pairs=paraphrases,
        k=10,
        search_limit=20,
    )
    assert result.recall_at_10 >= 0.80, f"paraphrase Recall@10 {result.recall_at_10:.3f} < 0.80"
    assert result.mrr >= 0.58, f"paraphrase MRR {result.mrr:.3f} < 0.58"
