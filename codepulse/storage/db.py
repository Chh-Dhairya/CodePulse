from __future__ import annotations

import json
from datetime import datetime
from typing import Optional
from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Text,
    DateTime,
    ForeignKey,
    Float,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session

Base = declarative_base()


class RepositoryModel(Base):
    __tablename__ = "repositories"
    __table_args__ = (
        # Natural key for repository identity (Phase 0.3): a repository is
        # identified by its `url` (the git URL, or the absolute local path
        # when indexing a local directory with no explicit URL -- see
        # IndexingEngine.index_directory). Enforced at the DB level so a
        # duplicate Repository row can never be created for the same
        # logical repository, even under a bug or a race.
        UniqueConstraint("url", name="uq_repositories_url"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    team_id = Column(Integer, default=1, nullable=False)
    name = Column(String(255), nullable=False)
    url = Column(Text, nullable=False)
    branch = Column(String(100), default="main")
    last_sync = Column(DateTime, default=datetime.utcnow)
    file_count = Column(Integer, default=0)
    symbol_count = Column(Integer, default=0)

    files = relationship("FileModel", back_populates="repository", cascade="all, delete-orphan")


class FileModel(Base):
    __tablename__ = "files"
    __table_args__ = (
        # Natural key for file identity (Phase 0.3): a file is identified by
        # (repo_id, path). Enforced at the DB level so a duplicate File row
        # can never be created for the same path within the same repository.
        UniqueConstraint("repo_id", "path", name="uq_files_repo_id_path"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    repo_id = Column(Integer, ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False)
    path = Column(Text, nullable=False)
    language = Column(String(50), nullable=False)
    size_bytes = Column(Integer, default=0)
    line_count = Column(Integer, default=0)
    # SHA-256 hex digest of the file's content (see codepulse.models.compute_content_hash).
    # Nullable because historical rows created before Phase 0.3 have no recorded
    # original content to hash (see the Phase 0.3 migration for details) --
    # such rows get a real hash the next time the file is (re-)indexed.
    content_hash = Column(String(64), nullable=True)
    last_indexed = Column(DateTime, default=datetime.utcnow)

    repository = relationship("RepositoryModel", back_populates="files")
    symbols = relationship("SymbolModel", back_populates="file", cascade="all, delete-orphan")


class SymbolModel(Base):
    __tablename__ = "symbols"
    __table_args__ = (
        # Natural key for symbol identity (Phase 0.4): a symbol is identified
        # by (file_id, name, start_line), per the roadmap's Phase 0
        # idempotency rule. Enforced at the DB level so a duplicate Symbol
        # row can never be created for the same symbol within the same file.
        UniqueConstraint(
            "file_id", "name", "start_line", name="uq_symbols_file_id_name_start_line"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    file_id = Column(Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False, index=True)
    type = Column(String(50), nullable=False)
    start_line = Column(Integer, nullable=False)
    end_line = Column(Integer, nullable=False)
    signature = Column(Text, default="")
    docstring = Column(Text, nullable=True)
    body = Column(Text, default="")
    body_hash = Column(String(64), default="")
    language = Column(String(50), nullable=False)
    complexity = Column(Integer, default=1)

    file = relationship("FileModel", back_populates="symbols")
    embeddings = relationship("EmbeddingModel", back_populates="symbol", cascade="all, delete-orphan")


class DependencyModel(Base):
    __tablename__ = "dependencies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_symbol_name = Column(String(255), nullable=False, index=True)
    source_file = Column(Text, nullable=False)
    target_symbol_name = Column(String(255), nullable=False, index=True)
    target_file = Column(Text, nullable=True)
    call_count = Column(Integer, default=1)


class EmbeddingModel(Base):
    __tablename__ = "code_embeddings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol_id = Column(Integer, ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False)
    chunk_type = Column(String(50), default="signature_doc")  # 'signature_doc', 'body_chunk', 'full'
    embedding_json = Column(Text, nullable=False)  # JSON serialized array of floats for portability
    chunk_content = Column(Text, default="")

    symbol = relationship("SymbolModel", back_populates="embeddings")


class StorageManager:
    """Manages database connection, tables, and session lifecycle."""

    def __init__(self, db_url: str | None = None) -> None:
        if db_url is None:
            from codepulse.config import get_settings
            db_url = get_settings().db_url
        self.db_url = db_url
        self.engine = create_engine(db_url, echo=False)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.create_tables()

    def create_tables(self) -> None:
        Base.metadata.create_all(bind=self.engine)

    def get_session(self) -> Session:
        return self.SessionLocal()
