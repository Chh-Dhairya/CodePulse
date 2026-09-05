from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np

from codepulse.embeddings.chunker import CodeChunk, SemanticChunker
from codepulse.embeddings.embedder import CodeEmbedder
from codepulse.models import FileParseResult, Symbol
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
        self.call_graph: CallGraphBuilder | None = None
        self.parse_results: dict[str, FileParseResult] = {}
        self.symbols_by_id: dict[str, Symbol] = {}

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
        all_files: list[str] = []

        # 1. Discover all source code files
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            for f in files:
                _, ext = os.path.splitext(f)
                if ext.lower() in IGNORE_EXTENSIONS:
                    continue
                full_path = os.path.join(root, f)
                all_files.append(full_path)

        total_files = len(all_files)
        logger.info("Found %d files to examine in '%s'", total_files, repo_path)

        parsed_files: list[FileParseResult] = []
        all_symbols: list[Symbol] = []
        all_chunks: list[CodeChunk] = []

        # 2. Parse files with AST parsers
        for idx, file_path in enumerate(all_files, start=1):
            rel_path = os.path.relpath(file_path, repo_path).replace("\\", "/")
            if progress_callback:
                progress_callback(rel_path, idx, total_files)

            parser = get_parser_for_file(file_path)
            if not parser:
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
        self.call_graph = CallGraphBuilder(parsed_files)

        # 4. Generate Embeddings & Populate Vector Index
        if all_chunks:
            chunk_texts = [c.content for c in all_chunks]
            embeddings = self.embedder.encode(chunk_texts)

            doc_ids = []
            metadatas = []
            for idx, c in enumerate(all_chunks):
                doc_id = f"{c.file_path}::{c.symbol_name}::{c.chunk_index}"
                doc_ids.append(doc_id)
                metadatas.append({
                    "symbol_name": c.symbol_name,
                    "file_path": c.file_path,
                    "chunk_type": c.chunk_type,
                    "start_line": c.start_line,
                    "end_line": c.end_line,
                    "snippet": c.content[:200],
                })

            self.vector_index.add_batch(doc_ids, embeddings, metadatas)

        # 5. Populate BM25 Index
        for sym in all_symbols:
            doc_id = f"{sym.file_path}::{sym.name}"
            self.symbols_by_id[doc_id] = sym
            self.bm25_index.add_symbol(doc_id, sym)

        self.bm25_index.build()

        # 6. Save to Database
        stats = self._persist_to_db(
            repo_name=repo_name,
            repo_url=repo_url or repo_path,
            parsed_files=parsed_files,
            all_symbols=all_symbols,
            all_chunks=all_chunks,
        )

        return {
            "repository_name": repo_name,
            "total_files": len(parsed_files),
            "total_symbols": len(all_symbols),
            "total_chunks": len(all_chunks),
            "call_graph_edges": len(self.call_graph.edges),
            "database_stats": stats,
        }

    def _persist_to_db(
        self,
        repo_name: str,
        repo_url: str,
        parsed_files: list[FileParseResult],
        all_symbols: list[Symbol],
        all_chunks: list[CodeChunk],
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
          - Symbol and Dependency rows are NOT upserted in this milestone --
            that is Phase 0.4 (symbol upsert) / Phase 3 (call-graph
            confidence) scope. Re-indexing still appends new Symbol and
            Dependency rows every time. This is a known, intentional
            limitation of Phase 0.3.
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

            # --- Symbols: intentionally not upserted yet (Phase 0.4 scope) ---
            symbol_record_map: dict[tuple[str, str], SymbolModel] = {}
            for sym in all_symbols:
                fm = file_record_map.get(sym.file_path)
                if not fm:
                    continue
                sm = SymbolModel(
                    file_id=fm.id,
                    name=sym.name,
                    type=sym.type.value if hasattr(sym.type, "value") else str(sym.type),
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
                symbol_record_map[(sym.file_path, sym.name)] = sm

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
            }
        except Exception as e:
            session.rollback()
            logger.error("Database save failed: %s", e)
            return {"error": str(e)}
        finally:
            session.close()

    def search(
        self,
        query: str,
        limit: int = 20,
        search_type: str = "hybrid",  # "keyword", "semantic", "hybrid"
    ) -> list[SearchResultItem]:
        """
        Executes search across the indexed repository.
        """
        if search_type == "keyword":
            bm25_res = self.bm25_index.search(query, top_k=limit)
            return self.ranker.fuse(query, bm25_results=bm25_res, vector_results=[], top_k=limit)

        elif search_type == "semantic":
            query_vec = self.embedder.encode(query)
            vector_res = self.vector_index.search(query_vec, top_k=limit)
            return self.ranker.fuse(query, bm25_results=[], vector_results=vector_res, top_k=limit)

        else:  # hybrid
            bm25_res = self.bm25_index.search(query, top_k=limit * 2)
            query_vec = self.embedder.encode(query)
            vector_res = self.vector_index.search(query_vec, top_k=limit * 2)

            # Compute graph centrality / importance scores
            graph_scores: dict[str, float] = {}
            if self.call_graph:
                for doc_id, sym in self.symbols_by_id.items():
                    callers = self.call_graph.reverse_adjacency.get((sym.file_path, sym.name), set())
                    # Normalized graph score (more callers = more important)
                    graph_scores[doc_id] = min(1.0, len(callers) / 5.0)

            return self.ranker.fuse(
                query,
                bm25_results=bm25_res,
                vector_results=vector_res,
                graph_scores=graph_scores,
                top_k=limit,
            )
