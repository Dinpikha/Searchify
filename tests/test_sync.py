# Exercises incremental SyncService runs on temporary roots with in-memory Files/Summaries
# repositories.
# Repeated runs verify reuse, retries, deletion/rename handling, locking, and interruption-
# safe progress.

"""Filesystem-to-summary integration tests with local temporary metadata only."""
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from searchify.core.file_registry import FileRegistry, RegistryBusy
from searchify.core.scanner import Scanner
from searchify.services.sync_service import SyncService
from searchify.ai.ollama_client import OllamaUnavailableError
from fakes import Files, Summaries


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve(); self.root=self.base/'source'; self.root.mkdir()
        self.registry=FileRegistry(self.base/'index.db'); self.registry.register(self.root)
        self.source=self.root/'file.kt'; self.source.write_text('fun hello() = 42')
        self.files=Files(); self.summaries=Mock(wraps=Summaries())
        self.summarizer=Mock(model_name='qwen2.5-coder:7b'); self.summarizer.summarize.return_value='Defines a helper function.'
        self.embeddings=Mock(model_name='embeddinggemma-2:270m'); self.embeddings.embed.return_value=[0.1]*768
        self.service=SyncService(db_path=self.registry.path,files=self.files,summaries=self.summaries,
            summarizer=self.summarizer,embeddings=self.embeddings,reports_dir=self.base/'reports')

    def test_new_unchanged_modified_and_legacy_hash_repair(self):
        first=self.service.sync(); self.assertEqual(first['completed'],1)
        second=self.service.sync(); self.assertEqual(second['completed'],0)
        self.assertEqual(self.summarizer.summarize.call_count,1)
        old=self.source.stat(); self.source.write_text('fun hello() = 43')
        os.utime(self.source, ns=(old.st_atime_ns,old.st_mtime_ns))
        third=self.service.sync(); self.assertEqual(third['completed'],1)
        self.assertEqual(self.summarizer.summarize.call_count,2)
        with self.registry.connection(True) as c:
            c.execute('UPDATE files SET sha256=?',(hashlib.sha256(b'').hexdigest(),))
            c.execute('DELETE FROM sync_state')
        repaired=self.service.sync()
        self.assertEqual(self.registry.metadata()[str(self.source)]['sha256'],hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(self.summarizer.summarize.call_count,2)

    def test_unsupported_empty_binary_and_secret_are_skipped(self):
        for name, data in [('photo.png',b'png'),('binary.kt',b'abc\0xyz'),('empty.xml',b''),('.env',b'API_KEY=PRIVATE')]:
            (self.root/name).write_bytes(data)
        result=self.service.sync(); self.assertEqual(result['completed'],1); self.assertEqual(result['skipped'],4)
        self.assertEqual(self.summarizer.summarize.call_count,1)
        self.service.sync(); self.assertEqual(self.summarizer.summarize.call_count,1)
        self.assertTrue(all(record['extension']=='.kt' for record in self.files.payloads))

    def test_failed_retries_are_opt_in_and_known_permanent_errors_are_not_retried(self):
        self.embeddings.embed.side_effect=ConnectionError('SECRET')
        self.assertEqual(self.service.sync()['failed'],1)
        self.embeddings.embed.side_effect=None
        self.assertEqual(self.service.sync()['completed'],0)
        self.assertEqual(self.service.sync(retry_failed=True)['completed'],1)

    def test_deleted_and_moved_files_do_not_trigger_unnecessary_ai(self):
        self.service.sync(); remote_id=next(iter(self.files.rows))
        new=self.root/'renamed.kt'; self.source.rename(new)
        self.service.sync()
        self.assertEqual(self.files.rows[remote_id]['relative_path'],'renamed.kt')
        self.assertEqual(self.summarizer.summarize.call_count,1)
        new.unlink(); report=self.service.sync()
        self.assertEqual(self.files.rows[remote_id]['status'],'error')
        self.assertEqual(report['stale'],1)
        self.service.sync(); self.assertEqual(self.summarizer.summarize.call_count,1)

    def test_dry_run_does_not_write_or_construct_network_providers(self):
        before=self.registry.path.read_bytes()
        service=SyncService(db_path=self.registry.path,reports_dir=self.base/'dry-reports')
        result=service.sync(dry_run=True)
        self.assertEqual(result['report']['remaining'],1)
        self.assertEqual(self.registry.path.read_bytes(),before)
        self.assertFalse((self.base/'dry-reports').exists())
        self.assertIsNone(service.files); self.assertIsNone(service.summarizer)

    def test_lock_prevents_duplicate_processing(self):
        with self.registry.lock(), self.assertRaises(RegistryBusy):
            self.service.sync()
        self.summarizer.summarize.assert_not_called()

    def test_progress_success_survives_later_failure_and_interruption(self):
        (self.root/'second.kt').write_text('fun second() = 2')
        self.embeddings.embed.side_effect=[[0.1]*768, KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt): self.service.sync()
        self.assertEqual(sum(row['summary_status']=='completed' for row in self.files.rows.values()),1)
        self.embeddings.embed.side_effect=None
        self.assertEqual(self.service.sync()['completed'],1)
        self.assertEqual(sum(row['summary_status']=='completed' for row in self.files.rows.values()),2)

    def test_unavailable_ollama_records_failures_without_downgrading_completed_files(self):
        self.service.sync()
        (self.root/'second.kt').write_text('fun second() = 2')
        self.summarizer.client.check_available.side_effect=OllamaUnavailableError('Start Ollama.')
        with self.assertRaises(OllamaUnavailableError): self.service.sync()
        self.assertEqual(self.service.last_result['failed'],1)
        self.assertEqual(sum(row['summary_status']=='completed' for row in self.files.rows.values()),1)

    def test_every_run_report_and_source_privacy(self):
        result=self.service.sync()
        self.service.sync()
        paths=list((self.base/'reports').glob('*.json')); self.assertEqual(len(paths),2)
        canary=self.source.read_text()
        for path in paths: self.assertNotIn(canary,path.read_text())
        with self.registry.connection() as c: self.assertNotIn(canary,'\n'.join(c.iterdump()))
        self.assertNotIn(canary,str(self.files.payloads))
