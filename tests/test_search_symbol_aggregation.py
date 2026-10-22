"""
Search results are per SYMBOL, and a symbol's keyword and semantic evidence is
combined (FUTURE_CHANGES item 20, 2026-10-15/17).

The defect: the lexical index holds one document per symbol
(`repo::file::symbol`) but the vector index holds one document per CHUNK
(`repo::file::symbol::chunk_index`, about 2.5 per symbol). Fusion merges
candidates by `doc_id`, so a symbol's keyword score and its chunk's vector
score never met: "hybrid" was two disjoint lists interleaved, RRF never added
a symbol's two ranks together, and about 46% of a semantic/hybrid top-10 was
repeat chunks of a symbol already shown. Semantic-only rows also lacked the
symbol's type, signature, docstring and language (chunk metadata has none).

Rules pinned down here, for every mode that uses the vector index
(semantic, hybrid, rrf):
  - at most ONE row per symbol;
  - a symbol found by both sources carries both scores, so fusing helps it;
  - the vector score is the BEST of the symbol's chunks (deterministic);
  - a vector-only row still describes the symbol (type, signature, docstring);
  - repository scoping still holds.

A stand-in embedder (hashed bag of words, normalised) is used so similarity
means "shares words with the query" and no model download is needed.
"""
from __future__ import annotations

import hashlib

import numpy as np
import pytest

from codepulse.embeddings.embedder import CodeEmbedder
from codepulse.indexer.engine import IndexingEngine
from codepulse.storage.db import StorageManager
from codepulse.symbol_identity import symbol_doc_id

_DIM = 384


class _BagOfWordsModel:
    def encode(self, texts, batch_size=32, show_progress_bar=False, convert_to_numpy=True, normalize_embeddings=True):
        out = []
        for text in texts:
            v = np.zeros(_DIM, dtype=np.float32)
            for word in text.lower().replace("_", " ").replace("(", " ").replace(")", " ").replace(":", " ").split():
                h = int(hashlib.md5(word.encode()).hexdigest(), 16)
                v[h % _DIM] += 1.0
            n = np.linalg.norm(v)
            out.append(v / n if n else v)
        return np.vstack(out)


def _embedder() -> CodeEmbedder:
    e = CodeEmbedder()
    e._model = _BagOfWordsModel()
    return e


# A long function so the chunker splits it into several chunks, plus small ones.
_LONG_BODY = "".join(f"    step_{i} = compute_checksum(buffer, {i})\n" for i in range(120))
FILES = {
    "ingest.py": (
        "def verify_archive(buffer):\n"
        '    """Verify the checksum of an archive buffer before unpacking."""\n'
        + _LONG_BODY
        + "    return step_119\n\n\n"
        "def unrelated_helper(x):\n"
        '    """Format a greeting for the welcome banner."""\n'
        "    return 'hello ' + x\n"
    ),
    "net.py": (
        "def open_socket(host):\n"
        '    """Open a network socket to the given host."""\n'
        "    return host\n"
    ),
}

QUERY = "verify the checksum of an archive buffer"


@pytest.fixture
def engine_and_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for name, content in FILES.items():
        (repo / name).write_text(content, encoding="utf-8")
    engine = IndexingEngine(
        storage=StorageManager(db_url=f"sqlite:///{(tmp_path / 'agg.db').as_posix()}"),
        embedder=_embedder(),
    )
    result = engine.index_directory(str(repo), repo_name="agg", repo_url=str(repo))
    return engine, result["repository_id"]


def _symbols(results):
    return [(r.file_path, r.symbol_name) for r in results]


def test_the_long_function_really_has_several_chunks(engine_and_repo):
    """Guard for the tests below: without several chunks per symbol they prove nothing."""
    engine, repo_id = engine_and_repo
    chunk_ids = [d for d in engine.vector_index.doc_ids if "::verify_archive@" in d]
    assert len(chunk_ids) >= 3, chunk_ids


@pytest.mark.parametrize("mode", ["semantic", "hybrid", "rrf"])
def test_each_symbol_appears_at_most_once(engine_and_repo, mode):
    engine, repo_id = engine_and_repo
    results = engine.search(QUERY, limit=10, search_type=mode, repository_id=repo_id)
    symbols = _symbols(results)
    assert len(symbols) == len(set(symbols)), f"{mode}: repeated symbols in {symbols}"
    assert len({r.doc_id for r in results}) == len(results)


@pytest.mark.parametrize("mode", ["semantic", "hybrid", "rrf"])
def test_result_ids_are_symbol_ids_not_chunk_ids(engine_and_repo, mode):
    engine, repo_id = engine_and_repo
    for r in engine.search(QUERY, limit=10, search_type=mode, repository_id=repo_id):
        assert r.doc_id == symbol_doc_id(repo_id, r.file_path, r.symbol_name, r.start_line), r.doc_id
        assert r.doc_id in engine.symbols_by_id


def test_a_symbol_found_by_both_sources_carries_both_scores(engine_and_repo):
    engine, repo_id = engine_and_repo
    for mode in ("hybrid", "rrf"):
        top = engine.search(QUERY, limit=10, search_type=mode, repository_id=repo_id)[0]
        assert top.symbol_name == "verify_archive"
        assert top.keyword_score > 0, f"{mode}: lexical evidence was dropped"
        assert top.vector_score > 0, f"{mode}: semantic evidence was dropped"


