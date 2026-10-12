"""
Module structure survives a restart: file-level import edges are persisted
(CODEPULSE_FUTURE_CHANGES.md item 15; user-approved, 2026-10-12).

`GET /v1/repositories/{id}/modules`, the CLI `modules` command, the MCP
`module_structure` tool and the VS Code Module Structure view all read
`IndexingEngine.analyze_module_structure`, which built its graph from
`parse_results` -- an in-memory cache that does not survive a restart. After
any restart a repository's module structure was therefore empty (0 clusters)
until it was re-indexed. Imports were not stored anywhere, so unlike the call
graph (`call_edges`) and the symbol map (`symbols`/`files`) the graph could
not be rebuilt from the database.

This changes Phase 4.3's original approved scope, which said "no
persistence"; the change is user-approved.

What is stored: the RESOLVED file-to-file edges (`module_edges`:
repo_id, source_file, target_file), exactly the edge set
`ModuleGraphBuilder` derives, plus one row with a NULL target for every file
that has no outgoing edge, so the node set is stored too (isolated files are
kept; the restored graph has exactly the files that were parsed, not
whatever `files` rows exist; and "no rows" reliably means "not indexed since
the table existed", which stays cold instead of showing a misleading
all-singletons graph). Raw import statements are not stored. Rows are
replaced wholesale on every index run (full or incremental) from the
complete, merged parse set, so re-indexing is idempotent and an incremental
run can never leave a partial set.

A database that has not been migrated yet (no `module_edges` table, e.g. a
real Alembic-tracked `codepulse.db` before `alembic upgrade head`) must keep
working exactly as before: indexing succeeds, module structure is simply cold
after a restart. The new code must never break indexing there.

Temporary databases only; Restart Model B as in test_restart_idempotency.py.
"""
from __future__ import annotations

import logging
import os
import subprocess

import pytest
from fastapi.testclient import TestClient

import codepulse.api.app as app_module
from codepulse.api.app import app
from codepulse.config import reset_settings
from codepulse.embeddings.embedder import CodeEmbedder
from codepulse.indexer.engine import IndexingEngine
from codepulse.parser.module_graph import ModuleGraphBuilder
from codepulse.storage.db import FileModel, ModuleEdge, RepositoryModel, StorageManager
from codepulse.storage.migrations import run_migrations
from tests.conftest import make_api_key_headers


@pytest.fixture(autouse=True)
def cleanup_settings():
    reset_settings()
    yield
    reset_settings()


@pytest.fixture(scope="module")
def shared_embedder():
    return CodeEmbedder()


# a -> b, a -> c, b -> c ; lonely and empty are isolated nodes (no edges either way).
FILES = {
    "a.py": "from b import beta\nfrom c import gamma\n\n\ndef alpha():\n    return beta() + gamma()\n",
    "b.py": "from c import gamma\n\n\ndef beta():\n    return gamma()\n",
    "c.py": "def gamma():\n    return 1\n",
    "lonely.py": "def lonely():\n    return 0\n",
    "empty.py": "",
}


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(tmp_path, name, files=None, git=False):
    repo = tmp_path / name
    repo.mkdir()
    for rel, content in (files or FILES).items():
        (repo / rel).write_text(content, encoding="utf-8")
    if git:
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@codepulse.local")
        _git(repo, "config", "user.name", "T")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "initial")
    return repo


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _db_url(tmp_path, name):
    return f"sqlite:///{str(tmp_path / name).replace(os.sep, '/')}"


def _new_engine(db_url, embedder):
    return IndexingEngine(storage=StorageManager(db_url=db_url), embedder=embedder)


def _restart(engine, db_url, embedder):
    engine.storage.engine.dispose()
    return _new_engine(db_url, embedder)


def _all_rows(engine, repo_id):
    """Every stored row, including the NULL-target node rows."""
    session = engine.storage.get_session()
    try:
        return sorted(
            ((r.source_file, r.target_file) for r in session.query(ModuleEdge).filter(ModuleEdge.repo_id == repo_id).all()),
            key=lambda row: (row[0], row[1] or ""),
        )
    finally:
        session.close()


def _rows(engine, repo_id):
    """The resolved edges only."""
    return sorted(row for row in _all_rows(engine, repo_id) if row[1] is not None)


