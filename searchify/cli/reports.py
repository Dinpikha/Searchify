"""
CLI Reports Module - Display Local Sync History

This module implements the `searchify report` command for viewing
saved synchronization reports. It loads JSON report files from the
local reports directory and renders them with sanitized output.

Features:
- Lists report history with summary counts (--history)
- Renders latest report with metrics, outcomes, and error details
- Sanitizes filenames, error messages, and identifiers
- Groups failures by category and stage for actionable insights
- Verbose mode shows status persistence errors

Report structure:
- Metrics: scan statistics (discovered, new, modified, etc.)
- Counts: outcome tallies (completed, failed, stale, skipped)
- Outcomes: per-file results with reasons and errors
- Problem groups: failures grouped by category/stage
- Run errors: pipeline-level errors

Used by: CLI app.py command dispatcher
"""

from searchify.cli import workspace
from searchify.cli.output import line
from searchify.reporting.history import load_reports
from searchify.reporting.sync_report import (
    ACTIONS,
    _request_id,
    safe_error_message,
    safe_filename,
    safe_identifier,
)


def render_report(report, path=None, verbose=False):
    """
    Render a synchronization report to console.

    Args:
        report: Report dict from load_reports()
        path: Optional report file path for display
        verbose: Include status persistence errors
    """
    line(
        "SYNCHRONIZATION REPORT"
        + (" — DRY RUN (no data written)" if report.get("dry_run") else "")
    )
    if report.get("dry_run"):
        line("Preview uses local state only; cloud state was not queried.")
    metrics, counts = report.get("metrics", {}), report["counts"]
    for key in (
        "total_discovered",
        "new",
        "modified",
        "unchanged",
        "deleted",
        "unverified",
        "known_excluded",
        "excluded_directories",
    ):
        if type(metrics.get(key)) is int:
            line(f"  {key.replace('_', ' ').capitalize()}: {metrics[key]}")
    for key in ("total", "completed", "failed", "stale", "skipped"):
        line(f"  {key.capitalize()}: {counts[key]}")
    if type(report.get("remaining")) is int:
        line(f"  Remaining: {report['remaining']}")
    duration = report.get("duration_seconds")
    if isinstance(duration, (int, float)):
        line(f"  Duration: {duration:.1f}s")
    if report.get("dry_run"):
        for outcome in report.get("outcomes", []):
            if outcome.get("reason") == "dry_run_would_process":
                line(
                    "  Would process: "
                    + safe_filename(outcome.get("filename"))
                    + " ["
                    + (safe_identifier(outcome.get("file_id")) or "local")
                    + "]"
                )
    groups = report.get("problem_groups", report.get("failure_groups", []))
    for group in groups if isinstance(groups, list) else []:
        category, stage = group.get("category"), group.get("stage")
        if category not in ACTIONS or stage not in {
            "extraction",
            "summarization",
            "embedding",
            "persistence",
        }:
            continue
        names = group.get("filenames", [])
        if not isinstance(names, list):
            continue
        line(f"{category} / {stage}: {len(names)}")
        for name in names:
            line(f"  - {safe_filename(name)}")
        line("  Suggested action: " + ACTIONS[category])
    for outcome in report.get("outcomes", []):
        error = outcome.get("error")
        if outcome.get("outcome") != "failed" or not isinstance(error, dict):
            continue
        name = safe_filename(outcome.get("filename"))
        # Messages are persisted only by the conservative diagnostic builder.
        # Rebuild safe messages from stored typed fields instead of trusting JSON prose.
        kind = safe_identifier(error.get("exception_type")) or "Exception"
        stage = (
            error.get("stage")
            if error.get("stage")
            in {"extraction", "summarization", "embedding", "persistence"}
            else "unknown"
        )
        line(
            f"  {name}: {kind} at {stage}; retryable={error.get('retryable') if type(error.get('retryable')) is bool else 'unknown'}"
        )
        line("    " + safe_error_message(error.get("message")))
        request_id = _request_id(error.get("provider_request_id"))
        if request_id:
            line("    Request ID: " + request_id)
        status = error.get("http_status")
        if type(status) is int and 100 <= status <= 599:
            line(f"    HTTP {status}")
    if verbose:
        for outcome in report.get("outcomes", []):
            secondary = outcome.get("status_error")
            if isinstance(secondary, dict):
                line(
                    "Status persistence error for "
                    + safe_filename(outcome.get("filename"))
                    + ": "
                    + safe_error_message(secondary.get("message"))
                )
    for error in report.get("run_errors", []):
        if isinstance(error, dict):
            category = error.get("category", "unknown")
            line("Run error: " + (category if category in ACTIONS else "unknown"))
            line(ACTIONS.get(category, ACTIONS["unknown"]))
            if verbose:
                line(safe_error_message(error.get("message")))
    if path:
        line(f"Local report: {path}")


def run(args):
    """
    Execute report command.

    Args:
        args: Parsed arguments (history, verbose flags)

    Returns:
        Exit code (0=OK, 1=no reports or invalid reports)
    """
    _, directory = workspace.local_paths()
    reports, invalid = load_reports(directory)
    if invalid:
        line(f"Warning: {invalid} unreadable or invalid report(s) were ignored.")
    if not reports:
        line(
            "No readable synchronization reports available."
            if invalid
            else "No saved synchronization reports yet."
        )
        return 1 if invalid else 0
    if args.history:
        for path, report in reports:
            c = report["counts"]
            line(
                f"{safe_filename(path.name)}  total: {c['total']}, completed: {c['completed']}, failed: {c['failed']}, stale: {c['stale']}, skipped: {c['skipped']}"
            )
    else:
        path, report = reports[0]
        render_report(report, path, verbose=getattr(args, "verbose", False))
    return 0
