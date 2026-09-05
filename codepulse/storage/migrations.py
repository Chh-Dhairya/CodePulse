from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from alembic.config import Config
from alembic import command
from sqlalchemy import create_engine, inspect

from codepulse.config import get_settings


def get_alembic_config(db_url: str | None = None) -> Config:
    """
    Constructs and returns an Alembic Config object configured with the project's alembic.ini
    and target database URL.
    """
    project_root = Path(__file__).resolve().parent.parent.parent
    ini_path = project_root / "alembic.ini"
    alembic_cfg = Config(str(ini_path))

    target_url = db_url or get_settings().db_url
    alembic_cfg.set_main_option("sqlalchemy.url", target_url)
    return alembic_cfg


def run_migrations(db_url: str | None = None, revision: str = "head") -> None:
    """
    Applies Alembic migrations up to the specified revision (default: head).
    """
    cfg = get_alembic_config(db_url=db_url)
    command.upgrade(cfg, revision)


def stamp_database(db_url: str | None = None, revision: str = "head") -> None:
    """
    Stamps the database at the specified revision without running migration scripts.
    Useful for bringing an existing database with matching schema under Alembic management.
    """
    cfg = get_alembic_config(db_url=db_url)
    command.stamp(cfg, revision)


def downgrade_migrations(db_url: str | None = None, revision: str = "base") -> None:
    """
    Downgrades Alembic migrations to the specified revision (default: base).
    """
    cfg = get_alembic_config(db_url=db_url)
    command.downgrade(cfg, revision)


def get_current_revision(db_url: str | None = None) -> Optional[str]:
    """
    Inspects the database and returns the current alembic_version revision string, or None if not stamped.
    """
    target_url = db_url or get_settings().db_url
    engine = create_engine(target_url)
    try:
        insp = inspect(engine)
        if "alembic_version" not in insp.get_table_names():
            return None
        with engine.connect() as conn:
            from sqlalchemy import text
            result = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            return result
    finally:
        engine.dispose()
