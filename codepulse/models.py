from __future__ import annotations

import hashlib
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class SymbolType(str, Enum):
    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    MODULE = "module"
    INTERFACE = "interface"
    VARIABLE = "variable"


class Symbol(BaseModel):
    """Represents an extracted code symbol (function, method, class, etc.)."""
    name: str
    type: SymbolType
    file_path: str
    start_line: int = Field(ge=1, description="1-indexed starting line")
    end_line: int = Field(ge=1, description="1-indexed ending line")
    signature: str = ""
    docstring: Optional[str] = None
    body: str = ""
    body_hash: str = ""
    language: str
    parent_symbol: Optional[str] = None
    complexity: int = 1
    # Phase 4.1 (roadmap §37 item 4.1: "Base-class/inheritance extraction
    # (Python + JS/TS)"; §29: "Symbol... needs... base-class field").
    # Raw, unresolved base-class expressions exactly as written in source
    # (e.g. ["Base", "pkg.Mixin"]) -- populated only for CLASS symbols,
    # always empty for every other symbol type. This is the honest,
    # never-fabricated extraction; RESOLVING each name to a real symbol
    # (with confidence) is a separate, derived step performed by
    # `CallGraphBuilder` -- the exact same layering already established
    # for `CallReference.callee_name` (raw) vs. `CallGraphEdge` (resolved,
    # confidence-scored). A `keyword_argument` in a Python class's base
    # list (e.g. `metaclass=Meta`) is deliberately excluded here -- it is
    # not a base class. A TypeScript `implements` clause is also
    # deliberately excluded -- interface implementation is a different
    # relationship than base-class inheritance, not extracted in this
    # milestone.
    base_classes: list[str] = Field(default_factory=list)

    def model_post_init(self, __context) -> None:
        if not self.body_hash and self.body:
            self.body_hash = hashlib.sha256(self.body.strip().encode("utf-8")).hexdigest()


class ImportItem(BaseModel):
    """An imported item with an optional alias (e.g., `from math import sqrt as s`)."""
    name: str
    alias: Optional[str] = None


class ImportStatement(BaseModel):
    """Represents an import in source code."""
    module: str
    items: list[ImportItem] = Field(default_factory=list)
    is_relative: bool = False
    level: int = 0  # Number of leading dots in relative imports
    line_number: int = 1
    file_path: str = ""


class CallReference(BaseModel):
    """A reference where a symbol/function is invoked."""
    caller_symbol: str
    callee_name: str
    line_number: int
    file_path: str


class CallGraphEdge(BaseModel):
    """A resolved or partially resolved call graph edge between two symbols."""
    source_symbol_name: str
    source_file: str
    target_symbol_name: str
    target_file: Optional[str] = None
    call_count: int = 1
    # Phase 3.4 (roadmap §37 item 3.4; §11): "high" -- resolved through an
    # explicit import mapping to a known file+symbol; "low" -- resolved
    # only by bare-name match against same-file symbols; "unresolved" --
    # the callee couldn't be matched to any known symbol at all. Always
    # set explicitly by CallGraphBuilder._build_graph (see its own
    # comments) -- the "unresolved" default here is a safe fallback for
    # any other construction path, never itself a claim of resolution.
    confidence: str = "unresolved"
    # Phase 4.1 (roadmap §37 item 4.1): "calls" (default, matching every
    # edge this project has ever persisted before this field existed) or
    # "inherits" (a resolved base-class relationship). Always set
    # explicitly by CallGraphBuilder._build_graph for both kinds -- the
    # "calls" default here is a safe fallback for any other construction
    # path, matching this project's existing historical data exactly.
    relationship_type: str = "calls"


class FileParseResult(BaseModel):
    """Complete AST parse result for a single file."""
    file_path: str
    language: str
    symbols: list[Symbol] = Field(default_factory=list)
    imports: list[ImportStatement] = Field(default_factory=list)
    calls: list[CallReference] = Field(default_factory=list)
    file_size_bytes: int = 0
    line_count: int = 0
    content_hash: str = Field(
        default="",
        description=(
            "SHA-256 hex digest of the file's raw source content, used as the "
            "change-detection key for idempotent File upsert (Phase 0.3)."
        ),
    )


def compute_content_hash(content: str) -> str:
    """
    Computes a deterministic SHA-256 hex digest for whole-file content.

    Follows the same hashing convention already used for `Symbol.body_hash`
    (SHA-256 over UTF-8-encoded text). Unlike `Symbol.body_hash`, the content
    is NOT stripped first: a file's identity hash must reflect the exact
    persisted bytes so that whitespace-only edits still register as a real
    content change during incremental re-indexing.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
