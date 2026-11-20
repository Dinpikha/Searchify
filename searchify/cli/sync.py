"""CLI for incremental file synchronization."""

import argparse
import sys
import time


# Reject zero/negative batch sizes during argument parsing, before constructing services.
def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


# Share sync flags with the unified parser; permanent retries require the normal retry opt-in.
def add_arguments(parser):
    parser.add_argument(
        "--batch-size",
        type=positive_int,
        default=5,
        help="Number of files to process per batch (default: 5)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview local changes without modifying data or calling AI/cloud services",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry eligible failed files unless their recorded error is known to be nonretryable; interrupted work resumes automatically",
    )
    parser.add_argument(
        "--retry-permanent",
        action="store_true",
        help="With --retry-failed, explicitly retry known nonretryable errors after fixing their configuration or cause",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show additional sanitized diagnostics; never raw tracebacks or provider bodies",
    )


# Translate SyncService callback events into terminal progress and a final sanitized report.
class ProgressDisplay:
    # Track per-outcome counters locally; service reports remain the authoritative saved
    # results.
    def __init__(self, verbose=False):
        self.verbose = verbose
        self.start_time = time.time()
        self.last_update = 0
        self.current_file = None
        self.state = {}
        self.total = 0
        self.completed = 0
        self.failed = 0
        self.stale = 0
        self.skipped = 0

    # Classification seeds counters; subsequent file and batch events update visible progress.
    def __call__(self, value, event):
        now = time.time()
        if event == "starting":
            print("Starting synchronization...")
        elif event == "scanned":
            self.total = value
            print(f"Found {self.total} discovered or indexed entries to classify.")
        elif event == "metadata_upserted":
            print(f"Updated metadata for {value} file(s).")
        elif event == "classified":
            self.completed = value.get("completed", 0)
            self.failed = value.get("failed", 0)
            self.stale = value.get("stale", 0)
            self.skipped = value.get("skipped", 0)
            print(
                f"Classified {value['total']} entries; {max(0, self.total - value['total'])} awaiting processing."
            )
        elif event == "skipped":
            self.skipped += 1
            self._print_progress(value, "skipped")
        elif event == "processing":
            self.current_file = value
            print(f"  Processing: {value}...")
        elif event == "completed":
            self.completed += 1
            self._print_progress(value, "done")
        elif event == "failed":
            self.failed += 1
            self._print_progress(value, "FAILED")
        elif event == "stale":
            self.stale += 1
            self._print_progress(value, "stale (skipped)")
        elif event == "batch_completed":
            self.state = value
            elapsed = time.time() - self.start_time
            print(
                f"  Batch complete: {self.state.get('completed', 0)} completed, "
                f"{self.state.get('failed', 0)} failed, {self.state.get('stale', 0)} stale "
                f"({elapsed:.1f}s elapsed)"
            )
        elif event == "error":
            print(f"Error: {value}")

        self.last_update = now

    # Include skipped/stale entries in progress because they also finish classification.
    def _print_progress(self, filename, status):
        elapsed = time.time() - self.start_time
        processed = self.completed + self.failed + self.stale + self.skipped
        print(f"  [{processed}/{self.total}] {filename}: {status} ({elapsed:.1f}s)")

    # Render persisted results and separately warn when report publication failed.
    def final_summary(self, result):
        from searchify.cli.reports import render_report

        render_report(result["report"], result.get("report_path"), verbose=self.verbose)
        if result.get("report_save_error"):
            print(
                "Warning: the local JSON report could not be saved; check reports-directory permissions."
            )


# Run searchify sync with lazy dependencies; return 2 for invalid flags, 130 for
# interruption, 1 for failure.
def run(args):
    try:
        from searchify.services.sync_service import SyncService
    except ImportError:
        print(
            "Sync runtime dependencies are unavailable. Use the intended environment and run: python -m pip install -e .",
            file=sys.stderr,
        )
        return 1
    try:
        from searchify.ui.mascot import show_banner

        show_banner()
    except Exception:  # noqa: BLE001,S110 - banner is cosmetic
        pass

    display = ProgressDisplay(verbose=args.verbose)

    if args.retry_permanent and not args.retry_failed:
        print("--retry-permanent requires --retry-failed.", file=sys.stderr)
        return 2
    service = SyncService(batch_size=args.batch_size)
    try:
        result = service.sync(
            progress_callback=display,
            retry_failed=args.retry_failed,
            dry_run=args.dry_run,
            retry_permanent=args.retry_permanent,
        )
    except KeyboardInterrupt:
        if service.last_result:
            display.final_summary(service.last_result)
        print(
            "Interrupted; successful files remain completed. Run sync again to resume safely; active workers retain their claims.",
            file=sys.stderr,
        )
        return 130
    except Exception as error:  # noqa: BLE001 - sanitize unexpected errors for CLI
        if service.last_result:
            display.final_summary(service.last_result)
        from searchify.reporting.sync_report import describe_error

        diagnostic = describe_error("persistence", error)
        print(
            f"Sync stopped: {diagnostic['exception_type']}. {diagnostic['corrective_action']}",
            file=sys.stderr,
        )
        return 1
    display.final_summary(result)
    return (
        1
        if result.get("failed")
        or result.get("report_save_error")
        or result["report"]["run_errors"]
        else 0
    )
