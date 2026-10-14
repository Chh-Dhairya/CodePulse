from __future__ import annotations

import logging
import math
import os
import time
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from codepulse.api.errors import _record_api_error, error_json_response, install_error_handlers
from codepulse.api.security import AuthorizedKey, require_api_key
from codepulse.auth import (
    ROLE_RANK,
    create_api_key,
    create_repository_owned_by,
    get_api_key_repository_ids,
    list_api_keys_for_repositories,
    revoke_api_key,
)
from codepulse.config import get_settings
from codepulse.embeddings.embedder import EmbeddingUnavailableError
from codepulse.indexer.engine import IndexingEngine
from codepulse.ingestion_safety import IngestionSafetyError, validate_git_url, validate_local_path
from codepulse.repository_identity import canonical_repository_url, find_repository
from codepulse.jobs import create_job, get_active_job, get_job, mark_failed, mark_running, mark_succeeded
from codepulse.rate_limit import get_limiter
from codepulse.storage.db import (
    CallEdge,
    FileModel,
    RepositoryModel,
    StorageManager,
    SymbolModel,
)

logger = logging.getLogger(__name__)

settings = get_settings()

app = FastAPI(
    title="CodePulse API",
    description="Production-grade Semantic Codebase Intelligence Engine",
    version="1.0.0",
)

# Phase 1.6 / roadmap §21: "Errors follow a single shape:
# {"error": {"code", "message", "details"}}" -- registered once, here,
# so every HTTPException/validation-error/unhandled-exception response
# across every endpoint is reshaped centrally. See
# codepulse/api/errors.py's module docstring for the full design.
install_error_handlers(app)


# -------------------------------------------------------------
# Phase 1.4: in-process token-bucket rate limiter (roadmap §37 item 1.4;
# backlog B11). Registered BEFORE CORSMiddleware below so that, per
# Starlette's "last middleware added wraps outermost" rule, CORSMiddleware
# ends up as the OUTERMOST layer -- a 429 rate-limit rejection generated
# here still passes back out through CORSMiddleware and gets real CORS
# headers attached, exactly as a 401/403 auth rejection already does
# (raised even deeper, inside a route's own dependency). See
# codepulse/rate_limit.py's module docstring for the full design
# rationale (identity = raw API key header or client IP, in-process only,
# no Redis, not a per-role/plan differentiated limit).
# -------------------------------------------------------------
@app.middleware("http")
async def _rate_limit_middleware(request: Request, call_next):
    # Reads settings fresh on every request (not the module-level
    # `settings` captured once at import time below, which CORSMiddleware
    # itself is stuck with by construction) so tests can reconfigure rate
    # limits via `codepulse.config.set_settings` + `reset_limiter()`.
    current_settings = get_settings()
    if not current_settings.rate_limit_enabled:
        return await call_next(request)

    identity = request.headers.get("X-API-Key") or (
        request.client.host if request.client else "unknown"
    )
    allowed, retry_after = get_limiter().check(identity)
    if not allowed:
        # Phase 1.6: built via the same shared error_json_response helper
        # the app's global exception handlers use -- see
        # codepulse/api/errors.py's module docstring for why this
        # middleware can't just `raise HTTPException(...)` and rely on
        # those handlers to catch it.
        retry_after_seconds = math.ceil(retry_after)
        _record_api_error(request, 429)
        return error_json_response(
            429,
            "Rate limit exceeded. Try again later.",
            {"retry_after_seconds": retry_after_seconds},
            headers={"Retry-After": str(retry_after_seconds)},
        )
    return await call_next(request)


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


# -------------------------------------------------------------
# 0. Health + Metrics (roadmap §37 item 7.4; §18 Observability
# Architecture; §21: "Every endpoint requires an API key (§15) except
# `/health/*`" -- these three, plus /metrics for the same reason
# real Prometheus scraping is never per-repository-scoped, carry NO
# `Depends(require_api_key)`.
# -------------------------------------------------------------
@app.get("/health/live")
def health_live():
    """Process is up. No dependency checks -- a failure here means the process itself is unresponsive, not that some downstream is degraded."""
    return {"status": "live"}


@app.get("/health/ready")
def health_ready():
    """DB reachable, embedding model loaded, index storage writable -- §18's own exact three checks. A "ready" failure should stop new traffic/jobs, not restart the process, which is why this is a separate endpoint from /health/live."""
    from sqlalchemy import text as _text

    checks: dict[str, bool] = {}

    try:
        session = storage_manager.get_session()
        try:
            session.execute(_text("SELECT 1"))
        finally:
            session.close()
        checks["database_reachable"] = True
    except Exception:
        checks["database_reachable"] = False

    checks["embedding_model_loaded"] = indexing_engine.embedder is not None and getattr(
        indexing_engine.embedder, "_model", None
    ) is not None

    try:
        db_dir = os.path.dirname(os.path.abspath(storage_manager.db_url.replace("sqlite:///", ""))) or "."
        checks["index_storage_writable"] = os.access(db_dir, os.W_OK)
    except Exception:
        checks["index_storage_writable"] = False

    ready = all(checks.values())
    status_code = 200 if ready else 503
    from fastapi.responses import JSONResponse as _JSONResponse
    return _JSONResponse(status_code=status_code, content={"status": "ready" if ready else "not_ready", "checks": checks})


