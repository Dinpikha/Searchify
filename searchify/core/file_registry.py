"""Local metadata and synchronization journal. Never stores extracted contents."""

import fcntl
import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class RegistryBusy(RuntimeError):
    pass


class FileRegistry:
    def __init__(self, db_path):
        self.path = Path(db_path).expanduser().resolve()

    @contextmanager
    def connection(self, write=False):
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA secure_delete=ON")
        else:
            conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        with closing(conn), conn:
            yield conn

    def initialize(self):
        from searchify.indexer import initialize

        initialize(self.path)
        self.initialize_journal()

    def initialize_journal(self):
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
            if "summary_version" not in {
                row[1] for row in conn.execute("PRAGMA table_info(sync_state)")
            }:
                conn.execute(
                    "ALTER TABLE sync_state ADD COLUMN summary_version INTEGER"
                )
            if "claim_owner" not in {
                row[1] for row in conn.execute("PRAGMA table_info(sync_state)")
            }:
                conn.execute("ALTER TABLE sync_state ADD COLUMN claim_owner TEXT")

    @contextmanager
    def lock(self):
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
        self.initialize()
        with self.connection(True) as conn:
            conn.execute(
                "INSERT INTO directory_rules VALUES(?,?) ON CONFLICT(root_id) DO UPDATE SET rules=excluded.rules",
                (root_id, json.dumps(rules.to_dict())),
            )

    def register(self, path, rules=None):
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
        """Remove local registration and metadata only, never original files."""
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
