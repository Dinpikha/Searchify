"""
CLI Directories Module - Explicit Per-Directory Rule Management

This module implements the `searchify dirs` subcommands for managing
registered directories and their scanning rules. All operations are
metadata-only - no files are moved, deleted, or synchronized.

Commands:
- add: Register a directory with a profile (source, android, documents)
- show: Display directory rules as JSON
- configure: Modify rules (include/exclude/extensions/max-size/sync)
- remove: Unregister directory (metadata only, with confirmation)

Key principles:
- Explicit registration: Only user-specified directories are scanned
- Per-directory rules: Each root has independent include/exclude/extensions
- Metadata-only removal: Original files and remote records preserved
- Workspace locking: Concurrent modifications prevented

Profiles:
- source: Default for code (excludes build, deps, cache dirs)
- android: Android projects (excludes build, .gradle, generated)
- documents: Text/markdown files (excludes .git, temp files)

Used by: CLI app.py command dispatcher
"""

import json
import sys
from pathlib import Path

from searchify.cli.output import line, literal
from searchify.cli.workspace import local_paths
from searchify.core.file_registry import FileRegistry
from searchify.core.scanner import DirectoryRules


def registered(registry, path):
    """
    Find registered directory by path.

    Args:
        registry: FileRegistry instance
        path: Directory path to find

    Returns:
        Directory row dict

    Raises:
        ValueError: If directory not registered
    """
    root = str(Path(path).expanduser().resolve())
    row = next((row for row in registry.directories() if row["path"] == root), None)
    if row is None:
        raise ValueError("Directory is not registered.")
    return row


def add(args):
    """
    Register a new directory with specified profile.

    Args:
        args: Parsed arguments (directory, profile)

    Returns:
        Exit code (0=success, 1=error)
    """
    db, _ = local_paths()
    registry = FileRegistry(db)
    try:
        root_id = registry.register(args.directory, DirectoryRules.preset(args.profile))
    except ValueError:
        print(
            "Directory does not exist or its rule profile is invalid.", file=sys.stderr
        )
        return 1
    line(f"Registered directory {root_id}. No indexing or synchronization was started.")
    return 0


def show(args):
    """
    Display rules for a registered directory.

    Args:
        args: Parsed arguments (directory)

    Returns:
        Exit code (0=success, 1=not registered)
    """
    db, _ = local_paths()
    registry = FileRegistry(db)
    try:
        row = registered(registry, args.directory)
    except ValueError:
        line("Directory is not registered.")
        return 1
    line(literal(row["path"]))
    line(json.dumps(registry.rules(row["id"]).to_dict(), indent=2))
    line("Secret/key files and symlinks are protected independently of these rules.")
    return 0


def configure(args):
    """
    Modify rules for a registered directory.

    Applies changes to include, exclude, extensions, max_size, or sync.
    Validates rules before saving. Acquires workspace lock for atomic update.

    Args:
        args: Parsed arguments (directory, profile, include, exclude, extensions, max_size, sync)

    Returns:
        Exit code (0=success, 1=validation error)
    """
    db, _ = local_paths()
    registry = FileRegistry(db)
    try:
        row = registered(registry, args.directory)
        rules = (
            DirectoryRules.preset(args.profile)
            if args.profile
            else registry.rules(row["id"])
        )
        if args.include is not None:
            rules.include = args.include
        if args.exclude is not None:
            rules.exclude = args.exclude
        if args.extensions is not None:
            rules.extensions = [
                v.strip() for v in args.extensions.split(",") if v.strip()
            ]
        if args.max_size is not None:
            rules.max_size = args.max_size
        if args.sync is not None:
            rules.sync_enabled = args.sync == "on"
        # Re-validate through from_dict
        rules = DirectoryRules.from_dict(rules.to_dict())
        with registry.lock():
            registry.save_rules(row["id"], rules)
    except ValueError:
        line(
            "Invalid rules or unregistered directory. Use implemented text extensions and a size between 1 and 2097152 bytes."
        )
        return 1
    line(
        "Directory-specific rules saved. No files were moved, deleted or synchronized."
    )
    return 0


def remove(args):
    """
    Unregister a directory (metadata only).

    Requires explicit confirmation unless --yes flag provided.
    Never deletes original files or remote Supabase records.
    Acquires workspace lock for atomic operation.

    Args:
        args: Parsed arguments (directory, yes)

    Returns:
        Exit code (0=success, 1=not registered/cancelled)
    """
    db, _ = local_paths()
    registry = FileRegistry(db)
    try:
        row = registered(registry, args.directory)
    except ValueError:
        line("Directory is not registered.")
        return 1
    line(
        "This removes local registration and cached metadata only; original files and remote records are retained."
    )
    line(literal(row["path"]))
    if not args.yes:
        try:
            confirmed = input("Type unregister to confirm: ").strip() == "unregister"
        except EOFError:
            confirmed = False
        if not confirmed:
            line("Cancelled; no data changed.")
            return 0
    with registry.lock():
        registry.unregister(row["path"])
    line("Directory unregistered. It will no longer be scanned by Searchify.")
    return 0
