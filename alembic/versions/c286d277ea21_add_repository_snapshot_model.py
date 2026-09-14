"""add_repository_snapshot_model

Revision ID: c286d277ea21
Revises: de22df2fc3ce
Create Date: 2026-09-14 20:00:00.000000

Phase 3.1 (roadmap §37 item 3.1: "Git-diff change detection
(added/modified/deleted)"; §29 Data Model, which explicitly names
`RepositorySnapshot` as required now: "Required for incremental indexing
(§9) and crash-safe reads (§17)").

This migration is purely additive and non-destructive: it creates one new
table (`repository_snapshots`) and touches no existing table, column, or
row. No `codepulse.db` data is read, transformed, or migrated.

`repository_snapshots`: one row per successful indexing run (a
run-history table, like `index_jobs` -- no natural-key `UniqueConstraint`)
-- `commit_sha` records the git commit the repository's content was at
(`NULL` for a non-git/local-path repository), `embedding_model`/
`embedding_dim` record what embedding configuration was used. See
codepulse.storage.db.RepositorySnapshotModel's own docstring for the full
design rationale, including what this milestone deliberately does NOT
implement (§17's full crash-safe status-gated read semantics).

Since this is a brand new table, no pre-flight duplicate check is needed
(unlike prior phases' migrations that added constraints to existing,
possibly-already-populated tables) -- there is no pre-existing data that
could violate anything here.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c286d277ea21'
down_revision: Union[str, Sequence[str], None] = 'de22df2fc3ce'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'repository_snapshots',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('repo_id', sa.Integer(), nullable=False),
        sa.Column('commit_sha', sa.String(length=64), nullable=True),
        sa.Column('embedding_model', sa.String(length=255), nullable=True),
        sa.Column('embedding_dim', sa.Integer(), nullable=True),
        sa.Column('indexed_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['repo_id'], ['repositories.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_repository_snapshots_repo_id'), 'repository_snapshots', ['repo_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_repository_snapshots_repo_id'), table_name='repository_snapshots')
    op.drop_table('repository_snapshots')
