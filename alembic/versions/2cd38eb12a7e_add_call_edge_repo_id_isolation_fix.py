"""add_call_edge_repo_id_isolation_fix

Revision ID: 2cd38eb12a7e
Revises: f6e481353553
Create Date: 2026-09-16 00:00:00.000000

Repository-isolation fix (not a named roadmap sprint item -- a real,
confirmed correctness/security bug found and disclosed by a Phase 3.4
pre-commit audit, fixed on the user's explicit instruction before
starting Phase 3.5).

`call_edges` (né `dependencies`/`DependencyModel`) has never carried a
repository association, since its Phase 0.4 introduction. `GET
/v1/symbols/{id}` queries it by symbol name alone -- confirmed LIVE, in
this session, that a caller authorized for one repository can receive
another, unrelated repository's call-graph edges (symbol name + file
path) whenever symbol names happen to collide across repositories. This
directly violates the roadmap's "Repository isolation" permanent
invariant (CLAUDE.md §4) and §15/§30's "every query... filtered by
repository_id" rule.

This migration:

  1. Adds a new, nullable `repo_id` column (FK to `repositories.id`,
     `ondelete='CASCADE'`, indexed -- the exact same pattern every other
     `repo_id` column in this schema already uses), via batch mode
     (SQLite requires a table rebuild to add a real FK constraint after
     the fact, the same reason `20fc2d0607e2` used batch mode to add a
     constraint to an existing table).
  2. Backfills existing rows conservatively and HONESTLY -- never a
     guess (CLAUDE.md: no fabricated metrics). For each distinct
     `source_file` value already present in `call_edges`, this looks up
     every `files.path` row that matches it: if exactly ONE distinct
     `repo_id` results (the common case -- most databases have only one,
     or a small number of, repositories, and most file paths are not
     duplicated verbatim across them), every matching row is backfilled
     with that `repo_id`. If the match is AMBIGUOUS (two or more
     repositories happen to have a file at that exact same relative
     path) or there is NO match at all (the file was since renamed,
     moved, or deleted), the row is left with `repo_id = NULL` rather
     than guessing -- a `NULL`-`repo_id` row is simply invisible to every
     repository-scoped query from now on (a real, disclosed, one-time
     completeness gap for that specific historical edge, not a security
     problem -- the opposite of the bug being fixed), until its
     repository is next re-indexed and persists a fresh, correctly
     `repo_id`-tagged replacement edge.

Non-destructive: no existing row is deleted, and no existing column's
value is changed. `IndexingEngine._persist_to_db` now sets `repo_id` on
every NEW `CallEdge` row going forward (see that method's own comment) --
this migration only concerns rows that already existed before this fix.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2cd38eb12a7e'
down_revision: Union[str, Sequence[str], None] = 'f6e481353553'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('call_edges', schema=None) as batch_op:
        batch_op.add_column(sa.Column('repo_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_call_edges_repo_id_repositories', 'repositories', ['repo_id'], ['id'], ondelete='CASCADE'
        )
    op.create_index(op.f('ix_call_edges_repo_id'), 'call_edges', ['repo_id'], unique=False)

    _backfill_repo_id()


def _backfill_repo_id() -> None:
    """
    Conservative, honest backfill -- see this module's own docstring for
    the exact rule. Only ever sets `repo_id` when exactly one repository
    can be identified for a given `source_file` value; leaves it `NULL`
    otherwise (ambiguous match, or no match at all).
    """
    bind = op.get_bind()

    distinct_files = bind.execute(sa.text("SELECT DISTINCT source_file FROM call_edges")).fetchall()
    for (source_file,) in distinct_files:
        matching_repo_ids = bind.execute(
            sa.text("SELECT DISTINCT repo_id FROM files WHERE path = :p"),
            {"p": source_file},
        ).fetchall()
        if len(matching_repo_ids) == 1:
            bind.execute(
                sa.text(
                    "UPDATE call_edges SET repo_id = :r "
                    "WHERE source_file = :p AND repo_id IS NULL"
                ),
                {"r": matching_repo_ids[0][0], "p": source_file},
            )
        # else: 0 matches (file since renamed/moved/deleted) or 2+
        # distinct repo_id matches (the same relative path exists in
        # more than one repository -- genuinely ambiguous) -- leave
        # NULL, honestly, rather than guessing.


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_call_edges_repo_id'), table_name='call_edges')
    with op.batch_alter_table('call_edges', schema=None) as batch_op:
        batch_op.drop_constraint('fk_call_edges_repo_id_repositories', type_='foreignkey')
        batch_op.drop_column('repo_id')
