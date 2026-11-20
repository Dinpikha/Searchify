# Checks extraction, generation, embedding validation, and summary persistence with
# synthetic inputs.
# Covers malformed/incomplete output and stale-file rejection at the pipeline boundaries.

from contextlib import closing
import hashlib
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

# The production environment supplies python-dotenv; tests need no external services.
try:
    import dotenv
except ImportError:
    dotenv = types.ModuleType('dotenv')
    dotenv.load_dotenv = lambda *args: None
    sys.modules['dotenv'] = dotenv

from searchify.ai.embeddings import validate_embedding, Embeddings
from searchify.ai.ollama_client import OllamaClient, OllamaError
from searchify.services.summary_service import SummaryService


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.db = self.root / 'index.db'
        self.content = 'def hello(): return 42'
        (self.root / 'sample.py').write_text(self.content)
        digest = hashlib.sha256(self.content.encode()).hexdigest()
        self.file = dict(id='f', directory_id='d', relative_path='sample.py',
                         filename='sample.py', status='indexed', content_hash=digest, summary_status='pending')
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript('CREATE TABLE directories(id INTEGER PRIMARY KEY, path TEXT);'
                               'CREATE TABLE files(path TEXT, root_id INTEGER, sha256 TEXT);')
            conn.execute('INSERT INTO directories VALUES(1, ?)', (str(self.root),))
            conn.execute('INSERT INTO files VALUES(?, 1, ?)', (str(self.root / 'sample.py'), digest))
        self.files = Mock()
        self.files.get_pending_summaries.return_value = [self.file]
        self.files.get_directory.return_value = {'path': str(self.root)}
        self.files.get_file.return_value = self.file
        self.files.update_summary_status.return_value = [{}]
        self.summaries = Mock()
        self.summaries.upsert_summary.return_value = {'id': 's'}
        self.summarizer = Mock(model_name='qwen2.5-coder:7b')
        self.summarizer.summarize.return_value = 'Defines a greeting helper.'
        self.embeddings = Mock(model_name='embeddinggemma-2:270m')
        self.embeddings.embed.return_value = [0.1] * 768
        self.service = SummaryService(self.files, self.summaries, self.summarizer,
                                      self.embeddings, self.db)

    def test_success_uploads_only_summary_and_vector(self):
        self.assertEqual(self.service.process_pending()['completed'], 1)
        self.embeddings.embed.assert_called_once_with('Defines a greeting helper.')
        self.assertNotIn(self.content, str(self.summaries.mock_calls))
        self.assertEqual(self.files.update_summary_status.call_args.args[1], 'completed')

    def test_stale_local_content_never_reaches_model(self):
        (self.root / 'sample.py').write_text('changed')
        self.assertEqual(self.service.process_pending()['stale'], 1)
        self.summarizer.summarize.assert_not_called()
        self.summaries.upsert_summary.assert_not_called()

    def test_remote_change_during_generation_prevents_save(self):
        self.files.get_file.return_value = dict(self.file, content_hash='changed')
        self.assertEqual(self.service.process_pending()['stale'], 1)
        self.summaries.upsert_summary.assert_not_called()

    def test_failure_is_sanitized_and_retry_can_finish(self):
        self.embeddings.embed.side_effect = RuntimeError(self.content)
        self.assertEqual(self.service.process_pending()['failed'], 1)
        self.assertNotIn(self.content, str(self.files.update_summary_status.call_args))
        self.summaries.upsert_summary.assert_not_called()
        self.embeddings.embed.side_effect = None
        self.assertEqual(self.service.process_pending(retry_failed=True)['completed'], 1)
        self.files.get_pending_summaries.assert_called_with(50, retry_failed=True, include_processing=True)

    def test_lost_claim_is_skipped(self):
        self.files.update_summary_status.return_value = []
        self.assertEqual(self.service.process_pending()['skipped'], 1)
        self.summarizer.summarize.assert_not_called()

    def test_embedding_save_failure_does_not_complete(self):
        self.summaries.save_embedding.side_effect = RuntimeError('failed')
        self.assertEqual(self.service.process_pending()['failed'], 1)
        self.assertEqual(self.files.update_summary_status.call_args.args[1], 'failed')

    def test_embedding_validation(self):
        for vector in ([0.] * 767, [float('nan')] * 768, [True] * 768):
            with self.assertRaises(ValueError):
                validate_embedding(vector)
        self.assertEqual(len(validate_embedding([0.1] * 768)), 768)

    def test_remote_ollama_rejected(self):
        for host in ('https://ollama.com', 'http://192.168.1.1:11434'):
            with self.assertRaises(ValueError):
                OllamaClient(host)

    def test_transient_transport_retry(self):
        from urllib.error import URLError
        client = OllamaClient(retries=1)
        client.opener = Mock()
        client.opener.open.side_effect = URLError('offline')
        with patch('searchify.ai.ollama_client.time.sleep'), self.assertRaises(OllamaError):
            client.post('/api/embed', {'input': 'description'})
        self.assertEqual(client.opener.open.call_count, 2)


