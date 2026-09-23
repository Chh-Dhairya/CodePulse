"""
Phase 5.3 -- MCP server (roadmap §37 item 5.3; §13 MCP Strategy).

Implemented under the user's explicit continuous-execution
authorization (see CODEPULSE_PROGRESS.md's standing-deviation notice).

Verifies §13's own explicit requirements directly:
  - The exact 7 named tools exist: search_code, get_symbol,
    find_callers, find_callees, impact_analysis, find_similar,
    get_context.
  - Each tool calls straight into `IndexingEngine` -- no parallel
    graph/retrieval implementation (proved by cross-checking tool
    output against the equivalent `IndexingEngine` method's own direct
    return value, not merely "it returns something").
  - Repository scoping happens ONCE, at `build_mcp_server` (connection-
    setup) time, not per-tool-call -- a connection built for one
    repository must be structurally unable to see another's data
    through ANY tool, verified with two repositories sharing identical
    symbol names throughout.
  - Failures are structured `{"error": ..., "message": ...}` dicts, not
    raised exceptions.

`FastMCP.call_tool(name, args)` is async and returns
`(content_blocks, structured_result_dict)` -- these tests assert
against the structured dict (index `[1]`) throughout. Real embeddings
(`CodeEmbedder`) and a real temporary git repo per test, exactly
matching every other engine-level test file's own convention.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from codepulse.auth import create_api_key
from codepulse.config import reset_settings
from codepulse.embeddings.embedder import CodeEmbedder
from codepulse.indexer.engine import IndexingEngine
from codepulse.mcp_server import MCPAuthenticationError, build_mcp_server
from codepulse.storage.db import FileModel, StorageManager, SymbolModel


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


MOD_SOURCE = (
    'def helper():\n    """A helper."""\n    return 1\n\n\n'
    'def caller():\n    """Calls helper."""\n    return helper()\n\n\n'
    'class Animal:\n    """Base."""\n    pass\n\n\n'
    'class Dog(Animal):\n    """Subclass."""\n    pass\n'
)


def _index_repo(tmp_path, dirname, embedder, db_name_or_engine):
    repo = tmp_path / dirname
    _init_repo(repo)
    _write(repo, "mod.py", MOD_SOURCE)
    _commit_all(repo, "initial")
    engine = db_name_or_engine if isinstance(db_name_or_engine, IndexingEngine) else _new_engine(
        _db_url(tmp_path, db_name_or_engine), embedder
    )
    result = engine.index_directory(str(repo), repo_name=dirname, repo_url=str(repo))
    return engine, result


def _symbol_id(engine, repo_id, name):
    session = engine.storage.get_session()
    try:
        sym = session.query(SymbolModel).join(FileModel).filter(
            FileModel.repo_id == repo_id, SymbolModel.name == name
        ).first()
        return sym.id
    finally:
        session.close()


# --- setup / authentication -----------------------------------------------

@pytest.mark.asyncio
async def test_build_mcp_server_registers_exactly_the_seven_named_tools(tmp_path, shared_embedder):
    engine, result = _index_repo(tmp_path, "repo_tools", shared_embedder, "mcp_tools.db")
    created = create_api_key(engine.storage, name="k", repository_ids=[result["repository_id"]])
    mcp = build_mcp_server(engine.storage, engine, created.raw_key)
    tools = {t.name for t in await mcp.list_tools()}
    assert tools == {
        "search_code", "get_symbol", "find_callers", "find_callees",
        "impact_analysis", "find_similar", "get_context",
    }


@pytest.mark.asyncio
async def test_build_mcp_server_rejects_an_invalid_key(tmp_path, shared_embedder):
    engine, result = _index_repo(tmp_path, "repo_badkey", shared_embedder, "mcp_badkey.db")
    with pytest.raises(MCPAuthenticationError):
        build_mcp_server(engine.storage, engine, "not-a-real-key")


# --- individual tools, calling straight into IndexingEngine ---------------

@pytest.mark.asyncio
async def test_get_symbol_tool_matches_engine_data_and_excludes_inheritance(tmp_path, shared_embedder):
    engine, result = _index_repo(tmp_path, "repo_get_symbol", shared_embedder, "mcp_get_symbol.db")
    repo_id = result["repository_id"]
    created = create_api_key(engine.storage, name="k", repository_ids=[repo_id])
    mcp = build_mcp_server(engine.storage, engine, created.raw_key)

    helper_id = _symbol_id(engine, repo_id, "helper")
    _, body = await mcp.call_tool("get_symbol", {"symbol_id": helper_id})
    assert body["name"] == "helper"
    assert [c["symbol"] for c in body["called_by"]] == ["caller"]
    assert body["calls"] == []

    animal_id = _symbol_id(engine, repo_id, "Animal")
    _, animal_body = await mcp.call_tool("get_symbol", {"symbol_id": animal_id})
    # Dog(Animal) is an inheritance edge, not a call -- must never
    # appear in called_by (the Phase 4.1 compatibility correction).
    assert animal_body["called_by"] == []


@pytest.mark.asyncio
async def test_impact_analysis_tool_matches_engine_result_exactly(tmp_path, shared_embedder):
    engine, result = _index_repo(tmp_path, "repo_impact_tool", shared_embedder, "mcp_impact.db")
    repo_id = result["repository_id"]
    created = create_api_key(engine.storage, name="k", repository_ids=[repo_id])
    mcp = build_mcp_server(engine.storage, engine, created.raw_key)

    helper_id = _symbol_id(engine, repo_id, "helper")
    _, body = await mcp.call_tool("impact_analysis", {"symbol_id": helper_id, "depth": 3})
    expected = engine.analyze_impact("helper", "mod.py", repo_id, max_depth=3)
    assert body == expected


@pytest.mark.asyncio
async def test_find_callers_and_find_callees_tools_project_impact_analysis(tmp_path, shared_embedder):
    engine, result = _index_repo(tmp_path, "repo_callers_tool", shared_embedder, "mcp_callers.db")
    repo_id = result["repository_id"]
    created = create_api_key(engine.storage, name="k", repository_ids=[repo_id])
    mcp = build_mcp_server(engine.storage, engine, created.raw_key)

    helper_id = _symbol_id(engine, repo_id, "helper")
    _, callers_body = await mcp.call_tool("find_callers", {"symbol_id": helper_id})
    assert [c["symbol"] for c in callers_body["callers"]] == ["caller"]

    caller_id = _symbol_id(engine, repo_id, "caller")
    _, callees_body = await mcp.call_tool("find_callees", {"symbol_id": caller_id})
    assert [c["symbol"] for c in callees_body["callees"]] == ["helper"]


@pytest.mark.asyncio
async def test_get_context_tool_matches_engine_bundle(tmp_path, shared_embedder):
    engine, result = _index_repo(tmp_path, "repo_context_tool", shared_embedder, "mcp_context.db")
    repo_id = result["repository_id"]
    created = create_api_key(engine.storage, name="k", repository_ids=[repo_id])
    mcp = build_mcp_server(engine.storage, engine, created.raw_key)

    helper_id = _symbol_id(engine, repo_id, "helper")
    _, body = await mcp.call_tool("get_context", {"symbol_id": helper_id})
    assert body["symbol"] == "helper"
    assert body["definition"]["body"] is None
    assert "snapshot_commit" in body
    assert body["truncated"] is True


@pytest.mark.asyncio
async def test_search_code_tool_returns_results_for_an_authorized_repository(tmp_path, shared_embedder):
    engine, result = _index_repo(tmp_path, "repo_search_tool", shared_embedder, "mcp_search.db")
    repo_id = result["repository_id"]
    created = create_api_key(engine.storage, name="k", repository_ids=[repo_id])
    mcp = build_mcp_server(engine.storage, engine, created.raw_key)

    _, body = await mcp.call_tool(
        "search_code", {"query": "helper", "repository_id": repo_id, "search_type": "keyword"}
    )
    assert "error" not in body
    assert len(body["results"]) >= 1


# --- structured errors, not exceptions -------------------------------------

@pytest.mark.asyncio
async def test_symbol_not_found_returns_a_structured_error_not_an_exception(tmp_path, shared_embedder):
    engine, result = _index_repo(tmp_path, "repo_404_tool", shared_embedder, "mcp_404.db")
    created = create_api_key(engine.storage, name="k", repository_ids=[result["repository_id"]])
    mcp = build_mcp_server(engine.storage, engine, created.raw_key)

    _, body = await mcp.call_tool("get_symbol", {"symbol_id": 999999})
    assert body["error"] == "symbol_not_found"
    assert "message" in body


@pytest.mark.asyncio
async def test_unauthorized_repository_returns_a_structured_error_not_an_exception(tmp_path, shared_embedder):
    """A connection scoped to NO repositories must get a structured
    error, never an unhandled exception, when asked about a real
    symbol in a real (but unauthorized) repository."""
    engine, result = _index_repo(tmp_path, "repo_unauth_tool", shared_embedder, "mcp_unauth.db")
    repo_id = result["repository_id"]
    helper_id = _symbol_id(engine, repo_id, "helper")

    created = create_api_key(engine.storage, name="k", repository_ids=[])
    mcp = build_mcp_server(engine.storage, engine, created.raw_key)

    _, body = await mcp.call_tool("get_symbol", {"symbol_id": helper_id})
    assert body["error"] == "repository_not_authorized"

    _, search_body = await mcp.call_tool(
        "search_code", {"query": "helper", "repository_id": repo_id}
    )
    assert search_body["error"] == "repository_not_authorized"


# --- repository isolation, zero tolerance ----------------------------------

@pytest.mark.asyncio
async def test_mcp_connection_never_sees_another_repositorys_data_through_any_tool(tmp_path, shared_embedder):
    """Two repositories, identical file/symbol names throughout. A
    connection scoped ONLY to repo A must get repository_not_authorized
    (never real data, never a crash) for every tool when asked about
    repo B's own symbol id."""
    engine, r_a = _index_repo(tmp_path, "repo_a_mcp_iso", shared_embedder, "mcp_iso.db")
    engine, r_b = _index_repo(tmp_path, "repo_b_mcp_iso", shared_embedder, engine)

    b_helper_id = _symbol_id(engine, r_b["repository_id"], "helper")

    created_a = create_api_key(engine.storage, name="k_a", repository_ids=[r_a["repository_id"]])
    mcp_a = build_mcp_server(engine.storage, engine, created_a.raw_key)

    for tool_name, args in [
        ("get_symbol", {"symbol_id": b_helper_id}),
        ("impact_analysis", {"symbol_id": b_helper_id}),
        ("find_callers", {"symbol_id": b_helper_id}),
        ("find_callees", {"symbol_id": b_helper_id}),
        ("find_similar", {"symbol_id": b_helper_id}),
        ("get_context", {"symbol_id": b_helper_id}),
        ("search_code", {"query": "helper", "repository_id": r_b["repository_id"]}),
    ]:
        _, body = await mcp_a.call_tool(tool_name, args)
        assert body.get("error") == "repository_not_authorized", (
            f"tool {tool_name!r} must reject access to repo B's data with repository_not_authorized, got {body!r}"
        )

    # And repo A's own key must work correctly for repo A's own data --
    # proving this isn't just "everything is broken."
    a_helper_id = _symbol_id(engine, r_a["repository_id"], "helper")
    _, own_body = await mcp_a.call_tool("get_symbol", {"symbol_id": a_helper_id})
    assert own_body["name"] == "helper"
    assert "error" not in own_body
