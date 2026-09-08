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
    __table_args__ = (
        # Natural key for embedding identity (Phase 0.5), per the roadmap's
        # Phase 0 idempotency rule (§8): "(symbol_id, chunk_type,
        # chunk_index)". Enforced at the DB level so a duplicate Embedding
        # row can never be created for the same chunk of the same symbol.
        UniqueConstraint(
            "symbol_id", "chunk_type", "chunk_index", name="uq_code_embeddings_symbol_chunk"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol_id = Column(Integer, ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False)
    chunk_type = Column(String(50), default="signature_doc")  # 'signature_doc', 'body_chunk', 'full'
    # Position of this chunk among a symbol's chunks (see
    # codepulse.embeddings.chunker.CodeChunk.chunk_index); part of the
    # natural key together with (symbol_id, chunk_type).
    chunk_index = Column(Integer, nullable=False, default=0)
    embedding_json = Column(Text, nullable=False)  # JSON serialized array of floats for portability
    chunk_content = Column(Text, default="")
    # SHA-256 hex digest of `chunk_content` (see codepulse.models.compute_content_hash),
    # used for change detection: reuse the row if unchanged, update in place
    # if the chunk's source content changed. Nullable because historical
    # rows created before Phase 0.5 have no recorded hash.
    content_hash = Column(String(64), nullable=True)
    # Identity of the model that produced this vector, and its dimension --
    # recorded so hydration can detect a model/dimension change and skip
    # loading an incompatible vector rather than silently corrupting the
    # in-memory vector index (roadmap §9: "embedding dimension/model must be
    # recorded"). Nullable for the same reason as content_hash.
    embedding_model = Column(String(150), nullable=True)
    embedding_dim = Column(Integer, nullable=True)

    symbol = relationship("SymbolModel", back_populates="embeddings")


class APIKeyModel(Base):
    """
    Phase 1.1 (roadmap §15, §37 item 1.1): API key model + hashing +
    creation flow.

    The raw key is NEVER stored -- only its SHA-256 hash (`key_hash`, see
    codepulse.auth.hash_api_key) and a short, non-secret prefix
    (`key_prefix`) kept purely for display/identification in a future
    key-management UI/CLI. This is Phase 1.1 scope only: the model,
    hashing, and a creation (+ verification-primitive) flow. Enforcing
    this at the API boundary (a FastAPI dependency required on every
    route) is Phase 1.2, deliberately not implemented here. Role-based
    permissions (viewer/indexer/admin, roadmap §15) are Phase 7 (B25)
    scope and are not modeled yet -- no `role` column exists on this table.
    """
    __tablename__ = "api_keys"
    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    # SHA-256 hex digest of the raw key (codepulse.auth.hash_api_key). A
    # fast cryptographic hash (not a slow/salted password hash) is
    # appropriate here because the input is already a high-entropy,
    # randomly generated secret (codepulse.auth.generate_raw_api_key), not
    # a user-chosen low-entropy password -- and a fast hash keeps
    # lookup-by-hash a simple indexed equality query for Phase 1.2's
    # authorization dependency.
    key_hash = Column(String(64), nullable=False)
    # Short, non-secret prefix of the raw key (e.g. "cpk_a1b2c3d4"), kept
    # only so a key can be identified/distinguished in a future listing
    # UI/CLI without ever re-displaying the secret itself.
    key_prefix = Column(String(32), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    # Nullable revocation timestamp: schema support only in Phase 1.1 (so
    # a later revocation milestone doesn't need its own migration) -- no
    # revoke() flow is implemented yet; codepulse.auth.verify_api_key does
    # respect this field defensively (a revoked key never verifies).
    revoked_at = Column(DateTime, nullable=True)

    repositories = relationship(
        "APIKeyRepositoryModel", back_populates="api_key", cascade="all, delete-orphan"
    )


class APIKeyRepositoryModel(Base):
    """
    Scopes an APIKey to a specific repository (roadmap §15: "API keys
    (hashed at rest, scoped to one or more repositories)"). One row per
    (api_key, repository) pair a key is permitted to access. This table
    only records the grant -- Phase 1.1 does not read or enforce it
    anywhere; that's Phase 1.2's central authorization dependency.
    """
    __tablename__ = "api_key_repositories"
    __table_args__ = (
        UniqueConstraint("api_key_id", "repo_id", name="uq_api_key_repositories_key_repo"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    api_key_id = Column(Integer, ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False)
    repo_id = Column(Integer, ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False)

    api_key = relationship("APIKeyModel", back_populates="repositories")


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
