from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Mapping

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.syntax import Syntax
from rich.table import Table
from rich.tree import Tree

from sqlalchemy import text

from codepulse.embeddings.embedder import CodeEmbedder
from codepulse.indexer.engine import IndexingEngine
from codepulse.parser.factory import get_parser_for_file
from codepulse.parser.graph import CallGraphBuilder
from codepulse.storage.db import StorageManager

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

console = Console(safe_box=True)
_stderr_console = Console(safe_box=True, stderr=True)


def _display_db_path(db_url: str) -> str:
    """
    Resolves a `sqlite:///<path>` URL's <path> component to an absolute
    path (against the CURRENT working directory, matching how
    `create_engine` itself would resolve it) purely for display -- so
    "which exact file is this command about to touch" is unambiguous,
    regardless of whether `db_url` came from a CLI flag, an env var, a
    relative `codepulse.toml` entry, or the plain relative default.
    Non-sqlite URLs (e.g. a future Postgres backend) are shown as-is.
    """
    prefix = "sqlite:///"
    if not db_url.startswith(prefix):
        return db_url
    raw_path = db_url[len(prefix):]
    if os.path.isabs(raw_path):
        return db_url
    return prefix + os.path.abspath(raw_path).replace(os.sep, "/")


def cmd_parse(args: argparse.Namespace) -> None:
    """Parses a single file and displays extracted AST symbols and complexity."""
    file_path = args.file_path
    if not os.path.exists(file_path):
        console.print(f"[bold red]Error:[/bold red] File not found: {file_path}")
        sys.exit(1)

    parser = get_parser_for_file(file_path)
    if not parser:
        console.print(f"[bold red]Error:[/bold red] No parser available for file: {file_path}")
        sys.exit(1)

    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    result = parser.parse_file(file_path, content)

    console.print(
        Panel.fit(
            f"[bold cyan]File:[/bold cyan] {file_path}\n"
            f"[bold cyan]Language:[/bold cyan] {result.language.upper()}\n"
            f"[bold cyan]Lines:[/bold cyan] {result.line_count} | [bold cyan]Size:[/bold cyan] {result.file_size_bytes} bytes",
            title="🔍 AST Parse Result",
            border_style="cyan",
        )
    )

    # 1. Symbols Table
    sym_table = Table(title="✨ Extracted Symbols", border_style="blue", show_header=True)
    sym_table.add_column("Type", style="magenta", width=12)
    sym_table.add_column("Name", style="bold green", width=25)
    sym_table.add_column("Lines", style="yellow", width=10)
    sym_table.add_column("Complexity", style="cyan", width=12)
    sym_table.add_column("Signature / Docstring", style="white")

    for sym in result.symbols:
        doc_preview = f"\n[italic dim]{sym.docstring[:80]}...[/italic dim]" if sym.docstring else ""
        sym_table.add_row(
            sym.type.value if hasattr(sym.type, "value") else str(sym.type),
            sym.name,
            f"L{sym.start_line}-{sym.end_line}",
            str(sym.complexity),
            f"[bold]{sym.signature}[/bold]{doc_preview}",
        )

    console.print(sym_table)

    # 2. Imports Table
    if result.imports:
        imp_table = Table(title="📦 Imports", border_style="magenta", show_header=True)
        imp_table.add_column("Module", style="bold yellow")
        imp_table.add_column("Items / Aliases", style="white")
        imp_table.add_column("Line", style="cyan")

        for imp in result.imports:
            items_str = ", ".join(f"{it.name}" + (f" as {it.alias}" if it.alias else "") for it in imp.items)
            imp_table.add_row(imp.module or "(relative)", items_str or "*", str(imp.line_number))

        console.print(imp_table)

    # 3. Call Invocations
    if result.calls:
        call_table = Table(title="📞 Function Calls", border_style="green", show_header=True)
        call_table.add_column("Caller", style="bold cyan")
        call_table.add_column("Callee", style="bold green")
        call_table.add_column("Line", style="yellow")

        for c in result.calls:
            call_table.add_row(c.caller_symbol, c.callee_name, str(c.line_number))

        console.print(call_table)


