from __future__ import annotations

import re
from typing import Sequence

# Regex to split camelCase / PascalCase and snake_case tokens
_CAMEL_SPLIT_REGEX = re.compile(r"([a-z0-9])([A-Z])|([A-Z]+)([A-Z][a-z])")
_NON_ALPHANUM_REGEX = re.compile(r"[^a-zA-Z0-9_]")


def split_identifier(identifier: str) -> list[str]:
    """
    Splits a programming identifier into its constituent words.
    
    Examples:
        'createDbConnection' -> ['create', 'Db', 'Connection']
        'POSTGRES_POOL_SIZE' -> ['POSTGRES', 'POOL', 'SIZE']
        'ASTVisitorV2' -> ['AST', 'Visitor', 'V2']
        'get_user_by_id' -> ['get', 'user', 'by', 'id']
    """
    if not identifier:
        return []

    # First split by underscores
    parts = identifier.split("_")
    sub_words: list[str] = []

    for part in parts:
        if not part:
            continue
        # Split camelCase / PascalCase
        # e.g., 'createDbConnection' -> 'create Db Connection'
        s = _CAMEL_SPLIT_REGEX.sub(r"\1\3 \2\4", part)
        for w in s.split():
            if w:
                sub_words.append(w)

    return sub_words


def tokenize_code(text: str, preserve_case: bool = False) -> list[str]:
    """
    Code-aware tokenizer for BM25 and lexical search.
    
    Produces both:
    1. Full raw tokens (e.g., 'createDbConnection')
    2. Split sub-tokens (e.g., 'create', 'db', 'connection')
    
    This ensures that searches for both exact identifiers ('createDbConnection')
    and sub-components ('connection') match with high relevance.
    """
    if not text:
        return []

    # Replace non-alphanumeric (except underscores) with spaces
    cleaned = _NON_ALPHANUM_REGEX.sub(" ", text)
    raw_tokens = cleaned.split()

    tokens: list[str] = []
    seen: set[str] = set()

    for raw in raw_tokens:
        # Filter out purely numeric or single-character noisy tokens
        if len(raw) == 1 and not raw.isalpha():
            continue

        base = raw if preserve_case else raw.lower()
        if base not in seen:
            tokens.append(base)
            seen.add(base)

        # Generate sub-tokens
        sub_tokens = split_identifier(raw)
        for st in sub_tokens:
            sub = st if preserve_case else st.lower()
            if sub and len(sub) > 1 and sub not in seen:
                tokens.append(sub)
                seen.add(sub)

    return tokens
