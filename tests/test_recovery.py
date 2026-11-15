"""Crash, lease and retry regressions. Temporary roots and fake providers only."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fakes import Files, Summaries
from test_repository_sync import repository
from searchify.core.file_registry import FileRegistry, RegistryBusy
from searchify.core.scanner import Scanner, DirectoryRules
from searchify.services.processing_claim import ProcessingClaim, recover_claim, expired
from searchify.services.summary_service import SummaryService
from searchify.services.sync_service import SyncService


class Crash(BaseException):
    """Simulate abrupt termination without a graceful interruption handler."""


class Query:
    """Read-only metadata query fixture for the actual repository selector."""
    def __init__(self, files):
        self.files = files; self.filters = []; self.bounds = (0, 99999)
    def select(self, *args, **kwargs): return self
    def order(self, *args): return self
    def eq(self, key, value):
        self.filters.append(lambda row: row.get(key)==value); return self
    def in_(self, key, values):
        self.filters.append(lambda row: row.get(key) in values); return self
    def range(self, start, end):
        self.bounds = (start, end); return self
    def execute(self):
        with self.files.mutex:
            rows = [deepcopy(row) for row in self.files.rows.values() if all(check(row) for check in self.filters)]
        start, end = self.bounds
        return SimpleNamespace(data=sorted(rows, key=lambda row: row['id'])[start:end+1])


class AtomicFiles(Files):
    def __init__(self):
        super().__init__(); self.mutex = threading.RLock()
    def update_summary_status(self, *args, **kwargs):
        with self.mutex:
            return super().update_summary_status(*args, **kwargs)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base/'source'; self.root.mkdir()
        self.source = self.root/'file.kt'; self.source.write_text('fun calculate() = 42')
        self.registry = FileRegistry(self.base/'index.db'); self.registry.register(self.root)
        entries, _, _ = Scanner(self.registry).scan(); self.registry.refresh(entries)
        self.entry = entries[0]
        self.files = AtomicFiles(); directory = self.files.ensure_directory(self.root)
        self.file = self.files.upsert_metadata([dict(owner_id=self.files.owner_id, directory_id=directory['id'],
            relative_path='file.kt', filename='file.kt', content_hash=self.entry['content_hash'],
            status='indexed', summary_status='pending')])[0]
        self.summaries = Summaries()
        self.summarizer = Mock(model_name='qwen2.5-coder:7b')
        self.summarizer.summarize.return_value = 'Describes a calculation helper.'
        self.embeddings = Mock(model_name='embeddinggemma-2:270m'); self.embeddings.embed.return_value = [0.1]*768
        self.service = self.summary_service(self.registry)

    def summary_service(self, registry):
        repo, _ = repository(); repo.db_path = registry.path
        repo.get_directory = self.files.get_directory
        repo.client.table.side_effect = lambda table: Query(self.files)
        # Use actual eligibility/pagination logic, fake owner-scoped cloud records.
        repo.owner_id = self.files.owner_id
        proxy = SimpleNamespace(get_pending_summaries=repo.get_pending_summaries,
            get_directory=self.files.get_directory, get_file=self.files.get_file,
            update_summary_status=self.files.update_summary_status)
        return SummaryService(proxy, self.summaries, self.summarizer, self.embeddings, registry.path)

    def sync_service(self, registry=None):
        return SyncService(db_path=(registry or self.registry).path, files=self.files, summaries=self.summaries,
            summarizer=self.summarizer, embeddings=self.embeddings, reports_dir=self.base/'reports')

    def remote(self): return next(iter(self.files.rows.values()))

    def interrupted(self):
        self.embeddings.embed.side_effect = Crash()
        with self.assertRaises(Crash): self.service.process_pending()
        self.embeddings.embed.side_effect = None
        return deepcopy(self.remote())

    def test_crash_is_worker_owned_and_standalone_recovers_idempotently(self):
        row = self.interrupted()
        state = self.registry.states()[str(self.source)]
        self.assertEqual(state['status'], 'processing')
        self.assertTrue(state['claim_owner'])
        self.assertEqual(state['claim_timestamp'], row['summary_updated_at'])
        self.assertEqual(self.service.process_pending()['completed'], 1)
        self.assertEqual(self.service.process_pending()['completed'], 0)
        self.assertEqual(self.remote()['summary_status'], 'completed')
        self.assertIsNone(self.registry.states()[str(self.source)]['claim_owner'])

    def test_standalone_crash_can_be_recovered_by_sync(self):
        self.interrupted()
        self.assertEqual(self.sync_service().sync()['completed'], 1)
        calls = self.summarizer.summarize.call_count
        self.assertEqual(self.service.process_pending(retry_failed=True)['completed'], 0)
        self.assertEqual(self.summarizer.summarize.call_count, calls)

    def test_sync_crash_can_be_recovered_by_standalone(self):
        self.embeddings.embed.side_effect = Crash()
        with self.assertRaises(Crash): self.sync_service().sync()
        self.assertEqual(self.registry.states()[str(self.source)]['status'], 'processing')
        self.embeddings.embed.side_effect = None
        self.assertEqual(self.service.process_pending()['completed'], 1)

    def test_expired_foreign_claim_is_recovered_but_fresh_claim_is_untouched(self):
        self.remote().update(summary_status='processing', summary_updated_at=datetime.now(timezone.utc).isoformat())
        token = self.remote()['summary_updated_at']
        self.assertEqual(self.service.process_pending(recover_processing=True)['completed'], 0)
        self.assertEqual(self.remote()['summary_updated_at'], token)
        self.remote()['summary_updated_at'] = (datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()
        self.assertEqual(self.service.process_pending(recover_processing=True)['completed'], 1)

    def test_recovery_requires_matching_owner_journal_token(self):
        self.interrupted()
        self.remote()['summary_updated_at'] = datetime.now(timezone.utc).isoformat()
        token = self.remote()['summary_updated_at']
        self.assertEqual(self.service.process_pending(recover_processing=True)['completed'], 0)
        self.assertEqual(self.remote()['summary_updated_at'], token)

    def test_recovery_cannot_reset_a_renewed_claim(self):
        self.remote().update(summary_status='processing', summary_updated_at=(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat())
        snapshot = deepcopy(self.remote())
        self.remote()['summary_updated_at'] = datetime.now(timezone.utc).isoformat()
        with self.registry.lock():
            self.assertIsNone(recover_claim(self.files, self.registry, snapshot))
        self.assertEqual(self.remote()['summary_status'], 'processing')

    def test_recovery_never_resets_unregistered_excluded_or_missing_files(self):
        self.remote().update(summary_status='processing', summary_updated_at=(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat())
        snapshot = deepcopy(self.remote())
        root_id = self.registry.directories()[0]['id']
        rules = self.registry.rules(root_id); rules.include = ['*.java']; self.registry.save_rules(root_id, rules)
        with self.registry.lock(): self.assertIsNone(recover_claim(self.files, self.registry, snapshot))
        rules.include = ['*']; self.registry.save_rules(root_id, rules)
        self.source.unlink()
        with self.registry.lock(): self.assertIsNone(recover_claim(self.files, self.registry, snapshot))
        self.registry.unregister(self.root)
        with self.registry.lock(): self.assertIsNone(recover_claim(self.files, self.registry, snapshot))
        self.assertEqual(self.remote(), snapshot)

    def test_disabled_directory_and_replaced_root_cannot_be_recovered(self):
        self.remote().update(summary_status='processing', summary_updated_at=(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat())
        snapshot = deepcopy(self.remote())
        directory = next(iter(self.files.directories.values())); directory['enabled'] = False
        with self.registry.lock(): self.assertIsNone(recover_claim(self.files, self.registry, snapshot))
        directory['enabled'] = True
        moved = self.base/'moved'; self.root.rename(moved); self.root.symlink_to(moved, target_is_directory=True)
        with self.registry.lock(): self.assertIsNone(recover_claim(self.files, self.registry, snapshot))
        self.assertEqual(self.remote(), snapshot)

    def test_active_workspace_lock_blocks_sync_and_summarize(self):
        with self.registry.lock():
            with self.assertRaises(RegistryBusy): self.service.process_pending(recover_processing=True)
            with self.assertRaises(RegistryBusy): self.sync_service().sync()
        self.assertEqual(self.remote()['summary_status'], 'pending')
        self.summarizer.summarize.assert_not_called()

    def test_concurrent_workers_with_separate_journals_do_not_duplicate_ai(self):
        other = FileRegistry(self.base/'other.db'); other.register(self.root)
        entries, _, _ = Scanner(other).scan(); other.refresh(entries)
        started = threading.Event(); release = threading.Event(); results = []; errors = []
        def embed(summary):
            started.set()
            if not release.wait(5): raise RuntimeError('test timed out')
            return [0.1]*768
        self.embeddings.embed.side_effect = embed
        def worker():
            try: results.append(self.service.process_pending())
            except BaseException as error: errors.append(error)
        thread = threading.Thread(target=worker); thread.start()
        try:
            self.assertTrue(started.wait(3))
            self.assertEqual(self.summary_service(other).process_pending(recover_processing=True)['completed'], 0)
            self.assertEqual(self.sync_service(other).sync(retry_failed=True)['completed'], 0)
            self.assertEqual(self.summarizer.summarize.call_count, 1)
        finally:
            release.set(); thread.join(5)
        self.assertFalse(thread.is_alive()); self.assertFalse(errors)
        self.assertEqual(results[0]['completed'], 1)

    def test_heartbeat_renews_token_and_is_worker_owned(self):
        file = deepcopy(self.file); captured = []; renewed = threading.Event()
        def journal(status, claim):
            captured.append((claim.owner, claim.file['claim_timestamp']))
            if len(captured)>1: renewed.set()
        with patch('searchify.services.processing_claim.HEARTBEAT_SECONDS', 0.01):
            claim = ProcessingClaim(self.files, file, journal)
            try:
                self.assertTrue(claim.acquire()); self.assertTrue(renewed.wait(2))
                self.assertEqual(captured[0][0], captured[1][0])
                self.assertNotEqual(captured[0][1], captured[1][1])
                self.assertFalse(expired(self.remote()['summary_updated_at']))
                loser = ProcessingClaim(self.files, deepcopy(self.file), lambda *args: None)
                self.assertFalse(loser.acquire()); self.assertNotEqual(loser.owner, claim.owner)
            finally: claim.close()

    def test_interruption_releases_only_owned_claim_and_preserves_completions(self):
        self.embeddings.embed.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.service.process_pending()
        self.assertEqual(self.remote()['summary_status'], 'pending')
        self.embeddings.embed.side_effect = None
        self.assertEqual(self.service.process_pending()['completed'], 1)
        calls = self.summarizer.summarize.call_count
        self.assertEqual(self.service.process_pending(retry_failed=True, recover_processing=True)['completed'], 0)
        self.assertEqual(self.summarizer.summarize.call_count, calls)

    def test_retries_honor_permanent_errors_and_reuse_successful_summary(self):
        real = self.summaries.save_embedding
        self.summaries.save_embedding = Mock(side_effect=ConnectionError('fixture'))
        self.assertEqual(self.service.process_pending()['failed'], 1)
        calls = self.summarizer.summarize.call_count
        self.assertEqual(self.service.process_pending()['completed'], 0)
        self.summaries.save_embedding = real
        self.assertEqual(self.service.process_pending(retry_failed=True)['completed'], 1)
        self.assertEqual(self.summarizer.summarize.call_count, calls)
        self.assertEqual(self.registry.states()[str(self.source)]['status'], 'completed')

    def test_nonretryable_errors_require_explicit_override_in_both_commands(self):
        from searchify.ai.ollama_client import OllamaError
        self.embeddings.embed.side_effect = OllamaError('Ollama HTTP error 404.', http_status=404)
        self.assertEqual(self.service.process_pending()['failed'], 1)
        self.embeddings.embed.side_effect = None
        self.assertEqual(self.service.process_pending(retry_failed=True)['completed'], 0)
        self.assertEqual(self.sync_service().sync(retry_failed=True)['completed'], 0)
        self.assertEqual(self.service.process_pending(retry_failed=True, retry_permanent=True)['completed'], 1)

    def test_expiration_rejects_missing_invalid_and_naive_tokens(self):
        for token in (None, '', 'broken', '2000-01-01T00:00:00'):
            self.assertFalse(expired(token))

    def test_metadata_refresh_cannot_overwrite_a_concurrent_claim(self):
        self.source.write_text('fun changed() = 100')
        original = self.files.upsert_metadata
        def upsert(records):
            self.files.update_summary_status(self.file['id'], 'processing',
                content_hash=self.file['content_hash'], expected_status='pending')
            return original(records)
        with patch.object(self.files, 'upsert_metadata', side_effect=upsert):
            result = self.sync_service().sync()
        self.assertEqual(result['completed'], 0)
        self.assertEqual(self.remote()['summary_status'], 'processing')
        self.assertEqual(self.remote()['content_hash'], self.file['content_hash'])
        self.summarizer.summarize.assert_not_called()

    def test_failed_status_persistence_retains_interrupted_recovery_journal(self):
        original = self.service.files.update_summary_status
        def update(identifier, status, **kwargs):
            if status=='failed': raise ConnectionError('fixture')
            return original(identifier, status, **kwargs)
        self.embeddings.embed.side_effect = ConnectionError('fixture')
        with patch.object(self.service.files, 'update_summary_status', side_effect=update):
            self.assertEqual(self.service.process_pending()['failed'], 1)
        state = self.registry.states()[str(self.source)]
        self.assertEqual(state['status'], 'processing'); self.assertTrue(state['claim_owner'])
        self.embeddings.embed.side_effect = None
        self.assertEqual(self.service.process_pending()['completed'], 1)

    def test_completed_remote_record_survives_a_final_journal_write_failure(self):
        original = self.service._journal
        def journal(file, status, *args, **kwargs):
            if status=='completed': raise OSError('fixture')
            return original(file, status, *args, **kwargs)
        with patch.object(self.service, '_journal', side_effect=journal):
            counts = self.service.process_pending()
        self.assertEqual(counts['completed'], 1); self.assertEqual(counts['journal_errors'], 1)
        self.assertEqual(self.remote()['summary_status'], 'completed')
        calls = self.summarizer.summarize.call_count
        self.assertEqual(self.sync_service().sync()['completed'], 0)
        self.assertEqual(self.registry.states()[str(self.source)]['status'], 'completed')
        self.assertEqual(self.summarizer.summarize.call_count, calls)

    def test_legacy_journal_without_owner_does_not_authorize_a_fresh_takeover(self):
        row = self.interrupted()
        with self.registry.connection(True) as connection:
            connection.execute('UPDATE sync_state SET claim_owner=NULL')
        self.assertEqual(self.service.process_pending(recover_processing=True)['completed'], 0)
        self.assertEqual(self.remote(), row)

    def test_journal_migration_is_idempotent_and_preserves_completed_records(self):
        self.service.process_pending()
        with self.registry.connection(True) as connection:
            connection.execute('ALTER TABLE sync_state DROP COLUMN claim_owner')
        self.registry.initialize_journal(); self.registry.initialize_journal()
        self.assertEqual(self.registry.states()[str(self.source)]['status'], 'completed')
        self.assertIsNone(self.registry.states()[str(self.source)]['claim_owner'])

    def test_heartbeat_failure_prevents_summary_persistence(self):
        failed = threading.Event()
        original = self.service.files.update_summary_status
        def update(identifier, status, **kwargs):
            if status=='processing' and kwargs.get('expected_status')=='processing':
                failed.set(); raise ConnectionError('fixture')
            return original(identifier, status, **kwargs)
        def embed(summary):
            if not failed.wait(2): raise RuntimeError('test timed out')
            return [0.1]*768
        self.embeddings.embed.side_effect = embed
        with patch.object(self.service.files, 'update_summary_status', side_effect=update), \
             patch('searchify.services.processing_claim.HEARTBEAT_SECONDS', 0.01):
            result = self.service.process_pending()
        self.assertEqual(result['completed'], 0)
        self.assertFalse(self.summaries.rows)
        self.assertEqual(self.remote()['summary_status'], 'processing')

    def test_retry_selection_skips_failed_files_that_are_no_longer_eligible(self):
        self.embeddings.embed.side_effect = ConnectionError('fixture')
        self.service.process_pending()
        self.embeddings.embed.side_effect = None
        root_id = self.registry.directories()[0]['id']
        rules = self.registry.rules(root_id); rules.exclude = ['*.kt']
        self.registry.save_rules(root_id, rules)
        calls = self.summarizer.summarize.call_count
        self.assertEqual(self.service.process_pending(retry_failed=True)['completed'], 0)
        self.assertEqual(self.summarizer.summarize.call_count, calls)
        self.assertEqual(self.remote()['summary_status'], 'failed')

    def test_standalone_retry_preserves_a_completed_siblings_timestamp(self):
        second = self.root/'second.kt'; second.write_text('fun second() = 2')
        entries, _, _ = Scanner(self.registry).scan(); self.registry.refresh(entries)
        entry = next(row for row in entries if row['filename']=='second.kt')
        self.files.upsert_metadata([dict(owner_id=self.files.owner_id, directory_id=self.file['directory_id'],
            relative_path='second.kt', filename='second.kt', content_hash=entry['content_hash'],
            status='indexed', summary_status='pending')])
        self.embeddings.embed.side_effect = [[0.1]*768, ConnectionError('fixture')]
        result = self.service.process_pending()
        self.assertEqual(result['completed'], 1); self.assertEqual(result['failed'], 1)
        completed = next(deepcopy(row) for row in self.files.rows.values() if row['summary_status']=='completed')
        self.embeddings.embed.side_effect = None
        self.assertEqual(self.service.process_pending(retry_failed=True)['completed'], 1)
        self.assertEqual(self.files.rows[completed['id']], completed)

    def test_repository_recovery_never_updates_cloud_only_records(self):
        self.remote().update(summary_status='processing', summary_updated_at=(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat())
        outside = self.base/'outside'; outside.mkdir(); (outside/'file.kt').write_text('fun external() = 9')
        directory = self.files.ensure_directory(outside)
        cloud = self.files.upsert_metadata([dict(owner_id=self.files.owner_id, directory_id=directory['id'],
            relative_path='file.kt', filename='file.kt', content_hash=self.file['content_hash'],
            status='indexed', summary_status='processing', summary_updated_at=self.remote()['summary_updated_at'])])[0]
        repo, _ = repository(); repo.owner_id=self.files.owner_id; repo.db_path=self.registry.path
        repo.get_directory=self.files.get_directory
        repo.client.table.side_effect=lambda table: Query(self.files)
        repo.update_summary_status=self.files.update_summary_status
        self.assertEqual(len(repo.recover_processing()), 1)
        self.assertEqual(self.files.rows[cloud['id']], cloud)
        self.assertEqual(self.files.rows[self.file['id']]['summary_status'], 'pending')

    def test_recovery_cannot_reset_a_file_that_became_ineligible(self):
        self.remote().update(summary_status='processing', summary_updated_at=(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat())
        snapshot = deepcopy(self.remote())
        self.remote()['status'] = 'skipped'
        with self.registry.lock():
            self.assertIsNone(recover_claim(self.files, self.registry, snapshot))
        self.assertEqual(self.remote()['summary_status'], 'processing')
        self.assertEqual(self.remote()['summary_updated_at'], snapshot['summary_updated_at'])
