"""initial_schema

Revision ID: 21a7cb7f1a85
Revises: 
Create Date: 2026-09-01 23:26:31.067744

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '21a7cb7f1a85'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Repositories
    op.create_table(
        'repositories',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('team_id', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('branch', sa.String(length=100), nullable=True, server_default='main'),
        sa.Column('last_sync', sa.DateTime(), nullable=True),
        sa.Column('file_count', sa.Integer(), nullable=True, server_default='0'),
        sa.Column('symbol_count', sa.Integer(), nullable=True, server_default='0'),
        sa.PrimaryKeyConstraint('id'),
    )

    # 2. Files
    op.create_table(
        'files',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('repo_id', sa.Integer(), nullable=False),
        sa.Column('path', sa.Text(), nullable=False),
        sa.Column('language', sa.String(length=50), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=True, server_default='0'),
        sa.Column('line_count', sa.Integer(), nullable=True, server_default='0'),
        sa.Column('last_indexed', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['repo_id'], ['repositories.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )

    # 3. Symbols
    op.create_table(
        'symbols',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('file_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('type', sa.String(length=50), nullable=False),
        sa.Column('start_line', sa.Integer(), nullable=False),
        sa.Column('end_line', sa.Integer(), nullable=False),
        sa.Column('signature', sa.Text(), nullable=True, server_default=''),
        sa.Column('docstring', sa.Text(), nullable=True),
        sa.Column('body', sa.Text(), nullable=True, server_default=''),
        sa.Column('body_hash', sa.String(length=64), nullable=True, server_default=''),
        sa.Column('language', sa.String(length=50), nullable=False),
        sa.Column('complexity', sa.Integer(), nullable=True, server_default='1'),
        sa.ForeignKeyConstraint(['file_id'], ['files.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_symbols_name', 'symbols', ['name'], unique=False)

    # 4. Dependencies
    op.create_table(
        'dependencies',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('source_symbol_name', sa.String(length=255), nullable=False),
        sa.Column('source_file', sa.Text(), nullable=False),
        sa.Column('target_symbol_name', sa.String(length=255), nullable=False),
        sa.Column('target_file', sa.Text(), nullable=True),
        sa.Column('call_count', sa.Integer(), nullable=True, server_default='1'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_dependencies_source_symbol_name', 'dependencies', ['source_symbol_name'], unique=False)
    op.create_index('ix_dependencies_target_symbol_name', 'dependencies', ['target_symbol_name'], unique=False)

    # 5. Code Embeddings
    op.create_table(
        'code_embeddings',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('symbol_id', sa.Integer(), nullable=False),
        sa.Column('chunk_type', sa.String(length=50), nullable=True, server_default='signature_doc'),
        sa.Column('embedding_json', sa.Text(), nullable=False),
        sa.Column('chunk_content', sa.Text(), nullable=True, server_default=''),
        sa.ForeignKeyConstraint(['symbol_id'], ['symbols.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('code_embeddings')
    op.drop_index('ix_dependencies_target_symbol_name', table_name='dependencies')
    op.drop_index('ix_dependencies_source_symbol_name', table_name='dependencies')
    op.drop_table('dependencies')
    op.drop_index('ix_symbols_name', table_name='symbols')
    op.drop_table('symbols')
    op.drop_table('files')
    op.drop_table('repositories')
