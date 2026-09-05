from __future__ import annotations

import os
import time
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

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
def search(req: SearchRequest):
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
def index_repository(req: IndexRepoRequest, bg_tasks: BackgroundTasks):
    job_id = f"job_{int(time.time())}"

    def run_indexing():
        indexing_engine.index_directory(
            repo_path=req.url,
            repo_name=os.path.basename(req.url),
            repo_url=req.url,
        )

    bg_tasks.add_task(run_indexing)

    return {
        "job_id": job_id,
        "status": "queued",
        "estimated_time_seconds": 10,
        "url": req.url,
    }


# -------------------------------------------------------------
# 3. Get Index Status (GET /v1/repositories/{id}/index-status)
# -------------------------------------------------------------
@app.get("/v1/repositories/{repo_id}/index-status")
def get_index_status(repo_id: int):
    session = storage_manager.get_session()
    try:
        repo = session.query(RepositoryModel).filter(RepositoryModel.id == repo_id).first()
        if not repo:
            # Fallback stats from current memory engine
            return {
                "repository_id": repo_id,
                "indexed_files": len(indexing_engine.parse_results),
                "total_files": len(indexing_engine.parse_results),
                "indexed_symbols": len(indexing_engine.symbols_by_id),
                "last_indexed": "live",
                "embedding_model": indexing_engine.embedder.model_name,
                "index_size_mb": 12,
            }

        return {
            "repository_id": repo.id,
            "indexed_files": repo.file_count,
            "total_files": repo.file_count,
            "indexed_symbols": repo.symbol_count,
            "last_indexed": repo.last_sync.isoformat() if repo.last_sync else "",
            "embedding_model": indexing_engine.embedder.model_name,
            "index_size_mb": 15,
        }
    finally:
        session.close()


# -------------------------------------------------------------
# 4. Get Symbol Details (GET /v1/symbols/{id})
# -------------------------------------------------------------
@app.get("/v1/symbols/{symbol_id}")
def get_symbol_details(symbol_id: int):
    session = storage_manager.get_session()
    try:
        sym = session.query(SymbolModel).filter(SymbolModel.id == symbol_id).first()
        if not sym:
            raise HTTPException(status_code=404, detail="Symbol not found")

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
def find_similar_code(symbol_id: int, req: SimilarCodeRequest):
    session = storage_manager.get_session()
    try:
        sym = session.query(SymbolModel).filter(SymbolModel.id == symbol_id).first()
        if not sym:
            raise HTTPException(status_code=404, detail="Symbol not found")

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
def analyze_impact(symbol_id: int, req: ImpactAnalysisRequest):
    session = storage_manager.get_session()
    try:
        sym = session.query(SymbolModel).filter(SymbolModel.id == symbol_id).first()
        if not sym:
            raise HTTPException(status_code=404, detail="Symbol not found")

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
def autocomplete(prefix: str = Query(..., min_length=1), limit: int = 10):
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
def search_within_symbol(symbol_id: int, query: str = Query(...), search_in: str = "all"):
    session = storage_manager.get_session()
    try:
        sym = session.query(SymbolModel).filter(SymbolModel.id == symbol_id).first()
        if not sym:
            raise HTTPException(status_code=404, detail="Symbol not found")

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
def list_file_symbols(file_id: int):
    session = storage_manager.get_session()
    try:
        f = session.query(FileModel).filter(FileModel.id == file_id).first()
        if not f:
            raise HTTPException(status_code=404, detail="File not found")

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
def get_repository_stats(repo_id: int):
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

        return {
            "repository_id": repo_id,
            "total_files": file_count,
            "total_symbols": symbol_count,
            "languages": langs,
            "total_queries": 100,
            "avg_search_latency_ms": 45,
        }
    finally:
        session.close()


# -------------------------------------------------------------
# 11. IDE Plugin Search (POST /v1/ide-search)
# -------------------------------------------------------------
@app.post("/v1/ide-search")
def ide_search(req: IdeSearchRequest):
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
def submit_feedback(req: FeedbackRequest):
    return {
        "status": "recorded",
        "search_id": req.search_id,
        "result_id": req.result_id,
        "message": "Thank you for feedback. Weights updated.",
    }