def _shape(graph):
    return {
        "files": graph.files,
        "adjacency": {k: sorted(v) for k, v in graph.adjacency.items()},
        "reverse_adjacency": {k: sorted(v) for k, v in graph.reverse_adjacency.items()},
        "in_degree": graph.in_degree,
        "out_degree": graph.out_degree,
        "clusters": graph.clusters,
        "rank_by_inbound": graph.rank_by_inbound(),
        "rank_by_outbound": graph.rank_by_outbound(),
    }


EXPECTED_EDGES = [("a.py", "b.py"), ("a.py", "c.py"), ("b.py", "c.py")]


# --- ModuleGraphBuilder.from_edges ------------------------------------------

def test_from_edges_rebuilds_a_graph_identical_to_the_one_built_from_parse_results(tmp_path, shared_embedder):
    repo = _make_repo(tmp_path, "r1")
    engine = _new_engine(_db_url(tmp_path, "from_edges.db"), shared_embedder)
    repo_id = engine.index_directory(str(repo), repo_name="r1", repo_url=str(repo))["repository_id"]
    fresh = engine.analyze_module_structure(repo_id)
    assert fresh.edges == EXPECTED_EDGES, "fixture sanity: the import edges the test relies on"
    assert {"lonely.py", "empty.py"} <= set(fresh.files), "fixture sanity: isolated nodes exist"

    rebuilt = ModuleGraphBuilder.from_edges(fresh.files, fresh.edges)

    assert _shape(rebuilt) == _shape(fresh)


# --- persistence at index time ----------------------------------------------

def test_index_persists_the_resolved_file_edges(tmp_path, shared_embedder):
    repo = _make_repo(tmp_path, "r2")
    engine = _new_engine(_db_url(tmp_path, "persist.db"), shared_embedder)
    repo_id = engine.index_directory(str(repo), repo_name="r2", repo_url=str(repo))["repository_id"]

    assert _rows(engine, repo_id) == EXPECTED_EDGES


def test_files_without_outgoing_edges_are_stored_as_node_rows(tmp_path, shared_embedder):
    repo = _make_repo(tmp_path, "r2b")
    engine = _new_engine(_db_url(tmp_path, "nodes.db"), shared_embedder)
    repo_id = engine.index_directory(str(repo), repo_name="r2b", repo_url=str(repo))["repository_id"]

    # a.py and b.py have outgoing edges; c.py (a pure target), lonely.py and the empty file do not.
    assert _all_rows(engine, repo_id) == sorted(
        EXPECTED_EDGES + [("c.py", None), ("empty.py", None), ("lonely.py", None)],
        key=lambda row: (row[0], row[1] or ""),
    )


def test_reindexing_is_idempotent_and_never_duplicates_edges(tmp_path, shared_embedder):
    repo = _make_repo(tmp_path, "r3")
    engine = _new_engine(_db_url(tmp_path, "idempotent.db"), shared_embedder)
    repo_id = engine.index_directory(str(repo), repo_name="r3", repo_url=str(repo))["repository_id"]

    rows_after_first = _all_rows(engine, repo_id)
    for _ in range(3):
        engine.index_directory(str(repo), repo_name="r3", repo_url=str(repo))

    assert _rows(engine, repo_id) == EXPECTED_EDGES
    assert _all_rows(engine, repo_id) == rows_after_first


def test_an_incremental_run_persists_the_complete_edge_set_not_just_the_changed_files(tmp_path, shared_embedder):
    repo = _make_repo(tmp_path, "r4", git=True)
    engine = _new_engine(_db_url(tmp_path, "incremental.db"), shared_embedder)
    repo_id = engine.index_directory(str(repo), repo_name="r4", repo_url=str(repo))["repository_id"]

    # Change ONE file (lonely now imports c) and commit.
    (repo / "lonely.py").write_text("from c import gamma\n\n\ndef lonely():\n    return gamma()\n", encoding="utf-8")
    _commit(repo, "lonely imports c")
    result = engine.index_directory(str(repo), repo_name="r4", repo_url=str(repo))

    assert result["incremental"] is True, "fixture sanity: this must exercise the incremental path"
    assert _rows(engine, repo_id) == sorted(EXPECTED_EDGES + [("lonely.py", "c.py")])


def test_a_deleted_file_loses_its_edges_on_the_next_incremental_run(tmp_path, shared_embedder):
    repo = _make_repo(tmp_path, "r5", git=True)
    engine = _new_engine(_db_url(tmp_path, "deleted.db"), shared_embedder)
    repo_id = engine.index_directory(str(repo), repo_name="r5", repo_url=str(repo))["repository_id"]

    (repo / "b.py").unlink()
    (repo / "a.py").write_text("from c import gamma\n\n\ndef alpha():\n    return gamma()\n", encoding="utf-8")
    _commit(repo, "drop b")
    result = engine.index_directory(str(repo), repo_name="r5", repo_url=str(repo))

    assert result["incremental"] is True
    assert _rows(engine, repo_id) == [("a.py", "c.py")]


