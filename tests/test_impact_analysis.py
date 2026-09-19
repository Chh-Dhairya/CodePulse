"""
Phase 4.2 -- Impact analysis: direct vs. transitive bucketing (roadmap
§37 item 4.2; §11 "Impact analysis output": "extend beyond the current
caller/callee list to explicitly bucket directly affected (depth 1) vs.
transitively affected (depth 2+) symbols/files"; §36 Phase 4 DoD:
"impact analysis output clearly separates confidence tiers and depth
buckets").

Approved architecture (explicit, point-by-point user sign-off, see
CODEPULSE_PROGRESS.md's Phase 4.2 entry):
  - CALLS ONLY. Inheritance relationships (Phase 4.1's
    `relationship_type="inherits"`) never participate in impact
    traversal -- `CallGraphBuilder.get_callers`/`get_callees` already
    only read the calls-only adjacency, unchanged by this milestone.
  - Every legacy response field (`direct_callers`, `indirect_callers`,
    `call_chain`, `dependencies`) keeps its exact pre-4.2 meaning; new
    `callers: {direct, transitive}` / `callees: {direct, transitive}`
    fields are purely additive.
  - `direct = depth == 1`, `transitive = depth >= 2` -- no other
    buckets.
  - Each caller/callee entry now also carries `confidence` (the
    existing high/low/unresolved vocabulary).
  - Traversal results are deterministically ordered (confidence, then
    call_count descending, then file, then symbol) -- independent of
    Python's per-process string-hash-randomized set iteration order.
  - `IndexingEngine.analyze_impact()` is the new shared intelligence-
    core method; both the REST route and the CLI `impact` command call
    it instead of independently duplicating `CallGraphBuilder` calls.

Unit-level coverage of ordinary call-graph traversal/reachability
already exists (tests/test_graph.py) and is not duplicated here except
where a fixture doubles as depth/confidence coverage. Base call-edge
confidence-tier computation is covered in tests/test_graph.py and
tests/test_call_edge_confidence.py. Inheritance/adjacency-separation
coverage is in tests/test_inheritance_graph.py. Phase 3.5's generic
incremental-persistence algorithm is covered end-to-end in
tests/test_call_graph_incremental_update.py -- this file adds only the
impact-analysis-specific angle (bucket membership after a change), not
a second copy of that algorithm's own correctness tests.

All persistence tests use temporary, disposable SQLite databases and
temporary git repositories under `tmp_path` -- never the production
codepulse.db.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

import codepulse.api.app as app_module
from codepulse.api.app import app
from codepulse.config import reset_settings
from codepulse.embeddings.embedder import CodeEmbedder
from codepulse.git_diff import get_head_commit_sha
from codepulse.indexer.engine import IndexingEngine
from codepulse.parser.graph import CallGraphBuilder
from codepulse.parser.python import PythonParser
from codepulse.storage.db import StorageManager
from tests.conftest import make_api_key_headers


@pytest.fixture(autouse=True)
def cleanup_settings():
    reset_settings()
    yield
    reset_settings()


@pytest.fixture(scope="module")
def shared_embedder():
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


def _db_url(tmp_path, name: str) -> str:
    db_path = tmp_path / name
    return f"sqlite:///{str(db_path).replace(os.sep, '/')}"


def _new_engine(db_url: str, embedder: CodeEmbedder) -> IndexingEngine:
    storage = StorageManager(db_url=db_url)
    return IndexingEngine(storage=storage, embedder=embedder)


# --- 1-8: graph-level direct/transitive + confidence + cycle/diamond ----
# Reuses test_graph.py's own well-established fixture shape (db.py /
# service.py, a 3-hop chain mixing "low" same-file and "high" import-
# resolved confidence) so these tests double as depth AND confidence
# coverage without inventing a new fixture.

def _make_chain_builder() -> CallGraphBuilder:
    py_parser = PythonParser()
    res_a = py_parser.parse_file(
        "db.py",
        'def db_connect():\n    return "conn"\n\ndef db_query(q):\n    conn = db_connect()\n    return conn\n',
    )
    res_b = py_parser.parse_file(
        "service.py",
        'from db import db_query\n\n'
        'def get_user_profile(user_id):\n    return db_query("SELECT *")\n\n'
        'def render_dashboard(user_id):\n    user = get_user_profile(user_id)\n    return user\n',
    )
    return CallGraphBuilder([res_a, res_b])


def test_direct_caller_is_bucketed_at_depth_one():
    builder = _make_chain_builder()
    callers = builder.get_callers("db_query", "db.py", max_depth=3)
    direct = [c for c in callers if c["depth"] == 1]
    assert any(c["symbol"] == "get_user_profile" for c in direct)


def test_second_hop_caller_is_bucketed_as_transitive():
    builder = _make_chain_builder()
    callers = builder.get_callers("db_query", "db.py", max_depth=3)
    transitive = [c for c in callers if c["depth"] >= 2]
    assert any(c["symbol"] == "render_dashboard" and c["depth"] == 2 for c in transitive)


def test_deeper_caller_reports_correct_shortest_path_depth():
    builder = _make_chain_builder()
    callers = builder.get_callers("db_connect", "db.py", max_depth=3)
    by_symbol = {c["symbol"]: c["depth"] for c in callers}
    assert by_symbol["db_query"] == 1
    assert by_symbol["get_user_profile"] == 2
    assert by_symbol["render_dashboard"] == 3


def test_direct_callee_is_bucketed_at_depth_one():
    builder = _make_chain_builder()
    callees = builder.get_callees("render_dashboard", "service.py", max_depth=3)
    direct = [c for c in callees if c["depth"] == 1]
    assert any(c["symbol"] == "get_user_profile" for c in direct)


def test_transitive_callee_is_bucketed_correctly():
    builder = _make_chain_builder()
    callees = builder.get_callees("render_dashboard", "service.py", max_depth=3)
    by_symbol = {c["symbol"]: c["depth"] for c in callees}
    assert by_symbol["db_query"] == 2
    assert by_symbol["db_connect"] == 3


def test_confidence_is_preserved_per_hop():
    """get_user_profile->db_query is import-resolved ("high");
    render_dashboard->get_user_profile and db_query->db_connect are
    same-file bare-name matches ("low") -- each hop's own confidence
    must survive into the traversal result, not just the direct
    single-edge case."""
    builder = _make_chain_builder()
    callees = builder.get_callees("render_dashboard", "service.py", max_depth=3)
    by_symbol = {c["symbol"]: c["confidence"] for c in callees}
    assert by_symbol["get_user_profile"] == "low"
    assert by_symbol["db_query"] == "high"
    assert by_symbol["db_connect"] == "low"


def test_cycle_does_not_infinite_loop_and_each_symbol_appears_once():
    py_parser = PythonParser()
    res = py_parser.parse_file(
        "mutual.py",
        'def x():\n    return y()\n\n\ndef y():\n    return x()\n',
    )
    builder = CallGraphBuilder([res])
    callees = builder.get_callees("x", "mutual.py", max_depth=5)
    names = [c["symbol"] for c in callees]
    assert names.count("y") == 1, f"y must appear exactly once despite the x<->y cycle, got {names}"
    assert "x" not in names, "the start symbol itself must never appear in its own callee list"


def test_diamond_reachability_reports_one_entry_at_the_shortest_depth():
    """top() calls left() and right(); both left() and right() call
    bottom() -- bottom is reachable via two paths but must appear
    exactly once, at its true shortest-path depth (2), not twice and
    not at some inconsistent depth."""
    py_parser = PythonParser()
    res = py_parser.parse_file(
        "diamond.py",
        'def top():\n    left()\n    right()\n\n'
        'def left():\n    return bottom()\n\n'
        'def right():\n    return bottom()\n\n'
        'def bottom():\n    return 1\n',
    )
    builder = CallGraphBuilder([res])
    callees = builder.get_callees("top", "diamond.py", max_depth=3)
    bottom_entries = [c for c in callees if c["symbol"] == "bottom"]
    assert len(bottom_entries) == 1, f"bottom must appear exactly once, got {bottom_entries}"
    assert bottom_entries[0]["depth"] == 2


# --- 9: deterministic ordering across independent processes -------------

_DETERMINISM_SCRIPT = '''
import json
import sys
sys.path.insert(0, {project_root!r})
from codepulse.parser.python import PythonParser
from codepulse.parser.graph import CallGraphBuilder

py_parser = PythonParser()
res = py_parser.parse_file(
    "diamond.py",
    "def top():\\n    left()\\n    mid()\\n    right()\\n\\n"
    "def left():\\n    return bottom()\\n\\n"
    "def mid():\\n    return bottom()\\n\\n"
    "def right():\\n    return bottom()\\n\\n"
    "def bottom():\\n    return 1\\n",
)
builder = CallGraphBuilder([res])
callees = builder.get_callees("top", "diamond.py", max_depth=3)
print(json.dumps(callees))
'''


def test_deterministic_ordering_is_identical_across_independent_processes_with_different_hash_seeds():
    """The audit's own finding: pre-4.2, `self.adjacency` values were
    plain Python `set`s, whose iteration order depends on per-process
    string-hash randomization (PYTHONHASHSEED). Two separate process
    runs against the byte-identical repository snapshot, with
    DIFFERENTLY seeded hash randomization, must still produce
    byte-identical ordered output -- proving the Phase 4.2 sort key
    (confidence, call_count, file, symbol) actually removed the
    dependency on set-iteration order, not just happened to look stable
    within one process."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = _DETERMINISM_SCRIPT.format(project_root=project_root)

    env1 = dict(os.environ, PYTHONHASHSEED="1")
    env2 = dict(os.environ, PYTHONHASHSEED="4242")

    out1 = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env1, check=True).stdout
    out2 = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env2, check=True).stdout

    assert out1 == out2, (
        "get_callees output must be byte-identical across processes with different "
        f"PYTHONHASHSEED values -- got:\nseed=1:    {out1!r}\nseed=4242: {out2!r}"
    )
    # Sanity: the ordering is not merely stable, it's the actual data.
    parsed = json.loads(out1)
    assert {c["symbol"] for c in parsed} == {"left", "mid", "right", "bottom"}


