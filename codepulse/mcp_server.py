"""
Phase 5.3 -- MCP server (roadmap §37 item 5.3; §13 MCP Strategy).

§13, quoted and held to strictly: *"Recommendation: yes, expose an MCP
server -- but strictly as an adapter, not a parallel implementation.
Each MCP tool calls the exact same internal Python functions the REST
API calls."* *"Proposed tools: search_code, get_symbol, find_callers,
find_callees, impact_analysis, find_similar, get_context (the §12
bundle)."* *"Permissions/repository selection: MCP connections are
scoped to one or more repositories at connection-setup time (via the
same API-key/permission model as REST -- §16), not per-tool-call."*
*"Failure behavior: tool calls return structured errors
({"error": "repository_not_indexed", "message": "..."}) rather than
throwing opaque exceptions."*

Architecture: this module is a THIN adapter over `IndexingEngine` and
`codepulse.auth`/`codepulse.api.security`'s existing authorization
primitives -- it introduces no new graph/retrieval/authorization logic
of its own. `build_mcp_server(storage, engine, api_key)` resolves the
given API key to an `AuthorizedKey` exactly ONCE, via the same
`verify_api_key` (Phase 1.1) function REST's own `require_api_key`
dependency calls -- not a second authentication mechanism -- and every
tool call authorizes against that SAME resolved `repository_ids` set,
never re-authenticating per call (per §13's own explicit "not
per-tool-call" instruction; FastAPI's per-request `Depends(
require_api_key)` mechanism does not apply here since MCP tool calls
are not individual HTTP requests).

Each tool below calls the exact same `IndexingEngine` method its REST
counterpart calls (`search`, `analyze_impact`, `find_similar_symbols`,
`get_context_bundle`) -- `find_callers`/`find_callees` both reuse
`analyze_impact` internally (there is no dedicated standalone
REST/engine method for either; §13 names them as separate TOOLS, not
as evidence of a separate implementation, so they are built as thin
projections of the one already-tested `analyze_impact` result rather
than a second call-graph traversal). `get_symbol`'s `called_by`/`calls`
query mirrors `GET /v1/symbols/{id}`'s own tested query exactly
(including the Phase 4.1 compatibility correction's `relationship_type
== "calls"` filter) -- a small, deliberate, disclosed duplication of a
plain parameterized DB read (not a graph algorithm), not extracted into
a third shared location this milestone; see CODEPULSE_PROGRESS.md's
Phase 5.3 entry for the explicit reasoning.
"""
from __future__ import annotations

from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from codepulse.auth import verify_api_key
from codepulse.api.security import AuthorizedKey
from codepulse.indexer.engine import IndexingEngine
from codepulse.storage.db import APIKeyRepositoryModel, CallEdge, StorageManager, SymbolModel


class MCPAuthenticationError(Exception):
    """Raised by `build_mcp_server` when the supplied API key is missing, unknown, or revoked."""


def _structured_error(code: str, message: str) -> dict[str, str]:
    """§13's own literal failure-behavior contract: `{"error": "...", "message": "..."}`."""
    return {"error": code, "message": message}


