"""
Phase 3.1/3.2 — Git-diff based incremental indexing, including rename/
move detection + symbol re-keying, engine-level integration tests
(roadmap §37 items 3.1 and 3.2; §9 Incremental Indexing Design).

Unit-level coverage for the individual new primitives already exists
elsewhere and is NOT duplicated here:
  - `codepulse/git_diff.py` itself, INCLUDING rename detection: tests/test_git_diff.py
  - `BM25Index.remove_docs`: tests/test_tantivy_lexical_index.py
  - `VectorIndex.remove_docs` + the monotonic-rowid fix: tests/test_sqlite_vec_semantic_index.py

This file exercises `IndexingEngine.index_directory()` end-to-end against
real, temporary git repositories (created fresh under `tmp_path` via the
real `git` binary), proving the actual wiring: `_plan_incremental_run`,
the `all_files` filtering, `_purge_stale_entries`, `_rekey_renamed_files`,
the call-graph-completeness merge, the corrected (non-fabricated)
`file_count`/`symbol_count`, and `_record_snapshot` all working together
correctly -- covering exactly what unit tests of the pieces in isolation
cannot prove.

Phase 3.2 scope, exactly as audited and confirmed with the user before
implementation (see CODEPULSE_PROGRESS.md): file-level re-keying only.
A PURE rename (no content change) fully preserves every `File`/`Symbol`/
`Embedding` id, verified directly below. A rename that ALSO edits
content such that a symbol's own `start_line` shifts is NOT specially
handled -- that symbol falls back to today's existing, pre-existing
Phase 0.4 behavior (a fresh id), exactly as an ordinary modified file
already does; this is a deliberate, disclosed scope boundary, not a gap
introduced by this milestone.

Scope boundary (unchanged from the rest of Phase 3): symbol-level
body_hash skip-embedding (3.3), call-graph confidence scoring (3.4), and
local BFS incremental graph updates (3.5) are NOT implemented and NOT
tested here.

All tests use temporary, disposable SQLite databases and temporary git
repositories under `tmp_path` -- never the production codepulse.db.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from codepulse.config import reset_settings
from codepulse.embeddings.embedder import CodeEmbedder
from codepulse.git_diff import get_head_commit_sha
from codepulse.indexer.engine import IndexingEngine
from codepulse.storage.db import (
    EmbeddingModel,
    FileModel,
    RepositoryModel,
    RepositorySnapshotModel,
    StorageManager,
    SymbolModel,
)


@pytest.fixture(autouse=True)
def cleanup_settings():
    reset_settings()
    yield
    reset_settings()


@pytest.fixture(scope="module")
def shared_embedder():
    """One real CodeEmbedder instance reused across tests in this module
    (same rationale as test_restart_idempotency.py's identical fixture --
    load the real SentenceTransformer model once, not per test)."""
    return CodeEmbedder()


def _git(repo_path, *args) -> None:
    subprocess.run(["git", *args], cwd=str(repo_path), check=True, capture_output=True, text=True)


def _init_repo(repo_path) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    _git(repo_path, "init", "-q")
    _git(repo_path, "config", "user.email", "test@codepulse.local")
    _git(repo_path, "config", "user.name", "CodePulse Test")


def _commit_all(repo_path, message: str) -> str:
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-q", "-m", message)
    return get_head_commit_sha(str(repo_path))


def _write(repo_path, rel_path: str, content: str) -> None:
    full = repo_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")


def _new_engine(db_url: str, embedder: CodeEmbedder) -> IndexingEngine:
    storage = StorageManager(db_url=db_url)
    return IndexingEngine(storage=storage, embedder=embedder)


def _db_url(tmp_path, name: str) -> str:
    db_path = tmp_path / name
    return f"sqlite:///{str(db_path).replace(os.sep, '/')}"


ALPHA = 'def alpha():\n    """First function, alpha."""\n    return 1\n'
BETA = 'def beta():\n    """Second function, beta."""\n    return 2\n'
GAMMA = 'def gamma():\n    """Third function, gamma."""\n    return 3\n'


# --- 1. Baseline: first run is always full, second run with no changes -----
# ---    is a real, valid, empty-changeset incremental run ------------------

def test_first_index_is_full_second_run_with_no_changes_is_empty_incremental(tmp_path, shared_embedder):
    repo = tmp_path / "repo_baseline"
    _init_repo(repo)
    _write(repo, "main.py", ALPHA)
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "baseline.db"), shared_embedder)

    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result1["incremental"] is False
    assert "no prior successful git-backed snapshot" in result1["incremental_reason"]
    assert result1["total_files"] == 1

    result2 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result2["incremental"] is True
    assert "no changes since the last indexed commit" in result2["incremental_reason"]
    assert result2["total_files"] == 0, "an empty changeset must not re-parse anything"
    assert result2["deleted_files"] == []


# --- 2. Added / modified / deleted are all detected and handled correctly --

def test_incremental_run_handles_added_modified_and_deleted_files_together(tmp_path, shared_embedder):
    repo = tmp_path / "repo_amd"
    _init_repo(repo)
    _write(repo, "keep.py", ALPHA)
    _write(repo, "keep2.py", 'def k2():\n    """Keep2."""\n    return 21\n')
    _write(repo, "keep3.py", 'def k3():\n    """Keep3."""\n    return 22\n')
    _write(repo, "keep4.py", 'def k4():\n    """Keep4."""\n    return 23\n')
    _write(repo, "keep5.py", 'def k5():\n    """Keep5."""\n    return 24\n')
    _write(repo, "change_me.py", BETA)
    _write(repo, "remove_me.py", GAMMA)
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "amd.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result1["total_files"] == 7
    repo_id = result1["repository_id"]
    # 2 of 7 previously-indexed files change below (modified + deleted;
    # the added file isn't counted against the prior total) --
    # comfortably under the default 50% incremental threshold.

    # added
    _write(repo, "added.py", 'def delta():\n    """Fourth function, delta."""\n    return 4\n')
    # modified (content AND symbol name change, so the old symbol truly
    # disappears rather than just being updated in place)
    _write(repo, "change_me.py", 'def beta_v2():\n    """Second function, beta, now renamed to beta_v2."""\n    return 22\n')
    # deleted
    os.remove(repo / "remove_me.py")
    _commit_all(repo, "amd")

    result2 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result2["incremental"] is True
    assert result2["total_files"] == 2, "only added.py and change_me.py should be reparsed"
    assert result2["deleted_files"] == ["remove_me.py"]

    session = engine.storage.get_session()
    try:
        paths = {f.path for f in session.query(FileModel).filter(FileModel.repo_id == repo_id).all()}
        assert paths == {
            "keep.py", "keep2.py", "keep3.py", "keep4.py", "keep5.py", "change_me.py", "added.py",
        }, "remove_me.py's row must be gone"

        symbol_names = {
            s.name
            for s in session.query(SymbolModel)
            .join(FileModel, SymbolModel.file_id == FileModel.id)
            .filter(FileModel.repo_id == repo_id)
            .all()
        }
        assert symbol_names == {"alpha", "k2", "k3", "k4", "k5", "beta_v2", "delta"}, (
            "stale 'beta' symbol must be gone, replaced by 'beta_v2'"
        )
    finally:
        session.close()

    # Lexical index: exactly 7 live documents -- NOT 8 (which would mean
    # the stale 'beta' doc_id was never purged).
    assert engine.bm25_index.num_docs == 7

    # keep.py's own symbol must still be findable (untouched by the run).
    keyword_results = engine.search("alpha", limit=5, search_type="keyword", repository_id=repo_id)
    assert any(r.symbol_name == "alpha" for r in keyword_results)

    # The deleted file's symbol must not be findable anymore.
    all_keyword = engine.search("gamma", limit=5, search_type="keyword", repository_id=repo_id)
    assert not any(r.symbol_name == "gamma" for r in all_keyword)


# --- 3. Unchanged files are not re-parsed or re-embedded --------------------

def test_unchanged_files_are_not_reparsed_or_reembedded(tmp_path, shared_embedder):
    repo = tmp_path / "repo_skip"
    _init_repo(repo)
    _write(repo, "stable.py", ALPHA)
    _write(repo, "changing.py", BETA)
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "skip.db"), shared_embedder)
    engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))

    _write(repo, "changing.py", 'def beta():\n    """Second function, beta, modified body."""\n    return 200\n')
    _commit_all(repo, "modify changing.py only")

    call_count = {"n": 0}
    original_encode = engine.embedder.encode

    def _spy_encode(texts, *args, **kwargs):
        call_count["n"] += 1
        assert not any("First function, alpha" in t for t in texts), (
            "stable.py's unchanged content must never be re-submitted for embedding"
        )
        return original_encode(texts, *args, **kwargs)

    engine.embedder.encode = _spy_encode  # type: ignore[method-assign]
    try:
        result = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    finally:
        engine.embedder.encode = original_encode  # type: ignore[method-assign]

    assert result["incremental"] is True
    assert result["total_files"] == 1
    assert call_count["n"] >= 1, "changing.py should still be embedded"


# --- 4. RepositoryModel.file_count / symbol_count reflect TRUE totals ------
# ---    after an incremental run, not just this run's parsed subset --------

def test_file_and_symbol_counts_reflect_true_totals_after_incremental_run(tmp_path, shared_embedder):
    repo = tmp_path / "repo_counts"
    _init_repo(repo)
    _write(repo, "a.py", ALPHA)
    _write(repo, "b.py", BETA)
    _write(repo, "c.py", GAMMA)
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "counts.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]

    _write(repo, "a.py", 'def alpha():\n    """First function, alpha, modified."""\n    return 100\n')
    _commit_all(repo, "modify a.py only")

    result2 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result2["incremental"] is True
    assert result2["total_files"] == 1, "only a.py should have been reparsed this run"

    session = engine.storage.get_session()
    try:
        repo_row = session.query(RepositoryModel).filter(RepositoryModel.id == repo_id).first()
        assert repo_row.file_count == 3, (
            "file_count must reflect the TRUE total (3 files), not just this "
            f"run's parsed subset (1) -- got {repo_row.file_count}"
        )
        assert repo_row.symbol_count == 3, (
            "symbol_count must reflect the TRUE total (3 symbols), not just this "
            f"run's parsed subset -- got {repo_row.symbol_count}"
        )
    finally:
        session.close()


# --- 5. Call graph remains complete across an incremental run --------------

def test_call_graph_remains_complete_across_an_incremental_run(tmp_path, shared_embedder):
    repo = tmp_path / "repo_callgraph"
    _init_repo(repo)
    _write(
        repo,
        "callers.py",
        'def helper():\n    """A helper function."""\n    return 1\n\n\n'
        'def caller():\n    """Calls helper."""\n    return helper()\n',
    )
    _write(repo, "unrelated.py", ALPHA)
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "callgraph.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]
    edges_full = len(engine.call_graphs[repo_id].edges)
    assert edges_full >= 1, "the caller->helper edge must exist after the full run"

    # Modify a completely unrelated file -- callers.py (source of the edge)
    # is untouched, so it will NOT be reparsed this run.
    _write(repo, "unrelated.py", 'def alpha():\n    """First function, alpha, modified."""\n    return 999\n')
    _commit_all(repo, "modify unrelated.py only")

    result2 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result2["incremental"] is True
    assert result2["total_files"] == 1

    edges_incremental = len(engine.call_graphs[repo_id].edges)
    assert edges_incremental == edges_full, (
        "the call graph must remain complete (the caller->helper edge from "
        "the untouched callers.py must not vanish) after an incremental run "
        f"-- expected {edges_full} edges, got {edges_incremental}"
    )


# --- 6. Fallback conditions: each forces a full re-index, with a real reason -

def test_falls_back_to_full_reindex_when_not_a_git_repository(tmp_path, shared_embedder):
    repo = tmp_path / "repo_no_git"
    repo.mkdir()
    (repo / "main.py").write_text(ALPHA, encoding="utf-8")

    engine = _new_engine(_db_url(tmp_path, "no_git.db"), shared_embedder)
    result = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result["incremental"] is False
    assert "not a git repository" in result["incremental_reason"]


def test_falls_back_to_full_reindex_when_embedding_model_changed(tmp_path, shared_embedder):
    repo = tmp_path / "repo_model_change"
    _init_repo(repo)
    _write(repo, "main.py", ALPHA)
    _commit_all(repo, "initial")

    db_url = _db_url(tmp_path, "model_change.db")
    engine1 = _new_engine(db_url, shared_embedder)
    engine1.index_directory(str(repo), repo_name="r", repo_url=str(repo))

    _write(repo, "main.py", 'def alpha():\n    """First function, alpha, modified."""\n    return 100\n')
    _commit_all(repo, "modify main.py")

    # Simulate a changed embedding configuration by recording a fabricated
    # snapshot row with a different model name, then re-indexing with the
    # real (different) model -- exercising the comparison directly rather
    # than actually loading a second real embedding model (slow, and
    # duplicative of what the model/dimension comparison itself needs).
    session = engine1.storage.get_session()
    try:
        repo_row = session.query(RepositoryModel).filter(RepositoryModel.url == str(repo)).first()
        snap = (
            session.query(RepositorySnapshotModel)
            .filter(RepositorySnapshotModel.repo_id == repo_row.id)
            .order_by(RepositorySnapshotModel.id.desc())
            .first()
        )
        snap.embedding_model = "some-other-model-entirely"
        session.commit()
    finally:
        session.close()

    result = engine1.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result["incremental"] is False
    assert "embedding model/dimension changed" in result["incremental_reason"]


def test_falls_back_to_full_reindex_when_changed_fraction_exceeds_threshold(tmp_path, shared_embedder, monkeypatch):
    repo = tmp_path / "repo_threshold"
    _init_repo(repo)
    _write(repo, "a.py", ALPHA)
    _write(repo, "b.py", BETA)
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "threshold.db"), shared_embedder)
    engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))

    # Change both files (2 of 2 = 100% > the default 50% threshold).
    _write(repo, "a.py", 'def alpha():\n    """Modified."""\n    return 100\n')
    _write(repo, "b.py", 'def beta():\n    """Modified."""\n    return 200\n')
    _commit_all(repo, "modify both")

    result = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result["incremental"] is False
    assert "exceeding" in result["incremental_reason"]
    assert result["total_files"] == 2, "a full re-index must still process every file"


def test_incremental_indexing_disabled_via_config_forces_full_reindex(tmp_path, shared_embedder, monkeypatch):
    repo = tmp_path / "repo_disabled"
    _init_repo(repo)
    _write(repo, "main.py", ALPHA)
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "disabled.db"), shared_embedder)
    engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))

    _write(repo, "main.py", 'def alpha():\n    """Modified."""\n    return 100\n')
    _commit_all(repo, "modify")

    monkeypatch.setenv("CODEPULSE_INCREMENTAL_INDEXING_ENABLED", "false")
    reset_settings()

    result = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result["incremental"] is False
    assert "disabled via config" in result["incremental_reason"]


def test_falls_back_to_full_reindex_after_a_fresh_engine_with_no_cached_call_graph(tmp_path, shared_embedder):
    """A process-restart-like scenario (Restart Model B, per
    test_restart_idempotency.py's own convention): a brand-new engine has
    no in-memory `call_graphs` cache for this repo_id even though a valid
    prior snapshot exists in the database -- must still force a full run
    (see `_plan_incremental_run`'s own guard and docstring)."""
    repo = tmp_path / "repo_restart_incremental"
    _init_repo(repo)
    _write(repo, "main.py", ALPHA)
    _commit_all(repo, "initial")

    db_url = _db_url(tmp_path, "restart_incremental.db")
    engine1 = _new_engine(db_url, shared_embedder)
    engine1.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    engine1.storage.engine.dispose()

    _write(repo, "main.py", 'def alpha():\n    """Modified."""\n    return 100\n')
    _commit_all(repo, "modify")

    engine2 = _new_engine(db_url, shared_embedder)
    result = engine2.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result["incremental"] is False
    assert "no in-memory call graph cached" in result["incremental_reason"]
    assert result["total_files"] == 1, "a full re-index after restart must still find and process the one real file"


# --- 7. A RepositorySnapshot row is recorded after every successful run ----

def test_repository_snapshot_recorded_after_full_and_incremental_runs(tmp_path, shared_embedder):
    repo = tmp_path / "repo_snapshot"
    _init_repo(repo)
    _write(repo, "main.py", ALPHA)
    _write(repo, "extra1.py", BETA)
    _write(repo, "extra2.py", GAMMA)
    commit1 = _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "snapshot.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]

    # Only main.py changes (1 of 3 previously-indexed files -- under the
    # default 50% incremental threshold).
    _write(repo, "main.py", 'def alpha():\n    """Modified."""\n    return 100\n')
    commit2 = _commit_all(repo, "modify")

    result2 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result2["incremental"] is True

    session = engine.storage.get_session()
    try:
        snapshots = (
            session.query(RepositorySnapshotModel)
            .filter(RepositorySnapshotModel.repo_id == repo_id)
            .order_by(RepositorySnapshotModel.id.asc())
            .all()
        )
        assert len(snapshots) == 2
        assert snapshots[0].commit_sha == commit1
        assert snapshots[1].commit_sha == commit2
        for snap in snapshots:
            assert snap.embedding_model == engine.embedder.model_name
            assert snap.embedding_dim == engine.embedder.dimension
    finally:
        session.close()


# --- Phase 3.2: rename/move detection + symbol re-keying --------------------

def test_pure_rename_preserves_file_symbol_and_embedding_ids(tmp_path, shared_embedder):
    repo = tmp_path / "repo_pure_rename"
    _init_repo(repo)
    _write(repo, "old_name.py", ALPHA)
    _write(repo, "keep.py", BETA)
    _write(repo, "keep2.py", GAMMA)
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "pure_rename.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]

    session = engine.storage.get_session()
    try:
        fm_before = session.query(FileModel).filter(
            FileModel.repo_id == repo_id, FileModel.path == "old_name.py"
        ).first()
        file_id_before = fm_before.id
        sym_before = session.query(SymbolModel).filter(SymbolModel.file_id == file_id_before).first()
        symbol_id_before = sym_before.id
        emb_before = session.query(EmbeddingModel).filter(EmbeddingModel.symbol_id == symbol_id_before).first()
        embedding_id_before = emb_before.id if emb_before else None
    finally:
        session.close()

    (repo / "old_name.py").rename(repo / "new_name.py")
    _commit_all(repo, "rename old_name.py to new_name.py")

    result2 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result2["incremental"] is True
    assert result2["renamed_files"] == [{"old": "old_name.py", "new": "new_name.py"}]

    session = engine.storage.get_session()
    try:
        # The row moved, not a delete+recreate -- no row left at the old path.
        assert session.query(FileModel).filter(
            FileModel.repo_id == repo_id, FileModel.path == "old_name.py"
        ).first() is None

        fm_after = session.query(FileModel).filter(
            FileModel.repo_id == repo_id, FileModel.path == "new_name.py"
        ).first()
        assert fm_after is not None
        assert fm_after.id == file_id_before, "File.id must be preserved across a pure rename"

        sym_after = session.query(SymbolModel).filter(SymbolModel.file_id == fm_after.id).first()
        assert sym_after is not None
        assert sym_after.id == symbol_id_before, "Symbol.id must be preserved across a pure rename"
        assert sym_after.name == "alpha"

        if embedding_id_before is not None:
            emb_after = session.query(EmbeddingModel).filter(EmbeddingModel.symbol_id == sym_after.id).first()
            assert emb_after is not None
            assert emb_after.id == embedding_id_before, "Embedding.id must be preserved across a pure rename"
    finally:
        session.close()

    # Repository-wide totals must reflect reality: still 3 files, unaffected
    # by the rename (not fewer, not more).
    session = engine.storage.get_session()
    try:
        repo_row = session.query(RepositoryModel).filter(RepositoryModel.id == repo_id).first()
        assert repo_row.file_count == 3
    finally:
        session.close()


def test_pure_rename_moves_search_results_to_the_new_path(tmp_path, shared_embedder):
    repo = tmp_path / "repo_rename_search"
    _init_repo(repo)
    _write(repo, "old_name.py", ALPHA)
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "rename_search.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]

    before = engine.search("alpha", limit=5, search_type="keyword", repository_id=repo_id)
    assert any(r.file_path == "old_name.py" for r in before)

    (repo / "old_name.py").rename(repo / "new_name.py")
    _commit_all(repo, "rename")
    engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))

    after = engine.search("alpha", limit=5, search_type="keyword", repository_id=repo_id)
    assert len(after) >= 1
    assert all(r.file_path != "old_name.py" for r in after), "no result must still reference the old path"
    assert any(r.file_path == "new_name.py" for r in after)
    for r in after:
        assert not r.doc_id.startswith(f"{repo_id}::old_name.py::"), "stale old-path doc_id must be gone"


