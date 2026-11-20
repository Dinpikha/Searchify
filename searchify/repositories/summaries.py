"""
Summary Repository - Supabase Summary and Embedding Operations

This module provides the high-level repository for file summaries and
embeddings in Supabase. It extends the base database repository with
additional validation and privacy enforcement.

Key validations:
- Rejects summary_data (only derived text and provenance allowed)
- Validates embedding dimensions (768) and non-zero magnitude
- Enforces model name consistency
- Semantic search validates query embedding and limits

All operations are owner-scoped via Supabase RLS (auth.uid()).

Used by: summary_service.py, semantic_search.py, search_service.py
"""

from searchify.ai.embeddings import validate_embedding
from searchify.database.repositories.summaries import (
    SummaryRepository as BaseSummaryRepository,
)


class SummaryRepository(BaseSummaryRepository):
    """
    Extended summary repository with validation layer.

    Wraps the base database repository to enforce privacy and
    consistency constraints before persistence.
    """

    def upsert_summary(
        self, file_id, content_hash, summary, summary_data, model_name, version=1
    ):
        """
        Upsert summary with privacy validation.

        Rejects any summary_data payload - only the derived summary
        text and model provenance are persisted. Original file content
        or chunks are never stored.

        Args:
            file_id: Supabase file UUID
            content_hash: SHA-256 of file content
            summary: Generated summary text
            summary_data: Must be empty/None (enforced)
            model_name: Summarization model name
            version: Summary schema version

        Returns:
            Created/updated summary record

        Raises:
            ValueError: If summary_data is provided
        """
        if summary_data:
            raise ValueError(
                "Only derived summary text and provenance may be persisted."
            )
        return super().upsert_summary(
            file_id, content_hash, summary, {}, model_name, version
        )

    def save_embedding(self, summary_id, embedding, model_name):
        """
        Save embedding with validation.

        Validates embedding dimensions (768), numeric values,
        and non-zero magnitude before persistence.

        Args:
            summary_id: Summary UUID
            embedding: 768-dim vector
            model_name: Embedding model name

        Returns:
            Created/updated embedding record

        Raises:
            ValueError: If embedding validation fails
            RuntimeError: If upsert returns no record
        """
        validate_embedding(embedding)
        result = super().save_embedding(summary_id, embedding, model_name)
        if not result:
            raise RuntimeError("Embedding upsert returned no record.")
        return result

    def semantic_search(self, embedding, limit=None, model_name=None):
        """
        Execute semantic search via pgvector RPC.

        Validates query embedding and limit, then calls
        match_file_summaries RPC with owner isolation via auth.uid().

        Args:
            embedding: 768-dim query vector
            limit: Result limit (1-1000, default: settings.INITIAL_CANDIDATE_LIMIT)
            model_name: Embedding model (default: settings.EMBEDDING_MODEL)

        Returns:
            List of ranked results with file_id, summary, path, similarity

        Raises:
            ValueError: If embedding or limit invalid
        """
        from searchify.config.settings import settings
        from searchify.retrieval.semantic_search import validate_limit

        vector = validate_embedding(embedding)
        if not any(vector):
            raise ValueError("Query embedding must have nonzero magnitude.")
        limit = validate_limit(
            settings.INITIAL_CANDIDATE_LIMIT if limit is None else limit
        )
        model_name = settings.EMBEDDING_MODEL if model_name is None else model_name
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("Embedding model name must be nonempty.")
        # Ownership comes exclusively from auth.uid() in the RPC, never caller input.
        response = self.client.rpc(
            "match_file_summaries",
            {
                "query_embedding": vector,
                "match_count": limit,
                "embedding_model": model_name,
            },
        ).execute()
        return response.data or []
