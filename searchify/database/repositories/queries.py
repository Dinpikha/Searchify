
from datetime import datetime, timezone

from searchify.database.supabase_client import (
    get_client,
    get_owner_id,
)


class QueryRepository:

    def __init__(self):
        self.client = get_client()
        self.owner_id = get_owner_id()

    def create_query(
        self,
        query_text,
        normalized_query,
        query_embedding=None,
    ):
        if not query_text.strip():
            raise ValueError("Query cannot be empty.")

        payload = {
            "owner_id": self.owner_id,
            "query_text": query_text.strip(),
            "normalized_query": normalized_query.strip(),
        }

        if query_embedding is not None:
            if len(query_embedding) != 768:
                raise ValueError("Query embedding must have 768 dimensions.")

            payload["query_embedding"] = query_embedding

        response = (
            self.client.table("query_history")
            .insert(payload)
            .execute()
        )

        if not response.data:
            raise RuntimeError("Failed to save query.")

        return response.data[0]

    def get_recent_queries(self, limit=10):
        response = (
            self.client.table("query_history")
            .select(
                "id, query_text, normalized_query, created_at"
            )
            .eq("owner_id", self.owner_id)
            .gt(
                "expires_at",
                datetime.now(timezone.utc).isoformat(),
            )
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )

        return response.data or []

    def record_matches(self, query_id, matches):
        if not matches:
            return []

        now = datetime.now(timezone.utc).isoformat()

        payload = []

        for position, match in enumerate(matches, start=1):
            payload.append({
                "owner_id": self.owner_id,
                "query_id": query_id,
                "file_id": match["file_id"],
                "semantic_score": match.get("semantic_score"),
                "lexical_score": match.get("lexical_score"),
                "rrf_score": match.get("rrf_score"),
                "rerank_score": match.get("rerank_score"),
                "retrieval_position": position,
                "selected": False,
                "matched_at": now,
            })

        response = (
            self.client.table("query_file_matches")
            .upsert(
                payload,
                on_conflict="query_id,file_id",
            )
            .execute()
        )

        return response.data or []

    def mark_selected(self, query_id, file_id):
        existing = (
            self.client.table("query_file_matches")
            .select("id")
            .eq("query_id", query_id)
            .eq("file_id", file_id)
            .eq("owner_id", self.owner_id)
            .maybe_single()
            .execute()
        )

        if not existing.data:
            raise ValueError("File is not associated with this query.")

        (
            self.client.table("query_file_matches")
            .update({"selected": False})
            .eq("query_id", query_id)
            .eq("owner_id", self.owner_id)
            .execute()
        )

        response = (
            self.client.table("query_file_matches")
            .update({"selected": True})
            .eq("query_id", query_id)
            .eq("file_id", file_id)
            .eq("owner_id", self.owner_id)
            .execute()
        )

        return response.data

    def get_query_matches(self, query_id):
        response = (
            self.client.table("query_file_matches")
            .select("*")
            .eq("query_id", query_id)
            .eq("owner_id", self.owner_id)
            .order("retrieval_position")
            .execute()
        )

        return response.data or []

    def delete_expired_queries(self):
        response = (
            self.client.table("query_history")
            .delete()
            .eq("owner_id", self.owner_id)
            .lt(
                "expires_at",
                datetime.now(timezone.utc).isoformat(),
            )
            .execute()
        )

        return len(response.data or [])