# --- restart ----------------------------------------------------------------

def test_restart_restores_the_same_module_structure_without_a_reindex(tmp_path, shared_embedder):
    repo = _make_repo(tmp_path, "r6")
    db_url = _db_url(tmp_path, "restart.db")
    engine1 = _new_engine(db_url, shared_embedder)
    repo_id = engine1.index_directory(str(repo), repo_name="r6", repo_url=str(repo))["repository_id"]
    before = _shape(engine1.analyze_module_structure(repo_id))
    assert before["clusters"], "fixture sanity"

    engine2 = _restart(engine1, db_url, shared_embedder)

    assert not engine2.parse_results, "this must be restored from the database, not from a parse cache"
    assert _shape(engine2.analyze_module_structure(repo_id)) == before


def test_the_restored_graph_has_exactly_the_parsed_files_not_stale_files_rows(tmp_path, shared_embedder):
    """The restored node set comes from the stored snapshot, not from `files` rows.

    A full re-index used to leave the `files` rows of deleted files behind
    (FUTURE_CHANGES item 16, now fixed), which is what this test was first
    written around. That path no longer produces stale rows, so one is planted
    directly: a `files` row for a file the snapshot never contained must not
    reappear as a cluster after a restart."""
    repo = _make_repo(tmp_path, "r6b")
    db_url = _db_url(tmp_path, "stale_rows.db")
    engine1 = _new_engine(db_url, shared_embedder)
    repo_id = engine1.index_directory(str(repo), repo_name="r6b", repo_url=str(repo))["repository_id"]
    session = engine1.storage.get_session()
    try:
        session.add(FileModel(
            repo_id=repo_id, path="ghost.py", language="python",
            size_bytes=1, line_count=1, content_hash="0" * 64,
        ))
        session.commit()
    finally:
        session.close()

    engine2 = _restart(engine1, db_url, shared_embedder)

    assert "ghost.py" not in engine2.analyze_module_structure(repo_id).files
    assert engine2.analyze_module_structure(repo_id).files == sorted(FILES)


def test_a_repository_with_no_stored_rows_stays_cold_instead_of_showing_singleton_clusters(tmp_path, shared_embedder):
    """A database migrated to the new table, with a repository indexed BEFORE it
    existed, has no rows for that repository. That must behave like before (empty),
    not as a confident graph in which no file imports anything."""
    repo = _make_repo(tmp_path, "r6c")
    db_url = _db_url(tmp_path, "migrated_not_reindexed.db")
    engine1 = _new_engine(db_url, shared_embedder)
    repo_id = engine1.index_directory(str(repo), repo_name="r6c", repo_url=str(repo))["repository_id"]
    session = engine1.storage.get_session()
    try:
        session.query(ModuleEdge).filter(ModuleEdge.repo_id == repo_id).delete()
        session.commit()
    finally:
        session.close()

    engine2 = _restart(engine1, db_url, shared_embedder)

    graph = engine2.analyze_module_structure(repo_id)
    assert graph.files == [] and graph.clusters == []


def test_a_repository_never_indexed_still_returns_an_empty_graph(tmp_path, shared_embedder):
    engine = _new_engine(_db_url(tmp_path, "never.db"), shared_embedder)

    graph = engine.analyze_module_structure(repo_id=999)

    assert graph.files == [] and graph.clusters == []


def test_after_a_restart_a_new_index_run_replaces_the_restored_graph(tmp_path, shared_embedder):
    repo = _make_repo(tmp_path, "r7")
    db_url = _db_url(tmp_path, "replace.db")
    engine1 = _new_engine(db_url, shared_embedder)
    repo_id = engine1.index_directory(str(repo), repo_name="r7", repo_url=str(repo))["repository_id"]
    engine2 = _restart(engine1, db_url, shared_embedder)

    (repo / "lonely.py").write_text("from c import gamma\n\n\ndef lonely():\n    return gamma()\n", encoding="utf-8")
    engine2.index_directory(str(repo), repo_name="r7", repo_url=str(repo))

    assert ("lonely.py", "c.py") in engine2.analyze_module_structure(repo_id).edges


