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
            "api_keys",
            "api_key_repositories",
        }
        assert expected_tables.issubset(table_names), f"Missing tables: {expected_tables - table_names}"

        # Verify revision recorded (head now includes the Phase 1.1 migration)
        current_rev = get_current_revision(db_url)
        assert current_rev == "b0bdd63949f4"

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

        # Verify Phase 0.3 columns/constraints on files/repositories
        file_cols = {col["name"]: col for col in insp.get_columns("files")}
        assert "content_hash" in file_cols

        file_uniques = insp.get_unique_constraints("files")
        assert any(
            set(uc["column_names"]) == {"repo_id", "path"} for uc in file_uniques
        ), f"Expected a UNIQUE(repo_id, path) constraint on files, got: {file_uniques}"

        repo_uniques = insp.get_unique_constraints("repositories")
        assert any(
            uc["column_names"] == ["url"] for uc in repo_uniques
        ), f"Expected a UNIQUE(url) constraint on repositories, got: {repo_uniques}"

        # Verify Phase 0.4 constraint on symbols
        symbol_uniques = insp.get_unique_constraints("symbols")
        assert any(
            set(uc["column_names"]) == {"file_id", "name", "start_line"} for uc in symbol_uniques
        ), f"Expected a UNIQUE(file_id, name, start_line) constraint on symbols, got: {symbol_uniques}"

        # Verify Phase 0.5 columns/constraint on code_embeddings
        embedding_cols = {col["name"]: col for col in insp.get_columns("code_embeddings")}
        assert "chunk_index" in embedding_cols
        assert "content_hash" in embedding_cols
        assert "embedding_model" in embedding_cols
        assert "embedding_dim" in embedding_cols

        embedding_uniques = insp.get_unique_constraints("code_embeddings")
        assert any(
            set(uc["column_names"]) == {"symbol_id", "chunk_type", "chunk_index"}
            for uc in embedding_uniques
        ), f"Expected a UNIQUE(symbol_id, chunk_type, chunk_index) constraint on code_embeddings, got: {embedding_uniques}"

        # Verify Phase 1.1 api_keys / api_key_repositories tables
        api_key_cols = {col["name"]: col for col in insp.get_columns("api_keys")}
        for expected_col in ("name", "key_hash", "key_prefix", "created_at", "revoked_at"):
            assert expected_col in api_key_cols, f"Missing api_keys.{expected_col}"

        api_key_uniques = insp.get_unique_constraints("api_keys")
        assert any(
            uc["column_names"] == ["key_hash"] for uc in api_key_uniques
        ), f"Expected a UNIQUE(key_hash) constraint on api_keys, got: {api_key_uniques}"

        api_key_repo_cols = {col["name"]: col for col in insp.get_columns("api_key_repositories")}
        for expected_col in ("api_key_id", "repo_id"):
            assert expected_col in api_key_repo_cols, f"Missing api_key_repositories.{expected_col}"

        api_key_repo_uniques = insp.get_unique_constraints("api_key_repositories")
        assert any(
            set(uc["column_names"]) == {"api_key_id", "repo_id"} for uc in api_key_repo_uniques
        ), f"Expected a UNIQUE(api_key_id, repo_id) constraint on api_key_repositories, got: {api_key_repo_uniques}"

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
        assert current_rev == "b0bdd63949f4"

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


