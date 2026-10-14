"""add_file_content_hash_and_natural_key_uniqueness

Revision ID: 20fc2d0607e2
Revises: 21a7cb7f1a85
Create Date: 2026-09-05 12:55:39.151635

Phase 0.3 (Repository/File upsert with content_hash).

This migration is additive and non-destructive:

  - Adds `files.content_hash` (nullable). Existing File rows predate content
    hashing and only ever stored path/language/size/line-count metadata --
    the actual source bytes that were indexed were never persisted anywhere
    in the database (Symbol.body only stores per-symbol snippets, not whole
    files), so there is no reliable, non-fabricated way to compute a
    historically-accurate hash for pre-existing rows during this migration.
    Per the Phase 0.3 spec, we do NOT invent/backfill a value for them --
    `content_hash` is left NULL for rows that predate this migration. Each
    such row gets a real, accurate content_hash automatically the next time
    its file is re-indexed by the application (see
    IndexingEngine._persist_to_db), at which point the current on-disk
    content is genuinely available to hash.

  - Adds a UNIQUE constraint on `files(repo_id, path)` and on
    `repositories(url)`, enforcing the natural-key invariants the Phase 0.3
    upsert logic depends on. A pre-flight check aborts the migration (rather
    than failing partway through a batch table-recreate, or silently
    accepting an inconsistent state) if any pre-existing duplicate rows
    would violate these constraints.

No table is dropped or recreated destructively and no existing row is
deleted or overwritten. SQLAlchemy/Alembic "batch" mode is used because
SQLite requires a copy-and-swap under the hood to add a constraint to an
existing table -- all existing rows and columns are carried over unchanged
by that process.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '20fc2d0607e2'
down_revision: Union[str, Sequence[str], None] = '21a7cb7f1a85'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _assert_no_duplicates() -> None:
    """
    Pre-flight safety check: refuse to add the UNIQUE constraints if any
    pre-existing data would violate them, rather than letting the batch
    table-recreate fail halfway (or, on backends that don't error the same
    way, silently accepting an inconsistent state).
    """
    bind = op.get_bind()

    dup_repo_urls = bind.execute(
        sa.text(
            "SELECT url, COUNT(*) AS c FROM repositories "
            "GROUP BY url HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if dup_repo_urls:
        raise RuntimeError(
            "Cannot add UNIQUE constraint on repositories.url: duplicate "
            f"url values already exist: {[row[0] for row in dup_repo_urls]}. "
            "Resolve duplicate Repository rows before applying this migration."
        )

    dup_files = bind.execute(
        sa.text(
            "SELECT repo_id, path, COUNT(*) AS c FROM files "
            "GROUP BY repo_id, path HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if dup_files:
        raise RuntimeError(
            "Cannot add UNIQUE constraint on files(repo_id, path): duplicate "
            f"(repo_id, path) pairs already exist: "
            f"{[(row[0], row[1]) for row in dup_files]}. Resolve duplicate "
            "File rows before applying this migration."
        )


def upgrade() -> None:
    """Upgrade schema."""
    _assert_no_duplicates()

    with op.batch_alter_table("files", schema=None) as batch_op:
        batch_op.add_column(sa.Column("content_hash", sa.String(length=64), nullable=True))
        batch_op.create_unique_constraint("uq_files_repo_id_path", ["repo_id", "path"])

    with op.batch_alter_table("repositories", schema=None) as batch_op:
        batch_op.create_unique_constraint("uq_repositories_url", ["url"])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("repositories", schema=None) as batch_op:
        batch_op.drop_constraint("uq_repositories_url", type_="unique")

    with op.batch_alter_table("files", schema=None) as batch_op:
        batch_op.drop_constraint("uq_files_repo_id_path", type_="unique")
        batch_op.drop_column("content_hash")
