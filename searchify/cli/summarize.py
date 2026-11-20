"""
CLI Summarize Module - Process Pending Summaries

This module implements the `searchify summarize` command for processing
files with pending summaries in Supabase. It uses the shared SummaryService
to process files through the summarization and embedding pipeline.

Key features:
- Processes up to --limit files from Supabase pending queue
- Automatic recovery of interrupted/expired processing claims
- Opt-in retry for failed files (--retry-failed)
- Retry known non-retryable errors after fix (--retry-permanent)
- Verbose sanitized error output
- Graceful interruption handling (preserves completed work)

Pipeline stages per file:
1. Fetch pending from Supabase
2. Validate local file exists and matches content hash
3. Acquire processing claim (prevents concurrent processing)
4. Generate summary (or reuse existing)
5. Validate summary for privacy
6. Generate embedding (or reuse existing)
7. Persist to Supabase
8. Release claim, update journal

Exit codes:
- 0: Success (or nothing to do)
- 1: Failures or journal errors
- 2: Invalid arguments
- 130: Interrupted

Used by: CLI app.py command dispatcher
"""

import argparse
import sys


def positive_int(value):
    """
    Validate positive integer argument.

    Args:
        value: String value to parse

    Returns:
        Positive integer

    Raises:
        argparse.ArgumentTypeError: If not positive
    """
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def add_arguments(parser):
    """
    Add summarize command arguments to parser.

    Args:
        parser: ArgumentParser instance
    """
    parser.add_argument("--limit", type=positive_int, default=50)
    parser.add_argument(
        "--recover-processing",
        action="store_true",
        help="Safely recover eligible interrupted/expired claims (also performed automatically)",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Include eligible failed files unless their recorded error is nonretryable",
    )
    parser.add_argument(
        "--retry-permanent",
        action="store_true",
        help="With --retry-failed, retry known nonretryable errors after fixing their cause",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show sanitized exception details without request bodies or credentials",
    )


def run(args):
    """
    Execute summarize command.

    Args:
        args: Parsed arguments

    Returns:
        Exit code
    """
    if getattr(args, "retry_permanent", False) and not args.retry_failed:
        print("--retry-permanent requires --retry-failed.", file=sys.stderr)
        return 2
    try:
        from searchify.services.summary_service import SummaryService

        result = SummaryService().process_pending(
            args.limit,
            args.retry_failed,
            args.recover_processing,
            retry_permanent=getattr(args, "retry_permanent", False),
        )
    except KeyboardInterrupt:
        print(
            "Interrupted; successful files remain completed. Run summarize again to resume.",
            file=sys.stderr,
        )
        return 130
    except Exception as error:  # noqa: BLE001 - sanitize unexpected errors for CLI
        from searchify.reporting.sync_report import describe_error, safe_error_message

        diagnostic = describe_error(
            getattr(error, "processing_stage", "persistence"), error
        )
        print(
            f"Error: {diagnostic['exception_type']}: {safe_error_message(diagnostic['message'])}",
            file=sys.stderr,
        )
        print(diagnostic["corrective_action"], file=sys.stderr)
        return 1

    total_attempted = result["completed"] + result["failed"] + result["stale"]
    if total_attempted == 0:
        if args.retry_failed:
            print("No pending or failed files to summarize.", file=sys.stderr)
        else:
            print(
                "No pending files to summarize. Use --retry-failed to include failed files.",
                file=sys.stderr,
            )
        return 0

    details = []
    if result["completed"]:
        details.append(f"completed: {result['completed']}")
    if result["failed"]:
        details.append(f"failed: {result['failed']}")
    if result["stale"]:
        details.append(f"stale (missing/changed local content): {result['stale']}")
    if result["skipped"]:
        details.append(f"skipped (concurrent claim lost): {result['skipped']}")
    print(", ".join(details))
    if result.get("journal_errors"):
        print(
            "Local progress journal could not be saved; completed remote records were preserved. Check local database permissions.",
            file=sys.stderr,
        )
    return 1 if result["failed"] or result.get("journal_errors") else 0
