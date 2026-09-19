"""add_base_classes_and_relationship_type

Revision ID: 135a7b064ea7
Revises: 2cd38eb12a7e
Create Date: 2026-09-19 00:00:00.000000

Phase 4.1 (roadmap §37 item 4.1: "Base-class/inheritance extraction
(Python + JS/TS)"; §29 Data Model: "Symbol | Yes (exists, needs upsert
key + base-class field) | Core"). Architecture approved explicitly by
the user before implementation (see CODEPULSE_PROGRESS.md): raw
base-class text lives on `Symbol`/`symbols`; resolved inheritance
relationships are persisted as `CallEdge`/`call_edges` rows,
distinguished from ordinary call edges by a new `relationship_type`
column, reusing that table's existing `repo_id` scoping, `confidence`
vocabulary, and Phase 3.5 incremental-update machinery rather than
introducing a second persistence mechanism.

This migration is purely additive and non-destructive:

  1. Adds `symbols.base_classes` (nullable `TEXT`, JSON-serialized
     `list[str]`, the same "Text column holding a JSON-serialized list,
     parsed in application code" convention `EmbeddingModel.
     embedding_json` already uses -- not a new serialization approach).
     `NULL` for every existing row (none of them were ever asked for
     this data before this migration existed -- not a guess, an honest
     absence, exactly like `confidence`'s own precedent in
     `f6e481353553`) and for every non-CLASS symbol going forward.
     Every existing row's `id`/`name`/`start_line`/`body_hash`/etc. is
     untouched.

  2. Adds `call_edges.relationship_type` (`VARCHAR(20)`, `NOT NULL`,
     `DEFAULT 'calls'`). Every one of this table's existing rows is
     UNAMBIGUOUSLY a call -- nothing else has ever been written to it
     (confirmed: no code path existed to write anything else before
     this migration) -- so `'calls'` is the correct backfill for every
     existing row, not a guess in the way `confidence`'s or `repo_id`'s
     own backfills had to be conservative about ambiguity. SQLite
     applies the column default to every existing row automatically
     when a `NOT NULL DEFAULT` column is added.

Both additions use Alembic "batch" mode (SQLite table-rebuild-under-
the-hood), matching every prior column-addition migration in this
project's history, even though a plain `ALTER TABLE ADD COLUMN` with a
constant default would likely work directly here -- consistency with
established migration style, not a functional requirement.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '135a7b064ea7'
down_revision: Union[str, Sequence[str], None] = '2cd38eb12a7e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('symbols', schema=None) as batch_op:
        batch_op.add_column(sa.Column('base_classes', sa.Text(), nullable=True))

    with op.batch_alter_table('call_edges', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'relationship_type',
                sa.String(length=20),
                nullable=False,
                server_default='calls',
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('call_edges', schema=None) as batch_op:
        batch_op.drop_column('relationship_type')

    with op.batch_alter_table('symbols', schema=None) as batch_op:
        batch_op.drop_column('base_classes')
