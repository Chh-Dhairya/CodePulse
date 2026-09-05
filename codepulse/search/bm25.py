from __future__ import annotations

import math
from collections import Counter
from typing import Any, Sequence

from codepulse.models import Symbol
from codepulse.search.tokenizer import tokenize_code


class BM25Index:
    """
    In-memory BM25 index optimized for codebase symbols.
    
    Supports:
    - Code-aware tokenization (splitting snake_case/camelCase).
    - BM25Okapi scoring with configurable k1 and b.
    - Normalized score outputs [0, 1].
    - Metadata tracking per document.
    """

    def __init__(self, k1: float | None = None, b: float | None = None) -> None:
        from codepulse.config import get_settings
        settings = get_settings()
        self.k1 = k1 if k1 is not None else settings.bm25_k1
        self.b = b if b is not None else settings.bm25_b
        self.doc_ids: list[str] = []
        self.doc_lengths: list[int] = []
        self.doc_freqs: list[Counter[str]] = []
        self.doc_metadata: list[dict[str, Any]] = []
        self.df: Counter[str] = Counter()  # Document frequency of each token
        self.idf: dict[str, float] = {}
        self.num_docs: int = 0
        self.avgdl: float = 0.0

    def add_symbol(
        self,
        doc_id: str,
        symbol: Symbol,
        extra_text: str = "",
        repo_id: int | None = None,
    ) -> None:
        """
        Adds a Symbol to the BM25 index.

        `repo_id` (Phase 0.6) is carried through into the document's
        metadata so repository-scoped search can filter candidates by
        owning repository without needing a second identity system --
        it's the same `RepositoryModel.id` already used elsewhere.
        Optional/None for backward compatibility with any caller that
        doesn't track a repository (e.g. tests exercising this index
        directly, unrelated to Phase 0.6).
        """
        # Combine name, docstring, signature, and body snippet
        text_parts = [
            symbol.name * 3,  # Boost symbol name
            symbol.signature,
            symbol.docstring or "",
            extra_text,
            symbol.body[:1000] if symbol.body else "",
        ]
        combined_text = " ".join(text_parts)
        self.add_document(doc_id, combined_text, metadata={
            "symbol_name": symbol.name,
            "file_path": symbol.file_path,
            "type": symbol.type.value if hasattr(symbol.type, "value") else str(symbol.type),
            "start_line": symbol.start_line,
            "end_line": symbol.end_line,
            "signature": symbol.signature,
            "docstring": symbol.docstring,
            "language": symbol.language,
            "repo_id": repo_id,
        })

    def add_document(self, doc_id: str, text: str, metadata: dict[str, Any] | None = None) -> None:
        """Indexes a single document text with its ID and optional metadata."""
        tokens = tokenize_code(text)
        token_counts = Counter(tokens)

        self.doc_ids.append(doc_id)
        self.doc_lengths.append(len(tokens))
        self.doc_freqs.append(token_counts)
        self.doc_metadata.append(metadata or {})

        # Update document frequency for unique tokens
        for token in token_counts.keys():
            self.df[token] += 1

        self.num_docs += 1

    def build(self) -> None:
        """Calculates IDFs and average document length after all documents are added."""
        if self.num_docs == 0:
            self.avgdl = 0.0
            return

        self.avgdl = sum(self.doc_lengths) / self.num_docs
        self.idf.clear()

        # Compute IDF for every token in vocabulary
        for token, freq in self.df.items():
            # BM25 standard IDF formula with smoothing
            idf_val = math.log(1 + (self.num_docs - freq + 0.5) / (freq + 0.5))
            self.idf[token] = max(idf_val, 0.01)

    def search(self, query: str, top_k: int = 20) -> list[dict[str, Any]]:
        """
        Searches the index with the given query.
        Returns a sorted list of matched documents with normalized BM25 scores in [0, 1].
        """
        if self.num_docs == 0:
            return []

        query_tokens = tokenize_code(query)
        if not query_tokens:
            return []

        scores: list[float] = [0.0] * self.num_docs

        for token in query_tokens:
            if token not in self.idf:
                continue

            q_idf = self.idf[token]

            for doc_idx in range(self.num_docs):
                tf = self.doc_freqs[doc_idx].get(token, 0)
                if tf == 0:
                    continue

                doc_len = self.doc_lengths[doc_idx]
                numerator = tf * (self.k1 + 1.0)
                denominator = tf + self.k1 * (1.0 - self.b + self.b * (doc_len / self.avgdl))
                scores[doc_idx] += q_idf * (numerator / denominator)

        # Collect and rank positive score documents
        results = []
        max_score = max(scores) if scores and max(scores) > 0 else 1.0

        for doc_idx, score in enumerate(scores):
            if score > 0:
                normalized_score = min(1.0, score / max_score) if max_score > 0 else 0.0
                results.append({
                    "doc_id": self.doc_ids[doc_idx],
                    "raw_score": score,
                    "normalized_score": normalized_score,
                    "metadata": self.doc_metadata[doc_idx],
                })

        # Sort descending by score
        results.sort(key=lambda x: x["raw_score"], reverse=True)
        return results[:top_k]
