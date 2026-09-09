"""add_index_job_model

Revision ID: de22df2fc3ce
Revises: b0bdd63949f4
Create Date: 2026-09-08 23:46:00.576010

Phase 1.3 (roadmap §37 item 1.3: "IndexJob state machine + /v1/jobs/{id}").

This migration is purely additive and non-destructive: it creates one new
table (`index_jobs`) and touches no existing table, column, or row. No
`codepulse.db` data is read, transformed, or migrated.

`index_jobs`: one row per indexing *run* (an event, not an idempotent
upsert-by-natural-key entity like every other table in this schema) --
`status` cycles through `queued -> running -> succeeded|failed|cancelled`
(see codepulse.storage.db.IndexJobModel's docstring). `result_summary`
holds a JSON-serialized copy of the real, measured result of
`IndexingEngine.index_directory()` once a job succeeds.

Since this is a brand new table, no pre-flight duplicate check is needed
(unlike prior phases' migrations that added constraints to existing,
possibly-already-populated tables) -- there is no pre-existing data that
could violate anything here.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'de22df2fc3ce'
down_revision: Union[str, Sequence[str], None] = 'b0bdd63949f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'index_jobs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('repo_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('result_summary', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['repo_id'], ['repositories.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_index_jobs_repo_id'), 'index_jobs', ['repo_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_index_jobs_repo_id'), table_name='index_jobs')
    op.drop_table('index_jobs')
