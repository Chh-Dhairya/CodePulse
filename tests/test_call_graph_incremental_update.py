"""
Phase 3.5 — Local BFS incremental graph update (roadmap §37 item 3.5;
§9: "rather than rebuilding the whole graph, recompute only edges whose
*source* symbol is in the changed set, plus a local BFS one hop out to
catch edges whose *target* changed (a symbol being renamed/deleted must
have inbound edges from unrelated files corrected too)").

Unit-level coverage of `CallGraphBuilder`'s own edge/confidence
computation already exists and is NOT duplicated here: tests/test_graph.py,
tests/test_call_edge_confidence.py. This file exercises
`IndexingEngine._persist_call_graph_edges` end-to-end, against real,
temporary git repositories, via `index_directory()` -- proving the
actual DB-level effect of the incremental update algorithm, which no
in-memory-only test could show.

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
from codepulse.storage.db import CallEdge, StorageManager


@pytest.fixture(autouse=True)
def cleanup_settings():
    reset_settings()
    yield
    reset_settings()


@pytest.fixture(scope="module")
def shared_embedder():
    """One real CodeEmbedder instance reused across tests in this module
    (same rationale as other engine-level test files this session)."""
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


HELPER_AND_CALLER = (
    'def helper():\n    """A helper function."""\n    return 1\n\n\n'
    'def caller():\n    """Calls helper."""\n    return helper()\n'
)


def test_full_reindex_replaces_call_edges_instead_of_accumulating(tmp_path, shared_embedder):
    """The long-disclosed Phase 0.4 gap this milestone closes: a full
    re-index must REPLACE, not duplicate, a repo's CallEdge rows."""
    repo = tmp_path / "repo_full_replace"
    _init_repo(repo)
    _write(repo, "mod.py", HELPER_AND_CALLER)
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "full_replace.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]

    session = engine.storage.get_session()
    try:
        count1 = session.query(CallEdge).filter(CallEdge.repo_id == repo_id).count()
    finally:
        session.close()
    assert count1 >= 1

    # Force a second FULL run (disable incremental entirely) against the
    # exact same, unchanged content.
    import codepulse.config as config_module
    config_module.reset_settings()
    os.environ["CODEPULSE_INCREMENTAL_INDEXING_ENABLED"] = "false"
    try:
        engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    finally:
        del os.environ["CODEPULSE_INCREMENTAL_INDEXING_ENABLED"]
        config_module.reset_settings()

    session = engine.storage.get_session()
    try:
        count2 = session.query(CallEdge).filter(CallEdge.repo_id == repo_id).count()
    finally:
        session.close()
    assert count2 == count1, "a second full re-index must REPLACE edges, not duplicate them"


def test_incremental_run_leaves_an_unrelated_symbols_edges_completely_untouched(tmp_path, shared_embedder):
    """A symbol whose file is neither changed nor a one-hop neighbor of
    anything changed must have its existing CallEdge row(s) left
    exactly as they were -- same row id, not deleted-and-recreated."""
    repo = tmp_path / "repo_untouched"
    _init_repo(repo)
    _write(repo, "mod.py", HELPER_AND_CALLER)
    _write(repo, "unrelated.py", 'def a():\n    """A."""\n    return b()\n\n\ndef b():\n    """B."""\n    return 1\n')
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "untouched.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]

    session = engine.storage.get_session()
    try:
        unrelated_edge_before = session.query(CallEdge).filter(
            CallEdge.repo_id == repo_id, CallEdge.source_symbol_name == "a"
        ).first()
        unrelated_edge_id = unrelated_edge_before.id
    finally:
        session.close()

    # Modify ONLY mod.py -- unrelated.py's a()/b() are untouched by git,
    # and b() is never called from mod.py, so there is no BFS reason to
    # touch unrelated.py's edge either.
    _write(repo, "mod.py", 'def helper():\n    """A helper function, edited."""\n    return 999\n\n\ndef caller():\n    """Calls helper."""\n    return helper()\n')
    _commit_all(repo, "edit mod.py only")
    result2 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result2["incremental"] is True

    session = engine.storage.get_session()
    try:
        unrelated_edge_after = session.query(CallEdge).filter(
            CallEdge.repo_id == repo_id, CallEdge.source_symbol_name == "a"
        ).first()
        assert unrelated_edge_after is not None
        assert unrelated_edge_after.id == unrelated_edge_id, (
            "an unrelated symbol's CallEdge row must be left untouched -- same row, not deleted+recreated"
        )
    finally:
        session.close()


