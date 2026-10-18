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
        rrf_k: int | None = None,
        vector_floor_rank: int | None = None,
    ) -> None:
        from codepulse.config import get_settings
        settings = get_settings()
        self.weight_keyword = weight_keyword if weight_keyword is not None else settings.weight_keyword
        self.weight_vector = weight_vector if weight_vector is not None else settings.weight_vector
        self.weight_graph = weight_graph if weight_graph is not None else settings.weight_graph
        self.exact_match_boost = (
            exact_match_boost if exact_match_boost is not None else settings.exact_match_boost
        )
        # Phase 2.3 (roadmap §37 item 2.3: "...selectable via API
        # search_type/config") -- resolved once at construction, matching
        # every other tunable ranking parameter above, rather than read
        # fresh from settings on every fuse_rrf() call.
        self.rrf_k = rrf_k if rrf_k is not None else settings.rrf_k
        self.vector_floor_rank = (
            vector_floor_rank if vector_floor_rank is not None else settings.vector_floor_rank
        )

    def _vector_scores_for_fusion(self, vector_results: list[dict[str, Any]]) -> dict[str, float]:
        """
        The semantic score each vector candidate contributes to weighted fusion
        (FUTURE_CHANGES item 20, part 2).

        The lexical score is relative to the query's best keyword hit; a raw
        cosine similarity is absolute and, for real code, sits in a narrow band
        (the old `(cosine + 1) / 2` lived in roughly 0.5..0.9), so weighting the
        two 0.4 / 0.4 compressed the semantic signal. Here the score is
        `(sim - floor) / (top - floor)` clipped to 0..1: `top` is the best
        similarity for this query, `floor` the similarity of the hit at
        `vector_floor_rank`. Hits ranked below the floor therefore never change
        anyone's score, so a result scores the same whatever `limit` was asked
        for, and input order is irrelevant. With no spread (a single candidate,
        or all equal) every candidate with positive similarity scores 1.0
        rather than being zeroed. `vector_floor_rank == 0` is the old absolute
        scaling. The value REPORTED on a result (`vector_score`) is always the
        real, unscaled one; only the fusion input is scaled.
        """
        if self.vector_floor_rank <= 0:
            return {it["doc_id"]: it.get("relevance_score", 0.0) for it in vector_results}

        def sim_of(it: dict[str, Any]) -> float:
            sim = it.get("similarity")
            if sim is None:
                sim = 2.0 * it.get("relevance_score", 0.5) - 1.0
            return max(0.0, sim)

        sims = sorted((sim_of(it) for it in vector_results), reverse=True)
        if not sims:
            return {}
        top = sims[0]
        floor = sims[min(self.vector_floor_rank, len(sims)) - 1]
        span = top - floor
        scaled: dict[str, float] = {}
        for it in vector_results:
            sim = sim_of(it)
            if top <= 0.0:
                scaled[it["doc_id"]] = 0.0
            elif span <= 1e-12:
                scaled[it["doc_id"]] = 1.0
            else:
                scaled[it["doc_id"]] = min(1.0, max(0.0, (sim - floor) / span))
        return scaled

    def _build_result_item(
        self,
        doc_id: str,
        metadata: dict[str, Any],
        *,
        final_score: float,
        keyword_score: float = 0.0,
        vector_score: float = 0.0,
        embedding_distance: float = 1.0,
        graph_score: float = 0.0,
    ) -> SearchResultItem:
        """
        Reconstructs a `SearchResultItem` from a candidate's metadata dict
        (the same shape `BM25Index`/`VectorIndex` attach to every result --
        `symbol_name`, `file_path`, `type`, `start_line`, `end_line`,
        `signature`, `docstring`, `snippet`, `language`) plus whatever
        score fields the calling fusion strategy already computed.

        Phase 2.3 (roadmap §36/§37 item 2.3: "wire the already-implemented
        (but unused) RRF fusion path to be selectable via the API") --
        shared by both `fuse()` (weighted linear fusion) and `fuse_rrf()`
        (Reciprocal Rank Fusion) so this metadata-to-result-object mapping
        is never duplicated between the two fusion strategies. `fuse()`'s
        own metadata-reconstruction logic is unchanged in behavior; it now
        simply calls this shared helper instead of inlining the same
        field-by-field extraction itself.
        """
        meta = metadata
        return SearchResultItem(
            doc_id=doc_id,
            symbol_name=meta.get("symbol_name", ""),
            file_path=meta.get("file_path", ""),
            symbol_type=meta.get("type", "function"),
            start_line=meta.get("start_line", 1),
            end_line=meta.get("end_line", 1),
            signature=meta.get("signature", ""),
            docstring=meta.get("docstring"),
            snippet=meta.get("snippet", meta.get("signature", "") or meta.get("symbol_name", "")),
            language=meta.get("language", ""),
            final_score=final_score,
            keyword_score=keyword_score,
            vector_score=vector_score,
            embedding_distance=embedding_distance,
            graph_score=graph_score,
        )

    def rank_by_similarity(self, vector_results: list[dict[str, Any]], top_k: int = 20) -> list[SearchResultItem]:
        """
        Semantic-only search: results ordered purely by vector similarity.

        Nothing is fused here, so there is no keyword/graph term and no
        exact-name boost, and the score reported is the real relevance of the
        match. (It used to go through `fuse()`: the score became
        `0.4 x relevance + 0.15 boost`, a narrow 0.2-0.36 band in which a
        name that merely appeared inside the query text jumped past much closer
        matches; on a second codebase MRR was 0.272 against 0.551 without the
        boost.) Ties break on `doc_id`, so the order is deterministic.
        """
        ordered = sorted(vector_results, key=lambda it: (-it.get("relevance_score", 0.0), it["doc_id"]))
        return [
            self._build_result_item(
                it["doc_id"],
                it.get("metadata", {}),
                final_score=it.get("relevance_score", 0.0),
                vector_score=it.get("relevance_score", 0.0),
                embedding_distance=it.get("embedding_distance", 1.0),
            )
            for it in ordered[:top_k]
        ]

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
        The semantic score is scaled relative to the query's own results before
        it is weighted against the lexical score (`_vector_scores_for_fusion`).
        """
        graph_scores = graph_scores or {}
        query_lower = query.lower().strip()
        fusion_vec_scores = self._vector_scores_for_fusion(vector_results)

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

            kw_score = data["kw_score"]
            vec_score = data["vec_score"]
            emb_dist = data["emb_dist"]
            g_score = data["graph_score"]

            # Compute weighted combination
            combined = (
                self.weight_keyword * kw_score
                + self.weight_vector * fusion_vec_scores.get(doc_id, 0.0)
                + self.weight_graph * g_score
            )

            # Apply exact match boost if the query explicitly names the symbol
            if sym_name and sym_name.lower() in query_lower:
                combined += self.exact_match_boost
            elif query_lower in sym_name.lower():
                combined += self.exact_match_boost * 0.5

            final_score = min(1.0, combined)

            scored_items.append(
                self._build_result_item(
                    doc_id,
                    meta,
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
        k: int | None = None,
        top_k: int = 20,
    ) -> list[SearchResultItem]:
        """
        Reciprocal Rank Fusion (RRF) combiner.
        Formula: RRF_score(d) = sum(1 / (k + rank_i(d)))

        `k` defaults to `self.rrf_k` (itself resolved from
        `settings.rrf_k`, default 60 -- the conventional constant from the
        original RRF paper) when not explicitly overridden by the caller.

        Phase 2.3 (roadmap §36/§37 item 2.3): returns `list[SearchResultItem]`
        -- the same return type `fuse()` already produces, and the shape
        `IndexingEngine.search()`/`/v1/search`'s `[r.to_dict() for r in
        results]` already expect -- rather than the raw
        `{"doc_id", "rrf_score", "metadata"}` dicts this method returned
        before this milestone wired it up to the API. The fusion algorithm
        itself (the RRF formula/ranking below) is unchanged; only the
        final step -- turning a ranked (doc_id, score) list into full
        result objects -- was added, reusing `_build_result_item` so this
        mapping is never duplicated against `fuse()`'s own.

        `final_score`/`relevance_score` on the returned items is the real,
        computed RRF score (unbounded above 0, not rescaled/clamped to
        [0, 1] the way `fuse()`'s weighted-linear score is) -- reporting
        the genuine computed value rather than inventing a normalization
        the roadmap does not ask for (CLAUDE.md: no fabricated metrics).
        `keyword_score`/`vector_score` are still populated from each
        candidate's own real, already-computed per-source score (the same
        values `fuse()` reads), even though RRF itself combines by rank,
        not by these scores -- for transparency, not because RRF's own
        formula uses them. `graph_score` is always 0.0: `fuse_rrf` does
        not accept a `graph_scores` argument (unchanged from before this
        milestone -- adding call-graph fusion to RRF would be a ranking
        algorithm change, out of this milestone's scope, which only wires
        up the already-implemented, unmodified RRF formula).
        """
        effective_k = k if k is not None else self.rrf_k

        rrf_scores: dict[str, float] = {}
        metadata_map: dict[str, dict[str, Any]] = {}
        kw_score_map: dict[str, float] = {}
        vec_score_map: dict[str, float] = {}
        emb_dist_map: dict[str, float] = {}

        for rank, item in enumerate(bm25_results, start=1):
            doc_id = item["doc_id"]
            rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + (1.0 / (effective_k + rank))
            metadata_map[doc_id] = item.get("metadata", {})
            kw_score_map[doc_id] = item.get("normalized_score", 0.0)

        for rank, item in enumerate(vector_results, start=1):
            doc_id = item["doc_id"]
            rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + (1.0 / (effective_k + rank))
            if doc_id not in metadata_map:
                metadata_map[doc_id] = item.get("metadata", {})
            vec_score_map[doc_id] = item.get("relevance_score", 0.0)
            emb_dist_map[doc_id] = item.get("embedding_distance", 1.0)

        sorted_docs = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)

        results: list[SearchResultItem] = []
        for doc_id, score in sorted_docs[:top_k]:
            results.append(
                self._build_result_item(
                    doc_id,
                    metadata_map.get(doc_id, {}),
                    final_score=score,
                    keyword_score=kw_score_map.get(doc_id, 0.0),
                    vector_score=vec_score_map.get(doc_id, 0.0),
                    embedding_distance=emb_dist_map.get(doc_id, 1.0),
                    graph_score=0.0,
                )
            )
        return results
