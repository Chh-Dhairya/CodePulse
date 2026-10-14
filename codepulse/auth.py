"""
Phase 1.1 — API key model + hashing + creation flow.

Scope, precisely (roadmap §37 item 1.1; see storage/db.py's APIKeyModel
docstring for the schema-level rationale):

  - Generate a cryptographically random API key.
  - Hash it for storage (never persist the raw key anywhere).
  - A creation flow that returns the raw key to the caller exactly once.
  - A verification PRIMITIVE (hash a candidate key, look up the matching
    non-revoked row) -- proves the hashing/storage actually works and is
    usable, but is NOT wired into any API endpoint, FastAPI dependency, or
    request/response flow. That wiring -- "central authorization
    dependency injected into every route" -- is explicitly Phase 1.2, not
    implemented here.

Deliberately NOT in scope for Phase 1.1 (do not infer these from later
Phase 1 milestones): request-header parsing, HTTP 401/403 responses,
FastAPI `Depends()` wiring, rate limiting (roadmap §15's viewer/
indexer/admin roles were Phase 7 / B25 scope -- `create_api_key` below
now accepts a `role` per repository grant, see its own docstring), key
expiry, audit logging.

Backend-hardening addition (not a roadmap sprint item): a real,
mutating `revoke_api_key` now exists below, plus `APIKeySummary`/
`APIKeyGrant`/`get_api_key_repository_ids`/
`list_api_keys_for_repositories` -- the key-management primitives
`codepulse/api/app.py`'s new `/v1/admin/keys` endpoints are thin
wrappers over. `create_api_key` itself is unchanged by this addition.

This module lives outside `codepulse/indexer/` deliberately: per CLAUDE.md
§3, "authentication belongs at the API boundary; the intelligence core
must remain independent of HTTP/auth concerns" -- `IndexingEngine` and the
rest of the intelligence core are untouched by this milestone and do not
import from this module.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError

from codepulse.storage.db import APIKeyModel, APIKeyRepositoryModel, RepositoryModel, StorageManager

API_KEY_PREFIX = "cpk_"
_RAW_KEY_ENTROPY_BYTES = 32  # 256 bits, via secrets.token_urlsafe
_KEY_PREFIX_DISPLAY_LENGTH = len(API_KEY_PREFIX) + 8  # e.g. "cpk_a1b2c3d4"

# Phase 7.2 (roadmap §37 item 7.2; §15's literal minimal-viable model):
# "roles `viewer` (search/read), `indexer` (viewer + trigger indexing),
# `admin` (all + manage keys/repos)." Ordered ranking used by
# `AuthorizedKey.authorize_role` (codepulse/api/security.py) --
# defined here, once, so both modules share the exact same vocabulary
# and ordering rather than each hardcoding its own copy.
VALID_ROLES = ("viewer", "indexer", "admin")
ROLE_RANK = {role: rank for rank, role in enumerate(VALID_ROLES)}


@dataclass(frozen=True)
class CreatedAPIKey:
    """
    Result of `create_api_key`. `raw_key` is populated exactly once, here,
    at creation time -- it is never persisted and cannot be recovered
    later (the standard, expected lifecycle for API keys, matching how
    GitHub/AWS/Stripe-style tokens work). Every other field mirrors a
    column on the persisted `APIKeyModel` row.
    """
    id: int
    name: str
    key_prefix: str
    created_at: datetime
    raw_key: str


def generate_raw_api_key() -> str:
    """Generates a new, cryptographically random API key string."""
    return f"{API_KEY_PREFIX}{secrets.token_urlsafe(_RAW_KEY_ENTROPY_BYTES)}"


def hash_api_key(raw_key: str) -> str:
    """
    Hashes a raw API key for storage/lookup.

    Uses the same SHA-256 hex-digest convention already established
    throughout this project (see codepulse.models.compute_content_hash) --
    a fast cryptographic hash, not a slow/salted password hash (bcrypt/
    argon2), is the correct choice here: the input is already a
    high-entropy, randomly generated secret (not a user-chosen low-entropy
    password), so there is no offline-guessing risk a slow hash would
    mitigate, and a fast hash keeps future lookup-by-hash authentication
    (Phase 1.2) a simple indexed equality query rather than requiring a
    per-row comparison.
    """
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def create_api_key(
    storage: StorageManager,
    name: str,
    repository_ids: list[int] | None = None,
    role: str = "viewer",
) -> CreatedAPIKey:
    """
    Creates a new API key: generates a random raw key, persists only its
    hash (+ a short non-secret prefix) and optional repository scoping,
    and returns the raw key to the caller exactly once.

    `repository_ids`, if given, must all reference existing repositories
    -- this is validated (raising ValueError otherwise) so a key can never
    silently claim to be scoped to a repository that doesn't exist.

    Phase 7.2 (roadmap §37 item 7.2): `role` is applied uniformly to
    every one of `repository_ids`' grant rows created in this one call
    (a caller wanting DIFFERENT roles per repository calls this more
    than once -- §15's "enforced at the repository-permission level,
    not globally" means role lives on the grant row, it does not mean
    one call must support a mixed batch; no roadmap text asks for
    that). Must be one of `VALID_ROLES` (`ValueError` otherwise).
    Defaults to `"viewer"` -- the SAFE, least-privilege choice for
    every NEWLY created grant, deliberately different from
    `APIKeyRepositoryModel.role`'s own schema-level default (`"admin"`,
    which exists only to honestly backfill grants that predate this
    role concept -- see that column's own docstring).
    """
    if role not in VALID_ROLES:
        raise ValueError(f"Invalid role {role!r}; must be one of {VALID_ROLES}")

    session = storage.get_session()
    try:
        if repository_ids:
            existing_ids = {
                r.id
                for r in session.query(RepositoryModel.id)
                .filter(RepositoryModel.id.in_(repository_ids))
                .all()
            }
            missing = set(repository_ids) - existing_ids
            if missing:
                raise ValueError(
                    f"Cannot scope API key to nonexistent repository id(s): {sorted(missing)}"
                )

        raw_key = generate_raw_api_key()
        now = datetime.utcnow()

        api_key = APIKeyModel(
            name=name,
            key_hash=hash_api_key(raw_key),
            key_prefix=raw_key[:_KEY_PREFIX_DISPLAY_LENGTH],
            created_at=now,
        )
        session.add(api_key)
        session.flush()  # assign api_key.id

        for repo_id in repository_ids or []:
            session.add(APIKeyRepositoryModel(api_key_id=api_key.id, repo_id=repo_id, role=role))

        session.commit()

        return CreatedAPIKey(
            id=api_key.id,
            name=api_key.name,
            key_prefix=api_key.key_prefix,
            created_at=api_key.created_at,
            raw_key=raw_key,
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@dataclass(frozen=True)
class APIKeyGrant:
    """One (repository, role) grant for an API key -- mirrors one row of
    `APIKeyRepositoryModel`."""
    repository_id: int
    role: str


@dataclass(frozen=True)
class APIKeySummary:
    """
    A non-secret view of a persisted API key -- backend-hardening
    addition (key-management REST endpoints), never includes the raw
    key or its hash, only what `APIKeyModel` itself already exposes for
    display purposes (see that model's own `key_prefix` rationale).
    `grants` is the key's COMPLETE grant list, unfiltered -- a caller
    that must not disclose a key's access to repositories it doesn't
    itself administer (e.g. an admin-role-gated REST endpoint) is
    responsible for filtering `.grants` down to its own visible set
    before returning it to a client; the functions below make no
    assumption about who is allowed to see what.
    """
    id: int
    name: str
    key_prefix: str
    created_at: datetime
    revoked_at: datetime | None
    grants: list[APIKeyGrant]


def get_api_key_repository_ids(storage: StorageManager, key_id: int) -> list[int] | None:
    """
    Returns the repository ids `key_id` is granted to (possibly empty,
    for an unscoped key), or `None` if no `APIKeyModel` with this id
    exists at all -- the `None`/non-`None` distinction is what lets a
    caller (an admin-role-gated REST endpoint) return a real 404
    separately from an authorization decision.
    """
    session = storage.get_session()
    try:
        api_key = session.query(APIKeyModel).filter(APIKeyModel.id == key_id).first()
        if api_key is None:
            return None
        return [
            row.repo_id
            for row in session.query(APIKeyRepositoryModel)
            .filter(APIKeyRepositoryModel.api_key_id == key_id)
            .all()
        ]
    finally:
        session.close()


def list_api_keys_for_repositories(storage: StorageManager, repository_ids: set[int]) -> list[APIKeySummary]:
    """
    Returns every API key that holds AT LEAST ONE grant among
    `repository_ids`, ordered by id. Each returned `APIKeySummary`
    carries its FULL grant list (see that class's own docstring for why
    filtering it down is the caller's job, not this function's).
    """
    if not repository_ids:
        return []
    session = storage.get_session()
    try:
        key_ids = {
            row.api_key_id
            for row in session.query(APIKeyRepositoryModel.api_key_id)
            .filter(APIKeyRepositoryModel.repo_id.in_(repository_ids))
            .all()
        }
        if not key_ids:
            return []
        keys = (
            session.query(APIKeyModel)
            .filter(APIKeyModel.id.in_(key_ids))
            .order_by(APIKeyModel.id)
            .all()
        )
        grants_by_key: dict[int, list[APIKeyGrant]] = {k.id: [] for k in keys}
        for row in (
            session.query(APIKeyRepositoryModel)
            .filter(APIKeyRepositoryModel.api_key_id.in_(key_ids))
            .all()
        ):
            grants_by_key[row.api_key_id].append(APIKeyGrant(repository_id=row.repo_id, role=row.role))
        return [
            APIKeySummary(
                id=k.id,
                name=k.name,
                key_prefix=k.key_prefix,
                created_at=k.created_at,
                revoked_at=k.revoked_at,
                grants=grants_by_key[k.id],
            )
            for k in keys
        ]
    finally:
        session.close()


def create_repository_owned_by(
    storage: StorageManager, repo_name: str, repo_url: str, api_key_id: int
) -> tuple[int, bool]:
    """
    Creates the repository row for `repo_url` AND the creating key's `admin`
    grant on it in ONE transaction, and returns `(repository_id, created)`.

    Owner semantics (CODEPULSE_FUTURE_CHANGES.md item 18, user-approved): a
    key that creates a new repository must be able to follow its own indexing
    job and see the result, which needs a grant on it. Only the call that
    actually CREATES the row gets the grant: if the repository already exists
    (including when another request created it a moment earlier and this one
    lost the race on the unique `url`), `created` is False and NO grant is
    written, so a second key never inherits admin on someone else's
    repository. Callers decide who may create (the `indexer`-somewhere rule
    lives at the endpoint); this function only makes creation and ownership
    atomic.
    """
    session = storage.get_session()
    try:
        existing = session.query(RepositoryModel).filter(RepositoryModel.url == repo_url).first()
        if existing is not None:
            return existing.id, False
        repo = RepositoryModel(name=repo_name, url=repo_url, file_count=0, symbol_count=0)
        session.add(repo)
        session.flush()
        session.add(APIKeyRepositoryModel(api_key_id=api_key_id, repo_id=repo.id, role="admin"))
        session.commit()
        return repo.id, True
    except IntegrityError:
        # Lost a race to create the same url: the other request owns it.
        session.rollback()
        existing = session.query(RepositoryModel).filter(RepositoryModel.url == repo_url).first()
        if existing is None:
            raise
        return existing.id, False
    finally:
        session.close()


def revoke_api_key(storage: StorageManager, key_id: int) -> APIKeySummary | None:
    """
    Sets `revoked_at` on the given key if it exists and is not already
    revoked -- the mutating revoke() flow this module's own docstring
    has disclosed as missing since Phase 1.1 (only the schema column
    existed; `verify_api_key` already respects it defensively, so a
    revoked key stops authenticating immediately once this runs).
    Idempotent: calling this again on an already-revoked key leaves its
    original `revoked_at` untouched rather than overwriting it with a
    later timestamp. Returns the key's up-to-date `APIKeySummary`
    (full, unfiltered grant list -- same caller responsibility as
    `list_api_keys_for_repositories`), or `None` if no key with this id
    exists.
    """
    session = storage.get_session()
    try:
        api_key = session.query(APIKeyModel).filter(APIKeyModel.id == key_id).first()
        if api_key is None:
            return None
        if api_key.revoked_at is None:
            api_key.revoked_at = datetime.utcnow()
            session.commit()
            session.refresh(api_key)
        grants = [
            APIKeyGrant(repository_id=row.repo_id, role=row.role)
            for row in session.query(APIKeyRepositoryModel)
            .filter(APIKeyRepositoryModel.api_key_id == key_id)
            .all()
        ]
        return APIKeySummary(
            id=api_key.id,
            name=api_key.name,
            key_prefix=api_key.key_prefix,
            created_at=api_key.created_at,
            revoked_at=api_key.revoked_at,
            grants=grants,
        )
    finally:
        session.close()


def verify_api_key(storage: StorageManager, raw_key: str) -> int | None:
    """
    Looks up the id of the APIKeyModel row matching `raw_key`, if any and
    only if it has not been revoked. Returns None for an unknown, wrong,
    or revoked key.

    This is a verification PRIMITIVE only -- proving the hash-and-lookup
    mechanism genuinely works -- not a FastAPI dependency, not wired into
    any endpoint or request path. That integration is Phase 1.2.
    """
    session = storage.get_session()
    try:
        api_key = (
            session.query(APIKeyModel).filter(APIKeyModel.key_hash == hash_api_key(raw_key)).first()
        )
        if api_key is None or api_key.revoked_at is not None:
            return None
        return api_key.id
    finally:
        session.close()