class LocalStorageTests(unittest.TestCase):
    def test_index_keeps_metadata_only_and_purges_legacy_content(self):
        import contextlib
        import io
        from searchify import indexer
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder).resolve()
            root = base / 'source'
            root.mkdir()
            canary = 'PRIVATE_SOURCE_CANARY_748206912: implementation body'
            source = root / 'example.py'
            source.write_text(canary)
            db = base / 'index.db'
            with patch.object(indexer, 'DB_DIR', base), patch.object(indexer, 'DB_PATH', db):
                indexer.initialize()
                with closing(indexer.connect()) as conn, conn:
                    conn.execute('INSERT INTO directories(path) VALUES (?)', (str(root),))
                    with contextlib.redirect_stdout(io.StringIO()):
                        indexer.index_directory(conn, 1, root)
                    row = conn.execute('SELECT path, sha256 FROM files').fetchone()
                    self.assertEqual(row['path'], str(source))
                    self.assertEqual(row['sha256'], indexer.calculate_hash(canary))
                    schema = conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
                    self.assertFalse(any('content' in row[0] or 'chunk' in row[0] for row in schema))
                    self.assertNotIn(canary, '\n'.join(conn.iterdump()))
                    # Simulate an index produced by the old caching architecture.
                    conn.execute('CREATE VIRTUAL TABLE file_search USING fts5(path, name, content)')
                    conn.execute('INSERT INTO file_search VALUES (?, ?, ?)', (str(source), source.name, canary))
                indexer.initialize()
                with closing(indexer.connect()) as conn:
                    self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='file_search'").fetchone())
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM files').fetchone()[0], 1)
                for artifact in base.glob('index.db*'):
                    self.assertNotIn(canary.encode(), artifact.read_bytes())


class LocalEligibilityTests(PipelineTests):
    def test_missing_file_does_not_complete_or_upload(self):
        (self.root / 'sample.py').unlink()
        self.assertEqual(self.service.process_pending()['stale'], 1)
        self.files.update_summary_status.assert_not_called()
        self.summarizer.summarize.assert_not_called()
        self.summaries.upsert_summary.assert_not_called()

    def test_empty_local_file_does_not_complete(self):
        (self.root / 'sample.py').write_text('')
        self.assertEqual(self.service.process_pending()['stale'], 1)
        self.summaries.upsert_summary.assert_not_called()

    def test_source_echo_is_not_uploaded(self):
        self.summarizer.summarize.return_value = self.content
        self.assertEqual(self.service.process_pending()['failed'], 1)
        self.summaries.upsert_summary.assert_not_called()

    def test_repository_checks_local_metadata_without_reading_contents(self):
        from importlib import import_module
        auth = types.ModuleType('searchify.database.supabase_client')
        auth.get_client, auth.get_owner_id = Mock(), Mock()
        with patch.dict(sys.modules, {'searchify.database.supabase_client': auth}):
            repository_module = import_module('searchify.repositories.files')
        repository = repository_module.FileRepository.__new__(repository_module.FileRepository)
        repository.db_path, repository.owner_id = self.db, 'owner'
        query = Mock()
        for method in ('select', 'eq', 'in_', 'order', 'range'):
            getattr(query, method).return_value = query
        repository.client = Mock()
        repository.client.table.return_value = query
        cloud_only = dict(self.file, id='cloud', relative_path='missing.py')
        local_metadata = dict(self.file, id='local', status='metadata_only')
        stale_hash = dict(self.file, id='stale', content_hash='old')
        query.execute.return_value = types.SimpleNamespace(data=[cloud_only, stale_hash, local_metadata])
        repository.get_directory = Mock(return_value={'path': str(self.root)})
        with patch('searchify.core.extractor.extract_content', side_effect=AssertionError('must not read yet')):
            self.assertEqual(repository.get_pending_summaries(limit=1), [local_metadata])
        self.assertEqual(repository.client.table.call_args.args, ('files',))
        selected_columns = query.select.call_args.args[0].split(', ')
        self.assertNotIn('content', selected_columns)
        self.assertNotIn('*', selected_columns)

    def test_real_summary_repository_writes_only_derived_data(self):
        from importlib import import_module
        auth = types.ModuleType('searchify.database.supabase_client')
        auth.get_client, auth.get_owner_id = Mock(), Mock()
        with patch.dict(sys.modules, {'searchify.database.supabase_client': auth}):
            module = import_module('searchify.repositories.summaries')
        repo = module.SummaryRepository.__new__(module.SummaryRepository)
        repo.owner_id = 'owner'
        repo.client = Mock()
        query = repo.client.table.return_value
        query.upsert.return_value = query
        query.execute.return_value = types.SimpleNamespace(data=[{'id': 'summary'}])
        self.service.summaries = repo
        self.assertEqual(self.service.process_pending()['completed'], 1)
        tables = [call.args[0] for call in repo.client.table.call_args_list]
        self.assertEqual(tables, ['file_summaries', 'file_summaries', 'file_summary_embeddings'])
        for call in query.upsert.call_args_list:
            payload = call.args[0]
            self.assertFalse({'content', 'chunks', 'extracted_content'} & payload.keys())
            self.assertNotIn(self.content, str(payload))
        with self.assertRaises(ValueError):
            repo.upsert_summary('f', 'hash', 'summary', {'content': self.content}, 'model')


if __name__ == '__main__':
    unittest.main()