@app.get("/metrics")
def metrics():
    """Real, current Prometheus exposition-format metrics -- §18's own named counters/histograms/gauge, generated fresh on every scrape."""
    from fastapi.responses import Response as _Response
    from codepulse.observability import job_queue_depth, render_metrics
    from codepulse.storage.db import IndexJobModel

    # Refresh the gauge (a point-in-time CURRENT value, not a running
    # total) right before serializing, from a real query -- never a
    # cached/stale count.
    try:
        session = storage_manager.get_session()
        try:
            depth = session.query(IndexJobModel).filter(
                IndexJobModel.status.in_(["queued", "running"])
            ).count()
        finally:
            session.close()
        job_queue_depth.set(depth)
    except Exception:
        pass

    body, content_type = render_metrics()
    return _Response(content=body, media_type=content_type)

# Request / Response Schemas
class SearchRequest(BaseModel):
    repository_id: Optional[int] = 1
    query: str
    limit: int = Field(default=20, ge=1, le=100)
    # Phase 2.3 (roadmap §37 item 2.3): "rrf" added -- the already-
    # implemented Reciprocal Rank Fusion path, now selectable here.
    search_type: str = Field(default="hybrid", pattern="^(hybrid|keyword|semantic|rrf)$")


class IndexRepoRequest(BaseModel):
    # Backend-hardening cleanup (not a roadmap sprint item): a
    # `repository_id` field used to be declared here but was never read
    # anywhere in `index_repository`'s body -- grep-confirmed dead before
    # removal. The route always resolves the real repository id itself,
    # via `_resolve_repo_id(repo_name, req.url)`, keyed on `url` (the
    # same natural-key identity `IndexingEngine.index_directory` uses
    # internally) -- a caller has never needed to (and could not
    # meaningfully) supply this id up front.
    source: str = "local"  # "local", "github", "gitlab"
    url: str  # local path or git URL
    branch: str = "main"
    index_dependencies: bool = True


class SimilarCodeRequest(BaseModel):
    limit: int = 10
    threshold: float = 0.75


class ImpactAnalysisRequest(BaseModel):
    depth: int = 3


class AgentContextRequest(BaseModel):
    # Phase 5.2 (roadmap §12/§37 item 5.2): the roadmap's own literal
    # path is `/v1/agent/context` -- no `{symbol_id}` path placeholder
    # -- so the target symbol is identified in the request body instead
    # of the URL, unlike every other symbol-scoped endpoint in this API.
    symbol_id: int
    include_body: bool = False
    max_related: int = 10


class IdeSearchRequest(BaseModel):
    repository_id: Optional[int] = 1
    selected_text: str
    file_context: Optional[str] = None
    limit: int = 5


class FeedbackRequest(BaseModel):
    search_id: str
    result_id: int
    feedback: str  # "relevant", "irrelevant", "perfect"


