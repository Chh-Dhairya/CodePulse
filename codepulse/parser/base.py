from __future__ import annotations

from abc import ABC, abstractmethod
from codepulse.models import FileParseResult, ImportStatement, Symbol, CallReference


class LanguageParser(ABC):
    """Abstract Base Class for language-specific AST parsers."""

    @property
    @abstractmethod
    def language(self) -> str:
        """Returns the canonical language identifier (e.g. 'python', 'javascript')."""
        pass

    @abstractmethod
    def extract_symbols(self, source_code: str, file_path: str = "") -> list[Symbol]:
        """Extract functions, classes, methods, and types with their signatures and docstrings."""
        pass

    @abstractmethod
    def extract_imports(self, source_code: str, file_path: str = "") -> list[ImportStatement]:
        """Extract module imports and imported identifiers."""
        pass

    @abstractmethod
    def extract_calls(
        self, source_code: str, file_path: str = "", symbols: list[Symbol] | None = None
    ) -> list[CallReference]:
        """Extract function/method invocations mapped to their enclosing caller symbols."""
        pass

    def parse_file(self, file_path: str, source_code: str) -> FileParseResult:
        """Parse source code completely and return aggregated FileParseResult."""
        symbols = self.extract_symbols(source_code, file_path=file_path)
        imports = self.extract_imports(source_code, file_path=file_path)
        calls = self.extract_calls(source_code, file_path=file_path, symbols=symbols)
        
        line_count = len(source_code.splitlines())
        file_size_bytes = len(source_code.encode("utf-8"))

        return FileParseResult(
            file_path=file_path,
            language=self.language,
            symbols=symbols,
            imports=imports,
            calls=calls,
            file_size_bytes=file_size_bytes,
            line_count=line_count,
        )
