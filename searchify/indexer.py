# Local SQLite metadata index and compatibility command entry point.
# FileRegistry/Scanner provide the current indexing workflow; older helpers remain
# available here. SummaryService uses resolve_indexed_file to validate registered
# identities before extraction. No new source-content cache is created.

import argparse
import hashlib
import os
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path

from searchify.core.extractor import MAX_CONTENT_SIZE, TEXT_EXTENSIONS, extract_content

DB_DIR = Path.home() / ".searchify"
DB_PATH = Path(
    os.getenv("SEARCHIFY_DB_PATH", str(DB_DIR / "searchify.db"))
).expanduser()

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".next",
    "dist",
    "build",
    ".idea",
    ".vscode",
    "Pods",
    "Library",
    "DerivedData",
    ".Trash",
}


# Open a writable metadata connection; callers own commit and close lifetimes.
def connect(db_path=None):
    path = Path(db_path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # WAL permits concurrent readers; foreign keys enforce root ownership and
    # secure deletion reduces remnants when old cached content is removed.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA secure_delete=ON")

    return conn


# Create root/file tables and indexes, then remove any legacy source cache.
def initialize(db_path=None):
    with closing(connect(db_path)) as conn, conn:
        # Absolute paths identify local files. Root deletion cascades metadata
        # removal through foreign keys without touching original files.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS directories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE NOT NULL,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                root_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                extension TEXT,
                size INTEGER,
                mtime_ns INTEGER,
                sha256 TEXT,
                indexed_at TEXT,
                last_seen_scan TEXT,
                FOREIGN KEY(root_id)
                    REFERENCES directories(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_files_root
            ON files(root_id);

            CREATE INDEX IF NOT EXISTS idx_files_extension
            ON files(extension);

        """)

    purge_legacy_content(db_path or DB_PATH)


def purge_legacy_content(db_path):
    """Remove the old source cache before indexing or summarizing existing data."""
    path = Path(db_path).resolve()
    if not path.exists():
        return
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        legacy = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='file_search'"
        ).fetchone()
    # Reclaim database pages and truncate WAL after dropping the historical text
    # cache, rather than leaving its contents in SQLite storage artifacts.
    if legacy:
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("PRAGMA secure_delete=ON")
            conn.execute("DROP TABLE file_search")
            conn.commit()
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


# Compatibility registration validates the root and inserts it idempotently.
def register_directory(directory):
    path = Path(directory).expanduser().resolve()

    if not path.exists() or not path.is_dir():
        print(f"Invalid directory: {path}")
        return

    initialize()

    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO directories(path) VALUES (?)", (str(path),))

    print(f"Registered: {path}")


# Hash UTF-8 extracted text, matching the summary pipeline content identity.
def calculate_hash(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class LocalContentError(ValueError):
    """A registered local file cannot be resolved or validated."""


def resolve_indexed_file(db_path, directory_path, relative_path, expected_hash):
    """Resolve only registered local metadata; do not read file contents."""
    root = Path(directory_path).expanduser().resolve()
    relative = Path(relative_path)
    candidate = root / relative
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or any(
            (root.joinpath(*relative.parts[: i + 1])).is_symlink()
            for i in range(len(relative.parts))
        )
    ):
        raise LocalContentError("Invalid local file identity.")
    path = candidate.resolve()
    if not path.is_relative_to(root) or not expected_hash:
        raise LocalContentError("Invalid local file identity.")
    try:
        with closing(
            sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
        ) as conn:
            row = conn.execute(
                """
                SELECT f.sha256, f.root_id FROM files f
                JOIN directories d ON d.id = f.root_id
                WHERE f.path = ? AND d.path = ?
            """,
                (str(path), str(root)),
            ).fetchone()
        if (
            row is None
            or row[0] != expected_hash
            or not path.is_file()
            or path.suffix.lower() not in TEXT_EXTENSIONS
            or path.stat().st_size > MAX_CONTENT_SIZE
        ):
            raise LocalContentError("Local file is missing, unsupported or stale.")
    except (sqlite3.Error, OSError):
        raise LocalContentError("Local index or file is unavailable.") from None
    from searchify.core.file_registry import FileRegistry
    from searchify.core.scanner import PROTECTED, matches

    rules = FileRegistry(db_path).rules(row[1])
    relative = path.relative_to(root).as_posix()
    if (
        not rules.sync_enabled
        or path.is_symlink()
        or path.suffix.lower() not in rules.extensions
        or path.stat().st_size > rules.max_size
        or not matches(relative, rules.include)
        or matches(relative, rules.exclude)
        or matches(path.name, PROTECTED)
    ):
        raise LocalContentError("Local file is missing, unsupported or stale.")
    return path


# Legacy single-root scan updates metadata and prunes missing rows only after a complete walk.
def index_directory(conn, root_id, root_path):
    scan_id = str(uuid.uuid4())
    errors = []

    print(f"\nIndexing: {root_path}")

    # Retain traversal errors so inaccessible paths cannot be mistaken for deletions.
    def on_walk_error(error):
        errors.append(error)

    scanned = 0
    updated = 0
    unchanged = 0

    for current, dirs, filenames in os.walk(
        root_path, followlinks=False, onerror=on_walk_error
    ):
        # Ignore hidden and unnecessary directories.
        dirs[:] = [
            d
            for d in dirs
            if d not in SKIP_DIRS
            and not d.startswith(".")
            and not (Path(current) / d).is_symlink()
        ]

        for filename in filenames:
            if filename.startswith("."):
                continue

            path = Path(current) / filename

            try:
                stat = path.stat()

                if not path.is_file():
                    continue

                absolute_path = str(path.resolve())
                size = stat.st_size
                mtime_ns = stat.st_mtime_ns
                extension = path.suffix.lower()

                existing = conn.execute(
                    "SELECT size, mtime_ns, sha256 FROM files WHERE path = ?",
                    (absolute_path,),
                ).fetchone()

                scanned += 1

                # Revalidate the extracted hash, including legacy empty/invalid values.
                content = extract_content(path)
                content_hash = calculate_hash(content) if content.strip() else None
                del content
                if (
                    existing
                    and existing["size"] == size
                    and existing["mtime_ns"] == mtime_ns
                    and existing["sha256"] == content_hash
                ):
                    conn.execute(
                        """
                        UPDATE files
                        SET last_seen_scan = ?
                        WHERE path = ?
                        """,
                        (scan_id, absolute_path),
                    )

                    unchanged += 1
                    continue

                conn.execute(
                    """
                    INSERT INTO files (
                        path, root_id, name, extension,
                        size, mtime_ns, sha256,
                        indexed_at, last_seen_scan
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)

                    ON CONFLICT(path) DO UPDATE SET
                        root_id = excluded.root_id,
                        name = excluded.name,
                        extension = excluded.extension,
                        size = excluded.size,
                        mtime_ns = excluded.mtime_ns,
                        sha256 = excluded.sha256,
                        indexed_at = excluded.indexed_at,
                        last_seen_scan = excluded.last_seen_scan
                    """,
                    (
                        absolute_path,
                        root_id,
                        filename,
                        extension,
                        size,
                        mtime_ns,
                        content_hash,
                        datetime.now().isoformat(),
                        scan_id,
                    ),
                )

                updated += 1

            except (OSError, PermissionError) as error:
                errors.append(error)
                continue

    # Only remove missing files if the directory scan was complete.
    if not errors:
        stale_files = conn.execute(
            """
            SELECT path FROM files
            WHERE root_id = ? AND last_seen_scan != ?
            """,
            (root_id, scan_id),
        ).fetchall()

        for file in stale_files:
            conn.execute("DELETE FROM files WHERE path = ?", (file["path"],))

    conn.commit()

    print(f"Files examined: {scanned}")
    print(f"New/updated: {updated}")
    print(f"Unchanged/skipped: {unchanged}")

    if errors:
        print(f"Warnings: {len(errors)}")
        print("Missing-file cleanup skipped to avoid deleting valid entries.")

    else:
        print(f"Removed missing files: {len(stale_files)}")


def index_all():
    """Refresh metadata using the same directory-specific scanner as sync."""
    from searchify.core.file_registry import FileRegistry
    from searchify.core.scanner import Scanner

    registry = FileRegistry(DB_PATH)
    with registry.lock():
        registry.initialize()
        if not registry.directories():
            print("No directories registered yet.")
            return
        previous = registry.states()
        entries, metrics, _ = Scanner(registry).scan()
        registry.refresh(entries)
        for entry in entries:
            if entry.get("synthetic"):
                continue
            old = previous.get(entry["path"], {})
            status = (
                "unavailable"
                if entry["change"] == "deleted"
                else "skipped"
                if not entry["eligible"]
                else "pending"
                if entry["change"] != "unchanged"
                else old.get("status", "pending")
            )
            registry.save_state(
                entry,
                status,
                error=old.get("error") if entry["change"] == "unchanged" else None,
            )
        print(
            "Metadata refreshed: "
            + ", ".join(f"{key}: {value}" for key, value in metrics.items())
        )


def search_files(query, limit=10):
    """Search indexed names without initializing data or reading original contents."""
    # Tokenize user input into bound substring parameters, then recheck current
    # filesystem availability and directory rules before displaying matches.
    terms = re.findall(r"[\w+-]+", query)
    if not terms:
        print("Please provide a searchable query.")
        return
    if not Path(DB_PATH).is_file():
        print(
            "No local index yet. Register a directory and run index or sync explicitly."
        )
        return
    from searchify.core.file_registry import FileRegistry
    from searchify.core.scanner import PROTECTED, PROTECTED_DIRS, matches

    registry = FileRegistry(DB_PATH)
    rules = {row["id"]: registry.rules(row["id"]) for row in registry.directories()}
    results = []
    with registry.connection() as conn:
        cursor = conn.execute(
            "SELECT f.path,f.name,f.extension,f.size,f.root_id,d.path AS root_path FROM files f "
            "JOIN directories d ON d.id=f.root_id WHERE "
            + " OR ".join("instr(lower(f.name),lower(?))>0" for _ in terms)
            + " ORDER BY f.name",
            terms,
        )
        for row in cursor:
            path = Path(row["path"])
            root = Path(row["root_path"])
            if (
                path.is_symlink()
                or not path.is_file()
                or not path.resolve().is_relative_to(root.resolve())
            ):
                continue
            relative = path.relative_to(root).as_posix()
            rule = rules[row["root_id"]]
            if (
                not matches(relative, rule.include)
                or matches(relative, rule.exclude)
                or matches(path.name, PROTECTED)
                or any(part in PROTECTED_DIRS for part in path.relative_to(root).parts)
            ):
                continue
            results.append(dict(row))
            if len(results) >= limit:
                break
    if not results:
        print("No matching available files found.")
        return
    print(f"Found {len(results)} matches:")
    for position, row in enumerate(results, 1):
        from searchify.cli.output import literal

        print(f"{position}. {literal(row['name'])}\n   {literal(row['path'])}")


# Legacy status initializes the index; the unified workspace dashboard is read-only.
def show_status():
    initialize()

    with connect() as conn:
        directories = conn.execute("SELECT COUNT(*) FROM directories").fetchone()[0]

        files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    print("\nSEARCHIFY INDEX STATUS")
    print(f"Registered directories: {directories}")
    print(f"Indexed files: {files}")
    print(f"Database: {DB_PATH}")


# Standalone indexer command parser; the public CLI dispatches through cli.app instead.
def main():
    parser = argparse.ArgumentParser(description="Searchify local file indexer")

    subparsers = parser.add_subparsers(dest="command")

    add_parser = subparsers.add_parser("add")
    add_parser.add_argument("directory")

    subparsers.add_parser("index")
    subparsers.add_parser("status")

    search_parser = subparsers.add_parser("search")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=10)

    args = parser.parse_args()

    if args.command == "add":
        register_directory(args.directory)

    elif args.command == "index":
        index_all()

    elif args.command == "status":
        show_status()

    elif args.command == "search":
        search_files(args.query, args.limit)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
