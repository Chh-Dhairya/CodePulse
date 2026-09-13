from codepulse.embeddings.chunker import SemanticChunker
from codepulse.models import Symbol, SymbolType
from codepulse.search.ranker import HybridRanker, SearchResultItem


def test_semantic_chunker():
    chunker = SemanticChunker(max_tokens=50)

    # Small function
    sym_small = Symbol(
        name="add",
        type=SymbolType.FUNCTION,
        file_path="math_utils.py",
        start_line=1,
        end_line=5,
        signature="def add(a: int, b: int) -> int:",
        docstring="Adds two numbers.",
        body="return a + b",
        language="python",
    )

    chunks = chunker.chunk_symbol(sym_small)
    # Should have signature_doc and full chunks
    assert len(chunks) >= 1
    assert chunks[0].chunk_type == "signature_doc"
    assert "add" in chunks[0].symbol_name


def test_hybrid_ranker_fusion():
    ranker = HybridRanker(weight_keyword=0.4, weight_vector=0.4, weight_graph=0.2, exact_match_boost=0.15)

    bm25_res = [
        {"doc_id": "doc1", "normalized_score": 0.9, "metadata": {"symbol_name": "create_db", "file_path": "db.py"}},
        {"doc_id": "doc2", "normalized_score": 0.4, "metadata": {"symbol_name": "login", "file_path": "auth.py"}},
    ]

    vector_res = [
        {"doc_id": "doc1", "relevance_score": 0.85, "embedding_distance": 0.15, "metadata": {"symbol_name": "create_db", "file_path": "db.py"}},
        {"doc_id": "doc2", "relevance_score": 0.2, "embedding_distance": 0.8, "metadata": {"symbol_name": "login", "file_path": "auth.py"}},
    ]

    graph_scores = {"doc1": 0.8, "doc2": 0.1}

    results = ranker.fuse("database create", bm25_results=bm25_res, vector_results=vector_res, graph_scores=graph_scores)

    assert len(results) == 2
    assert results[0].doc_id == "doc1"
    # doc1 score: 0.4*0.9 + 0.4*0.85 + 0.2*0.8 = 0.36 + 0.34 + 0.16 = 0.86 (+ exact match boost)
    assert results[0].final_score > 0.8
    assert results[0].keyword_score == 0.9
    assert results[0].vector_score == 0.85


# --- Phase 2.3 -- Reciprocal Rank Fusion (roadmap §36/§37 item 2.3) ---------

def _rrf_fixture():
    bm25_res = [
        {"doc_id": "doc1", "normalized_score": 0.9, "metadata": {"symbol_name": "create_db", "file_path": "db.py"}},
        {"doc_id": "doc2", "normalized_score": 0.4, "metadata": {"symbol_name": "login", "file_path": "auth.py"}},
        {"doc_id": "doc3", "normalized_score": 0.1, "metadata": {"symbol_name": "logout", "file_path": "auth.py"}},
    ]
    vector_res = [
        {"doc_id": "doc2", "relevance_score": 0.95, "embedding_distance": 0.05, "metadata": {"symbol_name": "login", "file_path": "auth.py"}},
        {"doc_id": "doc1", "relevance_score": 0.5, "embedding_distance": 0.5, "metadata": {"symbol_name": "create_db", "file_path": "db.py"}},
    ]
    return bm25_res, vector_res


def test_fuse_rrf_returns_search_result_items_not_raw_dicts():
    """Before Phase 2.3, fuse_rrf returned {"doc_id", "rrf_score",
    "metadata"} dicts -- incompatible with IndexingEngine.search()'s
    return type and /v1/search's `[r.to_dict() for r in results]`. This
    is the exact contract change this milestone requires."""
    ranker = HybridRanker()
    bm25_res, vector_res = _rrf_fixture()

    results = ranker.fuse_rrf(bm25_results=bm25_res, vector_results=vector_res)

    assert len(results) == 3
    for r in results:
        assert isinstance(r, SearchResultItem)
        # Round-trips through the same response shape every other
        # search_type already produces.
        d = r.to_dict()
        assert set(d.keys()) == {
            "doc_id", "symbol_name", "file_path", "type", "start_line", "end_line",
            "signature", "docstring", "snippet", "language", "relevance_score",
            "keyword_score", "vector_score", "embedding_distance", "graph_score",
        }