def cmd_index_and_search(args: argparse.Namespace) -> None:
    """Indexes a repository and opens an interactive search prompt or executes a search."""
    path = args.path
    if not os.path.exists(path):
        console.print(f"[bold red]Error:[/bold red] Path not found: {path}")
        sys.exit(1)

    console.print(f"[bold cyan]⚡ Indexing repository:[/bold cyan] {path}...")
    start_time = time.time()

    engine = IndexingEngine()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("[cyan]Processing files...", total=100)

        def cb(rel_path: str, curr: int, total: int):
            progress.update(task, completed=int((curr / max(1, total)) * 100), description=f"[cyan]{rel_path[:30]}")

        stats = engine.index_directory(path, repo_name=os.path.basename(path), progress_callback=cb)

    elapsed = time.time() - start_time
    console.print(
        Panel(
            f"[bold green]✓ Indexing Complete in {elapsed:.2f}s[/bold green]\n"
            f"Files: [bold]{stats['total_files']}[/bold] | Symbols: [bold]{stats['total_symbols']}[/bold] | "
            f"Chunks: [bold]{stats['total_chunks']}[/bold] | Call Graph Edges: [bold]{stats['call_graph_edges']}[/bold]",
            title="📊 Repository Stats",
            border_style="green",
        )
    )

    # If a query is provided directly
    if args.query:
        _perform_search(engine, args.query, args.type, args.limit, json_output=getattr(args, "json", False))
    else:
        # Interactive REPL mode
        console.print("[bold yellow]Interactive Search Mode. Type 'exit' to quit.[/bold yellow]\n")
        while True:
            try:
                query = console.input("[bold cyan]CodePulse Search > [/bold cyan]").strip()
                if query.lower() in ("exit", "quit", "q"):
                    break
                if not query:
                    continue

                _perform_search(engine, query, args.type, args.limit)
            except KeyboardInterrupt:
                break


def _perform_search(engine: IndexingEngine, query: str, search_type: str, limit: int, json_output: bool = False) -> None:
    t0 = time.time()
    results = engine.search(query, limit=limit, search_type=search_type)
    search_ms = (time.time() - t0) * 1000

    if json_output:
        print(json.dumps({"query": query, "search_type": search_type, "results": [r.to_dict() for r in results]}, indent=2, default=str))
        return

    if not results:
        console.print(f"[yellow]No matching code symbols found for '{query}'[/yellow]\n")
        return

    table = Table(
        title=f"🔎 Search Results for '{query}' ({search_type.upper()} - {search_ms:.1f}ms)",
        border_style="cyan",
        show_header=True,
    )
    table.add_column("Rank", style="dim", width=5)
    table.add_column("Symbol", style="bold green", width=24)
    table.add_column("File : Line", style="yellow", width=30)
    table.add_column("Score", style="bold cyan", width=8)
    table.add_column("Breakdown (KW / Vec / Graph)", style="magenta", width=25)
    table.add_column("Signature / Preview", style="white")

    for rank, res in enumerate(results, start=1):
        score_breakdown = f"{res.keyword_score:.2f} / {res.vector_score:.2f} / {res.graph_score:.2f}"
        table.add_row(
            str(rank),
            f"[{res.symbol_type}] {res.symbol_name}",
            f"{res.file_path}:{res.start_line}",
            f"{res.final_score:.2f}",
            score_breakdown,
            res.signature or res.snippet[:80],
        )

    console.print(table)
    console.print()


