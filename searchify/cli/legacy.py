"""
CLI Legacy Module - Compatibility Adapters

This module provides backward-compatible command implementations that
delegate to the working indexer and semantic search service. These
are used as legacy aliases for the new command structure.

Commands:
- add: Alias for `dirs add` (register directory)
- index: Alias for metadata index refresh
- search: Legacy search supporting both metadata (FTS5) and semantic modes

Search modes:
- Metadata search (default): Uses local SQLite FTS5 index on filenames
- Semantic search (--semantic): Uses Supabase pgvector on summaries

Both modes respect directory rules (include/exclude/protected) and
never read original file contents during search.

Used by: CLI app.py command dispatcher (legacy aliases)
"""

import sys
from pathlib import Path

from searchify.cli.output import line, literal


def add(args):
    """
    Legacy alias for directory registration.

    Args:
        args: Parsed arguments (directory)

    Returns:
        Exit code
    """
    path = Path(args.directory).expanduser().resolve()
    if not path.is_dir():
        print("Directory does not exist or is not a directory.", file=sys.stderr)
        return 1
    from searchify.indexer import register_directory

    register_directory(path)
    return 0


def index(args):
    """
    Legacy alias for explicit metadata index refresh.

    Args:
        args: Parsed arguments

    Returns:
        Exit code
    """
    from searchify.indexer import index_all

    index_all()
    return 0


def search(args):
    """
    Execute legacy search command.

    Supports two modes:
    1. Metadata search (default): Local FTS5 on indexed filenames
    2. Semantic search (--semantic): Vector similarity on Supabase summaries

    Both modes filter results by directory rules and protected patterns.

    Args:
        args: Parsed arguments (query, limit, semantic)

    Returns:
        Exit code
    """
    if not args.query.strip():
        print("Search query must not be empty.", file=sys.stderr)
        return 2
    if not args.semantic:
        from searchify.indexer import search_files

        search_files(args.query, args.limit)
        return 0
    from searchify.cli.workspace import local_paths
    from searchify.core.file_registry import FileRegistry
    from searchify.core.scanner import PROTECTED, PROTECTED_DIRS, matches
    from searchify.services.search_service import SearchService

    db, _ = local_paths()
    registry = FileRegistry(db)
    directories = sorted(
        registry.directories(), key=lambda d: len(Path(d["path"]).parts), reverse=True
    )
    rules = {d["id"]: registry.rules(d["id"]) for d in directories}
    ranked = SearchService().search(args.query, limit=args.limit)
    results = []
    # Only resolve/stat candidates after semantic ranking; never read their contents.
    for result in ranked:
        path = Path(result["path"]).expanduser().absolute()
        if ".." in path.parts:
            continue
        directory = next(
            (d for d in directories if path.is_relative_to(Path(d["path"]))), None
        )
        if directory is None:
            continue
        if path.is_symlink() or not path.is_file():
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(Path(directory["path"])):
            continue
        relative = resolved.relative_to(directory["path"]).as_posix()
        rule = rules[directory["id"]]
        if (
            matches(relative, rule.include)
            and not matches(relative, rule.exclude)
            and not matches(path.name, PROTECTED)
            and not any(part in PROTECTED_DIRS for part in Path(relative).parts)
        ):
            results.append(result)
    if not results:
        line("No matching summaries found.")
    for position, result in enumerate(results, 1):
        line(
            f"{position}. {literal(result['path'])}  similarity={result['similarity']:.4f}"
        )
        line(f"   {literal(result['summary'])}")
    return 0
