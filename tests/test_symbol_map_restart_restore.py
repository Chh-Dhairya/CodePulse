"""
`symbols_by_id` / `symbol_repo_map` survive a restart (follow-up to
tests/test_call_graph_restart_restore.py; CODEPULSE_FUTURE_CHANGES.md).

After the call graph itself became restorable, the in-memory symbol map was
the remaining restart gap. It feeds:
  - the graph component (20%) of hybrid ranking -- scores are computed by
    walking `symbols_by_id`, so with the map empty the component vanished
    after every restart and the same query ranked differently;
  - `GET /v1/autocomplete` (iterates `symbols_by_id`), which returned
    nothing after a restart;
  - the unscoped-stats fallback and `indexed_symbols` in the index-status
    endpoint.

The map is a derived cache of the durable `symbols`/`files` rows, restored by
`hydrate_repository` alongside the keyword index (same rows, same doc_id).

Because the map can now start non-empty, a FULL re-index must wipe that
repository's entries before re-adding the current symbols -- otherwise a
symbol from a file deleted since the last index would linger (the same-process
case was a latent version of this).

Temporary databases only; Restart Model B as in test_restart_idempotency.py.
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
from codepulse.storage.db import StorageManager
from codepulse.symbol_identity import symbol_doc_id
from tests.conftest import make_api_key_headers


@pytest.fixture(autouse=True)
def cleanup_settings():
    reset_settings()
    yield
    reset_settings()


@pytest.fixture(scope="module")
def shared_embedder():
    return CodeEmbedder()


CORE = (
    "def add(a, b):\n    \"\"\"Add two numbers.\"\"\"\n    return a + b\n\n\n"
    "def double(x):\n    \"\"\"Double a number.\"\"\"\n    return add(x, x)\n"
)
APP = (
    "from core import add\n\n\n"
    "def total(values):\n    \"\"\"Sum a list of values.\"\"\"\n    out = 0\n    for v in values:\n        out = add(out, v)\n    return out\n\n\n"
    "def report(values):\n    \"\"\"Print the total.\"\"\"\n    print(total(values))\n"
)
GONE = "def doomed():\n    \"\"\"This file is deleted before the re-index.\"\"\"\n    return 0\n"


def _make_repo(tmp_path, name, files):
    repo = tmp_path / name
    repo.mkdir()
    for rel, content in files.items():
        (repo / rel).write_text(content, encoding="utf-8")
    return repo


def _db_url(tmp_path, name):
    return f"sqlite:///{str(tmp_path / name).replace(os.sep, '/')}"


def _new_engine(db_url, embedder):
    return IndexingEngine(storage=StorageManager(db_url=db_url), embedder=embedder)


def _restart(engine, db_url, embedder):
    engine.storage.engine.dispose()
    return _new_engine(db_url, embedder)


def _symbol_view(engine):
    """The parts of every cached symbol that consumers read, plus its repo."""
    return {
        doc_id: (sym.name, str(sym.type), sym.file_path, sym.start_line, sym.signature, engine.symbol_repo_map[doc_id])
        for doc_id, sym in engine.symbols_by_id.items()
    }


def _ranked(results):
    return [(r.symbol_name, r.file_path, round(r.final_score, 6)) for r in results]


QUERIES = ["add numbers", "total of values", "double", "print report"]


def test_restart_restores_the_symbol_map_and_repo_map(tmp_path, shared_embedder):
    repo = _make_repo(tmp_path, "r1", {"core.py": CORE, "app.py": APP})
    db_url = _db_url(tmp_path, "restore.db")
    engine1 = _new_engine(db_url, shared_embedder)
    engine1.index_directory(str(repo), repo_name="r1", repo_url=str(repo))
    assert engine1.symbols_by_id, "fixture sanity"

    engine2 = _restart(engine1, db_url, shared_embedder)

    assert _symbol_view(engine2) == _symbol_view(engine1)


@pytest.mark.parametrize("search_type", ["hybrid", "keyword", "semantic"])
def test_search_ranks_identically_before_and_after_a_restart(tmp_path, shared_embedder, search_type):
    repo = _make_repo(tmp_path, "r2", {"core.py": CORE, "app.py": APP})
    db_url = _db_url(tmp_path, f"rank_{search_type}.db")
    engine1 = _new_engine(db_url, shared_embedder)
    repo_id = engine1.index_directory(str(repo), repo_name="r2", repo_url=str(repo))["repository_id"]
    before = {q: _ranked(engine1.search(q, limit=10, search_type=search_type, repository_id=repo_id)) for q in QUERIES}
    before_unscoped = {q: _ranked(engine1.search(q, limit=10, search_type=search_type)) for q in QUERIES}
    assert any(before.values()), "fixture sanity"

    engine2 = _restart(engine1, db_url, shared_embedder)

    for q in QUERIES:
        assert _ranked(engine2.search(q, limit=10, search_type=search_type, repository_id=repo_id)) == before[q], q
        assert _ranked(engine2.search(q, limit=10, search_type=search_type)) == before_unscoped[q], q


def test_the_graph_component_is_present_after_a_restart(tmp_path, shared_embedder):
    """The concrete symptom: `add` has callers, so its hybrid score carries a
    non-zero graph contribution -- which used to vanish after a restart."""
    repo = _make_repo(tmp_path, "r3", {"core.py": CORE, "app.py": APP})
    db_url = _db_url(tmp_path, "graph_component.db")
    engine1 = _new_engine(db_url, shared_embedder)
    repo_id = engine1.index_directory(str(repo), repo_name="r3", repo_url=str(repo))["repository_id"]

    engine2 = _restart(engine1, db_url, shared_embedder)

    hits = engine2.search("add", limit=10, search_type="hybrid", repository_id=repo_id)
    add_hit = next(r for r in hits if r.symbol_name == "add")
    assert add_hit.graph_score > 0


def test_symbol_map_is_restored_per_repository_with_the_right_repo_id(tmp_path, shared_embedder):
    repo_a = _make_repo(tmp_path, "ra", {"m.py": "def helper():\n    return 1\n"})
    repo_b = _make_repo(tmp_path, "rb", {"m.py": "def helper():\n    return 2\n\n\ndef only_b():\n    return helper()\n"})
    db_url = _db_url(tmp_path, "isolation.db")
    engine1 = _new_engine(db_url, shared_embedder)
    id_a = engine1.index_directory(str(repo_a), repo_name="ra", repo_url=str(repo_a))["repository_id"]
    id_b = engine1.index_directory(str(repo_b), repo_name="rb", repo_url=str(repo_b))["repository_id"]

    engine2 = _restart(engine1, db_url, shared_embedder)

    assert engine2.symbol_repo_map[symbol_doc_id(id_a, "m.py", "helper", 1)] == id_a
    assert engine2.symbol_repo_map[symbol_doc_id(id_b, "m.py", "helper", 1)] == id_b
    assert symbol_doc_id(id_a, "m.py", "only_b", 5) not in engine2.symbols_by_id
    assert engine2.symbol_repo_map[symbol_doc_id(id_b, "m.py", "only_b", 5)] == id_b


def test_autocomplete_works_immediately_after_a_restart(tmp_path, shared_embedder, monkeypatch):
    repo = _make_repo(tmp_path, "r4", {"core.py": CORE, "app.py": APP})
    db_url = _db_url(tmp_path, "autocomplete.db")
    engine1 = _new_engine(db_url, shared_embedder)
    repo_id = engine1.index_directory(str(repo), repo_name="r4", repo_url=str(repo))["repository_id"]

    engine2 = _restart(engine1, db_url, shared_embedder)
    monkeypatch.setattr(app_module, "storage_manager", engine2.storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine2)
    headers = make_api_key_headers(engine2.storage, repository_ids=[repo_id])

    response = TestClient(app).get("/v1/autocomplete?prefix=tot", headers=headers)

    assert response.status_code == 200
    assert [s["name"] for s in response.json()["suggestions"]] == ["total"]


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_a_full_reindex_drops_symbols_of_files_deleted_since_the_last_index(tmp_path, shared_embedder):
    """Same process: the map used to keep a deleted file's symbols forever."""
    repo = _make_repo(tmp_path, "r5", {"core.py": CORE, "gone.py": GONE})
    engine = _new_engine(_db_url(tmp_path, "stale_same_process.db"), shared_embedder)
    repo_id = engine.index_directory(str(repo), repo_name="r5", repo_url=str(repo))["repository_id"]
    assert symbol_doc_id(repo_id, "gone.py", "doomed", 1) in engine.symbols_by_id

    (repo / "gone.py").unlink()
    engine.index_directory(str(repo), repo_name="r5", repo_url=str(repo))  # not a git repo -> full run

    assert symbol_doc_id(repo_id, "gone.py", "doomed", 1) not in engine.symbols_by_id
    assert symbol_doc_id(repo_id, "gone.py", "doomed", 1) not in engine.symbol_repo_map
    assert symbol_doc_id(repo_id, "core.py", "add", 1) in engine.symbols_by_id


