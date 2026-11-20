"""
Directory Scanner Module

This module implements the core file system scanning logic for Searchify.
It walks registered directory roots, applies per-directory inclusion/exclusion
rules, computes file signatures for change detection, and optionally hashes
file content for content-based deduplication.

Key responsibilities:
- Traverse registered directories with configurable rules
- Apply include/exclude glob patterns (supports **/ recursive patterns)
- Detect file changes via inode+size+mtime+ctime signature
- Compute SHA-256 content hashes for content-based deduplication
- Identify protected/sensitive files that should never be indexed
- Handle nested registered roots (each root manages its own subtree)
- Generate synthetic entries for directory-level status (unavailable, protected, etc.)

The scanner produces a list of file entries with metadata used by the sync
pipeline to determine which files need processing.

Used by: sync_service.py (main sync pipeline), indexer.py (metadata refresh)
"""

import fnmatch
import hashlib
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from searchify.core.extractor import MAX_CONTENT_SIZE, TEXT_EXTENSIONS, extract_content

# Directory names that are considered sensitive/protected and should never be scanned.
# These are common directories containing credentials, keys, or local configuration.
PROTECTED_DIRS = {".git", ".aws", ".ssh", ".gnupg", ".kube", ".secrets", ".searchify"}

# File name patterns for sensitive files that should never be indexed or processed.
# These patterns use glob-style matching and cover common credential/key files.
PROTECTED = (
    ".env",  # Environment files
    ".env.*",  # Environment file variants
    "*.jks",  # Java keystores
    "*.keystore",  # Keystore files
    "*.pem",  # PEM certificates/keys
    "*.key",  # Key files
    "*credentials*",  # Credential files
    "*service-account*.json",  # Service account keys
    "google-services.json",  # Firebase/Google services config
    "local.properties",  # Android local properties (often contains SDK paths)
)


@dataclass
class DirectoryRules:
    """
    Per-directory scanning and processing rules.

    Each registered directory can have its own rules controlling:
    - Which files to include/exclude (glob patterns)
    - Which file extensions to process
    - Maximum file size for processing
    - Whether sync is enabled for this directory
    - Profile name (for preset configurations)

    Rules are stored in the local SQLite database and can be configured
    via the `searchify dirs configure` CLI command.
    """

    # Include patterns (glob-style, relative to directory root)
    # Default: ["*"] (all files)
    include: list = field(default_factory=lambda: ["*"])

    # Exclude patterns (glob-style, relative to directory root)
    # Default excludes common build/dependency/cache directories
    exclude: list = field(
        default_factory=lambda: [
            ".git/**",
            "**/node_modules/**",
            "**/__pycache__/**",
            "**/.venv/**",
            "**/venv/**",
            "**/dist/**",
            "**/.next/**",
        ]
    )

    # File extensions to process (subset of TEXT_EXTENSIONS)
    extensions: list = field(default_factory=lambda: sorted(TEXT_EXTENSIONS))

    # Maximum file size in bytes for this directory (default: 2MB)
    max_size: int = MAX_CONTENT_SIZE

    # Whether synchronization is enabled for this directory
    sync_enabled: bool = True

    # Profile name: "source", "android", "documents", or custom
    profile: str = "source"

    @classmethod
    def from_dict(cls, value):
        """
        Create DirectoryRules from a dictionary with validation.

        Args:
            value: Dictionary with rule fields

        Returns:
            DirectoryRules instance

        Raises:
            ValueError: If dictionary contains invalid fields or values
        """
        if not isinstance(value, dict) or set(value) - {
            "include",
            "exclude",
            "extensions",
            "max_size",
            "sync_enabled",
            "profile",
        }:
            raise ValueError("Invalid directory rule fields.")
        rules = cls(**value)
        if (
            type(rules.max_size) is not int
            or not 1 <= rules.max_size <= MAX_CONTENT_SIZE
            or type(rules.sync_enabled) is not bool
        ):
            raise ValueError("Invalid directory size or synchronization setting.")
        for patterns in (rules.include, rules.exclude, rules.extensions):
            if not isinstance(patterns, list) or any(
                not isinstance(p, str) or not p or len(p) > 256 for p in patterns
            ):
                raise ValueError("Invalid directory patterns.")
        if any(extension not in TEXT_EXTENSIONS for extension in rules.extensions):
            raise ValueError("Extraction is not implemented for a requested extension.")
        return rules

    def to_dict(self):
        """Convert rules to dictionary for storage."""
        return asdict(self)

    @classmethod
    def preset(cls, profile):
        """
        Get preset rules for a named profile.

        Args:
            profile: Profile name ("source", "android", "documents")

        Returns:
            DirectoryRules instance with profile-specific defaults

        Raises:
            ValueError: If profile name is unknown
        """
        if profile == "source":
            return cls()
        if profile == "android":
            return cls(
                profile="android",
                exclude=[
                    ".git/**",
                    "**/build/**",
                    "**/.gradle/**",
                    "**/.kotlin/**",
                    "**/.idea/**",
                    "**/.cxx/**",
                    "**/.externalNativeBuild/**",
                    "**/generated/**",
                    "**/debug/output-metadata.json",
                    "**/captures/**",
                    "*.log",
                ],
                extensions=[
                    ".kt",
                    ".kts",
                    ".java",
                    ".xml",
                    ".json",
                    ".gradle",
                    ".properties",
                    ".toml",
                    ".pro",
                    ".md",
                    ".txt",
                    ".yaml",
                    ".yml",
                    ".sh",
                    ".c",
                    ".cpp",
                    ".h",
                ],
            )
        if profile == "documents":
            return cls(
                profile="documents",
                extensions=[".txt", ".md", ".csv", ".rst"],
                exclude=[".git/**", "~*", "*.tmp", "*.bak"],
            )
        raise ValueError("Unknown directory profile.")


