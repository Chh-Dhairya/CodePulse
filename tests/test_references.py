"""
Phase 4.4 -- References endpoint (roadmap §37 item 4.4; §21 API
Strategy: "Endpoint groups: ... `references` (new) ... `graph`
(callers/callees/impact) ..." -- `references` is its own group,
explicitly separate from `graph`).

Approved architecture, derived directly from the roadmap and this
project's own established Phase 4 patterns (no separate approval
round-trip needed -- see CLAUDE.md's "Architectural Decision Rule":
roadmap intent is clear, existing architecture provides a direct
pattern, every choice below preserves existing contracts and
repository isolation):
  - "References" = DIRECT (single-hop) usages only, never a
    transitive/depth-bucketed traversal -- deliberately distinct from
    `analyze_impact` (Phase 4.2), which is calls-only and
    depth-bucketed. Preserving this distinction is itself the point
    (CLAUDE.md: "preserve the distinctions between... call graph,
    inheritance graph... references... impact analysis").
  - Two direct-reference kinds: `callers` (via `get_callers(...,
    max_depth=1)`, reusing that tested method exactly, not a new
    traversal) and `subclasses` (via the new `get_direct_subclasses`,
    reading ONLY the inheritance-specific adjacency Phase 4.1 already
    built, never the calls-only one). This gives inheritance data its
    first real query surface -- extracted since Phase 4.1, explicitly
    excluded from impact analysis by Phase 4.2's Decision 1, exposed
    here.
  - `IndexingEngine.analyze_references` is the shared intelligence-core
    method; `GET /v1/symbols/{id}/references` is a thin REST adapter,
    matching `analyze_impact`'s own established shape exactly.
  - No CLI command this milestone (`codepulse refs <name>` is
    explicitly Phase 6.1's "full CLI command set" item, not 4.4's --
    already disclosed this distinction throughout Phase 4). No MCP tool
    either (no MCP server exists until Phase 5.3, and §13's own named
    MCP tool list does not include a distinct "references" tool).

Unit-level call-graph/confidence/inheritance coverage already exists
(tests/test_graph.py, tests/test_inheritance_graph.py) and is not
duplicated here beyond what's needed to prove `get_direct_subclasses`
and `analyze_references` themselves.
"""
from __future__ import annotations

import os
import subprocess

import pytest
from fastapi.testclient import TestClient

import codepulse.api.app as app_module
from codepulse.api.app import app
from codepulse.config import reset_settings
from codepulse.embeddings.embedder import CodeEmbedder
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


def _commit_all(repo_path, message: str) -> None:
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


# --- 1-4: graph-level get_direct_subclasses -------------------------------

def test_get_direct_subclasses_finds_a_same_file_bare_name_match():
    parser = PythonParser()
    res = parser.parse_file(
        "animals.py",
        "class Animal:\n    pass\n\n\nclass Dog(Animal):\n    pass\n\n\nclass Cat(Animal):\n    pass\n",
    )
    builder = CallGraphBuilder([res])
    subclasses = builder.get_direct_subclasses("Animal", "animals.py")
    names = {s["symbol"] for s in subclasses}
    assert names == {"Dog", "Cat"}
    assert all(s["confidence"] == "low" for s in subclasses)


def test_get_direct_subclasses_finds_an_import_resolved_high_confidence_match():
    parser = PythonParser()
    res_a = parser.parse_file("base.py", "class Animal:\n    pass\n")
    res_b = parser.parse_file(
        "animals.py", "from base import Animal\n\n\nclass Dog(Animal):\n    pass\n"
    )
    builder = CallGraphBuilder([res_a, res_b])
    subclasses = builder.get_direct_subclasses("Animal", "base.py")
    assert subclasses == [{"symbol": "Dog", "file": "animals.py", "confidence": "high"}]


