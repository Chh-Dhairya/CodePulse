"""
One repository, one identity, however its path is spelled
(CODEPULSE_FUTURE_CHANGES.md item 2; user-approved 2026-10-14).

A repository is identified by `RepositoryModel.url` (unique). For a repository
indexed from a local directory that string was whatever the caller typed, so the
same directory spelled with backslashes vs forward slashes, a trailing
separator, `..` segments, or a lowercase vs uppercase Windows drive letter
(VS Code reports `c:\\x`, a CLI index stores `C:\\x`) became two repositories.
Re-indexing then created a duplicate with a new id and, because a differently
spelled path looked brand-new to the "re-indexing needs `indexer`" check, a
viewer-only key could bypass it.

`canonical_repository_url` is the one definition of identity:
  - a LOCAL PATH becomes absolute, with symlinks and `..` resolved
    (`os.path.realpath`, which on Windows also returns the true case of every
    component and normalizes slashes), an uppercase drive letter on Windows (also
    for a path that no longer exists, where realpath cannot fix the case), and no
    trailing separator;
  - anything that is not a local path -- a git URL, `https://...`, the synthetic
    `file:///...` ids some tests use -- is returned exactly as given.

`find_repository` is the one lookup: it matches the canonical form first, then
any row stored under another spelling (a database not yet migrated, or a
duplicate that predates this fix), so a lookup never creates a duplicate just
because an old row has an old spelling. When several rows match it returns the
lowest id, deterministically; it never merges or deletes anything.

Limit: identity follows the filesystem. A mapped network drive resolves to its
UNC path while the mapping exists; a path on a filesystem that is
case-insensitive but where `realpath` cannot report the true case (a deleted
directory, macOS) can still be spelled in more than one way.
"""
from __future__ import annotations

import os
import re

from sqlalchemy.orm import Session

from codepulse.storage.db import RepositoryModel

# `scheme://...` is a URL, not a path. A Windows drive (`C:\\x`, `C:/x`) has no `//` right after the colon.
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


def is_local_path(url: str) -> bool:
    return bool(url) and not _URL_SCHEME.match(url)


def canonical_repository_url(url: str) -> str:
    """The one canonical identity for `url`. Non-paths are returned unchanged; see the module docstring."""
    if not is_local_path(url):
        return url
    resolved = os.path.realpath(os.path.abspath(url))
    if os.name == "nt" and len(resolved) >= 2 and resolved[1] == ":":
        resolved = resolved[0].upper() + resolved[1:]
    return resolved


def identity_key(url: str) -> str:
    """The canonical form, case-folded where the filesystem is case-insensitive, for comparing two spellings."""
    return os.path.normcase(canonical_repository_url(url)) if is_local_path(url) else url


def find_repository(session: Session, url: str) -> RepositoryModel | None:
    """
    The repository `url` names, under any spelling of the same directory, or None.
    Prefers an exact canonical row, then an exact row, then any row whose canonical
    identity matches (lowest id first).
    """
    canonical = canonical_repository_url(url)
    exact = session.query(RepositoryModel).filter(RepositoryModel.url == canonical).first()
    if exact is not None or not is_local_path(url):
        return exact
    raw = session.query(RepositoryModel).filter(RepositoryModel.url == url).first()
    if raw is not None:
        return raw
    wanted = identity_key(url)
    for row in session.query(RepositoryModel).order_by(RepositoryModel.id).all():
        if is_local_path(row.url) and identity_key(row.url) == wanted:
            return row
    return None