def test_rename_with_a_content_edit_updates_the_matched_symbol_in_place(tmp_path, shared_embedder):
    repo = tmp_path / "repo_rename_edit"
    _init_repo(repo)
    _write(repo, "old_name.py", ALPHA)
    _write(repo, "keep.py", BETA)
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "rename_edit.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]

    session = engine.storage.get_session()
    try:
        fm_before = session.query(FileModel).filter(
            FileModel.repo_id == repo_id, FileModel.path == "old_name.py"
        ).first()
        sym_before = session.query(SymbolModel).filter(SymbolModel.file_id == fm_before.id).first()
        symbol_id_before = sym_before.id
    finally:
        session.close()

    # Rename AND edit the symbol's body -- name and start_line both stay
    # the same, only the returned value changes.
    (repo / "old_name.py").rename(repo / "new_name.py")
    (repo / "new_name.py").write_text(
        'def alpha():\n    """First function, alpha."""\n    return 999\n', encoding="utf-8"
    )
    _commit_all(repo, "rename with edit")

    result2 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result2["renamed_files"] == [{"old": "old_name.py", "new": "new_name.py"}]

    session = engine.storage.get_session()
    try:
        fm_after = session.query(FileModel).filter(
            FileModel.repo_id == repo_id, FileModel.path == "new_name.py"
        ).first()
        sym_after = session.query(SymbolModel).filter(SymbolModel.file_id == fm_after.id).first()
        assert sym_after.id == symbol_id_before, "matched symbol (same name, same start_line) keeps its id"
        assert "999" in sym_after.body, "the symbol's content must actually be updated, not just re-keyed"
    finally:
        session.close()


