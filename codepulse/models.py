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


class FileParseResult(BaseModel):
    """Complete AST parse result for a single file."""
    file_path: str
    language: str
    symbols: list[Symbol] = Field(default_factory=list)
    imports: list[ImportStatement] = Field(default_factory=list)
    calls: list[CallReference] = Field(default_factory=list)
    file_size_bytes: int = 0
    line_count: int = 0