def build_mcp_server(
    storage: StorageManager, engine: IndexingEngine, api_key: str, name: str = "codepulse"
) -> FastMCP:
    """
    Builds and returns a `FastMCP` server instance scoped to the
    repositories the given `api_key` is authorized for -- resolved
    ONCE, at connection-setup time, per §13's explicit instruction.
    Raises `MCPAuthenticationError` for a missing/unknown/revoked key,
    mirroring `require_api_key`'s own HTTP 401 case (there is no HTTP
    response here to return, so this is a plain exception the caller
    -- e.g. `codepulse serve`, Phase 6 -- decides how to report).
    """
    api_key_id = verify_api_key(storage, api_key)
    if api_key_id is None:
        raise MCPAuthenticationError("Invalid or revoked API key")

    session = storage.get_session()
    try:
        repo_ids = {
            row.repo_id
            for row in session.query(APIKeyRepositoryModel)
            .filter(APIKeyRepositoryModel.api_key_id == api_key_id)
            .all()
        }
    finally:
        session.close()

    authorized = AuthorizedKey(api_key_id=api_key_id, repository_ids=frozenset(repo_ids))

    mcp: FastMCP = FastMCP(name)

    def _authorize(repo_id: int) -> Optional[dict[str, str]]:
        if repo_id not in authorized.repository_ids:
            return _structured_error(
                "repository_not_authorized",
                f"This connection is not authorized for repository {repo_id}.",
            )
        return None

    def _load_symbol(symbol_id: int) -> Optional[dict[str, Any]]:
        """Extracts every field a tool might need into a plain dict
        WHILE the session is open (SQLAlchemy relationship access after
        `session.close()` would raise `DetachedInstanceError`), exactly
        matching the "access `sym.file` before closing" pattern every
        REST symbol route already follows."""
        db_session = storage.get_session()
        try:
            sym = db_session.query(SymbolModel).filter(SymbolModel.id == symbol_id).first()
            if not sym or not sym.file:
                return None
            return {
                "id": sym.id,
                "name": sym.name,
                "type": sym.type,
                "file_path": sym.file.path,
                "repo_id": sym.file.repo_id,
                "signature": sym.signature,
                "docstring": sym.docstring,
                "body": sym.body,
                "complexity": sym.complexity,
            }
        finally:
            db_session.close()

    @mcp.tool()
    def search_code(
        query: str, repository_id: int, limit: int = 20, search_type: str = "hybrid"
    ) -> dict[str, Any]:
        """Hybrid/keyword/semantic/RRF search over an indexed repository's code, scoped to this connection's authorized repositories."""
        auth_error = _authorize(repository_id)
        if auth_error:
            return auth_error
        results = engine.search(query, limit=limit, search_type=search_type, repository_id=repository_id)
        return {"results": [r.to_dict() for r in results]}

    @mcp.tool()
    def get_symbol(symbol_id: int) -> dict[str, Any]:
        """Fetches one symbol's definition plus its direct callers/callees (calls-only, matching GET /v1/symbols/{id})."""
        loaded = _load_symbol(symbol_id)
        if loaded is None:
            return _structured_error("symbol_not_found", f"No symbol with id {symbol_id}.")
        auth_error = _authorize(loaded["repo_id"])
        if auth_error:
            return auth_error

        db_session = storage.get_session()
        try:
            called_by = (
                db_session.query(CallEdge)
                .filter(CallEdge.target_symbol_name == loaded["name"])
                .filter(CallEdge.repo_id == loaded["repo_id"])
                .filter(CallEdge.relationship_type == "calls")
                .all()
            )
            calls = (
                db_session.query(CallEdge)
                .filter(CallEdge.source_symbol_name == loaded["name"])
                .filter(CallEdge.repo_id == loaded["repo_id"])
                .filter(CallEdge.relationship_type == "calls")
                .all()
            )
        finally:
            db_session.close()

        return {
            "symbol_id": loaded["id"],
            "name": loaded["name"],
            "type": loaded["type"],
            "file": loaded["file_path"],
            "docstring": loaded["docstring"],
            "source_code": loaded["body"],
            "called_by": [
                {"symbol": d.source_symbol_name, "file": d.source_file, "confidence": d.confidence}
                for d in called_by
            ],
            "calls": [
                {"symbol": d.target_symbol_name, "file": d.target_file, "confidence": d.confidence}
                for d in calls
            ],
            "complexity": loaded["complexity"],
        }

    @mcp.tool()
    def find_callers(symbol_id: int, depth: int = 3) -> dict[str, Any]:
        """Symbols that call this one, direct and transitive, confidence-scored and depth-bucketed (a projection of impact_analysis's own callers side)."""
        loaded = _load_symbol(symbol_id)
        if loaded is None:
            return _structured_error("symbol_not_found", f"No symbol with id {symbol_id}.")
        auth_error = _authorize(loaded["repo_id"])
        if auth_error:
            return auth_error
        impact = engine.analyze_impact(loaded["name"], loaded["file_path"], loaded["repo_id"], max_depth=depth)
        return {"symbol": loaded["name"], "callers": impact["call_chain"]}

    @mcp.tool()
    def find_callees(symbol_id: int, depth: int = 3) -> dict[str, Any]:
        """Symbols this one calls, direct and transitive, confidence-scored and depth-bucketed (a projection of impact_analysis's own callees side)."""
        loaded = _load_symbol(symbol_id)
        if loaded is None:
            return _structured_error("symbol_not_found", f"No symbol with id {symbol_id}.")
        auth_error = _authorize(loaded["repo_id"])
        if auth_error:
            return auth_error
        impact = engine.analyze_impact(loaded["name"], loaded["file_path"], loaded["repo_id"], max_depth=depth)
        return {"symbol": loaded["name"], "callees": impact["dependencies"]}

    @mcp.tool()
    def impact_analysis(symbol_id: int, depth: int = 3) -> dict[str, Any]:
        """Direct-vs-transitive impact bucketing for a symbol (calls only; see CODEPULSE_PROGRESS.md's Phase 4.2 Decision 1)."""
        loaded = _load_symbol(symbol_id)
        if loaded is None:
            return _structured_error("symbol_not_found", f"No symbol with id {symbol_id}.")
        auth_error = _authorize(loaded["repo_id"])
        if auth_error:
            return auth_error
        return engine.analyze_impact(loaded["name"], loaded["file_path"], loaded["repo_id"], max_depth=depth)

    @mcp.tool()
    def find_similar(symbol_id: int, threshold: float = 0.75, limit: int = 10) -> dict[str, Any]:
        """Semantically similar symbols within the SAME repository (embedding-based, repository-scoped)."""
        loaded = _load_symbol(symbol_id)
        if loaded is None:
            return _structured_error("symbol_not_found", f"No symbol with id {symbol_id}.")
        auth_error = _authorize(loaded["repo_id"])
        if auth_error:
            return auth_error
        query_text = f"{loaded['signature']}\n{loaded['docstring'] or ''}\n{loaded['body'][:500]}"
        similar = engine.find_similar_symbols(query_text, loaded["name"], loaded["repo_id"], threshold=threshold, limit=limit)
        return {"query_symbol": {"id": loaded["id"], "name": loaded["name"]}, "similar": similar}

    @mcp.tool()
    def get_context(symbol_id: int, include_body: bool = False, max_related: int = 10) -> dict[str, Any]:
        """The §12 agent-native context bundle: definition, direct callers/callees, semantically related symbols, snapshot commit, and a truncation flag."""
        loaded = _load_symbol(symbol_id)
        if loaded is None:
            return _structured_error("symbol_not_found", f"No symbol with id {symbol_id}.")
        auth_error = _authorize(loaded["repo_id"])
        if auth_error:
            return auth_error
        return engine.get_context_bundle(
            loaded["name"], loaded["file_path"], loaded["repo_id"],
            signature=loaded["signature"], docstring=loaded["docstring"], body=loaded["body"],
            include_body=include_body, max_related=max_related,
        )

    return mcp