def test_a_full_reindex_after_a_restart_does_not_resurrect_deleted_symbols(tmp_path, shared_embedder):
    repo = _make_repo(tmp_path, "r6", {"core.py": CORE, "gone.py": GONE})
    db_url = _db_url(tmp_path, "stale_after_restart.db")
    engine1 = _new_engine(db_url, shared_embedder)
    repo_id = engine1.index_directory(str(repo), repo_name="r6", repo_url=str(repo))["repository_id"]
    engine2 = _restart(engine1, db_url, shared_embedder)
    assert symbol_doc_id(repo_id, "gone.py", "doomed", 1) in engine2.symbols_by_id  # restored from the durable rows

    (repo / "gone.py").unlink()
    engine2.index_directory(str(repo), repo_name="r6", repo_url=str(repo))

    assert symbol_doc_id(repo_id, "gone.py", "doomed", 1) not in engine2.symbols_by_id
    assert engine2.symbol_repo_map.get(symbol_doc_id(repo_id, "gone.py", "doomed", 1)) is None


def test_a_full_reindex_only_clears_its_own_repositorys_entries(tmp_path, shared_embedder):
    repo_a = _make_repo(tmp_path, "rx", {"m.py": "def a_fn():\n    return 1\n"})
    repo_b = _make_repo(tmp_path, "ry", {"m.py": "def b_fn():\n    return 1\n"})
    engine = _new_engine(_db_url(tmp_path, "scoped_clear.db"), shared_embedder)
    id_a = engine.index_directory(str(repo_a), repo_name="rx", repo_url=str(repo_a))["repository_id"]
    id_b = engine.index_directory(str(repo_b), repo_name="ry", repo_url=str(repo_b))["repository_id"]

    engine.index_directory(str(repo_a), repo_name="rx", repo_url=str(repo_a))

    assert symbol_doc_id(id_b, "m.py", "b_fn", 1) in engine.symbols_by_id
    assert symbol_doc_id(id_a, "m.py", "a_fn", 1) in engine.symbols_by_id
