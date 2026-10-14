"""add_api_key_model

Revision ID: b0bdd63949f4
Revises: 49572f834366
Create Date: 2026-09-08 20:50:33.127639

Phase 1.1 (roadmap §37 item 1.1: "APIKey model + hashing + creation flow").

This migration is purely additive and non-destructive: it creates two new
tables (`api_keys`, `api_key_repositories`) and touches no existing table,
column, or row. No `codepulse.db` data is read, transformed, or migrated.

  - `api_keys`: one row per API key. Only `key_hash` (a SHA-256 hex digest)
    is stored, never the raw key -- see codepulse.auth.hash_api_key.
    `key_prefix` is a short, non-secret display value. `revoked_at` is
    schema support for a future revocation flow (not implemented in Phase
    1.1); it stays NULL for every key this milestone creates.
  - `api_key_repositories`: a (api_key_id, repository_id) grant table
    implementing "API keys ... scoped to one or more repositories"
    (roadmap §15). Phase 1.1 only writes to this table (via
    codepulse.auth.create_api_key) -- nothing reads/enforces it yet; that
    is Phase 1.2's central authorization dependency.

Since both tables are brand new, no pre-flight duplicate check is needed
(unlike prior phases' migrations, which added constraints to existing,
possibly-already-populated tables) -- there is no pre-existing data that
could violate either table's UNIQUE constraint.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b0bdd63949f4'
down_revision: Union[str, Sequence[str], None] = '49572f834366'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'api_keys',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('key_hash', sa.String(length=64), nullable=False),
        sa.Column('key_prefix', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('key_hash', name='uq_api_keys_key_hash'),
    )

    op.create_table(
        'api_key_repositories',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('api_key_id', sa.Integer(), nullable=False),
        sa.Column('repo_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['api_key_id'], ['api_keys.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['repo_id'], ['repositories.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('api_key_id', 'repo_id', name='uq_api_key_repositories_key_repo'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('api_key_repositories')
    op.drop_table('api_keys')
