"""
Base Summary Repository - Supabase Summary and Embedding CRUD

This module provides the base data access layer for file summaries
and embeddings in Supabase. All operations are owner-scoped via
auth.uid() through the shared Supabase client.

Tables:
- file_summaries: Summary text, model info, content hash, version
- file_summary_embeddings: 768-dim vectors with model name

Operations:
- get_summary: Fetch summary by file_id
- upsert_summary: Create or update summary (on_conflict=file_id)
- save_embedding: Store embedding (on_conflict=summary_id)
- get_summary_embedding: Fetch embedding by summary_id

Used by: searchify.repositories.summaries (extended with validation)
"""

from datetime import datetime, timezone

from searchify.database.supabase_client import (
    get_client,
    get_owner_id,
)


class SummaryRepository:
    """
    Base repository for summary and embedding persistence.

    All queries automatically filter by owner_id from authenticated session.
    Uses Supabase upsert with conflict resolution on primary keys.
    """

    def __init__(self):
        """Initialize with authenticated Supabase client and owner_id."""
        self.client = get_client()
        self.owner_id = get_owner_id()

    def get_summary(self, file_id):
        """
        Get summary for a file.

        Args:
            file_id: Supabase file UUID

        Returns:
            Summary dict or None
        """
        response = (
            self.client.table("file_summaries")
            .select("*")
            .eq("file_id", file_id)
            .eq("owner_id", self.owner_id)
            .maybe_single()
            .execute()
        )
        return response.data

    def upsert_summary(
        self,
        file_id,
        content_hash,
        summary,
        summary_data,
        model_name,
        version=1,
    ):
        """
        Create or update summary for a file.

        Uses upsert with on_conflict="file_id" to ensure one summary per file.

        Args:
            file_id: Supabase file UUID
            content_hash: SHA-256 of source content
            summary: Generated summary text
            summary_data: Additional provenance data (JSON)
            model_name: Summarization model name
            version: Summary schema version

        Returns:
            Created/updated summary record

        Raises:
            RuntimeError: If upsert returns no record
        """
        payload = {
            "owner_id": self.owner_id,
            "file_id": file_id,
            "source_content_hash": content_hash,
            "summary": summary,
            "summary_data": summary_data,
            "model_name": model_name,
            "summary_version": version,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        response = (
            self.client.table("file_summaries")
            .upsert(payload, on_conflict="file_id")
            .execute()
        )

        if not response.data:
            raise RuntimeError("Summary upsert returned no record.")

        return response.data[0]

    def save_embedding(
        self,
        summary_id,
        embedding,
        model_name,
    ):
        """
        Store embedding vector for a summary.

        Validates 768 dimensions before persistence. Uses upsert
        with on_conflict="summary_id" for one embedding per summary.

        Args:
            summary_id: Summary UUID
            embedding: 768-dim vector
            model_name: Embedding model name

        Returns:
            Created/updated embedding record

        Raises:
            ValueError: If embedding dimensions != 768
        """
        if len(embedding) != 768:
            raise ValueError(f"Expected 768 dimensions, received {len(embedding)}")

        payload = {
            "summary_id": summary_id,
            "owner_id": self.owner_id,
            "model_name": model_name,
            "embedding": embedding,
        }

        response = (
            self.client.table("file_summary_embeddings")
            .upsert(payload, on_conflict="summary_id")
            .execute()
        )

        return response.data

    def get_summary_embedding(self, summary_id):
        """
        Get embedding for a summary.

        Args:
            summary_id: Summary UUID

        Returns:
            Dict with embedding and model_name, or None
        """
        response = (
            self.client.table("file_summary_embeddings")
            .select("embedding, model_name")
            .eq("summary_id", summary_id)
            .eq("owner_id", self.owner_id)
            .maybe_single()
            .execute()
        )

        return response.data
