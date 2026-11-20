"""
Vector Embedding Generation Module

This module handles generation of 768-dimensional vector embeddings for
file summaries using a local embedding model via Ollama. Embeddings are
used for semantic similarity search in Supabase pgvector.

Key features:
- Strict validation of embedding dimensions (must be exactly 768)
- Rejection of non-finite values (NaN, infinity) and boolean values
- Rejection of zero vectors (all zeros)
- Configurable model via settings.EMBEDDING_MODEL (default: embeddinggemma-2:270m)
- No truncation - fails if input exceeds model's context window

The embedding model must match the Supabase schema (vector(768)) and
the HNSW index configuration.

Used by: summary_service.py (during file processing), search_service.py (query embedding),
semantic_search.py (retrieval)
"""

import math

from searchify.ai.ollama_client import OllamaClient, OllamaError
from searchify.config.settings import settings


def validate_embedding(embedding):
    """
    Validate that an embedding vector meets all requirements.

    Checks:
    - Is a list (not tuple, array, etc.)
    - Has exactly 768 dimensions
    - All values are finite numbers (float or int, not bool)
    - No NaN or infinity values
    - At least one non-zero value (not a zero vector)

    Args:
        embedding: List of numeric values

    Returns:
        The validated embedding list

    Raises:
        ValueError: If any validation check fails
    """
    if (
        not isinstance(embedding, list)
        or len(embedding) != 768
        or any(
            isinstance(value, bool)
            or not isinstance(value, (float, int))
            or not math.isfinite(value)
            for value in embedding
        )
    ):
        raise ValueError("Expected 768 finite numeric embedding dimensions.")
    if not any(embedding):
        raise ValueError("Expected a nonzero embedding vector.")
    return embedding


class Embeddings:
    """
    Generates vector embeddings using a local embedding model via Ollama.

    Attributes:
        client: OllamaClient instance for API communication
        model_name: Name of the embedding model (from settings.EMBEDDING_MODEL)
    """

    def __init__(self, client=None):
        """
        Initialize embeddings generator with optional custom client.

        Args:
            client: OllamaClient instance (creates default if None)
        """
        self.client = client or OllamaClient()
        self.model_name = settings.EMBEDDING_MODEL

    def embed(self, text):
        """
        Generate a 768-dimensional embedding for the given text.

        Calls Ollama's /api/embed endpoint with the configured model.
        Requests 768 dimensions explicitly and disables truncation
        (fails fast if input exceeds model context).

        Args:
            text: Input text to embed (typically a file summary)

        Returns:
            List of 768 floats representing the embedding vector

        Raises:
            OllamaError: If API call fails or returns invalid response
            ValueError: If returned embedding fails validation
        """
        data = self.client.post(
            "/api/embed",
            {
                "model": self.model_name,
                "input": text,
                "dimensions": 768,
                "truncate": False,
            },
        )
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != 1:
            raise OllamaError("Ollama returned an invalid embedding response.")
        return validate_embedding(vectors[0])
