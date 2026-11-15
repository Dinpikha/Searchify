from searchify.ai.embeddings import validate_embedding
from searchify.database.repositories.summaries import (
    SummaryRepository as BaseSummaryRepository,
)


class SummaryRepository(BaseSummaryRepository):
    def upsert_summary(
        self, file_id, content_hash, summary, summary_data, model_name, version=1
    ):
        if summary_data:
            raise ValueError(
                "Only derived summary text and provenance may be persisted."
            )
        return super().upsert_summary(
            file_id, content_hash, summary, {}, model_name, version
        )

    def save_embedding(self, summary_id, embedding, model_name):
        validate_embedding(embedding)
        result = super().save_embedding(summary_id, embedding, model_name)
        if not result:
            raise RuntimeError("Embedding upsert returned no record.")
        return result

    def semantic_search(self, embedding, limit=None, model_name=None):
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
