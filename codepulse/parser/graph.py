from __future__ import annotations

import os
from collections import defaultdict, deque
from typing import Optional

from codepulse.models import CallGraphEdge, FileParseResult, ImportStatement, Symbol, SymbolType


class ImportResolver:
    """Resolves import statements to target repository file paths (Phase 2A)."""

    def __init__(self, parse_results: dict[str, FileParseResult] | list[FileParseResult]) -> None:
        if isinstance(parse_results, list):
            self.files = {res.file_path.replace("\\", "/"): res for res in parse_results}
        else:
            self.files = {path.replace("\\", "/"): res for path, res in parse_results.items()}

        # Build symbol lookup index: (file_path, symbol_name) -> Symbol.
        # Phase 3.4: this was built here from this class's earliest
        # version but never actually read by anything -- now used by
        # `CallGraphBuilder._build_graph` to confirm an import-resolved
        # callee is a genuine known symbol in its target file (§11's
        # "high" confidence tier requires a known file+SYMBOL, not just
        # a known file).
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
    """
    Builds and queries the symbol call graph (Phase 2B - Acyclic &
    Cycle-Safe).

    Phase 4.1 (roadmap §37 item 4.1: "Base-class/inheritance extraction
    (Python + JS/TS)"): also resolves inheritance relationships (a
    class's `Symbol.base_classes`, raw text, into confidence-scored
    edges), reusing this class's own existing import-resolution
    philosophy verbatim (`_resolve_target`, factored out of the
    call-edge logic this milestone found already implementing it
    inline) rather than inventing a second resolution algorithm.

    CRITICAL invariant, enforced by construction, not by convention:
    inheritance edges are kept in `self.inheritance_edges`/
    `self.inheritance_adjacency`/`self.reverse_inheritance_adjacency` --
    entirely SEPARATE from `self.edges`/`self.adjacency`/
    `self.reverse_adjacency`, which remain calls-only, exactly as before
    this milestone. `get_callers`/`get_callees` traverse ONLY the
    calls-only structures and are therefore semantically unchanged by
    this milestone -- verified by this project's full pre-existing
    call-graph test suite continuing to pass unmodified. A future
    milestone wanting inheritance-aware traversal gets new, dedicated
    methods (not built here -- out of this milestone's exact scope,
    which is extraction + resolution, not a new query surface); it must
    NOT be achieved by merging the two adjacency structures.
    """

    # Phase 4.2 (roadmap §37 item 4.2; §11): deterministic ordering rule
    # for traversal results, reusing §12's own stated convention for the
    # adjacent `/v1/agent/context` bundle ("callers/callees... sorted by
    # confidence then call-count") rather than inventing a new one.
    # `None` (a legacy pre-Phase-3.4 row with no computed confidence,
    # see `CallEdge`'s own docstring) sorts last -- known-good
    # information before honestly-absent information, never fabricated.
    _CONFIDENCE_SORT_RANK = {"high": 0, "low": 1, "unresolved": 2, None: 3}

    def __init__(self, parse_results: dict[str, FileParseResult] | list[FileParseResult]) -> None:
        self.resolver = ImportResolver(parse_results)
        self.edges: list[CallGraphEdge] = []
        # Adjacency lists for fast bidirectional traversal -- CALLS ONLY.
        self.adjacency: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        self.reverse_adjacency: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        # Phase 4.2: per-edge (confidence, call_count) index, keyed by the
        # SAME (src_key, tgt_key) pairs used in `self.adjacency` -- CALLS
        # ONLY, built in the same loop that populates `self.adjacency`
        # (no second pass, no new traversal). `_build_graph`'s own
        # `call_counts`/`call_confidence` dicts already guarantee exactly
        # one edge per (src_key, tgt_key) pair, so this lookup is never
        # ambiguous. Lets `get_callers`/`get_callees` attach each result's
        # confidence and sort deterministically without a second graph
        # traversal or a new confidence vocabulary.
        self.edge_meta: dict[tuple[tuple[str, str], tuple[str, str]], tuple[Optional[str], int]] = {}
        # Phase 4.1: INHERITANCE ONLY -- deliberately separate structures,
        # never merged with the calls-only ones above. See this class's
        # own docstring for why.
        self.inheritance_edges: list[CallGraphEdge] = []
        self.inheritance_adjacency: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        self.reverse_inheritance_adjacency: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        # Phase 4.4 (roadmap §37 item 4.4 "References endpoint"): the
        # inheritance-side counterpart to `edge_meta`, same (src_key,
        # tgt_key) -> confidence lookup shape, built in the SAME loop
        # that populates `inheritance_adjacency` -- CALLS ONLY structures
        # above are never touched by this. Lets `get_direct_subclasses`
        # attach confidence without a second traversal or a new index.
        self.inheritance_edge_meta: dict[tuple[tuple[str, str], tuple[str, str]], Optional[str]] = {}
        self._build_graph()

    @classmethod
    def from_edges(
        cls,
        edges: list[CallGraphEdge],
        inheritance_edges: list[CallGraphEdge],
    ) -> "CallGraphBuilder":
        """
        Rebuilds a graph from ALREADY-RESOLVED edges (the durable
        `call_edges` rows) instead of from parse results, so a restarted
        process can answer callers/callees/subclasses without re-parsing
        the repository.

        Populates exactly the structures `_build_graph` populates, with the
        same keys (`(file, symbol)` pairs; an unresolved target file is
        `""`), so every query method behaves identically on a restored
        graph and a freshly built one. Edges are taken as stored -- no
        resolution is re-run, so the confidence on each edge is exactly
        what was computed (and persisted) at index time, including a
        legacy `None`.

        The one thing a restored graph does NOT have is `resolver`
        (parsed files / imports): it is empty. Anything needing the parse
        data -- notably an incremental update, which merges cached parse
        results for unchanged files -- must not treat a restored graph as
        its base; `IndexingEngine` tracks that.
        """
        graph = cls([])
        for edge in edges:
            graph.edges.append(edge)
            src_key = (edge.source_file, edge.source_symbol_name)
            tgt_key = (edge.target_file or "", edge.target_symbol_name)
            graph.adjacency[src_key].add(tgt_key)
            graph.reverse_adjacency[tgt_key].add(src_key)
            graph.edge_meta[(src_key, tgt_key)] = (edge.confidence, edge.call_count)
        for edge in inheritance_edges:
            graph.inheritance_edges.append(edge)
            src_key = (edge.source_file, edge.source_symbol_name)
            tgt_key = (edge.target_file or "", edge.target_symbol_name)
            graph.inheritance_adjacency[src_key].add(tgt_key)
            graph.reverse_inheritance_adjacency[tgt_key].add(src_key)
            graph.inheritance_edge_meta[(src_key, tgt_key)] = edge.confidence
        return graph

    def _resolve_target(
        self, name: str, file_path: str, local_symbols: dict[str, Symbol], import_map: dict[str, str]
    ) -> tuple[Optional[str], str]:
        """
        Shared resolution philosophy (roadmap §11), used identically by
        BOTH call-edge and (Phase 4.1) inheritance-edge resolution --
        factored out of what was previously call-edge-only inline logic,
        with IDENTICAL behavior (verified by this project's existing
        call-graph/confidence test suite continuing to pass unmodified
        after this refactor) so the two relationship kinds can never
        silently drift onto different resolution rules. Returns
        `(target_file_or_None, confidence)`.

        "high" -- resolved through an explicit import mapping to a known
        file+SYMBOL (not just a known file -- cross-checked against
        `self.resolver.symbol_index`). "low" -- resolved only by
        bare-name match against same-file symbols (the ambiguity §3.5's
        audit finding names: two unrelated classes/functions both
        defining a same-named symbol in this file would be conflated
        here, so this can never be "high" no matter how the match was
        made). "unresolved" -- everything else, including a resolved
        import file whose exact symbol couldn't be confirmed in it (a
        cross-file case, so it deliberately does NOT fall back to "low",
        which is defined as a same-file match only) and a true external
        reference with no local or import match at all. Never a fourth
        "ambiguous" tier -- deliberately reusing this exact 3-tier
        vocabulary for inheritance too, not inventing a parallel one.
        """
        if name in local_symbols:
            return file_path, "low"
        elif name in import_map:
            target_file = import_map[name]
            if (target_file, name) in self.resolver.symbol_index:
                return target_file, "high"
            return target_file, "unresolved"
        else:
            return None, "unresolved"

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

            # Map calls to target symbols. Phase 3.4 (roadmap §37 item
            # 3.4; §11): every resolution decision below already existed
            # -- this milestone's job is to SURFACE which one applied,
            # not to change which edges get created.
            call_counts: dict[tuple[str, str, str, Optional[str]], int] = defaultdict(int)
            call_confidence: dict[tuple[str, str, str, Optional[str]], str] = {}

            for call in res.calls:
                caller = call.caller_symbol
                callee = call.callee_name
                target_file, confidence = self._resolve_target(callee, file_path, local_symbols, import_map)
                key = (caller, file_path, callee, target_file)

                call_counts[key] += 1
                # Every call to this exact (caller, file, callee) triple
                # resolves through the identical branch above every time
                # (the decision depends only on `callee`/`file_path`, not
                # on the specific call site), so this is always a no-op
                # re-assignment of the same value, never a real conflict.
                call_confidence[key] = confidence

            for key, count in call_counts.items():
                src_sym, src_file, tgt_sym, tgt_file = key
                edge = CallGraphEdge(
                    source_symbol_name=src_sym,
                    source_file=src_file,
                    target_symbol_name=tgt_sym,
                    target_file=tgt_file,
                    call_count=count,
                    confidence=call_confidence[key],
                    relationship_type="calls",
                )
                self.edges.append(edge)

                src_key = (src_file, src_sym)
                tgt_key = (tgt_file or "", tgt_sym)
                self.adjacency[src_key].add(tgt_key)
                self.reverse_adjacency[tgt_key].add(src_key)
                self.edge_meta[(src_key, tgt_key)] = (call_confidence[key], count)

            # Phase 4.1: resolve inheritance relationships for every
            # CLASS symbol in this file, reusing the SAME `import_map`/
            # `local_symbols` already built above for calls (no second
            # pass over `self.resolver.files`) and the SAME
            # `_resolve_target` resolution philosophy. Each entry in a
            # class's `base_classes` (raw text, from `Symbol.
            # base_classes` -- see that field's own docstring for why
            # raw and resolved are kept separate) becomes exactly one
            # inheritance edge, stored ONLY in the inheritance-specific
            # structures below -- never in `self.edges`/`self.adjacency`/
            # `self.reverse_adjacency`.
            for sym in res.symbols:
                if sym.type != SymbolType.CLASS or not sym.base_classes:
                    continue
                for base_name in sym.base_classes:
                    target_file, confidence = self._resolve_target(
                        base_name, file_path, local_symbols, import_map
                    )
                    edge = CallGraphEdge(
                        source_symbol_name=sym.name,
                        source_file=file_path,
                        target_symbol_name=base_name,
                        target_file=target_file,
                        call_count=1,
                        confidence=confidence,
                        relationship_type="inherits",
                    )
                    self.inheritance_edges.append(edge)

                    src_key = (file_path, sym.name)
                    tgt_key = (target_file or "", base_name)
                    self.inheritance_adjacency[src_key].add(tgt_key)
                    self.reverse_inheritance_adjacency[tgt_key].add(src_key)
                    self.inheritance_edge_meta[(src_key, tgt_key)] = confidence

    def get_callees(self, symbol_name: str, file_path: str = "", max_depth: int = 3) -> list[dict]:
        """
        Returns symbols called by the given symbol up to max_depth
        (downstream impact). CALLS ONLY -- traverses `self.adjacency`
        exclusively, never `self.inheritance_adjacency` (Phase 4.1's own
        separation invariant, unchanged by Phase 4.2).

        Phase 4.2 (roadmap §37 item 4.2; §11): each result now also
        carries `confidence` (looked up from `self.edge_meta`, the same
        3-tier "high"/"low"/"unresolved" vocabulary already used
        everywhere else -- no new vocabulary), and results are sorted
        deterministically (confidence, then call_count descending, then
        file, then symbol) so the same repository snapshot always
        produces the same ordered list, regardless of process-local
        Python set-iteration order. The BFS/visited-set mechanics
        themselves are UNCHANGED from before this milestone -- still a
        single traversal, still cycle-safe and deduplicated purely
        through `visited`, still reporting each key's true shortest-path
        depth (the first, and only, time it is ever enqueued).
        """
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
                    confidence, call_count = self.edge_meta.get((curr_key, next_key), (None, 0))
                    results.append({
                        "symbol": tgt_sym,
                        "file": tgt_file if tgt_file else "external",
                        "depth": depth,
                        "confidence": confidence,
                        "_call_count": call_count,
                    })
                    queue.append((next_key, depth + 1))

        return self._sort_and_strip(results)

    def get_callers(self, symbol_name: str, file_path: str = "", max_depth: int = 3) -> list[dict]:
        """
        Returns symbols that call the given symbol up to max_depth
        (upstream blast radius). CALLS ONLY -- traverses
        `self.reverse_adjacency` exclusively, never
        `self.reverse_inheritance_adjacency`.

        Phase 4.2: same `confidence` + deterministic-ordering additions
        as `get_callees` (see its docstring) -- each queued item now also
        carries the key it directly calls (`points_to`), so the
        confidence of THAT exact edge (caller -> points_to) can be looked
        up from `self.edge_meta` at result-construction time. This is
        the only change to the traversal's own bookkeeping; the
        visited-set/queue mechanics, shortest-path depth assignment, and
        cycle safety are otherwise identical to before this milestone.
        """
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
                    queue.append((caller, 1, k))

        results = []
        while queue:
            curr_key, depth, points_to = queue.popleft()
            src_file, src_sym = curr_key
            confidence, call_count = self.edge_meta.get((curr_key, points_to), (None, 0))
            results.append({
                "symbol": src_sym,
                "file": src_file,
                "depth": depth,
                "confidence": confidence,
                "_call_count": call_count,
            })

            if depth < max_depth:
                for next_caller in self.reverse_adjacency.get(curr_key, set()):
                    if next_caller not in visited:
                        visited.add(next_caller)
                        queue.append((next_caller, depth + 1, curr_key))

        return self._sort_and_strip(results)

    def _sort_and_strip(self, results: list[dict]) -> list[dict]:
        """
        Phase 4.2 deterministic-ordering helper, shared by `get_callers`/
        `get_callees`: sorts by (confidence, call_count descending, file,
        symbol) -- reusing §12's own stated "sorted by confidence then
        call-count" convention rather than inventing a new ranking -- and
        strips the internal `_call_count` sort key before returning, so
        the public result shape stays exactly `{symbol, file, depth,
        confidence}`.
        """
        results.sort(
            key=lambda r: (
                self._CONFIDENCE_SORT_RANK.get(r["confidence"], 3),
                -r["_call_count"],
                r["file"],
                r["symbol"],
            )
        )
        for r in results:
            del r["_call_count"]
        return results

    def get_direct_subclasses(self, symbol_name: str, file_path: str = "") -> list[dict]:
        """
        Phase 4.4 (roadmap §37 item 4.4 "References endpoint"; §21's
        `references` endpoint group, kept explicitly SEPARATE from
        `graph (callers/callees/impact)`). "References" is a DIRECT,
        single-hop lookup, not a transitive/bucketed traversal the way
        impact analysis is (Phase 4.2's own explicit calls-only,
        depth-bucketed design is a different concept, deliberately not
        reused here) -- so this is a plain adjacency lookup, not a BFS.

        Reads `self.reverse_inheritance_adjacency`/
        `self.inheritance_edge_meta` exclusively -- NEVER the calls-only
        `self.reverse_adjacency`, preserving Phase 4.1's separation
        invariant. Returns every class that directly lists `symbol_name`
        as one of its `base_classes`, each with its own resolution
        `confidence` (the same high/low/unresolved vocabulary, no new
        tier), deterministically ordered by `(confidence, file, symbol)`
        -- no `call_count` tiebreaker, since every inheritance edge's
        `call_count` is always `1` (not a real signal, see
        `CallGraphEdge.relationship_type`'s own docstring).
        """
        file_path = file_path.replace("\\", "/")
        candidate_keys: list[tuple[str, str]] = []

        if file_path:
            candidate_keys.append((file_path, symbol_name))
            candidate_keys.append(("", symbol_name))
        else:
            candidate_keys = [
                (f, s) for (f, s) in self.reverse_inheritance_adjacency if s == symbol_name
            ]

        if not candidate_keys:
            candidate_keys.append(("", symbol_name))

        seen: set[tuple[str, str]] = set()
        results = []
        for key in candidate_keys:
            for sub_key in self.reverse_inheritance_adjacency.get(key, set()):
                if sub_key in seen:
                    continue
                seen.add(sub_key)
                src_file, src_sym = sub_key
                confidence = self.inheritance_edge_meta.get((sub_key, key))
                results.append({"symbol": src_sym, "file": src_file, "confidence": confidence})

        results.sort(
            key=lambda r: (self._CONFIDENCE_SORT_RANK.get(r["confidence"], 3), r["file"], r["symbol"])
        )
        return results
