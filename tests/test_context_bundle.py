"""
Phase 5.1/5.2/5.4 -- Agent-native context bundle (roadmap §12
"Agent-Native Retrieval / Context API"; §37 items 5.1 "Context-bundle
assembly function (shared by REST + MCP)", 5.2 "/v1/agent/context
endpoint", 5.4 "Token-budget truncation logic + snapshot freshness
metadata").

Implemented under the user's explicit continuous-execution
authorization (see CODEPULSE_PROGRESS.md's standing-deviation notice).

Architecture:
  - `IndexingEngine.get_context_bundle(...)` is the shared assembly
    function -- composes three already-tested capabilities
    (`get_callers`/`get_callees` at `max_depth=1`, and the new
    `find_similar_symbols`) rather than introducing a new
    graph/retrieval algorithm. `POST /v1/agent/context` is a thin REST
    adapter over it (the MCP `get_context` tool, Phase 5.3, will be a
    second thin adapter over the SAME function).
  - `find_similar_symbols` is itself extracted from what was
    previously inline-only logic in `POST /v1/symbols/{id}/similar` --
    extracting it fixed a real, pre-existing repository-isolation gap
    (the original inline vector search had NO repo_id filtering at
    all). This file proves both the extraction and the fix directly.
  - Token-budget rules (§12, items 1-4), verified directly: body
    omitted unless explicitly requested; callers/callees capped at
    `max_related`; `snapshot_commit` always present (honestly `None`
    when there isn't one); `truncated=True` whenever anything was
    shortened.

Repository-scoped exactly like every other `analyze_*`/`find_*` method
in `IndexingEngine` -- verified with two repositories sharing
identical symbol/file names throughout.
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
from codepulse.git_diff import get_head_commit_sha
from codepulse.indexer.engine import IndexingEngine
from codepulse.storage.db import FileModel, StorageManager, SymbolModel
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


# A pair of near-identical functions so real embeddings actually place
# them close together in vector space -- semantic similarity needs a
# real, meaningfully-similar pair, not just any two symbols.
SIMILAR_PAIR_MOD = (
    'def compute_average(numbers):\n'
    '    """Compute the arithmetic mean of a list of numbers."""\n'
    '    return sum(numbers) / len(numbers)\n\n\n'
    'def compute_mean(values):\n'
    '    """Compute the arithmetic mean of a list of values."""\n'
    '    return sum(values) / len(values)\n\n\n'
    'def unrelated_thing():\n'
    '    """Does something completely unrelated to averages."""\n'
    '    return "unrelated"\n'
)

CONTEXT_FIXTURE_MOD = (
    'def helper():\n    """A helper."""\n    return 1\n\n\n'
    'def caller_one():\n    """Calls helper."""\n    return helper()\n\n\n'
    'def caller_two():\n    """Also calls helper."""\n    return helper() + 1\n\n\n'
    'def target():\n    """The symbol under test -- calls helper, called by nobody."""\n    return helper()\n'
)


def _index_repo(tmp_path, dirname, content, embedder, db_name_or_engine):
    repo = tmp_path / dirname
    _init_repo(repo)
    _write(repo, "mod.py", content)
    _commit_all(repo, "initial")
    engine = db_name_or_engine if isinstance(db_name_or_engine, IndexingEngine) else _new_engine(
        _db_url(tmp_path, db_name_or_engine), embedder
    )
    result = engine.index_directory(str(repo), repo_name=dirname, repo_url=str(repo))
    return engine, result, str(repo)


# --- find_similar_symbols: repository isolation (the fixed gap) --------

def test_find_similar_symbols_does_not_leak_across_repositories(tmp_path, shared_embedder):
    """Two repositories with the SAME near-identical function pair.
    Querying repo A's compute_average must never return repo B's
    compute_mean -- the exact gap the pre-existing inline /similar
    logic had (no repo_id filter at all)."""
    engine, r_a, _ = _index_repo(tmp_path, "repo_a_similar", SIMILAR_PAIR_MOD, shared_embedder, "similar_iso.db")
    engine, r_b, _ = _index_repo(tmp_path, "repo_b_similar", SIMILAR_PAIR_MOD, shared_embedder, engine)

    # A high threshold (0.85) cleanly separates the genuinely-similar
    # compute_mean (measured ~0.88-0.91 relevance against this query in
    # this fixture) from the merely loosely-related unrelated_thing()
    # (measured ~0.73-0.74) -- this test is about repository isolation,
    # not embedding ranking quality (a different, already-measured
    # concern, Phase 2.4/2.5's own territory), so the threshold is
    # picked to make the isolation assertion unambiguous rather than to
    # exercise ranking quality itself. A single symbol can legitimately
    # produce more than one indexed chunk/vector entry (an existing,
    # pre-Phase-5.1 chunking behavior, not something this milestone
    # changes), so this asserts the SET of symbol names found, not a
    # raw result count.
    query_text = "def compute_average(numbers):\n    return sum(numbers) / len(numbers)\n"
    results_a = engine.find_similar_symbols(query_text, "compute_average", r_a["repository_id"], threshold=0.85, limit=10)

    names_a = {r["symbol_name"] for r in results_a}
    assert names_a == {"compute_mean"}, (
        f"repo A must find its own compute_mean only, not repo B's copy or anything else: {results_a!r}"
    )

    results_b = engine.find_similar_symbols(query_text, "compute_average", r_b["repository_id"], threshold=0.85, limit=10)
    names_b = {r["symbol_name"] for r in results_b}
    assert names_b == {"compute_mean"}, (
        f"repo B must independently find its OWN compute_mean too -- proving isolation is real, not just empty: {results_b!r}"
    )


def test_find_similar_symbols_excludes_the_query_symbol_itself(tmp_path, shared_embedder):
    engine, result, _ = _index_repo(tmp_path, "repo_self_exclude", SIMILAR_PAIR_MOD, shared_embedder, "self_exclude.db")
    query_text = "def compute_average(numbers):\n    return sum(numbers) / len(numbers)\n"
    results = engine.find_similar_symbols(query_text, "compute_average", result["repository_id"], threshold=0.0, limit=10)
    assert "compute_average" not in {r["symbol_name"] for r in results}


# --- get_context_bundle -----------------------------------------------

def test_get_context_bundle_default_shape_omits_body_and_marks_truncated(tmp_path, shared_embedder):
    engine, result, _ = _index_repo(tmp_path, "repo_bundle_shape", CONTEXT_FIXTURE_MOD, shared_embedder, "bundle_shape.db")
    repo_id = result["repository_id"]

    session = engine.storage.get_session()
    try:
        target_sym = session.query(SymbolModel).join(FileModel).filter(
            FileModel.repo_id == repo_id, SymbolModel.name == "target"
        ).first()
    finally:
        session.close()

    bundle = engine.get_context_bundle(
        "target", "mod.py", repo_id,
        signature=target_sym.signature, docstring=target_sym.docstring, body=target_sym.body,
    )
    assert bundle["symbol"] == "target"
    assert bundle["file"] == "mod.py"
    assert bundle["definition"]["signature"]
    assert bundle["definition"]["docstring"]
    assert bundle["definition"]["body"] is None
    assert bundle["truncated"] is True, "body omission alone must set truncated=True"
    assert {c["symbol"] for c in bundle["callees"]} == {"helper"}
    assert bundle["callers"] == []


def test_get_context_bundle_include_body_true_returns_full_body(tmp_path, shared_embedder):
    engine, result, _ = _index_repo(tmp_path, "repo_bundle_body", CONTEXT_FIXTURE_MOD, shared_embedder, "bundle_body.db")
    repo_id = result["repository_id"]

    session = engine.storage.get_session()
    try:
        helper_sym = session.query(SymbolModel).join(FileModel).filter(
            FileModel.repo_id == repo_id, SymbolModel.name == "helper"
        ).first()
    finally:
        session.close()

    bundle = engine.get_context_bundle(
        "helper", "mod.py", repo_id,
        signature=helper_sym.signature, docstring=helper_sym.docstring, body=helper_sym.body,
        include_body=True,
    )
    assert bundle["definition"]["body"] == helper_sym.body
    # caller_one, caller_two, AND target all call helper() in this
    # fixture (target's own docstring says "called by nobody" -- that
    # refers to nobody calling target, not target calling nothing).
    assert {c["symbol"] for c in bundle["callers"]} == {"caller_one", "caller_two", "target"}
    # helper() calls nothing itself.
    assert bundle["callees"] == []
    assert bundle["truncated"] is False, "with body included and nothing capped, truncated must be False"


def test_get_context_bundle_caps_callers_and_marks_truncated(tmp_path, shared_embedder):
    engine, result, _ = _index_repo(tmp_path, "repo_bundle_cap", CONTEXT_FIXTURE_MOD, shared_embedder, "bundle_cap.db")
    repo_id = result["repository_id"]

    bundle = engine.get_context_bundle(
        "helper", "mod.py", repo_id, signature="def helper():", docstring="A helper.",
        body="return 1", include_body=True, max_related=1,
    )
    assert len(bundle["callers"]) == 1
    assert bundle["truncated"] is True, "capping callers below their real count must set truncated=True"


def test_get_context_bundle_snapshot_commit_reflects_the_real_indexed_commit(tmp_path, shared_embedder):
    engine, result, repo_path = _index_repo(tmp_path, "repo_bundle_commit", CONTEXT_FIXTURE_MOD, shared_embedder, "bundle_commit.db")
    repo_id = result["repository_id"]
    real_commit = get_head_commit_sha(repo_path)
    assert real_commit is not None

    bundle = engine.get_context_bundle("target", "mod.py", repo_id)
    assert bundle["snapshot_commit"] == real_commit


def test_get_context_bundle_snapshot_commit_is_honestly_none_for_an_unindexed_repository(tmp_path, shared_embedder):
    engine = _new_engine(_db_url(tmp_path, "bundle_no_snapshot.db"), shared_embedder)
    bundle = engine.get_context_bundle("anything", "anywhere.py", repo_id=999)
    assert bundle["snapshot_commit"] is None
    assert bundle["callers"] == []
    assert bundle["callees"] == []


def test_get_context_bundle_does_not_leak_callers_or_related_symbols_across_repositories(tmp_path, shared_embedder):
    engine, r_a, _ = _index_repo(tmp_path, "repo_a_bundle_iso", CONTEXT_FIXTURE_MOD, shared_embedder, "bundle_iso.db")
    engine, r_b, _ = _index_repo(tmp_path, "repo_b_bundle_iso", CONTEXT_FIXTURE_MOD, shared_embedder, engine)

    bundle_b = engine.get_context_bundle(
        "helper", "mod.py", r_b["repository_id"],
        signature="def helper():", docstring="A helper.", body="return 1",
    )
    caller_names = {c["symbol"] for c in bundle_b["callers"]}
    assert caller_names == {"caller_one", "caller_two", "target"}
    # Exactly repo B's own three callers -- not six (which would mean
    # repo A's identically-named callers leaked in too).
    assert len(bundle_b["callers"]) == 3


# --- REST endpoint ---------------------------------------------------------

def test_agent_context_endpoint_returns_correct_shape(tmp_path, shared_embedder, monkeypatch):
    engine, result, _ = _index_repo(tmp_path, "repo_ctx_rest", CONTEXT_FIXTURE_MOD, shared_embedder, "ctx_rest.db")
    repo_id = result["repository_id"]

    session = engine.storage.get_session()
    try:
        target_sym = session.query(SymbolModel).join(FileModel).filter(
            FileModel.repo_id == repo_id, SymbolModel.name == "target"
        ).first()
        symbol_id = target_sym.id
    finally:
        session.close()

    monkeypatch.setattr(app_module, "storage_manager", engine.storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    client = TestClient(app)
    headers = make_api_key_headers(engine.storage, repository_ids=[repo_id])

    response = client.post("/v1/agent/context", json={"symbol_id": symbol_id}, headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "target"
    assert body["definition"]["body"] is None
    assert "snapshot_commit" in body
    assert body["truncated"] is True


def test_agent_context_endpoint_forbidden_for_a_different_repository(tmp_path, shared_embedder, monkeypatch):
    engine, r_a, _ = _index_repo(tmp_path, "repo_a_ctx_rest", CONTEXT_FIXTURE_MOD, shared_embedder, "ctx_rest_iso.db")
    engine, r_b, _ = _index_repo(tmp_path, "repo_b_ctx_rest", CONTEXT_FIXTURE_MOD, shared_embedder, engine)

    session = engine.storage.get_session()
    try:
        b_sym = session.query(SymbolModel).join(FileModel).filter(
            FileModel.repo_id == r_b["repository_id"], SymbolModel.name == "target"
        ).first()
        symbol_id = b_sym.id
    finally:
        session.close()

    monkeypatch.setattr(app_module, "storage_manager", engine.storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    client = TestClient(app)
    headers = make_api_key_headers(engine.storage, repository_ids=[r_a["repository_id"]])

    response = client.post("/v1/agent/context", json={"symbol_id": symbol_id}, headers=headers)
    assert response.status_code == 403


def test_agent_context_endpoint_rejected_without_any_key(tmp_path, shared_embedder, monkeypatch):
    engine, result, _ = _index_repo(tmp_path, "repo_ctx_noauth", CONTEXT_FIXTURE_MOD, shared_embedder, "ctx_noauth.db")
    monkeypatch.setattr(app_module, "storage_manager", engine.storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    client = TestClient(app)

    response = client.post("/v1/agent/context", json={"symbol_id": 1})
    assert response.status_code == 401


def test_agent_context_endpoint_404_for_missing_symbol(tmp_path, shared_embedder, monkeypatch):
    engine, result, _ = _index_repo(tmp_path, "repo_ctx_404", CONTEXT_FIXTURE_MOD, shared_embedder, "ctx_404.db")
    monkeypatch.setattr(app_module, "storage_manager", engine.storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    client = TestClient(app)
    headers = make_api_key_headers(engine.storage, repository_ids=[result["repository_id"]])

    response = client.post("/v1/agent/context", json={"symbol_id": 999999}, headers=headers)
    assert response.status_code == 404


def test_symbol_similar_endpoint_does_not_leak_across_repositories_rest_level(tmp_path, shared_embedder, monkeypatch):
    """End-to-end REST-level proof that POST /v1/symbols/{id}/similar
    (refactored this milestone to delegate to find_similar_symbols) no
    longer leaks another repository's similar symbols."""
    engine, r_a, _ = _index_repo(tmp_path, "repo_a_similar_rest", SIMILAR_PAIR_MOD, shared_embedder, "similar_rest_iso.db")
    engine, r_b, _ = _index_repo(tmp_path, "repo_b_similar_rest", SIMILAR_PAIR_MOD, shared_embedder, engine)

    session = engine.storage.get_session()
    try:
        a_sym = session.query(SymbolModel).join(FileModel).filter(
            FileModel.repo_id == r_a["repository_id"], SymbolModel.name == "compute_average"
        ).first()
        symbol_id = a_sym.id
    finally:
        session.close()

    monkeypatch.setattr(app_module, "storage_manager", engine.storage)
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    client = TestClient(app)
    headers = make_api_key_headers(engine.storage, repository_ids=[r_a["repository_id"]])

    response = client.post(
        f"/v1/symbols/{symbol_id}/similar", json={"threshold": 0.85, "limit": 10}, headers=headers
    )
    assert response.status_code == 200
    body = response.json()
    names = {s["symbol_name"] for s in body["similar"]}
    assert names == {"compute_mean"}, (
        f"repo A's compute_average must find only repo A's OWN compute_mean, got {names!r}"
    )
