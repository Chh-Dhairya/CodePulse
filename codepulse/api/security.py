"""
Phase 1.2 — Central authorization dependency (roadmap §37 item 1.2; see also
§15 Security Architecture, §21 API Strategy, §30 Multi-Tenancy, and backlog
item B8: "API key auth + central authorization").

Definition of Done extracted from the roadmap (do not infer beyond this):
  - §37 item 1.2, literally: "Central authorization dependency (FastAPI
    dependency injected into every route)".
  - B8's DoD: "No endpoint callable without a valid, scoped key."
  - §21: "Every endpoint requires an API key (§15) except `/health/*`." No
    `/health/*` endpoint exists yet (a later milestone), so this currently
    means every one of the 12 existing endpoints.
  - §15/§30: repository-scoped isolation must be enforced "in one central
    authorization function all endpoints must call, not re-implemented per
    endpoint" -- exactly what `require_api_key` below is, and exactly the
    "search ignores repository_id" class of bug this closes.

This module provides that one central function. `codepulse/api/app.py`
injects it into every route via `Depends(require_api_key)`; nothing
duplicates its logic per-endpoint.

Scope, precisely -- deliberately NOT inferring later milestones:
  - Reject a request with a missing, unknown, malformed, or revoked API key
    (HTTP 401). Reuses `codepulse.auth.verify_api_key` (Phase 1.1) as the
    sole verification mechanism -- this module does not implement a second
    authentication mechanism.
  - For a request that names a specific repository (a path parameter, a
    body field, or a symbol/file resolved to its owning repository), reject
    it (HTTP 403) unless the authenticated key is explicitly scoped to that
    repository. An API key created with no repository grants at all (Phase
    1.1 permits this) is authenticated but authorized for NO
    repository-scoped operation: fail-closed, least-privilege, matching the
    roadmap's literal "valid, SCOPED key" wording (not "valid key" alone).
    There is no wildcard/superuser key in this milestone.

Deliberately NOT in scope for Phase 1.2 (reserved for later roadmap
milestones -- do not infer these belong here):
  - Role-based permissions (viewer/indexer/admin, roadmap §15) -- Phase 7 /
    B25 (backlog: "APIKey/authorization extension", depends on B8).
  - Rate limiting -- Phase 1.4.
  - Ingestion-specific safety (path allow-listing, SSRF checks, size caps)
    -- Phase 1.5 / B10.
  - A structured `{"error": {"code", "message", "details"}}` response
    envelope for every endpoint -- Phase 1.6. This module raises plain
    `HTTPException`s with a `detail` string, matching every other error
    path already in `api/app.py`; reshaping error responses project-wide is
    1.6's job.
  - A dedicated security test suite -- Phase 1.7. This milestone's own
    focused tests cover only its own behavior.

This module lives in `codepulse/api/` -- the API boundary -- deliberately,
per CLAUDE.md §3 ("authentication belongs at the API boundary; the
intelligence core must remain independent of HTTP/auth concerns").
`codepulse/indexer/engine.py` is not imported here and is not modified by
this milestone.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import Header, HTTPException, status

from codepulse.auth import ROLE_RANK, verify_api_key
from codepulse.storage.db import APIKeyRepositoryModel


@dataclass(frozen=True)
class AuthorizedKey:
    """
    Authorization context for one authenticated request.

    `repository_ids` is the exact, complete set of repository ids the
    authenticated key is scoped to (Phase 1.1's `api_key_repositories`
    grant table) -- empty if the key was created with no repository grants
    at all. There is no sentinel meaning "all repositories": every
    repository-scoped endpoint must check membership in this set via
    `authorize_repository`/`authorize_search_scope`.

    Phase 7.2 (roadmap §37 item 7.2): `repository_roles` maps each
    granted `repository_id` to its own `role` (`"viewer"`/`"indexer"`/
    `"admin"`) -- per §15's "enforced at the repository-permission
    level, not globally," a single key can hold DIFFERENT roles for
    different repositories. `authorize_role` is the role-aware
    counterpart to `authorize_repository`; membership-only endpoints
    keep using `authorize_repository`/`authorize_search_scope`
    unchanged (every role implies at least "viewer," so a plain
    membership check remains correct for read-only endpoints -- it is
    not a security gap, just a coarser check than `authorize_role`
    would make for those routes).
    """

    api_key_id: int
    repository_ids: frozenset[int]
    repository_roles: dict[int, str] = field(default_factory=dict)

    def authorize_repository(self, repository_id: int) -> None:
        """Raises HTTP 403 unless this key is scoped to `repository_id`."""
        if repository_id not in self.repository_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key is not scoped to repository {repository_id}",
            )

    def authorize_role(self, repository_id: int, required_role: str) -> None:
        """
        Raises HTTP 403 unless this key is scoped to `repository_id`
        AND its role there is AT LEAST `required_role` (§15's ordering:
        `viewer` < `indexer` < `admin`). Always checks membership first
        (via `authorize_repository`, reused unmodified) so an
        unauthorized-repository request gets the same 403 it always
        has, before any role-specific detail is considered.
        """
        self.authorize_repository(repository_id)
        held_role = self.repository_roles.get(repository_id)
        if ROLE_RANK.get(held_role, -1) < ROLE_RANK[required_role]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"API key has role {held_role!r} for repository {repository_id}, "
                    f"but {required_role!r} (or higher) is required for this operation."
                ),
            )

    def authorize_search_scope(self, repository_id: int | None) -> None:
        """
        Authorizes a search-style request whose `repository_id` may
        legitimately be omitted (`None`), meaning "search across every
        repository" -- an existing feature of `IndexingEngine.search`
        (Phase 0.6). A `None` repository_id cannot be authorized against
        this key's specific repository grants without either (a) unsafely
        letting it reach repositories outside its scope -- exactly the
        cross-repo leakage class this milestone exists to close (roadmap
        §30) -- or (b) teaching `IndexingEngine.search` to filter by a
        *set* of repository ids, which would couple authorization logic
        into the intelligence core (explicitly out of scope for this
        milestone). So a `None` repository_id is rejected outright here;
        the caller must name the specific repository it wants to search.
        """
        if repository_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "A specific repository_id is required to authorize this "
                    "request; unscoped/global search cannot be authorized "
                    "against a scoped API key."
                ),
            )
        self.authorize_repository(repository_id)


def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> AuthorizedKey:
    """
    FastAPI dependency: verifies the `X-API-Key` request header and returns
    the caller's `AuthorizedKey` context, or raises HTTP 401.

    Resolves `codepulse.api.app`'s CURRENT `storage_manager` at call time
    via a deferred import (not a module-level one), so tests that
    monkeypatch `codepulse.api.app.storage_manager` to an isolated database
    -- the established pattern since Phase 0.7's
    `test_no_fabricated_metrics.py` -- are honored here exactly as they
    already are for `indexing_engine`. This dependency does not open a
    second, independent database connection, and never touches the real
    `codepulse.db` from a test.
    """
    if not x_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing API key")

    import codepulse.api.app as app_module  # deferred: avoids a circular import; re-reads the module's current storage_manager on every call

    api_key_id = verify_api_key(app_module.storage_manager, x_api_key)
    if api_key_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or revoked API key")

    session = app_module.storage_manager.get_session()
    try:
        grants = {
            row.repo_id: row.role
            for row in session.query(APIKeyRepositoryModel)
            .filter(APIKeyRepositoryModel.api_key_id == api_key_id)
            .all()
        }
    finally:
        session.close()

    return AuthorizedKey(
        api_key_id=api_key_id, repository_ids=frozenset(grants.keys()), repository_roles=grants
    )