def test_restored_graphs_are_repository_scoped(tmp_path, shared_embedder):
    repo_a = _make_repo(tmp_path, "ra", {"x.py": "from y import f\n\n\ndef g():\n    return f()\n", "y.py": "def f():\n    return 1\n"})
    repo_b = _make_repo(tmp_path, "rb", {"x.py": "def g():\n    return 1\n", "y.py": "def f():\n    return 2\n"})
    db_url = _db_url(tmp_path, "isolation.db")
    engine1 = _new_engine(db_url, shared_embedder)
    id_a = engine1.index_directory(str(repo_a), repo_name="ra", repo_url=str(repo_a))["repository_id"]
    id_b = engine1.index_directory(str(repo_b), repo_name="rb", repo_url=str(repo_b))["repository_id"]

    engine2 = _restart(engine1, db_url, shared_embedder)

    assert engine2.analyze_module_structure(id_a).edges == [("x.py", "y.py")]
    assert engine2.analyze_module_structure(id_b).edges == []
    assert engine2.analyze_module_structure(id_b).files == ["x.py", "y.py"]
    assert _rows(engine2, id_b) == []


def test_modules_endpoint_returns_clusters_immediately_after_a_restart(tmp_path, shared_embedder, monkeypatch):
    repo = _make_repo(tmp_path, "r8")
    db_url = _db_url(tmp_path, "rest.db")
    engine1 = _new_engine(db_url, shared_embedder)
    repo_id = engine1.index_directory(str(repo), repo_name="r8", repo_url=str(repo))["repository_id"]
    engine2 = _restart(engine1, db_url, shared_embedder)
    monkeypatch.setattr(app_module, "storage_manager", engine2.storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine2)
    headers = make_api_key_headers(engine2.storage, repository_ids=[repo_id])

    response = TestClient(app).get(f"/v1/repositories/{repo_id}/modules", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["clusters"], "module structure must not be empty after a restart"
    assert sorted(f for c in body["clusters"] for f in c["files"]) == sorted(FILES)
    # Without the persisted edges every file would be its own cluster; the import
    # chain a -> b -> c must keep a.py, b.py and c.py together.
    assert any({"a.py", "b.py", "c.py"} <= set(c["files"]) for c in body["clusters"])


def test_deleting_a_repository_removes_its_module_edges(tmp_path, shared_embedder):
    repo = _make_repo(tmp_path, "r9")
    engine = _new_engine(_db_url(tmp_path, "cascade.db"), shared_embedder)
    repo_id = engine.index_directory(str(repo), repo_name="r9", repo_url=str(repo))["repository_id"]
    assert _all_rows(engine, repo_id)

    session = engine.storage.get_session()
    try:
        session.delete(session.get(RepositoryModel, repo_id))
        session.commit()
    finally:
        session.close()

    assert _all_rows(engine, repo_id) == []


# --- a database that has not been migrated yet ------------------------------

@pytest.fixture
def unmigrated_db_url(tmp_path):
    """An Alembic-tracked DB at the previous head: no module_edges table, and
    StorageManager never creates tables on a database Alembic has touched.

    Built in a fixture on purpose: alembic/env.py calls logging.config.fileConfig(),
    which disables every existing logger and resets the root handlers, so running
    it inside the test body would silently remove pytest's log capture."""
    db_url = _db_url(tmp_path, "unmigrated.db")
    run_migrations(db_url=db_url, revision="b9014c049e33")
    logging.getLogger("codepulse.indexer.engine").disabled = False
    return db_url


def test_an_unmigrated_database_still_indexes_and_just_has_a_cold_module_graph(
    tmp_path, shared_embedder, unmigrated_db_url, caplog
):
    db_url = unmigrated_db_url
    repo = _make_repo(tmp_path, "r10")

    engine1 = _new_engine(db_url, shared_embedder)
    with caplog.at_level(logging.WARNING):
        result = engine1.index_directory(str(repo), repo_name="r10", repo_url=str(repo))
    repo_id = result["repository_id"]

    assert result["total_files"] == len(FILES), "indexing must be unaffected"
    assert any("module_edges" in r.getMessage() for r in caplog.records), "the degradation must be logged, not silent"
    assert engine1.analyze_module_structure(repo_id).edges == EXPECTED_EDGES, "in-process it still works from the parse cache"

    engine2 = _restart(engine1, db_url, shared_embedder)
    assert engine2.analyze_module_structure(repo_id).clusters == [], "cold after a restart, exactly as before this feature"
