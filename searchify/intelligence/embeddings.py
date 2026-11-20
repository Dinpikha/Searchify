"""
Query Embeddings Module

This module provides query-specific embedding generation that extends
the base Embeddings class with additional validation for search queries.

Key features:
- Validates query is non-empty text
- Ensures resulting embedding has non-zero magnitude
- Uses same model and local Ollama transport as summary embeddings
- Maintains consistency between query and document embeddings

Used by: search_service.py (SearchService)
"""

from searchify.ai.embeddings import Embeddings, validate_embedding


class QueryEmbeddings(Embeddings):
    """
    Query embedding generator with search-specific validation.

    Extends base Embeddings to add query-specific checks:
    - Input must be non-empty string
    - Output embedding must have non-zero magnitude
    """

    def embed(self, text):
        """
        Generate embedding for search query with validation.

        Args:
            text: Search query string

        Returns:
            768-dim embedding vector

        Raises:
            ValueError: If query is empty or embedding is zero vector
        """
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Search query must be nonempty text.")
        vector = validate_embedding(super().embed(text.strip()))
        if not any(vector):
            raise ValueError("Query embedding must have nonzero magnitude.")
        return vector