def test_phase_0_3_migration_preserves_data_and_adds_content_hash():
    """
    SCENARIO 3 (Phase 0.3) — Upgrading a pre-Phase-0.3 database:
    1. Build a database at the OLD head (21a7cb7f1a85, no content_hash / no
       natural-key uniqueness) and populate it with data, simulating the
       real pre-Phase-0.3 codepulse.db state.
    2. Upgrade to the new head (20fc2d0607e2).
    3. Verify all pre-existing rows are preserved untouched.
    4. Verify `content_hash` now exists on `files` and is NULL for the
       pre-existing row (no fabricated backfill value -- see the migration's
       docstring for why a real hash cannot be reconstructed for it).
    5. Verify the new UNIQUE constraints are now enforced at the DB level.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_db_path = f.name
    if os.path.exists(temp_db_path):
        os.remove(temp_db_path)

    db_url = f"sqlite:///{temp_db_path.replace(os.sep, '/')}"
    engine = None

    try:
        # 1. Build the OLD schema only (pre-Phase-0.3) and seed legacy data.
        run_migrations(db_url=db_url, revision="21a7cb7f1a85")
        assert get_current_revision(db_url) == "21a7cb7f1a85"

        engine = create_engine(db_url)
        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO repositories (id, team_id, name, url, file_count, symbol_count) "
                    "VALUES (1, 1, 'legacy_repo', 'file:///legacy/path', 1, 1)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO files (id, repo_id, path, language, size_bytes, line_count) "
                    "VALUES (1, 1, 'legacy_main.py', 'python', 42, 5)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO symbols (id, file_id, name, type, start_line, end_line, language) "
                    "VALUES (1, 1, 'legacy_func', 'function', 1, 5, 'python')"
                )
            )
            conn.commit()

        # 2. Upgrade to the Phase 0.3 revision specifically (not generic
        #    "head" -- this test targets that one migration step in
        #    isolation, and must keep doing so as later migrations are added).
        run_migrations(db_url=db_url, revision="20fc2d0607e2")
        assert get_current_revision(db_url) == "20fc2d0607e2"

        # 3. Pre-existing data preserved untouched.
        with engine.connect() as conn:
            repo_row = conn.execute(
                text("SELECT id, name, url, file_count, symbol_count FROM repositories WHERE id = 1")
            ).fetchone()
            assert repo_row is not None
            assert repo_row[1] == "legacy_repo"
            assert repo_row[2] == "file:///legacy/path"

            file_row = conn.execute(
                text("SELECT id, repo_id, path, language, content_hash FROM files WHERE id = 1")
            ).fetchone()
            assert file_row is not None
            assert file_row[2] == "legacy_main.py"

            # 4. content_hash column exists; legacy row is explicitly NULL,
            #    not a fabricated value.
            assert file_row[4] is None

            symbol_row = conn.execute(
                text("SELECT id, name FROM symbols WHERE id = 1")
            ).fetchone()
            assert symbol_row is not None
            assert symbol_row[1] == "legacy_func"

        # 5. New UNIQUE constraints are enforced going forward.
        with engine.connect() as conn:
            with pytest.raises(Exception):
                conn.execute(
                    text(
                        "INSERT INTO files (repo_id, path, language, size_bytes, line_count) "
                        "VALUES (1, 'legacy_main.py', 'python', 1, 1)"
                    )
                )
                conn.commit()

    finally:
        if engine is not None:
            engine.dispose()
        if os.path.exists(temp_db_path):
            try:
                os.remove(temp_db_path)
            except Exception:
                pass


def test_phase_0_3_migration_refuses_to_apply_over_duplicate_data():
    """
    The Phase 0.3 migration must not silently corrupt or lose data if the
    pre-existing database already violates the new natural-key uniqueness
    it's about to add. It should fail loudly instead.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_db_path = f.name
    if os.path.exists(temp_db_path):
        os.remove(temp_db_path)

    db_url = f"sqlite:///{temp_db_path.replace(os.sep, '/')}"
    engine = None

    try:
        run_migrations(db_url=db_url, revision="21a7cb7f1a85")

        engine = create_engine(db_url)
        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO repositories (id, team_id, name, url, file_count, symbol_count) "
                    "VALUES (1, 1, 'dup_repo', 'file:///dup/path', 2, 0)"
                )
            )
            # Two File rows with the same (repo_id, path) -- pre-existing
            # duplicate that would violate the new constraint.
            conn.execute(
                text(
                    "INSERT INTO files (id, repo_id, path, language) "
                    "VALUES (1, 1, 'dup.py', 'python')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO files (id, repo_id, path, language) "
                    "VALUES (2, 1, 'dup.py', 'python')"
                )
            )
            conn.commit()

        with pytest.raises(Exception):
            run_migrations(db_url=db_url, revision="head")

        # The database must remain at the old revision -- migration aborted,
        # nothing silently half-applied.
        assert get_current_revision(db_url) == "21a7cb7f1a85"

    finally:
        if engine is not None:
            engine.dispose()
        if os.path.exists(temp_db_path):
            try:
                os.remove(temp_db_path)
            except Exception:
                pass


