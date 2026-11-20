"""
CLI Workspace Module - Read-Only Dashboard and Directory Listing

This module implements the `searchify status`, `searchify dirs`, and welcome
commands. All operations are read-only - no data is initialized, indexed,
or processed.

Commands:
- status: Comprehensive health and sync status dashboard
- welcome: Default command (shows banner + status)
- directories: List registered directories with rules

Key principle: Never modifies state; safe to run anytime.

Used by: CLI app.py command dispatcher
"""

import sqlite3
from contextlib import closing
from pathlib import Path

from searchify.cli.output import line, literal, table


def local_paths():
    """
    Get local database and reports directory paths from settings.

    Returns:
        Tuple of (LOCAL_DB_PATH, REPORTS_DIR)
    """
    from searchify.config.settings import settings

    return settings.LOCAL_DB_PATH, settings.REPORTS_DIR


def read_workspace(db_path):
    """
    Read workspace metadata from local SQLite database.

    Opens database in read-only mode for safe concurrent access.

    Args:
        db_path: Path to SQLite database

    Returns:
        Dict with database status, directory list with file counts, total indexed
    """
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        return {"database": "not created", "directories": [], "indexed": None}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "SELECT d.id,d.path,COUNT(f.path) FROM directories d LEFT JOIN files f ON f.root_id=d.id GROUP BY d.id,d.path ORDER BY d.path"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    return {"database": "available", "directories": rows, "indexed": total}


def status(args):
    """
    Display comprehensive status dashboard.

    Shows:
    - Local database status
    - Registered directories with availability
    - File counts (indexed, supported, unsupported)
    - Workspace metrics (new, modified, deleted, etc.)
    - Sync state counts (completed, pending, failed, etc.)
    - Last sync timestamps
    - AI model configuration
    - Provider connectivity
    - Authentication status
    - Health issues

    Args:
        args: Parsed command arguments (scan, offline flags)

    Returns:
        Exit code (0=OK, 1=database unavailable)
    """
    from searchify.services.health_service import HealthService

    db, reports = local_paths()
    try:
        snapshot = HealthService(db, reports).snapshot(
            scan=getattr(args, "scan", False), offline=getattr(args, "offline", False)
        )
    except (sqlite3.Error, OSError, ValueError):
        line("Local database: unavailable or incompatible; no data was changed.")
        return 1
    line("SEARCHIFY STATUS")
    line(f"Local database: {snapshot['local_database']}")
    line(
        f"Registered directories: {len(snapshot['directories']) if snapshot['local_database'] != 'unavailable' else 'unknown'}"
    )
    line(f"Indexed file records: {snapshot['indexed_files']}")
    line(
        f"Supported: {snapshot['supported_files']} | Unsupported: {snapshot['unsupported_files']}"
    )
    for directory in snapshot["directories"]:
        line(
            f"  {literal(directory['path'])} — {'available' if directory['available'] else 'unavailable'}"
        )
        line(
            "    Exclusions: "
            + ", ".join(literal(p) for p in directory["rules"]["exclude"])
        )
    for key, value in snapshot["workspace"].items():
        line(
            f"{key.replace('_', ' ').capitalize()}: {value if value is not None else 'not scanned (status --scan)'}"
        )
    table(
        "Synchronization (local journal, checked against current file metadata)",
        ["State", "Files"],
        list(snapshot["statuses"].items()),
    )
    line(f"Remaining: {snapshot['remaining']}")
    line(f"Last sync: {snapshot['last_sync'] or 'no local history'}")
    line(
        f"Last successful sync: {snapshot['last_successful_sync'] or 'no local history'}"
    )
    line(f"Summarization: {literal(snapshot['models']['summarization'])}")
    line(
        f"Embeddings: {snapshot['models']['provider']} / {literal(snapshot['models']['embedding'])} / {snapshot['models']['dimensions']} dimensions"
    )
    table(
        "AI and cloud services",
        ["Service", "Connectivity"],
        [(name, value["status"]) for name, value in snapshot["providers"].items()],
    )
    line("Authentication: " + snapshot["authentication"]["status"])
    line(
        "Owner-scoped database connectivity: "
        + snapshot["authentication"].get("database", "not verified")
    )
    line("Runtime dimensions: " + snapshot["models"]["runtime_dimensions"])
    line(
        "Outstanding health issues: "
        + ("; ".join(snapshot["errors"]) if snapshot["errors"] else "none detected")
    )
    line(
        "Saved recommendations awaiting review: "
        + (
            str(snapshot["pending_recommendations"])
            if snapshot["pending_recommendations"] is not None
            else "not evaluated; use searchify review --save"
        )
    )
    line(
        "No indexing, summarization, embedding generation or synchronization was started."
    )
    return 1 if snapshot["local_database"] == "unavailable" else 0


def welcome(args):
    """
    Default command: show banner and status.

    Args:
        args: Parsed command arguments

    Returns:
        Exit code from status()
    """
    try:
        from searchify.ui.mascot import show_banner

        show_banner()
    except ImportError:
        line("(=^･ω･^=)  SEARCHIFY — your local-first file assistant")
    result = status(args)
    line("Run searchify help to discover commands.")
    return result


def directories(args):
    """
    List registered directories with their rules.

    Shows directory ID, path, availability, profile, and sync status.

    Args:
        args: Parsed command arguments

    Returns:
        Exit code (0)
    """
    from searchify.core.file_registry import FileRegistry

    db, _ = local_paths()
    registry = FileRegistry(db)
    dirs = registry.directories()
    if not dirs:
        line("No registered directories. Register one with searchify dirs add PATH.")
    for directory in dirs:
        rules = registry.rules(directory["id"])
        line(
            f"{directory['id']}  {literal(directory['path'])}  ({'available' if Path(directory['path']).is_dir() else 'unavailable'}; {rules.profile}; sync={'on' if rules.sync_enabled else 'off'})"
        )
    return 0