def cmd_impact(args: argparse.Namespace) -> None:
    """Performs upstream and downstream blast-radius impact analysis on a symbol."""
    path = args.path
    symbol_name = args.symbol
    engine = IndexingEngine()
    result = engine.index_directory(path)

    if not engine.call_graph:
        if getattr(args, "json", False):
            print(json.dumps({"error": "call_graph_not_available", "message": "Call graph not available."}))
        else:
            console.print("[red]Call graph not available.[/red]")
        return

    # Phase 4.2 (roadmap §37 item 4.2): redirected to the shared
    # `IndexingEngine.analyze_impact` intelligence-core method instead of
    # calling `CallGraphBuilder.get_callers`/`get_callees` directly (this
    # milestone's own audit found that duplicated the REST route's
    # identical logic a second time here). `result["repository_id"]` is
    # already returned by `index_directory` above -- no new CLI feature
    # or flag needed to plumb it through. The CLI's own presentation
    # (this Rich `Tree`) is unchanged.
    repository_id = result["repository_id"]
    impact = engine.analyze_impact(symbol_name, "", repository_id, max_depth=args.depth)
    if getattr(args, "json", False):
        print(json.dumps(impact, indent=2, default=str))
        return

    console.print(Panel(f"[bold magenta]Impact Analysis for symbol:[/bold magenta] [bold green]{symbol_name}[/bold green]"))
    callers = impact["call_chain"]
    callees = impact["dependencies"]

    # Tree visualization
    tree = Tree(f"[bold yellow]🎯 Symbol: {symbol_name}[/bold yellow]")

    callers_branch = tree.add(f"[bold red]▲ Blast Radius (Upstream Callers - {len(callers)} found)[/bold red]")
    for c in callers:
        callers_branch.add(f"[cyan]{c['symbol']}[/cyan] ({c['file']}) [dim]depth={c['depth']}[/dim] [dim]confidence={c.get('confidence')}[/dim]")

    callees_branch = tree.add(f"[bold blue]▼ Dependencies (Downstream Callees - {len(callees)} found)[/bold blue]")
    for c in callees:
        callees_branch.add(f"[cyan]{c['symbol']}[/cyan] ({c['file']}) [dim]depth={c['depth']}[/dim] [dim]confidence={c.get('confidence')}[/dim]")

    console.print(tree)
    console.print()


# --- Phase 6.1 (roadmap §37 item 6.1 "Full CLI command set"; §22 CLI
# Strategy; §45 backlog B21: "All §22 commands implemented with --json
# output"). Every command below follows the exact same established
# pattern `search`/`impact` already use: construct a fresh
# `IndexingEngine`, call `index_directory(path)` (cheap and idempotent
# for an already-indexed, unchanged repository, per Phase 3's own
# incremental-indexing machinery -- not a new "persistent session"
# mechanism), then delegate to the SAME shared `IndexingEngine.analyze_*`
# methods the REST/MCP adapters already call. No business logic is
# reimplemented here. --------------------------------------------------

def _print_json(data) -> None:
    print(json.dumps(data, indent=2, default=str))


def cmd_init(args: argparse.Namespace) -> None:
    """Creates a starter codepulse.toml in the target directory -- no infra required (§25 Local Deployment Strategy)."""
    target_dir = args.path
    os.makedirs(target_dir, exist_ok=True)
    config_path = os.path.join(target_dir, "codepulse.toml")
    if os.path.exists(config_path) and not args.force:
        console.print(f"[yellow]{config_path} already exists -- use --force to overwrite.[/yellow]")
        return

    from codepulse.config import CodePulseConfig
    defaults = CodePulseConfig()
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(
            "# CodePulse local configuration -- see CodePulse_Strategy_and_Roadmap.md §25.\n"
            "# Precedence: CLI flags > CODEPULSE_* env vars > this file > built-in defaults.\n\n"
            f'db_url = "{defaults.db_url}"\n'
            f'embedding_model = "{defaults.embedding_model}"\n'
        )
    console.print(f"[bold green]✓[/bold green] Created {config_path}")


def cmd_index(args: argparse.Namespace) -> None:
    """Full index of a repository (standalone -- `search` already indexes as a side effect; this command exists for a plain `codepulse index <path>` workflow with no query)."""
    engine = IndexingEngine()
    result = engine.index_directory(args.path, repo_name=os.path.basename(args.path))
    if args.json:
        _print_json(result)
        return
    console.print(
        Panel(
            f"[bold green]✓ Indexed[/bold green] {args.path}\n"
            f"Repository id: [bold]{result['repository_id']}[/bold] | "
            f"Files: [bold]{result.get('total_files', 0)}[/bold] | "
            f"Incremental: [bold]{result.get('incremental', False)}[/bold]",
            title="📊 Index Result",
            border_style="green",
        )
    )


def cmd_update(args: argparse.Namespace) -> None:
    """Incremental (git-diff based when available) re-index -- a thin, clearer-intent alias over the same index_directory() call, which already detects and prefers an incremental run automatically (Phase 3.1)."""
    engine = IndexingEngine()
    result = engine.index_directory(args.path, repo_name=os.path.basename(args.path))
    if args.json:
        _print_json(result)
        return
    kind = "incremental" if result.get("incremental") else "full"
    console.print(f"[bold green]✓[/bold green] Update complete ({kind}) -- {result.get('total_files', 0)} file(s) processed.")


