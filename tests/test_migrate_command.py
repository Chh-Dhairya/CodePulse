"""
`codepulse migrate`, schema status, and packaged migrations
(CODEPULSE_FUTURE_CHANGES.md items 8 and 17 and finding K; user-approved 2026-10-14).

Before this, bringing a database up to date meant running `alembic upgrade head` by
hand from the repository root. There was no CodePulse command for it, nothing told a
user their database was behind, a database CodePulse created itself was never
tracked by Alembic (so it could never be upgraded the Alembic way), and a built
wheel shipped no migrations at all (`alembic.ini` and the scripts sat outside the
package), so none of this could work from an installed copy.

What these tests pin down:
  - `get_schema_status` classifies a database: fresh (no tables), current, behind,
    unstamped (tables but never tracked) -- which is stampable only when its schema
    matches the models, else `diverged` -- and unknown (stamped with a revision this
    CodePulse does not have).
  - `migrate_database` does the safe thing for each state: creates a fresh database
    at head, upgrades a behind one after taking a consistent backup, stamps an
    unstamped one only when it provably matches, and REFUSES (changing nothing) for
    a diverged or unknown one. `--check` never changes anything.
  - A database `StorageManager` creates from scratch is stamped at head, so it can
    be upgraded later; a legacy unstamped database with tables is left alone.
  - The CLI warns on stderr when the database is behind, `doctor` reports the schema,
    and `codepulse migrate` has stable exit codes and JSON output.
  - The migrations ship inside the package and the Alembic config no longer depends on
    the repository-root `alembic.ini` or the current directory.

Temporary databases only; never the real codepulse.db.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

import codepulse.cli as cli
from codepulse.storage.db import Base, StorageManager
from codepulse.storage.migrations import (
    MigrationRefused,
    get_alembic_config,
    get_current_revision,
    get_schema_status,
    head_revision,
    migrate_database,
    run_migrations,
)

PREVIOUS_REVISION = "7c3e5a91d2f4"  # the revision before head (module_edges)


def _url(tmp_path, name="m.db"):
    return f"sqlite:///{(tmp_path / name).as_posix()}"


def _file_hash(tmp_path, name="m.db"):
    return hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()


def _tables(url):
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _create_all_unstamped(url):
    """A database as CodePulse used to create it: every table, no alembic_version."""
    engine = create_engine(url)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


def _seed_repo(url, repo_url="file:///seed"):
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO repositories (team_id, name, url, file_count, symbol_count) VALUES (1, 'r', :u, 0, 0)"), {"u": repo_url})
    finally:
        engine.dispose()


def _repo_urls(url):
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return [row[0] for row in conn.execute(text("SELECT url FROM repositories ORDER BY id"))]
    finally:
        engine.dispose()


# --- schema status -----------------------------------------------------------

def test_a_database_that_does_not_exist_is_fresh_and_asking_does_not_create_it(tmp_path):
    status = get_schema_status(_url(tmp_path))

    assert status.state == "fresh"
    assert not (tmp_path / "m.db").exists(), "looking must never create the file"


def test_an_empty_database_file_is_fresh(tmp_path):
    sqlite3.connect(tmp_path / "m.db").close()
    assert get_schema_status(_url(tmp_path)).state == "fresh"


def test_a_database_at_head_is_current(tmp_path):
    run_migrations(db_url=_url(tmp_path))
    status = get_schema_status(_url(tmp_path))
    assert status.state == "current"
    assert status.current == status.head == head_revision()
    assert status.pending == []


def test_a_database_behind_head_lists_exactly_the_pending_revisions_oldest_first(tmp_path):
    run_migrations(db_url=_url(tmp_path), revision=PREVIOUS_REVISION)
    status = get_schema_status(_url(tmp_path))
    assert status.state == "behind"
    assert status.current == PREVIOUS_REVISION
    assert status.pending, "there must be at least one pending revision"
    assert PREVIOUS_REVISION not in status.pending, "the current revision is already applied"
    assert status.pending[-1] == head_revision(), "oldest first, ending at head"


def test_tables_created_by_create_all_with_no_alembic_version_are_unstamped_and_stampable(tmp_path):
    _create_all_unstamped(_url(tmp_path))
    status = get_schema_status(_url(tmp_path))
    assert status.state == "unstamped"
    assert status.differences == []


def test_an_unstamped_database_missing_a_table_the_models_have_is_diverged(tmp_path):
    _create_all_unstamped(_url(tmp_path))
    engine = create_engine(_url(tmp_path))
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE module_edges"))
    engine.dispose()

    status = get_schema_status(_url(tmp_path))

    assert status.state == "diverged"
    assert any("module_edges" in d for d in status.differences)


def test_a_database_stamped_with_a_revision_this_codepulse_does_not_have_is_unknown(tmp_path):
    run_migrations(db_url=_url(tmp_path))
    engine = create_engine(_url(tmp_path))
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = 'ffffffffffff'"))
    engine.dispose()

    status = get_schema_status(_url(tmp_path))

    assert status.state == "unknown"
    assert "ffffffffffff" in status.detail


# --- migrate_database --------------------------------------------------------

def test_migrating_a_fresh_database_creates_the_schema_at_head_and_tracks_it(tmp_path):
    result = migrate_database(_url(tmp_path))

    assert result.action == "created"
    assert get_current_revision(_url(tmp_path)) == head_revision()
    assert "repositories" in _tables(_url(tmp_path)) and "module_edges" in _tables(_url(tmp_path))
    assert result.backup_path is None, "nothing to back up for a database that did not exist"


def test_migrating_a_behind_database_backs_it_up_then_upgrades_and_keeps_the_data(tmp_path):
    run_migrations(db_url=_url(tmp_path), revision=PREVIOUS_REVISION)
    _seed_repo(_url(tmp_path), "file:///keep-me")
    before_hash = _file_hash(tmp_path)

    result = migrate_database(_url(tmp_path))

    assert result.action == "upgraded"
    assert get_current_revision(_url(tmp_path)) == head_revision()
    assert _repo_urls(_url(tmp_path)) == ["file:///keep-me"]
    backup = Path(result.backup_path)
    assert backup.exists() and backup != tmp_path / "m.db"
    # the backup is a faithful copy of the database as it was BEFORE the upgrade
    restored = sqlite3.connect(backup)
    try:
        assert restored.execute("SELECT version_num FROM alembic_version").fetchone()[0] == PREVIOUS_REVISION
        assert restored.execute("SELECT url FROM repositories").fetchall() == [("file:///keep-me",)]
    finally:
        restored.close()
    assert before_hash != _file_hash(tmp_path), "sanity: the live database did change"


def test_no_backup_skips_the_copy(tmp_path):
    run_migrations(db_url=_url(tmp_path), revision=PREVIOUS_REVISION)
    result = migrate_database(_url(tmp_path), backup=False)
    assert result.action == "upgraded" and result.backup_path is None
    assert list(tmp_path.glob("*.bak")) == []


def test_migrating_a_current_database_changes_nothing_and_makes_no_backup(tmp_path):
    run_migrations(db_url=_url(tmp_path))
    before = _file_hash(tmp_path)

    result = migrate_database(_url(tmp_path))

    assert result.action == "up-to-date"
    assert _file_hash(tmp_path) == before
    assert list(tmp_path.glob("*.bak")) == []


def test_an_unstamped_database_whose_schema_matches_is_stamped_without_touching_its_data(tmp_path):
    _create_all_unstamped(_url(tmp_path))
    _seed_repo(_url(tmp_path), "file:///legacy")

    result = migrate_database(_url(tmp_path))

    assert result.action == "stamped"
    assert get_current_revision(_url(tmp_path)) == head_revision()
    assert _repo_urls(_url(tmp_path)) == ["file:///legacy"]
    assert result.backup_path and Path(result.backup_path).exists()


def test_a_diverged_unstamped_database_is_refused_and_left_exactly_as_it_was(tmp_path):
    _create_all_unstamped(_url(tmp_path))
    engine = create_engine(_url(tmp_path))
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE module_edges"))
    engine.dispose()
    before = _file_hash(tmp_path)

    with pytest.raises(MigrationRefused) as excinfo:
        migrate_database(_url(tmp_path))

    assert "module_edges" in str(excinfo.value)
    assert _file_hash(tmp_path) == before
    assert get_current_revision(_url(tmp_path)) is None
    assert list(tmp_path.glob("*.bak")) == []


def test_a_database_from_a_newer_codepulse_is_refused_and_left_alone(tmp_path):
    run_migrations(db_url=_url(tmp_path))
    engine = create_engine(_url(tmp_path))
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = 'ffffffffffff'"))
    engine.dispose()
    before = _file_hash(tmp_path)

    with pytest.raises(MigrationRefused):
        migrate_database(_url(tmp_path))

    assert _file_hash(tmp_path) == before


def test_check_only_reports_and_never_changes_anything(tmp_path):
    run_migrations(db_url=_url(tmp_path), revision=PREVIOUS_REVISION)
    before = _file_hash(tmp_path)

    result = migrate_database(_url(tmp_path), check=True)

    assert result.action == "needs-upgrade"
    assert _file_hash(tmp_path) == before
    assert list(tmp_path.glob("*.bak")) == []


def test_migrating_twice_is_idempotent(tmp_path):
    migrate_database(_url(tmp_path))
    again = migrate_database(_url(tmp_path))
    assert again.action == "up-to-date"


# --- StorageManager ----------------------------------------------------------

def test_a_database_storagemanager_creates_from_scratch_is_tracked_at_head(tmp_path):
    StorageManager(db_url=_url(tmp_path)).engine.dispose()
    assert get_current_revision(_url(tmp_path)) == head_revision()
    assert get_schema_status(_url(tmp_path)).state == "current"


def test_a_legacy_unstamped_database_with_tables_is_not_stamped_by_storagemanager(tmp_path):
    _create_all_unstamped(_url(tmp_path))
    StorageManager(db_url=_url(tmp_path)).engine.dispose()
    assert get_current_revision(_url(tmp_path)) is None, "stamping is a decision `codepulse migrate` makes after checking the schema"


def test_storagemanager_leaves_a_tracked_database_at_its_revision(tmp_path):
    run_migrations(db_url=_url(tmp_path), revision=PREVIOUS_REVISION)
    StorageManager(db_url=_url(tmp_path)).engine.dispose()
    assert get_current_revision(_url(tmp_path)) == PREVIOUS_REVISION


# --- the CLI -----------------------------------------------------------------

def _run_cli(monkeypatch, capsys, *argv):
    monkeypatch.setattr("sys.argv", ["codepulse", *argv])
    code = 0
    try:
        cli.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_migrate_creates_upgrades_and_reports_json(tmp_path, monkeypatch, capsys):
    url = _url(tmp_path)
    code, out, _ = _run_cli(monkeypatch, capsys, "--db-url", url, "migrate", "--json")
    assert code == 0 and json.loads(out)["action"] == "created"

    run_migrations(db_url=_url(tmp_path, "b.db"), revision=PREVIOUS_REVISION)
    code, out, _ = _run_cli(monkeypatch, capsys, "--db-url", _url(tmp_path, "b.db"), "migrate", "--json")
    payload = json.loads(out)
    assert code == 0 and payload["action"] == "upgraded" and payload["to"] == head_revision()
    assert payload["backup"] and Path(payload["backup"]).exists()


def test_cli_migrate_check_exit_codes(tmp_path, monkeypatch, capsys):
    run_migrations(db_url=_url(tmp_path, "cur.db"))
    code, _, _ = _run_cli(monkeypatch, capsys, "--db-url", _url(tmp_path, "cur.db"), "migrate", "--check")
    assert code == 0

    run_migrations(db_url=_url(tmp_path, "old.db"), revision=PREVIOUS_REVISION)
    code, _, _ = _run_cli(monkeypatch, capsys, "--db-url", _url(tmp_path, "old.db"), "migrate", "--check")
    assert code == 1, "needs an upgrade"


def test_cli_migrate_refuses_a_diverged_database_with_a_nonzero_exit_and_the_differences(tmp_path, monkeypatch, capsys):
    _create_all_unstamped(_url(tmp_path))
    engine = create_engine(_url(tmp_path))
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE module_edges"))
    engine.dispose()
    before = _file_hash(tmp_path)

    code, out, err = _run_cli(monkeypatch, capsys, "--db-url", _url(tmp_path), "migrate")

    assert code == 1
    assert "module_edges" in out + err
    assert _file_hash(tmp_path) == before


def test_other_commands_warn_on_stderr_when_the_database_is_behind(tmp_path, monkeypatch, capsys):
    run_migrations(db_url=_url(tmp_path), revision=PREVIOUS_REVISION)
    code, out, err = _run_cli(monkeypatch, capsys, "--db-url", _url(tmp_path), "doctor", "--json")
    assert "codepulse migrate" in err, "the warning goes to stderr"
    json.loads(out)  # stdout must stay valid JSON (the doctor report itself mentions the schema; the warning line is not on stdout)
    assert "⚠" not in out and "Database schema is" not in out.split('"database_schema"')[0]


def test_no_warning_when_the_database_is_current(tmp_path, monkeypatch, capsys):
    run_migrations(db_url=_url(tmp_path))
    _, _, err = _run_cli(monkeypatch, capsys, "--db-url", _url(tmp_path), "doctor", "--json")
    assert "codepulse migrate" not in err


def test_doctor_reports_the_schema_state(tmp_path, monkeypatch, capsys):
    run_migrations(db_url=_url(tmp_path), revision=PREVIOUS_REVISION)
    _, out, _ = _run_cli(monkeypatch, capsys, "--db-url", _url(tmp_path), "doctor", "--json")
    checks = json.loads(out)
    rows = checks["checks"] if isinstance(checks, dict) and "checks" in checks else checks
    schema = next(r for r in rows if r.get("check") == "database_schema")
    assert "behind" in schema["detail"].lower() or "migrate" in schema["detail"].lower()


# --- packaged migrations (finding K) ------------------------------------------

def test_the_alembic_config_does_not_depend_on_the_repository_root_or_the_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = get_alembic_config(_url(tmp_path))
    script_dir = Path(cfg.get_main_option("script_location"))
    package_dir = Path(cli.__file__).resolve().parent

    assert script_dir.is_dir()
    assert package_dir in script_dir.resolve().parents, "the scripts must live INSIDE the codepulse package so they ship with it"
    assert cfg.config_file_name is None, "no alembic.ini is needed"
    assert ScriptDirectory.from_config(cfg).get_heads() == [head_revision()]


def test_running_migrations_does_not_disable_existing_loggers(tmp_path):
    """alembic.ini's logging section used to be applied by every migration run, which disables every logger
    that already exists (the engine's, the API's) and resets the root handlers."""
    import logging

    logger = logging.getLogger("codepulse.indexer.engine")
    before = (logger.disabled, list(logging.getLogger().handlers))
    run_migrations(db_url=_url(tmp_path))
    assert (logger.disabled, list(logging.getLogger().handlers)) == before


def test_the_build_includes_the_migration_scripts():
    """The wheel used to ship no migrations (alembic.ini and the scripts sat outside the package)."""
    from setuptools import find_namespace_packages

    root = Path(__file__).resolve().parents[1]
    found = set(find_namespace_packages(where=str(root), include=["codepulse*"]))
    assert "codepulse.storage.schema_migrations" in found
    assert "codepulse.storage.schema_migrations.versions" in found
    text_ = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "schema_migrations" in text_, "package-data must also list the non-.py files (script.py.mako)"
    assert not (root / "alembic").exists(), "the old top-level alembic/ folder must be gone"
