"""
The call graph survives a restart (found by the 2026-10-12 real-VS-Code QA
run; CODEPULSE_FUTURE_CHANGES.md item 13).

Before this change `IndexingEngine.hydrate_all()` restored embeddings and the
keyword index at startup but NOT `call_graphs`, so after any server restart
`analyze_impact` / `analyze_references` silently returned all-empty results
(`direct_callers: 0`) until the repository was re-indexed -- which made the
VS Code extension's CodeLens read "0 callers" for symbols that do have them.
The edges were always durable (`call_edges`, with confidence and
`relationship_type`); they just were not read back.

What these tests pin down:
  - `CallGraphBuilder.from_edges` rebuilds a graph IDENTICAL, in every
    queryable structure, to the one built from parse results.
  - After a restart, impact analysis and references return exactly what the
    pre-restart engine returned, for every symbol.
  - Restoring is repository-scoped (isolation invariant).
  - A restored graph has no parse cache behind it, so an incremental update
    on top of it would silently drop every unchanged file's edges; the
    "force a full run" guard must therefore still fire after a restart, and
    a full run must replace the restored graph and re-enable incremental.
  - Legacy rows (NULL confidence / NULL target file) restore without error.

Restart model is the same as tests/test_restart_idempotency.py (Model B: a
fresh StorageManager + IndexingEngine on the same SQLite file). Temporary
databases only; never the real codepulse.db.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from codepulse.config import reset_settings
from codepulse.embeddings.embedder import CodeEmbedder
from codepulse.indexer.engine import IndexingEngine
from codepulse.parser.graph import CallGraphBuilder
from codepulse.storage.db import CallEdge, FileModel, RepositoryModel, StorageManager, SymbolModel


@pytest.fixture(autouse=True)
def cleanup_settings():
    reset_settings()
    yield
    reset_settings()


@pytest.fixture(scope="module")
def shared_embedder():
    return CodeEmbedder()


# Cross-file calls (import-resolved -> "high"), same-file calls ("low"), an
# unresolved call, and inheritance across files -- every edge kind.
CORE = (
    "def add(a, b):\n    return a + b\n\n\n"
    "def double(x):\n    return add(x, x)\n\n\n"
    "class Shape:\n    pass\n"
)
APP = (
    "from core import add, Shape\n\n\n"
    "def total(values):\n    out = 0\n    for v in values:\n        out = add(out, v)\n    return out\n\n\n"
    "def report(values):\n    print(total(values))\n    return missing_function(values)\n\n\n"
    "class Square(Shape):\n    pass\n"
)


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(tmp_path, name, files, git=False):
    repo = tmp_path / name
    repo.mkdir()
    for rel, content in files.items():
        (repo / rel).write_text(content, encoding="utf-8")
    if git:
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@codepulse.local")
        _git(repo, "config", "user.name", "T")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "initial")
    return repo


def _db_url(tmp_path, name):
    return f"sqlite:///{str(tmp_path / name).replace(os.sep, '/')}"


def _new_engine(db_url, embedder):
    return IndexingEngine(storage=StorageManager(db_url=db_url), embedder=embedder)


def _restart(engine, db_url, embedder):
    engine.storage.engine.dispose()
    return _new_engine(db_url, embedder)


def _symbols(engine, repo_id):
    session = engine.storage.get_session()
    try:
        rows = (
            session.query(SymbolModel.name, FileModel.path)
            .join(FileModel, SymbolModel.file_id == FileModel.id)
            .filter(FileModel.repo_id == repo_id)
            .order_by(FileModel.path, SymbolModel.name)
            .all()
        )
        return [(name, path) for name, path in rows]
    finally:
        session.close()


def _answers(engine, repo_id):
    """Every query surface the restored graph backs, for every symbol."""
    out = {}
    for name, path in _symbols(engine, repo_id):
        out[(path, name)] = (
            engine.analyze_impact(name, path, repo_id, max_depth=3),
            engine.analyze_references(name, path, repo_id),
        )
    return out


def _structures(graph):
    return {
        "edges": sorted(
            (e.source_file, e.source_symbol_name, e.target_file or "", e.target_symbol_name, e.call_count, e.confidence)
            for e in graph.edges
        ),
        "inheritance_edges": sorted(
            (e.source_file, e.source_symbol_name, e.target_file or "", e.target_symbol_name, e.confidence)
            for e in graph.inheritance_edges
        ),
        "adjacency": {k: set(v) for k, v in graph.adjacency.items()},
        "reverse_adjacency": {k: set(v) for k, v in graph.reverse_adjacency.items()},
        "edge_meta": dict(graph.edge_meta),
        "inheritance_adjacency": {k: set(v) for k, v in graph.inheritance_adjacency.items()},
        "reverse_inheritance_adjacency": {k: set(v) for k, v in graph.reverse_inheritance_adjacency.items()},
        "inheritance_edge_meta": dict(graph.inheritance_edge_meta),
    }


# --- CallGraphBuilder.from_edges -------------------------------------------

def test_from_edges_rebuilds_a_graph_identical_to_the_one_built_from_parse_results(tmp_path, shared_embedder):
    repo = _make_repo(tmp_path, "r1", {"core.py": CORE, "app.py": APP})
    engine = _new_engine(_db_url(tmp_path, "from_edges.db"), shared_embedder)
    result = engine.index_directory(str(repo), repo_name="r1", repo_url=str(repo))
    fresh = engine.call_graphs[result["repository_id"]]
    assert fresh.edges and fresh.inheritance_edges, "fixture must exercise both edge kinds"
    assert {e.confidence for e in fresh.edges} >= {"high", "low", "unresolved"}, "fixture must exercise every confidence tier"

    rebuilt = CallGraphBuilder.from_edges(fresh.edges, fresh.inheritance_edges)

    assert _structures(rebuilt) == _structures(fresh)
    for name, path in _symbols(engine, result["repository_id"]):
        assert rebuilt.get_callers(name, path, 3) == fresh.get_callers(name, path, 3)
        assert rebuilt.get_callees(name, path, 3) == fresh.get_callees(name, path, 3)
        assert rebuilt.get_direct_subclasses(name, path) == fresh.get_direct_subclasses(name, path)


# --- restart ----------------------------------------------------------------

def test_restart_restores_the_call_graph_so_impact_and_references_survive(tmp_path, shared_embedder):
    repo = _make_repo(tmp_path, "r2", {"core.py": CORE, "app.py": APP})
    db_url = _db_url(tmp_path, "restart.db")
    engine1 = _new_engine(db_url, shared_embedder)
    result = engine1.index_directory(str(repo), repo_name="r2", repo_url=str(repo))
    repo_id = result["repository_id"]
    before = _answers(engine1, repo_id)
    assert before[("core.py", "add")][0]["direct_callers"] >= 2, "fixture sanity: add has callers"
    assert before[("core.py", "Shape")][1]["subclasses"], "fixture sanity: Shape has a subclass"

    engine2 = _restart(engine1, db_url, shared_embedder)

    assert repo_id in engine2.call_graphs, "the call graph must be restored at startup"
    assert _answers(engine2, repo_id) == before
    assert _structures(engine2.call_graphs[repo_id]) == _structures(engine1.call_graphs[repo_id])


def test_restored_graph_is_scoped_to_its_own_repository(tmp_path, shared_embedder):
    # Same symbol names in both repositories, different callers.
    repo_a = _make_repo(tmp_path, "ra", {"m.py": "def helper():\n    return 1\n\n\ndef only_in_a():\n    return helper()\n"})
    repo_b = _make_repo(tmp_path, "rb", {"m.py": "def helper():\n    return 2\n\n\ndef b_one():\n    return helper()\n\n\ndef b_two():\n    return helper()\n"})
    db_url = _db_url(tmp_path, "isolation.db")
    engine1 = _new_engine(db_url, shared_embedder)
    id_a = engine1.index_directory(str(repo_a), repo_name="ra", repo_url=str(repo_a))["repository_id"]
    id_b = engine1.index_directory(str(repo_b), repo_name="rb", repo_url=str(repo_b))["repository_id"]

    engine2 = _restart(engine1, db_url, shared_embedder)

    callers_a = [c["symbol"] for c in engine2.analyze_impact("helper", "m.py", id_a)["callers"]["direct"]]
    callers_b = [c["symbol"] for c in engine2.analyze_impact("helper", "m.py", id_b)["callers"]["direct"]]
    assert callers_a == ["only_in_a"]
    assert sorted(callers_b) == ["b_one", "b_two"]


def test_legacy_rows_with_null_confidence_and_null_target_file_restore_without_error(tmp_path, shared_embedder):
    repo = _make_repo(tmp_path, "r3", {"m.py": "def a():\n    return 1\n"})
    db_url = _db_url(tmp_path, "legacy.db")
    engine1 = _new_engine(db_url, shared_embedder)
    repo_id = engine1.index_directory(str(repo), repo_name="r3", repo_url=str(repo))["repository_id"]
    session = engine1.storage.get_session()
    try:
        session.add(CallEdge(
            repo_id=repo_id, source_symbol_name="a", source_file="m.py",
            target_symbol_name="legacy_target", target_file=None,
            call_count=1, confidence=None, relationship_type="calls",
        ))
        session.commit()
    finally:
        session.close()

    engine2 = _restart(engine1, db_url, shared_embedder)

    callees = engine2.analyze_impact("a", "m.py", repo_id)["callees"]["direct"]
    assert [(c["symbol"], c["confidence"]) for c in callees] == [("legacy_target", None)]


def test_a_repository_with_no_stored_edges_restores_an_empty_graph_not_an_error(tmp_path, shared_embedder):
    repo = _make_repo(tmp_path, "r4", {"m.py": "def lonely():\n    return 1\n"})
    db_url = _db_url(tmp_path, "noedges.db")
    engine1 = _new_engine(db_url, shared_embedder)
    repo_id = engine1.index_directory(str(repo), repo_name="r4", repo_url=str(repo))["repository_id"]

    engine2 = _restart(engine1, db_url, shared_embedder)

    impact = engine2.analyze_impact("lonely", "m.py", repo_id)
    assert impact["direct_callers"] == 0 and impact["callees"]["direct"] == []


# --- incremental safety -----------------------------------------------------

def test_after_a_restart_the_next_run_is_still_forced_full_and_never_drops_edges(tmp_path, shared_embedder):
    repo = _make_repo(tmp_path, "r5", {"core.py": CORE, "app.py": APP}, git=True)
    db_url = _db_url(tmp_path, "incremental_guard.db")
    engine1 = _new_engine(db_url, shared_embedder)
    repo_id = engine1.index_directory(str(repo), repo_name="r5", repo_url=str(repo))["repository_id"]
    expected_edges = _structures(engine1.call_graphs[repo_id])["edges"]

    # Change ONE file and commit, as a user would between restarts.
    (repo / "core.py").write_text(CORE + "\n\ndef triple(x):\n    return add(x, add(x, x))\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "edit")

    engine2 = _restart(engine1, db_url, shared_embedder)
    assert repo_id in engine2.call_graphs  # restored for querying...
    result = engine2.index_directory(str(repo), repo_name="r5", repo_url=str(repo))

    # ...but it has no parse cache behind it, so it must not be treated as a base for an incremental run.
    assert result["incremental"] is False
    assert "no in-memory call graph cached" in result["incremental_reason"]
    assert result["total_files"] == 2
    # Every unchanged file's edges are still present (an incremental merge onto the restored graph would have dropped them).
    after = _structures(engine2.call_graphs[repo_id])["edges"]
    assert set(expected_edges) <= set(after)
    assert any(e[1] == "triple" for e in after), "the edit must be reflected"

    # The full run rebuilt a parse-backed graph, so incremental works again.
    third = engine2.index_directory(str(repo), repo_name="r5", repo_url=str(repo))
    assert third["incremental"] is True


def test_hydrating_never_replaces_a_graph_this_process_already_built(tmp_path, shared_embedder):
    repo = _make_repo(tmp_path, "r6", {"core.py": CORE, "app.py": APP})
    engine = _new_engine(_db_url(tmp_path, "no_overwrite.db"), shared_embedder)
    repo_id = engine.index_directory(str(repo), repo_name="r6", repo_url=str(repo))["repository_id"]
    built = engine.call_graphs[repo_id]

    engine.hydrate_repository(str(repo))

    assert engine.call_graphs[repo_id] is built
