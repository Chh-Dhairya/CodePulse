from __future__ import annotations

import logging
from typing import Sequence
import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384


class EmbeddingUnavailableError(RuntimeError):
    """
    Raised when the configured embedding model cannot be loaded or cannot
    produce a real embedding.

    Permanent project invariant (see CLAUDE.md / roadmap §17, §34): there is
    NO pseudo-embedding fallback. Callers MUST treat this as an explicit,
    observable failure -- skip persisting/using the affected vectors and
    report a degraded (lexical-only) status -- never silently substitute a
    hash-based, random, or otherwise fabricated vector in its place.
    """


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
                logger.error(
                    "Failed to load embedding model '%s': %s. No pseudo-embedding "
                    "fallback will be used -- embedding generation must fail explicitly.",
                    self.model_name,
                    e,
                )
                raise EmbeddingUnavailableError(
                    f"Embedding model '{self.model_name}' could not be loaded: {e}"
                ) from e
        return self._model

    def preload(self) -> None:
        """
        Loads the model now instead of on the first `encode()`. Used by
        `serve --mcp`: importing sentence-transformers for the first time
        from the stdio server's tool-call worker thread hangs on Windows,
        so the model must be loaded on the main thread before serving.
        Raises EmbeddingUnavailableError exactly as `encode()` would.
        """
        self._get_model()

    def encode(self, texts: str | list[str], batch_size: int = 32) -> np.ndarray:
        """
        Encodes a single string or list of strings into normalized embedding vectors.
        Returns a numpy array of shape (N, dimension).

        Raises EmbeddingUnavailableError if the configured model cannot be
        loaded or fails to produce embeddings. There is no fallback path --
        callers are responsible for handling this explicitly (see
        EmbeddingUnavailableError's docstring).
        """
        is_single = isinstance(texts, str)
        text_list = [texts] if is_single else texts

        if not text_list:
            return np.empty((0, self.dimension), dtype=np.float32)

        model = self._get_model()

        # Phase 7.4 (roadmap §37 item 7.4; §18: "embedding_duration_seconds").
        from codepulse.observability import embedding_duration_seconds
        with embedding_duration_seconds.time():
            try:
                embeddings = model.encode(
                    text_list,
                    batch_size=batch_size,
                    show_progress_bar=False,
                    convert_to_numpy=True,
                    normalize_embeddings=True,
                )
            except EmbeddingUnavailableError:
                raise
            except Exception as e:
                logger.error("Embedding model '%s' failed to encode input: %s", self.model_name, e)
                raise EmbeddingUnavailableError(
                    f"Embedding model '{self.model_name}' failed to encode input: {e}"
                ) from e

        return embeddings[0] if is_single else embeddings

    @staticmethod
    def cosine_similarity(v1: np.ndarray, v2: np.ndarray) -> float:
        """Computes cosine similarity between two normalized vectors."""
        dot = np.dot(v1, v2)
        norm1 = np.linalg.norm(v1)
        norm2 = np.linalg.norm(v2)
        if norm1 == 0 or norm2 == 0:
            return 0.0
        return float(dot / (norm1 * norm2))
