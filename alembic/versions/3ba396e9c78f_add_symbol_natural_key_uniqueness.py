"""add_symbol_natural_key_uniqueness

Revision ID: 3ba396e9c78f
Revises: 20fc2d0607e2
Create Date: 2026-09-05 13:29:49.980342

Phase 0.4 (Symbol upsert by (file_id, name, start_line)).

This migration is additive and non-destructive:

  - Adds a UNIQUE constraint on `symbols(file_id, name, start_line)`,
    enforcing the natural-key invariant the Phase 0.4 upsert logic depends
    on (roadmap §8: "Symbol rows, upserted by (file_id, name, start_line)
    key"). A pre-flight check aborts the migration (rather than failing
    partway through a batch table-recreate, or silently accepting an
    inconsistent state) if any pre-existing duplicate rows would violate
    this constraint.

  - No column is added and no data is transformed -- `symbols.body_hash`
    already existed from the initial schema and is reused as-is for
    Phase 0.4's change-detection logic (no new hashing convention is
    introduced by this migration).

No table is dropped or recreated destructively and no existing row is
deleted, inserted, or overwritten. SQLAlchemy/Alembic "batch" mode is used
because SQLite requires a copy-and-swap under the hood to add a constraint
to an existing table -- all existing rows and columns are carried over
unchanged by that process.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3ba396e9c78f'
down_revision: Union[str, Sequence[str], None] = '20fc2d0607e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _assert_no_duplicates() -> None:
    """
    Pre-flight safety check: refuse to add the UNIQUE constraint if any
    pre-existing data would violate it, rather than letting the batch
    table-recreate fail halfway (or, on backends that don't error the same
    way, silently accepting an inconsistent state).
    """
    bind = op.get_bind()

    dup_symbols = bind.execute(
        sa.text(
            "SELECT file_id, name, start_line, COUNT(*) AS c FROM symbols "
            "GROUP BY file_id, name, start_line HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if dup_symbols:
        raise RuntimeError(
            "Cannot add UNIQUE constraint on symbols(file_id, name, "
            "start_line): duplicate rows already exist: "
            f"{[(row[0], row[1], row[2]) for row in dup_symbols]}. Resolve "
            "duplicate Symbol rows before applying this migration."
        )


def upgrade() -> None:
    """Upgrade schema."""
    _assert_no_duplicates()

    with op.batch_alter_table("symbols", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_symbols_file_id_name_start_line", ["file_id", "name", "start_line"]
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("symbols", schema=None) as batch_op:
        batch_op.drop_constraint("uq_symbols_file_id_name_start_line", type_="unique")
