"""Classified failure history, privacy and atomic local persistence."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError

from searchify.reporting.sync_report import SyncReport, describe_error, now, safe_filename
from searchify.ai.ollama_client import OllamaClient, OllamaError


class ReportTests(unittest.TestCase):
    def test_categories_stages_request_ids_and_unknown_causes(self):
        class APIError(Exception): code='42501'
        report=SyncReport()
        errors=[('embedding',HTTPError('http://secret/?token=PRIVATE',429,'PRIVATE',{'x-request-id':'req_abcdefgh1234'},None)),
                ('persistence',APIError('sensitive SQL source')),
                ('summarization',RuntimeError('quota exceeded with PRIVATE source'))]
        for i,(stage,error) in enumerate(errors):
            report.record({'id':str(i),'filename':f'{i}.kt'},'failed',stage,error)
        snapshot=report.snapshot()
        self.assertEqual({group['category'] for group in snapshot['failure_groups']},
                         {'rate_limit','database_permission','unknown'})
        first=snapshot['outcomes'][0]['error']
        self.assertEqual(first['http_status'],429); self.assertTrue(first['retryable'])
        self.assertEqual(first['provider_request_id'],'req_abcdefgh1234')
        self.assertIsNone(snapshot['outcomes'][2]['error']['retryable'])
        self.assertNotIn('PRIVATE',json.dumps(snapshot)); errors[0][1].close()

    def test_one_file_failure_and_history_never_overwrites(self):
        with tempfile.TemporaryDirectory() as folder:
            for _ in range(2):
                report=SyncReport(); report.record({'id':'f','filename':'file.kt'},'failed','embedding',TimeoutError('PRIVATE'))
                report.finished_at=now(); path=report.save(folder)
                saved=json.loads(path.read_text())
                self.assertEqual(saved['counts']['failed'],1)
                self.assertEqual(saved['failure_groups'][0]['filenames'],['file.kt'])
                self.assertNotIn('PRIVATE',path.read_text())
                self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(len(list(Path(folder).glob('sync-*.json'))),2)
            self.assertEqual(len(list(Path(folder).glob('*.tmp'))),0)

    def test_redaction_of_metadata_messages_credentials_and_controls(self):
        with patch.dict('os.environ',{'MY_API_KEY':'SUPER_PRIVATE_KEY_12345'}):
            self.assertEqual(safe_filename('SUPER_PRIVATE_KEY_12345.kt'),'[redacted filename]')
            self.assertEqual(safe_filename('token=verysecret.kt'),'[redacted filename]')
            report=SyncReport()
            report.record({'id':'SUPER_PRIVATE_KEY_12345','filename':'token=verysecret.kt'},'failed','extraction',
                          ValueError('ORIGINAL_SOURCE api_key=SUPER_PRIVATE_KEY_12345'))
            text=json.dumps(report.snapshot())
            for secret in ('SUPER_PRIVATE_KEY_12345','verysecret','ORIGINAL_SOURCE'): self.assertNotIn(secret,text)
            self.assertNotIn('\x1b',safe_filename('a\x1b[31m.kt'))

    def test_ollama_transport_retains_http_request_metadata_without_body(self):
        client=OllamaClient(retries=0); client.opener=Mock()
        error=HTTPError('http://localhost:11434/api/embed',500,'PRIVATE',{'x-request-id':'req_abcdefgh1234'},None)
        self.addCleanup(error.close)
        client.opener.open.side_effect=error
        with self.assertRaises(OllamaError) as caught: client.post('/api/embed',{'input':'ORIGINAL_SOURCE'})
        detail=describe_error('embedding',caught.exception)
        self.assertEqual(detail['http_status'],500)
        self.assertEqual(detail['provider_request_id'],'req_abcdefgh1234')
        self.assertNotIn('PRIVATE',str(detail)); self.assertNotIn('ORIGINAL_SOURCE',str(detail))

    def test_atomic_persistence_does_not_publish_incomplete_json(self):
        with tempfile.TemporaryDirectory() as folder:
            report=SyncReport()
            with patch('searchify.reporting.sync_report.json.dump',side_effect=OSError('PRIVATE')):
                with self.assertRaises(OSError): report.save(folder)
            self.assertEqual(list(Path(folder).iterdir()),[])
