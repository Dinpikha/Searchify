"""
Search Service - Semantic Query Handling

This module provides the high-level search interface for Searchify.
It coordinates query embedding and semantic retrieval, ensuring that
only the query text, stored summaries, and metadata are used -
never the original file contents.

Key responsibilities:
- Validate and sanitize search queries
- Generate query embeddings using the same model as indexed summaries
- Delegate to semantic search retrieval (pgvector RPC)
- Enforce result limits and model consistency

The search flow:
1. User provides natural language query
2. QueryEmbeddings generates 768-dim vector using embeddinggemma-2:270m
3. SemanticSearch calls Supabase match_file_summaries RPC
4. Results returned as ranked file summaries with similarity scores

Privacy guarantee: Original file contents are never read during search.
Only stored summaries (generated during sync) and metadata are accessed.

Used by: CLI search command, future web UI API
"""

from searchify.config.settings import settings
from searchify.intelligence.embeddings import QueryEmbeddings
from searchify.retrieval.semantic_search import SemanticSearch, validate_limit


class SearchService:
    """
    High-level semantic search service.

    Combines query embedding generation with vector similarity search.
    Supports dependency injection for testing and alternative implementations.

    Attributes:
        embeddings: QueryEmbeddings instance for query vector generation
        retrieval: SemanticSearch instance for Supabase RPC calls
    """

    def __init__(self, embeddings=None, retrieval=None):
        """
        Initialize search service with optional custom components.

        Args:
            embeddings: QueryEmbeddings instance (default: new instance)
            retrieval: SemanticSearch instance (default: new instance)
        """
        self.embeddings = embeddings if embeddings is not None else QueryEmbeddings()
        self.retrieval = retrieval if retrieval is not None else SemanticSearch()

    def search(self, query, limit=None):
        """
        Execute semantic search for the given query.

        Args:
            query: Natural language search query string
            limit: Maximum results to return (default: settings.INITIAL_CANDIDATE_LIMIT=10,
                   max: 1000)

        Returns:
            List of result dicts with file_id, summary, path, similarity, etc.

        Raises:
            ValueError: If query is empty or limit is invalid
        """
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Search query must be nonempty text.")
        limit = validate_limit(
            settings.INITIAL_CANDIDATE_LIMIT if limit is None else limit
        )
        embedding = self.embeddings.embed(query.strip())
        return self.retrieval.search(
            embedding, limit=limit, model_name=self.embeddings.model_name
        )
