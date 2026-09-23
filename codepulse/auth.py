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
now accepts a `role` per repository grant, see its own docstring), a
mutating revoke() flow (only the `revoked_at` schema column exists),
key expiry, audit logging.

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
