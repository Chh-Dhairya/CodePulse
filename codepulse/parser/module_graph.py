from __future__ import annotations

from typing import Any

import networkx as nx

from codepulse.models import FileParseResult
from codepulse.parser.graph import ImportResolver


class ModuleGraphBuilder:
    """
    Phase 4.3 (roadmap §37 item 4.3: "Deterministic module-clustering
    via import graph"; §11 "Architecture discovery": "module/dependency-
    cluster detection via the existing import graph... to identify
    likely 'modules' and 'entry points'"; §45 backlog B18: "New
    graph-analysis module").

    Deliberately kept SEPARATE from `CallGraphBuilder` (roadmap §37
    item 4.3's own instruction) -- this class operates on the
    FILE-LEVEL import graph, not the symbol-level call/inheritance
    graph. It reuses `ImportResolver.resolve_import_path` exactly as
    written (no second import resolver, no duplicated resolution
    logic) purely to turn each file's own `ImportStatement`s into
    file-to-file edges.

    Library/shared-core only, per this milestone's explicit approved
    scope -- no REST route, CLI command, or MCP tool consumes this yet.
    A future one can, through `IndexingEngine.analyze_module_structure`,
    without duplicating any of this construction logic.

    Graph semantics (approved, roadmap §37 item 4.3 audit):
      - Nodes are every file this repository's parser actually parsed
        (`ImportResolver.files`), INCLUDING files with zero imports in
        either direction -- isolated files are never silently omitted.
      - An edge `source_file -> target_file` exists if `source_file`
        contains at least one import statement that
        `ImportResolver.resolve_import_path` resolves to `target_file`
        (a real, repository-local file). Multiple import statements in
        `source_file` resolving to the SAME `target_file` collapse to
        exactly one edge -- this graph represents file-to-file
        dependency EXISTENCE, not a raw import-statement count (unlike
        `CallGraphEdge.call_count`, which deliberately does count).
      - An import that does not resolve to a real file in this
        repository (an external package, a stdlib module, or a genuine
        parser gap) never becomes an edge and never creates a node for
        the unresolved target -- `ImportResolver.resolve_import_path`
        is the single source of truth for "does this import point at
        one of this repository's own files," reused unmodified.
      - A file importing itself is not recorded as a self-loop edge.

    `in_degree`/`out_degree` are computed from this exact edge set (the
    distinct-file-pair notion above), never from raw import-statement
    counts. They are exposed as RAW, uninterpreted signals -- no
    "importance"/"centrality"/"API probability" score is computed or
    stored anywhere in this class. §11's own literal heuristic (kept
    exactly as worded, not silently reversed to match conventional
    "entry point" intuition): a file with many inbound and few outbound
    edges is read by the roadmap as looking like an API/entry point; a
    file with many outbound and few inbound edges is read as looking
    like a utility/leaf module. `rank_by_inbound`/`rank_by_outbound`
    below surface the raw degree data sorted for each of those two
    readings -- they attach no label, score, or threshold of any kind.

    Clustering (approved, roadmap §37 item 4.3 audit -- empirically
    evaluated against CodePulse's own repository before this decision
    was made, not assumed): plain connected components over this same
    file graph was tried first and produced one 27-file component plus
    one singleton on this project's own `codepulse/` package -- not
    "coherent module clusters" by any reasonable reading, and a
    measured failure of B18's own self-referential DoD. Seeded Louvain
    community detection (`networkx.algorithms.community.
    louvain_communities`) over an UNDIRECTED view of the same edge set
    produced multiple genuinely coherent clusters on the same
    repository and is the approved algorithm. `_LOUVAIN_SEED` is fixed
    and NOT exposed as a caller-supplied parameter this milestone --
    empirically verified (same audit) that the identical seed produces
    byte-identical output across repeated runs and across independent
    processes with different `PYTHONHASHSEED` values, while a
    different seed can produce a different partition of the same
    graph. The seed is therefore part of this class's deterministic
    contract, not an incidental default. Clusters are NEVER read as an
    objective "correct" decomposition of the codebase -- they are a
    deterministic, graph-derived grouping, reproducible for a given
    snapshot and seed, nothing more.
    """

    _LOUVAIN_SEED = 0

    def __init__(self, parse_results: dict[str, FileParseResult] | list[FileParseResult]) -> None:
        self.resolver = ImportResolver(parse_results)
        self.files: list[str] = sorted(self.resolver.files.keys())

        self.adjacency: dict[str, set[str]] = {f: set() for f in self.files}
        self.reverse_adjacency: dict[str, set[str]] = {f: set() for f in self.files}

        for file_path, res in self.resolver.files.items():
            for imp in res.imports:
                target = self.resolver.resolve_import_path(file_path, imp)
                # `resolve_import_path` only ever returns a key already
                # present in `self.resolver.files` -- the `in self.files`
                # check is a defensive, explicit restatement of "only
                # resolved, repository-local imports become edges," not
                # a case this can actually fail today.
                if target and target != file_path and target in self.resolver.files:
                    self.adjacency[file_path].add(target)
                    self.reverse_adjacency[target].add(file_path)

        self.in_degree: dict[str, int] = {f: len(self.reverse_adjacency[f]) for f in self.files}
        self.out_degree: dict[str, int] = {f: len(self.adjacency[f]) for f in self.files}

        self.clusters: list[dict[str, Any]] = self._compute_clusters()

    def _compute_clusters(self) -> list[dict[str, Any]]:
        graph = nx.Graph()
        graph.add_nodes_from(self.files)
        for src, targets in self.adjacency.items():
            for tgt in targets:
                graph.add_edge(src, tgt)

        raw_communities = nx.algorithms.community.louvain_communities(
            graph, seed=self._LOUVAIN_SEED
        )

        # NetworkX's own community container order is arbitrary/internal
        # -- never used as the public id. Each community's own files are
        # sorted lexicographically first, then communities themselves
        # are sorted by (size descending, lexicographically-smallest
        # member), and only THEN assigned sequential ids in that order,
        # so the same graph and seed always produce the same clusters in
        # the same order with the same ids, regardless of container
        # iteration order.
        sorted_file_lists = sorted(
            (sorted(community) for community in raw_communities),
            key=lambda files: (-len(files), files[0]),
        )
        return [{"id": idx, "files": files} for idx, files in enumerate(sorted_file_lists)]

    def rank_by_inbound(self) -> list[dict[str, Any]]:
        """
        Raw per-file degree data, sorted by `(-in_degree, out_degree,
        file_path)` -- roadmap §11's literal "many inbound, few
        outbound" heuristic reading (files ranked at the top here are
        what the roadmap describes as looking like APIs/entry points;
        see this class's own docstring for why that wording is kept
        exactly as written rather than reversed to match conventional
        "entry point" intuition). No score, label, or threshold is
        attached -- only the measured `in_degree`/`out_degree` values.
        """
        return [
            {"file": f, "in_degree": self.in_degree[f], "out_degree": self.out_degree[f]}
            for f in sorted(self.files, key=lambda f: (-self.in_degree[f], self.out_degree[f], f))
        ]

    def rank_by_outbound(self) -> list[dict[str, Any]]:
        """
        Raw per-file degree data, sorted by `(-out_degree, in_degree,
        file_path)` -- roadmap §11's literal "many outbound, few
        inbound" heuristic reading (files ranked at the top here are
        what the roadmap describes as looking like utility/leaf
        modules). No score, label, or threshold is attached.
        """
        return [
            {"file": f, "in_degree": self.in_degree[f], "out_degree": self.out_degree[f]}
            for f in sorted(self.files, key=lambda f: (-self.out_degree[f], self.in_degree[f], f))
        ]
