from __future__ import annotations

import os
import tempfile
from pathlib import Path
import pytest
from sqlalchemy import create_engine, inspect, text

from codepulse.config import CodePulseConfig, set_settings, reset_settings
from codepulse.storage.db import (
    Base,
    RepositoryModel,
    FileModel,
    SymbolModel,
    DependencyModel,
    EmbeddingModel,
    StorageManager,
)
from codepulse.storage.migrations import (
    downgrade_migrations,
    get_alembic_config,
    get_current_revision,
    run_migrations,
    stamp_database,
)


@pytest.fixture(autouse=True)
def cleanup():
    reset_settings()
    yield
    reset_settings()


def test_alembic_config_loads():
    """Verify that Alembic configuration loads with proper settings and script location."""
    cfg = get_alembic_config()
    assert cfg is not None
    assert cfg.get_main_option("script_location") is not None
    assert "alembic" in cfg.get_main_option("script_location")


def test_fresh_database_migration_scenario_1():
    """
    SCENARIO 1 — Fresh database:
    1. Start from a fresh empty database.
    2. Run Alembic upgrade to head.
    3. Verify all expected tables and indexes exist.
    4. Verify application can connect and perform queries.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_db_path = f.name

    if os.path.exists(temp_db_path):
        os.remove(temp_db_path)

    db_url = f"sqlite:///{temp_db_path.replace(os.sep, '/')}"

    engine = None
    storage = None

    try:
        # 1. Run migration on fresh DB
        run_migrations(db_url=db_url, revision="head")

        # 2. Inspect created schema
        engine = create_engine(db_url)
        insp = inspect(engine)
        table_names = set(insp.get_table_names())

        expected_tables = {
            "alembic_version",
            "repositories",
            "files",
            "symbols",
            "dependencies",
            "code_embeddings",
        }
        assert expected_tables.issubset(table_names), f"Missing tables: {expected_tables - table_names}"

        # Verify revision recorded
        current_rev = get_current_revision(db_url)
        assert current_rev == "21a7cb7f1a85"

        # Verify columns on symbols table
        symbol_cols = {col["name"]: col for col in insp.get_columns("symbols")}
        assert "name" in symbol_cols
        assert "file_id" in symbol_cols
        assert "start_line" in symbol_cols
        assert "end_line" in symbol_cols
        assert "body_hash" in symbol_cols

        # Verify indexes
        symbol_indexes = [idx["name"] for idx in insp.get_indexes("symbols")]
        assert "ix_symbols_name" in symbol_indexes

        # 3. Verify application connects and operates on migrated database
        storage = StorageManager(db_url=db_url)
        session = storage.get_session()
        try:
            repo = RepositoryModel(name="test_repo", url="https://github.com/example/test")
            session.add(repo)
            session.commit()

            fetched = session.query(RepositoryModel).filter_by(name="test_repo").first()
            assert fetched is not None
            assert fetched.id == 1
        finally:
            session.close()

        # 4. Verify downgrade drops tables
        downgrade_migrations(db_url=db_url, revision="base")
        insp_after_downgrade = inspect(engine)
        tables_after_downgrade = set(insp_after_downgrade.get_table_names()) - {"alembic_version"}
        assert len(tables_after_downgrade) == 0

    finally:
        if storage is not None and hasattr(storage, "engine"):
            storage.engine.dispose()
        if engine is not None:
            engine.dispose()
        if os.path.exists(temp_db_path):
            try:
                os.remove(temp_db_path)
            except Exception:
                pass


def test_existing_database_stamping_scenario_2():
    """
    SCENARIO 2 — Existing database:
    1. Create an existing database with tables and data (simulating codepulse.db state).
    2. Stamp database with Alembic head revision.
    3. Verify that existing schema is brought under migration control without data loss.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_db_path = f.name

    db_url = f"sqlite:///{temp_db_path.replace(os.sep, '/')}"
    engine = None

    try:
        # Simulate pre-existing database populated with data
        engine = create_engine(db_url)
        Base.metadata.create_all(bind=engine)

        with engine.connect() as conn:
            conn.execute(
                text("INSERT INTO repositories (id, team_id, name, url, file_count, symbol_count) VALUES (1, 1, 'legacy_repo', 'file:///local', 5, 25)")
            )
            conn.execute(
                text("INSERT INTO files (id, repo_id, path, language, size_bytes, line_count) VALUES (1, 1, 'main.py', 'python', 100, 10)")
            )
            conn.execute(
                text("INSERT INTO symbols (id, file_id, name, type, start_line, end_line, language) VALUES (1, 1, 'main_func', 'function', 1, 10, 'python')")
            )
            conn.commit()

        # Check that alembic_version does not exist yet
        assert get_current_revision(db_url) is None

        # Stamp the existing database
        stamp_database(db_url=db_url, revision="head")

        # Verify alembic_version is now recorded
        current_rev = get_current_revision(db_url)
        assert current_rev == "21a7cb7f1a85"

        # Verify data integrity is 100% preserved
        with engine.connect() as conn:
            repo_count = conn.execute(text("SELECT count(*) FROM repositories")).scalar()
            file_count = conn.execute(text("SELECT count(*) FROM files")).scalar()
            symbol_count = conn.execute(text("SELECT count(*) FROM symbols")).scalar()

            assert repo_count == 1
            assert file_count == 1
            assert symbol_count == 1

    finally:
        if engine is not None:
            engine.dispose()
        if os.path.exists(temp_db_path):
            try:
                os.remove(temp_db_path)
            except Exception:
                pass
