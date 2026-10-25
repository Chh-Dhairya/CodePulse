"""
Where CodePulse keeps its database by default.

The default used to be `sqlite:///codepulse.db`, resolved against the CURRENT WORKING DIRECTORY. Anything that starts
the server or the CLI from inside a project (the VS Code extension does exactly that) therefore dropped a
`codepulse.db` into that project, one database per folder you ever ran it in, and a CLI run from another folder used
yet another. Now there is ONE database per user, in the operating system's per-user data directory, with two guards:

  - an explicit setting always wins (CLI flag, CODEPULSE_DB_URL, codepulse.toml: unchanged precedence);
  - if the working directory already holds a `codepulse.db`, that one is kept (the previous default), so an existing
    setup keeps working exactly as before and nobody's data is silently orphaned.

`CODEPULSE_DATA_DIR` overrides the directory (containers, tests, portable installs).

Pure functions (platform, environment, home and working directory are injectable) with no side effects, except
`ensure_database_directory`, which creates ONLY the per-user data directory, never a directory the user typed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping

DATABASE_FILENAME = "codepulse.db"
LEGACY_DATABASE_URL = f"sqlite:///{DATABASE_FILENAME}"
_SQLITE_PREFIX = "sqlite:///"


def user_data_dir(
    *, platform: str | None = None, env: Mapping[str, str] | None = None, home: Path | None = None
) -> Path:
    """The per-user directory CodePulse keeps its data in (not created here)."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    home = Path.home() if home is None else home

    explicit = env.get("CODEPULSE_DATA_DIR")
    if explicit:
        return Path(explicit)
    if platform == "win32":
        base = Path(env["LOCALAPPDATA"]) if env.get("LOCALAPPDATA") else home / "AppData" / "Local"
        return base / "CodePulse"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "CodePulse"
    base = Path(env["XDG_DATA_HOME"]) if env.get("XDG_DATA_HOME") else home / ".local" / "share"
    return base / "codepulse"


def default_database_url(
    *,
    cwd: Path | None = None,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> str:
    """The database URL used when nothing was configured (see the module docstring)."""
    cwd = Path.cwd() if cwd is None else cwd
    if (cwd / DATABASE_FILENAME).is_file():
        return LEGACY_DATABASE_URL
    return _SQLITE_PREFIX + (user_data_dir(platform=platform, env=env, home=home) / DATABASE_FILENAME).as_posix()


def ensure_database_directory(db_url: str) -> None:
    """
    Creates the per-user data directory if `db_url` is a SQLite file inside it and it does not exist yet.
    Any other missing directory is left alone: a path the user typed that does not exist is still an error.
    """
    if not db_url.startswith(_SQLITE_PREFIX):
        return
    raw = db_url[len(_SQLITE_PREFIX):].split("?", 1)[0]
    if raw in ("", ":memory:"):
        return
    parent = Path(os.path.abspath(raw)).parent
    if parent.exists():
        return
    if os.path.normcase(os.path.abspath(parent)) == os.path.normcase(os.path.abspath(user_data_dir())):
        parent.mkdir(parents=True, exist_ok=True)
