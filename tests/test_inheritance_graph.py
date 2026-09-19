"""
Phase 4.1 -- Base-class/inheritance extraction (roadmap §37 item 4.1;
§11; §29: "Symbol... needs... base-class field"; §29: "CallEdge...
add confidence").

Architecture under test (approved explicitly by the user before
implementation -- see CODEPULSE_PROGRESS.md): raw base-class text on
`Symbol.base_classes` (parser layer), resolved into confidence-scored
`CallEdge` rows distinguished by `relationship_type='inherits'` (graph
layer), reusing the exact same 3-tier (high/low/unresolved) resolution
vocabulary and the exact same Phase 3.5 incremental-persistence
machinery already proven for ordinary calls -- never a second/parallel
mechanism.

This file covers, in order: (1) parser-level raw base-class extraction
for Python and JS/TS, (2) `CallGraphBuilder` inheritance-edge
resolution and confidence tiering, (3) the hard adjacency-separation
invariant (`get_callers`/`get_callees` must never surface inheritance
as a call), (4) end-to-end persistence through `index_directory()`
(full-run and incremental), and (5) repository isolation for
inheritance data specifically. Unit-level call-edge/confidence
coverage already exists (tests/test_graph.py, tests/
test_call_edge_confidence.py) and is not duplicated here.

All persistence tests use temporary, disposable SQLite databases and
temporary git repositories under `tmp_path` -- never the production
codepulse.db.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from codepulse.config import reset_settings
from codepulse.embeddings.embedder import CodeEmbedder
from codepulse.git_diff import get_head_commit_sha
from codepulse.indexer.engine import IndexingEngine
from codepulse.models import SymbolType
from codepulse.parser.graph import CallGraphBuilder
from codepulse.parser.javascript import JavaScriptParser
from codepulse.parser.python import PythonParser
from codepulse.storage.db import CallEdge, FileModel, StorageManager, SymbolModel


# --- 1. Parser-level raw base-class extraction --------------------------


def test_python_extracts_single_base_class():
    parser = PythonParser()
    code = "class Dog(Animal):\n    pass\n"
    symbols = parser.extract_symbols(code, file_path="animals.py")
    cls = next(s for s in symbols if s.name == "Dog")
    assert cls.type == SymbolType.CLASS
    assert cls.base_classes == ["Animal"]


def test_python_extracts_multiple_base_classes():
    parser = PythonParser()
    code = "class Duck(Swimmer, Flyer):\n    pass\n"
    symbols = parser.extract_symbols(code, file_path="animals.py")
    cls = next(s for s in symbols if s.name == "Duck")
    assert cls.base_classes == ["Swimmer", "Flyer"]


def test_python_extracts_dotted_qualified_base_class():
    parser = PythonParser()
    code = "class Handler(base.RequestHandler):\n    pass\n"
    symbols = parser.extract_symbols(code, file_path="handlers.py")
    cls = next(s for s in symbols if s.name == "Handler")
    assert cls.base_classes == ["base.RequestHandler"]


def test_python_excludes_metaclass_keyword_argument():
    parser = PythonParser()
    code = "class Model(Base, metaclass=ModelMeta):\n    pass\n"
    symbols = parser.extract_symbols(code, file_path="models.py")
    cls = next(s for s in symbols if s.name == "Model")
    assert cls.base_classes == ["Base"]


def test_python_class_with_no_base_has_empty_base_classes():
    parser = PythonParser()
    code = "class Standalone:\n    pass\n"
    symbols = parser.extract_symbols(code, file_path="standalone.py")
    cls = next(s for s in symbols if s.name == "Standalone")
    assert cls.base_classes == []


def test_javascript_extracts_extends_base():
    parser = JavaScriptParser(is_typescript=False)
    code = "class Dog extends Animal {\n  bark() {}\n}\n"
    symbols = parser.extract_symbols(code, file_path="animals.js")
    cls = next(s for s in symbols if s.name == "Dog")
    assert cls.base_classes == ["Animal"]


def test_javascript_extracts_namespace_qualified_extends():
    parser = JavaScriptParser(is_typescript=False)
    code = "class Handler extends base.RequestHandler {\n}\n"
    symbols = parser.extract_symbols(code, file_path="handlers.js")
    cls = next(s for s in symbols if s.name == "Handler")
    assert cls.base_classes == ["base.RequestHandler"]


def test_javascript_class_with_no_extends_has_empty_base_classes():
    parser = JavaScriptParser(is_typescript=False)
    code = "class Standalone {\n}\n"
    symbols = parser.extract_symbols(code, file_path="standalone.js")
    cls = next(s for s in symbols if s.name == "Standalone")
    assert cls.base_classes == []


def test_typescript_extracts_extends_base():
    parser = JavaScriptParser(is_typescript=True)
    code = "class Dog extends Animal {\n  bark(): void {}\n}\n"
    symbols = parser.extract_symbols(code, file_path="animals.ts")
    cls = next(s for s in symbols if s.name == "Dog")
    assert cls.base_classes == ["Animal"]


def test_typescript_implements_is_not_treated_as_a_base_class():
    """§37 item 4.1 names "base-class/inheritance extraction," not
    "implements" -- a TS class implementing an interface but not
    extending anything must have an EMPTY base_classes list."""
    parser = JavaScriptParser(is_typescript=True)
    code = "class Service implements Runnable {\n}\n"
    symbols = parser.extract_symbols(code, file_path="service.ts")
    cls = next(s for s in symbols if s.name == "Service")
    assert cls.base_classes == []


def test_typescript_extends_and_implements_together_only_captures_extends():
    parser = JavaScriptParser(is_typescript=True)
    code = "class Dog extends Animal implements Runnable {\n}\n"
    symbols = parser.extract_symbols(code, file_path="animals.ts")
    cls = next(s for s in symbols if s.name == "Dog")
    assert cls.base_classes == ["Animal"]


# --- 2. CallGraphBuilder inheritance-edge resolution ---------------------


def _inheritance_edge(builder: CallGraphBuilder, source: str, target: str):
    matches = [
        e for e in builder.inheritance_edges
        if e.source_symbol_name == source and e.target_symbol_name == target
    ]
    assert len(matches) == 1, f"expected exactly one inheritance edge {source}->{target}, found {len(matches)}"
    return matches[0]


def test_inheritance_edge_high_confidence_for_import_resolved_base():
    py_parser = PythonParser()
    res_a = py_parser.parse_file("animals/base.py", "class Animal:\n    pass\n")
    res_b = py_parser.parse_file(
        "animals/dog.py",
        "from animals.base import Animal\n\n\nclass Dog(Animal):\n    pass\n",
    )
    builder = CallGraphBuilder([res_a, res_b])

    edge = _inheritance_edge(builder, "Dog", "Animal")
    assert edge.relationship_type == "inherits"
    assert edge.confidence == "high"
    assert edge.target_file == "animals/base.py"


def test_inheritance_edge_low_confidence_for_same_file_bare_name_match():
    py_parser = PythonParser()
    res = py_parser.parse_file(
        "animals.py",
        "class Animal:\n    pass\n\n\nclass Dog(Animal):\n    pass\n",
    )
    builder = CallGraphBuilder([res])

    edge = _inheritance_edge(builder, "Dog", "Animal")
    assert edge.relationship_type == "inherits"
    assert edge.confidence == "low"
    assert edge.target_file == "animals.py"


def test_inheritance_edge_unresolved_for_a_true_external_base():
    py_parser = PythonParser()
    res = py_parser.parse_file(
        "views.py",
        "import django.views\n\n\nclass MyView(django.views.View):\n    pass\n",
    )
    builder = CallGraphBuilder([res])

    matches = [e for e in builder.inheritance_edges if e.source_symbol_name == "MyView"]
    assert len(matches) == 1
    edge = matches[0]
    assert edge.confidence == "unresolved"


def test_inheritance_resolution_reuses_the_exact_three_tier_vocabulary():
    """No fourth 'ambiguous' tier -- every inheritance edge's confidence
    must be one of the exact same three values calls already use."""
    py_parser = PythonParser()
    res_a = py_parser.parse_file("base.py", "class Base:\n    pass\n")
    res_b = py_parser.parse_file(
        "mixed.py",
        "from base import Base\n\n\n"
        "class Local:\n    pass\n\n\n"
        "class HighConf(Base):\n    pass\n\n\n"
        "class LowConf(Local):\n    pass\n\n\n"
        "class Unresolved(SomeExternalThing):\n    pass\n",
    )
    builder = CallGraphBuilder([res_a, res_b])
    confidences = {e.source_symbol_name: e.confidence for e in builder.inheritance_edges}
    assert confidences["HighConf"] == "high"
    assert confidences["LowConf"] == "low"
    assert confidences["Unresolved"] == "unresolved"
    assert set(confidences.values()) <= {"high", "low", "unresolved"}


# --- 3. Adjacency separation / regression: calls vs inheritance --------


def test_inheritance_edges_never_appear_in_the_calls_only_edge_list():
    py_parser = PythonParser()
    res = py_parser.parse_file(
        "animals.py",
        "class Animal:\n    def speak(self):\n        return self.make_sound()\n\n\n"
        "class Dog(Animal):\n    pass\n",
    )
    builder = CallGraphBuilder([res])

    assert all(e.relationship_type == "calls" for e in builder.edges)
    assert all(e.relationship_type == "inherits" for e in builder.inheritance_edges)
    assert not any(e.target_symbol_name == "Animal" and e.source_symbol_name == "Dog" for e in builder.edges)


def test_calls_only_adjacency_has_no_inheritance_keys():
    py_parser = PythonParser()
    res = py_parser.parse_file(
        "animals.py",
        "class Animal:\n    pass\n\n\nclass Dog(Animal):\n    pass\n",
    )
    builder = CallGraphBuilder([res])

    dog_key = ("animals.py", "Dog")
    assert dog_key not in builder.adjacency or ("animals.py", "Animal") not in builder.adjacency.get(dog_key, set())
    assert dog_key in builder.inheritance_adjacency
    assert ("animals.py", "Animal") in builder.inheritance_adjacency[dog_key]


def test_get_callers_and_get_callees_are_unaffected_by_inheritance_edges():
    """Regression: a subclass must never appear as a "caller" of its
    base class, and a base class must never appear as a "callee" of its
    subclass, via get_callers/get_callees -- those traverse the
    calls-only structures exclusively."""
    py_parser = PythonParser()
    res_a = py_parser.parse_file(
        "db.py",
        'def db_connect():\n    return "conn"\n\ndef db_query(q):\n    conn = db_connect()\n    return conn\n',
    )
    res_b = py_parser.parse_file(
        "animals.py",
        "class Animal:\n    pass\n\n\nclass Dog(Animal):\n    pass\n",
    )
    builder = CallGraphBuilder([res_a, res_b])

    # Pre-existing call-graph behavior, unchanged.
    callers_of_db_connect = [c["symbol"] for c in builder.get_callers("db_connect", "db.py")]
    assert "db_query" in callers_of_db_connect

    # New Phase 4.1 data must not leak into these calls-only queries.
    callees_of_dog = [c["symbol"] for c in builder.get_callees("Dog", "animals.py")]
    assert "Animal" not in callees_of_dog
    callers_of_animal = [c["symbol"] for c in builder.get_callers("Animal", "animals.py")]
    assert "Dog" not in callers_of_animal


# --- 4. End-to-end persistence through index_directory() ----------------


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


def _new_engine(db_url: str, embedder: CodeEmbedder) -> IndexingEngine:
    storage = StorageManager(db_url=db_url)
    return IndexingEngine(storage=storage, embedder=embedder)


def _db_url(tmp_path, name: str) -> str:
    db_path = tmp_path / name
    return f"sqlite:///{str(db_path).replace(os.sep, '/')}"


ANIMAL_AND_DOG = (
    "class Animal:\n    \"\"\"Base animal.\"\"\"\n    pass\n\n\n"
    "class Dog(Animal):\n    \"\"\"A dog.\"\"\"\n    pass\n"
)


def test_full_index_persists_base_classes_and_inherits_edge(tmp_path, shared_embedder):
    repo = tmp_path / "repo_full_inherit"
    _init_repo(repo)
    _write(repo, "animals.py", ANIMAL_AND_DOG)
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "full_inherit.db"), shared_embedder)
    result = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result["repository_id"]

    session = engine.storage.get_session()
    try:
        dog = session.query(SymbolModel).join(FileModel).filter(
            FileModel.repo_id == repo_id, SymbolModel.name == "Dog"
        ).first()
        assert dog is not None
        assert json.loads(dog.base_classes) == ["Animal"]

        inherits_edge = session.query(CallEdge).filter(
            CallEdge.repo_id == repo_id,
            CallEdge.source_symbol_name == "Dog",
            CallEdge.target_symbol_name == "Animal",
        ).first()
        assert inherits_edge is not None
        assert inherits_edge.relationship_type == "inherits"
        assert inherits_edge.confidence == "low"

        # Untouched: Animal itself has no base classes.
        animal = session.query(SymbolModel).join(FileModel).filter(
            FileModel.repo_id == repo_id, SymbolModel.name == "Animal"
        ).first()
        assert animal.base_classes is None
    finally:
        session.close()


def test_full_reindex_replaces_inheritance_edges_instead_of_accumulating(tmp_path, shared_embedder):
    repo = tmp_path / "repo_full_inherit_replace"
    _init_repo(repo)
    _write(repo, "animals.py", ANIMAL_AND_DOG)
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "full_inherit_replace.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]

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
        edges = session.query(CallEdge).filter(
            CallEdge.repo_id == repo_id,
            CallEdge.source_symbol_name == "Dog",
            CallEdge.target_symbol_name == "Animal",
        ).all()
        assert len(edges) == 1, "a second full re-index must REPLACE inheritance edges, not duplicate them"
    finally:
        session.close()


def test_incremental_index_updates_inheritance_edge_when_base_class_changes(tmp_path, shared_embedder):
    """Scenario A: Child(Base) -> Child(OtherBase). The old edge must
    disappear and a new one must take its place."""
    repo = tmp_path / "repo_base_changed"
    _init_repo(repo)
    _write(
        repo, "classes.py",
        "class Base:\n    pass\n\n\nclass OtherBase:\n    pass\n\n\nclass Child(Base):\n    pass\n",
    )
    _write(repo, "keep1.py", "def k1():\n    return 1\n")
    _write(repo, "keep2.py", "def k2():\n    return 2\n")
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "base_changed.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]

    _write(
        repo, "classes.py",
        "class Base:\n    pass\n\n\nclass OtherBase:\n    pass\n\n\nclass Child(OtherBase):\n    pass\n",
    )
    _commit_all(repo, "swap child's base class")
    result2 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result2["incremental"] is True

    session = engine.storage.get_session()
    try:
        edges = session.query(CallEdge).filter(
            CallEdge.repo_id == repo_id,
            CallEdge.source_symbol_name == "Child",
            CallEdge.relationship_type == "inherits",
        ).all()
        targets = {e.target_symbol_name for e in edges}
        assert targets == {"OtherBase"}, f"expected only OtherBase, got {targets}"
    finally:
        session.close()


def test_incremental_index_adds_inheritance_edge_when_base_class_added(tmp_path, shared_embedder):
    """Scenario B: a previously base-less class gains a base class."""
    repo = tmp_path / "repo_base_added"
    _init_repo(repo)
    _write(repo, "classes.py", "class Base:\n    pass\n\n\nclass Standalone:\n    pass\n")
    _write(repo, "keep1.py", "def k1():\n    return 1\n")
    _write(repo, "keep2.py", "def k2():\n    return 2\n")
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "base_added.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]

    session = engine.storage.get_session()
    try:
        before = session.query(CallEdge).filter(
            CallEdge.repo_id == repo_id, CallEdge.relationship_type == "inherits"
        ).count()
        assert before == 0
    finally:
        session.close()

    _write(repo, "classes.py", "class Base:\n    pass\n\n\nclass Standalone(Base):\n    pass\n")
    _commit_all(repo, "add base class to Standalone")
    result2 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result2["incremental"] is True

    session = engine.storage.get_session()
    try:
        edge = session.query(CallEdge).filter(
            CallEdge.repo_id == repo_id,
            CallEdge.source_symbol_name == "Standalone",
            CallEdge.relationship_type == "inherits",
        ).first()
        assert edge is not None
        assert edge.target_symbol_name == "Base"
    finally:
        session.close()


def test_incremental_index_removes_inheritance_edge_when_base_class_removed(tmp_path, shared_embedder):
    """Scenario C: a class loses its base class entirely -- the stale
    inherits edge must not remain."""
    repo = tmp_path / "repo_base_removed"
    _init_repo(repo)
    _write(repo, "classes.py", ANIMAL_AND_DOG)
    _write(repo, "keep1.py", "def k1():\n    return 1\n")
    _write(repo, "keep2.py", "def k2():\n    return 2\n")
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "base_removed.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]

    _write(
        repo, "classes.py",
        "class Animal:\n    \"\"\"Base animal.\"\"\"\n    pass\n\n\n"
        "class Dog:\n    \"\"\"No longer a subclass.\"\"\"\n    pass\n",
    )
    _commit_all(repo, "remove Dog's base class")
    result2 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    assert result2["incremental"] is True

    session = engine.storage.get_session()
    try:
        edges = session.query(CallEdge).filter(
            CallEdge.repo_id == repo_id,
            CallEdge.source_symbol_name == "Dog",
            CallEdge.relationship_type == "inherits",
        ).all()
        assert edges == [], "a removed base class must leave no stale inherits edge behind"

        dog = session.query(SymbolModel).join(FileModel).filter(
            FileModel.repo_id == repo_id, SymbolModel.name == "Dog"
        ).first()
        assert dog.base_classes is None or json.loads(dog.base_classes) == []
    finally:
        session.close()


def test_repeated_incremental_runs_do_not_accumulate_duplicate_inheritance_edges(tmp_path, shared_embedder):
    repo = tmp_path / "repo_inherit_no_dup"
    _init_repo(repo)
    _write(repo, "classes.py", ANIMAL_AND_DOG)
    _write(repo, "keep1.py", "def k1():\n    return 1\n")
    _commit_all(repo, "initial")

    engine = _new_engine(_db_url(tmp_path, "inherit_no_dup.db"), shared_embedder)
    result1 = engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))
    repo_id = result1["repository_id"]

    for i in range(3):
        _write(
            repo, "classes.py",
            f"class Animal:\n    \"\"\"Edit {i}.\"\"\"\n    pass\n\n\nclass Dog(Animal):\n    pass\n",
        )
        _commit_all(repo, f"edit {i}")
        engine.index_directory(str(repo), repo_name="r", repo_url=str(repo))

    session = engine.storage.get_session()
    try:
        edges = session.query(CallEdge).filter(
            CallEdge.repo_id == repo_id,
            CallEdge.source_symbol_name == "Dog",
            CallEdge.relationship_type == "inherits",
        ).all()
        assert len(edges) == 1, f"expected exactly 1 Dog->Animal inherits edge, got {len(edges)}"
    finally:
        session.close()


# --- 5. Repository isolation for inheritance data -----------------------


def test_inheritance_resolution_does_not_cross_repository_boundaries(tmp_path, shared_embedder):
    """Two repositories both define a class `Base` and a subclass with
    the SAME names. Repo A's subclass must resolve only against Repo
    A's `Base`; Repo B's identically-named `Base` must never satisfy
    Repo A's resolution, and vice versa."""
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    _init_repo(repo_a)
    _write(repo_a, "classes.py", "class Base:\n    \"\"\"Repo A's Base.\"\"\"\n    pass\n\n\nclass Child(Base):\n    pass\n")
    _commit_all(repo_a, "initial")

    _init_repo(repo_b)
    _write(repo_b, "classes.py", "class Base:\n    \"\"\"Repo B's Base -- unrelated.\"\"\"\n    pass\n\n\nclass Unrelated:\n    pass\n")
    _commit_all(repo_b, "initial")

    engine = _new_engine(_db_url(tmp_path, "isolation.db"), shared_embedder)
    r_a = engine.index_directory(str(repo_a), repo_name="repo_a", repo_url=str(repo_a))
    r_b = engine.index_directory(str(repo_b), repo_name="repo_b", repo_url=str(repo_b))

    session = engine.storage.get_session()
    try:
        edge_a = session.query(CallEdge).filter(
            CallEdge.repo_id == r_a["repository_id"],
            CallEdge.source_symbol_name == "Child",
            CallEdge.relationship_type == "inherits",
        ).first()
        assert edge_a is not None
        assert edge_a.confidence == "low"

        # Repo B must have no "Child" symbol/edge at all -- it never
        # defined one, and Repo A's Child must never appear under B's id.
        leaked = session.query(CallEdge).filter(
            CallEdge.repo_id == r_b["repository_id"],
            CallEdge.source_symbol_name == "Child",
        ).first()
        assert leaked is None

        # Repo B's own inherits edges (none exist -- Unrelated has no
        # base) must not have been contaminated by Repo A's data either.
        b_inherits = session.query(CallEdge).filter(
            CallEdge.repo_id == r_b["repository_id"],
            CallEdge.relationship_type == "inherits",
        ).all()
        assert b_inherits == []
    finally:
        session.close()