def _resolve_symbol(engine: IndexingEngine, repo_id: int, name: str):
    """Looks up a symbol by (repo_id, name) -- a repo can have more than
    one symbol sharing a name (overloads, methods on different
    classes), so this returns the full match list, never silently
    picking one."""
    from codepulse.storage.db import FileModel, SymbolModel

    session = engine.storage.get_session()
    try:
        matches = (
            session.query(SymbolModel)
            .join(FileModel)
            .filter(FileModel.repo_id == repo_id, SymbolModel.name == name)
            .all()
        )
        return [
            {
                "id": s.id, "name": s.name, "type": s.type, "file_path": s.file.path,
                "signature": s.signature, "docstring": s.docstring, "body": s.body,
                "line_start": s.start_line, "line_end": s.end_line,
            }
            for s in matches
        ]
    finally:
        session.close()


def cmd_symbol(args: argparse.Namespace) -> None:
    """Looks up and displays one symbol's definition."""
    engine = IndexingEngine()
    result = engine.index_directory(args.path, repo_name=os.path.basename(args.path))
    matches = _resolve_symbol(engine, result["repository_id"], args.symbol)

    if not matches:
        if args.json:
            _print_json({"error": "symbol_not_found", "message": f"No symbol named {args.symbol!r}."})
        else:
            console.print(f"[red]No symbol named '{args.symbol}' found.[/red]")
        return

    if args.json:
        _print_json({"matches": matches})
        return

    for m in matches:
        console.print(
            Panel(
                f"[bold cyan]{m['signature']}[/bold cyan]\n\n"
                f"{m['docstring'] or '[dim](no docstring)[/dim]'}\n\n"
                f"[dim]{m['file_path']}:{m['line_start']}-{m['line_end']}[/dim]",
                title=f"[{m['type']}] {m['name']}",
                border_style="cyan",
            )
        )


def cmd_refs(args: argparse.Namespace) -> None:
    """Direct references (callers + direct subclasses) -- Phase 4.4's IndexingEngine.analyze_references, reused unmodified."""
    engine = IndexingEngine()
    result = engine.index_directory(args.path, repo_name=os.path.basename(args.path))
    refs = engine.analyze_references(args.symbol, "", result["repository_id"])
    if args.json:
        _print_json(refs)
        return

    tree = Tree(f"[bold yellow]References: {args.symbol}[/bold yellow]")
    callers_branch = tree.add(f"[bold cyan]Direct callers ({len(refs['callers'])})[/bold cyan]")
    for c in refs["callers"]:
        callers_branch.add(f"{c['symbol']} ({c['file']}) [dim]confidence={c.get('confidence')}[/dim]")
    sub_branch = tree.add(f"[bold magenta]Direct subclasses ({len(refs['subclasses'])})[/bold magenta]")
    for s in refs["subclasses"]:
        sub_branch.add(f"{s['symbol']} ({s['file']}) [dim]confidence={s.get('confidence')}[/dim]")
    console.print(tree)


def cmd_callers(args: argparse.Namespace) -> None:
    """Callers of a symbol, direct + transitive (a projection of IndexingEngine.analyze_impact)."""
    engine = IndexingEngine()
    result = engine.index_directory(args.path, repo_name=os.path.basename(args.path))
    impact = engine.analyze_impact(args.symbol, "", result["repository_id"], max_depth=args.depth)
    if args.json:
        _print_json({"symbol": args.symbol, "callers": impact["call_chain"]})
        return
    tree = Tree(f"[bold red]▲ Callers of {args.symbol} ({len(impact['call_chain'])} found)[/bold red]")
    for c in impact["call_chain"]:
        tree.add(f"{c['symbol']} ({c['file']}) [dim]depth={c['depth']} confidence={c.get('confidence')}[/dim]")
    console.print(tree)


def cmd_callees(args: argparse.Namespace) -> None:
    """Symbols called BY a symbol, direct + transitive (a projection of IndexingEngine.analyze_impact)."""
    engine = IndexingEngine()
    result = engine.index_directory(args.path, repo_name=os.path.basename(args.path))
    impact = engine.analyze_impact(args.symbol, "", result["repository_id"], max_depth=args.depth)
    if args.json:
        _print_json({"symbol": args.symbol, "callees": impact["dependencies"]})
        return
    tree = Tree(f"[bold blue]▼ Callees of {args.symbol} ({len(impact['dependencies'])} found)[/bold blue]")
    for c in impact["dependencies"]:
        tree.add(f"{c['symbol']} ({c['file']}) [dim]depth={c['depth']} confidence={c.get('confidence')}[/dim]")
    console.print(tree)