def test_get_direct_subclasses_is_direct_only_not_transitive():
    """Cat(Dog), Dog(Animal) -- Animal's direct subclasses must be
    {Dog} only, NEVER Cat (a grandchild, not a direct child)."""
    parser = PythonParser()
    res = parser.parse_file(
        "animals.py",
        "class Animal:\n    pass\n\n\nclass Dog(Animal):\n    pass\n\n\nclass Cat(Dog):\n    pass\n",
    )
    builder = CallGraphBuilder([res])
    subclasses = builder.get_direct_subclasses("Animal", "animals.py")
    names = {s["symbol"] for s in subclasses}
    assert names == {"Dog"}
    assert "Cat" not in names


def test_get_direct_subclasses_never_reads_the_calls_only_adjacency():
    """A function calling another function must never appear as a
    'subclass' -- get_direct_subclasses reads only the inheritance
    adjacency, never self.reverse_adjacency."""
    parser = PythonParser()
    res = parser.parse_file(
        "mod.py",
        "def helper():\n    return 1\n\n\ndef caller():\n    return helper()\n",
    )
    builder = CallGraphBuilder([res])
    assert builder.get_direct_subclasses("helper", "mod.py") == []


# --- 5. analyze_references end-to-end (engine level) ---------------------

REFERENCES_FIXTURE_MOD = (
    'class Animal:\n    """Base."""\n    pass\n\n\n'
    'class Dog(Animal):\n    """Direct subclass."""\n    pass\n\n\n'
    'def use_animal(a):\n    """A direct caller-of-nothing-relevant."""\n    return a\n\n\n'
    'def helper():\n    """Called by caller()."""\n    return 1\n\n\n'
    'def caller():\n    """Direct caller of helper()."""\n    return helper()\n\n\n'
    'def outer():\n    """Transitive (2-hop) caller of helper() -- must NOT appear as a direct reference."""\n    return caller()\n'
)


def _index_references_repo(tmp_path, dirname, embedder, db_name_or_engine):
    repo = tmp_path / dirname
    _init_repo(repo)
    _write(repo, "mod.py", REFERENCES_FIXTURE_MOD)
    _commit_all(repo, "initial")
    engine = db_name_or_engine if isinstance(db_name_or_engine, IndexingEngine) else _new_engine(
        _db_url(tmp_path, db_name_or_engine), embedder
    )
    result = engine.index_directory(str(repo), repo_name=dirname, repo_url=str(repo))
    return engine, result


def test_analyze_references_returns_direct_callers_and_subclasses(tmp_path, shared_embedder):
    engine, result = _index_references_repo(tmp_path, "repo_refs", shared_embedder, "refs.db")
    repo_id = result["repository_id"]

    animal_refs = engine.analyze_references("Animal", "mod.py", repo_id)
    assert animal_refs["symbol"] == "Animal"
    assert animal_refs["callers"] == []
    assert [s["symbol"] for s in animal_refs["subclasses"]] == ["Dog"]

    helper_refs = engine.analyze_references("helper", "mod.py", repo_id)
    assert [c["symbol"] for c in helper_refs["callers"]] == ["caller"]
    assert helper_refs["subclasses"] == []
    # helper() is a plain function -- never gets an inheritance entry.


def test_analyze_references_excludes_transitive_callers(tmp_path, shared_embedder):
    """outer() calls caller() which calls helper() -- helper()'s
    references must show ONLY caller() (depth 1), never outer()
    (depth 2), unlike analyze_impact which would bucket outer() as
    transitive."""
    engine, result = _index_references_repo(tmp_path, "repo_refs_transitive", shared_embedder, "refs_transitive.db")
    repo_id = result["repository_id"]

    helper_refs = engine.analyze_references("helper", "mod.py", repo_id)
    caller_names = {c["symbol"] for c in helper_refs["callers"]}
    assert caller_names == {"caller"}
    assert "outer" not in caller_names


