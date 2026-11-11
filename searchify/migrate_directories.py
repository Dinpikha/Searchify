
from pathlib import Path
import sqlite3

from searchify.indexer import DB_PATH
from searchify.supabase_client import authenticate


def migrate_directories():
    client, owner_id = authenticate()

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        directories = conn.execute(
            "SELECT path FROM directories"
        ).fetchall()

    payload = []

    for directory in directories:
        local_path = Path(directory["path"]).expanduser().resolve()

        # Avoid exposing your Mac username in cloud metadata.
        display_path = "~/" + str(
            local_path.relative_to(Path.home())
        )

        payload.append({
            "owner_id": owner_id,
            "path": display_path,
            "name": local_path.name,
            "enabled": True,
            "read_only": True,
        })

    if not payload:
        print("No registered directories found.")
        return

    result = client.table("directories").upsert(
        payload,
        on_conflict="owner_id,path"
    ).execute()

    print(f"Directories synced: {len(result.data)}")


if __name__ == "__main__":
    migrate_directories()
