"""
Where the database lives by default (found while preparing the VS Code extension for publication).

The default was `sqlite:///codepulse.db`, resolved against the CURRENT WORKING DIRECTORY. The extension starts the
server with the opened project as its working directory, so a `codepulse.db` appeared inside every project a user
opened (to be committed by accident), each project got its own database, and the CLI run from a different folder used
yet another one. The default is now ONE database per user, in the operating system's per-user data directory, with two
guards so nobody's existing data is silently orphaned or surprised:

  - an explicit setting always wins (CLI flag, CODEPULSE_DB_URL, codepulse.toml: unchanged precedence);
  - if the working directory already holds a `codepulse.db`, that one is kept (the previous default), so existing
    setups keep working exactly as before.

Everything here injects the platform, environment, home and working directory; nothing touches a real user directory
or a real `codepulse.db`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from codepulse.config import CodePulseConfig, load_config, reset_settings
from codepulse.data_location import (
    LEGACY_DATABASE_URL,
    default_database_url,
    ensure_database_directory,
    user_data_dir,
)
from codepulse.storage.db import StorageManager


@pytest.fixture(autouse=True)
def _clean_settings():
    reset_settings()
    yield
    reset_settings()


# --- the per-user directory ---------------------------------------------------------------------------------------


def test_on_windows_it_is_under_local_app_data(tmp_path):
    local = tmp_path / "AppData" / "Local"
    assert user_data_dir(platform="win32", env={"LOCALAPPDATA": str(local)}, home=tmp_path) == local / "CodePulse"


def test_on_windows_without_local_app_data_it_falls_back_under_the_home_folder(tmp_path):
    assert user_data_dir(platform="win32", env={}, home=tmp_path) == tmp_path / "AppData" / "Local" / "CodePulse"


def test_on_macos_it_is_under_application_support(tmp_path):
    assert user_data_dir(platform="darwin", env={}, home=tmp_path) == tmp_path / "Library" / "Application Support" / "CodePulse"


def test_on_linux_it_follows_xdg_data_home(tmp_path):
    xdg = tmp_path / "xdg"
    assert user_data_dir(platform="linux", env={"XDG_DATA_HOME": str(xdg)}, home=tmp_path) == xdg / "codepulse"


def test_on_linux_without_xdg_it_is_under_local_share(tmp_path):
    assert user_data_dir(platform="linux", env={}, home=tmp_path) == tmp_path / ".local" / "share" / "codepulse"


def test_an_explicit_data_dir_overrides_the_platform_choice(tmp_path):
    chosen = tmp_path / "somewhere"
    for platform in ("win32", "darwin", "linux"):
        assert user_data_dir(platform=platform, env={"CODEPULSE_DATA_DIR": str(chosen)}, home=tmp_path) == chosen


# --- the default database URL ---------------------------------------------------------------------------------------


def test_the_default_is_one_database_in_the_user_data_directory(tmp_path):
    cwd = tmp_path / "project"
    cwd.mkdir()
    data = tmp_path / "data"
    url = default_database_url(cwd=cwd, env={"CODEPULSE_DATA_DIR": str(data)}, home=tmp_path)
    assert url == f"sqlite:///{(data / 'codepulse.db').as_posix()}"
    assert not (cwd / "codepulse.db").exists(), "computing the default must not create anything"
    assert not data.exists(), "computing the default must not create anything"


def test_the_default_does_not_depend_on_which_directory_you_are_in(tmp_path):
    env = {"CODEPULSE_DATA_DIR": str(tmp_path / "data")}
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert default_database_url(cwd=a, env=env, home=tmp_path) == default_database_url(cwd=b, env=env, home=tmp_path)


def test_an_existing_database_in_the_working_directory_is_kept(tmp_path):
    """Existing setups (and the developer's own database) must keep working exactly as before."""
    cwd = tmp_path / "project"
    cwd.mkdir()
    (cwd / "codepulse.db").write_bytes(b"")
    url = default_database_url(cwd=cwd, env={"CODEPULSE_DATA_DIR": str(tmp_path / "data")}, home=tmp_path)
    assert url == LEGACY_DATABASE_URL == "sqlite:///codepulse.db"


def test_a_directory_named_codepulse_db_is_not_mistaken_for_a_database(tmp_path):
    cwd = tmp_path / "project"
    (cwd / "codepulse.db").mkdir(parents=True)
    url = default_database_url(cwd=cwd, env={"CODEPULSE_DATA_DIR": str(tmp_path / "data")}, home=tmp_path)
    assert url != LEGACY_DATABASE_URL


def test_the_url_uses_forward_slashes_even_for_a_windows_style_path(tmp_path):
    url = default_database_url(cwd=tmp_path, env={"CODEPULSE_DATA_DIR": str(tmp_path / "d e")}, home=tmp_path)
    assert "\\" not in url
    assert url.startswith("sqlite:///")


# --- it is what the configuration uses ------------------------------------------------------------------------------


def test_the_configuration_default_is_computed_not_fixed(tmp_path, monkeypatch):
    data = tmp_path / "data"
    cwd = tmp_path / "project"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("CODEPULSE_DATA_DIR", str(data))
    assert CodePulseConfig().db_url == f"sqlite:///{(data / 'codepulse.db').as_posix()}"
    (cwd / "codepulse.db").write_bytes(b"")
    assert CodePulseConfig().db_url == "sqlite:///codepulse.db"


def test_an_explicit_setting_still_beats_the_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEPULSE_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "codepulse.db").write_bytes(b"")
    assert load_config(config_file="/nonexistent.toml", cli_overrides={"db_url": "sqlite:///chosen.db"}).db_url == "sqlite:///chosen.db"
    monkeypatch.setenv("CODEPULSE_DB_URL", "sqlite:///from_env.db")
    assert load_config(config_file="/nonexistent.toml").db_url == "sqlite:///from_env.db"