def cmd_similar(args: argparse.Namespace) -> None:
    """Semantically similar symbols (IndexingEngine.find_similar_symbols, Phase 5.1)."""
    engine = IndexingEngine()
    result = engine.index_directory(args.path, repo_name=os.path.basename(args.path))
    matches = _resolve_symbol(engine, result["repository_id"], args.symbol)
    if not matches:
        if args.json:
            _print_json({"error": "symbol_not_found", "message": f"No symbol named {args.symbol!r}."})
        else:
            console.print(f"[red]No symbol named '{args.symbol}' found.[/red]")
        return

    m = matches[0]
    query_text = f"{m['signature']}\n{m['docstring'] or ''}\n{m['body'][:500]}"
    similar = engine.find_similar_symbols(
        query_text, m["name"], result["repository_id"], threshold=args.threshold, limit=args.limit
    )
    if args.json:
        _print_json({"query_symbol": {"id": m["id"], "name": m["name"]}, "similar": similar})
        return

    table = Table(title=f"Symbols similar to {args.symbol}", border_style="cyan")
    table.add_column("Symbol", style="bold green")
    table.add_column("File", style="yellow")
    table.add_column("Relevance", style="cyan")
    for s in similar:
        table.add_row(s["symbol_name"], s["file_path"], f"{s['relevance_score']:.2f}")
    console.print(table)


def cmd_status(args: argparse.Namespace) -> None:
    """Index freshness -- last snapshot commit/time, file/symbol counts. Honest, not fabricated: no job history is reported (local CLI indexing runs synchronously and never creates IndexJob rows -- those are REST's async ingestion flow, Phase 1.3)."""
    from codepulse.storage.db import RepositoryModel, RepositorySnapshotModel

    engine = IndexingEngine()
    session = engine.storage.get_session()
    try:
        repo_url = os.path.abspath(args.path)
        repo = session.query(RepositoryModel).filter(RepositoryModel.url == repo_url).first()
        if not repo:
            payload = {"indexed": False, "path": repo_url}
            if args.json:
                _print_json(payload)
            else:
                console.print(f"[yellow]{repo_url} has not been indexed yet -- run `codepulse index {args.path}`.[/yellow]")
            return

        snapshot = (
            session.query(RepositorySnapshotModel)
            .filter(RepositorySnapshotModel.repo_id == repo.id)
            .order_by(RepositorySnapshotModel.id.desc())
            .first()
        )
        payload = {
            "indexed": True,
            "repository_id": repo.id,
            "path": repo_url,
            "file_count": repo.file_count,
            "symbol_count": repo.symbol_count,
            "last_snapshot_commit": snapshot.commit_sha if snapshot else None,
            "last_indexed_at": snapshot.indexed_at if snapshot else None,
        }
    finally:
        session.close()

    if args.json:
        _print_json(payload)
        return
    console.print(
        Panel(
            f"Repository id: [bold]{payload['repository_id']}[/bold]\n"
            f"Files: [bold]{payload['file_count']}[/bold] | Symbols: [bold]{payload['symbol_count']}[/bold]\n"
            f"Last snapshot commit: [bold]{payload['last_snapshot_commit'] or '(none / non-git)'}[/bold]\n"
            f"Last indexed at: [bold]{payload['last_indexed_at']}[/bold]",
            title=f"📈 Status: {args.path}",
            border_style="cyan",
        )
    )


