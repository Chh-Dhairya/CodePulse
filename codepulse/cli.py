from __future__ import annotations

import argparse
import os
import sys
import time

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.syntax import Syntax
from rich.table import Table
from rich.tree import Tree

from codepulse.embeddings.embedder import CodeEmbedder
from codepulse.indexer.engine import IndexingEngine
from codepulse.parser.factory import get_parser_for_file
from codepulse.parser.graph import CallGraphBuilder

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

console = Console(safe_box=True)


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
        _perform_search(engine, args.query, args.type, args.limit)
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


def _perform_search(engine: IndexingEngine, query: str, search_type: str, limit: int) -> None:
    t0 = time.time()
    results = engine.search(query, limit=limit, search_type=search_type)
    search_ms = (time.time() - t0) * 1000

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
        console.print("[red]Call graph not available.[/red]")
        return

    console.print(Panel(f"[bold magenta]Impact Analysis for symbol:[/bold magenta] [bold green]{symbol_name}[/bold green]"))

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

    # 3. Impact analysis command
    impact_p = subparsers.add_parser("impact", help="Analyze call graph impact and blast radius")
    impact_p.add_argument("path", help="Repository path")
    impact_p.add_argument("symbol", help="Symbol name to analyze")
    impact_p.add_argument("--depth", "-d", type=int, default=3)

    args = parser.parse_args()

    # Initialize configuration with CLI overrides
    cli_overrides = {}
    if args.db_url:
        cli_overrides["db_url"] = args.db_url
    if args.embedding_model:
        cli_overrides["embedding_model"] = args.embedding_model

    from codepulse.config import get_settings
    get_settings(config_file=args.config, cli_overrides=cli_overrides, reload=True)

    if args.command == "parse":
        cmd_parse(args)
    elif args.command == "search":
        cmd_index_and_search(args)
    elif args.command == "impact":
        cmd_impact(args)


if __name__ == "__main__":
    main()
