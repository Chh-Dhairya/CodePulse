from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np
from sqlalchemy import inspect as sa_inspect

from codepulse.embeddings.chunker import CodeChunk, SemanticChunker
from codepulse.embeddings.embedder import CodeEmbedder, EmbeddingUnavailableError
from codepulse.git_diff import GitDiffError, detect_file_changes, get_head_commit_sha
from codepulse.indexer.ignore import RepositoryIgnoreRules
from codepulse.ingestion_safety import IngestionSafetyError
from codepulse.jobs import reap_stale_jobs
from codepulse.models import CallGraphEdge, FileParseResult, Symbol, SymbolType, compute_content_hash
from codepulse.parser.factory import get_parser_for_file
from codepulse.parser.graph import CallGraphBuilder
from codepulse.parser.module_graph import ModuleGraphBuilder
from codepulse.repository_identity import canonical_repository_url, find_repository
from codepulse.search.bm25 import BM25Index
from codepulse.search.ranker import HybridRanker, SearchResultItem
from codepulse.search.vector import VectorIndex
from codepulse.storage.db import (
    CallEdge,
    EmbeddingModel,
    FileModel,
    ModuleEdge,
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
# How many relpaths `_purge_stale_entries` binds into one `IN (...)` lookup.
_PURGE_LOOKUP_BATCH = 500

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
        # Repository ids whose `call_graphs` entry was restored from the
        # durable `call_edges` rows at startup rather than built from this
        # process's own parse results. Such a graph answers queries
        # correctly but has no parse cache behind it, so it can't be the
        # base of an incremental update (see `_plan_incremental_run`).
        self._restored_call_graph_repo_ids: set[int] = set()
        # Module-structure graphs restored from the durable `module_edges`
        # rows at startup (repo_id -> graph). Consulted by
        # `analyze_module_structure` only when this process has no parse
        # results for that repository; dropped as soon as the repository is
        # indexed here (the parse cache is then authoritative).
        self._restored_module_graphs: dict[int, ModuleGraphBuilder] = {}
        self._warned_module_edges_missing = False
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

        # Backend-hardening addition (not a roadmap sprint item): reclaims
        # any IndexJob left `queued`/`running` by a previous process that
        # crashed/restarted before it could finish -- see
        # `codepulse.jobs.reap_stale_jobs`'s own docstring for why a
        # startup-time reconciliation (not a heartbeat/timeout) is the
        # correct mechanism here. Same fail-open philosophy as hydration
        # above: a failure here is logged, not fatal to engine startup.
        try:
            reaped = reap_stale_jobs(self.storage)
            if reaped:
                logger.warning("Reaped %d stale index job(s) at startup: %s", len(reaped), reaped)
        except Exception as e:
            logger.warning("Startup job reaping failed (continuing without it): %s", e)

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
            repo_record = find_repository(session, repo_url)
            if repo_record is None:
                repo_record = RepositoryModel(
                    name=repo_name, url=canonical_repository_url(repo_url), file_count=0, symbol_count=0
                )
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
        durable guarantee. Phase 3.5's own "local BFS incremental graph
        update" (implemented -- see `_persist_call_graph_edges`) fixes
        the DURABLE `CallEdge` table's update strategy, not this: the
        in-memory graph cache's restart-survivability remains exactly
        this same best-effort, single-process-lifetime guarantee,
        unchanged by that milestone -- this guard is still needed and
        still correct after it.
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
        if repo_id not in self.call_graphs or repo_id in self._restored_call_graph_repo_ids:
            # A graph restored from stored edges at startup counts as cold
            # here: an incremental run merges THIS process's cached parse
            # results for unchanged files, and a restored graph has none,
            # so building on it would silently drop their edges.
            return IncrementalPlan(
                is_incremental=False, changed_relpaths=None,
                reason=(
                    "no in-memory call graph cached yet for this repository in this "
                    "process (e.g. after a restart; stored call edges are restored "
                    "for queries, but an incremental update also needs this "
                    "process's parse cache) -- a full re-index is needed to "
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
    def _doc_ids_for_file(
        session, repo_id: int, fm: FileModel
    ) -> tuple[list[str], list[str], list[tuple[str, str]]]:
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

        Also returns each symbol's `(file_path, symbol_name)` key
        (Phase 3.5: fed into the call-graph incremental-update BFS --
        see `_persist_call_graph_edges`'s own docstring for why the
        caller needs to know exactly which symbol identities are about
        to stop existing under their current path).
        """
        bm25_doc_ids: list[str] = []
        vector_doc_ids: list[str] = []
        symbol_keys: list[tuple[str, str]] = []
        symbol_rows = session.query(SymbolModel).filter(SymbolModel.file_id == fm.id).all()
        for sm in symbol_rows:
            bm25_doc_ids.append(f"{repo_id}::{fm.path}::{sm.name}")
            symbol_keys.append((fm.path, sm.name))
            embedding_rows = session.query(EmbeddingModel).filter(EmbeddingModel.symbol_id == sm.id).all()
            for em in embedding_rows:
                vector_doc_ids.append(f"{repo_id}::{fm.path}::{sm.name}::{em.chunk_index}")
        return bm25_doc_ids, vector_doc_ids, symbol_keys

    def _purge_stale_entries(self, repo_id: int, relpaths: list[str]) -> set[tuple[str, str]]:
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

        Returns the `(file_path, symbol_name)` key for every symbol that
        was deleted (Phase 3.5: fed into `_persist_call_graph_edges`'s
        incremental BFS -- a deleted symbol's inbound call-graph edges,
        recorded from unrelated, unchanged files, must be found and
        corrected too, not left pointing at a symbol that no longer
        exists).
        """
        if not relpaths:
            return set()

        session = self.storage.get_session()
        try:
            # Looked up in batches: a single `IN (...)` binding every path
            # overflows SQLite's variable limit when a large directory
            # becomes ignored or is deleted (tens of thousands of paths).
            file_rows: list[FileModel] = []
            for start in range(0, len(relpaths), _PURGE_LOOKUP_BATCH):
                batch = relpaths[start:start + _PURGE_LOOKUP_BATCH]
                file_rows.extend(
                    session.query(FileModel)
                    .filter(FileModel.repo_id == repo_id)
                    .filter(FileModel.path.in_(batch))
                    .all()
                )
            bm25_doc_ids: list[str] = []
            vector_doc_ids: list[str] = []
            removed_symbol_keys: set[tuple[str, str]] = set()
            for fm in file_rows:
                fm_bm25_ids, fm_vector_ids, fm_symbol_keys = self._doc_ids_for_file(session, repo_id, fm)
                bm25_doc_ids.extend(fm_bm25_ids)
                vector_doc_ids.extend(fm_vector_ids)
                removed_symbol_keys.update(fm_symbol_keys)

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

        return removed_symbol_keys

    def _purge_files_missing_from_full_run(
        self, repo_id: int, parsed_relpaths: set[str], failed_relpaths: set[str]
    ) -> set[tuple[str, str]]:
        """
        Purges (via `_purge_stale_entries`, so database rows, the keyword and
        vector indexes, the symbol maps and the parse cache all go together)
        every stored file of this repository that a FULL run did not parse:
        deleted, newly ignored, no longer supported, or now over the size cap.

        Two deliberate exceptions:
          - `failed_relpaths`: a file whose parse raised this run keeps its
            previous rows; the next successful parse replaces them.
          - A run that parsed NO files at all purges nothing. An empty or
            unmounted directory (or a checkout in progress) must never be
            read as "every file was deleted" and wipe the index; the cost is
            that a repository genuinely emptied of source files keeps its
            old rows, which is the behavior from before this purge existed.

        Returns the `(file, symbol)` keys removed, like `_purge_stale_entries`.
        """
        if not parsed_relpaths:
            session = self.storage.get_session()
            try:
                stored = session.query(FileModel.id).filter(FileModel.repo_id == repo_id).count()
            finally:
                session.close()
            if stored:
                logger.warning(
                    "Full run for repo_id=%s parsed no files but %d are stored: not purging "
                    "(an empty or unavailable directory must not wipe the index)",
                    repo_id, stored,
                )
            return set()

        session = self.storage.get_session()
        try:
            stored_paths = {path for (path,) in session.query(FileModel.path).filter(FileModel.repo_id == repo_id)}
        finally:
            session.close()
        stale = sorted(stored_paths - parsed_relpaths - failed_relpaths)
        if not stale:
            return set()
        logger.info("Full run for repo_id=%s: purging %d stored file(s) no longer indexable", repo_id, len(stale))
        return self._purge_stale_entries(repo_id, stale)

    def _rekey_renamed_files(self, repo_id: int, renamed_pairs: list[tuple[str, str]]) -> set[tuple[str, str]]:
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
        newly introduces or is required to fix.

        Returns the OLD `(old_path, symbol_name)` key for every symbol
        in every renamed file (Phase 3.5: fed into
        `_persist_call_graph_edges`'s incremental BFS -- a symbol whose
        FILE moved is, from the perspective of any OTHER, unrelated
        caller's already-persisted `CallEdge` row, indistinguishable
        from one that simply disappeared: that row's `target_file`
        still says the OLD path, and must be found and corrected, not
        left pointing at a path that no longer holds that symbol).

        Conservative fallback (matching the roadmap's own risk-register
        mitigation: "fallback to delete+recreate rather than a risky
        guess"): if no existing row is found at `old_path` (e.g. it was
        never actually indexed before), re-keying is skipped for that
        pair with a logged warning -- `new_path` still gets indexed
        normally as a fresh file via the ordinary parse loop, since it
        is already part of `changed_relpaths` regardless.
        """
        if not renamed_pairs:
            return set()

        removed_symbol_keys: set[tuple[str, str]] = set()
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

                bm25_doc_ids, vector_doc_ids, symbol_keys = self._doc_ids_for_file(session, repo_id, fm)
                removed_symbol_keys.update(symbol_keys)
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

        return removed_symbol_keys

    def _diff_and_purge_file_symbols(
        self, repo_id: int, rel_path: str, new_symbols: list[Symbol]
    ) -> tuple[set[int], set[tuple[str, str]]]:
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

        Also returns the `(rel_path, symbol_name)` key for every symbol
        that DISAPPEARED (case 2 above) -- Phase 3.5: fed into
        `_persist_call_graph_edges`'s incremental BFS, the same reason
        `_purge_stale_entries`/`_rekey_renamed_files` return theirs. A
        symbol whose `body_hash` merely changed (case 3) does NOT need
        its key returned here -- its `(file_path, name)` identity is
        unchanged, so it's already covered by the caller's own "every
        symbol in `all_symbols` not in `unchanged_ids`" pass.
        """
        unchanged_ids: set[int] = set()
        disappeared_keys: set[tuple[str, str]] = set()
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
                return unchanged_ids, disappeared_keys  # brand new file -- nothing to compare against

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
                disappeared_keys.add((rel_path, sm.name))
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

        return unchanged_ids, disappeared_keys

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
        # One identity per repository however its path is spelled (FUTURE_CHANGES item 2).
        resolved_repo_url = canonical_repository_url(repo_url or repo_path)

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
        # Phase 3.5: `(file_path, symbol_name)` identity of every symbol
        # that stopped existing under that identity this run (deleted,
        # renamed-within-a-file, or moved to a new path) -- accumulated
        # here and fed into `_persist_call_graph_edges`'s incremental
        # BFS (see that method's own docstring). Populated below by
        # `_rekey_renamed_files`, `_purge_stale_entries`, and (per-file,
        # inside the parse loop) `_diff_and_purge_file_symbols`.
        removed_symbol_keys: set[tuple[str, str]] = set()
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
            removed_symbol_keys |= self._rekey_renamed_files(repo_id, incremental_plan.renamed)
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
            removed_symbol_keys |= self._purge_stale_entries(repo_id, stale_paths)
        elif incremental_plan.reason:
            logger.info("Full re-index for repo_id=%s: %s", repo_id, incremental_plan.reason)

        # Files whose parse RAISED this run: their previous rows are kept (see
        # the full-run purge below) -- a transient failure must not delete
        # previously good data.
        failed_relpaths: set[str] = set()
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
                    file_unchanged_ids, file_disappeared_keys = self._diff_and_purge_file_symbols(
                        repo_id, rel_path, parse_res.symbols
                    )
                    unchanged_symbol_ids |= file_unchanged_ids
                    removed_symbol_keys |= file_disappeared_keys

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
                failed_relpaths.add(rel_path)
                logger.warning("Error parsing file '%s': %s", rel_path, e)

        # A FULL run's parsed set is the repository's complete current
        # indexable content, so any stored file outside it is stale. Until
        # 2026-10-12 only the incremental path purged (and a full run is what
        # non-git repositories, first runs and every post-restart run get),
        # so deleted/ignored files lingered in the database, the keyword
        # index after a restart and the restored symbol map.
        if not incremental_plan.is_incremental:
            removed_symbol_keys |= self._purge_files_missing_from_full_run(
                repo_id, {pf.file_path for pf in parsed_files}, failed_relpaths
            )

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
        # `self.call_graphs[repo_id]`). This IN-MEMORY graph is still a
        # full rebuild from merged parse data every run (cheap -- no
        # embedding involved), NOT the "recompute only what changed"
        # BFS Phase 3.5 implements -- that operates one layer down, at
        # the DURABLE `CallEdge` persistence step (`_persist_call_graph_
        # edges`), deciding which DB rows to touch from THIS complete,
        # freshly (re)built in-memory graph. Merge in this engine
        # instance's own cached `FileParseResult`s (populated by
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
        self._restored_call_graph_repo_ids.discard(repo_id)
        # Every parsed file and the resolved file-to-file import edges, from
        # the SAME complete (merged, on an incremental run) parse set the
        # call graph was just built from, persisted below so module
        # structure survives a restart. Clustering is computed on demand.
        module_snapshot = ModuleGraphBuilder.snapshot(cg_parsed_files)
        self._restored_module_graphs.pop(repo_id, None)

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
        #
        # Backend-hardening fix: mirrors the exact same "wipe this repo's
        # entries before a full re-index" treatment `bm25_index.remove_repo`
        # already receives below (step 5) -- see that call's own comment for
        # why this is only correct for a FULL run (an incremental run's
        # `_purge_stale_entries`/`remove_docs` calls already remove exactly
        # the stale entries, and a whole-repo wipe here would discard every
        # unchanged file's embeddings with nothing in this run to re-add
        # them). Closes a real, previously-disclosed gap: unlike
        # `BM25Index`, `VectorIndex` had no repository-replacement call at
        # all until now, so a full re-index of an already-indexed
        # repository silently accumulated duplicate vector entries forever
        # (see `VectorIndex.remove_repo`'s own docstring and
        # CODEPULSE_PROGRESS.md for the full history).
        if not incremental_plan.is_incremental:
            self.vector_index.remove_repo(repo_id)

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
            # The same wipe-then-readd for the in-memory symbol map: it can
            # start non-empty (restored from the durable rows at startup, or
            # left by an earlier run in this process), and re-adding only the
            # CURRENT symbols would otherwise leave a deleted file's symbols
            # behind in autocomplete and the graph-score walk.
            for stale_doc_id in [d for d, r in self.symbol_repo_map.items() if r == repo_id]:
                self.symbols_by_id.pop(stale_doc_id, None)
                self.symbol_repo_map.pop(stale_doc_id, None)
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

        # Phase 3.5 (roadmap §37 item 3.5: "Local BFS incremental graph
        # update"): the complete set of symbol identities that stopped
        # existing under their PRIOR (file_path, name) this run --
        # every disappeared/renamed-away key already accumulated above,
        # plus every symbol in `all_symbols` that wasn't recognized as
        # byte-identical (i.e. every genuinely new-or-changed symbol,
        # under its CURRENT identity -- stable across a body-only
        # change, so "current" and "prior" are the same key for that
        # case). Passed to `_persist_to_db` -> `_persist_call_graph_edges`
        # to decide exactly which `CallEdge` rows need recomputing.
        changed_symbol_keys = removed_symbol_keys | {
            (sym.file_path, sym.name) for sym in all_symbols if id(sym) not in unchanged_symbol_ids
        }

        # 6. Save to Database
        stats = self._persist_to_db(
            repo_name=repo_name,
            repo_url=resolved_repo_url,
            parsed_files=parsed_files,
            all_symbols=all_symbols,
            all_chunks=all_chunks,
            chunk_embeddings=chunk_embeddings,
            is_incremental=incremental_plan.is_incremental,
            changed_symbol_keys=changed_symbol_keys,
            module_snapshot=module_snapshot,
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

    def _persist_call_graph_edges(
        self,
        session,
        repo_id: int,
        is_incremental: bool,
        changed_symbol_keys: set[tuple[str, str]],
    ) -> None:
        """
        Phase 3.5 (roadmap §37 item 3.5: "Local BFS incremental graph
        update"; §9: "rather than rebuilding the whole graph, recompute
        only edges whose *source* symbol is in the changed set, plus a
        local BFS one hop out to catch edges whose *target* changed (a
        symbol being renamed/deleted must have inbound edges from
        unrelated files corrected too)").

        Replaces the prior behavior -- every run unconditionally
        appending every edge in `self.call_graph.edges` -- which is
        exactly the long-disclosed, previously-deferred gap every
        milestone since Phase 0.4 has pointed at this one to fix:
        "CallEdge rows still duplicate on every re-index." Uses
        `CallEdge.repo_id` (added by the repository-isolation fix that
        preceded this milestone) to scope every delete precisely to
        this repository -- nothing here can touch another repository's
        rows even by accident.

        FULL run (`is_incremental=False`): every existing `CallEdge` row
        for this `repo_id` is deleted, then the complete fresh set from
        `self.call_graph.edges` is inserted -- a full, correct,
        one-time replace. Mirrors `BM25Index.remove_repo`'s existing
        full-run pattern (Phase 2.1), which `CallEdge` could not follow
        until it had a `repo_id` to scope the delete by.

        INCREMENTAL run: a genuinely surgical update, not a full
        rebuild, matching §9's literal recompute-only-what-changed
        design:
          1. Delete every existing row whose `(source_file,
             source_symbol_name)` is in `changed_symbol_keys` -- these
             are about to be replaced by a fresh recompute of exactly
             that symbol's current outgoing calls.
          2. "Local BFS one hop out": query the REMAINING rows (after
             step 1) for this repo whose `(target_file,
             target_symbol_name)` is in `changed_symbol_keys` -- these
             belong to OTHER, unrelated (possibly unchanged-this-run)
             symbols that call something which just changed, was
             renamed, or disappeared. Their own `(source_file,
             source_symbol_name)` is collected as `one_hop_keys` and
             those rows are deleted too, to be recomputed fresh in step
             3 -- this is what "a symbol being renamed/deleted must
             have inbound edges from unrelated files corrected too"
             concretely means here: the caller's own edge, freshly
             re-derived from `self.call_graph` (which is guaranteed
             complete for this repository -- built from every file's
             parse result, cached or freshly reparsed, per Phase 3.1's
             own cache-completeness design -- so the caller's file
             does NOT need to have been reparsed this run for its edge
             to be correctly recomputed), will naturally reflect the
             correction (e.g. downgrading to `"unresolved"` if the
             callee it used to resolve to no longer exists under that
             identity).
          3. Insert every edge in `self.call_graph.edges` whose
             `(source_file, source_symbol_name)` is in
             `changed_symbol_keys | one_hop_keys`.

        A symbol whose file was neither changed nor a one-hop neighbor
        of anything changed is completely untouched -- its existing,
        already-correct `CallEdge` rows are left exactly as they were,
        avoiding both the accumulation problem and any unnecessary
        rewrite.

        Phase 4.1 (roadmap §37 item 4.1): also persists
        `self.call_graph.inheritance_edges` (`relationship_type=
        "inherits"`) through this EXACT SAME algorithm, alongside
        `self.call_graph.edges` (`relationship_type="calls"`) -- not a
        second persistence mechanism. This is correct, not just
        convenient, because the delete/BFS logic above is already
        relationship-type-AGNOSTIC: it deletes and recomputes by
        `(source_file, source_symbol_name)` / `(target_file,
        target_symbol_name)` alone, with no `relationship_type` filter
        anywhere -- so a changed class's stale "calls" edges AND stale
        "inherits" edges are both correctly deleted together (its
        complete outgoing edge set changed, regardless of kind), and the
        local-BFS-one-hop query already finds a caller through an
        ordinary function call and a subclass extending a renamed/
        deleted base class equally, via the exact same query. Each row
        is still written with its own correct `relationship_type`,
        taken directly from the in-memory `CallGraphEdge.
        relationship_type` field.
        """
        if not self.call_graph:
            return

        all_edges = self.call_graph.edges + self.call_graph.inheritance_edges

        if not is_incremental:
            session.query(CallEdge).filter(CallEdge.repo_id == repo_id).delete(synchronize_session=False)
            for edge in all_edges:
                session.add(CallEdge(
                    repo_id=repo_id,
                    source_symbol_name=edge.source_symbol_name,
                    source_file=edge.source_file,
                    target_symbol_name=edge.target_symbol_name,
                    target_file=edge.target_file,
                    call_count=edge.call_count,
                    confidence=edge.confidence,
                    relationship_type=edge.relationship_type,
                ))
            return

        if not changed_symbol_keys:
            # An incremental run with an empty changeset (e.g. no
            # changes since the last indexed commit) -- nothing to
            # recompute, nothing to touch.
            return

        for file_path, name in changed_symbol_keys:
            session.query(CallEdge).filter(
                CallEdge.repo_id == repo_id,
                CallEdge.source_file == file_path,
                CallEdge.source_symbol_name == name,
            ).delete(synchronize_session=False)

        one_hop_keys: set[tuple[str, str]] = set()
        for file_path, name in changed_symbol_keys:
            callers = (
                session.query(CallEdge.source_file, CallEdge.source_symbol_name)
                .filter(
                    CallEdge.repo_id == repo_id,
                    CallEdge.target_file == file_path,
                    CallEdge.target_symbol_name == name,
                )
                .all()
            )
            for src_file, src_name in callers:
                if (src_file, src_name) not in changed_symbol_keys:
                    one_hop_keys.add((src_file, src_name))

        for file_path, name in one_hop_keys:
            session.query(CallEdge).filter(
                CallEdge.repo_id == repo_id,
                CallEdge.source_file == file_path,
                CallEdge.source_symbol_name == name,
            ).delete(synchronize_session=False)

        recompute_keys = changed_symbol_keys | one_hop_keys
        for edge in all_edges:
            if (edge.source_file, edge.source_symbol_name) not in recompute_keys:
                continue
            session.add(CallEdge(
                repo_id=repo_id,
                source_symbol_name=edge.source_symbol_name,
                source_file=edge.source_file,
                target_symbol_name=edge.target_symbol_name,
                target_file=edge.target_file,
                call_count=edge.call_count,
                confidence=edge.confidence,
                relationship_type=edge.relationship_type,
            ))

    def _module_edges_table_exists(self, session) -> bool:
        """
        False on a database that has not been migrated to the revision that
        adds `module_edges` (an Alembic-tracked database, where
        `StorageManager` deliberately never creates tables). Everything
        touching the table checks this first, so an unmigrated database
        keeps indexing exactly as before and only lacks the restart-proof
        module structure -- logged once, never silent.
        """
        if sa_inspect(session.get_bind()).has_table("module_edges"):
            return True
        if not self._warned_module_edges_missing:
            self._warned_module_edges_missing = True
            logger.warning(
                "Table 'module_edges' does not exist (the database has not been migrated): "
                "module structure will not survive a restart until `alembic upgrade head` is run."
            )
        return False

    def _persist_module_edges(
        self, session, repo_id: int, files: list[str], edges: list[tuple[str, str]]
    ) -> None:
        """
        Replaces this repository's `module_edges` rows with the complete
        current snapshot -- on an incremental run that is the merged set, not
        just the changed files'. One row per resolved edge, plus one row with
        a NULL target for every file with no outgoing edge, so the node set
        is stored (see `ModuleEdge`). A wholesale replace is correct and cheap
        at file level, and is what makes re-indexing idempotent. Runs in the
        caller's transaction.
        """
        if not self._module_edges_table_exists(session):
            return
        session.query(ModuleEdge).filter(ModuleEdge.repo_id == repo_id).delete(synchronize_session=False)
        with_edges = {source for source, _ in edges}
        for source_file, target_file in edges:
            session.add(ModuleEdge(repo_id=repo_id, source_file=source_file, target_file=target_file))
        for file_path in files:
            if file_path not in with_edges:
                session.add(ModuleEdge(repo_id=repo_id, source_file=file_path, target_file=None))

    def _persist_to_db(
        self,
        repo_name: str,
        repo_url: str,
        parsed_files: list[FileParseResult],
        all_symbols: list[Symbol],
        all_chunks: list[CodeChunk],
        chunk_embeddings: list[tuple[Symbol, CodeChunk, np.ndarray]] | None = None,
        is_incremental: bool = False,
        changed_symbol_keys: set[tuple[str, str]] | None = None,
        module_snapshot: tuple[list[str], list[tuple[str, str]]] | None = None,
    ) -> dict[str, int]:
        """
        Persists repository hierarchy to the database.

        `is_incremental`/`changed_symbol_keys` (Phase 3.5, default to a
        safe full-rebuild for any caller that doesn't pass them, e.g. a
        pre-existing direct test call) drive `_persist_call_graph_edges`
        -- see that method's own docstring.

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
            repo_record = find_repository(session, repo_url)
            if repo_record is None:
                repo_record = RepositoryModel(
                    name=repo_name,
                    url=canonical_repository_url(repo_url),
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

                # Phase 4.1: JSON-serialized to match the same "Text
                # column holding a JSON-serialized list, parsed in
                # application code" convention `embedding_json` already
                # uses -- `None` (not `"[]"`) for a symbol with no base
                # classes, so a never-(re)indexed-since-this-migration
                # row (genuinely `NULL`) and a real "no bases" result
                # remain visibly the same thing, which they are (both
                # mean "nothing known/present"), not a false distinction.
                base_classes_json = json.dumps(sym.base_classes) if sym.base_classes else None

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
                        base_classes=base_classes_json,
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
                    existing_sm.base_classes = base_classes_json
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

            # Persist call graph edges. Phase 3.5 (roadmap §37 item 3.5:
            # "Local BFS incremental graph update") -- see
            # `_persist_call_graph_edges`'s own docstring for the full
            # design; this finally closes the long-disclosed "CallEdge
            # rows duplicate/accumulate on every re-index" gap (Phase
            # 0.4) for both full and incremental runs, using the
            # `repo_id` column the isolation fix added.
            self._persist_call_graph_edges(
                session, repo_record.id, is_incremental, changed_symbol_keys or set()
            )
            if module_snapshot is not None:
                self._persist_module_edges(session, repo_record.id, *module_snapshot)

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

        Backend-hardening addition (not a roadmap sprint item): this method
        ALSO rebuilds `bm25_index` for this repository from durably
        persisted `SymbolModel`/`FileModel` rows -- no re-parsing, no
        re-embedding, just re-tokenizing already-stored symbol text. This
        closes a real, previously-disclosed gap (`tests/
        test_restart_idempotency.py`'s own "BM25 index has no backing
        table and must not survive restart" assertion, since RELAXED to
        match): before this, keyword/hybrid search returned nothing at
        all until a fresh `index_directory()` call, even though every
        symbol needed to rebuild it already existed durably in `symbols`/
        `files`. `SymbolModel`/`FileModel` remain the actual source of
        truth throughout -- `bm25_index` is still a derived, rebuildable
        artifact (per `BM25Index`'s own class docstring), just rebuilt
        from SQL at startup instead of only ever from a fresh parse. The
        in-memory symbol map (`symbols_by_id`/`symbol_repo_map`) is a
        derived cache of the same symbol rows and is restored here too
        (2026-10-12), and the call graph is restored from the durable
        `call_edges` rows by `_restore_call_graph` (same date) -- both
        previously required a fresh `index_directory()` call, which made
        callers/impact/autocomplete and the graph part of hybrid ranking
        silently empty after every restart. What is still NOT restored:
        `parse_results` (so a restored graph cannot seed an incremental
        update; see `_plan_incremental_run`) and the module-structure
        import graph.

        Repository scoping: only embeddings/symbols that belong to (or
        whose File belongs to) the given `repo_url` are loaded, so data
        from one repository can never be hydrated into another's results.
        Each loaded vector's metadata is also tagged with `repo_id` (the
        same `RepositoryModel.id` used everywhere else), which is what
        lets `search(..., repository_id=...)` (Phase 0.6) filter hydrated
        embeddings correctly even though `hydrate_all()` loads every
        repository into the same shared `vector_index`/`bm25_index`.

        Vector-dimension safety: a persisted embedding row whose
        `embedding_dim` doesn't match the currently configured
        `vector_index.dimension` is skipped explicitly (e.g. the
        embedding model or dimension was changed since that row was
        written) rather than being loaded and silently corrupting
        similarity results. No equivalent skip exists for symbol
        hydration -- there is no "wrong shape" failure mode for plain
        text fields the way there is for a fixed-dimension vector; a row
        whose `type` doesn't parse as a known `SymbolType` (a defensive,
        not-currently-reachable case -- every parser only ever produces
        valid `SymbolType` values) is skipped and logged instead.
        """
        session = self.storage.get_session()
        try:
            repo_record = find_repository(session, repo_url)
            if repo_record is None:
                return {
                    "repo_id": None,
                    "hydrated_embeddings": 0,
                    "skipped_dimension_mismatch": 0,
                    "hydrated_symbols": 0,
                }

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

            # Backend-hardening addition: rebuild the lexical index from
            # every persisted symbol in this repository (not just ones
            # with an embedding -- index_directory() adds every symbol to
            # bm25_index regardless of embedding success, for degraded-mode
            # keyword search; hydration matches that behavior exactly).
            symbol_rows = (
                session.query(SymbolModel, FileModel)
                .join(FileModel, SymbolModel.file_id == FileModel.id)
                .filter(FileModel.repo_id == repo_record.id)
                .all()
            )
            hydrated_symbols = 0
            for sm, fm in symbol_rows:
                try:
                    symbol_obj = Symbol(
                        name=sm.name,
                        type=SymbolType(sm.type),
                        file_path=fm.path,
                        start_line=sm.start_line,
                        end_line=sm.end_line,
                        signature=sm.signature or "",
                        docstring=sm.docstring,
                        body=sm.body or "",
                        language=sm.language,
                    )
                except ValueError as e:
                    logger.warning(
                        "Skipping unreadable symbol row id=%s during lexical hydration: %s", sm.id, e
                    )
                    continue
                # Matches index_directory's own doc_id format exactly
                # (repo_id::file_path::name) -- required so a subsequent
                # index_directory() call's remove_repo/add_symbol pair
                # replaces this hydrated entry rather than duplicating it.
                doc_id = f"{repo_record.id}::{fm.path}::{sm.name}"
                self.bm25_index.add_symbol(doc_id, symbol_obj, repo_id=repo_record.id)
                # The in-memory symbol map (hybrid ranking's graph score,
                # autocomplete, stats) is a derived cache of these same
                # rows; restoring it keeps those correct after a restart.
                self.symbols_by_id[doc_id] = symbol_obj
                self.symbol_repo_map[doc_id] = repo_record.id
                hydrated_symbols += 1
            if hydrated_symbols:
                self.bm25_index.build()

            self._restore_call_graph(session, repo_record.id)
            self._restore_module_graph(session, repo_record.id)

            return {
                "repo_id": repo_record.id,
                "hydrated_embeddings": len(doc_ids),
                "skipped_dimension_mismatch": skipped_dimension_mismatch,
                "hydrated_symbols": hydrated_symbols,
            }
        finally:
            session.close()

    def _restore_module_graph(self, session, repo_id: int) -> None:
        """
        Rebuilds this repository's module-structure graph from its durable
        `module_edges` rows (resolved edges plus a NULL-target row per file
        without outgoing edges, so the node set comes from the same place),
        so module structure works immediately after a restart.

        Skipped -- the repository stays cold, exactly as before this
        feature -- when the table does not exist yet OR the repository has
        no rows (it has not been indexed since the table was added). A
        partial graph built from anything else would show a confident
        all-singletons structure for a repository that does have imports.
        """
        if repo_id in self._restored_module_graphs or not self._module_edges_table_exists(session):
            return
        rows = session.query(ModuleEdge.source_file, ModuleEdge.target_file).filter(
            ModuleEdge.repo_id == repo_id
        ).all()
        if not rows:
            return
        files = {source for source, _ in rows} | {target for _, target in rows if target is not None}
        self._restored_module_graphs[repo_id] = ModuleGraphBuilder.from_edges(
            sorted(files),
            [(source, target) for source, target in rows if target is not None],
        )

    def _restore_call_graph(self, session, repo_id: int) -> None:
        """
        Rebuilds `self.call_graphs[repo_id]` from the durable `call_edges`
        rows (calls AND inheritance), so callers/callees/impact/references
        work immediately after a restart instead of silently returning
        nothing until a re-index (2026-10-12 QA finding).

        Never replaces a graph this process already built from parse
        results. The restored graph is flagged in
        `_restored_call_graph_repo_ids` so `_plan_incremental_run` still
        forces a full run: it has no parse cache behind it.
        """
        if repo_id in self.call_graphs:
            return
        rows = (
            session.query(CallEdge)
            .filter(CallEdge.repo_id == repo_id)
            .order_by(CallEdge.id)
            .all()
        )
        calls: list[CallGraphEdge] = []
        inherits: list[CallGraphEdge] = []
        for row in rows:
            edge = CallGraphEdge(
                source_symbol_name=row.source_symbol_name,
                source_file=row.source_file,
                target_symbol_name=row.target_symbol_name,
                target_file=row.target_file,
                call_count=row.call_count,
                confidence=row.confidence,
                relationship_type=row.relationship_type,
            )
            (inherits if row.relationship_type == "inherits" else calls).append(edge)
        graph = CallGraphBuilder.from_edges(calls, inherits)
        self.call_graphs[repo_id] = graph
        self._restored_call_graph_repo_ids.add(repo_id)
        if self.call_graph is None:
            # Unscoped search reads this "most recently indexed" alias; with
            # no index run yet in this process, the first restored graph is
            # the closest equivalent (exact for the common single-repo case).
            self.call_graph = graph

    def hydrate_all(self) -> dict[str, Any]:
        """
        Phase 0.5 -- load-on-startup hydration for every repository in the
        database. Called automatically once from `__init__` so a freshly
        constructed engine (e.g. a new server process) has semantic AND
        (backend-hardening addition, not a roadmap sprint item) keyword
        search available immediately from durable storage, without
        re-indexing. See `hydrate_repository` for the per-repository
        details.
        """
        session = self.storage.get_session()
        try:
            repo_urls = [r.url for r in session.query(RepositoryModel.url).all()]
        finally:
            session.close()

        total_hydrated = 0
        total_skipped = 0
        total_symbols = 0
        for url in repo_urls:
            result = self.hydrate_repository(url)
            total_hydrated += result["hydrated_embeddings"]
            total_skipped += result["skipped_dimension_mismatch"]
            total_symbols += result["hydrated_symbols"]

        if total_hydrated or total_skipped or total_symbols:
            logger.info(
                "Startup hydration: %d embeddings + %d lexical symbols loaded across %d repositories (%d skipped: dimension mismatch)",
                total_hydrated,
                total_symbols,
                len(repo_urls),
                total_skipped,
            )

        return {
            "repositories": len(repo_urls),
            "hydrated_embeddings": total_hydrated,
            "skipped_dimension_mismatch": total_skipped,
            "hydrated_symbols": total_symbols,
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

        Backend-hardening addition (not a roadmap sprint item): query-side
        embedding-failure degradation. `"hybrid"`/`"rrf"` gracefully fall
        back to keyword-only results if the query-side `self.embedder.
        encode(query)` call raises `EmbeddingUnavailableError` -- mirroring
        `index_directory()`'s own established "proceed in lexical-only
        degraded mode" philosophy for the exact same exception, just on
        the read path instead of the write path. `fuse`/`fuse_rrf` both
        already handle an empty `vector_results` list correctly (the same
        code path a repository with zero embeddings already exercises),
        so no new fusion logic was needed -- only the failure needed
        catching. `"semantic"` (a request for vector-only results, with no
        other candidate source to fall back to) deliberately still lets
        the exception propagate uncaught -- returning an empty list would
        misrepresent "the model is broken" as "nothing matched," which
        this project's own "no fabricated/misleading success" discipline
        does not allow; `POST /v1/search` catches it there instead and
        returns a clear, honest 503 rather than an opaque 500 (see
        `codepulse/api/app.py`).
        """

        def _repo_filter(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
            if repository_id is None:
                return results
            return [r for r in results if r.get("metadata", {}).get("repo_id") == repository_id]

        def _encode_query_or_degrade(query: str) -> np.ndarray | None:
            """Returns the query embedding, or None if the embedding model
            is unavailable -- the caller treats None as "no vector
            candidates this call," never as an error to propagate. See
            this method's own docstring for why only hybrid/rrf use this
            (semantic has no fallback and must raise)."""
            try:
                return self.embedder.encode(query)
            except EmbeddingUnavailableError as e:
                logger.warning(
                    "Query-side embedding unavailable, degrading to keyword-only results "
                    "for this %s search: %s",
                    search_type,
                    e,
                )
                return None

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
            query_vec = _encode_query_or_degrade(query)
            vector_res = _repo_filter(self.vector_index.search(query_vec, top_k=vector_pool)) if query_vec is not None else []

            return self.ranker.fuse_rrf(bm25_results=bm25_res, vector_results=vector_res, top_k=limit)

        else:  # hybrid
            bm25_pool = self.bm25_index.num_docs if repository_id is not None else limit * 2
            vector_pool = len(self.vector_index.doc_ids) if repository_id is not None else limit * 2

            bm25_res = _repo_filter(self.bm25_index.search(query, top_k=bm25_pool))
            query_vec = _encode_query_or_degrade(query)
            vector_res = _repo_filter(self.vector_index.search(query_vec, top_k=vector_pool)) if query_vec is not None else []

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

    def analyze_impact(
        self, symbol_name: str, file_path: str, repo_id: int, max_depth: int = 3
    ) -> dict[str, Any]:
        """
        Phase 4.2 (roadmap §37 item 4.2 "Impact analysis: direct vs.
        transitive bucketing"; §11 "Impact analysis output"; DoD:
        "impact analysis output clearly separates confidence tiers and
        depth buckets"). The shared intelligence-core impact-analysis
        function -- REST and CLI both call this directly instead of each
        independently calling `CallGraphBuilder.get_callers`/
        `get_callees` and reimplementing bucketing (the architecture
        violation this milestone's own read-only audit found: graph
        logic embedded directly in the FastAPI route, and a second,
        independent copy in the CLI command).

        **CALLS ONLY** -- an explicit, approved architectural decision
        (see CODEPULSE_PROGRESS.md's Phase 4.2 entry): traverses only
        `CallGraphBuilder`'s calls-only adjacency via `get_callers`/
        `get_callees`, which never read `inheritance_adjacency`/
        `reverse_inheritance_adjacency` (Phase 4.1's own separation
        invariant). Inheritance-aware impact analysis is explicitly
        deferred, not part of this milestone.

        **Repository isolation**: `repo_id` must be the repository the
        REQUESTED symbol actually belongs to (the caller resolves this
        from the symbol's own file, never from an unrelated/client-
        suppliable parameter) -- looks up `self.call_graphs.get(repo_id)`
        (the correctly-scoped, per-repository dict), never the singular,
        "most recently indexed" `self.call_graph` alias. A cold cache
        (e.g. immediately after a process restart, before any
        `index_directory()` call -- `self.call_graphs` does not survive
        restart) degrades to empty callers/callees, exactly like the
        pre-4.2 REST handler's own fallback, never an error.

        **Direct/transitive buckets** (§11's own literal definition, no
        additional buckets): `direct` = `depth == 1`, `transitive` =
        `depth >= 2`, applied symmetrically to both callers and callees
        (the pre-4.2 endpoint only ever bucketed callers, as counts, and
        left callees entirely unbucketed). Each entry already carries
        `confidence` (threaded through by `get_callers`/`get_callees`
        themselves, reusing the existing high/low/unresolved
        vocabulary) and the two lists are already deterministically
        ordered by those methods -- this method does no additional
        sorting or filtering of its own.

        The four legacy fields (`direct_callers`, `indirect_callers`,
        `call_chain`, `dependencies`) are computed identically to their
        pre-4.2 values, from the SAME underlying `callers`/`callees`
        lists the new bucketed fields are built from -- this method is a
        strict superset of the old inline REST logic, not a redesign,
        guaranteeing zero behavioral drift for existing consumers/tests.
        """
        repo_call_graph = self.call_graphs.get(repo_id)
        if repo_call_graph:
            callers = repo_call_graph.get_callers(symbol_name, file_path, max_depth=max_depth)
            callees = repo_call_graph.get_callees(symbol_name, file_path, max_depth=max_depth)
        else:
            callers, callees = [], []

        direct_callers = [c for c in callers if c.get("depth") == 1]
        indirect_callers = [c for c in callers if c.get("depth", 0) > 1]
        direct_callees = [c for c in callees if c.get("depth") == 1]
        transitive_callees = [c for c in callees if c.get("depth", 0) > 1]

        return {
            "symbol": symbol_name,
            "direct_callers": len(direct_callers),
            "indirect_callers": len(indirect_callers),
            "call_chain": callers,
            "dependencies": callees,
            "callers": {"direct": direct_callers, "transitive": indirect_callers},
            "callees": {"direct": direct_callees, "transitive": transitive_callees},
        }

    def analyze_module_structure(self, repo_id: int) -> ModuleGraphBuilder:
        """
        Phase 4.3 (roadmap §37 item 4.3 "Deterministic module-clustering
        via import graph"; §11 "Architecture discovery"; §45 backlog
        B18). Library/shared-core-only per this milestone's explicit
        approved scope -- no REST route, CLI command, or MCP tool calls
        this yet; a future one can, through this exact method, without
        duplicating any of `ModuleGraphBuilder`'s own construction
        logic.

        Repository-scoped by the EXACT SAME pattern `index_directory`
        already uses to build `self.call_graphs[repo_id]`: filters
        `self.parse_results` (this engine's own cache, keyed
        `"{repo_id}::{rel_path}"`) down to only this repository's own
        files before constructing `ModuleGraphBuilder` -- isolation
        holds by construction, not by a runtime filter inside the
        analysis itself.

        Computed fresh from the current in-memory snapshot on every
        call when this process has parse results for the repository --
        no incremental-update machinery, no cross-call caching, per this
        milestone's original approved scope. 2026-10-12 (user-approved,
        CODEPULSE_FUTURE_CHANGES.md item 15): the resolved import edges
        are now persisted (`module_edges`), so after a process restart --
        when `self.parse_results` is empty -- the graph restored from
        them at startup is returned instead of an empty one. Returns an
        empty-graph `ModuleGraphBuilder` only when the repository has
        neither (never indexed, or the database is not yet migrated to
        `module_edges`).
        """
        prefix = f"{repo_id}::"
        repo_parsed_files = [
            cached for key, cached in self.parse_results.items() if key.startswith(prefix)
        ]
        if not repo_parsed_files and repo_id in self._restored_module_graphs:
            # No parse results in this process (a restart): use the graph
            # restored from the durable `module_edges` rows.
            return self._restored_module_graphs[repo_id]
        return ModuleGraphBuilder(repo_parsed_files)

    def analyze_references(self, symbol_name: str, file_path: str, repo_id: int) -> dict[str, Any]:
        """
        Phase 4.4 (roadmap §37 item 4.4 "References endpoint"; §21:
        `references` is its own endpoint group, explicitly SEPARATE from
        `graph (callers/callees/impact)`). "References" answers "what
        directly points at this symbol" -- DIRECT usages only, never a
        transitive/depth-bucketed traversal (that concept is
        `analyze_impact`'s, deliberately not reused here so the two
        stay distinct, per this project's own repeated discipline
        against conflating call graph / inheritance graph / impact
        analysis).

        Two direct-reference kinds, both single-hop:
          - `callers`: symbols that directly call this one (`get_callers`
            called with `max_depth=1` -- reuses that method's own
            tested BFS/confidence/deterministic-ordering machinery
            exactly as-is, just capped to one hop, not a new algorithm).
          - `subclasses`: classes that directly list this symbol as one
            of their `base_classes` (`get_direct_subclasses` -- reads
            ONLY the inheritance-specific adjacency, never the calls-only
            one, preserving Phase 4.1's separation invariant. This is
            inheritance data's first real query surface -- extracted and
            resolved since Phase 4.1, explicitly excluded from impact
            analysis by Phase 4.2's Decision 1, and finally exposed
            here).

        Repository-scoped by the EXACT SAME `self.call_graphs.get(repo_id)`
        pattern `analyze_impact`/`analyze_module_structure` already use.
        Computed fresh from the current in-memory snapshot on every call
        -- no persistence, no incremental machinery. A cold cache (never
        indexed this process, or after a restart) degrades to empty
        lists, never an error.
        """
        repo_call_graph = self.call_graphs.get(repo_id)
        if repo_call_graph:
            callers = repo_call_graph.get_callers(symbol_name, file_path, max_depth=1)
            subclasses = repo_call_graph.get_direct_subclasses(symbol_name, file_path)
        else:
            callers, subclasses = [], []

        return {
            "symbol": symbol_name,
            "callers": callers,
            "subclasses": subclasses,
        }

    def find_similar_symbols(
        self, query_text: str, exclude_symbol_name: str, repo_id: int,
        threshold: float = 0.75, limit: int = 10,
    ) -> list[dict[str, Any]]:
        """
        Phase 5.1 (roadmap §12 "related_symbols"). Shared intelligence-
        core semantic-similarity lookup -- extracted from what was
        previously inline-only logic in `POST /v1/symbols/{id}/similar`,
        so the new §12 context-bundle assembler (`get_context_bundle`)
        can reuse it instead of duplicating vector-search logic a
        second time.

        Fixed a real, pre-existing repository-isolation gap while
        extracting this: the original inline route logic called
        `self.vector_index.search(...)` with NO `repo_id` filtering at
        all (unlike `IndexingEngine.search()`'s own "semantic" branch,
        repo-scoped since Phase 0.6) -- a caller could receive another
        repository's "similar" symbols. This method filters candidates
        by `meta.get("repo_id") == repo_id`, over-fetching the entire
        vector index pool first (`top_k=len(self.vector_index.doc_ids)`)
        exactly like `search()`'s own established `_repo_filter`
        pattern, rather than trusting a small `top_k` to happen to
        contain enough same-repository results.
        """
        query_vec = self.embedder.encode(query_text)
        pool = len(self.vector_index.doc_ids)
        vector_res = self.vector_index.search(query_vec, top_k=pool) if pool else []

        similar_items: list[dict[str, Any]] = []
        for r in vector_res:
            meta = r.get("metadata", {})
            if meta.get("repo_id") != repo_id:
                continue
            if meta.get("symbol_name") == exclude_symbol_name:
                continue
            if r["relevance_score"] >= threshold:
                similar_items.append({
                    "symbol_name": meta.get("symbol_name"),
                    "file_path": meta.get("file_path"),
                    "similarity": round(r["similarity"], 4),
                    "relevance_score": round(r["relevance_score"], 4),
                })
            if len(similar_items) >= limit:
                break
        return similar_items

    def get_context_bundle(
        self,
        symbol_name: str,
        file_path: str,
        repo_id: int,
        signature: str = "",
        docstring: str | None = None,
        body: str = "",
        include_body: bool = False,
        max_related: int = 10,
    ) -> dict[str, Any]:
        """
        Phase 5.1 (roadmap §12 "Agent-Native Retrieval / Context API"):
        the shared context-bundle assembly function §12 itself requires
        be "shared by REST + MCP" -- `POST /v1/agent/context` (Phase
        5.2) and the MCP `get_context` tool (Phase 5.3) both call this
        directly; neither duplicates the assembly logic.

        Composes three ALREADY-EXISTING, independently-tested
        capabilities -- no new graph/retrieval algorithm is introduced
        here, purely assembly: `CallGraphBuilder.get_callers`/
        `get_callees` (reused with `max_depth=1`, exactly like
        `analyze_references` already does, for `callers`/`callees`) and
        `find_similar_symbols` (above) for `related_symbols`.

        Token-efficiency rules, implemented exactly per §12's own
        numbered list: (1) `definition.body` is `None` unless
        `include_body=True` -- signature+docstring are always included,
        the full body only on explicit request; (2) `callers`/`callees`
        are capped at `max_related` (default 10), already
        deterministically ordered by confidence then call-count (Phase
        4.2's own established sort, not a new ranking); (3)
        `snapshot_commit` is always present -- the most recent
        `RepositorySnapshotModel.commit_sha` for this repository, `None`
        (honestly, never fabricated) for a non-git repository or one
        never successfully indexed; (4) `truncated` is `True` whenever
        anything was shortened (the body was omitted, or `callers`/
        `callees` had more entries than `max_related`), so a caller/
        agent always knows when a field was silently capped.

        Repository-scoped via `self.call_graphs.get(repo_id)`, matching
        every other `analyze_*` method's own established pattern --
        never the singular `self.call_graph` alias.
        """
        repo_call_graph = self.call_graphs.get(repo_id)
        if repo_call_graph:
            all_callers = repo_call_graph.get_callers(symbol_name, file_path, max_depth=1)
            all_callees = repo_call_graph.get_callees(symbol_name, file_path, max_depth=1)
        else:
            all_callers, all_callees = [], []

        callers = all_callers[:max_related]
        callees = all_callees[:max_related]

        query_text = f"{signature}\n{docstring or ''}\n{body[:500]}"
        related_raw = self.find_similar_symbols(query_text, symbol_name, repo_id, limit=max_related)
        related_symbols = [
            {
                "symbol": r["symbol_name"],
                "file": r["file_path"],
                "relevance": r["relevance_score"],
                "reason": "semantic_similarity",
            }
            for r in related_raw
        ]

        session = self.storage.get_session()
        try:
            snapshot = (
                session.query(RepositorySnapshotModel)
                .filter(RepositorySnapshotModel.repo_id == repo_id)
                .order_by(RepositorySnapshotModel.id.desc())
                .first()
            )
            snapshot_commit = snapshot.commit_sha if snapshot else None
        finally:
            session.close()

        truncated = (
            (not include_body)
            or (len(all_callers) > max_related)
            or (len(all_callees) > max_related)
        )

        return {
            "symbol": symbol_name,
            "file": file_path,
            "definition": {
                "signature": signature,
                "docstring": docstring,
                "body": body if include_body else None,
            },
            "callers": callers,
            "callees": callees,
            "related_symbols": related_symbols,
            "snapshot_commit": snapshot_commit,
            "truncated": truncated,
        }
