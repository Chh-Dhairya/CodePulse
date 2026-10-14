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
    CallEdge,
    EmbeddingModel,
    StorageManager,
)
from codepulse.storage.migrations import (
    downgrade_migrations,
    get_alembic_config,
    get_current_revision,
    head_revision,
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
    # The scripts live inside the package (FUTURE_CHANGES finding K) and the config needs no alembic.ini;
    # this used to assert the folder was called "alembic".
    assert "schema_migrations" in cfg.get_main_option("script_location")


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
            "call_edges",
            "code_embeddings",
            "api_keys",
            "api_key_repositories",
            "index_jobs",
            "repository_snapshots",
        }
        assert expected_tables.issubset(table_names), f"Missing tables: {expected_tables - table_names}"

        # Verify revision recorded (head now includes the repository-url canonicalization and the module_edges migrations
        # on top of Phase 7.2's
        # api_key_repositories.role migration on top of Phase 4.1's
        # base_classes/relationship_type migration and the
        # repository-isolation fix)
        current_rev = get_current_revision(db_url)
        assert current_rev == "a41f0d2c6b8e"

        # Verify the isolation-fix column exists on call_edges
        call_edge_cols = {col["name"] for col in insp.get_columns("call_edges")}
        assert "repo_id" in call_edge_cols
        assert "confidence" in call_edge_cols

        # Verify Phase 4.1's additive columns exist
        assert "relationship_type" in call_edge_cols
        symbol_cols = {col["name"] for col in insp.get_columns("symbols")}
        assert "base_classes" in symbol_cols

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

        # Verify Phase 1.3 index_jobs table
        index_job_cols = {col["name"]: col for col in insp.get_columns("index_jobs")}
        for expected_col in (
            "repo_id", "status", "created_at", "started_at", "finished_at",
            "error_message", "result_summary",
        ):
            assert expected_col in index_job_cols, f"Missing index_jobs.{expected_col}"

        index_job_indexes = [idx["name"] for idx in insp.get_indexes("index_jobs")]
        assert "ix_index_jobs_repo_id" in index_job_indexes

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

        # Verify alembic_version is now recorded (head now includes the
        # module_edges migration on top of Phase
        # 7.2's api_key_repositories.role migration on top of Phase
        # 4.1's base_classes/relationship_type migration and the
        # repository-isolation fix)
        current_rev = get_current_revision(db_url)
        assert current_rev == "a41f0d2c6b8e"

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