def test_analyze_references_does_not_leak_across_repositories(tmp_path, shared_embedder):
    """Two repositories, identical class/function names throughout."""
    engine, r_a = _index_references_repo(tmp_path, "repo_a_refs", shared_embedder, "refs_iso.db")
    engine, r_b = _index_references_repo(tmp_path, "repo_b_refs", shared_embedder, engine)

    refs_a = engine.analyze_references("Animal", "mod.py", r_a["repository_id"])
    refs_b = engine.analyze_references("Animal", "mod.py", r_b["repository_id"])

    assert [s["symbol"] for s in refs_a["subclasses"]] == ["Dog"]
    assert [s["symbol"] for s in refs_b["subclasses"]] == ["Dog"]
    # Each repository's own analysis must be independently correct --
    # not merged, not doubled, not empty due to cross-repo confusion.
    assert len(refs_a["subclasses"]) == 1
    assert len(refs_b["subclasses"]) == 1


def test_analyze_references_returns_empty_lists_for_a_cold_cache(tmp_path, shared_embedder):
    """Restart-like behavior: a repo_id with no cached parse_results
    must degrade to empty lists, never an error."""
    engine = _new_engine(_db_url(tmp_path, "refs_cold.db"), shared_embedder)
    refs = engine.analyze_references("Anything", "anywhere.py", repo_id=999)
    assert refs == {"symbol": "Anything", "callers": [], "subclasses": []}


# --- 6. REST endpoint ------------------------------------------------------

def test_get_symbol_references_endpoint_returns_correct_shape(tmp_path, shared_embedder, monkeypatch):
    engine, result = _index_references_repo(tmp_path, "repo_refs_rest", shared_embedder, "refs_rest.db")
    repo_id = result["repository_id"]

    from codepulse.storage.db import FileModel, SymbolModel
    session = engine.storage.get_session()
    try:
        animal_sym = session.query(SymbolModel).join(FileModel).filter(
            FileModel.repo_id == repo_id, SymbolModel.name == "Animal"
        ).first()
        symbol_id = animal_sym.id
    finally:
        session.close()

    monkeypatch.setattr(app_module, "storage_manager", engine.storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    client = TestClient(app)
    headers = make_api_key_headers(engine.storage, repository_ids=[repo_id])

    response = client.get(f"/v1/symbols/{symbol_id}/references", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "Animal"
    assert [s["symbol"] for s in body["subclasses"]] == ["Dog"]
    assert body["callers"] == []


def test_get_symbol_references_endpoint_forbidden_for_a_different_repository(tmp_path, shared_embedder, monkeypatch):
    engine, r_a = _index_references_repo(tmp_path, "repo_a_refs_rest", shared_embedder, "refs_rest_iso.db")
    engine, r_b = _index_references_repo(tmp_path, "repo_b_refs_rest", shared_embedder, engine)

    from codepulse.storage.db import FileModel, SymbolModel
    session = engine.storage.get_session()
    try:
        b_sym = session.query(SymbolModel).join(FileModel).filter(
            FileModel.repo_id == r_b["repository_id"], SymbolModel.name == "Animal"
        ).first()
        symbol_id = b_sym.id
    finally:
        session.close()

    monkeypatch.setattr(app_module, "storage_manager", engine.storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    client = TestClient(app)
    headers = make_api_key_headers(engine.storage, repository_ids=[r_a["repository_id"]])

    response = client.get(f"/v1/symbols/{symbol_id}/references", headers=headers)
    assert response.status_code == 403


def test_get_symbol_references_endpoint_rejected_without_any_key(tmp_path, shared_embedder, monkeypatch):
    engine, result = _index_references_repo(tmp_path, "repo_refs_noauth", shared_embedder, "refs_noauth.db")
    monkeypatch.setattr(app_module, "storage_manager", engine.storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    client = TestClient(app)

    response = client.get("/v1/symbols/1/references")
    assert response.status_code == 401


def test_get_symbol_references_endpoint_404_for_missing_symbol(tmp_path, shared_embedder, monkeypatch):
    engine, result = _index_references_repo(tmp_path, "repo_refs_404", shared_embedder, "refs_404.db")
    monkeypatch.setattr(app_module, "storage_manager", engine.storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    client = TestClient(app)
    headers = make_api_key_headers(engine.storage, repository_ids=[result["repository_id"]])

    response = client.get("/v1/symbols/999999/references", headers=headers)
    assert response.status_code == 404
