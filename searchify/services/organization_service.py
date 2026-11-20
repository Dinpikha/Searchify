"""
Organization Service - Reversible File Organization Proposals

This module generates proposals for organizing files within registered
directories. It operates in PROPOSAL ONLY mode - no files are moved,
renamed, or deleted. All operations require explicit approval and
verification before execution.

Key features:
- Proposes moves for root-level files into type-based folders (Documents, Images, Archives)
- Skips protected files, important files, git-tracked projects, and files already in subdirectories
- Generates blockers for safety checks (destination exists, symlinks, git dirty, backup unverified)
- Includes verification requirements and rollback procedures for each proposal
- Preserves original file metadata for identity verification

DESTINATIONS mapping defines target folders by file extension.

Used by: CLI organize command
"""

import uuid
from datetime import datetime, timezone
from pathlib import Path

from searchify.core.scanner import PROTECTED, matches
from searchify.services.review_service import (
    IMPORTANT,
    backup_metadata,
    git_metadata,
    inventory,
    safe_path,
)

# Extension to destination folder mapping
DESTINATIONS = {
    ".pdf": "Documents",
    ".txt": "Documents",
    ".md": "Documents",
    ".csv": "Documents",
    ".docx": "Documents",
    ".png": "Images",
    ".jpg": "Images",
    ".jpeg": "Images",
    ".webp": "Images",
    ".gif": "Images",
    ".zip": "Archives",
    ".tar": "Archives",
    ".gz": "Archives",
}


class OrganizationService:
    """
    Generates file organization proposals for registered directories.

    Analyzes root-level files in each registered directory and proposes
    moves to appropriate category folders based on file extension.
    All proposals are non-executable and require manual approval.

    Attributes:
        registry: FileRegistry instance
        max_files: Maximum files to scan across all roots
        backup_path: Optional backup path for recovery verification
    """

    def __init__(self, registry, max_files=50000, backup_path=None):
        """
        Initialize organization service.

        Args:
            registry: FileRegistry instance
            max_files: Maximum files to process (default: 50000)
            backup_path: Path to backup for verification
        """
        self.registry, self.max_files, self.backup_path = (
            registry,
            max_files,
            backup_path,
        )

    def propose(self):
        """
        Generate organization proposals for all registered roots.

        For each registered directory:
        1. Inventory root-level files (non-recursive)
        2. Check git status and backup status
        3. For each eligible file, propose move to destination folder
        4. Validate destination safety (no conflicts, not symlink, etc.)
        5. Generate blockers if any safety check fails

        Returns:
            Proposal dict with schema_version, timestamp, roots, operations
        """
        operations = []
        roots = []
        remaining = self.max_files
        targets = set()
        for directory in self.registry.directories():
            root = Path(directory["path"])
            files, projects, _, complete = inventory(
                root, max(0, remaining), recursive=False
            )
            remaining -= len(files)
            git = git_metadata(root)
            backup = backup_metadata(self.backup_path)
            roots.append(
                {
                    "path": safe_path(root),
                    "git": git,
                    "backup": backup,
                    "scan_complete": complete,
                    "scope": "registered-root files only",
                }
            )
            if root in projects:
                continue  # Project source layouts require project-specific knowledge.
            for row in files:
                source = row["path"]
                folder = DESTINATIONS.get(source.suffix.lower())
                if (
                    source.parent != root
                    or folder is None
                    or row["protected"]
                    or matches(source.name, IMPORTANT)
                    or matches(source.name, PROTECTED)
                ):
                    continue
                destination = root / folder / source.name
                blocked = []
                if destination.exists() or destination in targets:
                    blocked.append(
                        "Destination already exists or is proposed twice; never overwrite."
                    )
                if (root / folder).is_symlink():
                    blocked.append("Destination folder is a symlink.")
                if git["dirty"] is True:
                    blocked.append("Git has uncommitted or untracked changes.")
                if git["dirty"] is None:
                    blocked.append("Git cleanliness has not been verified.")
                if not backup["verified"]:
                    blocked.append("Backup recoverability has not been verified.")
                targets.add(destination)
                operations.append(
                    {
                        "id": str(uuid.uuid4()),
                        "action": "proposed_move",
                        "source": safe_path(source),
                        "destination": safe_path(destination),
                        "reason": f"A root-level {source.suffix.lower()} file could be grouped in {folder}; its current placement may be intentional.",
                        "approval_required": True,
                        "executable": False,
                        "blockers": blocked,
                        "verification": {
                            "expected_stat_signature": row["signature"],
                            "size_bytes": row["size"],
                            "requirements": [
                                "Recheck source identity and byte hash before executing any approved move.",
                                "Verify destination is inside the same registered root and does not exist.",
                                "Verify a recoverable backup and review Git changes.",
                            ],
                        },
                        "rollback": "For an approved move, verify the moved file and return it to the original path only if that path is free. Never overwrite either location.",
                    }
                )
        return {
            "schema_version": 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": "proposal_only",
            "roots": roots,
            "operations": operations,
            "execution_supported": False,
            "no_original_files_changed": True,
        }
