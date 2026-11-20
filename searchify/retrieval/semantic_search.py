"""
Semantic Search Retrieval Module

This module provides the high-level interface for vector similarity search
over file summaries. It delegates to the SummaryRepository which calls
the Supabase pgvector RPC function (match_file_summaries).

Key privacy guarantee: Only the query embedding, stored summaries, and
metadata are used. Original file contents are NEVER read during search.

The RPC enforces:
- Owner isolation (RLS policies)
- Embedding model matching
- Summary completion status
- Content hash freshness
- Non-zero vector validation
- Result limit bounds (1-1000)

Used by: search_service.py, CLI search command
"""

from searchify.config.settings import settings


def validate_limit(limit):
    """
    Validate search result limit parameter.

    Args:
        limit: Limit value to validate

    Returns:
        Validated limit integer

    Raises:
        ValueError: If limit is not an integer between 1 and 1000
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("Result limit must be an integer between 1 and 1000.")
    return limit


class SemanticSearch:
    """
    Semantic search over file summaries using pgvector.

    Wraps SummaryRepository.semantic_search() with limit validation
    and default parameter handling.

    Attributes:
        summaries: SummaryRepository instance for Supabase RPC calls
    """

    def __init__(self, summaries=None):
        """
        Initialize semantic search with optional custom repository.

        Args:
            summaries: SummaryRepository instance (created if None)
        """
        if summaries is None:
            from searchify.repositories.summaries import SummaryRepository

            summaries = SummaryRepository()
        self.summaries = summaries

    def search(self, embedding, limit=None, model_name=None):
        """
        Execute semantic search with query embedding.

        Args:
            embedding: 768-dim query embedding vector
            limit: Maximum results (default: settings.INITIAL_CANDIDATE_LIMIT=10, max: 1000)
            model_name: Embedding model name (default: settings.EMBEDDING_MODEL)

        Returns:
            List of result dicts with file_id, summary_id, summary, path, similarity, etc.

        Raises:
            ValueError: If limit is invalid
        """
        limit = validate_limit(
            settings.INITIAL_CANDIDATE_LIMIT if limit is None else limit
        )
        return self.summaries.semantic_search(
            embedding, limit=limit, model_name=model_name or settings.EMBEDDING_MODEL
        )
