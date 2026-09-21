"""
Phase 4.3 -- Deterministic module clustering via import graph (roadmap
§37 item 4.3; §11 "Architecture discovery"; §45 backlog B18: "New
graph-analysis module").

Approved architecture (explicit, point-by-point user sign-off, see
CODEPULSE_PROGRESS.md's Phase 4.3 entry):
  - `ModuleGraphBuilder` (`codepulse/parser/module_graph.py`) operates
    on the FILE-LEVEL import graph, kept deliberately separate from
    `CallGraphBuilder`'s symbol-level call/inheritance graph.
  - Edges reuse `ImportResolver.resolve_import_path` unmodified; only
    resolved, repository-local imports become edges (no fake nodes for
    external/unresolved imports); repeated imports between the same
    file pair collapse to one edge (dependency EXISTENCE, not raw
    import-statement count).
  - `in_degree`/`out_degree` are raw, uninterpreted per-file signals --
    no invented score/label.
  - Clustering is seeded Louvain community detection
    (`seed=0`, fixed, not caller-configurable this milestone) over an
    UNDIRECTED view of the same edge set -- approved AFTER plain
    connected components was empirically tried and rejected (it
    produced one giant component plus one singleton on CodePulse's own
    repository, failing B18's own "coherent module clusters" DoD).
  - No REST/CLI/MCP surface this milestone (library/shared-core only):
    `IndexingEngine.analyze_module_structure(repo_id)` is the one new
    integration point, mirroring `analyze_impact`'s own established
    pattern, computed fresh from the current snapshot every call, with
    no incremental-update machinery and no persistence.

Unit-level coverage of `ImportResolver.resolve_import_path` itself
already exists (tests/test_graph.py) and is not duplicated here --
only the integration between it and `ModuleGraphBuilder` is tested.

All engine-level tests use temporary, disposable SQLite databases and
temporary git repositories under `tmp_path` -- never the production
codepulse.db.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from codepulse.config import reset_settings
from codepulse.embeddings.embedder import CodeEmbedder
from codepulse.indexer.engine import IndexingEngine
from codepulse.parser.module_graph import ModuleGraphBuilder
from codepulse.parser.python import PythonParser
from codepulse.storage.db import StorageManager


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


# --- A-G: small synthetic fixtures (unit-level, direct construction) ----

def test_isolated_file_has_zero_degree_and_its_own_singleton_cluster():
    parser = PythonParser()
    res = parser.parse_file("solo.py", "x = 1\n")
    mg = ModuleGraphBuilder([res])

    assert mg.files == ["solo.py"]
    assert mg.in_degree["solo.py"] == 0
    assert mg.out_degree["solo.py"] == 0
    assert mg.clusters == [{"id": 0, "files": ["solo.py"]}]


def test_simple_chain_has_correct_degrees_and_stays_in_one_cluster():
    """a.py -> b.py -> c.py"""
    parser = PythonParser()
    res_a = parser.parse_file("a.py", "from b import fb\n\n\ndef fa():\n    return fb()\n")
    res_b = parser.parse_file("b.py", "from c import fc\n\n\ndef fb():\n    return fc()\n")
    res_c = parser.parse_file("c.py", "def fc():\n    return 1\n")
    mg = ModuleGraphBuilder([res_a, res_b, res_c])

    assert mg.in_degree == {"a.py": 0, "b.py": 1, "c.py": 1}
    assert mg.out_degree == {"a.py": 1, "b.py": 1, "c.py": 0}
    assert len(mg.clusters) == 1
    assert mg.clusters[0]["files"] == ["a.py", "b.py", "c.py"]


def test_fully_connected_triangle_stays_in_one_cluster_with_equal_degrees():
    """x.py <-> y.py <-> z.py <-> x.py -- every pair imports every other."""
    parser = PythonParser()
    res_x = parser.parse_file(
        "x.py", "from y import fy\nfrom z import fz\n\n\ndef fx():\n    return fy() + fz()\n"
    )
    res_y = parser.parse_file(
        "y.py", "from x import fx\nfrom z import fz\n\n\ndef fy():\n    return fx() + fz()\n"
    )
    res_z = parser.parse_file(
        "z.py", "from x import fx\nfrom y import fy\n\n\ndef fz():\n    return fx() + fy()\n"
    )
    mg = ModuleGraphBuilder([res_x, res_y, res_z])

    assert mg.in_degree == {"x.py": 2, "y.py": 2, "z.py": 2}
    assert mg.out_degree == {"x.py": 2, "y.py": 2, "z.py": 2}
    assert len(mg.clusters) == 1
    assert mg.clusters[0]["files"] == ["x.py", "y.py", "z.py"]


def test_two_disconnected_groups_form_two_separate_clusters():
    parser = PythonParser()
    res_p = parser.parse_file("p.py", "from q import fq\n\n\ndef fp():\n    return fq()\n")
    res_q = parser.parse_file("q.py", "def fq():\n    return 1\n")
    res_r = parser.parse_file("r.py", "from s import fs\n\n\ndef fr():\n    return fs()\n")
    res_s = parser.parse_file("s.py", "def fs():\n    return 1\n")
    mg = ModuleGraphBuilder([res_p, res_q, res_r, res_s])

    assert len(mg.clusters) == 2
    file_sets = [set(c["files"]) for c in mg.clusters]
    assert {"p.py", "q.py"} in file_sets
    assert {"r.py", "s.py"} in file_sets


def test_duplicate_imports_between_the_same_file_pair_collapse_to_one_edge():
    parser = PythonParser()
    res_a = parser.parse_file("a.py", "from b import x\nfrom b import y\n")
    res_b = parser.parse_file("b.py", "x = 1\ny = 2\n")
    mg = ModuleGraphBuilder([res_a, res_b])

    assert mg.out_degree["a.py"] == 1
    assert mg.in_degree["b.py"] == 1
    assert mg.adjacency["a.py"] == {"b.py"}


def test_unresolved_external_imports_never_become_nodes_or_edges():
    parser = PythonParser()
    res = parser.parse_file("ext.py", "import os\nimport requests\n")
    mg = ModuleGraphBuilder([res])

    assert mg.files == ["ext.py"]
    assert "os" not in mg.files
    assert "requests" not in mg.files
    assert mg.out_degree["ext.py"] == 0


def test_relative_imports_resolve_to_the_correct_package_file():
    parser = PythonParser()
    res_init = parser.parse_file("pkg/__init__.py", "")
    res_a = parser.parse_file("pkg/a.py", "def helper():\n    return 1\n")
    res_b = parser.parse_file("pkg/b.py", "from .a import helper\n\n\ndef use():\n    return helper()\n")
    mg = ModuleGraphBuilder([res_init, res_a, res_b])

    assert mg.adjacency["pkg/b.py"] == {"pkg/a.py"}
    assert mg.in_degree["pkg/a.py"] == 1
    assert mg.out_degree["pkg/b.py"] == 1


# --- degree ranking orderings --------------------------------------------

def test_rank_by_inbound_and_outbound_use_their_documented_deterministic_orderings():
    parser = PythonParser()
    res_hub = parser.parse_file("hub.py", "def h():\n    return 1\n")
    res_a = parser.parse_file("a.py", "from hub import h\n\n\ndef fa():\n    return h()\n")
    res_b = parser.parse_file("b.py", "from hub import h\n\n\ndef fb():\n    return h()\n")
    mg = ModuleGraphBuilder([res_hub, res_a, res_b])

    inbound = mg.rank_by_inbound()
    assert inbound[0]["file"] == "hub.py"
    assert inbound[0]["in_degree"] == 2
    assert all(set(r.keys()) == {"file", "in_degree", "out_degree"} for r in inbound)

    outbound = mg.rank_by_outbound()
    top_out_files = {outbound[0]["file"], outbound[1]["file"]}
    assert top_out_files == {"a.py", "b.py"}


# --- ImportResolver integration (minimal -- resolution itself is
# already covered by tests/test_graph.py) --------------------------------

def test_module_graph_builder_reuses_import_resolver_without_reimplementing_it():
    parser = PythonParser()
    res_a = parser.parse_file(
        "app/security/auth.py",
        "from .hashing import hash_password\n\n\ndef register():\n    return hash_password('x')\n",
    )
    res_b = parser.parse_file("app/security/hashing.py", "def hash_password(pw):\n    return pw\n")
    mg = ModuleGraphBuilder([res_a, res_b])

    assert mg.adjacency["app/security/auth.py"] == {"app/security/hashing.py"}


# --- H: repository isolation, via IndexingEngine.analyze_module_structure

MODULE_A = "from mod_b import fb\n\n\ndef fa():\n    return fb()\n"
MODULE_B = "def fb():\n    return 1\n"


def _index_module_repo(tmp_path, dirname, embedder, db_name_or_engine):
    repo = tmp_path / dirname
    _init_repo(repo)
    _write(repo, "mod_a.py", MODULE_A)
    _write(repo, "mod_b.py", MODULE_B)
    _commit_all(repo, "initial")
    if isinstance(db_name_or_engine, IndexingEngine):
        engine = db_name_or_engine
    else:
        engine = _new_engine(_db_url(tmp_path, db_name_or_engine), embedder)
    result = engine.index_directory(str(repo), repo_name=dirname, repo_url=str(repo))
    return engine, result


def test_analyze_module_structure_does_not_leak_across_repositories(tmp_path, shared_embedder):
    """Two repositories, identical file/module names throughout. Neither
    clusters nor degrees may mix."""
    engine, r_a = _index_module_repo(tmp_path, "repo_a_module", shared_embedder, "module_isolation.db")
    engine, r_b = _index_module_repo(tmp_path, "repo_b_module", shared_embedder, engine)

    mg_a = engine.analyze_module_structure(r_a["repository_id"])
    mg_b = engine.analyze_module_structure(r_b["repository_id"])

    assert set(mg_a.files) == {"mod_a.py", "mod_b.py"}
    assert set(mg_b.files) == {"mod_a.py", "mod_b.py"}
    # Same file NAMES in both repos, but each analysis is built from
    # only its own repository's parsed files -- degrees/clusters must
    # each independently reflect exactly one repo's own two files, not
    # a merged four-file graph.
    assert mg_a.in_degree["mod_b.py"] == 1
    assert mg_b.in_degree["mod_b.py"] == 1
    assert len(mg_a.clusters) == 1
    assert len(mg_a.clusters[0]["files"]) == 2
    assert len(mg_b.clusters) == 1
    assert len(mg_b.clusters[0]["files"]) == 2


def test_analyze_module_structure_returns_empty_graph_for_an_unindexed_repository(tmp_path, shared_embedder):
    """Restart-like behavior: a repo_id with no cached parse_results
    (never indexed this process, or a cold cache after a restart) must
    degrade to an empty, non-crashing graph."""
    engine = _new_engine(_db_url(tmp_path, "module_cold.db"), shared_embedder)
    mg = engine.analyze_module_structure(repo_id=999)
    assert mg.files == []
    assert mg.clusters == []


# --- Determinism ----------------------------------------------------------

_DETERMINISM_SCRIPT = '''
import json
import sys
sys.path.insert(0, {project_root!r})
from codepulse.parser.python import PythonParser
from codepulse.parser.module_graph import ModuleGraphBuilder

parser = PythonParser()
files = {{
    "api.py": "from core import a, b, c\\n\\n\\ndef handle():\\n    return a() + b() + c()\\n",
    "core.py": "from util import u\\n\\n\\ndef a():\\n    return u()\\n\\n\\ndef b():\\n    return u()\\n\\n\\ndef c():\\n    return u()\\n",
    "util.py": "def u():\\n    return 1\\n",
    "leaf.py": "def leaf():\\n    return 1\\n",
}}
results = [parser.parse_file(path, code) for path, code in files.items()]
mg = ModuleGraphBuilder(results)
print(json.dumps({{
    "clusters": mg.clusters,
    "in_degree": mg.in_degree,
    "out_degree": mg.out_degree,
    "rank_by_inbound": mg.rank_by_inbound(),
    "rank_by_outbound": mg.rank_by_outbound(),
}}, sort_keys=True))
'''


def test_module_graph_result_is_identical_across_independent_processes_with_different_hash_seeds():
    """The same lesson Phase 4.2 already applied to `get_callers`/
    `get_callees`, verified here for `ModuleGraphBuilder` specifically:
    the fixed Louvain seed plus the explicit deterministic re-sort
    (§37 item 4.3's own approved contract) must produce byte-identical
    output across independent process runs with different
    PYTHONHASHSEED values, not just within one process."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = _DETERMINISM_SCRIPT.format(project_root=project_root)

    outputs = []
    for seed in ("1", "2", "3"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        out = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, env=env, check=True
        ).stdout
        outputs.append(out)

    assert outputs[0] == outputs[1] == outputs[2], (
        "ModuleGraphBuilder output must be byte-identical across processes with "
        f"different PYTHONHASHSEED values -- got: {outputs!r}"
    )
    parsed = json.loads(outputs[0])
    assert len(parsed["clusters"]) >= 1
    assert set(parsed["in_degree"].keys()) == {"api.py", "core.py", "util.py", "leaf.py"}


# --- The DoD's own self-referential test case ----------------------------

def _parse_codepulse_own_package() -> list:
    """Walks and parses this actual repository's own `codepulse/`
    package source tree -- the real validation case §45 backlog B18
    itself names ("Returns coherent module clusters on CodePulse's own
    repo as a test case")."""
    parser = PythonParser()
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    package_root = os.path.join(project_root, "codepulse")
    results = []
    for root, dirs, files in os.walk(package_root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fn in files:
            if fn.endswith(".py"):
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, project_root).replace(os.sep, "/")
                with open(full, encoding="utf-8") as f:
                    src = f.read()
                results.append(parser.parse_file(rel, src))
    return results


def test_codepulse_own_repository_produces_coherent_non_trivial_clusters():
    """B18's own literal DoD, made a permanent regression test. Does
    NOT hard-code exact Louvain membership for any file (a harmless
    dependency change should never break this test) -- only asserts
    the STRUCTURAL properties that distinguish "coherent clusters" from
    the "one giant blob" failure mode this milestone's own audit
    measured and rejected for plain connected components."""
    parse_results = _parse_codepulse_own_package()
    assert len(parse_results) > 10, "sanity: the real codepulse/ package must have been found"

    mg = ModuleGraphBuilder(parse_results)

    all_files = {res.file_path for res in parse_results}
    clustered_files = [f for c in mg.clusters for f in c["files"]]

    # Every parsed file appears, exactly once, and only files that were
    # actually analyzed appear.
    assert sorted(clustered_files) == sorted(all_files)
    assert len(clustered_files) == len(set(clustered_files)), "no file may appear in more than one cluster"
    assert set(clustered_files) <= all_files

    non_singleton_clusters = [c for c in mg.clusters if len(c["files"]) > 1]
    assert len(non_singleton_clusters) > 1, (
        "must produce MORE than one non-singleton cluster -- the exact failure mode "
        "plain connected components measurably produced on this same repository "
        f"(one giant component), got clusters={mg.clusters!r}"
    )

    largest = max(len(c["files"]) for c in mg.clusters)
    assert largest < len(all_files), (
        "must NOT be one giant cluster containing every file -- "
        f"largest cluster has {largest} of {len(all_files)} files"
    )

    # Degree information is present for every file, never silently
    # omitted, including any genuinely isolated file.
    assert set(mg.in_degree.keys()) == all_files
    assert set(mg.out_degree.keys()) == all_files

    # Deterministic: re-running against the identical parse results
    # produces byte-identical clusters.
    mg2 = ModuleGraphBuilder(parse_results)
    assert mg.clusters == mg2.clusters
