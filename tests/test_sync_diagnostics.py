# Injects transport and pipeline-stage failures to verify retry behavior and sanitized
# diagnostics.
# Captured logs/callbacks must identify failing stages without including source text or
# credentials.

"""Runtime and diagnostics tests: temporary files and mocked providers only."""
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

try:
    import dotenv
except ImportError:
    module = types.ModuleType('dotenv')
    module.load_dotenv = lambda *args: None
    sys.modules['dotenv'] = module

# Avoid authentication, Keychain and external SDK dependencies in unit tests.
auth = types.ModuleType('searchify.supabase_client')
auth.get_client = Mock()
auth.get_owner_id = Mock()
auth.authenticate = Mock()
with patch.dict(sys.modules, {'searchify.supabase_client': auth}):
    from searchify.services.sync_service import SyncService
from searchify.services.summary_service import SummaryService
from searchify.ai.ollama_client import (
    OllamaClient, OllamaError, OllamaUnavailableError, format_processing_error,
    check_processing_runtime,
)
from searchify.indexer import calculate_hash


class RuntimeTests(unittest.TestCase):
    def client(self, retries=0):
        client = OllamaClient(retries=retries)
        client.opener = Mock()
        return client

    def test_connection_failure_is_actionable_and_retried_without_payload(self):
        client = self.client(retries=2)
        client.opener.open.side_effect = URLError('TOKEN_AND_SOURCE_MUST_NOT_ESCAPE')
        with patch('searchify.ai.ollama_client.time.sleep') as sleep:
            with self.assertRaises(OllamaUnavailableError) as caught:
                client.check_available()
        self.assertIn('ollama serve', str(caught.exception))
        self.assertIn('OLLAMA_HOST', str(caught.exception))
        self.assertNotIn('TOKEN_AND_SOURCE', str(caught.exception))
        self.assertEqual(client.opener.open.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        for call in client.opener.open.call_args_list:
            request = call.args[0]
            self.assertEqual(request.get_method(), 'GET')
            self.assertEqual(request.full_url, 'http://localhost:11434/api/tags')
            self.assertIsNone(request.data)
            self.assertEqual(call.kwargs['timeout'], 5)

    def test_healthy_and_malformed_runtime_responses(self):
        for payload, healthy in (({'models': []}, True), ({'models': [], 'error': 'SECRET'}, False),
                                 ({}, False), ([], False)):
            with self.subTest(payload=payload):
                client = self.client()
                client.opener.open.return_value = io.BytesIO(json.dumps(payload).encode())
                if healthy:
                    client.check_available()
                else:
                    with self.assertRaises(OllamaUnavailableError):
                        client.check_available()

    def test_generation_http_failure_retains_status_after_retries(self):
        client = self.client(retries=1)
        error = HTTPError('http://localhost:11434/api/generate', 500,
                          'SOURCE_SECRET', {}, None)
        self.addCleanup(error.close)
        client.opener.open.side_effect = error
        with patch('searchify.ai.ollama_client.time.sleep'), self.assertRaises(OllamaError) as caught:
            client.post('/api/generate', {'prompt': 'PRIVATE_SOURCE'})
        self.assertEqual(client.opener.open.call_count, 2)
        diagnostic = format_processing_error('summarization', caught.exception)
        self.assertIn('500 after retries', diagnostic)
        self.assertNotIn('SOURCE_SECRET', diagnostic)
        self.assertNotIn('PRIVATE_SOURCE', diagnostic)

    def test_shared_client_checked_once(self):
        client = Mock()
        check_processing_runtime(Mock(client=client), Mock(client=client))
        client.check_available.assert_called_once()

    def test_http_error_is_sanitized(self):
        error = HTTPError('https://secret@example.test/?token=SECRET', 401,
                          'source code SECRET', {'Authorization': 'SECRET'}, None)
        diagnostic = format_processing_error('persistence', error)
        self.assertIn('HTTPError', diagnostic)
        self.assertIn('401', diagnostic)
        self.assertNotIn('SECRET', diagnostic)
        self.assertNotIn('example.test', diagnostic)
        error.close()

    def test_database_codes_and_untrusted_error_messages(self):
        class APIError(Exception):
            code = '42501'
        diagnostic = format_processing_error('persistence', APIError('API_KEY=SECRET source text'))
        self.assertIn('APIError', diagnostic)
        self.assertIn('42501', diagnostic)
        self.assertIn('permission denied', diagnostic)
        self.assertNotIn('SECRET', diagnostic)
        self.assertNotIn('source text', diagnostic)
        self.assertEqual(format_processing_error('embedding', OllamaError('Ollama returned invalid JSON.')),
                         'embedding: OllamaError: Ollama returned invalid JSON.')


class FileDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.content = 'SOURCE_CONTENT_CANARY must remain private'
        (self.root / 'file.kt').write_text(self.content)
        self.file = dict(id='f1', file_id='f1', directory_id='d1', filename='file.kt',
                         relative_path='file.kt', content_hash=calculate_hash(self.content),
                         status='indexed', summary_status='pending')
        self.files = Mock(owner_id='owner')
        self.files.get_directory.return_value = {'path': str(self.root)}
        self.files.get_file.return_value = self.file
        self.files.update_summary_status.return_value = [{}]
        self.files.get_pending_summaries.return_value = [self.file]
        self.query = self.files.client.table.return_value
        self.query.update.return_value = self.query
        self.query.eq.return_value = self.query
        self.summaries = Mock()
        self.summaries.upsert_summary.return_value = {'id': 's1'}
        self.summarizer = Mock(model_name='summary-model')
        self.summarizer.summarize.return_value = 'Derived description.'
        self.embeddings = Mock(model_name='embedding-model')
        self.embeddings.embed.return_value = [0.1] * 768
        self.sync = SyncService(files=self.files, summaries=self.summaries,
                                summarizer=self.summarizer, embeddings=self.embeddings,
                                db_path=self.root / 'missing.db')
        self.summary = self.sync.summary_service
        self.summary._read_content = Mock(return_value=self.content)
        self.summary._check_current = Mock()

    def test_empty_work_does_not_require_ollama(self):
        self.files.get_pending_summaries.return_value = []
        self.summarizer.client.check_available.side_effect = AssertionError('unnecessary probe')
        self.assertEqual(self.summary.process_pending()['failed'], 0)
        self.assertEqual(self.sync._process_batch([])['failed'], 0)
        self.summarizer.client.check_available.assert_not_called()

    def test_sync_checks_runtime_before_processing_any_file(self):
        self.summarizer.client.check_available.side_effect = OllamaUnavailableError('Start Ollama: ollama serve')
        progress = Mock()
        with self.assertRaisesRegex(OllamaUnavailableError, 'ollama serve'):
            self.sync._process_batch([self.file], progress)
        progress.assert_not_called()
        self.files.get_directory.assert_not_called()
        self.files.client.table.assert_not_called()
        self.summarizer.summarize.assert_not_called()

    def test_summary_checks_runtime_before_claiming(self):
        self.embeddings.client.check_available.side_effect = OllamaUnavailableError('Start Ollama: ollama serve')
        with self.assertRaises(OllamaUnavailableError):
            self.summary.process_pending()
        self.summary._read_content.assert_not_called()
        self.files.update_summary_status.assert_not_called()

    def test_sync_reports_each_processing_stage_without_secrets(self):
        for stage in ('extraction', 'summarization', 'embedding', 'persistence'):
            with self.subTest(stage=stage):
                targets = {
                    'extraction': self.summary._read_content,
                    'summarization': self.summarizer.summarize,
                    'embedding': self.embeddings.embed,
                    'persistence': self.summaries.upsert_summary,
                }
                target = targets[stage]
                target.side_effect = RuntimeError(self.content + ' API_KEY=KEY TOKEN=TOKEN password=PASSWORD')
                progress = Mock()
                with self.assertLogs('searchify.services.summary_service', level='ERROR') as captured:
                    result = self.sync._process_batch([self.file], progress)
                self.assertEqual(result['failed'], 1)
                payload = self.files.update_summary_status.call_args.kwargs
                diagnostic = payload['error']
                self.assertTrue(diagnostic.startswith(stage + ': RuntimeError:'))
                self.assertEqual(self.files.update_summary_status.call_args.args[1], 'failed')
                progress.assert_any_call(self.sync.report.outcomes[-1]['error']['message'], 'error')
                progress.assert_any_call('file.kt', 'failed')
                emitted = str(payload) + str(captured.output) + str(progress.call_args_list)
                for private in (self.content, 'API_KEY', 'TOKEN', 'PASSWORD'):
                    self.assertNotIn(private, emitted)
                target.side_effect = None

    def test_summary_reports_each_stage_and_retries(self):
        for stage in ('extraction', 'summarization', 'embedding', 'persistence'):
            with self.subTest(stage=stage):
                targets = {'extraction': self.summary._read_content,
                           'summarization': self.summarizer.summarize,
                           'embedding': self.embeddings.embed,
                           'persistence': self.summaries.save_embedding}
                target = targets[stage]
                target.side_effect = ValueError(self.content + ' credential=SECRET')
                with self.assertLogs('searchify.services.summary_service', level='ERROR') as captured:
                    result = self.summary.process_pending(retry_failed=True)
                self.assertEqual(result['failed'], 1)
                call = self.files.update_summary_status.call_args
                self.assertEqual(call.args, ('f1', 'failed'))
                self.assertTrue(call.kwargs['error'].startswith(stage + ': ValueError:'))
                self.assertNotIn(self.content, str(call) + str(captured.output))
                self.assertNotIn('SECRET', str(call) + str(captured.output))
                target.side_effect = None
        result = self.summary.process_pending(retry_failed=True)
        self.assertEqual(result['completed'], 1)
        self.files.get_pending_summaries.assert_called_with(50, retry_failed=True, include_processing=True)

    def test_failure_status_save_error_does_not_stop_next_file(self):
        self.embeddings.embed.side_effect = [RuntimeError('SECRET'), [0.1] * 768]
        self.files.update_summary_status.side_effect = [[{}], RuntimeError('CREDENTIAL'), [{}], [{}]]
        with self.assertLogs('searchify.services.summary_service', level='ERROR') as captured:
            result = self.sync._process_batch([self.file, self.file])
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['completed'], 1)
        self.assertNotIn('SECRET', str(captured.output))
        self.assertNotIn('CREDENTIAL', str(captured.output))


if __name__ == '__main__':
    unittest.main()
