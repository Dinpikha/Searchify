
from datetime import datetime, timezone

from searchify.database.supabase_client import (
    get_client,
    get_owner_id,
)


class SummaryRepository:

    def __init__(self):
        self.client = get_client()
        self.owner_id = get_owner_id()

    def get_summary(self, file_id):
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
        payload = {
            "owner_id": self.owner_id,
            "file_id": file_id,
            "source_content_hash": content_hash,
            "summary": summary,
            "summary_data": summary_data,
            "model_name": model_name,
            "summary_version": version,
            "generated_at": datetime.now(
                timezone.utc
            ).isoformat(),
            "updated_at": datetime.now(
                timezone.utc
            ).isoformat(),
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
        if len(embedding) != 768:
            raise ValueError(
                f"Expected 768 dimensions, received {len(embedding)}"
            )

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
        response = (
            self.client.table("file_summary_embeddings")
            .select("embedding, model_name")
            .eq("summary_id", summary_id)
            .eq("owner_id", self.owner_id)
            .maybe_single()
            .execute()
        )

        return response.data
