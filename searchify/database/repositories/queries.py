"""
Query Repository - Search History and Match Tracking

This module provides data access for query history and file match
tracking in Supabase. Supports search analytics and result selection.

Tables:
- query_history: User search queries with optional embeddings
- query_file_matches: Ranked results with scores and selection state

Operations:
- create_query: Log search query with normalized text and embedding
- get_recent_queries: Fetch recent non-expired queries
- record_matches: Store ranked results with multiple score types
- mark_selected: User selects a result (single selection per query)
- get_query_matches: Retrieve matches for a query
- delete_expired_queries: Cleanup expired query history

Used by: Future search analytics and query understanding features
"""

from datetime import datetime, timezone

from searchify.database.supabase_client import (
    get_client,
    get_owner_id,
)


class QueryRepository:
    """
    Repository for query history and match persistence.

    All operations owner-scoped via auth.uid(). Supports search
    analytics, result ranking, and user selection tracking.
    """

    def __init__(self):
        """Initialize with authenticated Supabase client and owner_id."""
        self.client = get_client()
        self.owner_id = get_owner_id()

    def create_query(
        self,
        query_text,
        normalized_query,
        query_embedding=None,
    ):
        """
        Create query history record.

        Args:
            query_text: Original user query
            normalized_query: Normalized query text
            query_embedding: Optional 768-dim embedding vector

        Returns:
            Created query record

        Raises:
            ValueError: If query_text empty or embedding invalid
            RuntimeError: If insert returns no record
        """
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

        response = self.client.table("query_history").insert(payload).execute()

        if not response.data:
            raise RuntimeError("Failed to save query.")

        return response.data[0]

    def get_recent_queries(self, limit=10):
        """
        Get recent non-expired queries.

        Filters by expires_at > now (30-day default retention).

        Args:
            limit: Maximum results

        Returns:
            List of query dicts
        """
        response = (
            self.client.table("query_history")
            .select("id, query_text, normalized_query, created_at")
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
        """
        Record ranked file matches for a query.

        Supports multiple score types: semantic, lexical, RRF, rerank.
        Uses upsert with on_conflict="query_id,file_id".

        Args:
            query_id: Query UUID
            matches: List of match dicts with file_id and scores

        Returns:
            List of created match records
        """
        if not matches:
            return []

        now = datetime.now(timezone.utc).isoformat()

        payload = []

        for position, match in enumerate(matches, start=1):
            payload.append(
                {
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
                }
            )

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
        """
        Mark a file as selected for a query.

        Enforces single selection: deselects all other matches for query.

        Args:
            query_id: Query UUID
            file_id: File UUID to select

        Returns:
            Updated match record

        Raises:
            ValueError: If file not associated with query
        """
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
        """
        Get all matches for a query ordered by retrieval position.

        Args:
            query_id: Query UUID

        Returns:
            List of match dicts
        """
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
        """
        Delete expired query history records.

        Returns:
            Count of deleted records
        """
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
