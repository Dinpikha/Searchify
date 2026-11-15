"""Semantic retrieval uses only the query, stored summaries and metadata."""

from searchify.config.settings import settings
from searchify.intelligence.embeddings import QueryEmbeddings
from searchify.retrieval.semantic_search import SemanticSearch, validate_limit


class SearchService:
    def __init__(self, embeddings=None, retrieval=None):
        self.embeddings = embeddings if embeddings is not None else QueryEmbeddings()
        self.retrieval = retrieval if retrieval is not None else SemanticSearch()

    def search(self, query, limit=None):
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Search query must be nonempty text.")
        limit = validate_limit(
            settings.INITIAL_CANDIDATE_LIMIT if limit is None else limit
        )
        embedding = self.embeddings.embed(query.strip())
        return self.retrieval.search(
            embedding, limit=limit, model_name=self.embeddings.model_name
        )
