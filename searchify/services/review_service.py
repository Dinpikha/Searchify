"""Conservative metadata-first recommendations, separate from synchronization."""

import hashlib
import os
import subprocess
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from searchify.core.scanner import (
    PROTECTED,
    PROTECTED_DIRS,
    matches,
    root_is_unredirected,
    signature,
)
from searchify.reporting.sync_report import safe_filename

GENERATED = {
    ".gradle",
    ".kotlin",
    ".cxx",
    ".externalNativeBuild",
    "build",
    "dist",
    "node_modules",
    "__pycache__",
    ".cache",
    "target",
}
MARKERS = {
    "pyproject.toml",
    "package.json",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
}
IMPORTANT = (
    "README*",
    "LICENSE*",
    "COPYING*",
    "gradlew*",
    "*.gradle",
    "*.kts",
    "*.properties",
    "package*.json",
    "pyproject.toml",
    "requirements*.txt",
)


def safe_path(path):
    return "/".join(
        safe_filename(part) if part else "" for part in str(path).split("/")
    )


def git_metadata(root):
    root = Path(root)
    if not root_is_unredirected(root):
        return {
            "status": "Git root boundary unverified",
            "dirty": None,
            "last_commit": None,
        }
    metadata = root / ".git"
    if not metadata.exists():
        return {"status": "not a Git project", "dirty": None, "last_commit": None}
    if not metadata.is_dir() or metadata.is_symlink():
        return {
            "status": "Git worktree metadata not inspected",
            "dirty": None,
            "last_commit": None,
        }
    prefix = [
        "git",
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "log.showSignature=false",
        "-C",
        str(root),
    ]
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
    try:
        status = subprocess.run(
            prefix + ["status", "--porcelain=v1", "-z", "--untracked-files=normal"],
            check=False,
            capture_output=True,
            timeout=3,
            env=env,
        )
        commit = subprocess.run(
            prefix + ["log", "-1", "--format=%ct"],
            check=False,
            capture_output=True,
            timeout=3,
            env=env,
        )
        stamp = commit.stdout.strip()
        return {
            "status": "checked" if status.returncode == 0 else "Git status unavailable",
            "dirty": bool(status.stdout) if status.returncode == 0 else None,
            "last_commit": int(stamp)
            if commit.returncode == 0 and stamp.isdigit()
            else None,
        }
    except (OSError, subprocess.TimeoutExpired):
        return {
            "status": "Git unavailable or timed out",
            "dirty": None,
            "last_commit": None,
        }


def backup_metadata(path=None):
    if path is None:
        return {"status": "unknown; no backup location supplied", "verified": False}
    try:
        available = Path(path).expanduser().is_dir()
    except OSError:
        available = False
    return {
        "status": "backup directory available; version coverage and recoverability unverified"
        if available
        else "backup directory unavailable",
        "verified": False,
    }


def inventory(root, max_files=50000, skip_roots=(), recursive=True):
    """Inspect names/stat metadata, not source contents. Bound work and skip symlinks."""
    root = Path(root)
    files = []
    projects = set()
    generated = defaultdict(int)
    complete = True
    visited = 0

    def onerror(error):
        nonlocal complete
        complete = False

    if not root_is_unredirected(root) or not root.is_dir():
        return files, projects, generated, False
    for current, dirs, names in os.walk(root, followlinks=False, onerror=onerror):
        visited += 1
        if visited > max_files:
            return files, projects, generated, False
        folder = Path(current)
        if ".git" in dirs or ".git" in names:
            projects.add(folder)
        dirs[:] = sorted(
            name
            for name in dirs
            if name not in PROTECTED_DIRS
            and not (folder / name).is_symlink()
            and (folder / name) not in skip_roots
        )
        if not recursive:
            dirs[:] = []
        for name in sorted(names):
            if len(files) >= max_files:
                return files, projects, generated, False
            path = folder / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
                rel = path.relative_to(root)
                parts = rel.parts
                gen = next(
                    (
                        root.joinpath(*parts[: i + 1])
                        for i, part in enumerate(parts[:-1])
                        if part in GENERATED
                    ),
                    None,
                )
                if name in MARKERS and gen is None:
                    projects.add(folder)
                if gen is not None:
                    generated[gen] += stat.st_size
                files.append(
                    {
                        "path": path,
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                        "signature": signature(stat),
                        "generated": gen is not None,
                        "protected": matches(name, PROTECTED) or name.startswith("."),
                    }
                )
            except OSError:
                complete = False
    return files, projects, generated, complete


def byte_hash(path, expected_signature):
    """Only shortlisted duplicate candidates are read; no extracted text is stored."""
    digest = hashlib.sha256()
    try:
        if path.is_symlink() or signature(path.stat()) != expected_signature:
            return None
        remaining = int(expected_signature.split(":")[2]) + 1
        read = 0
        with path.open("rb") as source:
            while remaining > 0:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                read += len(chunk)
                remaining -= len(chunk)
        if remaining == 0:
            return None
        if signature(path.stat()) != expected_signature:
            return None
        return digest.hexdigest()
    except OSError:
        return None


