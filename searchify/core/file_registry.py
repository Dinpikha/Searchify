"""
Local Metadata Registry and Synchronization Journal

This module provides the FileRegistry class which manages the local SQLite
database for Searchify. It stores:

1. Directory registrations (roots to scan)
2. File metadata (path, size, mtime, content hash)
3. Per-directory scanning rules
4. Synchronization state journal (processing status, errors, claims)

CRITICAL: This database NEVER stores extracted file contents. Only metadata,
hashes, and processing state are persisted. This is a core privacy guarantee.

The registry uses:
- WAL mode for concurrent read access
- Secure delete for privacy
- Advisory file locking (fcntl) for workspace serialization
- Foreign key constraints for data integrity

Used by: scanner.py, sync_service.py, indexer.py, summary_service.py
"""

import fcntl
import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path


def timestamp():
    """Generate UTC timestamp in ISO format for database records."""
    return datetime.now(timezone.utc).isoformat()


class RegistryBusy(RuntimeError):
    """Raised when another process holds the workspace lock."""


class FileRegistry:
    """
    Local SQLite registry for file metadata and sync state.

    The registry maintains two primary tables:
    - `files`: Metadata for each scanned file (path, size, mtime, sha256)
    - `sync_state`: Per-file processing state (status, remote_id, errors, claims)
    - `directory_rules`: Per-directory scanning configuration
    - `directories`: Registered root directories

    All operations are thread-safe within a process. Cross-process
    serialization is provided by the `lock()` context manager using
    kernel-enforced fcntl locks.
    """

    def __init__(self, db_path):
        """
        Initialize registry with database path.

        Args:
            db_path: Path to SQLite database file (~/.searchify/searchify.db by default)
        """
        self.path = Path(db_path).expanduser().resolve()

    @contextmanager
    def connection(self, write=False):
        """
        Context manager for database connections.

        Args:
            write: If True, open read-write connection with foreign keys and secure delete.
                   If False, open read-only URI connection for safe concurrent reads.

        Yields:
            sqlite3.Connection with row_factory set to sqlite3.Row
        """
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA secure_delete=ON")
        else:
            # Read-only URI connection allows concurrent readers without locking
            conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        with closing(conn), conn:
            yield conn

    def initialize(self):
        """
        Initialize database schema (files and directories tables).

        Delegates to indexer.initialize() for the base schema, then
        initializes the sync_state journal tables.
        """
        from searchify.indexer import initialize

        initialize(self.path)
        self.initialize_journal()

    def initialize_journal(self):
        """
        Create sync_state and directory_rules tables if they don't exist.

        The sync_state table tracks per-file processing status for the
        synchronization pipeline. It includes fields for:
        - Processing status (pending, processing, completed, failed, stale, skipped)
        - Remote Supabase file ID (remote_id)
        - Model versions for summary and embedding
        - Error details (JSON serialized)
        - Processing claim timestamp and owner (for concurrent worker coordination)
        - Reason for skipped/stale status

        Also handles schema migrations for summary_version and claim_owner columns.
        """
        with self.connection(True) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS directory_rules (
                    root_id INTEGER PRIMARY KEY REFERENCES directories(id) ON DELETE CASCADE,
                    rules TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sync_state (
                    path TEXT PRIMARY KEY,
                    root_id INTEGER NOT NULL REFERENCES directories(id) ON DELETE CASCADE,
                    signature TEXT,
                    content_hash TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    remote_id TEXT,
                    summary_model TEXT,
                    embedding_model TEXT,
                    summary_version INTEGER,
                    error TEXT,
                    claim_timestamp TEXT,
                    claim_owner TEXT,
                    reason TEXT,
                    updated_at TEXT NOT NULL
                );
            """)
            # Schema migration: add summary_version column if missing
            if "summary_version" not in {
                row[1] for row in conn.execute("PRAGMA table_info(sync_state)")
            }:
                conn.execute(
                    "ALTER TABLE sync_state ADD COLUMN summary_version INTEGER"
                )
            # Schema migration: add claim_owner column if missing
            if "claim_owner" not in {
                row[1] for row in conn.execute("PRAGMA table_info(sync_state)")
            }:
                conn.execute("ALTER TABLE sync_state ADD COLUMN claim_owner TEXT")

    @contextmanager
    def lock(self):
        """
        Acquire exclusive workspace lock using fcntl flock.

        This lock prevents multiple Searchify processes from concurrently
        modifying the registry or processing files. The lock is automatically
        released when the context exits, or if the process crashes (kernel
        releases the lock on file descriptor close).

        Uses a separate lock file (.sync.lock) adjacent to the database.

        Yields:
            None

        Raises:
            RegistryBusy: If another process holds the lock (non-blocking)
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Kernel releases the lock on interruption/crash. No stale lock deletion.
        with (self.path.parent / (self.path.name + ".sync.lock")).open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RegistryBusy(
                    "Another Searchify processing command holds the workspace lock."
                ) from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def directories(self):
        """
        List all registered directories.

        Returns:
            List of dicts with 'id' and 'path' keys, ordered by path.
            Returns empty list if database doesn't exist.
        """
        if not self.path.is_file():
            return []
        with self.connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT id, path FROM directories ORDER BY path"
                )
            ]

    def metadata(self, root_id=None):
        """
        Get file metadata for all files or a specific root.

        Args:
            root_id: Optional directory ID to filter by

        Returns:
            Dictionary mapping absolute file path -> file metadata dict
        """
        if not self.path.is_file():
            return {}
        with self.connection() as conn:
            sql = "SELECT * FROM files" + (
                " WHERE root_id = ?" if root_id is not None else ""
            )
            return {
                row["path"]: dict(row)
                for row in conn.execute(sql, (root_id,) if root_id is not None else ())
            }

    def states(self):
        """
        Get synchronization state for all files.

        Returns:
            Dictionary mapping absolute file path -> sync state dict.
            Error field is deserialized from JSON if present.
            Returns empty dict if database or sync_state table doesn't exist.
        """
        if not self.path.is_file():
            return {}
        with self.connection() as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='sync_state'"
            ).fetchone():
                return {}
            rows = [dict(row) for row in conn.execute("SELECT * FROM sync_state")]
        for row in rows:
            row["error"] = json.loads(row["error"]) if row["error"] else None
        return {row["path"]: row for row in rows}

    def rules(self, root_id):
        """
        Get scanning rules for a registered directory.

        Args:
            root_id: Directory ID

        Returns:
            DirectoryRules instance (default rules if none configured)
        """
        from searchify.core.scanner import DirectoryRules

        if not self.path.is_file():
            return DirectoryRules()
        with self.connection() as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='directory_rules'"
            ).fetchone():
                return DirectoryRules()
            row = conn.execute(
                "SELECT rules FROM directory_rules WHERE root_id=?", (root_id,)
            ).fetchone()
        return DirectoryRules.from_dict(json.loads(row[0])) if row else DirectoryRules()

    def save_rules(self, root_id, rules):
        """
        Save scanning rules for a directory.

        Args:
            root_id: Directory ID
            rules: DirectoryRules instance
        """
        self.initialize()
        with self.connection(True) as conn:
            conn.execute(
                "INSERT INTO directory_rules VALUES(?,?) ON CONFLICT(root_id) DO UPDATE SET rules=excluded.rules",
                (root_id, json.dumps(rules.to_dict())),
            )

    def register(self, path, rules=None):
        """
        Register a new directory root for scanning.

        Args:
            path: Directory path (will be expanded and resolved)
            rules: Optional DirectoryRules (defaults to preset for new registrations)

        Returns:
            Root ID (integer)

        Raises:
            ValueError: If path is not an existing directory
        """
        path = Path(path).expanduser().resolve()
        if not path.is_dir():
            raise ValueError("Directory does not exist.")
        self.initialize()
        with self.connection(True) as conn:
            existed = (
                conn.execute(
                    "SELECT 1 FROM directories WHERE path=?", (str(path),)
                ).fetchone()
                is not None
            )
            conn.execute(
                "INSERT OR IGNORE INTO directories(path) VALUES(?)", (str(path),)
            )
            root_id = conn.execute(
                "SELECT id FROM directories WHERE path=?", (str(path),)
            ).fetchone()[0]
        if rules is not None or not existed:
            from searchify.core.scanner import DirectoryRules

            self.save_rules(root_id, rules or DirectoryRules())
        return root_id

    def unregister(self, path):
        """
        Remove a directory registration and its local metadata.

        This only removes local registry data - NEVER touches original files
        on the filesystem.

        Args:
            path: Directory path to unregister

        Returns:
            True if directory was registered and removed, False otherwise
        """
        path = str(Path(path).expanduser().resolve())
        with self.connection(True) as conn:
            row = conn.execute(
                "SELECT id FROM directories WHERE path=?", (path,)
            ).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM files WHERE root_id=?", (row[0],))
            conn.execute("DELETE FROM directories WHERE id=?", (row[0],))
        return True

    def refresh(self, entries):
        """
        Update file metadata table with scan results.

        Updates the `files` table with new/changed file metadata.
        Skips synthetic entries, deleted files, and internal metadata.

        Args:
            entries: List of file entry dicts from Scanner.scan()
        """
        previous = self.metadata()
        with self.connection(True) as conn:
            for entry in entries:
                if (
                    entry.get("synthetic")
                    or entry["change"] == "deleted"
                    or entry.get("reason") == "internal_metadata"
                ):
                    continue
                old = previous.get(entry["path"], {})
                # Skip if unchanged (same hash, same root)
                if (
                    entry["change"] == "unchanged"
                    and old.get("sha256") == entry.get("content_hash")
                    and old.get("root_id") == entry["root_id"]
                ):
                    continue
                conn.execute(
                    """
                    INSERT INTO files(path,root_id,name,extension,size,mtime_ns,sha256,indexed_at)
                    VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                    root_id=excluded.root_id,name=excluded.name,extension=excluded.extension,
                    size=excluded.size,mtime_ns=excluded.mtime_ns,sha256=excluded.sha256,indexed_at=excluded.indexed_at
                """,
                    (
                        entry["path"],
                        entry["root_id"],
                        entry["filename"],
                        entry["extension"],
                        entry["size_bytes"],
                        entry["modified_ns"],
                        entry.get("content_hash"),
                        timestamp(),
                    ),
                )

    def save_state(self, entry, status, error=None, **updates):
        """
        Save or update synchronization state for a file.

        This is the primary method for tracking file processing state through
        the sync pipeline. It preserves existing values for fields not explicitly
        updated, allowing incremental state transitions.

        Args:
            entry: File entry dict (must contain 'path', 'root_id')
            status: Processing status ('pending', 'processing', 'completed', 'failed', 'stale', 'skipped')
            error: Optional error dict (will be JSON serialized)
            **updates: Additional fields to update (remote_id, summary_model, embedding_model,
                      summary_version, claim_timestamp, claim_owner)
        """
        with self.connection() as conn:
            found = conn.execute(
                "SELECT * FROM sync_state WHERE path=?", (entry["path"],)
            ).fetchone()
            current = dict(found) if found else {}
        values = {
            key: current.get(key)
            for key in (
                "remote_id",
                "summary_model",
                "embedding_model",
                "claim_timestamp",
                "claim_owner",
                "summary_version",
            )
        }
        values.update(updates)
        with self.connection(True) as conn:
            conn.execute(
                """
                INSERT INTO sync_state(path,root_id,signature,content_hash,status,remote_id,summary_model,
                                           embedding_model,summary_version,error,claim_timestamp,claim_owner,reason,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                root_id=excluded.root_id,signature=excluded.signature,content_hash=excluded.content_hash,status=excluded.status,
                remote_id=excluded.remote_id,summary_model=excluded.summary_model,embedding_model=excluded.embedding_model,
                summary_version=excluded.summary_version,error=excluded.error,claim_timestamp=excluded.claim_timestamp,claim_owner=excluded.claim_owner,reason=excluded.reason,updated_at=excluded.updated_at
            """,
                (
                    entry["path"],
                    entry["root_id"],
                    entry.get("signature"),
                    entry.get("content_hash"),
                    status,
                    values["remote_id"],
                    values["summary_model"],
                    values["embedding_model"],
                    values["summary_version"],
                    json.dumps(error) if error else None,
                    values["claim_timestamp"],
                    values["claim_owner"],
                    entry.get("reason"),
                    timestamp(),
                ),
            )
