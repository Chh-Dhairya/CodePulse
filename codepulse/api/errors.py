"""
Phase 1.6 — Structured error response format across all endpoints
(roadmap §37 item 1.6; §21 API Strategy: "Errors follow a single shape:
`{"error": {"code": "...", "message": "...", "details": {...}}}`").

Scope, precisely (do not infer requirements from later milestones):
  - Every error response this REST API returns -- from every existing
    `HTTPException` raise site (401/403/404/409/429/501), from FastAPI's
    own automatic request-validation failures (422), from the rate-limit
    middleware's own 429 (Phase 1.4, constructed directly rather than
    raised -- see below for why it needs its own explicit call), and from
    a genuinely unhandled exception (500) -- is reshaped into the single
    §21 envelope, via handlers registered ONCE on `app` (the same "one
    central choke point, not re-implemented per endpoint" principle
    already established for authorization in Phase 1.2 and rate limiting
    in Phase 1.4). No individual route/endpoint function's own
    `raise HTTPException(...)` call sites needed to change to achieve
    this -- reshaping happens centrally, after the fact.
  - `code` is derived deterministically from the HTTP status via Python's
    own `http.HTTPStatus` enum (e.g. 404 -> "not_found", 429 ->
    "too_many_requests", 501 -> "not_implemented") -- not an invented
    string table, so every status code already in use in this app (and
    any added later) maps correctly without this module needing a
    per-code entry.
  - `message` is the existing error text, unchanged -- no error-message
    wording anywhere in the app was rewritten by this milestone.
  - `details` carries real, already-computed structured information where
    it exists (currently: the rate limiter's real `retry_after_seconds`
    for 429s -- the exact same value already used for the `Retry-After`
    header, roadmap-required since Phase 1.4 -- and the raw Pydantic
    validation error list for 422s) and is an empty `{}` otherwise --
    never a fabricated/estimated value, per this project's "no fabricated
    metrics" invariant applied to error payloads too.
  - A genuinely unhandled exception (500) is logged server-side
    (`logger.exception`, so it is never silently swallowed) and reported
    to the client with a generic, non-leaking `message`
    ("Internal server error") and empty `details` -- never the raw
    exception text or a traceback, to avoid an information-disclosure
    regression while still adopting the new envelope shape.

Why the rate-limit middleware needs its own explicit call to
`error_json_response` rather than just `raise HTTPException(...)`:
Starlette's `ExceptionMiddleware` (which dispatches to `@app.exception_
handler(...)` registrations) sits INSIDE the app's user-added middleware
stack -- closer to routing, not wrapping it. An exception raised from
inside a custom `@app.middleware("http")` function (like the Phase 1.4
rate limiter) is never seen by `ExceptionMiddleware` at all, so a plain
`raise HTTPException(...)` there would never reach these handlers. Both
the middleware and the exception handlers below call the exact same
`error_json_response` helper instead, so the shaping logic itself is
never duplicated between the two call sites.

Deliberately NOT in scope for Phase 1.6 (do not infer these belong here):
  - Changing any error's HTTP status code or message wording -- this
    milestone reshapes the existing responses; it does not redesign what
    they say.
  - MCP's own distinct error shape (roadmap §13: a flat
    `{"error": "code_string", "message": "..."}`, no "details") -- a
    different section's requirement for a surface (MCP, Phase 5) that
    does not exist yet; this module implements only §21's REST shape.
  - A security test suite (Phase 1.7).

This module lives in `codepulse/api/` (not a top-level module like
`auth.py`/`jobs.py`/`rate_limit.py`) deliberately: unlike those, this is a
REST-HTTP-response-shape concern specifically (§21's envelope), not a
capability a future CLI or MCP surface would reuse -- MCP has its own,
different error shape per §13, and the CLI never returns HTTP responses
at all.
"""
from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def status_code_to_error_code(status_code: int) -> str:
    """
    Derives a stable, machine-parseable error `code` from an HTTP status
    code via Python's own `http.HTTPStatus` enum -- e.g. 404 -> "not_found",
    429 -> "too_many_requests" -- rather than an invented string table.
    Falls back to the generic "error" for a status code `HTTPStatus`
    itself doesn't recognize.
    """
    try:
        return HTTPStatus(status_code).name.lower()
    except ValueError:
        return "error"


def error_json_response(
    status_code: int,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """
    Builds the single §21 error envelope as a real `JSONResponse`:
    `{"error": {"code", "message", "details"}}`. The one place this
    shape is actually constructed -- every call site below (and the
    rate-limit middleware) goes through this, never duplicating the
    shaping logic.
    """
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": status_code_to_error_code(status_code),
                "message": message,
                "details": details or {},
            }
        },
        headers=headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    """
    Registers the structured-error-response handlers on `app`. Called
    once at app construction time in `codepulse/api/app.py`.
    """

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        details: dict[str, Any] = {}
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        if retry_after is not None:
            details["retry_after_seconds"] = int(retry_after)
        return error_json_response(exc.status_code, str(exc.detail), details, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return error_json_response(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "Request validation failed",
            {"errors": exc.errors()},
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception in request %s %s", request.method, request.url.path)
        return error_json_response(HTTPStatus.INTERNAL_SERVER_ERROR, "Internal server error")
