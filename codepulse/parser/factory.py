from __future__ import annotations

import os
from codepulse.parser.base import LanguageParser
from codepulse.parser.python import PythonParser
from codepulse.parser.javascript import JavaScriptParser

_PARSERS: dict[str, LanguageParser] = {}


def get_parser_for_language(language: str) -> LanguageParser:
    """Retrieve a cached parser instance for the requested language."""
    lang_lower = language.lower().strip()
    if lang_lower not in _PARSERS:
        if lang_lower in ("python", "py"):
            _PARSERS[lang_lower] = PythonParser()
        elif lang_lower in ("javascript", "js"):
            _PARSERS[lang_lower] = JavaScriptParser(is_typescript=False)
        elif lang_lower in ("typescript", "ts"):
            _PARSERS[lang_lower] = JavaScriptParser(is_typescript=True)
        else:
            raise ValueError(f"Unsupported language: '{language}'")
    return _PARSERS[lang_lower]


def get_parser_for_file(file_path: str) -> LanguageParser | None:
    """Determine the appropriate parser based on file extension."""
    _, ext = os.path.splitext(file_path)
    ext_lower = ext.lower()

    if ext_lower == ".py":
        return get_parser_for_language("python")
    elif ext_lower in (".js", ".jsx", ".mjs", ".cjs"):
        return get_parser_for_language("javascript")
    elif ext_lower in (".ts", ".tsx", ".mts", ".cts"):
        return get_parser_for_language("typescript")
    return None
