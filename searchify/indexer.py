
from pathlib import Path
import sqlite3
import os
import hashlib
import argparse
import re
import uuid
from datetime import datetime

DB_DIR = Path.home() / ".searchify"
DB_PATH = DB_DIR / "searchify.db"

SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__",
    "node_modules", ".next", "dist", "build",
    ".idea", ".vscode", "Pods", "Library",
    "DerivedData", ".Trash"
}

TEXT_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx",
    ".html", ".css", ".scss", ".json",
    ".md", ".txt", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".sql",
    ".sh", ".zsh", ".bash", ".java",
    ".c", ".cpp", ".h", ".go", ".rs",
    ".swift", ".kt", ".xml", ".env",
    ".log", ".csv"
}

MAX_CONTENT_SIZE = 2 * 1024 * 1024


def connect():
    DB_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    return conn


def initialize():
    with connect() as conn:
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

            CREATE VIRTUAL TABLE IF NOT EXISTS file_search
            USING fts5(
                path UNINDEXED,
                name,
                content,
                tokenize='unicode61'
            );
        """)


def register_directory(directory):
    path = Path(directory).expanduser().resolve()

    if not path.exists() or not path.is_dir():
        print(f"Invalid directory: {path}")
        return

    initialize()

    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO directories(path) VALUES (?)",
            (str(path),)
        )

    print(f"Registered: {path}")


def extract_content(path):
    if path.suffix.lower() not in TEXT_EXTENSIONS:
        return ""

    if path.stat().st_size > MAX_CONTENT_SIZE:
        return ""

    try:
        return path.read_text(
            encoding="utf-8",
            errors="replace"
        )
    except (OSError, UnicodeError):
        return ""


def calculate_hash(content):
    return hashlib.sha256(
        content.encode("utf-8")
    ).hexdigest()


def index_directory(conn, root_id, root_path):
    scan_id = str(uuid.uuid4())
    errors = []

    print(f"\nIndexing: {root_path}")

    def on_walk_error(error):
        errors.append(error)

    scanned = 0
    updated = 0
    unchanged = 0

    for current, dirs, filenames in os.walk(
        root_path,
        followlinks=False,
        onerror=on_walk_error
    ):
        # Ignore hidden and unnecessary directories.
        dirs[:] = [
            d for d in dirs
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
                    "SELECT size, mtime_ns FROM files WHERE path = ?",
                    (absolute_path,)
                ).fetchone()

                scanned += 1

                # Skip unchanged files.
                if (
                    existing
                    and existing["size"] == size
                    and existing["mtime_ns"] == mtime_ns
                ):
                    conn.execute(
                        """
                        UPDATE files
                        SET last_seen_scan = ?
                        WHERE path = ?
                        """,
                        (scan_id, absolute_path)
                    )

                    unchanged += 1
                    continue

                content = extract_content(path)
                content_hash = calculate_hash(content)

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
                        scan_id
                    )
                )

                # Refresh the full-text search entry.
                conn.execute(
                    "DELETE FROM file_search WHERE path = ?",
                    (absolute_path,)
                )

                conn.execute(
                    """
                    INSERT INTO file_search(path, name, content)
                    VALUES (?, ?, ?)
                    """,
                    (absolute_path, filename, content)
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
            (root_id, scan_id)
        ).fetchall()

        for file in stale_files:
            conn.execute(
                "DELETE FROM file_search WHERE path = ?",
                (file["path"],)
            )

            conn.execute(
                "DELETE FROM files WHERE path = ?",
                (file["path"],)
            )

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
    initialize()

    with connect() as conn:
        directories = conn.execute(
            "SELECT id, path FROM directories"
        ).fetchall()

        if not directories:
            print("No directories registered yet.")
            return

        for directory in directories:
            root = Path(directory["path"])

            if root.exists():
                index_directory(
                    conn,
                    directory["id"],
                    root
                )
            else:
                print(f"Directory unavailable: {root}")


def search_files(query, limit=10):
    initialize()

    terms = re.findall(r"[\w+-]+", query)

    if not terms:
        print("Please provide a searchable query.")
        return

    # Safely construct an FTS query.
    fts_query = " OR ".join(
        '"' + term.replace('"', '""') + '"'
        for term in terms
    )

    with connect() as conn:
        try:
            results = conn.execute(
                """
                SELECT
                    files.path,
                    files.name,
                    files.extension,
                    files.size,
                    bm25(file_search) AS score

                FROM file_search

                JOIN files ON files.path = file_search.path

                WHERE file_search MATCH ?

                ORDER BY score

                LIMIT ?
                """,
                (fts_query, limit)
            ).fetchall()

        except sqlite3.OperationalError as error:
            print(f"Search error: {error}")
            return

    if not results:
        print("No matching files found.")
        return

    print(f"\nFound {len(results)} matches:\n")

    for index, result in enumerate(results, 1):
        print(f"{index}. {result['name']}")
        print(f"   {result['path']}")
        print(f"   Type: {result['extension'] or 'No extension'}")
        print()


def show_status():
    initialize()

    with connect() as conn:
        directories = conn.execute(
            "SELECT COUNT(*) FROM directories"
        ).fetchone()[0]

        files = conn.execute(
            "SELECT COUNT(*) FROM files"
        ).fetchone()[0]

    print("\nSEARCHIFY INDEX STATUS")
    print(f"Registered directories: {directories}")
    print(f"Indexed files: {files}")
    print(f"Database: {DB_PATH}")


def main():
    parser = argparse.ArgumentParser(
        description="Searchify local file indexer"
    )

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