# --- 10-14: engine/API-level (repository isolation, legacy compat,
# inheritance exclusion, incremental updates, restart behavior) ---------

IMPACT_CHAIN = (
    'def db_connect():\n    """Connect."""\n    return "conn"\n\n\n'
    'def db_query(q):\n    """Query."""\n    conn = db_connect()\n    return conn\n'
)
IMPACT_SERVICE = (
    'from mod import db_query\n\n\n'
    'def get_user_profile(user_id):\n    """Profile."""\n    return db_query("SELECT *")\n\n\n'
    'def render_dashboard(user_id):\n    """Dashboard."""\n    user = get_user_profile(user_id)\n    return user\n'
)


def _index_impact_repo(tmp_path, dirname, embedder, db_name):
    repo = tmp_path / dirname
    _init_repo(repo)
    _write(repo, "mod.py", IMPACT_CHAIN)
    _write(repo, "service.py", IMPACT_SERVICE)
    _commit_all(repo, "initial")
    engine = _new_engine(_db_url(tmp_path, db_name), embedder)
    result = engine.index_directory(str(repo), repo_name=dirname, repo_url=str(repo))
    return repo, engine, result


def test_legacy_response_fields_are_unchanged_and_new_bucket_fields_are_additive(tmp_path, shared_embedder, monkeypatch):
    repo, engine, result = _index_impact_repo(tmp_path, "repo_legacy_compat", shared_embedder, "legacy_compat.db")

    from codepulse.storage.db import FileModel, SymbolModel
    session = engine.storage.get_session()
    try:
        sym = session.query(SymbolModel).join(FileModel).filter(
            FileModel.repo_id == result["repository_id"], SymbolModel.name == "db_query"
        ).first()
        symbol_id = sym.id
    finally:
        session.close()

    monkeypatch.setattr(app_module, "storage_manager", engine.storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    client = TestClient(app)
    headers = make_api_key_headers(engine.storage, repository_ids=[result["repository_id"]])

    response = client.post(f"/v1/symbols/{symbol_id}/impact-analysis", json={}, headers=headers)
    assert response.status_code == 200
    body = response.json()

    # Legacy fields: exact pre-4.2 semantics.
    assert body["symbol"] == "db_query"
    assert body["direct_callers"] == 1
    assert body["indirect_callers"] == 1
    assert len(body["call_chain"]) == 2
    assert isinstance(body["dependencies"], list)

    # New, additive fields.
    assert set(body["callers"].keys()) == {"direct", "transitive"}
    assert set(body["callees"].keys()) == {"direct", "transitive"}
    assert len(body["callers"]["direct"]) == 1
    assert body["callers"]["direct"][0]["symbol"] == "get_user_profile"
    assert body["callers"]["direct"][0]["confidence"] == "high"
    assert len(body["callers"]["transitive"]) == 1
    assert body["callers"]["transitive"][0]["symbol"] == "render_dashboard"


def test_impact_analysis_bucketed_fields_do_not_leak_across_repositories(tmp_path, shared_embedder, monkeypatch):
    """Two repositories, identical symbol names throughout. Neither the
    legacy fields NOR the new direct/transitive bucket fields may leak
    the other repository's callers/callees."""
    repo_a, engine, r_a = _index_impact_repo(tmp_path, "repo_a_iso", shared_embedder, "iso.db")
    repo_b = tmp_path / "repo_b_iso"
    _init_repo(repo_b)
    _write(repo_b, "mod.py", IMPACT_CHAIN)
    _write(repo_b, "service.py", IMPACT_SERVICE)
    _commit_all(repo_b, "initial")
    r_b = engine.index_directory(str(repo_b), repo_name="repo_b_iso", repo_url=str(repo_b))

    from codepulse.storage.db import FileModel, SymbolModel
    session = engine.storage.get_session()
    try:
        b_sym = session.query(SymbolModel).join(FileModel).filter(
            FileModel.repo_id == r_b["repository_id"], SymbolModel.name == "db_query"
        ).first()
        symbol_id = b_sym.id
    finally:
        session.close()

    monkeypatch.setattr(app_module, "storage_manager", engine.storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    client = TestClient(app)
    # A key scoped ONLY to repo A -- must never see repo B's data.
    headers = make_api_key_headers(engine.storage, repository_ids=[r_a["repository_id"]])

    response = client.post(f"/v1/symbols/{symbol_id}/impact-analysis", json={}, headers=headers)
    assert response.status_code == 403, "a key scoped to repo A must be forbidden from repo B's symbol entirely"

    # Now the correctly-scoped key: repo B's own results must be
    # complete and correct (not accidentally emptied by the isolation
    # fix), and must never contain any hint of repo A's data.
    headers_b = make_api_key_headers(engine.storage, repository_ids=[r_b["repository_id"]])
    response_b = client.post(f"/v1/symbols/{symbol_id}/impact-analysis", json={}, headers=headers_b)
    assert response_b.status_code == 200
    body = response_b.json()
    assert len(body["callers"]["direct"]) == 1
    assert body["callers"]["direct"][0]["symbol"] == "get_user_profile"
    assert len(body["callers"]["transitive"]) == 1
    assert body["callers"]["transitive"][0]["symbol"] == "render_dashboard"
    assert len(body["callees"]["direct"]) == 1
    assert body["callees"]["direct"][0]["symbol"] == "db_connect"
    assert len(body["callees"]["transitive"]) == 0


def test_inheritance_edges_never_appear_in_any_impact_analysis_field(tmp_path, shared_embedder, monkeypatch):
    """relationship_type="inherits" must never surface in callers,
    callees, call_chain, dependencies, direct_callers, or
    indirect_callers -- Decision 1's calls-only scope, verified
    end-to-end through the actual REST response, not just the
    in-memory graph (already covered separately in
    tests/test_inheritance_graph.py)."""
    repo = tmp_path / "repo_impact_inherit"
    _init_repo(repo)
    _write(
        repo, "animals.py",
        'class Animal:\n    """Base."""\n    pass\n\n\n'
        'class Dog(Animal):\n    """Sub."""\n    pass\n',
    )
    _write(repo, "mod.py", IMPACT_CHAIN)
    _commit_all(repo, "initial")
    engine = _new_engine(_db_url(tmp_path, "impact_inherit.db"), shared_embedder)
    result = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))

    from codepulse.storage.db import FileModel, SymbolModel
    session = engine.storage.get_session()
    try:
        animal_sym = session.query(SymbolModel).join(FileModel).filter(
            FileModel.repo_id == result["repository_id"], SymbolModel.name == "Animal"
        ).first()
        symbol_id = animal_sym.id
    finally:
        session.close()

    monkeypatch.setattr(app_module, "storage_manager", engine.storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    client = TestClient(app)
    headers = make_api_key_headers(engine.storage, repository_ids=[result["repository_id"]])

    response = client.post(f"/v1/symbols/{symbol_id}/impact-analysis", json={}, headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["direct_callers"] == 0
    assert body["indirect_callers"] == 0
    assert body["call_chain"] == []
    assert body["dependencies"] == []
    assert body["callers"] == {"direct": [], "transitive": []}
    assert body["callees"] == {"direct": [], "transitive": []}


def test_incremental_reindex_updates_impact_bucket_membership_for_a_changed_caller(tmp_path, shared_embedder):
    """index -> modify which function calls db_query -> incremental
    re-index -> analyze_impact reflects the new caller, not the old
    one. Reuses Phase 3.5's own already-proven incremental-persistence
    machinery (not a new mechanism) -- this test is the
    impact-analysis-specific angle on top of it."""
    repo = tmp_path / "repo_impact_incremental"
    _init_repo(repo)
    _write(repo, "mod.py", IMPACT_CHAIN)
    _write(
        repo, "service.py",
        'from mod import db_query\n\n\n'
        'def get_user_profile(user_id):\n    """Profile."""\n    return db_query("SELECT *")\n',
    )
    _write(repo, "keep1.py", "def k1():\n    return 1\n")
    _write(repo, "keep2.py", "def k2():\n    return 2\n")
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "impact_incremental.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]

    impact1 = engine.analyze_impact("db_query", "mod.py", repo_id, max_depth=3)
    assert {c["symbol"] for c in impact1["callers"]["direct"]} == {"get_user_profile"}

    # Change service.py so a DIFFERENT function calls db_query.
    _write(
        repo, "service.py",
        'from mod import db_query\n\n\n'
        'def new_caller(user_id):\n    """New caller."""\n    return db_query("SELECT *")\n',
    )
    _commit_all(repo, "swap caller of db_query")
    result2 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result2["incremental"] is True

    impact2 = engine.analyze_impact("db_query", "mod.py", repo_id, max_depth=3)
    direct_names = {c["symbol"] for c in impact2["callers"]["direct"]}
    assert direct_names == {"new_caller"}, f"expected only new_caller, got {direct_names}"


def test_incremental_reindex_updates_impact_bucket_membership_for_a_changed_callee(tmp_path, shared_embedder):
    """A function's own body changes which downstream function it
    calls -- the callee buckets must update accordingly."""
    repo = tmp_path / "repo_impact_incremental_callee"
    _init_repo(repo)
    _write(
        repo, "mod.py",
        'def helper_a():\n    """A."""\n    return 1\n\n\n'
        'def helper_b():\n    """B."""\n    return 2\n\n\n'
        'def caller():\n    """Calls helper_a."""\n    return helper_a()\n',
    )
    _write(repo, "keep1.py", "def k1():\n    return 1\n")
    _write(repo, "keep2.py", "def k2():\n    return 2\n")
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "impact_incremental_callee.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]

    impact1 = engine.analyze_impact("caller", "mod.py", repo_id, max_depth=3)
    assert {c["symbol"] for c in impact1["callees"]["direct"]} == {"helper_a"}

    _write(
        repo, "mod.py",
        'def helper_a():\n    """A."""\n    return 1\n\n\n'
        'def helper_b():\n    """B."""\n    return 2\n\n\n'
        'def caller():\n    """Calls helper_b now."""\n    return helper_b()\n',
    )
    _commit_all(repo, "swap callee")
    result2 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result2["incremental"] is True

    impact2 = engine.analyze_impact("caller", "mod.py", repo_id, max_depth=3)
    assert {c["symbol"] for c in impact2["callees"]["direct"]} == {"helper_b"}


def test_incremental_reindex_after_deleting_the_target_symbols_file(tmp_path, shared_embedder):
    """Deleting the file the target symbol lived in must leave an
    unrelated, unchanged caller's edge corrected to unresolved (Phase
    3.5's own BFS-one-hop mechanism), reflected here in the bucketed
    fields specifically."""
    repo = tmp_path / "repo_impact_deleted_target"
    _init_repo(repo)
    _write(repo, "mod.py", IMPACT_CHAIN)
    _write(
        repo, "service.py",
        'from mod import db_query\n\n\ndef get_user_profile(user_id):\n    """Profile."""\n    return db_query("SELECT *")\n',
    )
    _write(repo, "keep1.py", "def k1():\n    return 1\n")
    _write(repo, "keep2.py", "def k2():\n    return 2\n")
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "impact_deleted_target.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]

    impact1 = engine.analyze_impact("db_query", "mod.py", repo_id, max_depth=3)
    assert impact1["callers"]["direct"][0]["confidence"] == "high"

    os.remove(repo / "mod.py")
    _commit_all(repo, "delete mod.py entirely")
    result2 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result2["incremental"] is True
    assert result2["deleted_files"] == ["mod.py"]

    impact2 = engine.analyze_impact("db_query", "mod.py", repo_id, max_depth=3)
    # service.py was never reparsed this run, so its own call site to
    # db_query still exists, but must now resolve as unresolved (its
    # import target no longer exists) -- proving impact analysis reads
    # the CORRECTED graph, not a stale one.
    direct = impact2["callers"]["direct"]
    assert len(direct) == 1
    assert direct[0]["confidence"] == "unresolved"


def test_incremental_reindex_after_renaming_the_target_symbols_file(tmp_path, shared_embedder):
    repo = tmp_path / "repo_impact_renamed_target"
    _init_repo(repo)
    _write(repo, "mod.py", IMPACT_CHAIN)
    _write(
        repo, "service.py",
        'from mod import db_query\n\n\ndef get_user_profile(user_id):\n    """Profile."""\n    return db_query("SELECT *")\n',
    )
    _write(repo, "keep1.py", "def k1():\n    return 1\n")
    _write(repo, "keep2.py", "def k2():\n    return 2\n")
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "impact_renamed_target.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]

    (repo / "mod.py").rename(repo / "module.py")
    _commit_all(repo, "rename mod.py to module.py")
    result2 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result2["incremental"] is True
    assert result2["renamed_files"] == [{"old": "mod.py", "new": "module.py"}]

    # The renamed symbol's own new identity: analyze_impact against its
    # NEW file path must still find its (now-unresolved, since
    # service.py's own `from mod import db_query` is stale) caller.
    impact2 = engine.analyze_impact("db_query", "module.py", repo_id, max_depth=3)
    direct = impact2["callers"]["direct"]
    assert len(direct) == 1
    assert direct[0]["confidence"] == "unresolved"


def test_analyze_impact_returns_empty_buckets_not_an_error_when_call_graph_cache_is_cold(shared_embedder, tmp_path):
    """Restart behavior: `IndexingEngine.call_graphs` does not survive a
    process restart (tests/test_restart_idempotency.py's own established
    contract). A fresh engine, queried before any index_directory() call,
    must degrade to empty buckets, not raise."""
    engine = _new_engine(_db_url(tmp_path, "cold_cache.db"), shared_embedder)
    assert engine.call_graphs == {}

    impact = engine.analyze_impact("anything", "anywhere.py", repo_id=999, max_depth=3)
    assert impact == {
        "symbol": "anything",
        "direct_callers": 0,
        "indirect_callers": 0,
        "call_chain": [],
        "dependencies": [],
        "callers": {"direct": [], "transitive": []},
        "callees": {"direct": [], "transitive": []},
    }


def test_analyze_impact_after_simulated_restart_and_reindex_returns_correct_results(tmp_path, shared_embedder):
    """A fresh engine instance (simulating a process restart), after
    re-indexing, must produce identical impact-analysis results to the
    original engine -- the in-memory cache being cold beforehand must
    not leave any permanent incorrectness once rebuilt."""
    repo = tmp_path / "repo_impact_restart"
    _init_repo(repo)
    _write(repo, "mod.py", IMPACT_CHAIN)
    _write(
        repo, "service.py",
        'from mod import db_query\n\n\ndef get_user_profile(user_id):\n    """Profile."""\n    return db_query("SELECT *")\n',
    )
    _commit_all(repo, "initial")

    db_url = _db_url(tmp_path, "impact_restart.db")
    engine1 = _new_engine(db_url, shared_embedder)
    result1 = engine1.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]
    impact_before = engine1.analyze_impact("db_query", "mod.py", repo_id, max_depth=3)

    engine2 = _new_engine(db_url, shared_embedder)  # simulated restart
    assert engine2.call_graphs == {}
    engine2.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    impact_after = engine2.analyze_impact("db_query", "mod.py", repo_id, max_depth=3)

    assert impact_after["callers"]["direct"] == impact_before["callers"]["direct"]