def cmd_modules(args: argparse.Namespace) -> None:
    """
    Module clusters + raw inbound/outbound degree rankings, via
    IndexingEngine.analyze_module_structure (Phase 4.3's ModuleGraphBuilder,
    reused unmodified). Backend-hardening addition (not a roadmap sprint
    item) -- this engine capability existed since Phase 4.3 with no CLI
    caller until now.
    """
    engine = IndexingEngine()
    result = engine.index_directory(args.path, repo_name=os.path.basename(args.path))
    module_graph = engine.analyze_module_structure(result["repository_id"])
    payload = {
        "repository_id": result["repository_id"],
        "clusters": module_graph.clusters,
        "ranked_by_inbound": module_graph.rank_by_inbound(),
        "ranked_by_outbound": module_graph.rank_by_outbound(),
    }
    if args.json:
        _print_json(payload)
        return

    tree = Tree(f"[bold magenta]Module clusters ({len(payload['clusters'])})[/bold magenta]")
    for cluster in payload["clusters"]:
        branch = tree.add(f"[bold]Cluster {cluster['id']}[/bold] ({len(cluster['files'])} files)")
        for f in cluster["files"]:
            branch.add(f)
    console.print(tree)

    table = Table(title="Top files by inbound edges (possible API/entry points, per §11's literal heuristic)", border_style="cyan")
    table.add_column("File", style="yellow")
    table.add_column("In", justify="right")
    table.add_column("Out", justify="right")
    for row in payload["ranked_by_inbound"][:10]:
        table.add_row(row["file"], str(row["in_degree"]), str(row["out_degree"]))
    console.print(table)


def cmd_doctor(args: argparse.Namespace) -> None:
    """Environment/config diagnostics -- embedding model loads? DB reachable? ignore rules active?"""
    from codepulse.config import get_settings
    from codepulse.indexer.ignore import RepositoryIgnoreRules

    settings = get_settings()
    checks: list[dict] = []

    checks.append({"check": "config", "ok": True, "detail": f"db_url={settings.db_url}, embedding_model={settings.embedding_model}"})

    try:
        storage = StorageManager(db_url=settings.db_url)
        session = storage.get_session()
        session.execute(text("SELECT 1"))
        session.close()
        checks.append({"check": "database_reachable", "ok": True, "detail": settings.db_url})
    except Exception as e:
        checks.append({"check": "database_reachable", "ok": False, "detail": str(e)})

    try:
        embedder = CodeEmbedder()
        _ = embedder.encode("def f(): pass")
        checks.append({"check": "embedding_model_loads", "ok": True, "detail": settings.embedding_model})
    except Exception as e:
        checks.append({"check": "embedding_model_loads", "ok": False, "detail": str(e)})

    target = args.path or "."
    try:
        rules = RepositoryIgnoreRules(target)
        checks.append({"check": "ignore_rules_active", "ok": True, "detail": f"{target}"})
    except Exception as e:
        checks.append({"check": "ignore_rules_active", "ok": False, "detail": str(e)})

    if args.json:
        _print_json({"checks": checks})
        return

    table = Table(title="🩺 CodePulse Doctor", border_style="green")
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Detail", style="dim")
    for c in checks:
        status = "[bold green]OK[/bold green]" if c["ok"] else "[bold red]FAIL[/bold red]"
        table.add_row(c["check"], status, c["detail"])
    console.print(table)


def resolve_mcp_api_key(cli_value: str | None, env: Mapping[str, str] | None = None) -> str | None:
    """
    Resolves the raw API key `--mcp` scopes its connection to, per §13.

    `--api-key` takes priority when given. Otherwise falls back to the
    `CODEPULSE_MCP_API_KEY` environment variable -- added specifically so
    a process SPAWNING `codepulse serve --mcp` (the VS Code extension's
    Phase 3 MCP integration; any other MCP client launcher) never has to
    put a secret directly into that child process's command-line
    arguments, which are visible to other processes on the same machine
    (`ps`/Task Manager, `/proc/[pid]/cmdline`) -- a real, avoidable
    exposure `--api-key`-only would otherwise force on every caller.
    """
    if cli_value:
        return cli_value
    return (os.environ if env is None else env).get("CODEPULSE_MCP_API_KEY") or None


