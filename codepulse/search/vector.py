from __future__ import annotations

from typing import Any, Sequence
import numpy as np

from codepulse.embeddings.chunker import CodeChunk


class VectorIndex:
    """
    High-performance vector index for dense embeddings.
    
    Supports:
    - Fast batch matrix-vector dot products (cosine similarity for normalized vectors).
    - Top-K nearest neighbor retrieval.
    - Metadata tracking per vector.
    """

    def __init__(self, dimension: int = 384) -> None:
        self.dimension = dimension
        self.doc_ids: list[str] = []
        self.metadata: list[dict[str, Any]] = []
        self.vectors: np.ndarray = np.empty((0, dimension), dtype=np.float32)

    def add(self, doc_id: str, vector: np.ndarray, metadata: dict[str, Any] | None = None) -> None:
        """Adds a single normalized vector with doc_id and metadata."""
        if vector.ndim == 1:
            vec_2d = vector.reshape(1, -1)
        else:
            vec_2d = vector

        # Ensure L2-normalized
        norm = np.linalg.norm(vec_2d, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        normalized_vec = vec_2d / norm

        if self.vectors.shape[0] == 0:
            self.vectors = normalized_vec.astype(np.float32)
        else:
            self.vectors = np.vstack([self.vectors, normalized_vec.astype(np.float32)])

        self.doc_ids.append(doc_id)
        self.metadata.append(metadata or {})

    def add_batch(
        self,
        doc_ids: list[str],
        vectors: np.ndarray,
        metadatas: list[dict[str, Any]] | None = None,
    ) -> None:
        """Adds a batch of vectors to the index."""
        if len(doc_ids) == 0:
            return

        # Normalize
        norm = np.linalg.norm(vectors, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        normalized_vecs = (vectors / norm).astype(np.float32)

        if self.vectors.shape[0] == 0:
            self.vectors = normalized_vecs
        else:
            self.vectors = np.vstack([self.vectors, normalized_vecs])

        self.doc_ids.extend(doc_ids)
        if metadatas:
            self.metadata.extend(metadatas)
        else:
            self.metadata.extend([{} for _ in doc_ids])

    def search(self, query_vector: np.ndarray, top_k: int = 20) -> list[dict[str, Any]]:
        """
        Calculates cosine similarities between query_vector and all indexed vectors.
        Returns top_k most similar entries with scores in [0, 1] and distances in [0, 2].
        """
        if self.vectors.shape[0] == 0:
            return []

        # Ensure query vector is normalized 1D
        q = query_vector.flatten()
        q_norm = np.linalg.norm(q)
        if q_norm > 0:
            q = q / q_norm

        # Matrix-vector multiplication for all dot products (Cosine similarity)
        # Shape: (N,)
        similarities = np.dot(self.vectors, q)

        # Clip values to [-1.0, 1.0] to guard against numerical float precision
        similarities = np.clip(similarities, -1.0, 1.0)

        num_results = min(top_k, len(self.doc_ids))
        if num_results == 0:
            return []

        # Get top-k indices using argpartition for fast O(N) retrieval
        if len(similarities) > num_results:
            top_indices = np.argpartition(similarities, -num_results)[-num_results:]
            top_indices = top_indices[np.argsort(-similarities[top_indices])]
        else:
            top_indices = np.argsort(-similarities)

        results = []
        for idx in top_indices:
            sim = float(similarities[idx])
            # Cosine distance: 1 - cosine_sim
            distance = max(0.0, 1.0 - sim)
            # Normalized relevance score in [0, 1]
            relevance = max(0.0, min(1.0, (sim + 1.0) / 2.0))

            results.append({
                "doc_id": self.doc_ids[idx],
                "similarity": sim,
                "relevance_score": relevance,
                "embedding_distance": distance,
                "metadata": self.metadata[idx],
            })

        return results
