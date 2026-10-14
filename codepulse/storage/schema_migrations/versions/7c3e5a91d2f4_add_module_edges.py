"""add_module_edges

Revision ID: 7c3e5a91d2f4
Revises: b9014c049e33
Create Date: 2026-10-12 00:00:00.000000

Persists the file-level import graph so module structure survives a
restart (CODEPULSE_FUTURE_CHANGES.md item 15; user-approved change to
Phase 4.3's original "no persistence" scope).

`GET /v1/repositories/{id}/modules`, the CLI `modules` command, the MCP
`module_structure` tool and the VS Code Module Structure view all read
`IndexingEngine.analyze_module_structure`, which built its graph from
`parse_results`, an in-memory cache. After any server restart module
structure was therefore empty (0 clusters) until the repository was
re-indexed, because imports were not stored anywhere. The call graph
(`call_edges`) and the symbol map (`symbols`/`files`) could be restored from
existing tables; this one could not.

This migration is purely additive and non-destructive: it only CREATES
`module_edges` (one row per resolved source-file -> target-file import edge,
plus one row with a NULL `target_file` for each file that has no resolved
imports, so the node set is stored too; `repo_id` NOT NULL with ON DELETE
CASCADE, unique per (repo_id, source_file, target_file)). No existing table,
column or row is touched, so there is no pre-existing-data failure mode and
no backfill: a repository's rows are written the next time it is indexed
(until then its module structure behaves exactly as it did before this
migration -- cold after a restart, never a misleading partial graph).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c3e5a91d2f4'
down_revision: Union[str, Sequence[str], None] = 'b9014c049e33'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'module_edges',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('repo_id', sa.Integer(), nullable=False),
        sa.Column('source_file', sa.Text(), nullable=False),
        sa.Column('target_file', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['repo_id'], ['repositories.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('repo_id', 'source_file', 'target_file', name='uq_module_edges_repo_source_target'),
    )
    op.create_index(op.f('ix_module_edges_repo_id'), 'module_edges', ['repo_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_module_edges_repo_id'), table_name='module_edges')
    op.drop_table('module_edges')
