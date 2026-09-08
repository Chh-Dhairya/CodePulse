from __future__ import annotations

import os
import time
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from codepulse.api.security import AuthorizedKey, require_api_key
from codepulse.config import get_settings
from codepulse.indexer.engine import IndexingEngine
from codepulse.storage.db import (
    DependencyModel,
    FileModel,
    RepositoryModel,
    StorageManager,
    SymbolModel,
)

settings = get_settings()

app = FastAPI(
    title="CodePulse API",
    description="Production-grade Semantic Codebase Intelligence Engine",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global in-memory engine and storage instances
storage_manager = StorageManager()
indexing_engine = IndexingEngine(storage=storage_manager)

# Request / Response Schemas
class SearchRequest(BaseModel):
    repository_id: Optional[int] = 1
    query: str
    limit: int = Field(default=20, ge=1, le=100)
    search_type: str = Field(default="hybrid", pattern="^(hybrid|keyword|semantic)$")


class IndexRepoRequest(BaseModel):
    repository_id: Optional[int] = None
    source: str = "local"  # "local", "github", "gitlab"
    url: str  # local path or git URL
    branch: str = "main"
    index_dependencies: bool = True


class SimilarCodeRequest(BaseModel):
    limit: int = 10
    threshold: float = 0.75


class ImpactAnalysisRequest(BaseModel):
    depth: int = 3


class IdeSearchRequest(BaseModel):
    repository_id: Optional[int] = 1
    selected_text: str
    file_context: Optional[str] = None
    limit: int = 5


class FeedbackRequest(BaseModel):
    search_id: str
    result_id: int
    feedback: str  # "relevant", "irrelevant", "perfect"


# -------------------------------------------------------------
# 1. Search Endpoint (POST /v1/search)
# -------------------------------------------------------------
@app.post("/v1/search")
def search(req: SearchRequest, auth: AuthorizedKey = Depends(require_api_key)):
    # Phase 1.2: repository_id may legitimately be omitted (None) to mean
    # "search every repository" (an existing Phase 0.6 IndexingEngine
    # feature) -- but that can never be authorized against a specific
    # key's repository grants, so it's rejected here. See
    # AuthorizedKey.authorize_search_scope's docstring.
    auth.authorize_search_scope(req.repository_id)
    t0 = time.time()
    results = indexing_engine.search(
        req.query,
        limit=req.limit,
        search_type=req.search_type,
        repository_id=req.repository_id,
    )
    exec_time_ms = int((time.time() - t0) * 1000)

    return {
        "results": [r.to_dict() for r in results],
        "execution_time_ms": exec_time_ms,
        "query": req.query,
        "search_type": req.search_type,
    }


# -------------------------------------------------------------
# 2. Index Repository (POST /v1/repositories/index)
# -------------------------------------------------------------
@app.post("/v1/repositories/index", status_code=202)
def index_repository(
    req: IndexRepoRequest, bg_tasks: BackgroundTasks, auth: AuthorizedKey = Depends(require_api_key)
):
    # Phase 1.2: this endpoint creates/re-indexes a repository -- there is
    # no *existing* repository scope to check a not-yet-indexed path
    # against (the roadmap's repository-scoping model authorizes access to
    # already-known repositories; ingestion-path safety itself -- allow-
    # listing, SSRF checks, size caps -- is explicitly Phase 1.5 / B10, not
    # this milestone). So only authentication (a valid, non-revoked key) is
    # required here; no `auth.authorize_repository(...)` call.
    job_id = f"job_{int(time.time())}"

    def run_indexing():
        indexing_engine.index_directory(
            repo_path=req.url,
            repo_name=os.path.basename(req.url),
            repo_url=req.url,
        )

    bg_tasks.add_task(run_indexing)

    # Phase 0.7: no `estimated_time_seconds` here -- indexing duration is
    # not measured or predicted anywhere in the system (no timing model,
    # no historical job data), so a fixed guess would be a fabricated
    # metric. Real job progress/duration tracking is Phase 1 (IndexJob).
    return {
        "job_id": job_id,
        "status": "queued",
        "url": req.url,
    }


# -------------------------------------------------------------
# 3. Get Index Status (GET /v1/repositories/{id}/index-status)
# -------------------------------------------------------------
@app.get("/v1/repositories/{repo_id}/index-status")
def get_index_status(repo_id: int, auth: AuthorizedKey = Depends(require_api_key)):
    auth.authorize_repository(repo_id)
    session = storage_manager.get_session()
    try:
        repo = session.query(RepositoryModel).filter(RepositoryModel.id == repo_id).first()
        # Phase 0.7: no `index_size_mb` in either branch below -- no on-disk
        # or in-memory index-footprint measurement exists anywhere in the
        # system, so a fixed number would be a fabricated metric with no
        # authoritative source. `embedding_model` is kept: it's a
        # configured value (the actual model name in use), not a metric.
        if not repo:
            # Fallback stats from current memory engine
            return {
                "repository_id": repo_id,
                "indexed_files": len(indexing_engine.parse_results),
                "total_files": len(indexing_engine.parse_results),
                "indexed_symbols": len(indexing_engine.symbols_by_id),
                "last_indexed": "live",
                "embedding_model": indexing_engine.embedder.model_name,
            }

        return {
            "repository_id": repo.id,
            "indexed_files": repo.file_count,
            "total_files": repo.file_count,
            "indexed_symbols": repo.symbol_count,
            "last_indexed": repo.last_sync.isoformat() if repo.last_sync else "",
            "embedding_model": indexing_engine.embedder.model_name,
        }
    finally:
        session.close()


# -------------------------------------------------------------
# 4. Get Symbol Details (GET /v1/symbols/{id})
# -------------------------------------------------------------
@app.get("/v1/symbols/{symbol_id}")
def get_symbol_details(symbol_id: int, auth: AuthorizedKey = Depends(require_api_key)):
    session = storage_manager.get_session()
    try:
        sym = session.query(SymbolModel).filter(SymbolModel.id == symbol_id).first()
        if not sym:
            raise HTTPException(status_code=404, detail="Symbol not found")
        # Phase 1.2: a symbol is not requested by repository_id directly,
        # but every symbol belongs to exactly one repository (via its
        # file's non-nullable repo_id FK) -- resolve and authorize it
        # before returning any content, per §15/§30's "every query (search,
        # symbol lookup, graph traversal) is filtered by repository_id and
        # the caller's permitted repository set."
        if not sym.file:
            raise HTTPException(status_code=404, detail="Symbol not found")
        auth.authorize_repository(sym.file.repo_id)

        # Query call graph relations
        called_by = (
            session.query(DependencyModel)
            .filter(DependencyModel.target_symbol_name == sym.name)
            .all()
        )
        calls = (
            session.query(DependencyModel)
            .filter(DependencyModel.source_symbol_name == sym.name)
            .all()
        )

        return {
            "symbol_id": sym.id,
            "name": sym.name,
            "type": sym.type,
            "file": sym.file.path if sym.file else "",
            "line_start": sym.start_line,
            "line_end": sym.end_line,
            "docstring": sym.docstring,
            "source_code": sym.body,
            "called_by": [{"symbol": d.source_symbol_name, "file": d.source_file} for d in called_by],
            "calls": [{"symbol": d.target_symbol_name, "file": d.target_file} for d in calls],
            "complexity": sym.complexity,
        }
    finally:
        session.close()


# -------------------------------------------------------------
# 5. Find Similar Code (POST /v1/symbols/{id}/similar)
# -------------------------------------------------------------
@app.post("/v1/symbols/{symbol_id}/similar")
def find_similar_code(
    symbol_id: int, req: SimilarCodeRequest, auth: AuthorizedKey = Depends(require_api_key)
):
    session = storage_manager.get_session()
    try:
        sym = session.query(SymbolModel).filter(SymbolModel.id == symbol_id).first()
        if not sym:
            raise HTTPException(status_code=404, detail="Symbol not found")
        if not sym.file:
            raise HTTPException(status_code=404, detail="Symbol not found")
        auth.authorize_repository(sym.file.repo_id)

        query_text = f"{sym.signature}\n{sym.docstring or ''}\n{sym.body[:500]}"
        query_vec = indexing_engine.embedder.encode(query_text)
        vector_res = indexing_engine.vector_index.search(query_vec, top_k=req.limit + 1)

        similar_items = []
        for r in vector_res:
            meta = r.get("metadata", {})
            if meta.get("symbol_name") == sym.name:
                continue  # skip self
            if r["relevance_score"] >= req.threshold:
                similar_items.append({
                    "symbol_name": meta.get("symbol_name"),
                    "file_path": meta.get("file_path"),
                    "similarity": round(r["similarity"], 4),
                    "relevance_score": round(r["relevance_score"], 4),
                })

        return {
            "query_symbol": {"id": sym.id, "name": sym.name},
            "similar": similar_items[: req.limit],
        }
    finally:
        session.close()


# -------------------------------------------------------------
# 6. Analyze Impact (POST /v1/symbols/{id}/impact-analysis)
# -------------------------------------------------------------
@app.post("/v1/symbols/{symbol_id}/impact-analysis")
def analyze_impact(
    symbol_id: int, req: ImpactAnalysisRequest, auth: AuthorizedKey = Depends(require_api_key)
):
    session = storage_manager.get_session()
    try:
        sym = session.query(SymbolModel).filter(SymbolModel.id == symbol_id).first()
        if not sym:
            raise HTTPException(status_code=404, detail="Symbol not found")
        if not sym.file:
            raise HTTPException(status_code=404, detail="Symbol not found")
        auth.authorize_repository(sym.file.repo_id)

        if indexing_engine.call_graph:
            callers = indexing_engine.call_graph.get_callers(sym.name, sym.file.path if sym.file else "", max_depth=req.depth)
            callees = indexing_engine.call_graph.get_callees(sym.name, sym.file.path if sym.file else "", max_depth=req.depth)
        else:
            callers, callees = [], []

        return {
            "symbol": sym.name,
            "direct_callers": len([c for c in callers if c.get("depth") == 1]),
            "indirect_callers": len([c for c in callers if c.get("depth", 0) > 1]),
            "call_chain": callers,
            "dependencies": callees,
        }
    finally:
        session.close()


# -------------------------------------------------------------
# 7. Autocomplete (GET /v1/autocomplete)
# -------------------------------------------------------------
@app.get("/v1/autocomplete")
def autocomplete(
    prefix: str = Query(..., min_length=1),
    limit: int = 10,
    auth: AuthorizedKey = Depends(require_api_key),
):
    # Phase 1.2: authentication only. This endpoint has no repository_id
    # parameter at all -- it already searches the global in-memory engine
    # state unscoped by repository (a pre-existing limitation documented
    # since Phase 0.7, not something this milestone's literal scope -- "a
    # FastAPI dependency injected into every route" -- extends to fixing;
    # doing so would mean redesigning this endpoint's signature, which is
    # 1.3+-shaped scope creep, not 1.2).
    prefix_lower = prefix.lower()
    matches = []

    for doc_id, sym in indexing_engine.symbols_by_id.items():
        if sym.name.lower().startswith(prefix_lower):
            matches.append({
                "name": sym.name,
                "type": sym.type.value if hasattr(sym.type, "value") else str(sym.type),
                "file": sym.file_path,
                "line": sym.start_line,
                "signature": sym.signature,
            })
            if len(matches) >= limit:
                break

    return {"suggestions": matches}


# -------------------------------------------------------------
# 8. Search within Function (POST /v1/symbols/{id}/search)
# -------------------------------------------------------------
@app.post("/v1/symbols/{symbol_id}/search")
def search_within_symbol(
    symbol_id: int,
    query: str = Query(...),
    search_in: str = "all",
    auth: AuthorizedKey = Depends(require_api_key),
):
    session = storage_manager.get_session()
    try:
        sym = session.query(SymbolModel).filter(SymbolModel.id == symbol_id).first()
        if not sym:
            raise HTTPException(status_code=404, detail="Symbol not found")
        if not sym.file:
            raise HTTPException(status_code=404, detail="Symbol not found")
        auth.authorize_repository(sym.file.repo_id)

        target_text = ""
        if search_in == "docstring":
            target_text = sym.docstring or ""
        elif search_in == "source":
            target_text = sym.body or ""
        else:
            target_text = f"{sym.signature}\n{sym.docstring or ''}\n{sym.body or ''}"

        found = query.lower() in target_text.lower()
        matches = []
        if found:
            for idx, line in enumerate(target_text.splitlines(), start=sym.start_line):
                if query.lower() in line.lower():
                    matches.append({"line": idx, "snippet": line.strip()})

        return {"found": found, "matches": matches}
    finally:
        session.close()


# -------------------------------------------------------------
# 9. List Symbols in File (GET /v1/files/{file_id}/symbols)
# -------------------------------------------------------------
@app.get("/v1/files/{file_id}/symbols")
def list_file_symbols(file_id: int, auth: AuthorizedKey = Depends(require_api_key)):
    session = storage_manager.get_session()
    try:
        f = session.query(FileModel).filter(FileModel.id == file_id).first()
        if not f:
            raise HTTPException(status_code=404, detail="File not found")
        auth.authorize_repository(f.repo_id)

        return {
            "file_id": f.id,
            "file_path": f.path,
            "symbols": [
                {
                    "symbol_id": s.id,
                    "name": s.name,
                    "type": s.type,
                    "line_start": s.start_line,
                    "line_end": s.end_line,
                    "complexity": s.complexity,
                }
                for s in f.symbols
            ],
        }
    finally:
        session.close()


# -------------------------------------------------------------
# 10. Get Repository Stats (GET /v1/repositories/{id}/stats)
# -------------------------------------------------------------
@app.get("/v1/repositories/{repo_id}/stats")
def get_repository_stats(repo_id: int, auth: AuthorizedKey = Depends(require_api_key)):
    auth.authorize_repository(repo_id)
    session = storage_manager.get_session()
    try:
        repo = session.query(RepositoryModel).filter(RepositoryModel.id == repo_id).first()
        file_count = repo.file_count if repo else len(indexing_engine.parse_results)
        symbol_count = repo.symbol_count if repo else len(indexing_engine.symbols_by_id)

        # Language distribution
        langs: dict[str, dict[str, int]] = {}
        for res in indexing_engine.parse_results.values():
            l = res.language
            if l not in langs:
                langs[l] = {"files": 0, "symbols": 0}
            langs[l]["files"] += 1
            langs[l]["symbols"] += len(res.symbols)

        # Phase 0.7: no `total_queries` / `avg_search_latency_ms` here --
        # no query log or latency-aggregation table exists anywhere in the
        # system (SearchFeedback/query-logging is a later roadmap
        # milestone), so fixed numbers would be fabricated metrics with no
        # authoritative source. Building that tracking infrastructure now
        # would be scope creep beyond removing fabricated values.
        return {
            "repository_id": repo_id,
            "total_files": file_count,
            "total_symbols": symbol_count,
            "languages": langs,
        }
    finally:
        session.close()


# -------------------------------------------------------------
# 11. IDE Plugin Search (POST /v1/ide-search)
# -------------------------------------------------------------
@app.post("/v1/ide-search")
def ide_search(req: IdeSearchRequest, auth: AuthorizedKey = Depends(require_api_key)):
    auth.authorize_search_scope(req.repository_id)
    t0 = time.time()
    results = indexing_engine.search(
        req.selected_text,
        limit=req.limit,
        search_type="hybrid",
        repository_id=req.repository_id,
    )
    exec_time_ms = int((time.time() - t0) * 1000)

    return {
        "results": [
            {
                "symbol_name": r.symbol_name,
                "type": r.symbol_type,
                "file": r.file_path,
                "line": r.start_line,
                "relevance": r.final_score,
                "signature": r.signature,
            }
            for r in results
        ],
        "execution_time_ms": exec_time_ms,
    }


# -------------------------------------------------------------
# 12. Feedback (POST /v1/search/feedback)
# -------------------------------------------------------------
@app.post("/v1/search/feedback")
def submit_feedback(req: FeedbackRequest, auth: AuthorizedKey = Depends(require_api_key)):
    # Phase 1.2: authentication only -- this endpoint has no repository_id
    # or resolvable-to-a-repository field (search_id/result_id are opaque),
    # so no repository-scope check is possible or required here.
    # Phase 0.7: this endpoint does not persist feedback anywhere (no
    # SearchFeedback table/row is written) and does not adjust ranking
    # weights. The previous response claimed both ("status": "recorded",
    # "...Weights updated.") -- a fabricated success/effect claim. Real
    # persistence is a later roadmap milestone (B28); until then, the
    # response must not claim an effect that didn't happen.
    return {
        "status": "received",
        "search_id": req.search_id,
        "result_id": req.result_id,
        "message": "Feedback received. Not yet persisted or used to adjust ranking.",
    }
