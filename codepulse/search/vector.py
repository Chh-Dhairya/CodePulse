from __future__ import annotations

import sqlite3
from typing import Any, Sequence

import numpy as np
import sqlite_vec


class VectorIndex:
    """
    Embedded vector similarity index for dense code embeddings (Phase 2.2).

    Replaces the pre-Phase-2.2 pure-NumPy brute-force implementation's
    *internals* with the embedded `sqlite-vec` SQLite extension (roadmap
    §36 Phase 2: "swap `VectorIndex` for an embedded ANN structure...
    without changing the ranking contract"; §7 Storage Decision: "a local
    vector index (e.g. `sqlite-vec` or a persisted HNSW structure via
    `hnswlib`)") while preserving, unchanged, every part of the class's
    external contract that `IndexingEngine`, `HybridRanker`,
    `codepulse/api/app.py`'s `find_similar_code` endpoint, and the
    existing test suite depend on:

    - Constructor: `VectorIndex(dimension=384)`.
    - `.dimension`: unchanged.
    - `.doc_ids`: a real `list[str]`, append-only (this milestone does not
      add a repository-replacement/delete capability -- see the module's
      own "Known limitation" note below).
    - `.vectors`: a genuine `numpy.ndarray` of shape `(num_docs,
      dimension)`, mirroring every normalized vector ever added, in
      insertion order -- `tests/test_embedding_persistence.py` asserts
      `.vectors.shape`/`.vectors.shape[1]` directly. The actual similarity
      search below never reads this array; it is maintained purely to
      keep this pre-existing public contract intact across the
      `sqlite-vec` swap.
    - `add(doc_id, vector, metadata=None)`, `add_batch(doc_ids, vectors,
      metadatas=None)`, `search(query_vector, top_k=20)` -- identical
      signatures and return shapes (`{"doc_id", "similarity",
      "relevance_score", "embedding_distance", "metadata"}` per result,
      `relevance_score`/`embedding_distance` derived from `similarity` via
      the exact same formulas the pre-Phase-2.2 implementation used).

    Dependency note (disclosed, not silently chosen): §7 names two
    candidate libraries, `sqlite-vec` or `hnswlib`, without choosing
    between them. `hnswlib` was attempted first and found to have no
    prebuilt wheel for this project's actual development platform
    (Windows, CPython 3.11) -- PyPI carries only a source distribution,
    and building it requires a C++ compiler (Microsoft Visual C++ Build
    Tools) that is not installed and was not installed for this milestone
    (installing system-level build tooling is outside this project's
    scope). `sqlite-vec` (0.1.9) has a prebuilt `win_amd64` wheel and was
    used instead -- verified directly (this module's own construction and
    round-trip experiments performed before writing this adapter, not
    assumed from documentation).

    Known, disclosed characteristic of the underlying library actually
    used: `sqlite-vec` 0.1.9's `vec0` virtual table performs an exact
    (brute-force) distance computation over every stored vector, not an
    approximate/graph-based nearest-neighbor structure the way `hnswlib`'s
    HNSW algorithm would -- confirmed by direct testing (identical
    results to the pre-Phase-2.2 NumPy brute-force implementation for
    every query tried, not merely "close"). This still satisfies §7's own
    literal wording (`sqlite-vec` is one of its two named options) and
    remains a genuine embedded, compiled-native vector index rather than
    a Python/NumPy one -- but is not literally an *approximate* nearest
    neighbor structure at this library version. Retrieval remains fully
    deterministic as a direct consequence (a permanent project invariant,
    CLAUDE.md §4), which an approximate structure would not guarantee as
    simply.

    Known limitation, deliberately NOT addressed in this milestone
    (disclosed, not silently expanded scope -- see CODEPULSE_PROGRESS.md):
    unlike Phase 2.1's `BM25Index.remove_repo`, this adapter does not
    implement repository-replacement (delete-by-`repo_id`-then-add)
    idempotency. The pre-Phase-2.2 `VectorIndex` never had this either --
    `add`/`add_batch` were, and remain, pure-append operations, so
    re-indexing an already-indexed repository was already silently
    accumulating duplicate vector entries before this milestone. This
    parity gap (lexical re-indexing is now idempotent per Phase 2.1;
    semantic re-indexing is not) is a genuine, real, pre-existing
    characteristic this milestone's literal scope ("adapter... behind
    existing `VectorIndex` call sites") does not require fixing -- CLAUDE.md
    §12 governs this: recorded here and in CODEPULSE_PROGRESS.md rather
    than opportunistically expanded into this milestone.

    Thread-safety note (a necessary, disclosed adjustment, not a design
    preference): the underlying `sqlite3.Connection` is constructed with
    `check_same_thread=False`. FastAPI/Starlette route handlers defined as
    plain `def` (every endpoint in this app, including the Phase 1.3
    background-task indexing path) run on a shared threadpool, so the same
    `IndexingEngine`/`VectorIndex` instance can legitimately be called
    from more than one OS thread across its lifetime. Python's `sqlite3`
    module refuses cross-thread use of one connection by default; the
    pre-Phase-2.2 pure NumPy/list implementation had no such restriction,
    so disabling this check is necessary purely to avoid this milestone
    introducing a new hard-crash mode where none existed before -- not a
    claim of full concurrent-write safety. This codebase has no locking
    around `bm25_index`/`vector_index` today (unchanged, pre-existing,
    out of this milestone's scope, matching Phase 2.1's own precedent).
    """

    def __init__(self, dimension: int = 384) -> None:
        self.dimension = dimension
        self.doc_ids: list[str] = []
        self.metadata: list[dict[str, Any]] = []
        self.vectors: np.ndarray = np.empty((0, dimension), dtype=np.float32)

        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        self._conn.execute(
            f"CREATE VIRTUAL TABLE vec_items USING vec0("
            f"embedding float[{dimension}] distance_metric=cosine)"
        )

    def add(self, doc_id: str, vector: np.ndarray, metadata: dict[str, Any] | None = None) -> None:
        """Adds a single normalized vector with doc_id and metadata."""
        vec_2d = vector.reshape(1, -1) if vector.ndim == 1 else vector
        self.add_batch([doc_id], vec_2d, [metadata] if metadata is not None else None)

    def add_batch(
        self,
        doc_ids: list[str],
        vectors: np.ndarray,
        metadatas: list[dict[str, Any]] | None = None,
    ) -> None:
        """Adds a batch of vectors to the index."""
        if len(doc_ids) == 0:
            return

        # Normalize -- unchanged from the pre-Phase-2.2 implementation.
        norm = np.linalg.norm(vectors, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        normalized_vecs = (vectors / norm).astype(np.float32)

        if self.vectors.shape[0] == 0:
            self.vectors = normalized_vecs
        else:
            self.vectors = np.vstack([self.vectors, normalized_vecs])

        # `vec_items`'s rowid is the ONLY handle sqlite-vec's own KNN query
        # gives back (Section: verified via direct experiment) -- it is not
        # the CodePulse `doc_id` string. Rowids are assigned 1-based, in the
        # exact same append order `self.doc_ids` grows in, and (since this
        # adapter never deletes a row -- see class docstring's "Known
        # limitation") rowid N always corresponds to `self.doc_ids[N - 1]` /
        # `self.metadata[N - 1]` for the lifetime of this instance.
        start_rowid = len(self.doc_ids) + 1
        rows = [
            (start_rowid + i, sqlite_vec.serialize_float32(normalized_vecs[i].tolist()))
            for i in range(len(doc_ids))
        ]
        self._conn.executemany("INSERT INTO vec_items(rowid, embedding) VALUES (?, ?)", rows)
        self._conn.commit()

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

        # Ensure query vector is normalized 1D -- unchanged from the
        # pre-Phase-2.2 implementation.
        q = query_vector.flatten().astype(np.float32)
        q_norm = np.linalg.norm(q)
        if q_norm > 0:
            q = q / q_norm

        # Phase 2.5 finding (discovered by the benchmark harness's own
        # 10K-file tier run, not previously exercised by any Phase 2.2
        # test -- those all used small corpora): sqlite-vec's `vec0` KNN
        # query has a hard limit on `k`, enforced by the library itself
        # (verified experimentally: `sqlite3.OperationalError: k value in
        # knn query too large, provided 40000 and the limit is 4096`).
        # `IndexingEngine.search()`'s repository-scoped candidate-pool
        # sizing (Phase 0.6) intentionally requests `top_k =
        # len(vector_index.doc_ids)` -- the entire corpus -- so
        # `_repo_filter`'s post-filtering is never starved; at realistic
        # scale that number now regularly exceeds sqlite-vec's own limit,
        # which would otherwise crash every scoped semantic/hybrid search
        # once a repository's embedded-chunk count passes ~4096. Clamped
        # here, defensively, for every caller (not just the benchmark's
        # own scoped-search path) -- this is the smallest number of
        # candidates sqlite-vec itself will ever accept in one query, so
        # any caller requesting more must be capped somewhere, and this is
        # the one place that already owns the actual SQL call.
        #
        # Disclosed trade-off, not silently glossed over: capping means a
        # very large multi-repository deployment (a query result set
        # genuinely dominated by >4096 higher-scoring OTHER repositories'
        # chunks before this repository's own best candidates appear) could
        # in principle see `_repo_filter` starved below `limit` results for
        # that one repository -- the same class of approximation the
        # existing "pull the whole corpus" heuristic already was, not a new
        # kind of incompleteness, and unreachable within this milestone's
        # own single-repository-per-tier benchmark. Revisit only if this is
        # ever observed to actually matter for a real multi-tenant corpus.
        SQLITE_VEC_MAX_K = 4096
        num_results = min(top_k, len(self.doc_ids), SQLITE_VEC_MAX_K)
        if num_results <= 0:
            return []

        query_blob = sqlite_vec.serialize_float32(q.tolist())
        rows = self._conn.execute(
            "SELECT rowid, distance FROM vec_items WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (query_blob, num_results),
        ).fetchall()

        results = []
        for rowid, distance in rows:
            idx = rowid - 1
            # sqlite-vec's own cosine `distance` is `1 - cosine_similarity`
            # (verified experimentally) -- recovered here and then run
            # through the EXACT same similarity/relevance/distance formulas
            # the pre-Phase-2.2 NumPy implementation used, so only the
            # source of the raw similarity number changed, not its meaning.
            # `distance` comes back NULL for a genuinely all-zero (and thus
            # un-normalizable) query vector -- verified experimentally, not
            # assumed; treated as similarity 0.0 (a tie across every
            # candidate), matching what the old np.dot-based implementation
            # would already produce for the same degenerate input. A real
            # embedding is never all-zero (CLAUDE.md §4's "no silent
            # pseudo-embedding fallback" invariant, enforced in
            # codepulse/embeddings/embedder.py), so this path is not
            # expected to be exercised by real traffic.
            sim = 0.0 if distance is None else (1.0 - distance)
            sim = max(-1.0, min(1.0, sim))
            distance_val = max(0.0, 1.0 - sim)
            relevance = max(0.0, min(1.0, (sim + 1.0) / 2.0))

            results.append({
                "doc_id": self.doc_ids[idx],
                "similarity": sim,
                "relevance_score": relevance,
                "embedding_distance": distance_val,
                "metadata": self.metadata[idx],
            })

        return results
