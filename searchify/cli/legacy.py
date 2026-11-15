"""Small adapters that reuse the working indexer and semantic search service."""
from pathlib import Path
import sys
from searchify.cli.output import line, literal


def add(args):
    path = Path(args.directory).expanduser().resolve()
    if not path.is_dir():
        print('Directory does not exist or is not a directory.', file=sys.stderr)
        return 1
    from searchify.indexer import register_directory
    register_directory(path)
    return 0


def index(args):
    from searchify.indexer import index_all
    index_all()
    return 0


def search(args):
    if not args.query.strip():
        print('Search query must not be empty.', file=sys.stderr)
        return 2
    if not args.semantic:
        from searchify.indexer import search_files
        search_files(args.query, args.limit)
        return 0
    from searchify.services.search_service import SearchService
    from searchify.core.file_registry import FileRegistry
    from searchify.core.scanner import matches, PROTECTED, PROTECTED_DIRS
    from searchify.cli.workspace import local_paths
    db,_=local_paths(); registry=FileRegistry(db)
    directories=sorted(registry.directories(),key=lambda d:len(Path(d['path']).parts),reverse=True)
    rules={d['id']:registry.rules(d['id']) for d in directories}
    ranked=SearchService().search(args.query, limit=args.limit)
    results=[]
    # Only resolve/stat candidates after semantic ranking; never read their contents.
    for result in ranked:
        path=Path(result['path']).expanduser().absolute()
        if '..' in path.parts: continue
        directory=next((d for d in directories if path.is_relative_to(Path(d['path']))),None)
        if directory is None: continue
        if path.is_symlink() or not path.is_file(): continue
        resolved=path.resolve()
        if not resolved.is_relative_to(Path(directory['path'])): continue
        relative=resolved.relative_to(directory['path']).as_posix(); rule=rules[directory['id']]
        if (matches(relative,rule.include) and not matches(relative,rule.exclude) and not matches(path.name,PROTECTED)
                and not any(part in PROTECTED_DIRS for part in Path(relative).parts)):
            results.append(result)
    if not results:
        line('No matching summaries found.')
    for position, result in enumerate(results, 1):
        line(f"{position}. {literal(result['path'])}  similarity={result['similarity']:.4f}")
        line(f"   {literal(result['summary'])}")
    return 0
