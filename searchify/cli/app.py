"""
CLI Application - Unified Command Discovery and Dispatch

This module defines the Searchify command-line interface with lazy loading
of operational dependencies. It builds the argument parser, registers all
commands and subcommands, and dispatches to handler functions.

Key design:
- Lazy imports: Command modules only imported when invoked
- Safe argument builders: Can be imported without AI/SDK dependencies
- Unified error handling: Sanitizes exceptions to prevent credential leakage
- Handler format: "module:function" strings for dynamic dispatch

Commands:
- help: Show command help
- status: Show workspace, sync, and health status
- doctor: Diagnose configuration and connectivity
- sync: Synchronize registered files
- summarize: Process pending summaries
- report: View sync reports
- dirs: Manage registered directories (add, remove, show, configure)
- review: Review project activity and generate recommendations
- organize: Propose file organization (no execution)
- search: Search metadata or semantic summaries
- add/index: Legacy compatibility aliases

Used by: __main__.py (entry point)
"""

import argparse
import importlib
import sys


def positive_int(value):
    """
    Validate positive integer argument.

    Args:
        value: String value to parse

    Returns:
        Positive integer

    Raises:
        argparse.ArgumentTypeError: If not a positive integer
    """
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def result_limit(value):
    """
    Validate search result limit (1-1000).

    Args:
        value: String value to parse

    Returns:
        Validated integer between 1 and 1000

    Raises:
        argparse.ArgumentTypeError: If out of range
    """
    number = positive_int(value)
    if number > 1000:
        raise argparse.ArgumentTypeError("must be between 1 and 1000")
    return number


def build_parser():
    """
    Build the complete argument parser with all commands.

    Creates ArgumentParser with subparsers for each command.
    Uses lazy handler strings ("module:function") for dynamic import.

    Returns:
        Tuple of (parser, pages_dict) where pages_dict maps command tuples to parsers
    """
    parser = argparse.ArgumentParser(
        prog="searchify",
        description="Local-first file discovery and management.",
        epilog="Run searchify help COMMAND for details. Review and organize propose changes only.",
    )
    parser.set_defaults(handler="searchify.cli.workspace:welcome")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    pages = {(): parser}

    def command(name, help_text, handler, **kwargs):
        """
        Register a command with subparser.

        Args:
            name: Command name
            help_text: Help description
            handler: Handler string "module:function"
            **kwargs: Additional ArgumentParser arguments

        Returns:
            Configured subparser
        """
        page = commands.add_parser(
            name, help=help_text, description=help_text, **kwargs
        )
        page.set_defaults(handler=handler)
        pages[(name,)] = page
        return page

    # Help command
    help_page = command("help", "Show all commands or help for a command.", "help")
    help_page.add_argument(
        "topic", nargs="*", help="Command and optional subcommand, e.g. dirs add"
    )

    # Status command
    status = command(
        "status",
        "Show workspace, synchronization and read-only service health.",
        "searchify.cli.workspace:status",
    )
    status.add_argument(
        "--offline", action="store_true", help="Skip provider connectivity probes"
    )
    status.add_argument(
        "--scan",
        action="store_true",
        default=True,
        help="Scan registered filenames and metadata (default); never extract contents",
    )
    status.add_argument(
        "--no-scan",
        dest="scan",
        action="store_false",
        help="Use indexed metadata without searching for new files",
    )

    # Doctor command
    doctor = command(
        "doctor",
        "Diagnose configuration, dependencies and connectivity without refreshing credentials.",
        "searchify.cli.doctor:run",
    )
    doctor_checks = doctor.add_mutually_exclusive_group()
    doctor_checks.add_argument(
        "--offline", action="store_true", help="Skip provider connectivity probes"
    )
    doctor_checks.add_argument(
        "--auth",
        action="store_true",
        help="Inspect the cached keyring session; macOS may request Keychain access",
    )

    # Import argument builders (safe - no AI/SDK dependencies)
    from searchify.cli.summarize import add_arguments as summarize_arguments
    from searchify.cli.sync import add_arguments as sync_arguments

    # Sync command
    sync_arguments(
        command(
            "sync",
            "Synchronize registered files using the existing sync engine.",
            "searchify.cli.sync:run",
        )
    )

    # Summarize command
    summarize_arguments(
        command(
            "summarize",
            "Process pending summaries (existing command).",
            "searchify.cli.summarize:run",
        )
    )

    # Report command
    report = command(
        "report",
        "Show the latest saved local sync report.",
        "searchify.cli.reports:run",
    )
    report.add_argument(
        "--verbose",
        action="store_true",
        help="Show additional sanitized status-persistence diagnostics",
    )
    report.add_argument(
        "--history", action="store_true", help="List saved synchronization reports"
    )

    # Directories command group
    dirs = command(
        "dirs",
        "List or manage explicitly registered directories.",
        "searchify.cli.workspace:directories",
    )
    subdirs = dirs.add_subparsers(dest="directory_command", metavar="ACTION")

    # dirs add
    add = subdirs.add_parser(
        "add",
        help="Register a directory without indexing or syncing it.",
        description="Register a directory without indexing or syncing it.",
    )
    add.add_argument("directory", help="Existing local directory to register")
    add.add_argument(
        "--profile",
        choices=("source", "android", "documents"),
        default="source",
        help="Directory-specific extraction and exclusion preset",
    )
    add.set_defaults(handler="searchify.cli.directories:add")
    pages[("dirs", "add")] = add

    # dirs remove
    remove = subdirs.add_parser(
        "remove",
        help="Safely remove local registration and metadata only.",
        description="Never deletes original files or remote records.",
    )
    remove.add_argument("directory", help="Registered directory to remove")
    remove.add_argument(
        "--yes",
        action="store_true",
        help="Explicitly confirm metadata-only unregistration without prompting",
    )
    remove.set_defaults(handler="searchify.cli.directories:remove")
    pages[("dirs", "remove")] = remove

    # dirs show
    show = subdirs.add_parser(
        "show", help="Inspect the rules of one registered directory."
    )
    show.add_argument("directory")
    show.set_defaults(handler="searchify.cli.directories:show")
    pages[("dirs", "show")] = show

    # dirs configure
    configure = subdirs.add_parser(
        "configure",
        help="Modify rules for one registered directory; no automatic processing.",
    )
    configure.add_argument("directory")
    configure.add_argument("--profile", choices=("source", "android", "documents"))
    configure.add_argument(
        "--include",
        action="append",
        help="Replace includes; repeat for multiple relative globs",
    )
    configure.add_argument(
        "--exclude",
        action="append",
        help="Replace exclusions; repeat for multiple relative globs",
    )
    configure.add_argument(
        "--extensions",
        help="Comma-separated implemented text extensions, including dots",
    )
    configure.add_argument(
        "--max-size",
        type=positive_int,
        help="Maximum extracted bytes (at most 2097152)",
    )
    configure.add_argument("--sync", choices=("on", "off"))
    configure.set_defaults(handler="searchify.cli.directories:configure")
    pages[("dirs", "configure")] = configure

    # Review command
    review = command(
        "review",
        "Review project activity, generated folders, duplicate candidates and clutter; no cleanup.",
        "searchify.cli.review:run",
    )
    review.add_argument(
        "--days", type=positive_int, default=90, help="Minimum inactivity in days"
    )
    review.add_argument(
        "--max-files",
        type=positive_int,
        default=50000,
        help="Bound the metadata inventory; incomplete scans are explicitly reported",
    )
    review.add_argument(
        "--backup-path",
        help="Inspect availability of a known backup directory; does not verify recovery",
    )
    review.add_argument(
        "--save",
        action="store_true",
        help="Explicitly save the metadata-only recommendation artifact locally",
    )

    # Organize command
    organize = command(
        "organize",
        "Propose organization for user review; never execute moves, renames, deletion or uploads.",
        "searchify.cli.organize:run",
    )
    organize.add_argument("--max-files", type=positive_int, default=50000)
    organize.add_argument(
        "--backup-path",
        help="Inspect a known backup directory without claiming its coverage is verified",
    )
    organize.add_argument(
        "--save",
        action="store_true",
        help="Explicitly save the metadata-only plan locally",
    )

    # Search command
    search = command(
        "search",
        "Search indexed metadata, or stored summaries with --semantic.",
        "searchify.cli.legacy:search",
    )
    search.add_argument("query", help="Search query (quote queries containing spaces)")
    search.add_argument(
        "--limit", type=result_limit, default=10, help="Maximum number of results"
    )
    search.add_argument(
        "--semantic",
        action="store_true",
        help="Use existing local embeddings and summary similarity search",
    )

    # Legacy compatibility aliases
    alias = command(
        "add", "Compatibility alias for dirs add.", "searchify.cli.legacy:add"
    )
    alias.add_argument("directory")
    command(
        "index",
        "Explicitly refresh the existing local metadata index.",
        "searchify.cli.legacy:index",
    )
    return parser, pages


