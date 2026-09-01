from __future__ import annotations

from typing import Any, Sequence
from codepulse.models import Symbol


class SearchResultItem:
    """A scored result item returned from the hybrid search engine."""

    def __init__(
        self,
        doc_id: str,
        symbol_name: str,
        file_path: str,
        symbol_type: str,
        start_line: int,
        end_line: int,
        signature: str = "",
        docstring: str | None = None,
        snippet: str = "",
        language: str = "",
        final_score: float = 0.0,
        keyword_score: float = 0.0,
        vector_score: float = 0.0,
        embedding_distance: float = 1.0,
        graph_score: float = 0.0,
    ) -> None:
        self.doc_id = doc_id
        self.symbol_name = symbol_name
        self.file_path = file_path
        self.symbol_type = symbol_type
        self.start_line = start_line
        self.end_line = end_line
        self.signature = signature
        self.docstring = docstring
        self.snippet = snippet
        self.language = language
        self.final_score = final_score
        self.keyword_score = keyword_score
        self.vector_score = vector_score
        self.embedding_distance = embedding_distance
        self.graph_score = graph_score

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "symbol_name": self.symbol_name,
            "file_path": self.file_path,
            "type": self.symbol_type,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "signature": self.signature,
            "docstring": self.docstring,
            "snippet": self.snippet,
            "language": self.language,
            "relevance_score": round(self.final_score, 4),
            "keyword_score": round(self.keyword_score, 4),
            "vector_score": round(self.vector_score, 4),
            "embedding_distance": round(self.embedding_distance, 4),
            "graph_score": round(self.graph_score, 4),
        }


class HybridRanker:
    """
    Merges and ranks candidate results from BM25, Dense Vector Search, and the Call Graph.
    
    Supports:
    1. Weighted Linear Fusion (40% keyword + 40% vector + 20% graph + exact match boost)
    2. Reciprocal Rank Fusion (RRF)
    """

    def __init__(
        self,
        weight_keyword: float | None = None,
        weight_vector: float | None = None,
        weight_graph: float | None = None,
        exact_match_boost: float | None = None,
    ) -> None:
        from codepulse.config import get_settings
        settings = get_settings()
        self.weight_keyword = weight_keyword if weight_keyword is not None else settings.weight_keyword
        self.weight_vector = weight_vector if weight_vector is not None else settings.weight_vector
        self.weight_graph = weight_graph if weight_graph is not None else settings.weight_graph
        self.exact_match_boost = (
            exact_match_boost if exact_match_boost is not None else settings.exact_match_boost
        )

    def fuse(
        self,
        query: str,
        bm25_results: list[dict[str, Any]],
        vector_results: list[dict[str, Any]],
        graph_scores: dict[str, float] | None = None,  # doc_id -> score in [0, 1]
        top_k: int = 20,
    ) -> list[SearchResultItem]:
        """
        Fuses BM25 and Vector search results using weighted linear combination.
        """
        graph_scores = graph_scores or {}
        query_lower = query.lower().strip()

        # Collect all unique candidate doc_ids
        candidates: dict[str, dict[str, Any]] = {}

        # 1. Process BM25 matches
        for item in bm25_results:
            doc_id = item["doc_id"]
            candidates[doc_id] = {
                "metadata": item.get("metadata", {}),
                "kw_score": item.get("normalized_score", 0.0),
                "vec_score": 0.0,
                "emb_dist": 1.0,
                "graph_score": graph_scores.get(doc_id, 0.0),
            }

        # 2. Process Vector matches
        for item in vector_results:
            doc_id = item["doc_id"]
            if doc_id not in candidates:
                candidates[doc_id] = {
                    "metadata": item.get("metadata", {}),
                    "kw_score": 0.0,
                    "vec_score": item.get("relevance_score", 0.0),
                    "emb_dist": item.get("embedding_distance", 1.0),
                    "graph_score": graph_scores.get(doc_id, 0.0),
                }
            else:
                candidates[doc_id]["vec_score"] = item.get("relevance_score", 0.0)
                candidates[doc_id]["emb_dist"] = item.get("embedding_distance", 1.0)
                if not candidates[doc_id]["metadata"] and item.get("metadata"):
                    candidates[doc_id]["metadata"] = item["metadata"]

        scored_items: list[SearchResultItem] = []

        for doc_id, data in candidates.items():
            meta = data["metadata"]
            sym_name = meta.get("symbol_name", "")
            file_path = meta.get("file_path", "")
            sym_type = meta.get("type", "function")
            start_line = meta.get("start_line", 1)
            end_line = meta.get("end_line", 1)
            signature = meta.get("signature", "")
            docstring = meta.get("docstring")
            snippet = meta.get("snippet", signature or sym_name)
            language = meta.get("language", "")

            kw_score = data["kw_score"]
            vec_score = data["vec_score"]
            emb_dist = data["emb_dist"]
            g_score = data["graph_score"]

            # Compute weighted combination
            combined = (
                self.weight_keyword * kw_score
                + self.weight_vector * vec_score
                + self.weight_graph * g_score
            )

            # Apply exact match boost if the query explicitly names the symbol
            if sym_name and sym_name.lower() in query_lower:
                combined += self.exact_match_boost
            elif query_lower in sym_name.lower():
                combined += self.exact_match_boost * 0.5

            final_score = min(1.0, combined)

            scored_items.append(
                SearchResultItem(
                    doc_id=doc_id,
                    symbol_name=sym_name,
                    file_path=file_path,
                    symbol_type=sym_type,
                    start_line=start_line,
                    end_line=end_line,
                    signature=signature,
                    docstring=docstring,
                    snippet=snippet,
                    language=language,
                    final_score=final_score,
                    keyword_score=kw_score,
                    vector_score=vec_score,
                    embedding_distance=emb_dist,
                    graph_score=g_score,
                )
            )

        # Sort descending by final score
        scored_items.sort(key=lambda x: x.final_score, reverse=True)
        return scored_items[:top_k]

    def fuse_rrf(
        self,
        bm25_results: list[dict[str, Any]],
        vector_results: list[dict[str, Any]],
        k: int = 60,
        top_k: int = 20,
    ) -> list[dict[str, Any]]:
        """
        Reciprocal Rank Fusion (RRF) combiner.
        Formula: RRF_score(d) = sum(1 / (k + rank_i(d)))
        """
        rrf_scores: dict[str, float] = {}
        metadata_map: dict[str, dict[str, Any]] = {}

        for rank, item in enumerate(bm25_results, start=1):
            doc_id = item["doc_id"]
            rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + (1.0 / (k + rank))
            metadata_map[doc_id] = item.get("metadata", {})

        for rank, item in enumerate(vector_results, start=1):
            doc_id = item["doc_id"]
            rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + (1.0 / (k + rank))
            if doc_id not in metadata_map:
                metadata_map[doc_id] = item.get("metadata", {})

        sorted_docs = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
        results = []
        for doc_id, score in sorted_docs[:top_k]:
            results.append({
                "doc_id": doc_id,
                "rrf_score": score,
                "metadata": metadata_map.get(doc_id, {}),
            })
        return results
