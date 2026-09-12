from __future__ import annotations

from typing import Any, Sequence

import tantivy

from codepulse.models import Symbol
from codepulse.search.tokenizer import tokenize_code


def _build_schema() -> "tantivy.Schema":
    """
    Phase 2.1 -- the Tantivy schema backing `BM25Index`.

    Fields, and why each is shaped the way it is (verified against the
    actually-installed `tantivy` 0.22.2 Python binding -- see
    CODEPULSE_PROGRESS.md's Phase 2.1 section for the exact APIs checked):

    - `doc_id`: the CodePulse-level identity string (e.g.
      "{repo_id}::{file_path}::{name}"), stored so it can be read back on
      every hit. `tokenizer_name="raw"` means the whole string is one exact
      token -- this field is never searched against, only ever stored/
      retrieved and, for `add_document`'s direct callers, exact-matched.
    - `repo_id`: `indexed=True` (required for `IndexWriter.delete_documents`,
      which deletes by exact term match on an indexed field) and
      `fast=True` (columnar storage, cheap random access) -- this is the
      field Phase 2.1's repository-replacement semantics deletes by (see
      `remove_repo` below). `stored=True` so it round-trips onto every hit
      without a second lookup. May be absent on a document added with
      `repo_id=None` (direct `BM25Index` usage outside `IndexingEngine`,
      e.g. `tests/test_bm25_vector.py`) -- verified experimentally that an
      absent field reads back as `None` via `Document.get_first`, matching
      the pre-Tantivy dict-based `metadata.get("repo_id")` behavior exactly.
    - `body`: the single combined searchable text (see `add_symbol`'s own
      docstring for its exact construction, unchanged from the pre-Tantivy
      implementation). Uses the built-in `tokenizer_name="whitespace"` --
      the text is pre-tokenized through this project's own
      `codepulse.search.tokenizer.tokenize_code` (the same code-aware
      snake_case/camelCase splitter the old in-memory index used) and
      joined with spaces BEFORE being handed to Tantivy, so indexing and
      querying both go through the identical, already-tested tokenizer;
      Tantivy's own "default" tokenizer is deliberately NOT used here, since
      it doesn't split camelCase identifiers the way `tokenize_code` does,
      and reusing a "raw"/"whitespace" field is the standard way to feed a
      pre-tokenized stream into Tantivy without it re-processing the terms.
      Not stored -- nothing reads this field back, only the parsed
      `metadata` JSON blob (below) is used to reconstruct a result.
    - `metadata`: a JSON field, stored, carrying the exact metadata dict
      `add_symbol`/`add_document` were given (including its own
      `repo_id` key) -- read back verbatim via `Document.get_first`
      (verified experimentally that a Python dict passed to
      `Document.add_json` round-trips as a real `dict`, not a JSON string).
    """
    builder = tantivy.SchemaBuilder()
    builder.add_text_field("doc_id", stored=True, tokenizer_name="raw")
    builder.add_integer_field("repo_id", stored=True, indexed=True, fast=True)
    builder.add_text_field("body", stored=False, tokenizer_name="whitespace")
    builder.add_json_field("metadata", stored=True)
    return builder.build()


