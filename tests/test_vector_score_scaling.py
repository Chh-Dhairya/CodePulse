"""
The semantic score is scaled RELATIVE to the query's own results before fusion
(FUTURE_CHANGES item 20, part 2, 2026-10-18).

The defect: the lexical score handed to the fuser is relative (the best keyword
hit is always 1.0), but the semantic score was `(cosine + 1) / 2`, an absolute
number that lives in roughly 0.5..0.9 for real code. The two were weighted
0.4 / 0.4 as if they were on one scale; they were not, so the semantic signal
was compressed into a narrow band and a weak keyword match ranked as if it were
strong. Measured on 84 paraphrase queries over two codebases, hybrid reached
Recall@10 0.667 while semantic alone reached 0.929.

The rule now: a vector hit's fusion score is `(sim - floor) / (top - floor)`,
clipped to 0..1, where `top` is the best similarity for the query and `floor`
the similarity of the hit at rank `vector_floor_rank` (default 20). It is
independent of how many MORE hits were fetched beyond that rank, so a result's
score never depends on the `limit` the caller asked for. The reported
`vector_score` stays the real value; only the fusion input is scaled. A
`vector_floor_rank` of 0 restores the old absolute scaling.
"""
from __future__ import annotations

import pytest

from codepulse.config import reset_settings
from codepulse.search.ranker import HybridRanker


@pytest.fixture(autouse=True)
def cleanup_settings():
    reset_settings()
    yield
    reset_settings()


def _hit(i: int, similarity: float) -> dict:
    return {
        "doc_id": f"1::f.py::sym{i}",
        "similarity": similarity,
        "relevance_score": (similarity + 1.0) / 2.0,
        "embedding_distance": 1.0 - similarity,
        "metadata": {"symbol_name": f"sym{i}", "file_path": "f.py", "type": "function", "repo_id": 1},
    }


def _vector_only(floor_rank: int) -> HybridRanker:
    return HybridRanker(weight_keyword=0.0, weight_vector=1.0, weight_graph=0.0, exact_match_boost=0.0, vector_floor_rank=floor_rank)


HITS = [_hit(i, s) for i, s in enumerate([0.80, 0.70, 0.60, 0.50, 0.40])]


def test_the_best_hit_scores_one_and_the_floor_hit_scores_zero():
    results = _vector_only(floor_rank=5).fuse("anything", [], HITS, top_k=5)
    scores = {r.symbol_name: r.final_score for r in results}
    assert scores["sym0"] == pytest.approx(1.0)
    assert scores["sym4"] == pytest.approx(0.0)
    assert scores["sym2"] == pytest.approx(0.5)  # halfway between top 0.8 and floor 0.4 is 0.6


def test_hits_beyond_the_floor_rank_clip_to_zero():
    results = _vector_only(floor_rank=3).fuse("anything", [], HITS, top_k=5)
    scores = {r.symbol_name: r.final_score for r in results}
    assert scores["sym2"] == pytest.approx(0.0)  # the floor hit
    assert scores["sym3"] == 0.0 and scores["sym4"] == 0.0
    assert scores["sym1"] == pytest.approx(0.5)  # (0.70 - 0.60) / (0.80 - 0.60)


def test_a_score_does_not_depend_on_how_many_extra_hits_were_fetched():
    """Determinism across `limit`: hits ranked below the floor must not move anyone's score."""
    few = _vector_only(floor_rank=3).fuse("q", [], HITS[:3], top_k=10)
    many = _vector_only(floor_rank=3).fuse("q", [], HITS + [_hit(9, 0.1), _hit(10, -0.2)], top_k=10)
    few_scores = {r.symbol_name: round(r.final_score, 9) for r in few}
    many_scores = {r.symbol_name: round(r.final_score, 9) for r in many if r.symbol_name in few_scores}
    assert few_scores == many_scores


def test_the_input_order_does_not_matter():
    forward = _vector_only(floor_rank=5).fuse("q", [], HITS, top_k=5)
    backward = _vector_only(floor_rank=5).fuse("q", [], list(reversed(HITS)), top_k=5)
    assert [(r.doc_id, round(r.final_score, 9)) for r in forward] == [(r.doc_id, round(r.final_score, 9)) for r in backward]


def test_a_single_hit_is_not_zeroed():
    """With one candidate there is no spread; it must not lose all its semantic credit."""
    (only,) = _vector_only(floor_rank=20).fuse("q", [], [HITS[0]], top_k=5)
    assert only.final_score == pytest.approx(1.0)


def test_equal_hits_all_score_one():
    tied = [_hit(i, 0.5) for i in range(3)]
    assert all(r.final_score == pytest.approx(1.0) for r in _vector_only(floor_rank=20).fuse("q", [], tied, top_k=5))


