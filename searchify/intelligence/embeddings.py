"""Query embeddings share the summary model and local-only Ollama transport."""
from searchify.ai.embeddings import Embeddings, validate_embedding


class QueryEmbeddings(Embeddings):
    def embed(self, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Search query must be nonempty text.')
        vector = validate_embedding(super().embed(text.strip()))
        if not any(vector):
            raise ValueError('Query embedding must have nonzero magnitude.')
        return vector