class BM25Index:
    """
    Embedded Tantivy-backed lexical index for codebase symbols (Phase 2.1).

    Replaces the pre-Phase-2.1 pure-Python, in-memory BM25Okapi
    implementation's *internals* with the embedded `tantivy` library
    (roadmap §36 Phase 2: "swap `BM25Index` for a Tantivy-backed embedded
    lexical index... without changing the ranking contract") while
    preserving, unchanged, every part of the class's external contract that
    `IndexingEngine`, `HybridRanker`, and the existing test suite depend on:

    - Constructor: `BM25Index(k1=None, b=None)`, falling back to
      `get_settings().bm25_k1`/`bm25_b` -- `.k1`/`.b` remain real,
      readable attributes (`tests/test_config.py`) even though Tantivy's
      own internal BM25 scorer does not expose a configurable k1/b through
      its Python binding (verified: no such parameter exists anywhere in
      `tantivy.Query`/`tantivy.Searcher`/`tantivy.Index`'s introspected
      method surface) -- Tantivy's fixed internal BM25 implementation is
      used as-is. This is a disclosed, expected candidate-generation
      difference from the old hand-rolled formula, not a defect: the
      roadmap's own words are "without changing the ranking contract," and
      the *contract* is the shape/meaning of what `HybridRanker` consumes
      (Section 5/9 of the Phase 2.1 architecture gate), not
      bit-for-bit-identical BM25 arithmetic.
    - `.doc_ids`: a real `list[str]` of every currently-indexed document id
      (`tests/test_ignore_discovery.py` reads this directly).
    - `.num_docs`: the current, live document count (post-deletion, not a
      monotonic counter) -- `tests/test_restart_idempotency.py` and
      `IndexingEngine.search()`'s scoped-search candidate-pool sizing both
      depend on this being accurate at any point in time, not just after a
      `build()` call.
    - `add_symbol(doc_id, symbol, extra_text="", repo_id=None)`,
      `add_document(doc_id, text, metadata=None)`, `build()`,
      `search(query, top_k=20)` -- identical signatures and return shapes.

    Two behaviors are new in Phase 2.1, both required by the Phase 2.1
    architecture gate's approved idempotency design and *not* available in
    the old parallel-list implementation:

    - `remove_repo(repo_id)`: deletes every currently-indexed document
      belonging to `repo_id`. `IndexingEngine.index_directory()` calls this
      once, immediately before (re-)adding a repository's freshly-parsed
      symbols, so repeated indexing replaces rather than accumulates a
      repository's lexical documents (see that method's own comment for
      the full reasoning; this was a real, pre-existing, untested gap in
      the old `BM25Index`, not something Phase 2.1 is required to fix by
      the roadmap's literal text, but is fixed here because Tantivy's
      `IndexWriter.delete_documents` makes it essentially free and it
      closes a genuine correctness gap with zero ranking-contract impact).
    - A single `IndexWriter` is created once, in `__init__`, and reused for
      every subsequent `add_symbol`/`add_document`/`remove_repo`/`build()`
      call for the lifetime of this `BM25Index` instance -- verified
      experimentally that a Tantivy `IndexWriter` is safe to reuse across
      many add/delete/commit cycles (not a one-shot, single-use object).

    Source of truth: per the Phase 2.1 architecture gate's Section 5, this
    Tantivy index is a DERIVED, REBUILDABLE search artifact, never the
    durable source of truth -- `SymbolModel`/`FileModel` rows remain
    authoritative, exactly as they already are for the pre-Tantivy
    implementation. The index lives entirely in memory
    (`tantivy.Index(schema)` with no path argument -- verified
    experimentally that this constructs an in-RAM index) and is NOT
    persisted to disk in this milestone; it does not survive process
    restart, matching the existing, explicitly-tested contract in
    `tests/test_restart_idempotency.py` ("BM25 index has no backing table
    and must not survive restart").
    """

    def __init__(self, k1: float | None = None, b: float | None = None) -> None:
        from codepulse.config import get_settings
        settings = get_settings()
        self.k1 = k1 if k1 is not None else settings.bm25_k1
        self.b = b if b is not None else settings.bm25_b

        self._schema = _build_schema()
        self._index = tantivy.Index(self._schema)
        # A single writer, created once and reused for this instance's
        # entire lifetime -- verified experimentally (see module docstring)
        # that this is safe across many add/delete/commit cycles, avoiding
        # the cost (and, per tantivy-py's own locking model, the risk) of
        # constructing a fresh writer on every `index_directory()` call.
        self._writer = self._index.writer(heap_size=50_000_000, num_threads=1)
        # A searcher reflecting the index's state as of the most recent
        # `build()` (or, before the first `build()`, the empty index) --
        # refreshed at the end of every `build()`. A `Searcher` is a frozen
        # snapshot in tantivy-py (verified experimentally: a `Searcher`
        # fetched before a commit does NOT see documents added by that
        # commit), so `search()` must never reuse a stale one.
        self._searcher = self._index.searcher()

        # Live bookkeeping mirroring the actual indexed document set --
        # needed because tantivy-py's `Document`/`DocAddress` have no
        # concept of "the CodePulse doc_id" as a first-class handle, and
        # because `.doc_ids`/`.num_docs` (an existing, tested external
        # contract -- see class docstring) must reflect the CURRENT
        # (post-deletion) state at any moment, not just immediately after a
        # `build()`.
        self.doc_ids: list[str] = []
        self._repo_by_doc_id: dict[str, int | None] = {}

    @property
    def num_docs(self) -> int:
        return len(self.doc_ids)

    def add_symbol(
        self,
        doc_id: str,
        symbol: Symbol,
        extra_text: str = "",
        repo_id: int | None = None,
    ) -> None:
        """
        Adds a Symbol to the lexical index.

        `repo_id` (Phase 0.6) is carried through into the document's
        metadata so repository-scoped search can filter candidates by
        owning repository without needing a second identity system --
        it's the same `RepositoryModel.id` already used elsewhere.
        Optional/None for backward compatibility with any caller that
        doesn't track a repository (e.g. tests exercising this index
        directly, unrelated to Phase 0.6).
        """
        # Combine name, docstring, signature, and body snippet -- unchanged
        # construction from the pre-Tantivy implementation (Phase 2.1 does
        # not redesign the search corpus).
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
        meta = metadata or {}
        repo_id = meta.get("repo_id")

        tokens = tokenize_code(text)

        doc = tantivy.Document()
        doc.add_text("doc_id", doc_id)
        if repo_id is not None:
            doc.add_integer("repo_id", repo_id)
        doc.add_text("body", " ".join(tokens))
        doc.add_json("metadata", meta)
        self._writer.add_document(doc)

        self.doc_ids.append(doc_id)
        self._repo_by_doc_id[doc_id] = repo_id

    def remove_repo(self, repo_id: int) -> None:
        """
        Deletes every currently-indexed lexical document belonging to
        `repo_id` (Phase 2.1 repository-replacement semantics -- see class
        docstring and `IndexingEngine.index_directory()`'s call site).
        Safe to call for a `repo_id` with no existing documents (verified
        experimentally: `IndexWriter.delete_documents` on a non-matching
        term is a harmless no-op), which covers both the first-ever index
        of a repository and an empty repository (roadmap Phase 2.1 test
        cases A and F).
        """
        self._writer.delete_documents("repo_id", repo_id)
        removed = {doc_id for doc_id, r in self._repo_by_doc_id.items() if r == repo_id}
        for doc_id in removed:
            del self._repo_by_doc_id[doc_id]
        if removed:
            self.doc_ids = [d for d in self.doc_ids if d not in removed]

    def build(self) -> None:
        """
        Commits every `add_document`/`remove_repo` call made since the
        last `build()`, and refreshes this index's searcher so `search()`
        immediately sees the committed state -- the same two-phase
        "accumulate, then finalize" shape the pre-Tantivy implementation's
        `build()` (IDF/avgdl computation) had, now mapped onto Tantivy's
        own commit/reload lifecycle (verified experimentally: a `Searcher`
        fetched before `commit()`+`reload()` does not see the new writes;
        one fetched after does).
        """
        self._writer.commit()
        self._index.reload()
        self._searcher = self._index.searcher()

    def search(self, query: str, top_k: int = 20) -> list[dict[str, Any]]:
        """
        Searches the index with the given query.
        Returns a sorted list of matched documents with normalized lexical
        relevance scores in [0, 1] -- same shape as the pre-Tantivy
        implementation: `{"doc_id", "raw_score", "normalized_score",
        "metadata"}` per result, sorted descending, truncated to `top_k`.
        """
        if self.num_docs == 0 or top_k < 1:
            return []

        query_tokens = tokenize_code(query)
        if not query_tokens:
            return []

        subqueries = [
            (tantivy.Occur.Should, tantivy.Query.term_query(self._schema, "body", token))
            for token in query_tokens
        ]
        tantivy_query = tantivy.Query.boolean_query(subqueries)
        search_result = self._searcher.search(tantivy_query, limit=top_k)

        hits = list(search_result.hits)
        # Preserve the exact pre-Tantivy normalization semantics: divide
        # every raw score in THIS result set by the highest raw score in
        # that same set (not a min-max/global normalization), with the
        # same zero-max safety fallback the old implementation used.
        max_score = max((score for score, _ in hits), default=0.0)
        if max_score <= 0:
            max_score = 1.0

        results: list[dict[str, Any]] = []
        for score, doc_address in hits:
            doc = self._searcher.doc(doc_address)
            normalized_score = min(1.0, score / max_score) if max_score > 0 else 0.0
            results.append({
                "doc_id": doc.get_first("doc_id"),
                "raw_score": float(score),
                "normalized_score": normalized_score,
                "metadata": doc.get_first("metadata") or {},
            })

        # Tantivy's own TopDocs collector already returns hits sorted
        # descending by score, truncated to `limit` -- this sort is a
        # cheap, defensive guarantee of that same ordering (matching the
        # pre-Tantivy implementation's own explicit sort), not a
        # correction of anything observed to be wrong.
        results.sort(key=lambda r: r["raw_score"], reverse=True)
        return results[:top_k]