def test_storage_manager_never_races_ahead_of_a_pending_migration():
    """
    Backend-hardening fix -- direct regression test for the exact
    incident class documented in CODEPULSE_PROGRESS.md's "Known
    technical debt" (three real, separate occurrences): an ad-hoc
    `StorageManager()` construction against an already-Alembic-managed
    database used to silently `create_all()` a table a pending
    migration hadn't created yet, racing ahead of Alembic.

    Reproduces the exact shape of the incident: migrate only partway
    (to `de22df2fc3ce`, the revision immediately BEFORE
    `c286d277ea21` added `repository_snapshots`), so the database has
    a real `alembic_version` table but is deliberately missing a table
    that exists on `Base.metadata`. Before this fix, constructing
    `StorageManager` against this database would have silently created
    `repository_snapshots` anyway (via `create_all()`), leaving Alembic
    unaware that table now exists outside its own migration history --
    exactly what happened for real with `index_jobs` (Phase 1.3),
    `repository_snapshots` (Phase 3.1), and `call_edges` (Phase 3.4).
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_db_path = f.name
    if os.path.exists(temp_db_path):
        os.remove(temp_db_path)

    db_url = f"sqlite:///{temp_db_path.replace(os.sep, '/')}"
    engine = None
    storage = None

    try:
        run_migrations(db_url=db_url, revision="de22df2fc3ce")

        engine = create_engine(db_url)
        insp = inspect(engine)
        before_tables = set(insp.get_table_names())
        assert "alembic_version" in before_tables
        assert "repository_snapshots" not in before_tables, (
            "test setup assumption violated -- de22df2fc3ce must predate "
            "the repository_snapshots table for this regression test to "
            "actually exercise the incident scenario"
        )

        # THE FIX UNDER TEST: constructing StorageManager against this
        # partially-migrated database must NOT create the missing table.
        storage = StorageManager(db_url=db_url)

        insp_after = inspect(engine)
        after_tables = set(insp_after.get_table_names())
        assert "repository_snapshots" not in after_tables, (
            "StorageManager() must never create_all() a table on an "
            "already-Alembic-managed database (alembic_version present) "
            "-- doing so races ahead of a pending migration, the exact "
            "root cause of three real, documented incidents"
        )
        assert get_current_revision(db_url) == "de22df2fc3ce", (
            "alembic_version must stay exactly where the last real "
            "migration left it -- StorageManager must never touch it"
        )

        # The database remains fully usable for every table THAT DOES
        # already exist -- this fix only prevents creating NEW tables
        # outside Alembic, it doesn't break normal operation.
        session = storage.get_session()
        try:
            repo = RepositoryModel(name="partial_repo", url="https://example.com/partial")
            session.add(repo)
            session.commit()
            assert session.query(RepositoryModel).filter_by(name="partial_repo").first() is not None
        finally:
            session.close()

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


def test_storage_manager_still_creates_tables_on_a_genuinely_fresh_database():
    """
    The other half of the same fix: a database Alembic has never touched
    at all (no `alembic_version` table -- the exact shape of every test
    fixture in this project's own suite, e.g. `tests/conftest.py`'s
    `isolated_app`) must still get a full, zero-setup schema from
    `StorageManager`'s `create_all()`.

    2026-10-14 (FUTURE_CHANGES items 8/17, user-approved): this test used to
    assert that such a database was NOT tracked ("no migration history, by
    design"), which is exactly why a database CodePulse created itself could
    never be upgraded later. It is now recorded at the head revision as soon
    as it is created (a legacy database that already has tables but no
    tracking is still left alone; see test_migrate_command.py).
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_db_path = f.name
    if os.path.exists(temp_db_path):
        os.remove(temp_db_path)

    db_url = f"sqlite:///{temp_db_path.replace(os.sep, '/')}"
    storage = None
    try:
        storage = StorageManager(db_url=db_url)
        insp = inspect(storage.engine)
        table_names = set(insp.get_table_names())
        assert "alembic_version" in table_names, (
            "a database StorageManager creates from scratch must be tracked from the start, "
            "or it can never be upgraded later"
        )
        assert get_current_revision(db_url) == head_revision()
        assert {"repositories", "files", "symbols", "call_edges"}.issubset(table_names)
    finally:
        if storage is not None and hasattr(storage, "engine"):
            storage.engine.dispose()
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


def test_phase_1_3_migration_preserves_data_and_adds_index_jobs_table():
    """
    SCENARIO 7 (Phase 1.3) — Upgrading a pre-Phase-1.3 database:
    1. Build a database at the Phase 1.1 head (b0bdd63949f4, no
       index_jobs table yet) and populate it with data, simulating a real
       post-Phase-1.2 codepulse.db state.
    2. Upgrade to the new head (de22df2fc3ce).
    3. Verify all pre-existing rows are preserved untouched.
    4. Verify the new `index_jobs` table exists with the expected columns/
       index and is genuinely usable (a real insert succeeds; its FK to
       `repositories` is honored).

    Like Phase 1.1's migration, this one only CREATES a new table -- it
    does not alter any existing table -- so there is no pre-existing-
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
        # 1. Build the Phase 1.1 schema and seed data.
        run_migrations(db_url=db_url, revision="b0bdd63949f4")
        assert get_current_revision(db_url) == "b0bdd63949f4"

        engine = create_engine(db_url)
        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO repositories (id, team_id, name, url, file_count, symbol_count) "
                    "VALUES (1, 1, 'phase11_repo', 'file:///phase11/path', 1, 1)"
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
                    "INSERT INTO api_keys (id, name, key_hash, key_prefix, created_at) "
                    "VALUES (1, 'existing-key', :h, 'cpk_aaaaaaaa', :ts)"
                ),
                {"h": "b" * 64, "ts": "2026-01-01 00:00:00"},
            )
            conn.commit()

        # 2. Upgrade to the Phase 1.3 revision specifically (not generic
        #    "head" -- this test targets that one migration step in
        #    isolation, and must keep doing so as later migrations are added).
        run_migrations(db_url=db_url, revision="de22df2fc3ce")
        assert get_current_revision(db_url) == "de22df2fc3ce"

        # 3. Pre-existing data preserved untouched.
        with engine.connect() as conn:
            repo_count = conn.execute(text("SELECT count(*) FROM repositories")).scalar()
            file_count = conn.execute(text("SELECT count(*) FROM files")).scalar()
            symbol_count = conn.execute(text("SELECT count(*) FROM symbols")).scalar()
            api_key_count = conn.execute(text("SELECT count(*) FROM api_keys")).scalar()
            assert (repo_count, file_count, symbol_count, api_key_count) == (1, 1, 1, 1)

        # 4. New table exists, is correctly shaped, and is genuinely usable.
        insp = inspect(engine)
        assert "index_jobs" in insp.get_table_names()

        index_job_cols = {col["name"] for col in insp.get_columns("index_jobs")}
        assert {
            "id", "repo_id", "status", "created_at", "started_at",
            "finished_at", "error_message", "result_summary",
        }.issubset(index_job_cols)

        index_job_indexes = [idx["name"] for idx in insp.get_indexes("index_jobs")]
        assert "ix_index_jobs_repo_id" in index_job_indexes

        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO index_jobs (id, repo_id, status, created_at) "
                    "VALUES (1, 1, 'queued', :ts)"
                ),
                {"ts": "2026-01-01 00:00:00"},
            )
            conn.commit()

            job_count = conn.execute(text("SELECT count(*) FROM index_jobs")).scalar()
            assert job_count == 1

    finally:
        if engine is not None:
            engine.dispose()
        if os.path.exists(temp_db_path):
            try:
                os.remove(temp_db_path)
            except Exception:
                pass


def test_module_edges_migration_preserves_data_creates_the_table_and_downgrades_cleanly():
    """
    Upgrading a database at the previous head (b9014c049e33, no module_edges
    table) to 7c3e5a91d2f4:
      1. existing rows in every other table are preserved untouched;
      2. `module_edges` exists with the expected columns, the repo_id index,
         the unique (repo_id, source_file, target_file) constraint, and a
         foreign key to `repositories` that cascades on delete;
      3. a downgrade removes exactly that table and nothing else.

    Purely additive (it only creates a table), so there is no
    pre-existing-duplicate-data failure mode to test.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_db_path = f.name
    if os.path.exists(temp_db_path):
        os.remove(temp_db_path)
    db_url = f"sqlite:///{temp_db_path.replace(os.sep, '/')}"
    engine = None

    try:
        run_migrations(db_url=db_url, revision="b9014c049e33")
        assert get_current_revision(db_url) == "b9014c049e33"
        engine = create_engine(db_url)
        with engine.connect() as conn:
            conn.execute(text(
                "INSERT INTO repositories (id, team_id, name, url, file_count, symbol_count) "
                "VALUES (1, 1, 'r', 'file:///r', 1, 1)"))
            conn.execute(text(
                "INSERT INTO files (id, repo_id, path, language, size_bytes, line_count, content_hash) "
                "VALUES (1, 1, 'main.py', 'python', 42, 5, 'deadbeef')"))
            conn.commit()
        assert "module_edges" not in inspect(engine).get_table_names()

        run_migrations(db_url=db_url, revision="7c3e5a91d2f4")
        assert get_current_revision(db_url) == "7c3e5a91d2f4"

        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM repositories")).scalar() == 1
            assert conn.execute(text("SELECT count(*) FROM files")).scalar() == 1

        insp = inspect(engine)
        assert "module_edges" in insp.get_table_names()
        assert {c["name"] for c in insp.get_columns("module_edges")} == {"id", "repo_id", "source_file", "target_file"}
        assert "ix_module_edges_repo_id" in [i["name"] for i in insp.get_indexes("module_edges")]
        assert any(
            sorted(u["column_names"]) == ["repo_id", "source_file", "target_file"]
            for u in insp.get_unique_constraints("module_edges")
        )
        fks = insp.get_foreign_keys("module_edges")
        assert [(fk["referred_table"], fk["constrained_columns"]) for fk in fks] == [("repositories", ["repo_id"])]
        assert fks[0]["options"].get("ondelete") == "CASCADE"

        # Usable: insert, reject a duplicate, cascade on repository delete.
        with engine.connect() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.execute(text("INSERT INTO module_edges (repo_id, source_file, target_file) VALUES (1, 'a.py', 'b.py')"))
            conn.commit()
            with pytest.raises(Exception):
                conn.execute(text("INSERT INTO module_edges (repo_id, source_file, target_file) VALUES (1, 'a.py', 'b.py')"))
            conn.rollback()
            conn.execute(text("DELETE FROM repositories WHERE id = 1"))
            conn.commit()
            assert conn.execute(text("SELECT count(*) FROM module_edges")).scalar() == 0

        downgrade_migrations(db_url=db_url, revision="b9014c049e33")
        assert get_current_revision(db_url) == "b9014c049e33"
        assert "module_edges" not in inspect(engine).get_table_names()
        assert "api_key_repositories" in inspect(engine).get_table_names()
    finally:
        if engine is not None:
            engine.dispose()
        if os.path.exists(temp_db_path):
            try:
                os.remove(temp_db_path)
            except Exception:
                pass


def test_repository_url_canonicalization_migration_rewrites_spellings_and_never_merges_or_deletes(tmp_path):
    """
    Upgrading a database at 7c3e5a91d2f4 to a41f0d2c6b8e canonicalizes the local-path
    `repositories.url` values that exist, leaving everything that is not a local path
    untouched, and NEVER merging or deleting a duplicate that predates the fix.
    """
    from codepulse.repository_identity import canonical_repository_url

    proj = tmp_path / "proj"
    other = tmp_path / "other"
    dup = tmp_path / "dup"
    for d in (proj, other, dup, other / "sub"):
        d.mkdir(parents=True, exist_ok=True)

    db_url = f"sqlite:///{(tmp_path / 'mig.db').as_posix()}"
    rows = [
        (1, str(proj) + os.sep),                         # trailing separator
        (2, os.path.join(str(other), ".", "sub", "..")),  # dot segments
        (3, "https://github.com/org/repo"),               # not a path
        (4, "file:///seed/anchor"),                       # not a path
        (5, str(dup)),                                    # already canonical ...
        (6, str(dup) + os.sep),                           # ... and a duplicate of 5 that predates the fix
    ]
    if os.name == "nt":
        rows.append((7, str(tmp_path / "winproj")[0].lower() + str(tmp_path / "winproj")[1:]))  # lowercase drive
        (tmp_path / "winproj").mkdir()

    engine = None
    try:
        run_migrations(db_url=db_url, revision="7c3e5a91d2f4")
        engine = create_engine(db_url)
        with engine.connect() as conn:
            for repo_id, url in rows:
                conn.execute(
                    text("INSERT INTO repositories (id, team_id, name, url, file_count, symbol_count) VALUES (:i, 1, :n, :u, 1, 1)"),
                    {"i": repo_id, "n": f"r{repo_id}", "u": url},
                )
            conn.execute(text(
                "INSERT INTO files (id, repo_id, path, language, size_bytes, line_count, content_hash) "
                "VALUES (1, 1, 'main.py', 'python', 1, 1, 'aa')"))
            conn.commit()

        run_migrations(db_url=db_url, revision="head")
        assert get_current_revision(db_url) == "a41f0d2c6b8e"

        with engine.connect() as conn:
            urls = dict(conn.execute(text("SELECT id, url FROM repositories")).fetchall())
            assert conn.execute(text("SELECT count(*) FROM files WHERE repo_id = 1")).scalar() == 1, "child rows untouched"

        assert urls[1] == canonical_repository_url(str(proj))
        assert urls[2] == canonical_repository_url(str(other))
        assert urls[3] == "https://github.com/org/repo"
        assert urls[4] == "file:///seed/anchor"
        assert urls[5] == canonical_repository_url(str(dup))
        assert urls[6] == str(dup) + os.sep, "the duplicate of row 5 must be left unchanged, not merged or deleted"
        assert len(urls) == len(rows), "no row is ever deleted"
        if os.name == "nt":
            assert urls[7][0].isupper()

        # idempotent: running it again changes nothing
        downgrade_migrations(db_url=db_url, revision="7c3e5a91d2f4")
        run_migrations(db_url=db_url, revision="head")
        with engine.connect() as conn:
            assert dict(conn.execute(text("SELECT id, url FROM repositories")).fetchall()) == urls
    finally:
        if engine is not None:
            engine.dispose()
