import math

from searchify.ai.ollama_client import OllamaClient, OllamaError
from searchify.config.settings import settings


def validate_embedding(embedding):
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
    def __init__(self, client=None):
        self.client = client or OllamaClient()
        self.model_name = settings.EMBEDDING_MODEL

    def embed(self, text):
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