class CreateAPIKeyRequest(BaseModel):
    name: str
    # Empty list = an unscoped key (authenticated but authorized for no
    # repository-scoped operation, matching Phase 1.2's own "no
    # wildcard/superuser key" design) -- see create_key's own
    # authorization comment for why this is the one case that doesn't
    # require admin on anything.
    repository_ids: list[int] = Field(default_factory=list)
    role: str = "viewer"


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
    # Backend-hardening addition (not a roadmap sprint item): "hybrid"/
    # "rrf" already gracefully degrade to keyword-only results inside
    # IndexingEngine.search() itself if the embedding model is
    # unavailable (see that method's own docstring). Only a pure
    # "semantic" request has no fallback and can still raise here --
    # caught explicitly so it surfaces as a clear, honest 503 rather
    # than the generic, opaque 500 an uncaught exception would produce.
    try:
        results = indexing_engine.search(
            req.query,
            limit=req.limit,
            search_type=req.search_type,
            repository_id=req.repository_id,
        )
    except EmbeddingUnavailableError as e:
        raise HTTPException(
            status_code=503,
            detail=(
                f"The embedding model is currently unavailable, so a "
                f"search_type='{req.search_type}' request cannot be "
                f"completed as asked: {e}. Try search_type='keyword' instead."
            ),
        )
    elapsed = time.time() - t0
    exec_time_ms = int(elapsed * 1000)

    # Phase 7.4 (roadmap §37 item 7.4; §18: "search_requests_total
    # {search_type}", "search_latency_seconds", "search_zero_result_total").
    from codepulse.observability import search_latency_seconds, search_requests_total, search_zero_result_total
    search_requests_total.labels(search_type=req.search_type).inc()
    search_latency_seconds.observe(elapsed)
    if not results:
        search_zero_result_total.inc()

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
    # already-known repositories). So only authentication (a valid,
    # non-revoked key) is required here; no `auth.authorize_repository(...)`
    # call. Unchanged by Phase 1.3/1.5.

    # Phase 1.5 / roadmap §15: git-URL ingestion safety (SSRF check).
    # This codebase has no git-clone implementation at all (grep-confirmed
    # -- see codepulse/ingestion_safety.py's module docstring for the full
    # rationale): `req.source` has always been accepted but never actually
    # branched on, so a non-"local" source previously fell through to
    # silently mis-treating a git URL as a local path (indexing zero files
    # while claiming "queued"). This milestone's job is the SSRF check
    # itself -- a real, independently-tested safety primitive -- not
    # inventing git-URL fetching, which no sprint item assigns to any
    # milestone. So an unsafe URL is rejected here (400) even though the
    # feature isn't implemented; a *safe* URL still gets an honest 501
    # rather than being silently mishandled.
    if req.source != "local":
        try:
            validate_git_url(req.url)
        except IngestionSafetyError as e:
            raise HTTPException(status_code=400, detail=str(e))
        raise HTTPException(
            status_code=501,
            detail=(
                f"Ingestion source '{req.source}' is not implemented; only "
                f"source='local' is currently supported."
            ),
        )

    # Phase 7.2 (roadmap §37 item 7.2; §15: "indexer (viewer + trigger
    # indexing)"): if `req.url` already names a KNOWN repository, this
    # call is a RE-index of something a key may already be scoped to at
    # only "viewer" -- require at least "indexer" role for that
    # specific repository.
    #
    # 2026-10-14 (FUTURE_CHANGES item 18, user-approved owner semantics): a
    # genuinely NEW repository used to be open to ANY authenticated key, which
    # then could not poll its own job or ever see the repository (the VS Code
    # extension's Index Workspace on a new folder). Now creating one requires
    # the `indexer` role (or higher) on at least one EXISTING repository, and
    # the creating key is made `admin` of it (below).
    #
    # All of this is decided BEFORE the path is validated, so a key that may
    # not index this repository cannot use the 400-vs-403 difference to probe
    # which paths exist on the host.
    _session = storage_manager.get_session()
    try:
        _existing_repo = find_repository(_session, req.url)
        _existing_repo_id = _existing_repo.id if _existing_repo else None
    finally:
        _session.close()
    if _existing_repo_id is not None:
        auth.authorize_role(_existing_repo_id, "indexer")
    elif not any(ROLE_RANK.get(role, -1) >= ROLE_RANK["indexer"] for role in auth.repository_roles.values()):
        raise HTTPException(
            status_code=403,
            detail=(
                "Creating a new repository requires the 'indexer' role (or higher) on at "
                "least one existing repository."
            ),
        )

    # Phase 1.5 / roadmap §15: local-path ingestion safety -- allow-list
    # containment + `..`-traversal/symlink-escape rejection. See
    # codepulse.ingestion_safety.validate_local_path's docstring; an empty
    # (default) ingestion_allowed_roots means unrestricted, preserving the
    # local-first zero-configuration experience.
    try:
        resolved_path = validate_local_path(req.url, get_settings().ingestion_allowed_roots)
    except IngestionSafetyError as e:
        raise HTTPException(status_code=400, detail=str(e))

    repo_name = os.path.basename(req.url.rstrip("/\\")) or os.path.basename(req.url)
    # One identity per repository however its path is spelled (FUTURE_CHANGES
    # item 2): the lookup above finds an existing repository under any spelling,
    # and a new one is stored -- and indexed -- under the canonical form.
    canonical_url = canonical_repository_url(req.url)
    # Phase 1.3: resolve (or create) the real repository id up front, so the
    # new IndexJob row can be linked to a real repository id and the
    # one-active-job-per-repository check below has something to check
    # against, before indexing itself has actually run. Deliberately still
    # keyed on req.url (not resolved_path) -- unchanged natural-key identity
    # from Phase 1.3; only the actual filesystem walk below uses the
    # safety-verified, canonicalized resolved_path.
    if _existing_repo_id is not None:
        repo_id = _existing_repo_id
    else:
        # The row and the creator's admin grant are written in one
        # transaction, and only the call that actually creates the
        # repository becomes its owner.
        repo_id, created = create_repository_owned_by(storage_manager, repo_name, canonical_url, auth.api_key_id)
        if not created:
            # Another request created this url first: no grant here; apply
            # the normal existing-repository role check.
            auth.authorize_role(repo_id, "indexer")

    # Phase 1.3 / roadmap §8: "one active job per repository (advisory
    # lock on repo_id) to avoid concurrent conflicting writes." See
    # codepulse.jobs.get_active_job's docstring for why this is an
    # application-level check, not a hard DB constraint.
    active = get_active_job(storage_manager, repo_id)
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Repository {repo_id} already has an active indexing job "
                f"(job_{active.id}, status={active.status})"
            ),
        )

    job = create_job(storage_manager, repo_id)

    def run_indexing():
        # Phase 1.3: drives the job through the roadmap §8 state machine
        # (queued [already set by create_job above] -> running ->
        # succeeded|failed) around the actual index_directory() call.
        # IndexingEngine itself has no knowledge of jobs -- this
        # orchestration lives entirely here, at the API boundary (see
        # codepulse/jobs.py's module docstring).
        mark_running(storage_manager, job.id)
        # Phase 7.4 (roadmap §37 item 7.4; §18: "indexing_duration_seconds",
        # "files_indexed_total", "symbols_indexed_total").
        from codepulse.observability import files_indexed_total, indexing_duration_seconds, symbols_indexed_total
        _t0 = time.time()
        try:
            result = indexing_engine.index_directory(
                repo_path=resolved_path,
                repo_name=repo_name,
                repo_url=canonical_url,
            )
            indexing_duration_seconds.observe(time.time() - _t0)
            files_indexed_total.inc(result.get("total_files", 0))
            symbols_indexed_total.inc(result.get("total_symbols", 0))
            mark_succeeded(storage_manager, job.id, result)
        except Exception as e:
            # Phase 1.5: this also catches IngestionSafetyError raised
            # inside index_directory() for the "max files per repo" cap
            # (discoverable only after the walk, so it can't be checked
            # synchronously up front the way the allow-list/SSRF checks
            # above are) -- surfaced as a real "failed" job with the real
            # error message, never a fabricated success.
            logger.exception("Indexing job job_%s failed", job.id)
            mark_failed(storage_manager, job.id, str(e))

    bg_tasks.add_task(run_indexing)

    # Phase 0.7: no `estimated_time_seconds` here -- indexing duration is
    # not measured or predicted anywhere in the system (no timing model,
    # no historical job data), so a fixed guess would be a fabricated
    # metric.
    # Phase 1.3: `job_id`/`status` are now real and backed by the IndexJob
    # row just created above (pollable via GET /v1/jobs/{job_id} through
    # to actual completion) -- replacing the previous fake
    # `f"job_{int(time.time())}"` with no backing state at all.
    return {
        "job_id": f"job_{job.id}",
        "repository_id": repo_id,
        "status": job.status,
        "url": canonical_url,
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

        # Query call graph relations. Repository-isolation fix: scoped to
        # `sym.file.repo_id` -- previously filtered by symbol name alone,
        # a confirmed cross-repository data leak (a caller authorized for
        # one repository could receive another, unrelated repository's
        # call-graph edges whenever symbol names happened to collide).
        # `CallEdge.repo_id` is nullable (a legacy row that predates this
        # fix and could not be unambiguously backfilled) -- such a row
        # simply never matches this filter, so it's invisible here rather
        # than leaking; see CallEdge's own docstring.
        #
        # Phase 4.1 compatibility correction: Phase 4.1 started persisting
        # inheritance relationships (`relationship_type="inherits"`) into
        # this SAME `CallEdge` table. Without this filter, a base class's
        # `called_by` (or a subclass's `calls`) would silently include its
        # subclasses (or base classes), mislabeled as ordinary call
        # relationships -- `called_by`/`calls` are documented, historical,
        # calls-only fields; this endpoint's contract was never widened to
        # include inheritance. Explicitly restricting to `"calls"` keeps
        # this endpoint's semantics exactly what they were before Phase
        # 4.1 introduced the second relationship type into this table.
        called_by = (
            session.query(CallEdge)
            .filter(CallEdge.target_symbol_name == sym.name)
            .filter(CallEdge.repo_id == sym.file.repo_id)
            .filter(CallEdge.relationship_type == "calls")
            .all()
        )
        calls = (
            session.query(CallEdge)
            .filter(CallEdge.source_symbol_name == sym.name)
            .filter(CallEdge.repo_id == sym.file.repo_id)
            .filter(CallEdge.relationship_type == "calls")
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
            # Phase 3.4 (roadmap §11, §42): "confidence scoring surfaced
            # to callers, not hidden" -- `d.confidence` is `None` for any
            # row persisted before this milestone's migration (see
            # CallEdge's own docstring), never fabricated to something
            # else just to fill the field.
            "called_by": [
                {"symbol": d.source_symbol_name, "file": d.source_file, "confidence": d.confidence}
                for d in called_by
            ],
            "calls": [
                {"symbol": d.target_symbol_name, "file": d.target_file, "confidence": d.confidence}
                for d in calls
            ],
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

        # Phase 5.1 (roadmap §12 "related_symbols"): moved into the
        # shared `IndexingEngine.find_similar_symbols` intelligence-core
        # method so §12's context-bundle assembler can reuse it instead
        # of duplicating semantic-similarity logic a second time. Fixed
        # a real, pre-existing repository-isolation gap while extracting
        # it: the vector search here was NOT scoped to `repo_id` at all
        # (unlike `IndexingEngine.search()`'s own "semantic" branch,
        # which has been repo-scoped since Phase 0.6) -- a caller could
        # receive another repository's "similar" symbols. The shared
        # method now filters by `repo_id` exactly like `search()` does.
        query_text = f"{sym.signature}\n{sym.docstring or ''}\n{sym.body[:500]}"
        similar_items = indexing_engine.find_similar_symbols(
            query_text, sym.name, sym.file.repo_id, threshold=req.threshold, limit=req.limit
        )

        return {
            "query_symbol": {"id": sym.id, "name": sym.name},
            "similar": similar_items,
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

        # Phase 4.2 (roadmap §37 item 4.2): graph analysis moved out of
        # this route and into the shared `IndexingEngine.analyze_impact`
        # intelligence-core method (this milestone's own audit found the
        # bucketing logic embedded directly in this handler, duplicated a
        # third time in the CLI). `sym.file.repo_id` -- resolved from the
        # REQUESTED symbol's own file, never a client-suppliable
        # parameter -- is passed through explicitly so the shared method
        # looks up the correctly-scoped `indexing_engine.call_graphs`
        # (per-repo) rather than the singular, "most recently indexed"
        # `indexing_engine.call_graph` alias (the same repository-
        # isolation fix already applied here since the Phase 3.4 audit).
        return indexing_engine.analyze_impact(
            sym.name, sym.file.path if sym.file else "", sym.file.repo_id, max_depth=req.depth
        )
    finally:
        session.close()


# -------------------------------------------------------------
# 6b. References (GET /v1/symbols/{id}/references)
# -------------------------------------------------------------
@app.get("/v1/symbols/{symbol_id}/references")
def get_symbol_references(symbol_id: int, auth: AuthorizedKey = Depends(require_api_key)):
    session = storage_manager.get_session()
    try:
        sym = session.query(SymbolModel).filter(SymbolModel.id == symbol_id).first()
        if not sym:
            raise HTTPException(status_code=404, detail="Symbol not found")
        if not sym.file:
            raise HTTPException(status_code=404, detail="Symbol not found")
        auth.authorize_repository(sym.file.repo_id)

        # Phase 4.4 (roadmap §37 item 4.4; §21's `references` endpoint
        # group, kept explicitly SEPARATE from `graph (callers/callees/
        # impact)`): thin adapter over the shared
        # `IndexingEngine.analyze_references` intelligence-core method --
        # same repository-isolation pattern as `analyze_impact` above
        # (`sym.file.repo_id` resolved from the requested symbol's own
        # file, never a client-suppliable parameter).
        return indexing_engine.analyze_references(
            sym.name, sym.file.path if sym.file else "", sym.file.repo_id
        )
    finally:
        session.close()


# -------------------------------------------------------------
# 6c. Agent Context Bundle (POST /v1/agent/context)
# -------------------------------------------------------------
@app.post("/v1/agent/context")
def get_agent_context(req: AgentContextRequest, auth: AuthorizedKey = Depends(require_api_key)):
    session = storage_manager.get_session()
    try:
        sym = session.query(SymbolModel).filter(SymbolModel.id == req.symbol_id).first()
        if not sym:
            raise HTTPException(status_code=404, detail="Symbol not found")
        if not sym.file:
            raise HTTPException(status_code=404, detail="Symbol not found")
        auth.authorize_repository(sym.file.repo_id)

        # Phase 5.2 (roadmap §37 item 5.2; §12 "Agent-Native Retrieval /
        # Context API"): thin adapter over the shared
        # `IndexingEngine.get_context_bundle` intelligence-core method
        # (Phase 5.1) -- same repository-isolation pattern as every
        # other symbol-scoped route (`sym.file.repo_id` resolved from
        # the requested symbol's own file, never a client-suppliable
        # parameter).
        return indexing_engine.get_context_bundle(
            sym.name,
            sym.file.path if sym.file else "",
            sym.file.repo_id,
            signature=sym.signature,
            docstring=sym.docstring,
            body=sym.body,
            include_body=req.include_body,
            max_related=req.max_related,
        )
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
    # Phase 1.2: authentication only, no required repository_id parameter
    # -- deliberately kept as a "no repository concept" endpoint (like
    # feedback/ingestion), not redesigned into a repository_id-scoped
    # route the way /v1/search is. An UNSCOPED key's behavior is
    # therefore unchanged from Phase 1.2: it still sees suggestions
    # blended across every repository currently in memory, matching the
    # tested convention `tests/test_api_authorization.py` documents
    # ("endpoints with no repository concept... succeed with an
    # unscoped key").
    #
    # Backend-hardening fix (not a roadmap sprint item): a key that IS
    # scoped to specific repositories used to leak suggestions from
    # EVERY repository regardless -- a real, previously-undisclosed
    # cross-repository data leak (the same class of bug Phase 3.4/5.1
    # each found and fixed for other endpoints, just never revisited
    # here). Now filtered so a scoped key only ever sees suggestions
    # from repositories it actually holds a grant for.
    prefix_lower = prefix.lower()
    matches = []

    for doc_id, sym in indexing_engine.symbols_by_id.items():
        if auth.repository_ids and indexing_engine.symbol_repo_map.get(doc_id) not in auth.repository_ids:
            continue
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

        # Language distribution.
        #
        # Backend-hardening fix (not a roadmap sprint item): this used to
        # iterate `indexing_engine.parse_results.values()` unfiltered --
        # a real, previously-undisclosed cross-repository leak, since
        # that dict holds every currently-indexed repository's parsed
        # files. A caller authorized for THIS repo_id (the check above)
        # would get a language/file/symbol breakdown blended with every
        # OTHER repository this engine has ever parsed in-process. Now
        # scoped by the same `"{repo_id}::"` key-prefix filter
        # `analyze_module_structure` already established for the exact
        # same dict.
        prefix = f"{repo_id}::"
        langs: dict[str, dict[str, int]] = {}
        for key, res in indexing_engine.parse_results.items():
            if not key.startswith(prefix):
                continue
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


def _parse_job_id(raw: str) -> Optional[int]:
    """
    Phase 1.3: accepts either the `"job_<id>"` form POST
    /v1/repositories/index returns, or a bare integer, for
    GET /v1/jobs/{job_id}. Returns None if unparseable.
    """
    s = raw[len("job_"):] if raw.startswith("job_") else raw
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


# -------------------------------------------------------------
# 13. Get Job Status (GET /v1/jobs/{job_id})
# -------------------------------------------------------------
@app.get("/v1/jobs/{job_id}")
def get_job_status(job_id: str, auth: AuthorizedKey = Depends(require_api_key)):
    """
    Phase 1.3 (roadmap §37 item 1.3; §21: "jobs (new -- real job status,
    replacing the fake job_id)"). Polls a real IndexJob row through to its
    actual completion -- `status` is one of the roadmap §8 state machine's
    values (`queued`, `running`, `succeeded`, `failed`; `cancelled` is a
    valid schema value but unreachable in this milestone -- no cancel flow
    exists yet, matching how Phase 1.1 left `revoked_at` schema-only).
    `result` is populated only once `status == "succeeded"`, from the
    real, measured return value of `IndexingEngine.index_directory()` --
    never fabricated/estimated.
    """
    numeric_id = _parse_job_id(job_id)
    if numeric_id is None:
        raise HTTPException(status_code=404, detail="Job not found")

    job = get_job(storage_manager, numeric_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    # Phase 1.3: a job belongs to exactly one repository -- resolve and
    # authorize it before returning any content, the same established
    # Phase 1.2 pattern already used for symbol/file endpoints that
    # resolve their owning repository via a FK rather than a direct
    # repository_id parameter.
    auth.authorize_repository(job.repo_id)

    return {
        "job_id": f"job_{job.id}",
        "repository_id": job.repo_id,
        "status": job.status,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "error": job.error_message,
        "result": job.result,
    }


# -------------------------------------------------------------
# 14. API Key Management (POST/GET /v1/admin/keys, DELETE /v1/admin/keys/{id})
#
# Backend-hardening addition (not a roadmap sprint item) -- gives §15's
# `admin` role ("all + manage keys/repos") its first real REST surface;
# before this, `create_api_key`/`revoke_api_key` were Python-function-only
# operations (disclosed as a known gap since Phase 7.2). Thin wrappers
# over codepulse.auth's key-management primitives, matching this API's
# established "shared logic lives outside app.py" pattern.
#
# Authorization model, deliberately simple and symmetric (no separate
# "key owner" concept exists in the schema -- adding one would need its
# own migration, out of this addition's scope): creating a key scoped to
# specific repositories, or revoking a key that holds any grant, both
# require the caller to hold "admin" role on EVERY repository involved --
# never on a subset, so one admin can never silently create/revoke access
# to a repository a DIFFERENT admin, not the caller, actually administers.
# An unscoped key (no repository_ids) has no specific repository to check
# admin-on; for that one case, the caller must hold "admin" somewhere
# (proving it is genuinely an admin-role key, not merely authenticated).
# -------------------------------------------------------------
def _require_admin_somewhere(auth: AuthorizedKey, action: str) -> None:
    if not any(role == "admin" for role in auth.repository_roles.values()):
        raise HTTPException(
            status_code=403,
            detail=f"{action} an unscoped API key requires admin role on at least one repository.",
        )


@app.post("/v1/admin/keys", status_code=201)
def create_key(req: CreateAPIKeyRequest, auth: AuthorizedKey = Depends(require_api_key)):
    if req.repository_ids:
        for repository_id in req.repository_ids:
            auth.authorize_role(repository_id, "admin")
    else:
        _require_admin_somewhere(auth, "Creating")

    try:
        created = create_api_key(
            storage_manager, req.name, repository_ids=req.repository_ids or None, role=req.role
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "id": created.id,
        "name": created.name,
        "key_prefix": created.key_prefix,
        "created_at": created.created_at.isoformat(),
        # The raw key is returned exactly once, here -- it is never
        # persisted and cannot be recovered by any later call (matches
        # codepulse.auth.create_api_key's own documented lifecycle).
        "api_key": created.raw_key,
        "repository_ids": req.repository_ids,
        "role": req.role,
    }


@app.get("/v1/admin/keys")
def list_keys(auth: AuthorizedKey = Depends(require_api_key)):
    admin_repository_ids = {
        repository_id for repository_id, role in auth.repository_roles.items() if role == "admin"
    }
    if not admin_repository_ids:
        raise HTTPException(
            status_code=403, detail="Listing API keys requires admin role on at least one repository."
        )

    summaries = list_api_keys_for_repositories(storage_manager, admin_repository_ids)
    return {
        "keys": [
            {
                "id": s.id,
                "name": s.name,
                "key_prefix": s.key_prefix,
                "created_at": s.created_at.isoformat(),
                "revoked_at": s.revoked_at.isoformat() if s.revoked_at else None,
                # Only the grants the CALLER administers are disclosed --
                # a key visible here because it shares one admin
                # repository with the caller may hold OTHER grants on
                # repositories the caller has no rights over; those are
                # never included, deliberately.
                "grants": [
                    {"repository_id": g.repository_id, "role": g.role}
                    for g in s.grants
                    if g.repository_id in admin_repository_ids
                ],
            }
            for s in summaries
        ]
    }


@app.delete("/v1/admin/keys/{key_id}")
def revoke_key(key_id: int, auth: AuthorizedKey = Depends(require_api_key)):
    key_repository_ids = get_api_key_repository_ids(storage_manager, key_id)
    if key_repository_ids is None:
        raise HTTPException(status_code=404, detail="API key not found")

    if key_repository_ids:
        for repository_id in key_repository_ids:
            auth.authorize_role(repository_id, "admin")
    else:
        _require_admin_somewhere(auth, "Revoking")

    summary = revoke_api_key(storage_manager, key_id)
    return {
        "id": summary.id,
        "revoked_at": summary.revoked_at.isoformat() if summary.revoked_at else None,
    }


# -------------------------------------------------------------
# 15. Module Structure (GET /v1/repositories/{repo_id}/modules)
#
# Backend-hardening addition (not a roadmap sprint item) -- gives Phase
# 4.3's `ModuleGraphBuilder`/`IndexingEngine.analyze_module_structure`
# its first query surface. That milestone's own explicit approved scope
# deliberately built the engine capability with NO REST/CLI/MCP caller
# ("library/shared-core only... a future one can, through this exact
# method, without duplicating any of ModuleGraphBuilder's own
# construction logic" -- see that method's own docstring); this is that
# future caller. Thin wrapper only: no clustering/ranking logic lives
# here, exactly matching every other REST route's relationship to the
# shared intelligence core.
# -------------------------------------------------------------
@app.get("/v1/repositories/{repo_id}/modules")
def get_repository_modules(repo_id: int, auth: AuthorizedKey = Depends(require_api_key)):
    """
    Computed fresh on every call from the current in-memory parse-result
    snapshot (unchanged from `analyze_module_structure`'s own documented
    behavior -- no persistence, no incremental machinery). Returns an
    empty graph, not an error, for a repository with no cached parse
    results this process lifetime (e.g. before its first index, or
    after a restart) -- the same restart-behavior contract every other
    in-memory-graph-backed endpoint (impact-analysis, references)
    already has.
    """
    auth.authorize_repository(repo_id)
    module_graph = indexing_engine.analyze_module_structure(repo_id)
    return {
        "repository_id": repo_id,
        "clusters": module_graph.clusters,
        # §11's own literal heuristic wording, kept as raw, unlabeled
        # degree data -- see ModuleGraphBuilder.rank_by_inbound/outbound's
        # own docstrings for why no "entry point"/"utility" label is
        # attached here.
        "ranked_by_inbound": module_graph.rank_by_inbound(),
        "ranked_by_outbound": module_graph.rank_by_outbound(),
    }


# -------------------------------------------------------------
# 16. List Repositories (GET /v1/repositories)
#
# Backend-hardening addition (not a roadmap sprint item) -- Phase 0 of
# `CODEPULSE_CLIENT_STRATEGY.md`'s VS Code extension plan (and the Web
# UI plan's own Phase 0): every existing repository-scoped route needs
# a `repo_id` the caller already knows -- there was no way to enumerate
# which repositories a key is even authorized for. A client opening an
# unfamiliar workspace (the extension) or building its first dashboard
# screen (the Web UI) both need exactly this.
# -------------------------------------------------------------
@app.get("/v1/repositories")
def list_repositories(auth: AuthorizedKey = Depends(require_api_key)):
    """
    Lists every repository the CALLING KEY actually holds a grant for --
    never every repository in the database. An unscoped key (no grants
    at all) gets an empty list, not an error and not every repository
    in the system: matches Phase 1.2's own "no wildcard/superuser key"
    design (an unscoped key is authenticated but authorized for NO
    repository-scoped operation) exactly, the same rule every other
    repository-scoped route already enforces via `authorize_repository`.
    """
    if not auth.repository_ids:
        return {"repositories": []}

    session = storage_manager.get_session()
    try:
        repos = (
            session.query(RepositoryModel)
            .filter(RepositoryModel.id.in_(auth.repository_ids))
            .order_by(RepositoryModel.id)
            .all()
        )
        return {
            "repositories": [
                {
                    "repository_id": r.id,
                    "name": r.name,
                    "url": r.url,
                    "branch": r.branch,
                    "file_count": r.file_count,
                    "symbol_count": r.symbol_count,
                    "last_sync": r.last_sync.isoformat() if r.last_sync else None,
                }
                for r in repos
            ]
        }
    finally:
        session.close()


# -------------------------------------------------------------
# 17. Resolve Symbol at Location (GET /v1/repositories/{repo_id}/resolve)
#
# Backend-hardening addition (not a roadmap sprint item) -- the second
# Phase 0 prerequisite from `CODEPULSE_CLIENT_STRATEGY.md`'s VS Code
# extension plan specifically: a hover provider or CodeLens needs to
# answer "what symbol contains line N of this file," and neither
# `GET /v1/symbols/{id}` (needs a symbol id) nor
# `GET /v1/files/{id}/symbols` (needs a file id, returns every symbol
# in the file, not just the one at a specific line) answers that
# directly. Resolving by (repo_id, path, line) server-side, in one
# call, keeps "which symbol contains this line" logic in the shared
# intelligence core rather than duplicated client-side -- CLAUDE.md's
# own "REST/CLI/MCP call the same underlying functions" rule, applied
# to a brand-new capability rather than an existing engine method.
# -------------------------------------------------------------
@app.get("/v1/repositories/{repo_id}/resolve")
def resolve_symbol_at_location(
    repo_id: int,
    path: str = Query(..., min_length=1),
    line: int = Query(..., ge=1),
    auth: AuthorizedKey = Depends(require_api_key),
):
    """
    Finds the symbol in `path` (a file path relative to the repository
    root, exactly as stored in `FileModel.path`) that CONTAINS `line`
    (1-indexed, inclusive of `start_line`/`end_line`). When a line falls
    inside more than one symbol's range at once (e.g. a method's own
    line range is nested inside its enclosing class's range), the
    NARROWEST-range match wins -- the most specific symbol actually at
    that line, matching what a hover/CodeLens caller actually wants
    (the method, not the whole class). Returns `{"symbol": null}` (200,
    not 404) for a real file with no symbol at that exact line (e.g. a
    blank line, an import, or a comment) -- a real, valid outcome, not
    an error. 404 is reserved for the file itself not being found in
    this repository.
    """
    auth.authorize_repository(repo_id)
    session = storage_manager.get_session()
    try:
        file_record = (
            session.query(FileModel)
            .filter(FileModel.repo_id == repo_id, FileModel.path == path)
            .first()
        )
        if file_record is None:
            raise HTTPException(status_code=404, detail=f"File not found in this repository: {path!r}")

        candidates = (
            session.query(SymbolModel)
            .filter(
                SymbolModel.file_id == file_record.id,
                SymbolModel.start_line <= line,
                SymbolModel.end_line >= line,
            )
            .all()
        )
        if not candidates:
            return {"repository_id": repo_id, "file": path, "line": line, "symbol": None}

        narrowest = min(candidates, key=lambda s: (s.end_line - s.start_line, s.start_line))
        return {
            "repository_id": repo_id,
            "file": path,
            "line": line,
            "symbol": {
                "symbol_id": narrowest.id,
                "name": narrowest.name,
                "type": narrowest.type,
                "line_start": narrowest.start_line,
                "line_end": narrowest.end_line,
                "docstring": narrowest.docstring,
                "complexity": narrowest.complexity,
            },
        }
    finally:
        session.close()


@app.get("/v1/repositories/{repo_id}/file-symbols")
def list_symbols_for_file_path(
    repo_id: int,
    path: str = Query(..., min_length=1),
    auth: AuthorizedKey = Depends(require_api_key),
):
    """
    Lists every symbol declared in `path` (relative to the repository
    root), ordered by `start_line`. Mirrors `GET /v1/files/{file_id}/symbols`
    but keyed by `(repo_id, path)` like `resolve` above, so a client that
    only knows a workspace-relative path (e.g. a CodeLens provider scanning
    an open document) doesn't need a separate file_id lookup round-trip.
    """
    auth.authorize_repository(repo_id)
    session = storage_manager.get_session()
    try:
        file_record = (
            session.query(FileModel)
            .filter(FileModel.repo_id == repo_id, FileModel.path == path)
            .first()
        )
        if file_record is None:
            raise HTTPException(status_code=404, detail=f"File not found in this repository: {path!r}")

        symbols = (
            session.query(SymbolModel)
            .filter(SymbolModel.file_id == file_record.id)
            .order_by(SymbolModel.start_line)
            .all()
        )
        return {
            "repository_id": repo_id,
            "file": path,
            "symbols": [
                {
                    "symbol_id": s.id,
                    "name": s.name,
                    "type": s.type,
                    "line_start": s.start_line,
                    "line_end": s.end_line,
                    "complexity": s.complexity,
                }
                for s in symbols
            ],
        }
    finally:
        session.close()
