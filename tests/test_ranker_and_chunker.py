from codepulse.embeddings.chunker import SemanticChunker
from codepulse.models import Symbol, SymbolType
from codepulse.search.ranker import HybridRanker


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