def test_phase_0_4_migration_preserves_data_and_adds_symbol_uniqueness():
    """
    SCENARIO 4 (Phase 0.4) — Upgrading a pre-Phase-0.4 database:
    1. Build a database at the Phase 0.3 head (20fc2d0607e2, no
       (file_id, name, start_line) uniqueness on symbols yet) and populate
       it with data, simulating a real post-Phase-0.3 codepulse.db state.
    2. Upgrade to the new head (3ba396e9c78f).
    3. Verify all pre-existing rows (including symbols) are preserved
       untouched.
    4. Verify the new UNIQUE(file_id, name, start_line) constraint on
       `symbols` is now enforced at the DB level.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_db_path = f.name
    if os.path.exists(temp_db_path):
        os.remove(temp_db_path)

    db_url = f"sqlite:///{temp_db_path.replace(os.sep, '/')}"
    engine = None

    try:
        # 1. Build the Phase 0.3 schema and seed data (as if Phase 0.3 had
        #    already run for real, matching this project's actual history).
        run_migrations(db_url=db_url, revision="20fc2d0607e2")
        assert get_current_revision(db_url) == "20fc2d0607e2"

        engine = create_engine(db_url)
        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO repositories (id, team_id, name, url, file_count, symbol_count) "
                    "VALUES (1, 1, 'phase03_repo', 'file:///phase03/path', 1, 1)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO files (id, repo_id, path, language, size_bytes, line_count, content_hash) "
                    "VALUES (1, 1, 'main.py', 'python', 42, 5, 'deadbeef')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO symbols (id, file_id, name, type, start_line, end_line, language, body_hash) "
                    "VALUES (1, 1, 'existing_func', 'function', 1, 5, 'python', 'abc123')"
                )
            )
            conn.commit()

        # 2. Upgrade to the Phase 0.4 revision specifically (not generic
        #    "head" -- this test targets that one migration step in
        #    isolation, and must keep doing so as later migrations are added).
        run_migrations(db_url=db_url, revision="3ba396e9c78f")
        assert get_current_revision(db_url) == "3ba396e9c78f"

        # 3. Pre-existing data preserved untouched.
        with engine.connect() as conn:
            symbol_row = conn.execute(
                text("SELECT id, file_id, name, start_line, body_hash FROM symbols WHERE id = 1")
            ).fetchone()
            assert symbol_row is not None
            assert symbol_row[2] == "existing_func"
            assert symbol_row[4] == "abc123"

            repo_count = conn.execute(text("SELECT count(*) FROM repositories")).scalar()
            file_count = conn.execute(text("SELECT count(*) FROM files")).scalar()
            symbol_count = conn.execute(text("SELECT count(*) FROM symbols")).scalar()
            assert (repo_count, file_count, symbol_count) == (1, 1, 1)

        # 4. New UNIQUE constraint is enforced going forward.
        with engine.connect() as conn:
            with pytest.raises(Exception):
                conn.execute(
                    text(
                        "INSERT INTO symbols (file_id, name, type, start_line, end_line, language) "
                        "VALUES (1, 'existing_func', 'function', 1, 5, 'python')"
                    )
                )
                conn.commit()

    finally:
        if engine is not None:
            engine.dispose()
        if os.path.exists(temp_db_path):
            try:
                os.remove(temp_db_path)
            except Exception:
                pass


def test_phase_0_4_migration_refuses_to_apply_over_duplicate_data():
    """
    The Phase 0.4 migration must not silently corrupt or lose data if the
    pre-existing database already violates the new (file_id, name,
    start_line) uniqueness it's about to add. It should fail loudly instead.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_db_path = f.name
    if os.path.exists(temp_db_path):
        os.remove(temp_db_path)

    db_url = f"sqlite:///{temp_db_path.replace(os.sep, '/')}"
    engine = None

    try:
        run_migrations(db_url=db_url, revision="20fc2d0607e2")

        engine = create_engine(db_url)
        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO repositories (id, team_id, name, url, file_count, symbol_count) "
                    "VALUES (1, 1, 'dup_repo', 'file:///dup/path', 1, 2)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO files (id, repo_id, path, language) "
                    "VALUES (1, 1, 'dup.py', 'python')"
                )
            )
            # Two Symbol rows with the same (file_id, name, start_line) --
            # pre-existing duplicate that would violate the new constraint.
            conn.execute(
                text(
                    "INSERT INTO symbols (id, file_id, name, type, start_line, end_line, language) "
                    "VALUES (1, 1, 'dup_func', 'function', 1, 2, 'python')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO symbols (id, file_id, name, type, start_line, end_line, language) "
                    "VALUES (2, 1, 'dup_func', 'function', 1, 2, 'python')"
                )
            )
            conn.commit()

        with pytest.raises(Exception):
            run_migrations(db_url=db_url, revision="head")

        # The database must remain at the old revision -- migration aborted,
        # nothing silently half-applied.
        assert get_current_revision(db_url) == "20fc2d0607e2"

    finally:
        if engine is not None:
            engine.dispose()
        if os.path.exists(temp_db_path):
            try:
                os.remove(temp_db_path)
            except Exception:
                pass


