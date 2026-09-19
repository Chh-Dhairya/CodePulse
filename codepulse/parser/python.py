from __future__ import annotations

import hashlib
from typing import Any
from tree_sitter_languages import get_parser

from codepulse.models import CallReference, ImportItem, ImportStatement, Symbol, SymbolType
from codepulse.parser.base import LanguageParser


class PythonParser(LanguageParser):
    """AST parser for Python source code using Tree-sitter."""

    def __init__(self) -> None:
        self._parser = get_parser("python")

    @property
    def language(self) -> str:
        return "python"

    def extract_symbols(self, source_code: str, file_path: str = "") -> list[Symbol]:
        source_bytes = source_code.encode("utf-8")
        tree = self._parser.parse(source_bytes)
        symbols: list[Symbol] = []

        def walk(node: Any, parent_class: str | None = None) -> None:
            if node.type in ("function_definition", "async_function_definition"):
                sym = self._parse_function(node, source_bytes, file_path, parent_class)
                if sym:
                    symbols.append(sym)
                # Continue walking into function body to support nested functions/classes
                body_node = node.child_by_field_name("body")
                if body_node:
                    for child in body_node.children:
                        walk(child, parent_class=sym.name if sym else parent_class)
                return

            elif node.type == "class_definition":
                sym = self._parse_class(node, source_bytes, file_path, parent_class)
                if sym:
                    symbols.append(sym)
                body_node = node.child_by_field_name("body")
                if body_node:
                    for child in body_node.children:
                        walk(child, parent_class=sym.name if sym else parent_class)
                return

            for child in node.children:
                walk(child, parent_class)

        walk(tree.root_node)
        return symbols

    def _parse_function(
        self, node: Any, source_bytes: bytes, file_path: str, parent_class: str | None
    ) -> Symbol | None:
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None

        name = source_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        body_text = source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
        docstring = self._extract_docstring(node, source_bytes)
        signature = self._extract_signature(node, source_bytes)
        complexity = self._calculate_complexity(node)

        symbol_type = SymbolType.METHOD if parent_class else SymbolType.FUNCTION

        return Symbol(
            name=name,
            type=symbol_type,
            file_path=file_path,
            start_line=start_line,
            end_line=end_line,
            signature=signature,
            docstring=docstring,
            body=body_text,
            body_hash=hashlib.sha256(body_text.strip().encode("utf-8")).hexdigest(),
            language="python",
            parent_symbol=parent_class,
            complexity=complexity,
        )

    def _parse_class(
        self, node: Any, source_bytes: bytes, file_path: str, parent_class: str | None
    ) -> Symbol | None:
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None

        name = source_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        body_text = source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
        docstring = self._extract_docstring(node, source_bytes)
        signature = self._extract_signature(node, source_bytes)
        base_classes = self._extract_base_classes(node, source_bytes)

        return Symbol(
            name=name,
            type=SymbolType.CLASS,
            file_path=file_path,
            start_line=start_line,
            end_line=end_line,
            signature=signature,
            docstring=docstring,
            body=body_text,
            body_hash=hashlib.sha256(body_text.strip().encode("utf-8")).hexdigest(),
            language="python",
            parent_symbol=parent_class,
            complexity=1,
            base_classes=base_classes,
        )

    def _extract_base_classes(self, node: Any, source_bytes: bytes) -> list[str]:
        """
        Phase 4.1 (roadmap §37 item 4.1: "Base-class/inheritance
        extraction (Python + JS/TS)"). `class_definition`'s
        `superclasses` field is the `argument_list` in `class Foo(Base,
        pkg.Mixin, metaclass=Meta):` -- confirmed via a direct tree-
        sitter grammar probe during this milestone's own design audit.

        Keeps `identifier` (`Base`) and `attribute` (`pkg.Mixin`, a
        dotted/qualified name) nodes -- the only two node types that can
        actually name a base class in this grammar. Deliberately
        EXCLUDES `keyword_argument` (e.g. `metaclass=Meta`) -- not a
        base class -- and punctuation/commas. Returns the raw text
        exactly as written; RESOLVING each name to a real symbol is a
        separate step performed by `CallGraphBuilder`, not here (see
        `Symbol.base_classes`'s own docstring for why raw and resolved
        are deliberately kept apart).
        """
        superclasses = node.child_by_field_name("superclasses")
        if not superclasses:
            return []

        bases: list[str] = []
        for child in superclasses.children:
            if child.type in ("identifier", "attribute"):
                bases.append(
                    source_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
                )
        return bases

    def _extract_docstring(self, node: Any, source_bytes: bytes) -> str | None:
        body_node = node.child_by_field_name("body")
        if not body_node or not body_node.children:
            return None

        first_stmt = body_node.children[0]
        if first_stmt.type == "expression_statement" and first_stmt.children:
            expr = first_stmt.children[0]
            if expr.type == "string":
                doc = source_bytes[expr.start_byte:expr.end_byte].decode("utf-8", errors="replace")
                # Strip quotes (triple or single)
                for q in ('"""', "'''", '"', "'"):
                    if doc.startswith(q) and doc.endswith(q) and len(doc) >= 2 * len(q):
                        return doc[len(q):-len(q)].strip()
                return doc.strip()
        return None

    def _extract_signature(self, node: Any, source_bytes: bytes) -> str:
        body_node = node.child_by_field_name("body")
        if body_node:
            sig_bytes = source_bytes[node.start_byte:body_node.start_byte]
            sig_text = sig_bytes.decode("utf-8", errors="replace").strip()
            if sig_text.endswith(":"):
                sig_text = sig_text[:-1].strip()
            return " ".join(sig_text.split())
        
        # Fallback to first line
        full_text = source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
        return full_text.splitlines()[0].strip().rstrip(":")

    def _calculate_complexity(self, node: Any) -> int:
        complexity = 1
        branching_types = {
            "if_statement", "elif_clause", "for_statement", "while_statement",
            "except_clause", "with_statement", "boolean_operator", "conditional_expression"
        }

        def count_branches(n: Any) -> None:
            nonlocal complexity
            if n.type in branching_types:
                complexity += 1
            for child in n.children:
                count_branches(child)

        count_branches(node)
        return complexity

    def extract_imports(self, source_code: str, file_path: str = "") -> list[ImportStatement]:
        source_bytes = source_code.encode("utf-8")
        tree = self._parser.parse(source_bytes)
        imports: list[ImportStatement] = []

        def walk(node: Any) -> None:
            if node.type == "import_statement":
                # e.g., import math, sys as s
                line_num = node.start_point[0] + 1
                for child in node.children:
                    if child.type == "dotted_name":
                        mod_name = source_bytes[child.start_byte:child.end_byte].decode("utf-8")
                        imports.append(
                            ImportStatement(
                                module=mod_name,
                                items=[],
                                is_relative=False,
                                level=0,
                                line_number=line_num,
                                file_path=file_path,
                            )
                        )
                    elif child.type == "aliased_import":
                        name_n = child.child_by_field_name("name")
                        alias_n = child.child_by_field_name("alias")
                        if name_n:
                            mod_name = source_bytes[name_n.start_byte:name_n.end_byte].decode("utf-8")
                            alias = source_bytes[alias_n.start_byte:alias_n.end_byte].decode("utf-8") if alias_n else None
                            imports.append(
                                ImportStatement(
                                    module=mod_name,
                                    items=[ImportItem(name=mod_name, alias=alias)],
                                    is_relative=False,
                                    level=0,
                                    line_number=line_num,
                                    file_path=file_path,
                                )
                            )

            elif node.type == "import_from_statement":
                # e.g., from os.path import join as j, exists
                # e.g., from ..utils import helper
                line_num = node.start_point[0] + 1
                module_name = ""
                level = 0
                is_relative = False

                module_node = node.child_by_field_name("module_name")
                if module_node:
                    if module_node.type == "relative_import":
                        is_relative = True
                        # Count dots
                        dot_str = ""
                        for sub in module_node.children:
                            if sub.type == "import_prefix":
                                dot_str += source_bytes[sub.start_byte:sub.end_byte].decode("utf-8")
                            elif sub.type == "dotted_name":
                                module_name = source_bytes[sub.start_byte:sub.end_byte].decode("utf-8")
                        level = dot_str.count(".")
                    else:
                        module_name = source_bytes[module_node.start_byte:module_node.end_byte].decode("utf-8")

                items: list[ImportItem] = []
                for child in node.children:
                    if child.type == "dotted_name" and child != module_node:
                        item_name = source_bytes[child.start_byte:child.end_byte].decode("utf-8")
                        items.append(ImportItem(name=item_name))
                    elif child.type == "aliased_import":
                        name_n = child.child_by_field_name("name")
                        alias_n = child.child_by_field_name("alias")
                        if name_n:
                            item_name = source_bytes[name_n.start_byte:name_n.end_byte].decode("utf-8")
                            alias = source_bytes[alias_n.start_byte:alias_n.end_byte].decode("utf-8") if alias_n else None
                            items.append(ImportItem(name=item_name, alias=alias))

                imports.append(
                    ImportStatement(
                        module=module_name,
                        items=items,
                        is_relative=is_relative,
                        level=level,
                        line_number=line_num,
                        file_path=file_path,
                    )
                )

            for child in node.children:
                walk(child)

        walk(tree.root_node)
        return imports

    def extract_calls(
        self, source_code: str, file_path: str = "", symbols: list[Symbol] | None = None
    ) -> list[CallReference]:
        if symbols is None:
            symbols = self.extract_symbols(source_code, file_path=file_path)

        source_bytes = source_code.encode("utf-8")
        tree = self._parser.parse(source_bytes)
        calls: list[CallReference] = []

        def find_enclosing_symbol(line: int) -> str:
            # Match most specific / deepest symbol containing line
            matching = [s for s in symbols if s.start_line <= line <= s.end_line]
            if not matching:
                return "<module>"
            # Sort by smallest line span
            matching.sort(key=lambda s: (s.end_line - s.start_line))
            return matching[0].name

        def walk(node: Any) -> None:
            if node.type == "call":
                func_node = node.child_by_field_name("function")
                if func_node:
                    callee_name = source_bytes[func_node.start_byte:func_node.end_byte].decode("utf-8", errors="replace")
                    # If attribute call like obj.method(), extract method name
                    if func_node.type == "attribute":
                        attr_node = func_node.child_by_field_name("attribute")
                        if attr_node:
                            callee_name = source_bytes[attr_node.start_byte:attr_node.end_byte].decode("utf-8", errors="replace")
                    
                    line_num = node.start_point[0] + 1
                    caller = find_enclosing_symbol(line_num)
                    calls.append(
                        CallReference(
                            caller_symbol=caller,
                            callee_name=callee_name,
                            line_number=line_num,
                            file_path=file_path,
                        )
                    )

            for child in node.children:
                walk(child)

        walk(tree.root_node)
        return calls
