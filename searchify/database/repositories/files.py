from searchify.database.supabase_client import (
    get_client,
    get_owner_id,
)


class FileRepository:
    def __init__(self):
        self.client = get_client()
        self.owner_id = get_owner_id()

    def get_file(self, file_id):
        response = (
            self.client.table("files")
            .select("*")
            .eq("id", file_id)
            .eq("owner_id", self.owner_id)
            .maybe_single()
            .execute()
        )
        return response.data

    def get_pending_summaries(self, limit=50):
        response = (
            self.client.table("files")
            .select(
                "id, directory_id, relative_path, filename, "
                "extension, size_bytes, content_hash, status"
            )
            .eq("owner_id", self.owner_id)
            .eq("summary_status", "pending")
            .order("created_at")
            .limit(limit)
            .execute()
        )
        return response.data or []

    def update_summary_status(self, file_id, status, error=None):
        allowed = {"pending", "processing", "completed", "failed"}

        if status not in allowed:
            raise ValueError(f"Invalid summary status: {status}")

        payload = {
            "summary_status": status,
            "summary_error": error,
        }

        return (
            self.client.table("files")
            .update(payload)
            .eq("id", file_id)
            .eq("owner_id", self.owner_id)
            .execute()
        )

    def get_file_count(self):
        response = (
            self.client.table("files")
            .select("id", count="exact")
            .eq("owner_id", self.owner_id)
            .execute()
        )
        return response.count or 0