def test_incremental_run_recomputes_a_changed_symbols_own_source_edges(tmp_path, shared_embedder):
    """The direct, source-side half of §9's algorithm: a changed
    symbol's own outgoing calls must be freshly recomputed."""
    repo = tmp_path / "repo_source_recompute"
    _init_repo(repo)
    _write(
        repo, "mod.py",
        'def helper():\n    """A helper."""\n    return 1\n\n\n'
        'def other_helper():\n    """Another helper."""\n    return 2\n\n\n'
        'def caller():\n    """Calls helper."""\n    return helper()\n',
    )
    _write(repo, "keep1.py", 'def k1():\n    return 1\n')
    _write(repo, "keep2.py", 'def k2():\n    return 2\n')
    _commit_all(repo, "initial")
    # 3 files total; modifying only mod.py (1/3 = 33%) stays under the
    # default 50% incremental threshold.

    engine = _new_engine(_db_url(tmp_path, "source_recompute.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]

    # Change caller() to call other_helper() instead of helper().
    _write(
        repo, "mod.py",
        'def helper():\n    """A helper."""\n    return 1\n\n\n'
        'def other_helper():\n    """Another helper."""\n    return 2\n\n\n'
        'def caller():\n    """Calls other_helper now."""\n    return other_helper()\n',
    )
    _commit_all(repo, "change caller's target")
    result2 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result2["incremental"] is True

    session = engine.storage.get_session()
    try:
        caller_edges = session.query(CallEdge).filter(
            CallEdge.repo_id == repo_id, CallEdge.source_symbol_name == "caller"
        ).all()
        target_names = {e.target_symbol_name for e in caller_edges}
        assert target_names == {"other_helper"}, (
            f"caller's edge must be recomputed to reflect its new call target, got {target_names}"
        )
    finally:
        session.close()


def test_local_bfs_one_hop_corrects_an_unrelated_callers_edge_when_the_target_disappears(tmp_path, shared_embedder):
    """The core §9 scenario: `helper()` disappears from `lib.py` (the
    file still exists, just without that symbol). `service.py` (an
    UNCHANGED file this run) still calls `helper()` in its own,
    unmodified source. Its existing CallEdge row must be found via the
    local BFS one hop out and corrected -- not left forever pointing at
    a symbol that no longer exists."""
    repo = tmp_path / "repo_bfs_disappear"
    _init_repo(repo)
    _write(repo, "lib.py", 'def helper():\n    """A helper."""\n    return 1\n\n\ndef keep_me():\n    """Stays."""\n    return 2\n')
    _write(repo, "service.py", 'from lib import helper\n\n\ndef caller():\n    """Calls helper."""\n    return helper()\n')
    _write(repo, "keep1.py", 'def k1():\n    """Keep1."""\n    return 1\n')
    _write(repo, "keep2.py", 'def k2():\n    """Keep2."""\n    return 2\n')
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "bfs_disappear.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]

    session = engine.storage.get_session()
    try:
        before = session.query(CallEdge).filter(
            CallEdge.repo_id == repo_id, CallEdge.source_symbol_name == "caller", CallEdge.target_symbol_name == "helper"
        ).first()
        assert before is not None
        assert before.confidence == "high"
    finally:
        session.close()

    # Remove helper() from lib.py -- lib.py still exists (keep_me stays),
    # so this is the "disappeared within a still-existing file" case.
    # service.py is NOT touched at all this commit.
    _write(repo, "lib.py", 'def keep_me():\n    """Stays."""\n    return 2\n')
    _commit_all(repo, "remove helper() from lib.py")
    result2 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result2["incremental"] is True
    # Only lib.py was reparsed this run -- service.py must NOT have been
    # reparsed, proving the correction comes from the BFS mechanism, not
    # from service.py being in this run's own changed-file set.
    assert result2["total_files"] == 1

    session = engine.storage.get_session()
    try:
        caller_edges = session.query(CallEdge).filter(
            CallEdge.repo_id == repo_id, CallEdge.source_symbol_name == "caller"
        ).all()
        assert len(caller_edges) == 1, "caller's edge to helper must still exist (its own source line is unchanged)"
        corrected = caller_edges[0]
        assert corrected.target_symbol_name == "helper"
        assert corrected.confidence == "unresolved", (
            "the edge must be corrected to 'unresolved' now that helper() no longer exists in lib.py, "
            f"got confidence={corrected.confidence!r}"
        )
    finally:
        session.close()


def test_local_bfs_one_hop_corrects_an_unrelated_callers_edge_after_a_whole_file_deletion(tmp_path, shared_embedder):
    """Same correction, but the target's WHOLE FILE was deleted (the
    `_purge_stale_entries` / `deleted_relpaths` path) rather than just
    one symbol disappearing within a still-existing file."""
    repo = tmp_path / "repo_bfs_delete"
    _init_repo(repo)
    _write(repo, "lib.py", 'def helper():\n    """A helper."""\n    return 1\n')
    _write(repo, "service.py", 'from lib import helper\n\n\ndef caller():\n    """Calls helper."""\n    return helper()\n')
    _write(repo, "keep1.py", 'def k1():\n    """Keep1."""\n    return 1\n')
    _write(repo, "keep2.py", 'def k2():\n    """Keep2."""\n    return 2\n')
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "bfs_delete.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]

    os.remove(repo / "lib.py")
    _commit_all(repo, "delete lib.py entirely")
    result2 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result2["incremental"] is True
    assert result2["deleted_files"] == ["lib.py"]

    session = engine.storage.get_session()
    try:
        caller_edges = session.query(CallEdge).filter(
            CallEdge.repo_id == repo_id, CallEdge.source_symbol_name == "caller"
        ).all()
        assert len(caller_edges) == 1
        assert caller_edges[0].confidence == "unresolved"
    finally:
        session.close()


def test_local_bfs_one_hop_corrects_an_unrelated_callers_edge_after_a_rename(tmp_path, shared_embedder):
    """§9's own literal parenthetical: "a symbol being renamed/deleted
    must have inbound edges from unrelated files corrected too." Here
    `lib.py` (the file `helper()` lives in) is renamed to `library.py`
    -- Phase 3.2's rename mechanism re-keys it, and `_rekey_renamed_
    files` reports `helper()`'s OLD `(lib.py, helper)` identity as
    "removed," which must trigger this same BFS correction for
    service.py's caller() -- an entirely unrelated, UNCHANGED file."""
    repo = tmp_path / "repo_bfs_rename"
    _init_repo(repo)
    _write(repo, "lib.py", 'def helper():\n    """A helper."""\n    return 1\n')
    _write(repo, "service.py", 'from lib import helper\n\n\ndef caller():\n    """Calls helper."""\n    return helper()\n')
    _write(repo, "keep1.py", 'def k1():\n    return 1\n')
    _write(repo, "keep2.py", 'def k2():\n    return 2\n')
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "bfs_rename.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]

    (repo / "lib.py").rename(repo / "library.py")
    _commit_all(repo, "rename lib.py to library.py")
    result2 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result2["incremental"] is True
    assert result2["renamed_files"] == [{"old": "lib.py", "new": "library.py"}]
    # service.py must NOT have been reparsed -- only library.py (the
    # rename's new path) was.
    assert result2["total_files"] == 1

    session = engine.storage.get_session()
    try:
        caller_edges = session.query(CallEdge).filter(
            CallEdge.repo_id == repo_id, CallEdge.source_symbol_name == "caller"
        ).all()
        assert len(caller_edges) == 1, "caller's edge must still exist (its own source line is unchanged)"
        corrected = caller_edges[0]
        assert corrected.target_file != "lib.py", "must not still point at the renamed-away old path"
        assert corrected.confidence == "unresolved", (
            "service.py's own `from lib import helper` is now unresolvable (lib.py no longer "
            f"exists), so this must be 'unresolved', got confidence={corrected.confidence!r}"
        )
    finally:
        session.close()


def test_repeated_incremental_runs_do_not_accumulate_duplicate_edges(tmp_path, shared_embedder):
    """Running the SAME incremental change scenario across several
    index_directory() calls must never grow the CallEdge count for
    symbols that stop changing."""
    repo = tmp_path / "repo_no_dup"
    _init_repo(repo)
    _write(repo, "mod.py", HELPER_AND_CALLER)
    _write(repo, "keep1.py", 'def k1():\n    return 1\n')
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "no_dup.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]

    for i in range(3):
        _write(repo, "mod.py", f'def helper():\n    """Edit {i}."""\n    return {i}\n\n\ndef caller():\n    return helper()\n')
        _commit_all(repo, f"edit {i}")
        engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))

    session = engine.storage.get_session()
    try:
        caller_edges = session.query(CallEdge).filter(
            CallEdge.repo_id == repo_id, CallEdge.source_symbol_name == "caller"
        ).all()
        assert len(caller_edges) == 1, f"expected exactly 1 caller->helper edge after repeated edits, got {len(caller_edges)}"
    finally:
        session.close()


def test_empty_incremental_changeset_does_not_touch_call_edges(tmp_path, shared_embedder):
    repo = tmp_path / "repo_empty_changeset"
    _init_repo(repo)
    _write(repo, "mod.py", HELPER_AND_CALLER)
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "empty_changeset.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]

    session = engine.storage.get_session()
    try:
        before_ids = {e.id for e in session.query(CallEdge).filter(CallEdge.repo_id == repo_id).all()}
    finally:
        session.close()

    result2 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result2["incremental"] is True
    assert result2["total_files"] == 0

    session = engine.storage.get_session()
    try:
        after_ids = {e.id for e in session.query(CallEdge).filter(CallEdge.repo_id == repo_id).all()}
    finally:
        session.close()
    assert after_ids == before_ids, "an empty changeset must not touch any CallEdge row"