def test_restart_and_reindex_preserves_inheritance_repository_isolation(tmp_path, shared_embedder):
    """A fresh IndexingEngine instance (simulating a process restart)
    re-indexing both repositories must not merge or cross-contaminate
    their inheritance edges."""
    repo_a = tmp_path / "repo_a_restart"
    repo_b = tmp_path / "repo_b_restart"
    _init_repo(repo_a)
    _write(repo_a, "classes.py", ANIMAL_AND_DOG)
    _commit_all(repo_a, "initial")

    _init_repo(repo_b)
    _write(repo_b, "classes.py", "class Animal:\n    pass\n\n\nclass Cat(Animal):\n    pass\n")
    _commit_all(repo_b, "initial")

    db_url = _db_url(tmp_path, "restart_isolation.db")
    engine1 = _new_engine(db_url, shared_embedder)
    r_a = engine1.index_directory(str(repo_a), repo_name="repo_a", repo_url=str(repo_a))
    r_b = engine1.index_directory(str(repo_b), repo_name="repo_b", repo_url=str(repo_b))

    # Simulate a restart: brand-new engine instance, same DB.
    engine2 = _new_engine(db_url, shared_embedder)
    engine2.index_directory(str(repo_a), repo_name="repo_a", repo_url=str(repo_a))
    engine2.index_directory(str(repo_b), repo_name="repo_b", repo_url=str(repo_b))

    session = engine2.storage.get_session()
    try:
        a_edges = session.query(CallEdge).filter(
            CallEdge.repo_id == r_a["repository_id"],
            CallEdge.relationship_type == "inherits",
        ).all()
        assert len(a_edges) == 1
        assert a_edges[0].source_symbol_name == "Dog"

        b_edges = session.query(CallEdge).filter(
            CallEdge.repo_id == r_b["repository_id"],
            CallEdge.relationship_type == "inherits",
        ).all()
        assert len(b_edges) == 1
        assert b_edges[0].source_symbol_name == "Cat"
    finally:
        session.close()