def test_phase_0_5_migration_preserves_data_and_adds_embedding_fields():
    """
    SCENARIO 5 (Phase 0.5) — Upgrading a pre-Phase-0.5 database:
    1. Build a database at the Phase 0.4 head (3ba396e9c78f, no chunk_index/
       content_hash/embedding_model/embedding_dim on code_embeddings yet)
       and populate it with data, including an embedding row, simulating a
       real post-Phase-0.4 codepulse.db state.
    2. Upgrade to the new head (49572f834366).
    3. Verify all pre-existing rows (including the embedding row) are
       preserved untouched.
    4. Verify the new UNIQUE(symbol_id, chunk_type, chunk_index) constraint
       on `code_embeddings` is now enforced at the DB level.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_db_path = f.name
    if os.path.exists(temp_db_path):
        os.remove(temp_db_path)

    db_url = f"sqlite:///{temp_db_path.replace(os.sep, '/')}"
    engine = None

    try:
        # 1. Build the Phase 0.4 schema and seed data (as if Phase 0.3/0.4
        #    had already run for real, matching this project's actual history).
        run_migrations(db_url=db_url, revision="3ba396e9c78f")
        assert get_current_revision(db_url) == "3ba396e9c78f"

        engine = create_engine(db_url)
        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO repositories (id, team_id, name, url, file_count, symbol_count) "
                    "VALUES (1, 1, 'phase04_repo', 'file:///phase04/path', 1, 1)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO files (id, repo_id, path, language, size_bytes, line_count, content_hash) "
                    "VALUES (1, 1, 'main.py', 'python', 42, 5, 'deadbeef')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO symbols (id, file_id, name, type, start_line, end_line, language, body_hash) "
                    "VALUES (1, 1, 'existing_func', 'function', 1, 5, 'python', 'abc123')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO code_embeddings (id, symbol_id, chunk_type, embedding_json, chunk_content) "
                    "VALUES (1, 1, 'signature_doc', '[0.1, 0.2, 0.3]', 'def existing_func(): ...')"
                )
            )
            conn.commit()

        # 2. Upgrade to the Phase 0.5 revision specifically (not generic
        #    "head" -- this test targets that one migration step in
        #    isolation, and must keep doing so as later migrations are added).
        run_migrations(db_url=db_url, revision="49572f834366")
        assert get_current_revision(db_url) == "49572f834366"

        # 3. Pre-existing data preserved untouched.
        with engine.connect() as conn:
            embedding_row = conn.execute(
                text(
                    "SELECT id, symbol_id, chunk_type, chunk_index, embedding_json, "
                    "content_hash, embedding_model, embedding_dim FROM code_embeddings WHERE id = 1"
                )
            ).fetchone()
            assert embedding_row is not None
            assert embedding_row[1] == 1  # symbol_id preserved
            assert embedding_row[2] == "signature_doc"  # chunk_type preserved
            assert embedding_row[4] == "[0.1, 0.2, 0.3]"  # embedding_json untouched

            # New columns: chunk_index defaults to 0 for pre-existing rows;
            # content_hash/embedding_model/embedding_dim are honestly NULL,
            # not a fabricated value (no original chunk text was hashed).
            assert embedding_row[3] == 0
            assert embedding_row[5] is None
            assert embedding_row[6] is None
            assert embedding_row[7] is None

            repo_count = conn.execute(text("SELECT count(*) FROM repositories")).scalar()
            file_count = conn.execute(text("SELECT count(*) FROM files")).scalar()
            symbol_count = conn.execute(text("SELECT count(*) FROM symbols")).scalar()
            embedding_count = conn.execute(text("SELECT count(*) FROM code_embeddings")).scalar()
            assert (repo_count, file_count, symbol_count, embedding_count) == (1, 1, 1, 1)

        # 4. New UNIQUE constraint is enforced going forward.
        with engine.connect() as conn:
            with pytest.raises(Exception):
                conn.execute(
                    text(
                        "INSERT INTO code_embeddings (symbol_id, chunk_type, chunk_index, embedding_json) "
                        "VALUES (1, 'signature_doc', 0, '[0.9, 0.9, 0.9]')"
                    )
                )
                conn.commit()

    finally:
        if engine is not None:
            engine.dispose()
        if os.path.exists(temp_db_path):
            try:
                os.remove(temp_db_path)
            except Exception:
                pass


def test_phase_0_5_migration_refuses_to_apply_over_duplicate_data():
    """
    The Phase 0.5 migration must not silently corrupt or lose data if the
    pre-existing database already violates the new (symbol_id, chunk_type,
    chunk_index) uniqueness it's about to add (every pre-existing row would
    default to chunk_index=0). It should fail loudly instead.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_db_path = f.name
    if os.path.exists(temp_db_path):
        os.remove(temp_db_path)

    db_url = f"sqlite:///{temp_db_path.replace(os.sep, '/')}"
    engine = None

    try:
        run_migrations(db_url=db_url, revision="3ba396e9c78f")

        engine = create_engine(db_url)
        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO repositories (id, team_id, name, url, file_count, symbol_count) "
                    "VALUES (1, 1, 'dup_repo', 'file:///dup/path', 1, 1)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO files (id, repo_id, path, language) "
                    "VALUES (1, 1, 'dup.py', 'python')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO symbols (id, file_id, name, type, start_line, end_line, language) "
                    "VALUES (1, 1, 'dup_func', 'function', 1, 2, 'python')"
                )
            )
            # Two Embedding rows with the same (symbol_id, chunk_type) --
            # pre-existing duplicate that would collide once both default to
            # chunk_index=0, violating the new constraint.
            conn.execute(
                text(
                    "INSERT INTO code_embeddings (id, symbol_id, chunk_type, embedding_json) "
                    "VALUES (1, 1, 'signature_doc', '[0.1]')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO code_embeddings (id, symbol_id, chunk_type, embedding_json) "
                    "VALUES (2, 1, 'signature_doc', '[0.2]')"
                )
            )
            conn.commit()

        with pytest.raises(Exception):
            run_migrations(db_url=db_url, revision="head")

        # The database must remain at the old revision -- migration aborted,
        # nothing silently half-applied.
        assert get_current_revision(db_url) == "3ba396e9c78f"

    finally:
        if engine is not None:
            engine.dispose()
        if os.path.exists(temp_db_path):
            try:
                os.remove(temp_db_path)
            except Exception:
                pass


