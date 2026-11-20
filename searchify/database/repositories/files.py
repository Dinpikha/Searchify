"""
Base File Repository - Supabase File Metadata CRUD

This module provides the base data access layer for file metadata
in Supabase. All operations are owner-scoped via auth.uid() through
the shared Supabase client.

Table: files
- id, owner_id, directory_id, relative_path, filename, extension
- size_bytes, modified_ns, content_hash
- status, summary_status, summary_error, summary_updated_at
- created_at, updated_at

Operations:
- get_file: Fetch file by ID
- get_pending_summaries: List files needing summarization
- update_summary_status: Update summary_status with error
- get_file_count: Total file count for owner

Used by: searchify.repositories.files (extended with sync pipeline support)
"""

from searchify.database.supabase_client import (
    get_client,
    get_owner_id,
)


class FileRepository:
    """
    Base repository for file metadata persistence.

    All queries automatically filter by owner_id from authenticated session.
    """

    def __init__(self):
        """Initialize with authenticated Supabase client and owner_id."""
        self.client = get_client()
        self.owner_id = get_owner_id()

    def get_file(self, file_id):
        """
        Get full file metadata by ID.

        Args:
            file_id: Supabase file UUID

        Returns:
            File dict or None
        """
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
        """
        Get files with pending summary_status.

        Args:
            limit: Maximum results

        Returns:
            List of file dicts with minimal fields
        """
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
        """
        Update summary_status for a file.

        Args:
            file_id: Supabase file UUID
            status: New summary_status (pending, processing, completed, failed)
            error: Optional error message

        Returns:
            Updated rows

        Raises:
            ValueError: If status not in allowed set
        """
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
        """
        Get total file count for current owner.

        Returns:
            Integer count
        """
        response = (
            self.client.table("files")
            .select("id", count="exact")
            .eq("owner_id", self.owner_id)
            .execute()
        )
        return response.count or 0