def cmd_serve(args: argparse.Namespace) -> None:
    """Starts the local REST API (default), or the MCP server over stdio with --mcp (§22: "start local API/MCP server")."""
    if args.mcp:
        api_key = resolve_mcp_api_key(args.api_key)
        if not api_key:
            console.print(
                "[bold red]Error:[/bold red] --api-key (or the CODEPULSE_MCP_API_KEY environment "
                "variable) is required for --mcp (MCP connections are scoped to repositories at "
                "connection-setup time, §13)."
            )
            sys.exit(1)
        import asyncio
        from codepulse.config import get_settings
        from codepulse.embeddings.embedder import CodeEmbedder as _Embedder
        from codepulse.mcp_server import MCPAuthenticationError, build_mcp_server

        settings = get_settings()
        storage = StorageManager(db_url=settings.db_url)
        engine = IndexingEngine(storage=storage, embedder=_Embedder())
        try:
            mcp = build_mcp_server(storage, engine, api_key)
        except MCPAuthenticationError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)
        console.print("[bold green]✓[/bold green] MCP server starting (stdio transport)...")
        asyncio.run(mcp.run_stdio_async())
        return

    import uvicorn
    console.print(f"[bold green]✓[/bold green] Starting CodePulse API on http://{args.host}:{args.port}")
    uvicorn.run("codepulse.api.app:app", host=args.host, port=args.port)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="codepulse",
        description="CodePulse: Semantic Codebase Intelligence Engine",
    )
    parser.add_argument("--config", "-c", help="Path to codepulse.toml config file", default=None)
    parser.add_argument("--db-url", help="Override database URL", default=None)
    parser.add_argument("--embedding-model", help="Override embedding model name", default=None)

    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. Parse command
    parse_p = subparsers.add_parser("parse", help="Parse a file and view AST symbols, complexity, and calls")
    parse_p.add_argument("file_path", help="Path to source code file")

    # 2. Search / Index command
    search_p = subparsers.add_parser("search", help="Index and search a repository")
    search_p.add_argument("path", help="Repository path to index")
    search_p.add_argument("--query", "-q", help="Search query (natural language or code identifier)", default=None)
    search_p.add_argument("--type", "-t", choices=["hybrid", "keyword", "semantic"], default="hybrid")
    search_p.add_argument("--limit", "-l", type=int, default=10)
    search_p.add_argument("--json", action="store_true", help="Emit raw JSON instead of a formatted table (requires --query)")

    # 3. Impact analysis command
    impact_p = subparsers.add_parser("impact", help="Analyze call graph impact and blast radius")
    impact_p.add_argument("path", help="Repository path")
    impact_p.add_argument("symbol", help="Symbol name to analyze")
    impact_p.add_argument("--depth", "-d", type=int, default=3)
    impact_p.add_argument("--json", action="store_true", help="Emit raw JSON instead of a formatted tree")

    # 4. init
    init_p = subparsers.add_parser("init", help="Create a local codepulse.toml config -- no infra required")
    init_p.add_argument("path", nargs="?", default=".", help="Directory to create codepulse.toml in (default: cwd)")
    init_p.add_argument("--force", action="store_true", help="Overwrite an existing codepulse.toml")

    # 5. index
    index_p = subparsers.add_parser("index", help="Full index of a repository")
    index_p.add_argument("path", help="Repository path")
    index_p.add_argument("--json", action="store_true")

    # 6. update
    update_p = subparsers.add_parser("update", help="Incremental (git-diff based when available) re-index")
    update_p.add_argument("path", help="Repository path")
    update_p.add_argument("--json", action="store_true")

    # 7. symbol
    symbol_p = subparsers.add_parser("symbol", help="Look up a symbol's definition")
    symbol_p.add_argument("path", help="Repository path")
    symbol_p.add_argument("symbol", help="Symbol name")
    symbol_p.add_argument("--json", action="store_true")

    # 8. refs
    refs_p = subparsers.add_parser("refs", help="Direct references (callers + direct subclasses) of a symbol")
    refs_p.add_argument("path", help="Repository path")
    refs_p.add_argument("symbol", help="Symbol name")
    refs_p.add_argument("--json", action="store_true")

    # 9. callers
    callers_p = subparsers.add_parser("callers", help="Callers of a symbol, direct and transitive")
    callers_p.add_argument("path", help="Repository path")
    callers_p.add_argument("symbol", help="Symbol name")
    callers_p.add_argument("--depth", "-d", type=int, default=3)
    callers_p.add_argument("--json", action="store_true")

    # 10. callees
    callees_p = subparsers.add_parser("callees", help="Symbols called by a symbol, direct and transitive")
    callees_p.add_argument("path", help="Repository path")
    callees_p.add_argument("symbol", help="Symbol name")
    callees_p.add_argument("--depth", "-d", type=int, default=3)
    callees_p.add_argument("--json", action="store_true")

    # 11. similar
    similar_p = subparsers.add_parser("similar", help="Semantically similar symbols")
    similar_p.add_argument("path", help="Repository path")
    similar_p.add_argument("symbol", help="Symbol name")
    similar_p.add_argument("--threshold", type=float, default=0.75)
    similar_p.add_argument("--limit", "-l", type=int, default=10)
    similar_p.add_argument("--json", action="store_true")

    # 12. status
    status_p = subparsers.add_parser("status", help="Index freshness for a repository")
    status_p.add_argument("path", help="Repository path")
    status_p.add_argument("--json", action="store_true")

    # 12b. modules
    modules_p = subparsers.add_parser("modules", help="Module clusters + inbound/outbound degree rankings")
    modules_p.add_argument("path", help="Repository path")
    modules_p.add_argument("--json", action="store_true")

    # 13. doctor
    doctor_p = subparsers.add_parser("doctor", help="Environment/config diagnostics")
    doctor_p.add_argument("path", nargs="?", default=None, help="Directory to check ignore rules against (default: cwd)")
    doctor_p.add_argument("--json", action="store_true")

    # 14. serve
    serve_p = subparsers.add_parser("serve", help="Start the local API server (or the MCP server with --mcp)")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8000)
    serve_p.add_argument("--mcp", action="store_true", help="Start the MCP server (stdio transport) instead of the REST API")
    serve_p.add_argument(
        "--api-key",
        default=None,
        help=(
            "Raw API key to scope the MCP connection to (required with --mcp, unless "
            "CODEPULSE_MCP_API_KEY is set in the environment instead -- preferred for "
            "spawned/managed processes, since CLI args are visible to other processes "
            "on the same machine)"
        ),
    )

    args = parser.parse_args()

    # Initialize configuration with CLI overrides
    cli_overrides = {}
    if args.db_url:
        cli_overrides["db_url"] = args.db_url
    if args.embedding_model:
        cli_overrides["embedding_model"] = args.embedding_model

    from codepulse.config import get_settings
    settings = get_settings(config_file=args.config, cli_overrides=cli_overrides, reload=True)

    # Backend-hardening addition (not a roadmap sprint item): db_url
    # defaults to "sqlite:///codepulse.db", resolved relative to the
    # CURRENT WORKING DIRECTORY -- with zero prior feedback about which
    # actual file that is. Running the same command from two different
    # directories has always silently used two different databases (no
    # error, no warning), a real footgun this discloses up front rather
    # than leaving a user to discover it only once results look wrong.
    # Skipped for `parse` and `init`, which never touch a database at all.
    if args.command not in ("parse", "init"):
        resolved_db = _display_db_path(settings.db_url)
        db_file = resolved_db[len("sqlite:///"):] if resolved_db.startswith("sqlite:///") else None
        # A resolved path is genuinely more alarming when the file
        # doesn't exist yet -- that means THIS command is about to
        # silently create a brand-new, empty database (the exact
        # "wrong directory" mistake this whole diagnostic exists to
        # catch), not just reuse one that already has prior work in
        # it. A quiet path-only line doesn't mean much to a user who
        # has no reason yet to suspect anything is wrong; "a new,
        # empty database is being created" does.
        if db_file is not None and not os.path.exists(db_file):
            _stderr_console.print(
                f"[yellow]⚠ No existing database found -- creating a NEW, empty one at:[/yellow]\n"
                f"[yellow]  {resolved_db}[/yellow]\n"
                f"[dim]If you expected to reuse an existing database instead, check your current "
                f"directory, or set --db-url / CODEPULSE_DB_URL explicitly.[/dim]"
            )
        else:
            _stderr_console.print(f"[dim]Using database: {resolved_db}[/dim]")

    if args.command == "parse":
        cmd_parse(args)
    elif args.command == "search":
        cmd_index_and_search(args)
    elif args.command == "impact":
        cmd_impact(args)
    elif args.command == "init":
        cmd_init(args)
    elif args.command == "index":
        cmd_index(args)
    elif args.command == "update":
        cmd_update(args)
    elif args.command == "symbol":
        cmd_symbol(args)
    elif args.command == "refs":
        cmd_refs(args)
    elif args.command == "callers":
        cmd_callers(args)
    elif args.command == "callees":
        cmd_callees(args)
    elif args.command == "similar":
        cmd_similar(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "modules":
        cmd_modules(args)
    elif args.command == "doctor":
        cmd_doctor(args)
    elif args.command == "serve":
        cmd_serve(args)


if __name__ == "__main__":
    main()
