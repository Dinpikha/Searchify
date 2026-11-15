"""Retrieve ranked summaries and file metadata without opening source files."""
from searchify.config.settings import settings


def validate_limit(limit):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError('Result limit must be an integer between 1 and 1000.')
    return limit


class SemanticSearch:
    def __init__(self, summaries=None):
        if summaries is None:
            from searchify.repositories.summaries import SummaryRepository
            summaries = SummaryRepository()
        self.summaries = summaries

    def search(self, embedding, limit=None, model_name=None):
        limit = validate_limit(settings.INITIAL_CANDIDATE_LIMIT if limit is None else limit)
        return self.summaries.semantic_search(
            embedding, limit=limit, model_name=model_name or settings.EMBEDDING_MODEL)