# --- creating the directory -------------------------------------------------------------------------------------------


def test_the_data_directory_is_created_when_a_database_is_opened_there(tmp_path, monkeypatch):
    data = tmp_path / "deep" / "data"
    monkeypatch.setenv("CODEPULSE_DATA_DIR", str(data))
    url = f"sqlite:///{(data / 'codepulse.db').as_posix()}"
    assert not data.exists()

    StorageManager(db_url=url)

    assert (data / "codepulse.db").is_file()


def test_an_explicit_path_in_a_missing_directory_is_not_silently_created(tmp_path, monkeypatch):
    """Only the per-user directory is created on demand; a path the user typed that does not exist is still an error."""
    monkeypatch.setenv("CODEPULSE_DATA_DIR", str(tmp_path / "data"))
    url = f"sqlite:///{(tmp_path / 'typo' / 'codepulse.db').as_posix()}"
    with pytest.raises(Exception):
        StorageManager(db_url=url)
    assert not (tmp_path / "typo").exists()


def test_ensure_database_directory_ignores_non_sqlite_and_memory_urls(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPULSE_DATA_DIR", str(tmp_path / "data"))
    for url in ("sqlite:///:memory:", "sqlite://", "postgresql://user@host/db"):
        ensure_database_directory(url)
    assert not (tmp_path / "data").exists()


def test_init_writes_an_explicit_project_local_database_setting(tmp_path):
    """`init` is the opt-in to a project-local setup: it must say so, and the line must really keep the database there."""
    import argparse

    import codepulse.cli as cli

    target = tmp_path / "proj"
    cli.cmd_init(argparse.Namespace(path=str(target), force=False))
    content = (target / "codepulse.toml").read_text(encoding="utf-8")
    assert "per-user data directory" in content
    assert load_config(config_file=target / "codepulse.toml").db_url == "sqlite:///codepulse.db"
