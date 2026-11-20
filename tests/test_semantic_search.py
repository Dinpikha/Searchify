# Exercises query embeddings, result validation, and the summary RPC adapter through
# injected fakes.
# Assertions preserve summary-only retrieval without opening indexed source files.

"""Semantic search tests run without Ollama, file reads or a Supabase account."""
import importlib
import sys
import types
import unittest
from unittest.mock import Mock, patch

try:
    import dotenv
except ImportError:
    dotenv = types.ModuleType('dotenv')
    dotenv.load_dotenv = lambda *args: None
    sys.modules['dotenv'] = dotenv

from searchify.intelligence.embeddings import QueryEmbeddings
from searchify.retrieval.semantic_search import SemanticSearch
from searchify.services.search_service import SearchService
from searchify.config.settings import settings


class SemanticSearchTests(unittest.TestCase):
    def setUp(self):
        self.vector = [1.0] + [0.0] * 767

    def repository(self):
        auth = types.ModuleType('searchify.database.supabase_client')
        auth.get_client, auth.get_owner_id = Mock(), Mock()
        with patch.dict(sys.modules, {'searchify.database.supabase_client': auth}):
            module = importlib.import_module('searchify.repositories.summaries')
        repo = module.SummaryRepository.__new__(module.SummaryRepository)
        repo.client = Mock()
        repo.owner_id = 'authenticated-user'
        return repo

    def test_query_uses_same_local_model_and_dimensions(self):
        client = Mock()
        client.post.return_value = {'embeddings': [self.vector]}
        embeddings = QueryEmbeddings(client)
        self.assertEqual(embeddings.embed('  authentication helpers  '), self.vector)
        client.post.assert_called_once_with('/api/embed', {
            'model': settings.EMBEDDING_MODEL, 'input': 'authentication helpers',
            'dimensions': 768, 'truncate': False,
        })
        self.assertEqual(embeddings.model_name, settings.EMBEDDING_MODEL)

    def test_embedding_validation_prevents_rpc(self):
        repo = self.repository()
        for vector in ([1.] * 767, [float('nan')] * 768, [float('inf')] * 768,
                       [True] * 768, [0.] * 768):
            with self.subTest(vector=vector[:1]), self.assertRaises(ValueError):
                repo.semantic_search(vector)
        repo.client.rpc.assert_not_called()

    def test_query_embedding_validation(self):
        client = Mock()
        embeddings = QueryEmbeddings(client)
        for vector in ([0.] * 768, [1.] * 769, [False] * 768):
            client.post.return_value = {'embeddings': [vector]}
            with self.assertRaises(ValueError):
                embeddings.embed('query')
        for query in ('', '  ', None, 12):
            with self.assertRaises(ValueError):
                embeddings.embed(query)

    def test_ranked_results_and_limit_are_forwarded_without_file_access(self):
        repo = self.repository()
        rows = [dict(file_id='a', summary='First summary', path='~/project/a.py', similarity=0.95),
                dict(file_id='b', summary='Second summary', path='~/project/b.py', similarity=0.65)]
        repo.client.rpc.return_value.execute.return_value.data = rows
        embeddings = Mock(model_name=settings.EMBEDDING_MODEL)
        embeddings.embed.return_value = self.vector
        service = SearchService(embeddings, SemanticSearch(repo))
        with patch('searchify.core.extractor.extract_content', side_effect=AssertionError('source read')), \
             patch('sqlite3.connect', side_effect=AssertionError('local database read')), \
             patch('pathlib.Path.open', side_effect=AssertionError('file read')):
            self.assertEqual(service.search('  authentication  ', limit=2), rows)
        embeddings.embed.assert_called_once_with('authentication')
        repo.client.rpc.assert_called_once_with('match_file_summaries', {
            'query_embedding': self.vector, 'match_count': 2,
            'embedding_model': settings.EMBEDDING_MODEL,
        })
        repo.client.table.assert_not_called()

    def test_no_caller_selected_user_or_query_text_sent_to_rpc(self):
        repo = self.repository()
        repo.client.rpc.return_value.execute.return_value.data = []
        self.assertEqual(repo.semantic_search(self.vector), [])
        params = repo.client.rpc.call_args.args[1]
        self.assertEqual(set(params), {'query_embedding', 'match_count', 'embedding_model'})
        with self.assertRaises(TypeError):
            repo.semantic_search(self.vector, owner_id='another-user')

    def test_invalid_limit_fails_before_embedding(self):
        embeddings = Mock()
        service = SearchService(embeddings, Mock())
        for limit in (0, -1, True, 1.5, '2', 1001):
            with self.assertRaises(ValueError):
                service.search('query', limit=limit)
        embeddings.embed.assert_not_called()

    def test_empty_results_and_model_filter(self):
        repo = self.repository()
        repo.client.rpc.return_value.execute.return_value.data = None
        self.assertEqual(repo.semantic_search(self.vector, limit=1, model_name='custom-local-model'), [])
        self.assertEqual(repo.client.rpc.call_args.args[1]['embedding_model'], 'custom-local-model')

    def test_rpc_failure_is_not_reported_as_no_matches(self):
        repo = self.repository()
        repo.client.rpc.return_value.execute.side_effect = RuntimeError('offline')
        with self.assertRaises(RuntimeError):
            repo.semantic_search(self.vector)


if __name__ == '__main__':
    unittest.main()
