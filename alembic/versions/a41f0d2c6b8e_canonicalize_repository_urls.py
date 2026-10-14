"""canonicalize_repository_urls

Revision ID: a41f0d2c6b8e
Revises: 7c3e5a91d2f4
Create Date: 2026-10-14 00:00:00.000000

Repository identity (CODEPULSE_FUTURE_CHANGES.md item 2; user-approved).

A repository was identified by the exact string it was first indexed under, so
the same directory spelled with backslashes vs forward slashes, a trailing
separator, `..` segments or a lowercase vs uppercase Windows drive letter became
two repositories. From this revision on the application stores and compares ONE
canonical form (see `codepulse.repository_identity`); this migration brings the
rows that already exist into that form.

DATA-ONLY and non-destructive: no table, column or constraint changes, nothing is
deleted and nothing is merged.
  - A row whose `url` is a LOCAL PATH is rewritten to its canonical form
    (absolute, `..` resolved, symlinks resolved where the path exists, uppercase
    drive letter on Windows, no trailing separator).
  - Anything that is not a local path (`https://...`, `ssh://...`, `file:///...`)
    is left exactly as it is.
  - If another row already holds the canonical form -- a DUPLICATE that predates
    this fix -- the row is left unchanged and the pair is logged as a warning.
    The two rows each own their own files, symbols, embeddings, jobs and API-key
    grants, so merging them is a decision for a person, not a migration. The
    application still finds the lowest-id row for either spelling.

The canonicalization is written out here (not imported) so this migration keeps
doing what it did on the day it was written even if the application's rule later
changes. If this runs on a machine where the paths do not exist (a restored
backup), `realpath` cannot resolve symlinks or fix the case of components, so the
result is the normalized absolute path with an uppercase drive letter; rows are
only ever rewritten to a form no other row holds.

Downgrade is a no-op: the original spellings were not recorded and cannot be
restored, and the canonical form is valid under the previous schema too.
"""
import logging
import os
import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a41f0d2c6b8e'
down_revision: Union[str, Sequence[str], None] = '7c3e5a91d2f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


def _canonical(url: str) -> str:
    if not url or _URL_SCHEME.match(url):
        return url
    resolved = os.path.realpath(os.path.abspath(url))
    if os.name == "nt" and len(resolved) >= 2 and resolved[1] == ":":
        resolved = resolved[0].upper() + resolved[1:]
    return resolved


def upgrade() -> None:
    """Upgrade data."""
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, url FROM repositories ORDER BY id")).fetchall()
    taken = {url: repo_id for repo_id, url in rows}
    rewritten = 0
    for repo_id, url in rows:
        canonical = _canonical(url)
        if canonical == url:
            continue
        holder = taken.get(canonical)
        if holder is not None and holder != repo_id:
            logger.warning(
                "repository %s (%r) and repository %s both name %r; left unchanged, merge them by hand if they are the same project",
                repo_id, url, holder, canonical,
            )
            continue
        bind.execute(sa.text("UPDATE repositories SET url = :u WHERE id = :i"), {"u": canonical, "i": repo_id})
        del taken[url]
        taken[canonical] = repo_id
        rewritten += 1
    if rewritten:
        logger.info("canonicalized %d repository url(s)", rewritten)


def downgrade() -> None:
    """Downgrade: nothing to undo (the original spellings were not recorded)."""