class ReviewService:
    def __init__(
        self,
        registry,
        days=90,
        large_generated_bytes=100 * 1024 * 1024,
        max_files=50000,
        backup_path=None,
    ):
        self.registry, self.days, self.large_bytes = (
            registry,
            days,
            large_generated_bytes,
        )
        self.max_files, self.backup_path = max_files, backup_path

    def review(self, progress_callback=None):
        recommendations = []
        project_details = []
        evidence = []
        remaining = self.max_files
        directories = self.registry.directories()
        roots = {Path(d["path"]) for d in directories}
        for directory in directories:
            root = Path(directory["path"])
            if progress_callback:
                try:
                    progress_callback(safe_path(root))
                except Exception:  # noqa: BLE001,S110 - progress callback is optional
                    pass
            if remaining <= 0:
                evidence.append(
                    {"root": safe_path(root), "complete": False, "files_inspected": 0}
                )
                continue
            files, projects, generated, complete = inventory(
                root, remaining, skip_roots=roots - {root}
            )
            remaining -= len(files)
            evidence.append(
                {
                    "root": safe_path(root),
                    "complete": complete,
                    "files_inspected": len(files),
                }
            )
            for project in sorted(projects):
                git = git_metadata(project)
                backup = backup_metadata(self.backup_path)
                recent = max(
                    [
                        row["mtime"]
                        for row in files
                        if row["path"].is_relative_to(project)
                        and not row["generated"]
                        and not row["protected"]
                    ]
                    + [git["last_commit"] or 0]
                )
                details = {
                    "path": safe_path(project),
                    "git": git,
                    "backup": backup,
                    "last_activity": datetime.fromtimestamp(
                        recent, timezone.utc
                    ).isoformat()
                    if recent
                    else None,
                }
                project_details.append(details)
                git_activity_known = git["status"] == "not a Git project" or (
                    git["status"] == "checked" and git["last_commit"] is not None
                )
                if (
                    complete
                    and git_activity_known
                    and recent
                    and time.time() - recent >= self.days * 86400
                ):
                    recommendations.append(
                        {
                            "category": "inactive_project",
                            "paths": [safe_path(project)],
                            "reason": f"No observed source-file or commit activity for at least {self.days} days. Inactivity does not imply the project is unnecessary.",
                            "suggestion": "Review its purpose and future plans; consider an archive only after explicit approval, Git review and verified backup.",
                            "git": git,
                            "backup": backup,
                        }
                    )
            for folder, size in sorted(generated.items()):
                if size >= self.large_bytes:
                    project = next(
                        (
                            p
                            for p in sorted(
                                projects, key=lambda p: len(p.parts), reverse=True
                            )
                            if folder.is_relative_to(p)
                        ),
                        root,
                    )
                    recommendations.append(
                        {
                            "category": "large_generated_folder",
                            "paths": [safe_path(folder)],
                            "protected_files_observed": sum(
                                row["protected"] and row["path"].is_relative_to(folder)
                                for row in files
                            ),
                            "size_bytes": size,
                            "reason": "Recognized generated/cache folder with "
                            + ("at least " if not complete else "")
                            + f"{size} bytes observed.",
                            "suggestion": "Review whether outputs are reproducible; do not delete until Git changes and backup/rebuild options are verified.",
                            "git": git_metadata(project),
                            "backup": backup_metadata(self.backup_path),
                        }
                    )
            loose = [
                row
                for row in files
                if row["path"].parent == root
                and not row["protected"]
                and not matches(row["path"].name, IMPORTANT)
            ]
            if len(loose) >= 5 and root not in projects:
                recommendations.append(
                    {
                        "category": "loose_files",
                        "paths": [safe_path(row["path"]) for row in loose],
                        "reason": "At least five non-configuration files are directly in the registered root; their placement may be intentional.",
                        "suggestion": "Review an organization proposal; no automatic moves are recommended.",
                        "git": git_metadata(root),
                        "backup": backup_metadata(self.backup_path),
                    }
                )
            # Rank duplicate candidates by identical size, then verify bytes locally within a budget.
            by_size = defaultdict(list)
            for row in files:
                if row["size"] > 0 and not row["protected"] and not row["generated"]:
                    by_size[row["size"]].append(row)
            budget = 32 * 1024 * 1024
            checked = 0
            groups = defaultdict(list)
            for size, rows in sorted(by_size.items()):
                if len(rows) < 2:
                    continue
                for row in rows:
                    if size > budget or checked >= 200:
                        continue
                    digest = byte_hash(row["path"], row["signature"])
                    budget -= size
                    checked += 1
                    if digest:
                        groups[(size, digest)].append(row["path"])
            for (size, _), paths in groups.items():
                if len(paths) < 2:
                    continue
                recommendations.append(
                    {
                        "category": "duplicate_files",
                        "paths": [safe_path(path) for path in paths],
                        "size_bytes_each": size,
                        "reason": "Shortlisted same-size files have identical SHA-256 byte hashes at review time.",
                        "suggestion": "Review ownership and usage of each copy. Do not delete or consolidate without explicit approval and verified backup.",
                        "git": git_metadata(root),
                        "backup": backup_metadata(self.backup_path),
                    }
                )
        for recommendation in recommendations:
            recommendation["id"] = str(uuid.uuid4())
            recommendation["approval_required"] = True
        return {
            "schema_version": 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": "recommendations_only",
            "coverage": evidence,
            "projects": project_details,
            "recommendations": recommendations,
            "limits": {
                "max_files": self.max_files,
                "duplicate_bytes_per_root": 32 * 1024 * 1024,
                "duplicate_candidates_per_root": 200,
            },
            "no_original_files_changed": True,
        }
