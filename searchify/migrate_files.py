"""
File Metadata Migration Script

This module provides a one-time migration utility to sync local file
metadata from SQLite to Supabase. It reads the local index database
and upserts file records to the remote Supabase database.

Key features:
- Reads local directories and files from SQLite
- Maps local paths to remote directory IDs
- Categorizes files as "code" or "text" based on extension
- Preserves existing remote summary status for unchanged files
- Only sets summary_status="pending" for new/changed files
- Batched upserts (100 records per batch) for efficiency

This is a standalone migration script, not part of the regular sync pipeline.
Run directly: python -m searchify.migrate_files

Used for: Initial setup, disaster recovery, manual migrations
"""

import mimetypes
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from searchify.indexer import DB_PATH
from searchify.supabase_client import authenticate

# Extensions considered as source code files
CODE_EXTENSIONS = {
    ".py",  # Python
    ".js",  # JavaScript
    ".jsx",  # React JavaScript
    ".ts",  # TypeScript
    ".tsx",  # React TypeScript
    ".java",  # Java
    ".c",  # C
    ".cpp",  # C++
    ".h",  # C/C++ headers
    ".go",  # Go
    ".rs",  # Rust
    ".swift",  # Swift
    ".kt",  # Kotlin
    ".sh",  # Shell
    ".zsh",  # Zsh
    ".sql",  # SQL
    ".html",  # HTML
    ".css",  # CSS
}


def display_path(path):
    """
    Format path for display with home directory as ~.

    Args:
        path: Path object or string

    Returns:
        Display string (e.g., "~/project/file.py")
    """
    return "~/" + str(path.relative_to(Path.home()))


def migrate_files():
    """
    Execute file metadata migration from local SQLite to Supabase.

    Process:
    1. Authenticate with Supabase
    2. Load local directories and files from SQLite
    3. Fetch existing remote directories and file hashes
    4. Build migration payload for each file
    5. Upsert to Supabase in batches (unchanged first, then changed)

    Preserves remote summary status for files with matching content hashes.
    Only marks files as "pending" summary if content hash differs.
    """
    client, owner_id = authenticate()

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        directories = conn.execute("SELECT id, path FROM directories").fetchall()

        files = conn.execute(
            """SELECT path, root_id, name, extension,
                      size, mtime_ns, sha256
                 FROM files"""
        ).fetchall()

    # Fetch remote directory mapping
    remote = (
        client.table("directories")
        .select("id, path")
        .eq("owner_id", owner_id)
        .execute()
    )

    directory_map = {d["path"]: d["id"] for d in remote.data}

    # Local root ID -> resolved path mapping
    local_roots = {d["id"]: Path(d["path"]).expanduser().resolve() for d in directories}

    # Fetch existing remote file hashes for change detection
    existing_hashes = {}
    offset = 0
    while True:
        page = (
            client.table("files")
            .select("directory_id, relative_path, content_hash")
            .eq("owner_id", owner_id)
            .order("id")
            .range(offset, offset + 999)
            .execute()
            .data
            or []
        )
        for record in page:
            existing_hashes[(record["directory_id"], record["relative_path"])] = record[
                "content_hash"
            ]
        if len(page) < 1000:
            break
        offset += len(page)

    payload = []

    for file in files:
        root = local_roots.get(file["root_id"])
        if not root:
            continue

        absolute_path = Path(file["path"]).resolve()

        try:
            relative_path = str(absolute_path.relative_to(root))
            remote_directory_id = directory_map[display_path(root)]
        except (ValueError, KeyError):
            continue

        ext = file["extension"] or ""
        category = "code" if ext in CODE_EXTENSIONS else "text"

        modified_at = datetime.fromtimestamp(
            file["mtime_ns"] / 1_000_000_000, tz=timezone.utc
        ).isoformat()

        record = {
            "owner_id": owner_id,
            "directory_id": remote_directory_id,
            "relative_path": relative_path,
            "filename": file["name"],
            "extension": ext,
            "mime_type": mimetypes.guess_type(file["name"])[0],
            "file_category": category,
            "size_bytes": file["size"],
            "modified_at": modified_at,
            "modified_ns": file["mtime_ns"],
            "content_hash": file["sha256"],
            "status": "metadata_only",
        }
        # Only set summary_status=pending if content hash differs (new/changed file)
        if existing_hashes.get((remote_directory_id, relative_path)) != file["sha256"]:
            record.update(
                summary_status="pending", summary_error=None, summary_updated_at=None
            )
        payload.append(record)

    synced = 0
    # Keep payload column sets identical; unchanged records must not reset status.
    # Process unchanged records first (no summary_status), then changed ones.
    for changed in (True, False):
        records = [
            record for record in payload if ("summary_status" in record) == changed
        ]
        for start in range(0, len(records), 100):
            batch = records[start : start + 100]
            client.table("files").upsert(
                batch, on_conflict="directory_id,relative_path"
            ).execute()
            synced += len(batch)
            print(f"Synced {synced}/{len(payload)}")

    print(f"Finished. File records synced: {len(payload)}")


if __name__ == "__main__":
    migrate_files()
