"""Explicit per-directory rule management and metadata-only unregistration."""

import json
import sys
from pathlib import Path

from searchify.cli.output import line, literal
from searchify.cli.workspace import local_paths
from searchify.core.file_registry import FileRegistry
from searchify.core.scanner import DirectoryRules


def registered(registry, path):
    root = str(Path(path).expanduser().resolve())
    row = next((row for row in registry.directories() if row["path"] == root), None)
    if row is None:
        raise ValueError("Directory is not registered.")
    return row


def add(args):
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
