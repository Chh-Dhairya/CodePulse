"""
Phase 2.4 — Relevance evaluation harness (roadmap §36 Phase 2 tasks: "add
the relevance-evaluation harness and baseline dataset (§20)"; §37 item
2.4: "Curated relevance dataset (100-300 query/symbol pairs) + eval
harness"; §20 Testing Strategy: "curate ~100-300 (query, expected-symbol)
pairs from CodePulse's own repository as a starting relevance dataset;
track Recall@10, MRR as a regression gate on ranking changes"; §45
backlog B14 DoD: "Recall@10/MRR tracked and gated in CI").

This module implements the harness itself -- loading the curated dataset
(tests/relevance/dataset.json) and computing real Recall@10/MRR against
whatever search function is handed to it. It is deliberately decoupled
from `IndexingEngine`: `evaluate()` takes a plain callable
(`str, int -> list[SearchResultItem]`), so the metric-computation logic
here is independently unit-testable with small synthetic inputs, and the
harness itself never imports `codepulse.indexer.engine` (avoiding a
circular/heavy dependency for something that is, itself, a test-only
tool -- confirmed against §22 CLI Strategy's own explicit command list,
which does not name any `codepulse eval`/relevance command; this harness
has no CLI/API/MCP exposure requirement).

"Gated in CI" (§36/§45's phase-level DoD wording): no CI system
(GitHub Actions or otherwise) exists anywhere in this repository as of
this milestone -- confirmed, no `.github/workflows` directory exists.
Introducing CI infrastructure from scratch is judged out of this
milestone's literal scope (§37 item 2.4's own text: "dataset + eval
harness," not "CI pipeline") and inconsistent with this project's
established "don't introduce infrastructure without explicit roadmap
justification" discipline. Instead, `tests/test_relevance_eval.py`
expresses the regression gate as a real pytest assertion against a
measured baseline -- this project's own existing, only gating mechanism
-- so the moment any CI system is wired up to run `pytest` (a separate,
undertaken-when-needed operational step), this gate is automatically
"in CI" for free, with zero further code change required here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

DEFAULT_DATASET_PATH = Path(__file__).parent / "dataset.json"


@dataclass(frozen=True)
class RelevancePair:
    """One (query, expected-symbol) ground-truth pair from the curated dataset."""

    query: str
    expected_file_path: str
    expected_symbol_name: str
    source: str = "unknown"


class _HasFilePathAndSymbolName(Protocol):
    file_path: str
    symbol_name: str


def load_relevance_dataset(
    path: str | Path | None = None, size_band: tuple[int, int] = (100, 300)
) -> list[RelevancePair]:
    """
    Loads the curated relevance dataset (default:
    `tests/relevance/dataset.json`, this milestone's own curated,
    100-300-pair dataset drawn from CodePulse's own repository).

    Raises `ValueError` if the loaded dataset falls outside the roadmap's
    explicit ~100-300 pair size band (§20) -- a real, structural
    validation of the dataset's own health, not a fabricated pass.
    `size_band` is that band; the smaller per-codebase external datasets
    (tests/relevance/external_*.json, added for FUTURE_CHANGES item 20)
    pass their own, narrower expectation.
    """
    dataset_path = Path(path) if path is not None else DEFAULT_DATASET_PATH
    with open(dataset_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    pairs_raw = raw["pairs"] if isinstance(raw, dict) else raw
    pairs = [
        RelevancePair(
            query=p["query"],
            expected_file_path=p["expected_file_path"],
            expected_symbol_name=p["expected_symbol_name"],
            source=p.get("source", "unknown"),
        )
        for p in pairs_raw
    ]

    low, high = size_band
    if not (low <= len(pairs) <= high):
        raise ValueError(
            f"Relevance dataset at {dataset_path} has {len(pairs)} pairs, outside "
            f"the expected {low}-{high} pair band (the roadmap's band for the main dataset is "
            f"~100-300, §20) -- the dataset file "
            f"itself is malformed/truncated, not merely a low score."
        )
    return pairs


def _matches(result: _HasFilePathAndSymbolName, pair: RelevancePair) -> bool:
    return result.file_path == pair.expected_file_path and result.symbol_name == pair.expected_symbol_name


def reciprocal_rank(results: list[_HasFilePathAndSymbolName], pair: RelevancePair) -> float:
    """
    Returns `1 / rank` (1-indexed) of the first result matching `pair`'s
    expected (file_path, symbol_name), or `0.0` if the expected symbol
    does not appear anywhere in `results` at all.
    """
    for idx, r in enumerate(results, start=1):
        if _matches(r, pair):
            return 1.0 / idx
    return 0.0


def recall_at_k(results: list[_HasFilePathAndSymbolName], pair: RelevancePair, k: int) -> bool:
    """Whether the expected symbol appears anywhere in the top `k` results."""
    return any(_matches(r, pair) for r in results[:k])


@dataclass
class PerQueryResult:
    pair: RelevancePair
    found_at_rank: int | None  # 1-indexed; None if not found at all
    reciprocal_rank: float
    recall_at_10: bool


@dataclass
class EvaluationResult:
    """
    Aggregate Recall@10 and MRR over an entire dataset run -- both are
    real, computed values (CLAUDE.md: no fabricated metrics), never
    estimated or hardcoded.
    """

    num_queries: int
    recall_at_10: float
    mrr: float
    per_query: list[PerQueryResult] = field(default_factory=list)

    def misses(self) -> list[PerQueryResult]:
        """Queries where the expected symbol was not found in the top 10 --
        useful for diagnosing exactly which relevance cases are failing,
        not just the aggregate score."""
        return [pq for pq in self.per_query if not pq.recall_at_10]


def evaluate(
    search_fn: Callable[[str, int], list[Any]],
    pairs: list[RelevancePair] | None = None,
    k: int = 10,
    search_limit: int = 20,
) -> EvaluationResult:
    """
    Runs every pair's query through `search_fn(query, limit=search_limit)`
    and computes real Recall@`k` and MRR against the returned results.

    `search_fn` is a plain callable -- deliberately not `IndexingEngine`
    itself -- so this function has zero dependency on how search is
    actually implemented; a caller typically passes something like
    `functools.partial(engine.search, search_type="hybrid",
    repository_id=repo_id)`.

    `search_limit` (default 20) must be `>= k` -- MRR needs to look
    beyond the top-`k` cutoff to find a lower-ranked (but still present)
    match, while Recall@`k` itself only ever looks at the first `k` of
    whatever `search_fn` returns.
    """
    if pairs is None:
        pairs = load_relevance_dataset()
    if search_limit < k:
        raise ValueError(f"search_limit ({search_limit}) must be >= k ({k})")

    per_query: list[PerQueryResult] = []
    for pair in pairs:
        results = search_fn(pair.query, search_limit)
        rr = reciprocal_rank(results, pair)
        found_at_rank = int(round(1.0 / rr)) if rr > 0 else None
        per_query.append(
            PerQueryResult(
                pair=pair,
                found_at_rank=found_at_rank,
                reciprocal_rank=rr,
                recall_at_10=recall_at_k(results, pair, k),
            )
        )

    num_queries = len(per_query)
    recall = sum(1 for pq in per_query if pq.recall_at_10) / num_queries if num_queries else 0.0
    mrr = sum(pq.reciprocal_rank for pq in per_query) / num_queries if num_queries else 0.0

    return EvaluationResult(num_queries=num_queries, recall_at_10=recall, mrr=mrr, per_query=per_query)