def test_rekey_renamed_files_is_a_safe_no_op_when_the_old_path_was_never_indexed(tmp_path, shared_embedder):
    """Defensive fallback (matches the roadmap's own risk-register
    mitigation: fall back to delete+recreate rather than a risky guess).
    `old_name.py` is tracked by git (so git still reports the rename) but
    was never actually indexed by this engine (excluded by
    `.codepulseignore`), so there is no existing File row to re-key --
    `_rekey_renamed_files` must skip it cleanly and `new_name.py` must
    still be indexed normally as a fresh file."""
    repo = tmp_path / "repo_rename_no_prior_row"
    _init_repo(repo)
    _write(repo, ".codepulseignore", "old_name.py\n")
    _write(repo, "old_name.py", ALPHA)
    _write(repo, "keep.py", BETA)
    _write(repo, "keep2.py", 'def k2():\n    """Keep2."""\n    return 21\n')
    _write(repo, "keep3.py", 'def k3():\n    """Keep3."""\n    return 22\n')
    _commit_all(repo, "initial")
    # Only keep.py/keep2.py/keep3.py count toward file_count (old_name.py
    # is ignored) -- the single rename below (1 of 3) stays comfortably
    # under the default 50% incremental threshold.

    engine = _new_engine(_db_url(tmp_path, "rename_no_prior_row.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]

    session = engine.storage.get_session()
    try:
        assert session.query(FileModel).filter(
            FileModel.repo_id == repo_id, FileModel.path == "old_name.py"
        ).first() is None, "old_name.py must never have been indexed (ignored)"
    finally:
        session.close()

    (repo / "old_name.py").rename(repo / "new_name.py")
    _commit_all(repo, "rename an ignored file")

    result2 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result2["renamed_files"] == [{"old": "old_name.py", "new": "new_name.py"}]

    session = engine.storage.get_session()
    try:
        fm = session.query(FileModel).filter(
            FileModel.repo_id == repo_id, FileModel.path == "new_name.py"
        ).first()
        assert fm is not None, "new_name.py must be indexed normally despite the failed re-key"
        sym = session.query(SymbolModel).filter(SymbolModel.file_id == fm.id).first()
        assert sym is not None and sym.name == "alpha"
    finally:
        session.close()


def test_call_graph_reflects_the_new_path_after_a_rename(tmp_path, shared_embedder):
    repo = tmp_path / "repo_rename_callgraph"
    _init_repo(repo)
    _write(
        repo,
        "old_name.py",
        'def helper():\n    """A helper."""\n    return 1\n\n\n'
        'def caller():\n    """Calls helper."""\n    return helper()\n',
    )
    _write(repo, "keep.py", BETA)
    _write(repo, "keep2.py", GAMMA)
    _commit_all(repo, "initial")
    # 3 files total; renaming 1 of them stays under the 50% threshold.

    engine = _new_engine(_db_url(tmp_path, "rename_callgraph.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]
    edges_before = len(engine.call_graphs[repo_id].edges)
    assert edges_before >= 1

    (repo / "old_name.py").rename(repo / "new_name.py")
    _commit_all(repo, "rename")
    result2 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result2["incremental"] is True

    edges_after = engine.call_graphs[repo_id].edges
    assert len(edges_after) == edges_before
    assert all(e.source_file != "old_name.py" and e.target_file != "old_name.py" for e in edges_after), (
        "the in-memory call graph must reflect the renamed file's new path, not the old one"
    )


def test_renamed_file_and_an_unrelated_addition_together(tmp_path, shared_embedder):
    """A rename combined with an ordinary add in the same commit -- proves
    `changed_relpaths`/`_purge_stale_entries` correctly separate the two
    (the rename's new path must NOT be purged as if it were a plain
    modified file -- see the ordering fix in `index_directory`)."""
    repo = tmp_path / "repo_rename_plus_add"
    _init_repo(repo)
    _write(repo, "old_name.py", ALPHA)
    _write(repo, "keep.py", BETA)
    _write(repo, "keep2.py", GAMMA)
    _write(repo, "keep3.py", 'def k3():\n    """Keep3."""\n    return 23\n')
    _write(repo, "keep4.py", 'def k4():\n    """Keep4."""\n    return 24\n')
    _commit_all(repo, "initial")
    # 5 files total; rename(1) + add(1, not counted against the prior
    # total) stays under the 50% threshold (1/5 = 20%).

    engine = _new_engine(_db_url(tmp_path, "rename_plus_add.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]
    session = engine.storage.get_session()
    try:
        symbol_id_before = session.query(SymbolModel).join(FileModel).filter(
            FileModel.repo_id == repo_id, FileModel.path == "old_name.py"
        ).first().id
    finally:
        session.close()

    (repo / "old_name.py").rename(repo / "new_name.py")
    _write(repo, "added.py", GAMMA)
    _commit_all(repo, "rename plus add")

    result2 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result2["incremental"] is True
    assert result2["renamed_files"] == [{"old": "old_name.py", "new": "new_name.py"}]
    assert result2["total_files"] == 2, "only new_name.py (renamed) and added.py (new) should be reparsed"

    session = engine.storage.get_session()
    try:
        paths = {f.path for f in session.query(FileModel).filter(FileModel.repo_id == repo_id).all()}
        assert paths == {"new_name.py", "keep.py", "keep2.py", "keep3.py", "keep4.py", "added.py"}
        sym_after = session.query(SymbolModel).join(FileModel).filter(
            FileModel.repo_id == repo_id, FileModel.path == "new_name.py"
        ).first()
        assert sym_after.id == symbol_id_before, "the rename must have re-keyed, not purged, the existing symbol"
    finally:
        session.close()
