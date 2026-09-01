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
    create_engine,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session

Base = declarative_base()


class RepositoryModel(Base):
    __tablename__ = "repositories"

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

    id = Column(Integer, primary_key=True, autoincrement=True)
    repo_id = Column(Integer, ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False)
    path = Column(Text, nullable=False)
    language = Column(String(50), nullable=False)
    size_bytes = Column(Integer, default=0)
    line_count = Column(Integer, default=0)
    last_indexed = Column(DateTime, default=datetime.utcnow)

    repository = relationship("RepositoryModel", back_populates="files")
    symbols = relationship("SymbolModel", back_populates="file", cascade="all, delete-orphan")


class SymbolModel(Base):
    __tablename__ = "symbols"

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