def main(argv=None):
    """
    Main CLI entry point.

    Parses arguments, handles help command specially, then dynamically
    imports and invokes the handler function. All exceptions are caught
    and sanitized to prevent credential/secret leakage.

    Args:
        argv: Command line arguments (default: sys.argv[1:])

    Returns:
        Exit code (0=success, non-zero=error)
    """
    parser, pages = build_parser()
    args = parser.parse_args(argv)

    # Special handling for help command
    if args.handler == "help":
        page = pages.get(tuple(args.topic))
        if page is None:
            parser.error("unknown help topic; use searchify help to list commands")
        page.print_help()
        return 0

    # Planned feature placeholder
    if args.handler == "planned":
        print(
            f"{args.planned_feature} is not implemented yet (Phase {args.planned_phase}). No operations were performed.",
            file=sys.stderr,
        )
        return 2

    # Dynamic handler dispatch
    module, name = args.handler.split(":")
    try:
        result = getattr(importlib.import_module(module), name)(args)
        return result if isinstance(result, int) else 0
    except ImportError:
        print(
            "Required runtime dependency is unavailable. Use the intended Python environment and install Searchify with: python -m pip install -e .",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. No further work will be started.", file=sys.stderr)
        return 130
    except (OSError, ValueError):
        # Raw exception bodies may contain paths with credentials or provider payloads.
        print(
            "Command could not complete. Check local paths, permissions and configuration.",
            file=sys.stderr,
        )
        return 1
    except Exception:  # noqa: BLE001 - top-level safety net for CLI
        print(
            "Command failed. Inspect the local sync report when available; provider details were withheld for privacy.",
            file=sys.stderr,
        )
        return 1
