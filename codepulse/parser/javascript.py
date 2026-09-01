from __future__ import annotations

import hashlib
from typing import Any
from tree_sitter_languages import get_parser

from codepulse.models import CallReference, ImportItem, ImportStatement, Symbol, SymbolType
from codepulse.parser.base import LanguageParser


class JavaScriptParser(LanguageParser):
    """AST parser for JavaScript and TypeScript source code using Tree-sitter."""

    def __init__(self, is_typescript: bool = False) -> None:
        self._is_typescript = is_typescript
        try:
            self._parser = get_parser("typescript" if is_typescript else "javascript")
        except Exception:
            # Fallback to javascript parser
            self._parser = get_parser("javascript")

    @property
    def language(self) -> str:
        return "typescript" if self._is_typescript else "javascript"

    def extract_symbols(self, source_code: str, file_path: str = "") -> list[Symbol]:
        source_bytes = source_code.encode("utf-8")
        tree = self._parser.parse(source_bytes)
        symbols: list[Symbol] = []

        def walk(node: Any, parent_class: str | None = None) -> None:
            # 1. Function Declarations: function foo() {}
            if node.type in ("function_declaration", "generator_function_declaration"):
                sym = self._parse_function_declaration(node, source_bytes, file_path, parent_class)
                if sym:
                    symbols.append(sym)
                body_node = node.child_by_field_name("body")
                if body_node:
                    for child in body_node.children:
                        walk(child, parent_class=sym.name if sym else parent_class)
                return

            # 2. Variable declarations with arrow functions / function expressions: const foo = () => {}
            elif node.type == "lexical_declaration" or node.type == "variable_declaration":
                syms = self._parse_variable_functions(node, source_bytes, file_path, parent_class)
                symbols.extend(syms)
                for child in node.children:
                    walk(child, parent_class)
                return

            # 3. Class Declarations: class MyClass {}
            elif node.type == "class_declaration":
                sym = self._parse_class_declaration(node, source_bytes, file_path, parent_class)
                if sym:
                    symbols.append(sym)
                body_node = node.child_by_field_name("body")
                if body_node:
                    for child in body_node.children:
                        walk(child, parent_class=sym.name if sym else parent_class)
                return

            # 4. Method Definitions inside class body: foo() {}
            elif node.type == "method_definition":
                sym = self._parse_method_definition(node, source_bytes, file_path, parent_class)
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

    def _parse_function_declaration(
        self, node: Any, source_bytes: bytes, file_path: str, parent_class: str | None
    ) -> Symbol | None:
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None

        name = source_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        body_text = source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
        docstring = self._extract_jsdoc(node, source_bytes)
        signature = self._extract_signature(node, source_bytes)
        complexity = self._calculate_complexity(node)

        return Symbol(
            name=name,
            type=SymbolType.METHOD if parent_class else SymbolType.FUNCTION,
            file_path=file_path,
            start_line=start_line,
            end_line=end_line,
            signature=signature,
            docstring=docstring,
            body=body_text,
            body_hash=hashlib.sha256(body_text.strip().encode("utf-8")).hexdigest(),
            language=self.language,
            parent_symbol=parent_class,
            complexity=complexity,
        )

    def _parse_variable_functions(
        self, node: Any, source_bytes: bytes, file_path: str, parent_class: str | None
    ) -> list[Symbol]:
        results: list[Symbol] = []
        for child in node.children:
            if child.type == "variable_declarator":
                name_n = child.child_by_field_name("name")
                value_n = child.child_by_field_name("value")
                if name_n and value_n and value_n.type in ("arrow_function", "function_expression"):
                    name = source_bytes[name_n.start_byte:name_n.end_byte].decode("utf-8", errors="replace")
                    start_line = node.start_point[0] + 1
                    end_line = node.end_point[0] + 1
                    body_text = source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
                    docstring = self._extract_jsdoc(node, source_bytes)
                    signature = self._extract_signature(node, source_bytes)
                    complexity = self._calculate_complexity(value_n)

                    results.append(
                        Symbol(
                            name=name,
                            type=SymbolType.METHOD if parent_class else SymbolType.FUNCTION,
                            file_path=file_path,
                            start_line=start_line,
                            end_line=end_line,
                            signature=signature,
                            docstring=docstring,
                            body=body_text,
                            body_hash=hashlib.sha256(body_text.strip().encode("utf-8")).hexdigest(),
                            language=self.language,
                            parent_symbol=parent_class,
                            complexity=complexity,
                        )
                    )
        return results

    def _parse_class_declaration(
        self, node: Any, source_bytes: bytes, file_path: str, parent_class: str | None
    ) -> Symbol | None:
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None

        name = source_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        body_text = source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
        docstring = self._extract_jsdoc(node, source_bytes)
        signature = self._extract_signature(node, source_bytes)

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
            language=self.language,
            parent_symbol=parent_class,
            complexity=1,
        )

    def _parse_method_definition(
        self, node: Any, source_bytes: bytes, file_path: str, parent_class: str | None
    ) -> Symbol | None:
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None

        name = source_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        body_text = source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
        docstring = self._extract_jsdoc(node, source_bytes)
        signature = self._extract_signature(node, source_bytes)
        complexity = self._calculate_complexity(node)

        return Symbol(
            name=name,
            type=SymbolType.METHOD,
            file_path=file_path,
            start_line=start_line,
            end_line=end_line,
            signature=signature,
            docstring=docstring,
            body=body_text,
            body_hash=hashlib.sha256(body_text.strip().encode("utf-8")).hexdigest(),
            language=self.language,
            parent_symbol=parent_class,
            complexity=complexity,
        )

    def _extract_jsdoc(self, node: Any, source_bytes: bytes) -> str | None:
        # Check previous sibling for comment
        prev = node.prev_sibling
        if prev and prev.type == "comment":
            cmt = source_bytes[prev.start_byte:prev.end_byte].decode("utf-8", errors="replace")
            if cmt.startswith("/**") and cmt.endswith("*/"):
                lines = [l.strip().lstrip("/*").rstrip("*/").lstrip("*").strip() for l in cmt.splitlines()]
                clean_lines = [l for l in lines if l]
                return "\n".join(clean_lines) if clean_lines else None
            elif cmt.startswith("//"):
                return cmt.lstrip("/").strip()
        return None

    def _extract_signature(self, node: Any, source_bytes: bytes) -> str:
        body_node = node.child_by_field_name("body")
        if body_node:
            sig_bytes = source_bytes[node.start_byte:body_node.start_byte]
            sig_text = sig_bytes.decode("utf-8", errors="replace").strip()
            return " ".join(sig_text.split())
        
        full_text = source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
        return full_text.splitlines()[0].strip().rstrip("{").strip()

    def _calculate_complexity(self, node: Any) -> int:
        complexity = 1
        branching_types = {
            "if_statement", "for_statement", "for_in_statement", "while_statement",
            "do_statement", "catch_clause", "switch_case", "ternary_expression",
            "binary_expression"  # Logical && and ||
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
            # ES6: import { a, b as c } from './module';
            # ES6: import React from 'react';
            # ES6: import * as path from 'path';
            if node.type == "import_statement":
                line_num = node.start_point[0] + 1
                source_node = node.child_by_field_name("source")
                module_name = ""
                if source_node:
                    module_name = source_bytes[source_node.start_byte:source_node.end_byte].decode("utf-8").strip("'\"")

                is_relative = module_name.startswith(".")
                level = 1 if module_name.startswith("./") else (2 if module_name.startswith("../") else 0)

                items: list[ImportItem] = []
                for child in node.children:
                    if child.type == "import_clause":
                        for sub in child.children:
                            if sub.type == "identifier":
                                default_name = source_bytes[sub.start_byte:sub.end_byte].decode("utf-8")
                                items.append(ImportItem(name=default_name, alias="default"))
                            elif sub.type == "named_imports":
                                for spec in sub.children:
                                    if spec.type == "import_specifier":
                                        name_n = spec.child_by_field_name("name")
                                        alias_n = spec.child_by_field_name("alias")
                                        if name_n:
                                            item_name = source_bytes[name_n.start_byte:name_n.end_byte].decode("utf-8")
                                            alias = source_bytes[alias_n.start_byte:alias_n.end_byte].decode("utf-8") if alias_n else None
                                            items.append(ImportItem(name=item_name, alias=alias))
                            elif sub.type == "namespace_import":
                                for ns_child in sub.children:
                                    if ns_child.type == "identifier":
                                        alias = source_bytes[ns_child.start_byte:ns_child.end_byte].decode("utf-8")
                                        items.append(ImportItem(name="*", alias=alias))

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

            # CommonJS: const foo = require('foo')
            elif node.type == "call_expression":
                func_n = node.child_by_field_name("function")
                args_n = node.child_by_field_name("arguments")
                if func_n and args_n:
                    fn_name = source_bytes[func_n.start_byte:func_n.end_byte].decode("utf-8", errors="replace")
                    if fn_name == "require" and args_n.children:
                        for arg in args_n.children:
                            if arg.type == "string":
                                mod_name = source_bytes[arg.start_byte:arg.end_byte].decode("utf-8").strip("'\"")
                                line_num = node.start_point[0] + 1
                                is_rel = mod_name.startswith(".")
                                imports.append(
                                    ImportStatement(
                                        module=mod_name,
                                        items=[],
                                        is_relative=is_rel,
                                        level=1 if mod_name.startswith("./") else (2 if mod_name.startswith("../") else 0),
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
            matching = [s for s in symbols if s.start_line <= line <= s.end_line]
            if not matching:
                return "<module>"
            matching.sort(key=lambda s: (s.end_line - s.start_line))
            return matching[0].name

        def walk(node: Any) -> None:
            if node.type == "call_expression":
                func_node = node.child_by_field_name("function")
                if func_node:
                    callee_name = source_bytes[func_node.start_byte:func_node.end_byte].decode("utf-8", errors="replace")
                    if func_node.type == "member_expression":
                        prop_node = func_node.child_by_field_name("property")
                        if prop_node:
                            callee_name = source_bytes[prop_node.start_byte:prop_node.end_byte].decode("utf-8", errors="replace")

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