def matches(path, patterns):
    """
    Check if a path matches any of the given glob patterns.

    Supports **/ recursive patterns by expanding them to also match
    without the leading **/. Uses both fnmatch and pathlib.Path.match
    for comprehensive pattern matching.

    Args:
        path: Path string (normalized to forward slashes)
        patterns: List of glob patterns

    Returns:
        True if path matches any pattern, False otherwise
    """
    path = path.replace(os.sep, "/")
    for pattern in patterns:
        variants = [pattern]
        # Expand **/ prefix to also match without it
        while variants[-1].startswith("**/"):
            variants.append(variants[-1][3:])
        for variant in variants:
            if (
                fnmatch.fnmatchcase(path, variant)
                or Path(path).match(variant)
                or (variant.endswith("/**") and matches(path, [variant[:-3]]))
            ):
                return True
    return False


def signature(stat):
    """
    Create a file signature for change detection.

    The signature combines device, inode, size, mtime, and ctime
    to uniquely identify a file's state. Any change to content,
    metadata, or file identity will change the signature.

    Args:
        stat: os.stat_result object

    Returns:
        Colon-separated signature string
    """
    return ":".join(
        str(value)
        for value in (
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )
    )


def root_is_unredirected(root):
    """
    Verify that a registered root path has not been redirected via symlinks.

    Registered roots must be canonical paths. This check ensures the root
    is not a symlink and its resolved path matches its absolute path.
    This prevents a malicious or accidental symlink from causing the scanner
    to traverse outside the intended directory.

    Args:
        root: Path to the registered root directory

    Returns:
        True if root is a valid canonical path, False otherwise
    """
    root = Path(root).absolute()
    try:
        return not root.is_symlink() and root.resolve() == root
    except (OSError, RuntimeError):
        return False


