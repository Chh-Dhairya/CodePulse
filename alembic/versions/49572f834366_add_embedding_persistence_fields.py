"""add_embedding_persistence_fields

Revision ID: 49572f834366
Revises: 3ba396e9c78f
Create Date: 2026-09-05 14:19:04.742535

Phase 0.5 (Embedding persistence + hydration).

This migration is additive and non-destructive:

  - Adds `code_embeddings.chunk_index` (NOT NULL, default 0), completing the
    natural key `(symbol_id, chunk_type, chunk_index)` specified by the
    roadmap's Phase 0 idempotency rule (§8). Every pre-existing row
    implicitly becomes chunk_index=0 (there was no concept of multiple
    indexed chunks per (symbol, chunk_type) before this migration).
  - Adds `code_embeddings.content_hash` (nullable) for change detection --
    left NULL for pre-existing rows, for the same reason `files.content_hash`
    was left NULL in the Phase 0.3 migration: the exact chunk text that
    produced a historical row was never separately hashed, so no
    historically-accurate hash can be reconstructed without fabricating one.
    Each such row gets a real hash the next time its symbol is re-indexed.
  - Adds `code_embeddings.embedding_model` and `code_embeddings.embedding_dim`
    (both nullable), recording which model/dimension produced a vector so
    hydration can detect a model or dimension change and skip loading an
    incompatible vector rather than silently corrupting the in-memory
    vector index.
  - Adds a UNIQUE constraint on `code_embeddings(symbol_id, chunk_type,
    chunk_index)`. A pre-flight check aborts the migration (rather than
    failing partway through a batch table-recreate, or silently accepting
    an inconsistent state) if any pre-existing (symbol_id, chunk_type) pair
    already has more than one row -- such rows would collide once they all
    default to chunk_index=0.

No table is dropped or recreated destructively and no existing row is
deleted or has its embedding_json/chunk_content overwritten. SQLAlchemy/
Alembic "batch" mode is used because SQLite requires a copy-and-swap under
the hood to add columns and a constraint to an existing table -- all
existing rows are carried over unchanged by that process.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '49572f834366'
down_revision: Union[str, Sequence[str], None] = '3ba396e9c78f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _assert_no_duplicates() -> None:
    """
    Pre-flight safety check: refuse to add the UNIQUE constraint if any
    pre-existing data would violate it once every row defaults to
    chunk_index=0, rather than letting the batch table-recreate fail
    halfway (or, on backends that don't error the same way, silently
    accepting an inconsistent state).
    """
    bind = op.get_bind()

    dup = bind.execute(
        sa.text(
            "SELECT symbol_id, chunk_type, COUNT(*) AS c FROM code_embeddings "
            "GROUP BY symbol_id, chunk_type HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if dup:
        raise RuntimeError(
            "Cannot add UNIQUE constraint on code_embeddings(symbol_id, "
            "chunk_type, chunk_index): duplicate (symbol_id, chunk_type) "
            "pairs already exist, and would collide once defaulted to "
            f"chunk_index=0: {[(row[0], row[1]) for row in dup]}. Resolve "
            "duplicate embedding rows before applying this migration."
        )


def upgrade() -> None:
    """Upgrade schema."""
    _assert_no_duplicates()

    with op.batch_alter_table("code_embeddings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("chunk_index", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("content_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("embedding_model", sa.String(length=150), nullable=True))
        batch_op.add_column(sa.Column("embedding_dim", sa.Integer(), nullable=True))
        batch_op.create_unique_constraint(
            "uq_code_embeddings_symbol_chunk", ["symbol_id", "chunk_type", "chunk_index"]
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("code_embeddings", schema=None) as batch_op:
        batch_op.drop_constraint("uq_code_embeddings_symbol_chunk", type_="unique")
        batch_op.drop_column("embedding_dim")
        batch_op.drop_column("embedding_model")
        batch_op.drop_column("content_hash")
        batch_op.drop_column("chunk_index")
