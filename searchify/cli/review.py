"""
CLI Review Module - Present Recommendations for User Review

This module implements the `searchify review` command for generating
and displaying file/project recommendations. It operates in
RECOMMENDATIONS ONLY mode - no files are modified, deleted, or cleaned up.

Analysis performed:
- Inactive projects (no activity for N days)
- Large generated/cache folders
- Loose files in registered roots
- Duplicate files (same size + byte hash)

Each recommendation includes:
- Category and reason
- Affected file paths
- Actionable suggestion
- Git status (clean/dirty/unavailable)
- Backup status

Options:
- --days: Inactivity threshold (default: 90)
- --max-files: Scan limit (default: 50000)
- --backup-path: Check backup availability
- --save: Save review artifact locally

Used by: CLI app.py command dispatcher
"""

from searchify.cli.output import line
from searchify.cli.workspace import local_paths
from searchify.config.settings import settings
from searchify.core.file_registry import FileRegistry
from searchify.services.review_service import ReviewService


def run(args):
    """
    Execute review command.

    Args:
        args: Parsed arguments (days, max_files, backup_path, save)

    Returns:
        Exit code (0)
    """
    db, _ = local_paths()
    review = ReviewService(
        FileRegistry(db),
        days=args.days,
        max_files=args.max_files,
        backup_path=args.backup_path or settings.BACKUP_PATH,
    ).review(progress_callback=lambda root: line("Inspecting registered root: " + root))
    line("PROJECT AND FILE REVIEW — no original files changed")
    for root in review["coverage"]:
        line(
            f"{root['root']}: {root['files_inspected']} metadata entries; coverage={'complete' if root['complete'] else 'partial'}"
        )
    if not review["recommendations"]:
        line(
            "No recommendations from the inspected evidence. Partial coverage is not a clean bill of health."
        )
    for item in review["recommendations"]:
        line(item["category"] + ": " + item["reason"])
        for path in item["paths"]:
            line("  " + path)
        line("  " + item["suggestion"])
        line(
            "  Git: "
            + item["git"]["status"]
            + "; uncommitted changes: "
            + str(item["git"]["dirty"])
        )
        line("  Backup: " + item["backup"]["status"])
    if args.save:
        from searchify.reporting.artifacts import save_artifact

        line(
            "Saved review: "
            + str(save_artifact(settings.REVIEWS_DIR, "review", review))
        )
    return 0
