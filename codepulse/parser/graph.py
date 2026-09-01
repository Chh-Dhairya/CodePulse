from __future__ import annotations

import os
from collections import defaultdict, deque
from typing import Optional

from codepulse.models import CallGraphEdge, FileParseResult, ImportStatement, Symbol


class ImportResolver:
    """Resolves import statements to target repository file paths (Phase 2A)."""

    def __init__(self, parse_results: dict[str, FileParseResult] | list[FileParseResult]) -> None:
        if isinstance(parse_results, list):
            self.files = {res.file_path.replace("\\", "/"): res for res in parse_results}
        else:
            self.files = {path.replace("\\", "/"): res for path, res in parse_results.items()}

        # Build symbol lookup index: (file_path, symbol_name) -> Symbol
        self.symbol_index: dict[tuple[str, str], Symbol] = {}
        for file_path, res in self.files.items():
            for sym in res.symbols:
                self.symbol_index[(file_path, sym.name)] = sym

    def resolve_import_path(self, current_file: str, imp: ImportStatement) -> Optional[str]:
        """Resolves an ImportStatement in current_file to a target file path in the repo."""
        current_file = current_file.replace("\\", "/")
        current_dir = os.path.dirname(current_file)

        if imp.is_relative:
            # Handle relative imports: . or ..
            rel_dir = current_dir
            for _ in range(max(0, imp.level - 1)):
                rel_dir = os.path.dirname(rel_dir)

            mod_parts = imp.module.split(".") if imp.module else []
            target_path = os.path.normpath(os.path.join(rel_dir, *mod_parts)).replace("\\", "/")
        else:
            # Absolute project import (e.g., 'app.models.user')
            mod_parts = imp.module.split(".")
            target_path = os.path.normpath(os.path.join(*mod_parts)).replace("\\", "/")

        # Candidates to check
        candidates = [
            f"{target_path}.py",
            f"{target_path}/__init__.py",
            f"{target_path}.js",
            f"{target_path}.ts",
            f"{target_path}/index.js",
            f"{target_path}/index.ts",
            target_path,
        ]

        for cand in candidates:
            cand_norm = os.path.normpath(cand).replace("\\", "/")
            if cand_norm in self.files:
                return cand_norm

            # Also check suffix matches (e.g. if candidate is 'models/user.py' and file is 'src/models/user.py')
            for existing_path in self.files:
                if existing_path.endswith(cand_norm) or cand_norm.endswith(existing_path):
                    return existing_path

        return None


class CallGraphBuilder:
    """Builds and queries the symbol call graph (Phase 2B - Acyclic & Cycle-Safe)."""

    def __init__(self, parse_results: dict[str, FileParseResult] | list[FileParseResult]) -> None:
        self.resolver = ImportResolver(parse_results)
        self.edges: list[CallGraphEdge] = []
        # Adjacency lists for fast bidirectional traversal
        self.adjacency: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        self.reverse_adjacency: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        self._build_graph()

    def _build_graph(self) -> None:
        for file_path, res in self.resolver.files.items():
            # Build quick map of imported item names in this file -> resolved target file
            import_map: dict[str, str] = {}
            for imp in res.imports:
                resolved_file = self.resolver.resolve_import_path(file_path, imp)
                if resolved_file:
                    for item in imp.items:
                        lookup_name = item.alias if item.alias else item.name
                        import_map[lookup_name] = resolved_file

            local_symbols = {s.name: s for s in res.symbols}

            # Map calls to target symbols
            call_counts: dict[tuple[str, str, str, Optional[str]], int] = defaultdict(int)

            for call in res.calls:
                caller = call.caller_symbol
                callee = call.callee_name

                # Check if callee is in the same file
                if callee in local_symbols:
                    call_counts[(caller, file_path, callee, file_path)] += 1
                elif callee in import_map:
                    target_file = import_map[callee]
                    call_counts[(caller, file_path, callee, target_file)] += 1
                else:
                    # External / unresolved call
                    call_counts[(caller, file_path, callee, None)] += 1

            for (src_sym, src_file, tgt_sym, tgt_file), count in call_counts.items():
                edge = CallGraphEdge(
                    source_symbol_name=src_sym,
                    source_file=src_file,
                    target_symbol_name=tgt_sym,
                    target_file=tgt_file,
                    call_count=count,
                )
                self.edges.append(edge)

                src_key = (src_file, src_sym)
                tgt_key = (tgt_file or "", tgt_sym)
                self.adjacency[src_key].add(tgt_key)
                self.reverse_adjacency[tgt_key].add(src_key)

    def get_callees(self, symbol_name: str, file_path: str = "", max_depth: int = 3) -> list[dict]:
        """Returns symbols called by the given symbol up to max_depth (downstream impact)."""
        file_path = file_path.replace("\\", "/")
        
        start_keys: list[tuple[str, str]] = []
        if file_path:
            start_keys.append((file_path, symbol_name))
        else:
            # Find all keys in adjacency matching the symbol name
            for (f, sym) in self.adjacency.keys():
                if sym == symbol_name:
                    start_keys.append((f, sym))

        if not start_keys:
            start_keys.append(("", symbol_name))

        visited = set(start_keys)
        queue = deque([(k, 1) for k in start_keys])
        results = []

        while queue:
            curr_key, depth = queue.popleft()
            if depth > max_depth:
                continue

            for next_key in self.adjacency.get(curr_key, set()):
                if next_key not in visited:
                    visited.add(next_key)
                    tgt_file, tgt_sym = next_key
                    results.append({
                        "symbol": tgt_sym,
                        "file": tgt_file if tgt_file else "external",
                        "depth": depth,
                    })
                    queue.append((next_key, depth + 1))

        return results

    def get_callers(self, symbol_name: str, file_path: str = "", max_depth: int = 3) -> list[dict]:
        """Returns symbols that call the given symbol up to max_depth (upstream blast radius)."""
        file_path = file_path.replace("\\", "/")
        candidate_keys: list[tuple[str, str]] = []

        if file_path:
            candidate_keys.append((file_path, symbol_name))
            candidate_keys.append(("", symbol_name))
        else:
            # Match any target key where symbol name matches
            for (f, sym) in self.reverse_adjacency.keys():
                if sym == symbol_name:
                    candidate_keys.append((f, sym))

        if not candidate_keys:
            candidate_keys.append(("", symbol_name))

        visited = set()
        queue = deque()

        for k in candidate_keys:
            visited.add(k)
            for caller in self.reverse_adjacency.get(k, set()):
                if caller not in visited:
                    visited.add(caller)
                    queue.append((caller, 1))

        results = []
        while queue:
            curr_key, depth = queue.popleft()
            src_file, src_sym = curr_key
            results.append({
                "symbol": src_sym,
                "file": src_file,
                "depth": depth,
            })

            if depth < max_depth:
                for next_caller in self.reverse_adjacency.get(curr_key, set()):
                    if next_caller not in visited:
                        visited.add(next_caller)
                        queue.append((next_caller, depth + 1))

        return results