def test_fuse_rrf_formula_is_correct_reciprocal_rank_sum():
    """doc1: rank 1 in bm25 (1/(60+1)) + rank 2 in vector (1/(60+2)).
    doc2: rank 2 in bm25 (1/(60+2)) + rank 1 in vector (1/(60+1)) -- same
    total by symmetry, so doc1 and doc2 tie; doc3 (bm25-only, rank 3) is
    strictly lower and must rank last."""
    ranker = HybridRanker(rrf_k=60)
    bm25_res, vector_res = _rrf_fixture()

    results = ranker.fuse_rrf(bm25_results=bm25_res, vector_results=vector_res)

    expected_doc1 = (1.0 / 61) + (1.0 / 62)
    expected_doc3 = 1.0 / 63

    by_id = {r.doc_id: r for r in results}
    assert round(by_id["doc1"].final_score, 10) == round(expected_doc1, 10)
    assert round(by_id["doc2"].final_score, 10) == round(expected_doc1, 10)
    assert round(by_id["doc3"].final_score, 10) == round(expected_doc3, 10)
    assert results[-1].doc_id == "doc3", "the bm25-only, lowest-ranked doc must sort last"


def test_fuse_rrf_metadata_and_per_source_scores_are_preserved():
    ranker = HybridRanker()
    bm25_res, vector_res = _rrf_fixture()

    results = ranker.fuse_rrf(bm25_results=bm25_res, vector_results=vector_res)
    by_id = {r.doc_id: r for r in results}

    assert by_id["doc1"].symbol_name == "create_db"
    assert by_id["doc1"].file_path == "db.py"
    # Real, already-computed per-source scores are surfaced for
    # transparency, even though RRF's own combination is rank-based, not
    # score-based -- not fabricated, and not used by the RRF formula itself.
    assert by_id["doc1"].keyword_score == 0.9
    assert by_id["doc1"].vector_score == 0.5
    # fuse_rrf never receives graph_scores -- always exactly 0.0, never fabricated.
    assert by_id["doc1"].graph_score == 0.0


def test_fuse_rrf_top_k_truncates():
    ranker = HybridRanker()
    bm25_res, vector_res = _rrf_fixture()

    results = ranker.fuse_rrf(bm25_results=bm25_res, vector_results=vector_res, top_k=1)
    assert len(results) == 1


def test_fuse_rrf_k_parameter_override_changes_the_score():
    ranker = HybridRanker(rrf_k=60)
    bm25_res, vector_res = _rrf_fixture()

    default_results = {r.doc_id: r.final_score for r in ranker.fuse_rrf(bm25_results=bm25_res, vector_results=vector_res)}
    override_results = {r.doc_id: r.final_score for r in ranker.fuse_rrf(bm25_results=bm25_res, vector_results=vector_res, k=1)}

    assert default_results["doc1"] != override_results["doc1"]
    assert round(override_results["doc3"], 10) == round(1.0 / (1 + 3), 10)


def test_fuse_rrf_empty_inputs_returns_empty_list():
    ranker = HybridRanker()
    assert ranker.fuse_rrf(bm25_results=[], vector_results=[]) == []


def test_fuse_deterministic_behavior_unaffected_by_rrf_refactor():
    """Regression guard: fuse()'s own behavior must be byte-for-byte
    unchanged now that it shares _build_result_item with fuse_rrf()."""
    ranker = HybridRanker(weight_keyword=0.4, weight_vector=0.4, weight_graph=0.2, exact_match_boost=0.15)
    bm25_res = [{"doc_id": "doc1", "normalized_score": 0.9, "metadata": {"symbol_name": "create_db", "file_path": "db.py"}}]
    vector_res = [{"doc_id": "doc1", "relevance_score": 0.85, "embedding_distance": 0.15, "metadata": {"symbol_name": "create_db", "file_path": "db.py"}}]

    run1 = ranker.fuse("database create", bm25_results=bm25_res, vector_results=vector_res, graph_scores={"doc1": 0.8})
    run2 = ranker.fuse("database create", bm25_results=bm25_res, vector_results=vector_res, graph_scores={"doc1": 0.8})
    assert run1[0].final_score == run2[0].final_score
    # "create_db" is not a substring of "database create" (or vice versa),
    # so the exact-match boost does not apply here -- plain weighted sum.
    expected = min(1.0, 0.4 * 0.9 + 0.4 * 0.85 + 0.2 * 0.8)
    assert run1[0].final_score == expected
