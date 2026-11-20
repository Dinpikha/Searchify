"""
Directory Registration Migration Script

This module provides a one-time migration utility to sync local directory
registrations from SQLite to Supabase. It reads registered directories
from the local index database and upserts them to the remote Supabase
database with privacy-preserving path display.

Key features:
- Reads local directory registrations from SQLite
- Converts absolute paths to display paths (~/project) to avoid exposing usernames
- Sets default enabled=True, read_only=True for migrated directories
- Batched upsert on owner_id,path conflict key
- Idempotent: safe to run multiple times

This is a standalone migration script, not part of the regular sync pipeline.
Run directly: python -m searchify.migrate_directories

Used for: Initial setup, disaster recovery, manual migrations
"""

import sqlite3
from pathlib import Path

from searchify.indexer import DB_PATH
from searchify.supabase_client import authenticate


def migrate_directories():
    """
    Execute directory registration migration from local SQLite to Supabase.

    Process:
    1. Authenticate with Supabase
    2. Load local directory paths from SQLite
    3. Convert absolute paths to display format (~/project)
    4. Build payload with owner_id, display path, name, enabled, read_only
    5. Upsert to Supabase with on_conflict="owner_id,path"

    Privacy: Display paths use ~ instead of absolute paths to avoid
    leaking local username in cloud metadata.
    """
    client, owner_id = authenticate()

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        directories = conn.execute("SELECT path FROM directories").fetchall()

    payload = []

    for directory in directories:
        local_path = Path(directory["path"]).expanduser().resolve()

        # Avoid exposing your Mac username in cloud metadata.
        display_path = "~/" + str(local_path.relative_to(Path.home()))

        payload.append(
            {
                "owner_id": owner_id,
                "path": display_path,
                "name": local_path.name,
                "enabled": True,
                "read_only": True,
            }
        )

    if not payload:
        print("No registered directories found.")
        return

    result = (
        client.table("directories")
        .upsert(payload, on_conflict="owner_id,path")
        .execute()
    )

    print(f"Directories synced: {len(result.data)}")


if __name__ == "__main__":
    migrate_directories()
