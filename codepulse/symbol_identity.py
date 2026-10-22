"""
The one place that decides what a symbol's in-memory id looks like.

FUTURE_CHANGES item 31: the ids used to be `repo::file::name`, so same-named
symbols in one file (every class's `__init__`, `run`, `get`, property setters,
overloads) shared one id. The roadmap (section 29, idempotency rule) defines a
symbol's natural key as `(file_id, name, start_line)` and the database enforces
it; these ids now carry the same three parts, plus the repository.

An id is a pure function of the symbol, so it can be rebuilt from a database row
alone (after a restart, or when purging a file's old documents) with no knowledge
of the symbol's siblings. Ids exist only in memory (the lexical and vector indexes
are rebuilt from the database at startup); nothing persists them.

A symbol whose lines shift gets a new id, which matches how incremental indexing
already treats it (the same `(name, start_line)` key: the old row is replaced and
the symbol is re-chunked and re-embedded).
"""
from __future__ import annotations


def symbol_doc_id(repo_id: int, file_path: str, name: str, start_line: int) -> str:
    """Id of a symbol in the lexical index, the in-memory symbol maps and result lists."""
    return f"{repo_id}::{file_path}::{name}@{start_line}"


def chunk_doc_id(repo_id: int, file_path: str, name: str, start_line: int, chunk_index: int) -> str:
    """Id of one embedded chunk of a symbol in the vector index."""
    return f"{symbol_doc_id(repo_id, file_path, name, start_line)}::{chunk_index}"
