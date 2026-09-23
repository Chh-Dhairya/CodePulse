"""add_api_key_repository_role

Revision ID: b9014c049e33
Revises: 135a7b064ea7
Create Date: 2026-09-23 00:00:00.000000

Phase 7.2 (roadmap §37 item 7.2: "RBAC roles + repo permission model";
§15's literal minimal-viable model: "roles `viewer` (search/read),
`indexer` (viewer + trigger indexing), `admin` (all + manage keys/
repos). Enforced at the repository-permission level, not globally.").
Architecture: `role` lives on `api_key_repositories`
(`APIKeyRepositoryModel`), the per-(key, repository) grant table --
not on `api_keys` itself -- matching "not globally" literally. See
`APIKeyRepositoryModel`'s own docstring for the full reasoning.

This migration is purely additive and non-destructive:

  Adds `api_key_repositories.role` (`VARCHAR(20)`, `NOT NULL`,
  `DEFAULT 'admin'`). Every existing grant row, before this migration,
  had UNRESTRICTED access to every endpoint that checked repository
  membership alone (no role concept existed to restrict it) --
  backfilling every existing row to `'admin'` reflects that row's
  actual historical capability honestly, not a guess (there is only
  one historically-correct answer here, unlike an ambiguous backfill
  elsewhere in this project's history). `codepulse.auth.
  create_api_key`'s own default for grants created AFTER this
  migration is intentionally the safer `'viewer'` (least privilege) --
  a forward-looking application-level default, not a migration
  concern. SQLite applies the column default to every existing row
  automatically when a `NOT NULL DEFAULT` column is added.

Uses Alembic "batch" mode (SQLite table-rebuild-under-the-hood),
matching every prior column-addition migration in this project's
history.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b9014c049e33'
down_revision: Union[str, Sequence[str], None] = '135a7b064ea7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('api_key_repositories', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'role',
                sa.String(length=20),
                nullable=False,
                server_default='admin',
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('api_key_repositories', schema=None) as batch_op:
        batch_op.drop_column('role')