def test_phase_1_1_migration_preserves_data_and_adds_api_key_tables():
    """
    SCENARIO 6 (Phase 1.1) — Upgrading a pre-Phase-1.1 database:
    1. Build a database at the Phase 0.5 head (49572f834366, no api_keys/
       api_key_repositories tables yet) and populate it with data,
       simulating a real post-Phase-0.9/0.10 codepulse.db state.
    2. Upgrade to the new head (b0bdd63949f4).
    3. Verify all pre-existing rows are preserved untouched.
    4. Verify the new `api_keys`/`api_key_repositories` tables exist with
       the expected columns/constraints and are genuinely usable (a real
       insert succeeds; a duplicate key_hash is rejected).

    Unlike prior phases' migrations, this one only CREATES new tables --
    it does not alter any existing table -- so there is no pre-existing-
    duplicate-data failure mode to test here (see the migration's own
    docstring).
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_db_path = f.name
    if os.path.exists(temp_db_path):
        os.remove(temp_db_path)

    db_url = f"sqlite:///{temp_db_path.replace(os.sep, '/')}"
    engine = None

    try:
        # 1. Build the Phase 0.5 schema and seed data.
        run_migrations(db_url=db_url, revision="49572f834366")
        assert get_current_revision(db_url) == "49572f834366"

        engine = create_engine(db_url)
        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO repositories (id, team_id, name, url, file_count, symbol_count) "
                    "VALUES (1, 1, 'phase05_repo', 'file:///phase05/path', 1, 1)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO files (id, repo_id, path, language, size_bytes, line_count, content_hash) "
                    "VALUES (1, 1, 'main.py', 'python', 42, 5, 'deadbeef')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO symbols (id, file_id, name, type, start_line, end_line, language, body_hash) "
                    "VALUES (1, 1, 'existing_func', 'function', 1, 5, 'python', 'abc123')"
                )
            )
            conn.commit()

        # 2. Upgrade to the Phase 1.1 revision specifically (not generic
        #    "head" -- this test targets that one migration step in
        #    isolation, and must keep doing so as later migrations are added).
        run_migrations(db_url=db_url, revision="b0bdd63949f4")
        assert get_current_revision(db_url) == "b0bdd63949f4"

        # 3. Pre-existing data preserved untouched.
        with engine.connect() as conn:
            repo_count = conn.execute(text("SELECT count(*) FROM repositories")).scalar()
            file_count = conn.execute(text("SELECT count(*) FROM files")).scalar()
            symbol_count = conn.execute(text("SELECT count(*) FROM symbols")).scalar()
            assert (repo_count, file_count, symbol_count) == (1, 1, 1)

        # 4. New tables exist, are correctly shaped, and are genuinely usable.
        insp = inspect(engine)
        assert "api_keys" in insp.get_table_names()
        assert "api_key_repositories" in insp.get_table_names()

        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO api_keys (id, name, key_hash, key_prefix, created_at) "
                    "VALUES (1, 'test-key', :h, 'cpk_aaaaaaaa', :ts)"
                ),
                {"h": "a" * 64, "ts": "2026-01-01 00:00:00"},
            )
            conn.execute(
                text(
                    "INSERT INTO api_key_repositories (id, api_key_id, repo_id) VALUES (1, 1, 1)"
                )
            )
            conn.commit()

            # Duplicate key_hash must be rejected at the DB level.
            with pytest.raises(Exception):
                conn.execute(
                    text(
                        "INSERT INTO api_keys (name, key_hash, key_prefix, created_at) "
                        "VALUES ('dup', :h, 'cpk_bbbbbbbb', :ts)"
                    ),
                    {"h": "a" * 64, "ts": "2026-01-01 00:00:00"},
                )
                conn.commit()

    finally:
        if engine is not None:
            engine.dispose()
        if os.path.exists(temp_db_path):
            try:
                os.remove(temp_db_path)
            except Exception:
                pass
