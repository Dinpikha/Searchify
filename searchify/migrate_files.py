import sqlite3
import mimetypes
from pathlib import Path
from datetime import datetime, timezone

from searchify.indexer import DB_PATH
from searchify.supabase_client import authenticate

CODE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java",
    ".c", ".cpp", ".h", ".go", ".rs", ".swift",
    ".kt", ".sh", ".zsh", ".sql", ".html", ".css"
}

def display_path(path):
    return "~/" + str(path.relative_to(Path.home()))

def migrate_files():
    client, owner_id = authenticate()

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        directories = conn.execute(
            "SELECT id, path FROM directories"
        ).fetchall()

        files = conn.execute(
            """SELECT path, root_id, name, extension,
                      size, mtime_ns, sha256
               FROM files"""
        ).fetchall()

    remote = client.table("directories").select(
        "id, path"
    ).eq("owner_id", owner_id).execute()

    directory_map = {
        d["path"]: d["id"] for d in remote.data
    }

    local_roots = {
        d["id"]: Path(d["path"]).expanduser().resolve()
        for d in directories
    }

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
            file["mtime_ns"] / 1_000_000_000,
            tz=timezone.utc
        ).isoformat()

        payload.append({
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
        })

    for start in range(0, len(payload), 100):
        batch = payload[start:start + 100]

        client.table("files").upsert(
            batch,
            on_conflict="directory_id,relative_path"
        ).execute()

        print(f"Synced {min(start + 100, len(payload))}/{len(payload)}")

    print(f"Finished. File records synced: {len(payload)}")


if __name__ == "__main__":
    migrate_files()