from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np

from codepulse.embeddings.chunker import CodeChunk, SemanticChunker
from codepulse.embeddings.embedder import CodeEmbedder, EmbeddingUnavailableError
from codepulse.indexer.ignore import RepositoryIgnoreRules
from codepulse.ingestion_safety import IngestionSafetyError
from codepulse.models import FileParseResult, Symbol, compute_content_hash
from codepulse.parser.factory import get_parser_for_file
from codepulse.parser.graph import CallGraphBuilder
from codepulse.search.bm25 import BM25Index
from codepulse.search.ranker import HybridRanker, SearchResultItem
from codepulse.search.vector import VectorIndex
from codepulse.storage.db import (
    DependencyModel,
    EmbeddingModel,
    FileModel,
    RepositoryModel,
    StorageManager,
    SymbolModel,
)

logger = logging.getLogger(__name__)

# Files/folders to ignore during indexing
IGNORE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".idea",
    ".vscode",
    "dist",
    "build",
    "target",
}
IGNORE_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dll",
    ".dylib",
    ".exe",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".lock",
    ".bin",
    ".db",
    ".sqlite",
    ".sqlite3",
}


class IndexingEngine:
    """
    Orchestrates repository indexing, AST parsing, call graph construction,
    embedding generation, BM25 indexing, and database storage.
    """

    def __init__(
        self,
        storage: StorageManager | None = None,
        embedder: CodeEmbedder | None = None,
        chunker: SemanticChunker | None = None,
    ) -> None:
        self.storage = storage or StorageManager()
        self.embedder = embedder or CodeEmbedder()
        self.chunker = chunker or SemanticChunker()

        # In-memory search engines (per repository / session)
        self.bm25_index = BM25Index()
        self.vector_index = VectorIndex(dimension=self.embedder.dimension)
        self.ranker = HybridRanker()
        # `call_graph` mirrors the most recently indexed repository's graph
        # (pre-Phase-0.6 behavior, preserved for existing callers -- cli.py's
        # cmd_impact and api/app.py's analyze_impact are single/last-repo-
        # oriented and out of this milestone's scope). `call_graphs` is the
        # Phase 0.6 addition: one CallGraphBuilder per repository, keyed by
        # RepositoryModel.id, so multiple indexed repositories' graphs can
        # coexist without one overwriting another -- needed so hybrid
        # search's graph-derived score can be correctly repository-scoped.
        self.call_graph: CallGraphBuilder | None = None
        self.call_graphs: dict[int, CallGraphBuilder] = {}
        self.parse_results: dict[str, FileParseResult] = {}
        self.symbols_by_id: dict[str, Symbol] = {}
        # Phase 0.6: doc_id -> owning RepositoryModel.id, built alongside
        # symbols_by_id, so hybrid search's graph-score step can skip
        # symbols belonging to a different repository than the one a scoped
        # search is asking about.
        self.symbol_repo_map: dict[str, int] = {}

        # Phase 0.5: load-on-startup hydration. Restores the in-memory
        # vector index from durably persisted embeddings so semantic search
        # is functional immediately after a fresh process/engine starts,
        # without requiring a full re-index. This only reads already-computed
        # vectors from the database -- it never calls the embedding model,
        # so it's cheap even before any model weights are loaded. A failure
        # here (e.g. a corrupt row) is logged and does not prevent the
        # engine from starting; it simply leaves that data un-hydrated.
        try:
            self.hydrate_all()
        except Exception as e:
            logger.warning("Startup embedding hydration failed (continuing without it): %s", e)

    def _resolve_repo_id(self, repo_name: str, repo_url: str) -> int:
        """
        Phase 0.6: resolves (or creates) the RepositoryModel row for
        `repo_url` and returns its id, BEFORE parsing/chunking/embedding
        begins. Needed so in-memory BM25/vector documents can be tagged with
        the correct, real `repo_id` at the moment they're added -- reusing
        the exact natural key (`url`) and upsert semantics established in
        Phase 0.3, not a second identity system.

        `_persist_to_db` performs its own (idempotent) upsert of the same
        row later with final file/symbol counts -- that's unaffected by
        this early lookup, which only needs the stable id and creates the
        row with zero counts if it doesn't exist yet (harmless: a repo row
        with file_count=0 momentarily, corrected the moment `_persist_to_db`
        runs later in the same call).
        """
        session = self.storage.get_session()
        try:
            repo_record = session.query(RepositoryModel).filter(RepositoryModel.url == repo_url).first()
            if repo_record is None:
                repo_record = RepositoryModel(name=repo_name, url=repo_url, file_count=0, symbol_count=0)
                session.add(repo_record)
                session.commit()
            return repo_record.id
        finally:
            session.close()

    def index_directory(
        self,
        repo_path: str,
        repo_name: str = "local_repo",
        repo_url: str = "",
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, Any]:
        """
        Indexes all parseable files within a repository directory.
        """
        repo_path = os.path.abspath(repo_path)
        resolved_repo_url = repo_url or repo_path

        # Phase 0.6: resolve the real repo_id BEFORE parsing/chunking, so
        # every in-memory BM25/vector document created below can be tagged
        # with it (required for repository-scoped search -- see
        # _resolve_repo_id's docstring for why this can't wait until the
        # final DB persist step, which happens after these in-memory
        # structures are already populated).
        repo_id = self._resolve_repo_id(repo_name, resolved_repo_url)

        # Phase 0.8: load this repository's .gitignore/.codepulseignore
        # rules ONCE, before walking the tree, and reuse them for every
        # discovered path -- see codepulse.indexer.ignore for the exact
        # semantics (additive combination, root-only, malformed-file
        # handling). This is layered on top of, not a replacement for, the
        # existing hardcoded IGNORE_DIRS/IGNORE_EXTENSIONS filtering below.
        ignore_rules = RepositoryIgnoreRules(repo_path)

        all_files: list[str] = []

        # 1. Discover all source code files
        for root, dirs, files in os.walk(repo_path):
            kept_dirs = []
            for d in dirs:
                if d in IGNORE_DIRS:
                    continue
                rel_dir = os.path.relpath(os.path.join(root, d), repo_path).replace("\\", "/")
                if ignore_rules.is_ignored(rel_dir, is_dir=True):
                    # Prune here (not just skip later) so nothing beneath
                    # an ignored directory is walked, read, or parsed at all.
                    continue
                kept_dirs.append(d)
            dirs[:] = kept_dirs

            for f in files:
                _, ext = os.path.splitext(f)
                if ext.lower() in IGNORE_EXTENSIONS:
                    continue
                full_path = os.path.join(root, f)
                rel_path = os.path.relpath(full_path, repo_path).replace("\\", "/")
                if ignore_rules.is_ignored(rel_path, is_dir=False):
                    continue
                all_files.append(full_path)

        total_files = len(all_files)
        logger.info("Found %d files to examine in '%s'", total_files, repo_path)

        # Phase 1.5 / roadmap §15: "max files per repo" resource limit --
        # checked here (after discovery, before parsing/embedding, the
        # expensive part) rather than via a second pre-flight walk at the
        # API boundary, to avoid duplicating this exact discovery loop
        # (see codepulse/ingestion_safety.py's module docstring for why).
        # Raising here surfaces as a real, non-fabricated "failed" job
        # status (codepulse.jobs.mark_failed, called from the caller's own
        # except block) rather than a fabricated success.
        from codepulse.config import get_settings

        _ingestion_settings = get_settings()
        max_files = _ingestion_settings.ingestion_max_files_per_repo
        max_file_size = _ingestion_settings.ingestion_max_file_size_bytes
        if total_files > max_files:
            raise IngestionSafetyError(
                f"Repository at '{repo_path}' has {total_files} discoverable "
                f"files, exceeding the configured ingestion_max_files_per_repo "
                f"limit of {max_files}"
            )

        parsed_files: list[FileParseResult] = []
        all_symbols: list[Symbol] = []
        all_chunks: list[CodeChunk] = []
        # Maps id(chunk) -> the Symbol object it was generated from. Needed
        # because a chunk's own start_line/end_line describe the chunk's
        # line range (which differs from the symbol's for body_chunk pieces),
        # not the symbol's identity -- this is how embeddings get correctly
        # linked back to a specific Symbol row during persistence (Phase 0.5).
        chunk_symbol_map: dict[int, Symbol] = {}

        # 2. Parse files with AST parsers
        for idx, file_path in enumerate(all_files, start=1):
            rel_path = os.path.relpath(file_path, repo_path).replace("\\", "/")
            if progress_callback:
                progress_callback(rel_path, idx, total_files)

            parser = get_parser_for_file(file_path)
            if not parser:
                continue

            # Phase 1.5 / roadmap §15: "max file size parsed" resource
            # limit. A single oversized file is skipped (logged, not
            # fatal to the run) -- the same per-file error-tolerance
            # already established for malformed/unparseable files just
            # below, not a new failure mode.
            try:
                file_size = os.path.getsize(file_path)
            except OSError:
                file_size = 0
            if file_size > max_file_size:
                logger.warning(
                    "Skipping oversized file '%s' (%d bytes > limit %d)",
                    rel_path,
                    file_size,
                    max_file_size,
                )
                continue

            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
                    source_code = fh.read()

                parse_res = parser.parse_file(rel_path, source_code)
                self.parse_results[rel_path] = parse_res
                parsed_files.append(parse_res)

                for sym in parse_res.symbols:
                    all_symbols.append(sym)
                    # Generate semantic chunks
                    chunks = self.chunker.chunk_symbol(sym)
                    for c in chunks:
                        chunk_symbol_map[id(c)] = sym
                    all_chunks.extend(chunks)

            except Exception as e:
                logger.warning("Error parsing file '%s': %s", rel_path, e)

        logger.info(
            "Parsed %d files. Extracted %d symbols and %d semantic chunks.",
            len(parsed_files),
            len(all_symbols),
            len(all_chunks),
        )

        # 3. Build Call Graph
        # Phase 0.6: stored per-repo (call_graphs) so a previously indexed
        # repository's graph isn't discarded/overwritten by this run --
        # `call_graph` is kept in sync as a "most recently indexed" alias
        # for existing callers that are out of this milestone's scope.
        repo_call_graph = CallGraphBuilder(parsed_files)
        self.call_graphs[repo_id] = repo_call_graph
        self.call_graph = repo_call_graph

        # 4. Generate Embeddings & Populate Vector Index
        #
        # Permanent invariant: NO silent pseudo-embedding fallback. If the
        # configured embedding model cannot be loaded or fails to encode,
        # CodeEmbedder.encode() raises EmbeddingUnavailableError -- caught
        # here explicitly. Symbol/File persistence still proceeds (lexical
        # search remains available, per roadmap §17's "degraded mode"), but
        # no embedding is generated, persisted, or added to the vector index
        # for this run, and the failure is reported in the returned stats
        # rather than silently reported as success.
        chunk_embeddings: list[tuple[Symbol, CodeChunk, np.ndarray]] = []
        embedding_error: str | None = None

        if all_chunks:
            chunk_texts = [c.content for c in all_chunks]
            try:
                embeddings = self.embedder.encode(chunk_texts)
            except EmbeddingUnavailableError as e:
                embedding_error = str(e)
                logger.error(
                    "Embedding generation failed for '%s' -- proceeding in "
                    "lexical-only degraded mode, no embeddings persisted this run: %s",
                    repo_path,
                    e,
                )
                embeddings = None

            if embeddings is not None:
                doc_ids = []
                metadatas = []
                for idx, c in enumerate(all_chunks):
                    # Phase 0.6: doc_id is repo-qualified and metadata carries
                    # an explicit repo_id -- without this, two repositories
                    # indexing files at the same relative path (a realistic,
                    # explicitly-tested scenario) would collide in this
                    # shared in-memory vector index instead of staying
                    # isolated.
                    doc_id = f"{repo_id}::{c.file_path}::{c.symbol_name}::{c.chunk_index}"
                    doc_ids.append(doc_id)
                    metadatas.append({
                        "repo_id": repo_id,
                        "symbol_name": c.symbol_name,
                        "file_path": c.file_path,
                        "chunk_type": c.chunk_type,
                        "start_line": c.start_line,
                        "end_line": c.end_line,
                        "snippet": c.content[:200],
                    })
                    chunk_embeddings.append((chunk_symbol_map[id(c)], c, embeddings[idx]))

                self.vector_index.add_batch(doc_ids, embeddings, metadatas)

        # 5. Populate BM25 Index
        # Phase 0.6: doc_id is repo-qualified (same collision reasoning as
        # the vector index above) and repo_id is threaded into the BM25
        # metadata and symbol_repo_map so repository-scoped search can
        # filter candidates without a second identity system.
        for sym in all_symbols:
            doc_id = f"{repo_id}::{sym.file_path}::{sym.name}"
            self.symbols_by_id[doc_id] = sym
            self.symbol_repo_map[doc_id] = repo_id
            self.bm25_index.add_symbol(doc_id, sym, repo_id=repo_id)

        self.bm25_index.build()

        # 6. Save to Database
        stats = self._persist_to_db(
            repo_name=repo_name,
            repo_url=resolved_repo_url,
            parsed_files=parsed_files,
            all_symbols=all_symbols,
            all_chunks=all_chunks,
            chunk_embeddings=chunk_embeddings,
        )

        return {
            "repository_name": repo_name,
            "repository_id": repo_id,
            "total_files": len(parsed_files),
            "total_symbols": len(all_symbols),
            "total_chunks": len(all_chunks),
            "call_graph_edges": len(repo_call_graph.edges),
            "embedding_status": "failed" if embedding_error else ("skipped_no_chunks" if not all_chunks else "success"),
            "embedding_error": embedding_error,
            "degraded_mode": embedding_error is not None,
            "database_stats": stats,
        }

    def _persist_to_db(
        self,
        repo_name: str,
        repo_url: str,
        parsed_files: list[FileParseResult],
        all_symbols: list[Symbol],
        all_chunks: list[CodeChunk],
        chunk_embeddings: list[tuple[Symbol, CodeChunk, np.ndarray]] | None = None,
    ) -> dict[str, int]:
        """
        Persists repository hierarchy to the database.

        Phase 0.3 behavior (Repository/File upsert with content_hash):
          - Repository rows are upserted by natural key `url` -- the same
            value used consistently across calls to `index_directory` (an
            explicit git/repo URL, or the absolute local repo path when none
            is given). Re-indexing the same repository updates the existing
            row instead of inserting a duplicate one.
          - File rows are upserted by natural key `(repo_id, path)`:
              * new path            -> insert a new File row.
              * existing path, same content_hash    -> reuse the row as-is
                (only `last_indexed` is refreshed); no new row is created.
              * existing path, different content_hash -> update the existing
                row in place (no new row is created).
            Both natural keys are also enforced with a UNIQUE constraint at
            the database level (see storage/db.py), so this invariant does
            not depend on application logic alone.
        Phase 0.4 behavior (Symbol upsert by (file_id, name, start_line)):
          - Symbol rows are upserted by natural key `(file_id, name,
            start_line)`, using `body_hash` for change detection:
              * new (file_id, name, start_line)     -> insert a new row.
              * existing key, same body_hash         -> reuse the row
                untouched (zero writes -- Symbol has no freshness/timestamp
                field to refresh, unlike File).
              * existing key, different body_hash    -> update the existing
                row in place (no new row is created).
            Enforced with a UNIQUE constraint at the database level (see
            storage/db.py), not only in application logic.
          - Dependency (call graph edge) rows are NOT upserted in this
            milestone -- that is Phase 3 (call-graph confidence) scope.
            Re-indexing still appends new Dependency rows every time. This
            is a known, intentional limitation of Phase 0.4.
        Phase 0.5 behavior (Embedding persistence + hydration):
          - Embedding rows are upserted by natural key `(symbol_id,
            chunk_type, chunk_index)`, using a `content_hash` of the chunk's
            own text for change detection:
              * new (symbol_id, chunk_type, chunk_index) -> insert a new row.
              * existing key, same content_hash          -> reuse the row
                untouched; no new row is created.
              * existing key, different content_hash     -> update the
                existing row in place (no new row is created).
            Enforced with a UNIQUE constraint at the database level (see
            storage/db.py), not only in application logic. `chunk_embeddings`
            is empty whenever embedding generation failed or produced no
            chunks this run -- in that case no Embedding rows are touched at
            all (existing durable embeddings are left exactly as they were;
            nothing is deleted just because this run couldn't re-embed).
          - A chunk whose parent symbol no longer produces a chunk at that
            (chunk_type, chunk_index) after a content change (e.g. a large
            function shrinks below the multi-chunk threshold) can leave a
            stale Embedding row behind -- this mirrors Phase 0.4's decision
            not to delete stale Symbol rows, and is intentionally out of
            scope here too (real stale-data cleanup is Phase 3, per the
            roadmap's `ON DELETE CASCADE` design for whole-symbol removal).
        """
        session = self.storage.get_session()
        try:
            now = datetime.utcnow()

            # --- Repository upsert (natural key: url) ---
            repo_record = (
                session.query(RepositoryModel)
                .filter(RepositoryModel.url == repo_url)
                .first()
            )
            if repo_record is None:
                repo_record = RepositoryModel(
                    name=repo_name,
                    url=repo_url,
                    file_count=len(parsed_files),
                    symbol_count=len(all_symbols),
                    last_sync=now,
                )
                session.add(repo_record)
            else:
                repo_record.name = repo_name
                repo_record.file_count = len(parsed_files)
                repo_record.symbol_count = len(all_symbols)
                repo_record.last_sync = now

            session.flush()

            # --- File upsert (natural key: (repo_id, path)) ---
            file_record_map: dict[str, FileModel] = {}
            new_files = 0
            updated_files = 0
            unchanged_files = 0

            for pf in parsed_files:
                existing_fm = (
                    session.query(FileModel)
                    .filter(
                        FileModel.repo_id == repo_record.id,
                        FileModel.path == pf.file_path,
                    )
                    .first()
                )

                if existing_fm is None:
                    fm = FileModel(
                        repo_id=repo_record.id,
                        path=pf.file_path,
                        language=pf.language,
                        size_bytes=pf.file_size_bytes,
                        line_count=pf.line_count,
                        content_hash=pf.content_hash,
                        last_indexed=now,
                    )
                    session.add(fm)
                    file_record_map[pf.file_path] = fm
                    new_files += 1
                elif existing_fm.content_hash != pf.content_hash:
                    existing_fm.language = pf.language
                    existing_fm.size_bytes = pf.file_size_bytes
                    existing_fm.line_count = pf.line_count
                    existing_fm.content_hash = pf.content_hash
                    existing_fm.last_indexed = now
                    file_record_map[pf.file_path] = existing_fm
                    updated_files += 1
                else:
                    # Content unchanged: reuse the existing row untouched aside
                    # from a freshness timestamp -- do not insert another row.
                    existing_fm.last_indexed = now
                    file_record_map[pf.file_path] = existing_fm
                    unchanged_files += 1

            session.flush()

            # --- Symbol upsert (natural key: (file_id, name, start_line)) ---
            symbol_record_map: dict[tuple[str, str, int], SymbolModel] = {}
            new_symbols = 0
            updated_symbols = 0
            unchanged_symbols = 0

            for sym in all_symbols:
                fm = file_record_map.get(sym.file_path)
                if not fm:
                    continue

                sym_type_value = sym.type.value if hasattr(sym.type, "value") else str(sym.type)
                map_key = (sym.file_path, sym.name, sym.start_line)

                existing_sm = (
                    session.query(SymbolModel)
                    .filter(
                        SymbolModel.file_id == fm.id,
                        SymbolModel.name == sym.name,
                        SymbolModel.start_line == sym.start_line,
                    )
                    .first()
                )

                if existing_sm is None:
                    sm = SymbolModel(
                        file_id=fm.id,
                        name=sym.name,
                        type=sym_type_value,
                        start_line=sym.start_line,
                        end_line=sym.end_line,
                        signature=sym.signature,
                        docstring=sym.docstring,
                        body=sym.body,
                        body_hash=sym.body_hash,
                        language=sym.language,
                        complexity=sym.complexity,
                    )
                    session.add(sm)
                    symbol_record_map[map_key] = sm
                    new_symbols += 1
                elif existing_sm.body_hash != sym.body_hash:
                    existing_sm.type = sym_type_value
                    existing_sm.end_line = sym.end_line
                    existing_sm.signature = sym.signature
                    existing_sm.docstring = sym.docstring
                    existing_sm.body = sym.body
                    existing_sm.body_hash = sym.body_hash
                    existing_sm.language = sym.language
                    existing_sm.complexity = sym.complexity
                    symbol_record_map[map_key] = existing_sm
                    updated_symbols += 1
                else:
                    # Content unchanged: reuse the existing row untouched --
                    # zero net writes, per the roadmap's Phase 0 idempotency rule.
                    symbol_record_map[map_key] = existing_sm
                    unchanged_symbols += 1

            session.flush()

            # --- Embedding upsert (natural key: (symbol_id, chunk_type, chunk_index)) ---
            new_embeddings = 0
            updated_embeddings = 0
            unchanged_embeddings = 0

            for sym, chunk, vector in (chunk_embeddings or []):
                map_key = (sym.file_path, sym.name, sym.start_line)
                sm = symbol_record_map.get(map_key)
                if not sm:
                    # The symbol this chunk belongs to wasn't persisted this
                    # run (shouldn't normally happen -- every symbol that
                    # produces chunks also goes through the upsert above).
                    continue

                chash = compute_content_hash(chunk.content)
                embedding_json = json.dumps(np.asarray(vector, dtype=np.float64).tolist())

                existing_em = (
                    session.query(EmbeddingModel)
                    .filter(
                        EmbeddingModel.symbol_id == sm.id,
                        EmbeddingModel.chunk_type == chunk.chunk_type,
                        EmbeddingModel.chunk_index == chunk.chunk_index,
                    )
                    .first()
                )

                if existing_em is None:
                    em = EmbeddingModel(
                        symbol_id=sm.id,
                        chunk_type=chunk.chunk_type,
                        chunk_index=chunk.chunk_index,
                        embedding_json=embedding_json,
                        chunk_content=chunk.content,
                        content_hash=chash,
                        embedding_model=self.embedder.model_name,
                        embedding_dim=int(np.asarray(vector).shape[-1]),
                    )
                    session.add(em)
                    new_embeddings += 1
                elif existing_em.content_hash != chash:
                    existing_em.embedding_json = embedding_json
                    existing_em.chunk_content = chunk.content
                    existing_em.content_hash = chash
                    existing_em.embedding_model = self.embedder.model_name
                    existing_em.embedding_dim = int(np.asarray(vector).shape[-1])
                    updated_embeddings += 1
                else:
                    # Content unchanged: reuse the existing row untouched --
                    # zero net writes, per the roadmap's Phase 0 idempotency rule.
                    unchanged_embeddings += 1

            session.flush()

            # Persist call graph edges (unchanged; not upserted -- Phase 3 scope)
            if self.call_graph:
                for edge in self.call_graph.edges:
                    dm = DependencyModel(
                        source_symbol_name=edge.source_symbol_name,
                        source_file=edge.source_file,
                        target_symbol_name=edge.target_symbol_name,
                        target_file=edge.target_file,
                        call_count=edge.call_count,
                    )
                    session.add(dm)

            session.commit()
            return {
                "repo_id": repo_record.id,
                "saved_files": len(file_record_map),
                "new_files": new_files,
                "updated_files": updated_files,
                "unchanged_files": unchanged_files,
                "saved_symbols": len(symbol_record_map),
                "new_symbols": new_symbols,
                "updated_symbols": updated_symbols,
                "unchanged_symbols": unchanged_symbols,
                "new_embeddings": new_embeddings,
                "updated_embeddings": updated_embeddings,
                "unchanged_embeddings": unchanged_embeddings,
            }
        except Exception as e:
            session.rollback()
            logger.error("Database save failed: %s", e)
            return {"error": str(e)}
        finally:
            session.close()

    def hydrate_repository(self, repo_url: str) -> dict[str, Any]:
        """
        Phase 0.5 -- load-on-startup hydration.

        Restores the in-memory `vector_index` for ONE repository from
        durably persisted `code_embeddings` rows, without re-parsing or
        re-embedding anything. This is what makes semantic search survive a
        process restart: a fresh `IndexingEngine` can call this to become
        functional again purely from the database, no embedding-model call
        required.

        Only semantic retrieval is restored this way. Keyword/hybrid search
        and the call graph (`bm25_index`, `symbols_by_id`, `call_graph`) are
        not durable and still require a fresh `index_directory()` call --
        `BM25Index`/`CallGraphBuilder` have no backing DB table; persisting
        them is out of Phase 0.5's scope (that's Phase 2/Phase 3 territory).

        Repository scoping: only embeddings that belong to Symbols whose
        File belongs to the given `repo_url` are loaded, so embeddings from
        one repository can never be hydrated into another's results. Each
        loaded vector's metadata is also tagged with `repo_id` (the same
        `RepositoryModel.id` used everywhere else), which is what lets
        `search(..., repository_id=...)` (Phase 0.6) filter hydrated
        embeddings correctly even though `hydrate_all()` loads every
        repository into the same shared `vector_index`.

        Vector-dimension safety: a persisted row whose `embedding_dim`
        doesn't match the currently configured `vector_index.dimension` is
        skipped explicitly (e.g. the embedding model or dimension was
        changed since that row was written) rather than being loaded and
        silently corrupting similarity results.
        """
        session = self.storage.get_session()
        try:
            repo_record = (
                session.query(RepositoryModel).filter(RepositoryModel.url == repo_url).first()
            )
            if repo_record is None:
                return {"repo_id": None, "hydrated_embeddings": 0, "skipped_dimension_mismatch": 0}

            rows = (
                session.query(EmbeddingModel, SymbolModel, FileModel)
                .join(SymbolModel, EmbeddingModel.symbol_id == SymbolModel.id)
                .join(FileModel, SymbolModel.file_id == FileModel.id)
                .filter(FileModel.repo_id == repo_record.id)
                .all()
            )

            doc_ids: list[str] = []
            vectors: list[np.ndarray] = []
            metadatas: list[dict[str, Any]] = []
            skipped_dimension_mismatch = 0

            for em, sm, fm in rows:
                if em.embedding_dim is not None and em.embedding_dim != self.vector_index.dimension:
                    skipped_dimension_mismatch += 1
                    continue
                try:
                    raw = json.loads(em.embedding_json)
                except (TypeError, ValueError) as e:
                    logger.warning(
                        "Skipping unreadable embedding row id=%s during hydration: %s", em.id, e
                    )
                    continue
                vec = np.asarray(raw, dtype=np.float32)
                if vec.shape[-1] != self.vector_index.dimension:
                    skipped_dimension_mismatch += 1
                    continue

                # Phase 0.6: repo-qualified doc_id + explicit repo_id in
                # metadata, matching the scheme used at index time -- see
                # index_directory's comment on why this prefix is needed to
                # avoid collisions between repositories sharing a path.
                doc_id = f"{repo_record.id}::{fm.path}::{sm.name}::{em.chunk_index}"
                doc_ids.append(doc_id)
                vectors.append(vec)
                metadatas.append({
                    "repo_id": repo_record.id,
                    "symbol_name": sm.name,
                    "file_path": fm.path,
                    "chunk_type": em.chunk_type,
                    "start_line": sm.start_line,
                    "end_line": sm.end_line,
                    "snippet": (em.chunk_content or "")[:200],
                })

            if vectors:
                self.vector_index.add_batch(doc_ids, np.vstack(vectors), metadatas)

            return {
                "repo_id": repo_record.id,
                "hydrated_embeddings": len(doc_ids),
                "skipped_dimension_mismatch": skipped_dimension_mismatch,
            }
        finally:
            session.close()

    def hydrate_all(self) -> dict[str, Any]:
        """
        Phase 0.5 -- load-on-startup hydration for every repository in the
        database. Called automatically once from `__init__` so a freshly
        constructed engine (e.g. a new server process) has semantic search
        available immediately from durable storage, without re-indexing.
        See `hydrate_repository` for the per-repository details.
        """
        session = self.storage.get_session()
        try:
            repo_urls = [r.url for r in session.query(RepositoryModel.url).all()]
        finally:
            session.close()

        total_hydrated = 0
        total_skipped = 0
        for url in repo_urls:
            result = self.hydrate_repository(url)
            total_hydrated += result["hydrated_embeddings"]
            total_skipped += result["skipped_dimension_mismatch"]

        if total_hydrated or total_skipped:
            logger.info(
                "Startup hydration: %d embeddings loaded across %d repositories (%d skipped: dimension mismatch)",
                total_hydrated,
                len(repo_urls),
                total_skipped,
            )

        return {
            "repositories": len(repo_urls),
            "hydrated_embeddings": total_hydrated,
            "skipped_dimension_mismatch": total_skipped,
        }

    def search(
        self,
        query: str,
        limit: int = 20,
        search_type: str = "hybrid",  # "keyword", "semantic", "hybrid"
        repository_id: int | None = None,
    ) -> list[SearchResultItem]:
        """
        Executes search across the indexed repository/repositories.

        Phase 0.6 -- repository-scoped search: when `repository_id` is
        given, every candidate-generation step (BM25, vector, and the
        call-graph-derived hybrid signal) is filtered to documents/symbols
        tagged with that exact `repo_id` (the same `RepositoryModel.id`
        used throughout the data model -- not a second identity system).
        A search scoped to repository A can never return a result whose
        ownership chain (repository -> file -> symbol -> chunk/embedding)
        traces to a different repository, regardless of how well that
        other repository's content happens to score against the query.

        This filtering is enforced HERE, inside the search implementation
        itself -- not left to a caller (REST/CLI/MCP) to post-filter -- and
        happens BEFORE the result set is truncated to `limit`, so a
        well-populated target repository is never starved of results just
        because another repository's documents also scored highly.

        When `repository_id` is None, behavior is byte-for-byte identical
        to pre-Phase-0.6 search: fully global/unscoped across every
        indexed/hydrated repository, exactly as before this milestone.
        """

        def _repo_filter(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
            if repository_id is None:
                return results
            return [r for r in results if r.get("metadata", {}).get("repo_id") == repository_id]

        if search_type == "keyword":
            # Scoped: pull every candidate so filtering can't starve the
            # result set below `limit`; the ranker still truncates to
            # `limit` at the end. Unscoped: unchanged (`top_k=limit`).
            bm25_pool = self.bm25_index.num_docs if repository_id is not None else limit
            bm25_res = _repo_filter(self.bm25_index.search(query, top_k=bm25_pool))
            return self.ranker.fuse(query, bm25_results=bm25_res, vector_results=[], top_k=limit)

        elif search_type == "semantic":
            vector_pool = len(self.vector_index.doc_ids) if repository_id is not None else limit
            query_vec = self.embedder.encode(query)
            vector_res = _repo_filter(self.vector_index.search(query_vec, top_k=vector_pool))
            return self.ranker.fuse(query, bm25_results=[], vector_results=vector_res, top_k=limit)

        else:  # hybrid
            bm25_pool = self.bm25_index.num_docs if repository_id is not None else limit * 2
            vector_pool = len(self.vector_index.doc_ids) if repository_id is not None else limit * 2

            bm25_res = _repo_filter(self.bm25_index.search(query, top_k=bm25_pool))
            query_vec = self.embedder.encode(query)
            vector_res = _repo_filter(self.vector_index.search(query_vec, top_k=vector_pool))

            # Compute graph centrality / importance scores, scoped to the
            # requested repository's own call graph (self.call_graphs) so a
            # symbol's caller count from a DIFFERENT repository's graph can
            # never leak into this repository's score.
            graph_scores: dict[str, float] = {}
            graph = self.call_graphs.get(repository_id) if repository_id is not None else self.call_graph
            if graph:
                for doc_id, sym in self.symbols_by_id.items():
                    if repository_id is not None and self.symbol_repo_map.get(doc_id) != repository_id:
                        continue
                    callers = graph.reverse_adjacency.get((sym.file_path, sym.name), set())
                    # Normalized graph score (more callers = more important)
                    graph_scores[doc_id] = min(1.0, len(callers) / 5.0)

            return self.ranker.fuse(
                query,
                bm25_results=bm25_res,
                vector_results=vector_res,
                graph_scores=graph_scores,
                top_k=limit,
            )
