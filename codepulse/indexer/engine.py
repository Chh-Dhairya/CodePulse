from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np

from codepulse.embeddings.chunker import CodeChunk, SemanticChunker
from codepulse.embeddings.embedder import CodeEmbedder, EmbeddingUnavailableError
from codepulse.git_diff import GitDiffError, detect_file_changes, get_head_commit_sha
from codepulse.indexer.ignore import RepositoryIgnoreRules
from codepulse.ingestion_safety import IngestionSafetyError
from codepulse.models import FileParseResult, Symbol, compute_content_hash
from codepulse.parser.factory import get_parser_for_file
from codepulse.parser.graph import CallGraphBuilder
from codepulse.search.bm25 import BM25Index
from codepulse.search.ranker import HybridRanker, SearchResultItem
from codepulse.search.vector import VectorIndex
from codepulse.storage.db import (
    CallEdge,
    EmbeddingModel,
    FileModel,
    RepositoryModel,
    RepositorySnapshotModel,
    StorageManager,
    SymbolModel,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IncrementalPlan:
    """
    Phase 3.1/3.2 -- the outcome of `IndexingEngine._plan_incremental_run`.

    `is_incremental=False` means `index_directory()` must process every
    discovered file exactly as it always has (this milestone's baseline,
    unchanged, safety-net behavior) -- `changed_relpaths` is then
    meaningless and always `None`. `is_incremental=True` means only the
    files in `changed_relpaths` (added + modified + a rename's NEW path;
    possibly an empty set if nothing changed since the last indexed
    commit) need to be parsed, `deleted_relpaths` names files whose
    stale rows/index-entries must be removed, and `renamed` (Phase 3.2)
    names `(old_path, new_path)` pairs whose EXISTING `File`/`Symbol`/
    `Embedding` rows must be re-keyed to the new path in place --
    NEITHER purged (they are not gone) NOR treated as a fresh add
    (their identity/ids must be preserved) the way `changed_relpaths`/
    `deleted_relpaths` are.

    `reason` is a real, human-readable explanation of which of §9's own
    "when a full re-index is still required" conditions applied (or,
    for an incremental run, a short summary of what changed) -- surfaced
    in `index_directory()`'s own return dict so a caller/test can see
    *why* a given run took the path it did, rather than needing to
    reverse-engineer it from side effects.
    """

    is_incremental: bool
    changed_relpaths: set[str] | None
    deleted_relpaths: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)
    reason: str = ""

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

    def _plan_incremental_run(self, repo_path: str, repo_id: int) -> IncrementalPlan:
        """
        Phase 3.1 (roadmap §37 item 3.1; §9 Incremental Indexing Design's
        own "when a full re-index is still required" list, checked here
        in the same order §9 states it): decides whether
        `index_directory()` can process only the files git says changed
        since the last successful snapshot, or must fall back to
        processing every discovered file exactly as it always has.

        Every fallback condition below returns `is_incremental=False`
        with a real, disclosed `reason` -- never a silent guess.

        Also forces a full run whenever this engine instance has no
        in-memory call graph cached yet for this `repo_id` (e.g. right
        after a process restart, per `hydrate_repository`'s own existing,
        documented contract that the call graph is not durable and
        requires a fresh `index_directory()` call to restore). Without
        this guard, an incremental run in a fresh process would silently
        rebuild `self.call_graphs[repo_id]` from only the changed files
        (this engine's `self.parse_results` cache used to keep the call
        graph complete across incremental runs -- see `index_directory`'s
        call-graph-building step -- is itself in-memory and lost on
        restart), regressing call-graph completeness relative to every
        pre-Phase-3.1 (always-full) run. This is disclosed, not silently
        patched over: full call-graph completeness for incremental runs
        is a best-effort, single-process-lifetime enhancement, not a
        durable guarantee -- true incremental *graph* maintenance across
        restarts is roadmap item 3.5 (local BFS incremental graph
        update), explicitly out of this milestone's scope.
        """
        from codepulse.config import get_settings

        settings = get_settings()
        if not settings.incremental_indexing_enabled:
            return IncrementalPlan(
                is_incremental=False, changed_relpaths=None,
                reason="incremental indexing disabled via config",
            )

        current_commit = get_head_commit_sha(repo_path)
        if current_commit is None:
            return IncrementalPlan(
                is_incremental=False, changed_relpaths=None,
                reason="not a git repository (or a git repository with no commits yet)",
            )

        session = self.storage.get_session()
        try:
            last_snapshot = (
                session.query(RepositorySnapshotModel)
                .filter(RepositorySnapshotModel.repo_id == repo_id)
                .filter(RepositorySnapshotModel.commit_sha.isnot(None))
                .order_by(RepositorySnapshotModel.id.desc())
                .first()
            )
            repo_row = session.query(RepositoryModel).filter(RepositoryModel.id == repo_id).first()
            prior_file_count = repo_row.file_count if repo_row is not None else 0
        finally:
            session.close()

        if last_snapshot is None:
            return IncrementalPlan(
                is_incremental=False, changed_relpaths=None,
                reason="no prior successful git-backed snapshot exists for this repository",
            )

        # Checked only once we know a real prior snapshot exists (a first
        # run never reaches here) -- see this method's own docstring for
        # why a cold `self.call_graphs` cache (e.g. after a process
        # restart) must also force a full run.
        if repo_id not in self.call_graphs:
            return IncrementalPlan(
                is_incremental=False, changed_relpaths=None,
                reason=(
                    "no in-memory call graph cached yet for this repository in this "
                    "process (e.g. after a restart) -- a full re-index is needed to "
                    "fully restore it"
                ),
            )

        if last_snapshot.embedding_model != self.embedder.model_name or last_snapshot.embedding_dim != self.embedder.dimension:
            return IncrementalPlan(
                is_incremental=False, changed_relpaths=None,
                reason=(
                    f"embedding model/dimension changed since the last snapshot "
                    f"({last_snapshot.embedding_model}/{last_snapshot.embedding_dim} -> "
                    f"{self.embedder.model_name}/{self.embedder.dimension})"
                ),
            )

        if last_snapshot.commit_sha == current_commit:
            # Nothing changed at all -- still a real, valid incremental
            # outcome (an empty changeset), not a failure or a reason to
            # fall back to a full walk.
            return IncrementalPlan(
                is_incremental=True, changed_relpaths=set(),
                reason=f"no changes since the last indexed commit {current_commit[:8]}",
            )

        try:
            changes = detect_file_changes(repo_path, since_commit=last_snapshot.commit_sha)
        except GitDiffError as e:
            return IncrementalPlan(
                is_incremental=False, changed_relpaths=None,
                reason=f"git-diff change detection failed, falling back to a full re-index: {e}",
            )

        if prior_file_count > 0:
            changed_fraction = changes.total_changed / prior_file_count
            if changed_fraction > settings.incremental_max_changed_file_fraction:
                return IncrementalPlan(
                    is_incremental=False, changed_relpaths=None,
                    reason=(
                        f"{changes.total_changed} of {prior_file_count} previously-indexed files changed "
                        f"({changed_fraction:.0%}), exceeding the {settings.incremental_max_changed_file_fraction:.0%} "
                        f"incremental threshold -- a full re-index is cheaper"
                    ),
                )

        return IncrementalPlan(
            is_incremental=True,
            changed_relpaths=set(changes.changed_or_added),
            deleted_relpaths=changes.deleted,
            renamed=changes.renamed,
            reason=(
                f"incremental run since commit {last_snapshot.commit_sha[:8]}: "
                f"{len(changes.added)} added, {len(changes.modified)} modified, "
                f"{len(changes.deleted)} deleted, {len(changes.renamed)} renamed"
            ),
        )

    @staticmethod
    def _doc_ids_for_file(session, repo_id: int, fm: FileModel) -> tuple[list[str], list[str]]:
        """
        Phase 3.1/3.2 -- computes the exact `BM25Index`/`VectorIndex`
        `doc_id` strings for every symbol currently associated with the
        given `FileModel` row, from the *durable* `Symbol`/`Embedding`
        rows (not from potentially-incomplete in-memory state). Shared
        by `_purge_stale_entries` (which then deletes the rows too) and
        `_rekey_renamed_files` (which does NOT delete the rows -- it
        re-keys them to a new path instead), since both need the exact
        same doc_id-reconstruction logic, computed against the file's
        CURRENT (pre-change) `path`.
        """
        bm25_doc_ids: list[str] = []
        vector_doc_ids: list[str] = []
        symbol_rows = session.query(SymbolModel).filter(SymbolModel.file_id == fm.id).all()
        for sm in symbol_rows:
            bm25_doc_ids.append(f"{repo_id}::{fm.path}::{sm.name}")
            embedding_rows = session.query(EmbeddingModel).filter(EmbeddingModel.symbol_id == sm.id).all()
            for em in embedding_rows:
                vector_doc_ids.append(f"{repo_id}::{fm.path}::{sm.name}::{em.chunk_index}")
        return bm25_doc_ids, vector_doc_ids

    def _purge_stale_entries(self, repo_id: int, relpaths: list[str]) -> None:
        """
        Phase 3.1 -- removes the stale `File`/`Symbol` (cascading to
        `Embedding`, per the existing schema's `ON DELETE CASCADE`) rows
        for the given relpaths, plus their lexical (`BM25Index`) and
        semantic (`VectorIndex`) index entries and this engine's
        in-memory caches (`symbols_by_id`, `symbol_repo_map`,
        `parse_results`) -- identified from the *durable* rows about to
        be deleted (not from potentially-incomplete in-memory state), so
        this is correct even if the file was indexed in a previous
        process lifetime.

        This serves TWO distinct Phase 3.1 purposes, both requiring the
        exact same operation:

          1. Permanently-deleted files (`IncrementalPlan.deleted_relpaths`)
             -- called once, and nothing re-adds these rows/entries back.

          2. Modified files (`IncrementalPlan.changed_relpaths`), called
             BEFORE they are re-parsed -- a modified symbol's `doc_id` is
             derived from `repo_id::file_path::symbol_name`, which is
             stable across a content-only change, so without removing the
             OLD entries first, re-adding the freshly re-parsed symbols
             under the same doc_id strings would silently accumulate
             duplicate lexical/vector entries. This is the same class of
             problem Phase 2.1's `remove_repo` (whole-repo granularity)
             and this milestone's own `remove_docs` (fine-grained) exist
             to solve -- applied here specifically to the modified-file
             case.

        A relpath with no existing rows (a never-before-indexed path, the
        common case for newly-*added* files) is a safe no-op, matching
        `remove_repo`/`remove_docs`'s own no-op-safety precedent.
        """
        if not relpaths:
            return

        session = self.storage.get_session()
        try:
            file_rows = (
                session.query(FileModel)
                .filter(FileModel.repo_id == repo_id)
                .filter(FileModel.path.in_(relpaths))
                .all()
            )
            bm25_doc_ids: list[str] = []
            vector_doc_ids: list[str] = []
            for fm in file_rows:
                fm_bm25_ids, fm_vector_ids = self._doc_ids_for_file(session, repo_id, fm)
                bm25_doc_ids.extend(fm_bm25_ids)
                vector_doc_ids.extend(fm_vector_ids)

            for fm in file_rows:
                session.delete(fm)
            session.commit()
        finally:
            session.close()

        if bm25_doc_ids:
            self.bm25_index.remove_docs(bm25_doc_ids)
            self.bm25_index.build()
        if vector_doc_ids:
            self.vector_index.remove_docs(vector_doc_ids)
        for doc_id in bm25_doc_ids:
            self.symbols_by_id.pop(doc_id, None)
            self.symbol_repo_map.pop(doc_id, None)
        for relpath in relpaths:
            self.parse_results.pop(f"{repo_id}::{relpath}", None)

    def _rekey_renamed_files(self, repo_id: int, renamed_pairs: list[tuple[str, str]]) -> None:
        """
        Phase 3.2 (roadmap §37 item 3.2: "Rename/move detection + symbol
        re-keying"; §9: "re-key existing Symbol/Embedding rows to the
        new path instead of delete+recreate -- this preserves symbol
        IDs"). For each `(old_path, new_path)` pair git identified as a
        rename/move:

          1. Finds the EXISTING `FileModel` row at `(repo_id, old_path)`.
          2. Removes its symbols' CURRENT (old-path-encoded) `BM25Index`/
             `VectorIndex` doc_ids -- these strings embed the file path,
             so they go stale the moment the path changes, even though
             nothing about the underlying `Symbol`/`Embedding` DB rows
             needs to change. Does NOT delete the DB rows themselves --
             unlike `_purge_stale_entries`, which this deliberately does
             NOT call, since those rows are being preserved, not removed.
          3. Updates that row's `path` column to `new_path` IN PLACE (a
             plain `UPDATE`, not delete+insert) -- this is the crux of
             "re-keying": `file_id` (and therefore every associated
             `Symbol.id`/`Embedding.id`, via the existing, unchanged
             `file_id` foreign key) is preserved exactly.

        `new_path` is expected to be included in this run's
        `changed_relpaths` (by `_plan_incremental_run`, via
        `ChangeSet.changed_or_added`) so it gets parsed normally by the
        ordinary "Parse files" step immediately after this method runs
        -- when `_persist_to_db`'s File-upsert-by-`(repo_id, path)`
        lookup runs for `new_path`, it will find this ALREADY-re-keyed
        row and correctly treat it as an existing file being updated in
        place (matched-by-natural-key, per the existing Phase 0.4
        `Symbol` upsert), not a brand new one -- for a pure rename (byte-
        identical content), every symbol's `(name, start_line)` is
        unchanged, so every existing `Symbol` row is reused untouched,
        fully satisfying "preserves symbol IDs" for that common case.

        Scope boundary, explicitly NOT solved here (a deliberate,
        disclosed choice for this milestone -- see CODEPULSE_PROGRESS.md):
        for a rename that ALSO edits content such that a symbol's own
        `start_line` shifts, that one symbol will not be matched by the
        existing `(file_id, name, start_line)` key and will be treated
        as a fresh symbol (a new id) the same way an ordinary modified
        file already behaves today -- a pre-existing Phase 0.4 limitation
        (no `(name, type)`-based, start_line-independent matching exists
        anywhere in this codebase yet), not something this milestone
        newly introduces or is required to fix. `CallEdge` rows (named
        `DependencyModel` before Phase 3.4) recorded before the rename
        also keep referencing the OLD path string (call graph edges are
        never deduped/corrected across a rename, or across any other
        kind of re-index -- a pre-existing gap explicitly assigned to
        roadmap items 3.4/3.5's "Call graph
        updates" work, not 3.2's).

        Conservative fallback (matching the roadmap's own risk-register
        mitigation: "fallback to delete+recreate rather than a risky
        guess"): if no existing row is found at `old_path` (e.g. it was
        never actually indexed before), re-keying is skipped for that
        pair with a logged warning -- `new_path` still gets indexed
        normally as a fresh file via the ordinary parse loop, since it
        is already part of `changed_relpaths` regardless.
        """
        if not renamed_pairs:
            return

        for old_path, new_path in renamed_pairs:
            session = self.storage.get_session()
            try:
                fm = (
                    session.query(FileModel)
                    .filter(FileModel.repo_id == repo_id)
                    .filter(FileModel.path == old_path)
                    .first()
                )
                if fm is None:
                    logger.warning(
                        "Rename detected (%s -> %s) but no existing File row found "
                        "for repo_id=%s at the old path -- skipping re-key; %s will "
                        "be indexed as a fresh file instead.",
                        old_path, new_path, repo_id, new_path,
                    )
                    continue

                bm25_doc_ids, vector_doc_ids = self._doc_ids_for_file(session, repo_id, fm)
                fm.path = new_path
                session.commit()
            finally:
                session.close()

            if bm25_doc_ids:
                self.bm25_index.remove_docs(bm25_doc_ids)
                self.bm25_index.build()
            if vector_doc_ids:
                self.vector_index.remove_docs(vector_doc_ids)
            for doc_id in bm25_doc_ids:
                self.symbols_by_id.pop(doc_id, None)
                self.symbol_repo_map.pop(doc_id, None)
            self.parse_results.pop(f"{repo_id}::{old_path}", None)

    def _diff_and_purge_file_symbols(
        self, repo_id: int, rel_path: str, new_symbols: list[Symbol]
    ) -> set[int]:
        """
        Phase 3.3 (roadmap §37 item 3.3: "Symbol-level body_hash
        comparison to skip unchanged embeddings"; §9: "within a changed
        file, compare `body_hash` per extracted symbol against the
        previous snapshot's symbols... Unchanged symbols skip
        re-embedding and re-chunking entirely -- this is the single
        biggest cost saver on incremental runs").

        Compares `new_symbols` (this run's freshly parsed symbols for
        `rel_path`) against the EXISTING, durable `SymbolModel` rows for
        that same file (if any), and:

          1. Returns `{id(sym) for sym in new_symbols if unchanged}` --
             Python object identity is a safe, cheap key here because
             `new_symbols` is a single parse pass's freshly constructed
             list, used only within the same `index_directory()` call
             that produced it.
          2. For every existing row that DISAPPEARED entirely (no
             matching new symbol at all -- deleted, or renamed to a
             different name WITHIN the file) -- deletes its
             `SymbolModel` row (cascading to `Embedding`, per the
             existing schema) and purges its lexical/vector doc_ids.
             This closes a real, previously-open, previously-disclosed
             gap as a necessary side effect of building this comparison
             at all (`_persist_to_db`'s own docstring: "this mirrors
             Phase 0.4's decision not to delete stale Symbol rows...
             real stale-data cleanup is Phase 3").
          3. For every symbol that MATCHED an existing row but has a
             DIFFERENT `body_hash` -- purges its lexical/vector doc_ids
             (it will be re-chunked/re-embedded and re-added by the
             normal pipeline immediately after this call returns,
             under the same doc_id string).
          4. For every symbol that matched AND has the SAME `body_hash`
             -- does nothing at all: its doc_id is deliberately left
             untouched (not purged, not scheduled for re-add), since
             its existing lexical/vector entries and DB rows are
             already correct.

        Matching key -- a deliberate, disclosed scope choice (mirroring
        the Phase 3.2 precedent, see CODEPULSE_PROGRESS.md): `(name,
        start_line)` plus a `type` equality check -- the EXACT SAME
        natural key `_persist_to_db`'s own Symbol upsert already uses,
        NOT a broader `(name, type)`-only key. §9's own text describes
        matching by "(name, type) first, falling back to signature
        similarity if the name changed" -- a fuller, start_line-
        independent matcher (needed to correctly recognize an unchanged
        symbol whose `start_line` merely shifted because unrelated code
        above it in the file changed) is deliberately NOT built here.
        Using the SAME key `_persist_to_db` uses guarantees the two can
        never disagree about which row is "the same symbol": a symbol
        whose `start_line` shifts simply isn't eligible for this
        optimization and falls through to a full re-chunk/re-embed with
        a fresh id -- exactly today's pre-existing (Phase 0.4) behavior
        for that case, not a regression. Building the fuller matcher is
        a disclosed, deferred enhancement, not silently done.

        Deliberately NOT called for a file that was renamed this run
        (see `index_directory`'s own caller) -- a rename changes every
        one of its symbols' doc_id strings (which embed the file path)
        regardless of body content, so "leave unchanged symbols' doc_ids
        alone" does not apply there; `_rekey_renamed_files` already
        handles that case's own (whole-file) doc_id purge correctly.
        """
        unchanged_ids: set[int] = set()
        bm25_doc_ids_to_purge: list[str] = []
        vector_doc_ids_to_purge: list[str] = []

        session = self.storage.get_session()
        try:
            fm = (
                session.query(FileModel)
                .filter(FileModel.repo_id == repo_id)
                .filter(FileModel.path == rel_path)
                .first()
            )
            if fm is None:
                return unchanged_ids  # brand new file -- nothing to compare against

            existing_by_key: dict[tuple[str, int], SymbolModel] = {
                (sm.name, sm.start_line): sm
                for sm in session.query(SymbolModel).filter(SymbolModel.file_id == fm.id).all()
            }
            matched_existing_ids: set[int] = set()

            for sym in new_symbols:
                sym_type_value = sym.type.value if hasattr(sym.type, "value") else str(sym.type)
                existing_sm = existing_by_key.get((sym.name, sym.start_line))
                if existing_sm is None or existing_sm.type != sym_type_value:
                    continue  # a genuinely new symbol -- nothing to purge, nothing to skip
                matched_existing_ids.add(existing_sm.id)
                if existing_sm.body_hash == sym.body_hash:
                    unchanged_ids.add(id(sym))
                else:
                    bm25_doc_ids_to_purge.append(f"{repo_id}::{rel_path}::{sym.name}")
                    embedding_rows = (
                        session.query(EmbeddingModel)
                        .filter(EmbeddingModel.symbol_id == existing_sm.id)
                        .all()
                    )
                    for em in embedding_rows:
                        vector_doc_ids_to_purge.append(
                            f"{repo_id}::{rel_path}::{sym.name}::{em.chunk_index}"
                        )

            disappeared = [sm for sm in existing_by_key.values() if sm.id not in matched_existing_ids]
            for sm in disappeared:
                bm25_doc_ids_to_purge.append(f"{repo_id}::{rel_path}::{sm.name}")
                embedding_rows = session.query(EmbeddingModel).filter(EmbeddingModel.symbol_id == sm.id).all()
                for em in embedding_rows:
                    vector_doc_ids_to_purge.append(f"{repo_id}::{rel_path}::{sm.name}::{em.chunk_index}")
                session.delete(sm)

            if disappeared:
                session.commit()
        finally:
            session.close()

        if bm25_doc_ids_to_purge:
            self.bm25_index.remove_docs(bm25_doc_ids_to_purge)
            self.bm25_index.build()
        if vector_doc_ids_to_purge:
            self.vector_index.remove_docs(vector_doc_ids_to_purge)
        for doc_id in bm25_doc_ids_to_purge:
            self.symbols_by_id.pop(doc_id, None)
            self.symbol_repo_map.pop(doc_id, None)

        return unchanged_ids

    def _record_snapshot(self, repo_id: int, repo_path: str) -> None:
        """
        Phase 3.1 -- records one `RepositorySnapshotModel` row for this
        successful `index_directory()` run (full or incremental alike),
        so the *next* call's `_plan_incremental_run` has a real prior
        commit/embedding-configuration to compare against. Failure to
        record a snapshot is logged, not raised -- a missing snapshot
        row only ever causes the *next* run to safely fall back to a
        full re-index (§9's own "no prior successful snapshot" case),
        never data loss or a fabricated result for *this* run.
        """
        try:
            commit_sha = get_head_commit_sha(repo_path)
            session = self.storage.get_session()
            try:
                session.add(
                    RepositorySnapshotModel(
                        repo_id=repo_id,
                        commit_sha=commit_sha,
                        embedding_model=self.embedder.model_name,
                        embedding_dim=self.embedder.dimension,
                    )
                )
                session.commit()
            finally:
                session.close()
        except Exception as e:
            logger.warning("Failed to record repository snapshot for repo_id=%s: %s", repo_id, e)

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

        # Phase 3.1: decide, before walking the directory, whether this
        # run can be limited to git-diff-detected changed/added files
        # (with deleted files cleaned up separately) or must process
        # every discovered file exactly as every pre-Phase-3.1 run did.
        # See `_plan_incremental_run`'s own docstring for the full
        # decision logic (every §9 fallback condition, in order).
        incremental_plan = self._plan_incremental_run(repo_path, repo_id)

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

        # Phase 3.1: when incremental, restrict the (expensive) parse/
        # chunk/embed loop below to just the files git-diff says were
        # added, modified, or renamed/moved (a rename's NEW path) --
        # unchanged files are neither re-read nor re-parsed, which is the
        # whole cost-saving point of §9. A full run (`is_incremental=
        # False`) leaves `all_files` untouched and skips this block
        # entirely, exactly matching every pre-Phase-3.1 run's behavior.
        renamed_new_paths: set[str] = set()
        if incremental_plan.is_incremental and incremental_plan.changed_relpaths is not None:
            changed_set = incremental_plan.changed_relpaths
            all_files = [
                fp
                for fp in all_files
                if os.path.relpath(fp, repo_path).replace("\\", "/") in changed_set
            ]
            total_files = len(all_files)
            logger.info(
                "Incremental run for repo_id=%s: %s -- parsing %d changed/added/renamed file(s)",
                repo_id,
                incremental_plan.reason,
                total_files,
            )
            # Phase 3.2: re-key renamed files' EXISTING File/Symbol/
            # Embedding rows to their new path FIRST.
            self._rekey_renamed_files(repo_id, incremental_plan.renamed)
            renamed_new_paths = {new for _, new in incremental_plan.renamed}
            # Phase 3.3: deleted files are still handled here, file-
            # granularity, exactly as Phase 3.1 established (nothing
            # left to compare a deleted file's symbols against, and
            # nothing will re-parse it). MODIFIED files (and a rename's
            # new path) are deliberately EXCLUDED from this file-level
            # purge as of Phase 3.3 -- purging an entire modified file's
            # doc_ids up front, as Phase 3.1 originally did, would have
            # thrown away exactly the still-correct, still-valid entries
            # for its UNCHANGED symbols before the new, symbol-level
            # `_diff_and_purge_file_symbols` (called per-file inside the
            # parse loop below) ever gets a chance to decide they didn't
            # need touching at all. Added files never needed this
            # (nothing exists yet to purge) either way.
            stale_paths = sorted(set(incremental_plan.deleted_relpaths))
            self._purge_stale_entries(repo_id, stale_paths)
        elif incremental_plan.reason:
            logger.info("Full re-index for repo_id=%s: %s", repo_id, incremental_plan.reason)

        parsed_files: list[FileParseResult] = []
        all_symbols: list[Symbol] = []
        all_chunks: list[CodeChunk] = []
        # Maps id(chunk) -> the Symbol object it was generated from. Needed
        # because a chunk's own start_line/end_line describe the chunk's
        # line range (which differs from the symbol's for body_chunk pieces),
        # not the symbol's identity -- this is how embeddings get correctly
        # linked back to a specific Symbol row during persistence (Phase 0.5).
        chunk_symbol_map: dict[int, Symbol] = {}
        # Phase 3.3: `id(sym)` of every freshly parsed symbol determined
        # to be byte-identical (same `body_hash`) to its existing,
        # durable row -- populated per-file by `_diff_and_purge_file_symbols`
        # below. Accumulated across every file parsed this run so both
        # the chunking step (immediately below) and the BM25 population
        # step (later in this method) can skip the same symbols.
        unchanged_symbol_ids: set[int] = set()

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
                # Phase 3.1: repo-qualified key (previously bare `rel_path`,
                # which this dict's only prior use -- write-only, never
                # read -- never exercised the cross-repo collision risk
                # that a bare key would create once it became load-bearing
                # for call-graph-completeness below).
                self.parse_results[f"{repo_id}::{rel_path}"] = parse_res
                parsed_files.append(parse_res)

                # Phase 3.3: compare this file's freshly parsed symbols
                # against its existing, durable rows (if any) and purge
                # exactly the doc_ids that need it -- skipped for a
                # rename's new path, since `_rekey_renamed_files` already
                # purged that file's doc_ids in full (every symbol's
                # doc_id embeds the file path, so it changes regardless
                # of body content -- see that method's own docstring).
                # A brand-new file (never indexed before) or a full,
                # non-incremental run both safely no-op here (nothing to
                # compare against / not called at all, respectively).
                if incremental_plan.is_incremental and rel_path not in renamed_new_paths:
                    unchanged_symbol_ids |= self._diff_and_purge_file_symbols(
                        repo_id, rel_path, parse_res.symbols
                    )

                for sym in parse_res.symbols:
                    all_symbols.append(sym)
                    if id(sym) in unchanged_symbol_ids:
                        # Phase 3.3 -- the single biggest cost saver §9
                        # describes: this symbol's body is byte-identical
                        # to what's already durably stored and correctly
                        # indexed, so it is neither re-chunked nor
                        # re-submitted for embedding at all.
                        continue
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
        #
        # Phase 3.1: on an incremental run, `parsed_files` above only
        # covers this run's changed/added files -- building the call
        # graph from that alone would silently regress completeness
        # (edges from every unchanged file would vanish from
        # `self.call_graphs[repo_id]`). To avoid that regression without
        # doing roadmap item 3.5's job (local BFS incremental graph
        # update, a genuinely different, deferred feature), merge in this
        # engine instance's own cached `FileParseResult`s (populated by
        # every parse, this run and prior ones, keyed by
        # "{repo_id}::{rel_path}") for every currently-existing,
        # unchanged file. `_plan_incremental_run`'s own
        # "no in-memory call graph cached yet" guard is what keeps this
        # safe after a process restart (that case always forces a full
        # run instead, so this merge is never reached with a cold cache).
        if incremental_plan.is_incremental:
            cg_parsed_files = list(parsed_files)
            seen_relpaths = {pf.file_path for pf in parsed_files}
            prefix = f"{repo_id}::"
            for key, cached in self.parse_results.items():
                if not key.startswith(prefix):
                    continue
                cached_relpath = key[len(prefix):]
                if cached_relpath in seen_relpaths or cached_relpath in incremental_plan.deleted_relpaths:
                    continue
                cg_parsed_files.append(cached)
        else:
            cg_parsed_files = parsed_files
        repo_call_graph = CallGraphBuilder(cg_parsed_files)
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

        # 5. Populate BM25 (Tantivy-backed, Phase 2.1) Index
        # Phase 0.6: doc_id is repo-qualified (same collision reasoning as
        # the vector index above) and repo_id is threaded into the BM25
        # metadata and symbol_repo_map so repository-scoped search can
        # filter candidates without a second identity system.
        #
        # Phase 2.1: `remove_repo` deletes every lexical document this
        # engine has previously indexed for THIS repository before the
        # fresh add pass below -- required so that re-indexing an
        # already-indexed repository (a real, supported flow: a new
        # IndexJob can be created once the previous one is no longer
        # active, Phase 1.3) replaces rather than accumulates its lexical
        # documents. Safe to call on a never-before-indexed (or empty)
        # repository -- a no-op in that case (see BM25Index.remove_repo's
        # own docstring). This was a real, pre-existing, untested gap in
        # the pre-Phase-2.1 in-memory BM25Index (no doc_id-based replace
        # logic existed there at all); fixed here because Tantivy's
        # delete-by-term makes it essentially free, with zero impact on
        # the ranking contract.
        #
        # Phase 3.1: this whole-repo wipe is only correct for a FULL run,
        # where `all_symbols` below is about to cover every symbol in the
        # repository (so wipe-then-readd-everything is a correct
        # replace). On an INCREMENTAL run, `all_symbols` only covers the
        # changed/added subset -- calling `remove_repo` here would wipe
        # every unchanged file's lexical documents too, with nothing in
        # this run re-adding them back. `_purge_stale_entries` (called
        # earlier, before the parse loop) already removed exactly the
        # stale entries for modified/deleted files, so no whole-repo wipe
        # is needed -- or correct -- for an incremental run.
        if not incremental_plan.is_incremental:
            self.bm25_index.remove_repo(repo_id)
        for sym in all_symbols:
            doc_id = f"{repo_id}::{sym.file_path}::{sym.name}"
            self.symbols_by_id[doc_id] = sym
            self.symbol_repo_map[doc_id] = repo_id
            if id(sym) in unchanged_symbol_ids:
                # Phase 3.3: this symbol's existing lexical document is
                # already correct and was deliberately left untouched by
                # `_diff_and_purge_file_symbols` (never purged) -- adding
                # it again here would create a duplicate Tantivy
                # document under the same doc_id. `unchanged_symbol_ids`
                # is only ever populated during an incremental run (see
                # its own docstring), so this is unreachable -- and would
                # be unsafe -- during a full run, where `remove_repo`
                # just unconditionally wiped every symbol's document
                # above and every one of them genuinely needs re-adding.
                continue
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

        # Phase 3.1: record this run's snapshot (commit_sha + embedding
        # config) AFTER successful persistence, whether this run was
        # incremental or full -- so the *next* call's
        # `_plan_incremental_run` has an up-to-date basis for comparison.
        # See `_record_snapshot`'s own docstring for why a failure here
        # is logged rather than raised.
        self._record_snapshot(repo_id, repo_path)

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
            # Phase 3.1: discloses whether this run was incremental (and
            # why), rather than requiring a caller to reverse-engineer it
            # from `total_files` alone.
            "incremental": incremental_plan.is_incremental,
            "incremental_reason": incremental_plan.reason,
            "deleted_files": list(incremental_plan.deleted_relpaths),
            # Phase 3.2: {old, new} pairs re-keyed in place this run.
            "renamed_files": [{"old": old, "new": new} for old, new in incremental_plan.renamed],
            # Phase 3.3: how many symbols were recognized as byte-
            # identical to their existing, durable row and therefore
            # skipped re-chunking/re-embedding (and, for BM25, skipped
            # re-adding) entirely this run.
            "symbols_skipped_unchanged": len(unchanged_symbol_ids),
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
                    # Placeholder -- overwritten below with a real DB count
                    # once this run's File/Symbol upserts are flushed.
                    file_count=len(parsed_files),
                    symbol_count=len(all_symbols),
                    last_sync=now,
                )
                session.add(repo_record)
            else:
                repo_record.name = repo_name
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

            # Phase 3.1: `repo_record.file_count`/`symbol_count` must
            # reflect the repository's TRUE total, not just this run's
            # `len(parsed_files)`/`len(all_symbols)` -- those only equal
            # the true total on a FULL run (where every file is always
            # parsed). On an INCREMENTAL run, `parsed_files`/`all_symbols`
            # cover only the changed/added subset, so using their lengths
            # here would fabricate a shrunken file/symbol count -- a
            # direct violation of the "no fabricated metrics" invariant,
            # and it would also corrupt `_plan_incremental_run`'s own
            # `prior_file_count` denominator (used for the >50%-changed
            # fallback check) on every subsequent run. Querying the real,
            # durable row counts is correct for a full run too (identical
            # result, since every parseable file is always upserted into
            # `FileModel`/`SymbolModel` on a full run) and is unaffected
            # by whether `_purge_stale_entries` already ran earlier in
            # `index_directory` (it always runs, and commits, before this
            # method is ever called).
            repo_record.file_count = (
                session.query(FileModel).filter(FileModel.repo_id == repo_record.id).count()
            )
            repo_record.symbol_count = (
                session.query(SymbolModel)
                .join(FileModel, SymbolModel.file_id == FileModel.id)
                .filter(FileModel.repo_id == repo_record.id)
                .count()
            )

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

            # Persist call graph edges (Phase 3.4: now carrying a real,
            # computed `confidence` tier per edge -- see
            # CallGraphBuilder._build_graph. Still NOT upserted/deduped
            # across re-indexes -- rows simply accumulate every run, a
            # pre-existing, disclosed Phase 0.4 limitation this milestone
            # does not change; recomputing only the edges touching
            # changed symbols is roadmap item 3.5's job, not 3.4's.
            #
            # Post-Phase-3.4 isolation fix: every row now carries
            # `repo_id=repo_record.id` -- the repository this exact
            # `_persist_to_db` call is persisting, which is guaranteed to
            # be the SAME repository `self.call_graph` was just built for
            # earlier in this same `index_directory()` invocation (that
            # alias is only ever read-then-written within one call, never
            # across two interleaved ones). Without this, `CallEdge` rows
            # had no repository association at all, and callers filtering
            # by symbol name alone (e.g. GET /v1/symbols/{id}) could
            # receive an unrelated repository's edges -- a real, confirmed
            # cross-repository data leak, fixed here at the source.
            if self.call_graph:
                for edge in self.call_graph.edges:
                    ce = CallEdge(
                        repo_id=repo_record.id,
                        source_symbol_name=edge.source_symbol_name,
                        source_file=edge.source_file,
                        target_symbol_name=edge.target_symbol_name,
                        target_file=edge.target_file,
                        call_count=edge.call_count,
                        confidence=edge.confidence,
                    )
                    session.add(ce)

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
        search_type: str = "hybrid",  # "keyword", "semantic", "hybrid", "rrf"
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

        elif search_type == "rrf":
            # Phase 2.3 (roadmap §36/§37 item 2.3): the already-implemented
            # Reciprocal Rank Fusion path, now reachable via `search_type`.
            # Candidate generation and repository-scoping are unchanged
            # from -- and pool-sized identically to -- the "hybrid" branch
            # below, so RRF is isolated exactly as strongly as weighted
            # fusion already is; only the final fusion step differs (rank-
            # based RRF instead of weighted-linear combination). No
            # call-graph signal is computed here: `HybridRanker.fuse_rrf`
            # does not accept one (unchanged from before this milestone --
            # extending RRF's own formula to fuse graph scores would be a
            # ranking-algorithm change, out of this milestone's scope,
            # which only wires up the existing, unmodified RRF formula).
            bm25_pool = self.bm25_index.num_docs if repository_id is not None else limit * 2
            vector_pool = len(self.vector_index.doc_ids) if repository_id is not None else limit * 2

            bm25_res = _repo_filter(self.bm25_index.search(query, top_k=bm25_pool))
            query_vec = self.embedder.encode(query)
            vector_res = _repo_filter(self.vector_index.search(query_vec, top_k=vector_pool))

            return self.ranker.fuse_rrf(bm25_results=bm25_res, vector_results=vector_res, top_k=limit)

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
