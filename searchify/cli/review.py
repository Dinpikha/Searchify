"""Present recommendations for user review, never execute cleanup."""

from searchify.cli.output import line
from searchify.cli.workspace import local_paths
from searchify.config.settings import settings
from searchify.core.file_registry import FileRegistry
from searchify.services.review_service import ReviewService


def run(args):
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