def test_a_non_positive_top_similarity_scores_zero():
    unrelated = [_hit(i, s) for i, s in enumerate([-0.1, -0.3])]
    assert all(r.final_score == 0.0 for r in _vector_only(floor_rank=20).fuse("q", [], unrelated, top_k=5))


def test_the_reported_vector_score_stays_the_real_value():
    results = _vector_only(floor_rank=5).fuse("q", [], HITS, top_k=5)
    by_name = {r.symbol_name: r for r in results}
    assert by_name["sym1"].vector_score == pytest.approx((0.70 + 1.0) / 2.0)
    assert by_name["sym1"].embedding_distance == pytest.approx(0.30)


def test_floor_rank_zero_restores_the_old_absolute_scaling():
    results = _vector_only(floor_rank=0).fuse("q", [], HITS, top_k=5)
    for r in results:
        assert r.final_score == pytest.approx(r.vector_score)


def test_the_two_sources_are_now_on_comparable_scales():
    """A strong keyword hit and a strong semantic hit on different symbols are no longer separated by the scales."""
    bm25 = [{"doc_id": "1::f.py::kw", "normalized_score": 1.0, "metadata": {"symbol_name": "kw", "file_path": "f.py", "repo_id": 1}}]
    vector = [_hit(0, 0.80), _hit(1, 0.70), _hit(2, 0.60)]
    ranker = HybridRanker(weight_keyword=0.4, weight_vector=0.4, weight_graph=0.0, exact_match_boost=0.0, vector_floor_rank=3)
    ranked = {r.symbol_name: r.final_score for r in ranker.fuse("zzz", bm25, vector, top_k=5)}
    # the best semantic hit now earns the same 0.4 the best keyword hit does
    assert ranked["sym0"] == pytest.approx(ranked["kw"])


def test_the_setting_defaults_to_20_and_is_configurable(monkeypatch):
    from codepulse.config import get_settings

    assert get_settings().vector_floor_rank == 20
    reset_settings()
    monkeypatch.setenv("CODEPULSE_VECTOR_FLOOR_RANK", "50")
    assert get_settings().vector_floor_rank == 50
    assert HybridRanker().vector_floor_rank == 50


def test_the_setting_rejects_a_negative_value(monkeypatch):
    from codepulse.config import get_settings

    monkeypatch.setenv("CODEPULSE_VECTOR_FLOOR_RANK", "-1")
    reset_settings()
    with pytest.raises(Exception):
        get_settings()


def test_semantic_only_ranks_purely_by_similarity_with_no_name_boost():
    """Semantic-only search fuses nothing, so a name-match boost has no business in it. With the old scoring
    (0.4 x relevance + 0.15 boost) a symbol whose name merely appeared in the query outranked a much closer match."""
    close = _hit(0, 0.80)
    close["metadata"]["symbol_name"] = "unrelated_name"
    named = _hit(1, 0.55)
    named["metadata"]["symbol_name"] = "name"  # appears inside the query text below
    results = HybridRanker().rank_by_similarity([named, close], top_k=5)
    assert [r.symbol_name for r in results] == ["unrelated_name", "name"]


def test_semantic_only_reports_the_real_relevance_as_its_score():
    results = HybridRanker().rank_by_similarity(HITS, top_k=5)
    for r in results:
        assert r.final_score == pytest.approx(r.vector_score)
        assert r.keyword_score == 0.0 and r.graph_score == 0.0
    assert results[0].final_score == pytest.approx((0.80 + 1.0) / 2.0)


def test_semantic_only_is_deterministic_for_ties_and_respects_top_k():
    tied = [_hit(i, 0.5) for i in (3, 1, 2)]
    ids = [r.doc_id for r in HybridRanker().rank_by_similarity(tied, top_k=2)]
    assert ids == sorted(t["doc_id"] for t in tied)[:2]


def test_the_engine_uses_scaled_fusion_for_hybrid_and_pure_similarity_for_semantic(monkeypatch):
    """Wiring check at the engine boundary, without indexing anything."""
    import numpy as np

    from codepulse.indexer.engine import IndexingEngine
    from codepulse.storage.db import StorageManager

    engine = IndexingEngine(storage=StorageManager(db_url="sqlite://"))
    calls = []
    monkeypatch.setattr(engine.ranker, "fuse", lambda *a, **k: calls.append("fuse") or [])
    monkeypatch.setattr(engine.ranker, "rank_by_similarity", lambda *a, **k: calls.append("similarity") or [])
    monkeypatch.setattr(engine.embedder, "encode", lambda q: np.zeros(384, dtype=np.float32))
    engine.search("q", search_type="hybrid")
    engine.search("q", search_type="semantic")
    engine.search("q", search_type="keyword")
    assert calls == ["fuse", "similarity", "fuse"]
