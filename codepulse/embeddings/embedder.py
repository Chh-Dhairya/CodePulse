from __future__ import annotations

import logging
from typing import Sequence
import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384


class CodeEmbedder:
    """
    Generates dense vector embeddings for code snippets and natural language queries.
    Uses SentenceTransformer ('all-MiniLM-L6-v2' by default, 384 dimensions).
    """

    def __init__(
        self,
        model_name: str | None = None,
        device: str | None = None,
        dimension: int | None = None,
    ) -> None:
        from codepulse.config import get_settings
        settings = get_settings()
        self.model_name = model_name or settings.embedding_model
        self.device = device or settings.embedding_device
        self.dimension = dimension or settings.embedding_dimension
        self._model = None

    def _get_model(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                logger.info("Loading embedding model '%s' on %s...", self.model_name, self.device)
                self._model = SentenceTransformer(self.model_name, device=self.device)
            except Exception as e:
                logger.warning(
                    "Could not load SentenceTransformer ('%s'): %s. Using deterministic fallback.",
                    self.model_name,
                    e,
                )
                self._model = "fallback"
        return self._model

    def encode(self, texts: str | list[str], batch_size: int = 32) -> np.ndarray:
        """
        Encodes a single string or list of strings into normalized embedding vectors.
        Returns a numpy array of shape (N, 384).
        """
        is_single = isinstance(texts, str)
        text_list = [texts] if is_single else texts

        if not text_list:
            return np.empty((0, self.dimension), dtype=np.float32)

        model = self._get_model()

        if model != "fallback":
            embeddings = model.encode(
                text_list,
                batch_size=batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
                normalize_embeddings=True,
            )
            return embeddings[0] if is_single else embeddings
        else:
            # Deterministic pseudo-embedding for testing when weights aren't downloaded
            arr = np.zeros((len(text_list), self.dimension), dtype=np.float32)
            for idx, t in enumerate(text_list):
                import hashlib
                h = hashlib.sha256(t.encode("utf-8")).digest()
                # 384 floats * 4 bytes/float = 1536 bytes (32 bytes * 48 = 1536)
                raw = np.frombuffer((h * 48)[: self.dimension * 4], dtype=np.float32).copy()
                norm = np.linalg.norm(raw)
                arr[idx] = raw / (norm if norm > 0 else 1.0)

            return arr[0] if is_single else arr

    @staticmethod
    def cosine_similarity(v1: np.ndarray, v2: np.ndarray) -> float:
        """Computes cosine similarity between two normalized vectors."""
        dot = np.dot(v1, v2)
        norm1 = np.linalg.norm(v1)
        norm2 = np.linalg.norm(v2)
        if norm1 == 0 or norm2 == 0:
            return 0.0
        return float(dot / (norm1 * norm2))