def test_fusing_both_sources_scores_a_symbol_above_either_source_alone(engine_and_repo):
    engine, repo_id = engine_and_repo
    hybrid = engine.search(QUERY, limit=10, search_type="hybrid", repository_id=repo_id)[0]
    keyword = engine.search(QUERY, limit=10, search_type="keyword", repository_id=repo_id)[0]
    semantic = engine.search(QUERY, limit=10, search_type="semantic", repository_id=repo_id)[0]
    assert hybrid.symbol_name == keyword.symbol_name == semantic.symbol_name == "verify_archive"
    # Keyword mode still reports a fused-scale score, so hybrid (which adds the semantic term) must exceed it.
    assert hybrid.final_score > keyword.final_score
    # Semantic-only mode reports the real relevance instead of a weighted score (see
    # HybridRanker.rank_by_similarity), so it is not on the fused scale; compare with the
    # symbol's weighted semantic term instead: scaled semantic scores are at most 1.0, so that term alone
    # can contribute at most `weight_vector`, and the keyword term must lift hybrid above it.
    assert hybrid.final_score > engine.ranker.weight_vector
    assert semantic.final_score == pytest.approx(semantic.vector_score)


def test_the_semantic_score_is_the_best_of_the_symbols_chunks(engine_and_repo):
    engine, repo_id = engine_and_repo
    query_vec = engine.embedder.encode(QUERY)
    chunk_hits = [
        h for h in engine.vector_index.search(query_vec, top_k=len(engine.vector_index.doc_ids))
        if h["metadata"]["symbol_name"] == "verify_archive"
    ]
    best = max(h["relevance_score"] for h in chunk_hits)
    top = engine.search(QUERY, limit=5, search_type="semantic", repository_id=repo_id)[0]
    assert top.symbol_name == "verify_archive"
    assert top.vector_score == pytest.approx(best)


def test_a_semantic_only_row_still_describes_the_symbol(engine_and_repo):
    engine, repo_id = engine_and_repo
    top = engine.search(QUERY, limit=5, search_type="semantic", repository_id=repo_id)[0]
    assert top.symbol_type == "function"
    assert top.signature.startswith("def verify_archive")
    assert top.docstring and "checksum" in top.docstring
    assert top.language == "python"
    assert top.start_line == 1 and top.end_line > 100, "the symbol's own line range, not one chunk's"


def test_aggregation_is_deterministic(engine_and_repo):
    engine, repo_id = engine_and_repo
    runs = [
        [(r.doc_id, round(r.final_score, 6)) for r in engine.search(QUERY, limit=10, search_type="hybrid", repository_id=repo_id)]
        for _ in range(3)
    ]
    assert runs[0] == runs[1] == runs[2]


def test_an_unscoped_search_still_fills_the_limit_with_distinct_symbols(engine_and_repo):
    """Unscoped search used to fetch `limit*2` chunks; folded to symbols that can fall short of `limit`."""
    engine, _ = engine_and_repo
    results = engine.search(QUERY, limit=3, search_type="semantic")
    assert len(results) == 3
    assert len({(r.file_path, r.symbol_name) for r in results}) == 3


def test_similar_symbols_lists_each_symbol_once(engine_and_repo):
    """`find_similar_symbols` (Similar Code view, context bundle) walked raw chunk hits, so one
    multi-chunk symbol could be listed several times."""
    engine, repo_id = engine_and_repo
    similar = engine.find_similar_symbols(QUERY, exclude_symbol_name="unrelated_helper", repo_id=repo_id, threshold=0.0, limit=10)
    names = [(s["file_path"], s["symbol_name"]) for s in similar]
    assert ("ingest.py", "verify_archive") in names
    assert len(names) == len(set(names)), names
    assert all("unrelated_helper" != n for _, n in names)
    # similarity is the best chunk's, and the list is best first
    scores = [s["similarity"] for s in similar]
    assert scores == sorted(scores, reverse=True)


# --- same-named symbols in one file ---------------------------------------------------
#
# Same-named symbols in one file (three classes' `__init__`) used to share one id (FUTURE_CHANGES
# item 31, fixed: ids now carry the start line, see tests/test_symbol_identity.py). Folding chunks per
# symbol must keep them apart: a result must never be described with another symbol's line range.

_COLLIDING = '''class Alpha:
    def __init__(self, a):
        """Alpha constructor stores the alpha value."""
        self.a = a


class Beta:
    def __init__(self, b):
        """Beta constructor stores the beta value."""
        self.b = b


class Gamma:
    def __init__(self, c):
        """Gamma constructor stores the gamma value."""
        self.c = c
'''
_INIT_LINES = {"alpha": 2, "beta": 8, "gamma": 14}


@pytest.fixture
def colliding_engine(tmp_path):
    repo = tmp_path / "colliding"
    repo.mkdir()
    (repo / "m.py").write_text(_COLLIDING, encoding="utf-8")
    engine = IndexingEngine(
        storage=StorageManager(db_url=f"sqlite:///{(tmp_path / 'col.db').as_posix()}"), embedder=_embedder()
    )
    return engine, engine.index_directory(str(repo), repo_name="col", repo_url=str(repo))["repository_id"]


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
@pytest.mark.parametrize("who", ["alpha", "beta", "gamma"])
def test_a_result_is_never_described_with_another_symbols_line_range(colliding_engine, mode, who):
    engine, repo_id = colliding_engine
    results = engine.search(f"{who} constructor stores the {who} value", limit=10, search_type=mode, repository_id=repo_id)
    first_init = next(r for r in results if r.symbol_name == "__init__")
    assert first_init.start_line <= _INIT_LINES[who] <= first_init.end_line, (
        f"{mode}/{who}: the best __init__ result spans {first_init.start_line}-{first_init.end_line}, "
        f"not the line {_INIT_LINES[who]} of {who}'s own __init__"
    )