class Scanner:
    """
    File system scanner for registered directories.

    The Scanner walks each registered directory root, applies per-directory
    rules, and produces a list of file entries with metadata including
    change status, content hashes, and eligibility for processing.

    Attributes:
        registry: FileRegistry instance for accessing directory rules and metadata
        exclude_paths: Set of absolute paths to exclude from scanning (internal dirs)
    """

    def __init__(self, registry, exclude_paths=()):
        """
        Initialize scanner with registry and exclusion paths.

        Args:
            registry: FileRegistry instance
            exclude_paths: Additional paths to exclude from scanning
        """
        self.registry = registry
        from searchify.config.settings import settings

        # Build set of absolute paths to exclude from scanning
        # Includes user-provided paths plus internal Searchify directories
        self.exclude_paths = {
            Path(path).absolute()
            for path in (
                *exclude_paths,
                settings.REPORTS_DIR,
                settings.REVIEWS_DIR,
                settings.PLANS_DIR,
            )
        }
        # Also exclude the registry database and its WAL/SHM files, and lock file
        self.exclude_paths.update(
            {
                registry.path,
                Path(str(registry.path) + "-wal"),
                Path(str(registry.path) + "-shm"),
                Path(str(registry.path) + ".sync.lock"),
            }
        )

    def scan(self, hash_contents=True):
        """
        Scan all registered directories and return file entries.

        This is the main entry point. It processes each registered directory
        in order of deepest path first (to handle nested roots correctly),
        walks the directory tree, and builds file entries.

        Args:
            hash_contents: If True, compute SHA-256 content hashes for eligible files.
                          If False, only compute signatures (faster, for dry-run).

        Returns:
            Tuple of (entries, metrics, excluded):
            - entries: List of file entry dictionaries
            - metrics: Dictionary with scan statistics
            - excluded: List of excluded directory paths
        """
        # Load previous metadata and sync states for change detection
        old, states = self.registry.metadata(), self.registry.states()
        entries, excluded = [], []

        # Process directories deepest-first so nested roots are handled before parents
        directories = sorted(
            self.registry.directories(),
            key=lambda d: len(Path(d["path"]).parts),
            reverse=True,
        )
        registered_roots = {Path(d["path"]) for d in directories}

        for directory in directories:
            root = Path(directory["path"])

            # Verify root boundary hasn't been redirected via symlink
            if not root_is_unredirected(root):
                # Treat as incomplete scan rather than deleting all indexed files
                entries.append(
                    {
                        "root_id": directory["id"],
                        "root_path": str(root),
                        "path": str(root),
                        "relative_path": ".",
                        "filename": root.name,
                        "synthetic": True,
                        "change": "unavailable",
                        "eligible": False,
                        "reason": "incomplete_scan",
                        "root_error": "registered_root_boundary_unverified",
                    }
                )
                continue

            rules = self.registry.rules(directory["id"])
            seen, complete = set(), True

            # Skip protected directories entirely
            if any(part in PROTECTED_DIRS for part in root.parts):
                entries.append(
                    {
                        "root_id": directory["id"],
                        "root_path": str(root),
                        "path": str(root),
                        "relative_path": ".",
                        "filename": root.name,
                        "synthetic": True,
                        "change": "unchanged",
                        "eligible": False,
                        "reason": "protected_directory",
                    }
                )
                continue

            # Skip internal Searchify directories
            if root.absolute() in self.exclude_paths:
                entries.append(
                    {
                        "root_id": directory["id"],
                        "root_path": str(root),
                        "path": str(root),
                        "relative_path": ".",
                        "filename": root.name,
                        "synthetic": True,
                        "change": "unchanged",
                        "eligible": False,
                        "reason": "internal_metadata",
                    }
                )
                continue

            # Handle missing or disabled directories
            if not root.is_dir() or not rules.sync_enabled:
                entries.append(
                    {
                        "root_id": directory["id"],
                        "root_path": str(root),
                        "path": str(root),
                        "relative_path": ".",
                        "filename": root.name,
                        "synthetic": True,
                        "change": "unavailable",
                        "eligible": False,
                        "reason": "missing_directory"
                        if rules.sync_enabled
                        else "sync_disabled",
                    }
                )
                continue

            # Error handler for os.walk - marks scan as incomplete
            # Mark this root incomplete so an unreadable subtree does not produce false
            # deletion records.
            def walk_error(error):
                nonlocal complete
                complete = False

            # Walk the directory tree
            for current, dirs, names in os.walk(
                root, followlinks=False, onerror=walk_error
            ):
                # Process subdirectories - filter out excluded/protected/nested roots
                for name in sorted(dirs.copy()):
                    path = Path(current) / name
                    relative = path.relative_to(root).as_posix()
                    if path.absolute() in self.exclude_paths:
                        dirs.remove(name)
                        excluded.append(str(path))
                        continue
                    # Nested registered roots manage their own subtree
                    if path in registered_roots and path != root:
                        dirs.remove(name)
                        continue
                    # Skip symlinks, protected dirs, and excluded patterns
                    if (
                        path.is_symlink()
                        or name in PROTECTED_DIRS
                        or matches(relative, rules.exclude)
                    ):
                        dirs.remove(name)
                        excluded.append(str(path))
                dirs.sort()

                # Process files
                for name in sorted(names):
                    path = Path(current) / name
                    relative = path.relative_to(root).as_posix()
                    absolute = str(path.absolute())
                    seen.add(absolute)
                    previous = old.get(absolute)
                    state = states.get(absolute, {})

                    # Build base entry with file metadata
                    entry = {
                        "path": absolute,
                        "root_id": directory["id"],
                        "root_path": str(root),
                        "relative_path": relative,
                        "filename": name,
                        "extension": path.suffix.lower(),
                        "eligible": False,
                        "reason": None,
                        "size_bytes": 0,
                        "modified_ns": 0,
                        "signature": None,
                        "content_hash": None,
                        "max_size": rules.max_size,
                        "allowed_extensions": rules.extensions,
                    }

                    try:
                        # Get file stats without following symlinks
                        stat = path.lstat()
                        entry.update(
                            size_bytes=stat.st_size,
                            modified_ns=stat.st_mtime_ns,
                            signature=signature(stat),
                        )

                        # Determine change status by comparing signature with previous state
                        entry["change"] = (
                            "new"
                            if previous is None
                            else "unchanged"
                            if entry["signature"] == state.get("signature")
                            else "modified"
                            if state.get("signature")
                            or previous.get("size") != stat.st_size
                            or previous.get("mtime_ns") != stat.st_mtime_ns
                            else "unverified"
                        )

                        # Apply exclusion rules in priority order
                        if path.absolute() in self.exclude_paths:
                            entry["reason"] = "internal_metadata"
                        elif path.is_symlink():
                            entry["reason"] = "symlink"
                        elif not path.is_file():
                            entry["reason"] = "not_regular_file"
                        elif matches(name, PROTECTED):
                            entry["reason"] = "protected_file"
                        elif matches(relative, rules.exclude) or not matches(
                            relative, rules.include
                        ):
                            entry["reason"] = "excluded_pattern"
                        elif entry["extension"] not in rules.extensions:
                            entry["reason"] = "unsupported_file_type"
                        elif stat.st_size > rules.max_size:
                            entry["reason"] = "size_restriction"
                        elif (
                            entry["change"] == "unchanged"
                            and state.get("reason") == "empty_or_binary"
                        ):
                            entry["reason"] = "empty_or_binary"
                        elif hash_contents:
                            # Compute or reuse content hash for content-based deduplication
                            cached = state.get("content_hash")
                            if (
                                entry["change"] == "unchanged"
                                and isinstance(cached, str)
                                and re.fullmatch(r"[0-9a-f]{64}", cached)
                                and cached != hashlib.sha256(b"").hexdigest()
                            ):
                                # Reuse valid cached hash for unchanged files
                                entry["content_hash"] = cached
                            else:
                                # Extract content and compute new hash
                                content = extract_content(
                                    path, rules.max_size, rules.extensions, strict=True
                                )
                                after = path.stat()
                                # Verify file didn't change during read
                                if signature(after) != entry["signature"]:
                                    entry["reason"] = "changed_during_scan"
                                elif not content.strip():
                                    entry["reason"] = "empty_or_binary"
                                else:
                                    entry["content_hash"] = hashlib.sha256(
                                        content.encode()
                                    ).hexdigest()
                                del content  # Explicitly delete content from memory
                            entry["eligible"] = entry["content_hash"] is not None
                        else:
                            # Dry-run mode: don't hash, just mark as eligible if no exclusion reason
                            entry["eligible"] = True
                            entry["content_hash"] = (
                                state.get("content_hash")
                                if entry["change"] == "unchanged"
                                else None
                            )

                    except OSError as error:
                        from searchify.reporting.sync_report import describe_error

                        entry.update(
                            change="unavailable",
                            reason="local_read_error",
                            scan_error=describe_error("extraction", error),
                        )

                    entries.append(entry)

            # After walking, check for deleted files (files in old but not seen)
            if complete:
                for path, previous in old.items():
                    if previous["root_id"] == directory["id"] and path not in seen:
                        # Nested registered roots own their own paths
                        if any(
                            other != root
                            and other.is_relative_to(root)
                            and Path(path).is_relative_to(other)
                            for other in registered_roots
                        ):
                            continue
                        # Excluded subtrees are not missing files
                        rel = Path(path).relative_to(root).as_posix()
                        if any(
                            Path(path).is_relative_to(Path(folder))
                            for folder in excluded
                        ):
                            entries.append(
                                {
                                    "path": path,
                                    "root_id": directory["id"],
                                    "root_path": str(root),
                                    "relative_path": rel,
                                    "filename": previous.get("name", Path(path).name),
                                    "extension": previous.get(
                                        "extension", Path(path).suffix.lower()
                                    ),
                                    "size_bytes": previous.get("size", 0),
                                    "modified_ns": previous.get("mtime_ns", 0),
                                    "signature": states.get(path, {}).get("signature"),
                                    "content_hash": previous.get("sha256"),
                                    "change": "unchanged",
                                    "observed": False,
                                    "eligible": False,
                                    "reason": "excluded_pattern",
                                }
                            )
                            continue
                        # File genuinely missing - mark as deleted
                        entries.append(
                            {
                                "path": path,
                                "root_id": directory["id"],
                                "root_path": str(root),
                                "relative_path": rel,
                                "filename": previous.get("name", Path(path).name),
                                "change": "deleted",
                                "eligible": False,
                                "reason": "missing_file",
                            }
                        )
                else:  # noqa: PLW0120 - else on for loop executes when no break occurs
                    if not complete:
                        entries.append(
                            {
                                "path": str(root),
                                "root_id": directory["id"],
                                "root_path": str(root),
                                "relative_path": ".",
                                "filename": root.name,
                                "synthetic": True,
                                "change": "unavailable",
                                "eligible": False,
                                "reason": "incomplete_scan",
                            }
                        )

        # Compute scan metrics
        metrics = {
            "total_discovered": sum(
                e["change"] not in {"deleted", "unavailable"}
                and e.get("observed", True)
                for e in entries
            ),
            **{
                key: sum(
                    e["change"] == key and e.get("observed", True) for e in entries
                )
                for key in ("new", "modified", "unchanged", "deleted", "unverified")
            },
            "known_excluded": sum(e.get("observed") is False for e in entries),
            "excluded_directories": len(excluded),
        }
        return entries, metrics, excluded
