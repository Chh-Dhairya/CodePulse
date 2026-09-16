"""rename_dependency_to_call_edge_add_confidence

Revision ID: f6e481353553
Revises: c286d277ea21
Create Date: 2026-09-15 00:00:00.000000

Phase 3.4 (roadmap §37 item 3.4: "CallEdge.confidence scoring"; §11:
"every `CallEdge` should carry a `confidence` field — `high` when
resolved through an explicit import mapping to a known file+symbol,
`low` when resolved only by bare-name match against same-file symbols,
`unresolved` when the callee couldn't be matched to any known symbol at
all"; §29 Data Model: "`CallEdge` | Yes (rename from `DependencyModel`,
add `confidence`) | §11").

This migration:

  1. Renames the `dependencies` table to `call_edges` (matching the
     roadmap's own target entity name), and renames its two existing
     indexes to match (`ix_dependencies_*` -> `ix_call_edges_*`, the
     exact names SQLAlchemy's `index=True` would auto-generate for a
     model actually named `call_edges` -- kept in sync with what a
     brand-new database created from today's models would produce).
  2. Adds a new, nullable `confidence` column.

Non-destructive: every existing row's `id`, `source_symbol_name`,
`source_file`, `target_symbol_name`, `target_file`, and `call_count`
value is preserved exactly (a table rename changes no row data at all).

`confidence` is deliberately left NULL for pre-existing rows, not
backfilled with a guessed value -- CLAUDE.md's "no fabricated metrics"
rule applies here: this migration has no way to honestly reconstruct
which resolution path (import-mapped, same-file bare-name, or
unresolved) produced a historical row, since that information was
never persisted before this migration existed. Each such row gets a
real, accurate `confidence` automatically the next time its repository
is re-indexed (see `codepulse.parser.graph.CallGraphBuilder._build_graph`
and `IndexingEngine._persist_to_db`), at which point the resolution
decision is freshly recomputed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f6e481353553'
down_revision: Union[str, Sequence[str], None] = 'c286d277ea21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.rename_table('dependencies', 'call_edges')
    op.drop_index('ix_dependencies_source_symbol_name', table_name='call_edges')
    op.drop_index('ix_dependencies_target_symbol_name', table_name='call_edges')
    op.create_index(
        op.f('ix_call_edges_source_symbol_name'), 'call_edges', ['source_symbol_name'], unique=False
    )
    op.create_index(
        op.f('ix_call_edges_target_symbol_name'), 'call_edges', ['target_symbol_name'], unique=False
    )
    with op.batch_alter_table('call_edges', schema=None) as batch_op:
        batch_op.add_column(sa.Column('confidence', sa.String(length=20), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('call_edges', schema=None) as batch_op:
        batch_op.drop_column('confidence')
    op.drop_index(op.f('ix_call_edges_source_symbol_name'), table_name='call_edges')
    op.drop_index(op.f('ix_call_edges_target_symbol_name'), table_name='call_edges')
    op.create_index(
        'ix_dependencies_source_symbol_name', 'call_edges', ['source_symbol_name'], unique=False
    )
    op.create_index(
        'ix_dependencies_target_symbol_name', 'call_edges', ['target_symbol_name'], unique=False
    )
    op.rename_table('call_edges', 'dependencies')
