# Offline cross-module regression coverage for indexing, synchronization, and summary
# processing.
# External dependencies are faked or patched so default discovery does not require user
# credentials.

"""Cross-phase regression and performance contracts; no live providers."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from searchify.core.file_registry import FileRegistry
from searchify.core.scanner import Scanner, DirectoryRules
from searchify.core.extractor import extract_content
from searchify.services.sync_service import SyncService
from searchify.cli.app import main
from fakes import Files, Summaries


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve(); self.root=self.base/'source'; self.root.mkdir()
        self.registry=FileRegistry(self.base/'index.db'); self.registry.register(self.root)
        self.source=self.root/'file.kt'; self.source.write_text('fun original() = 1')
        self.files=Files(); self.summaries=Summaries()
        self.summarizer=Mock(model_name='qwen2.5-coder:7b'); self.summarizer.summarize.return_value='Derived helper description.'
        self.embeddings=Mock(model_name='embeddinggemma-2:270m'); self.embeddings.embed.return_value=[0.1]*768
        self.service=SyncService(db_path=self.registry.path,files=self.files,summaries=self.summaries,
            summarizer=self.summarizer,embeddings=self.embeddings,reports_dir=self.base/'reports')

    def test_unchanged_scan_does_not_reextract_or_call_ai_and_repairs_invalid_hash(self):
        with patch('searchify.core.scanner.extract_content',wraps=extract_content) as extractor:
            self.service.sync(); self.assertEqual(extractor.call_count,1)
            self.service.sync(); self.assertEqual(extractor.call_count,1)
            with self.registry.connection(True) as c:
                c.execute('UPDATE sync_state SET content_hash=?',('z'*64,))
                c.execute('UPDATE files SET sha256=?',('z'*64,))
            self.service.sync(); self.assertEqual(extractor.call_count,2)
        self.assertEqual(self.summarizer.summarize.call_count,1)

    def test_nested_registration_does_not_duplicate_processing_or_keep_old_remote_search_record(self):
        child=self.root/'nested'; child.mkdir(); source=child/'inside.kt'; source.write_text('fun inside() = 1')
        self.service.sync(); self.assertEqual(self.summarizer.summarize.call_count,2)
        child_id=self.registry.register(child,DirectoryRules.preset('android'))
        entries,_,_=Scanner(self.registry).scan()
        self.assertEqual(sum(e.get('path')==str(source) for e in entries),1)
        self.assertEqual(next(e['root_id'] for e in entries if e['path']==str(source)),child_id)
        self.service.sync()
        old=[row for row in self.files.rows.values() if row['relative_path']=='nested/inside.kt']
        self.assertTrue(old); self.assertEqual(old[0]['status'],'error')
        self.assertEqual(self.registry.states()[str(source)]['root_id'],child_id)

    def test_source_change_during_generation_cannot_be_completed_and_is_refreshed_next_run(self):
        def changed(text):
            self.source.write_text('fun changed() = 2'); return [0.1]*768
        self.embeddings.embed.side_effect=changed
        result=self.service.sync()
        self.assertEqual(result['stale'],1); self.assertEqual(result['completed'],0)
        detail=result['report']['outcomes'][0]['error']; self.assertEqual(detail['category'],'hash_mismatch')
        self.embeddings.embed.side_effect=None
        self.assertEqual(self.service.sync()['completed'],1)

    def test_failed_embedding_persistence_is_not_completed_and_retry_does_not_touch_other_successes(self):
        (self.root/'second.kt').write_text('fun second() = 2')
        real=self.summaries.save_embedding
        calls=0
        def persist(*args):
            nonlocal calls
            calls+=1
            if calls==2: raise ConnectionError('PRIVATE_PAYLOAD')
            return real(*args)
        self.summaries.save_embedding=persist
        result=self.service.sync(); self.assertEqual(result['completed'],1); self.assertEqual(result['failed'],1)
        self.summaries.save_embedding=real
        self.assertEqual(self.service.sync(retry_failed=True)['completed'],1)
        self.assertEqual(self.summarizer.summarize.call_count,2)

    def test_conditional_claim_loss_never_calls_ai(self):
        with patch.object(self.files,'update_summary_status',return_value=[]): result=self.service.sync()
        self.assertEqual(result['skipped'],1)
        self.summarizer.summarize.assert_not_called()

    def test_cli_sync_dry_run_report_status_review_organize_are_safe_with_temp_workspace(self):
        paths=(self.registry.path,self.base/'reports')
        with patch('searchify.cli.sync.SyncService',create=True):
            pass  # Service is lazily imported; the actual class is patched below.
        with patch('searchify.cli.workspace.local_paths',return_value=paths), \
             patch('searchify.cli.directories.local_paths',return_value=paths), \
             patch('searchify.cli.review.local_paths',return_value=paths), \
             patch('searchify.cli.organize.local_paths',return_value=paths), \
             patch('searchify.services.health_service.HealthService._providers',return_value={'ollama':{'status':'not checked (test)'},'supabase':{'status':'not checked (test)'}}), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['dirs','show',str(self.root)]),0)
            self.assertEqual(main(['status','--offline','--scan']),0)
            self.assertEqual(main(['review','--max-files','10']),0)
            self.assertEqual(main(['organize','--max-files','10']),0)
            with patch('searchify.services.sync_service.SyncService',return_value=self.service):
                self.assertEqual(main(['sync','--dry-run']),0)
            self.assertEqual(main(['report']),0)
        self.assertEqual(self.source.read_text(),'fun original() = 1')
        self.assertFalse((self.base/'reports').exists())

    def test_excluding_and_reenabling_a_directory_reuses_valid_summaries(self):
        folder=self.root/'folder'; folder.mkdir(); target=folder/'nested.kt'; target.write_text('fun nested() = 3')
        self.service.sync(); calls=self.summarizer.summarize.call_count
        root_id=self.registry.directories()[0]['id']
        rules=self.registry.rules(root_id); rules.exclude=['folder/**']; self.registry.save_rules(root_id,rules)
        result=self.service.sync()
        remote=next(row for row in self.files.rows.values() if row['relative_path']=='folder/nested.kt')
        self.assertEqual(remote['status'],'skipped'); self.assertEqual(remote['summary_status'],'completed')
        rules.exclude=[]; self.registry.save_rules(root_id,rules)
        self.service.sync()
        self.assertEqual(self.files.rows[remote['id']]['status'],'indexed')
        self.assertEqual(self.summarizer.summarize.call_count,calls)

    def test_restoring_an_identical_missing_file_reuses_valid_artifacts(self):
        original=self.source.read_bytes(); self.service.sync(); self.source.unlink(); self.service.sync()
        self.source.write_bytes(original); self.service.sync()
        self.assertEqual(self.summarizer.summarize.call_count,1)
        self.assertEqual(next(iter(self.files.rows.values()))['status'],'indexed')

    def test_dry_run_respects_retry_flags_and_never_writes_report_or_metadata(self):
        self.embeddings.embed.side_effect=ConnectionError('PRIVATE')
        self.service.sync(); self.embeddings.embed.side_effect=None
        before=self.registry.path.read_bytes(); reports=len(list((self.base/'reports').iterdir()))
        result=self.service.sync(dry_run=True)
        self.assertEqual(result['report']['outcomes'][0]['reason'],'retry_required')
        result=self.service.sync(dry_run=True,retry_failed=True)
        self.assertEqual(result['report']['outcomes'][0]['reason'],'dry_run_would_process')
        self.assertEqual(before,self.registry.path.read_bytes())
        self.assertEqual(reports,len(list((self.base/'reports').iterdir())))

    def test_reports_inside_registered_root_are_not_summarized(self):
        self.service.reports_dir=self.root/'history'
        self.service.sync(); self.service.sync()
        self.assertEqual(self.summarizer.summarize.call_count,1)
        self.assertEqual(len(self.files.rows),1)

    def test_claim_changed_during_generation_is_reported_accurately(self):
        def stolen(text):
            next(iter(self.files.rows.values()))['summary_updated_at']='other-worker-stamp'
            return [0.1]*768
        self.embeddings.embed.side_effect=stolen
        result=self.service.sync()
        self.assertEqual(result['stale'],1)
        detail=result['report']['outcomes'][0]['error']
        self.assertEqual(detail['category'],'claim_conflict'); self.assertEqual(detail['stage'],'persistence')
        self.assertIn('claim changed',detail['message'])
        self.assertEqual(self.summaries.rows,{})

    def test_sensitive_summary_echo_never_reaches_supabase(self):
        self.summarizer.summarize.return_value='Uses API_KEY=TOPSECRET from the source.'
        result=self.service.sync()
        self.assertEqual(result['failed'],1); self.assertEqual(self.summaries.rows,{})
        self.assertNotIn('TOPSECRET',json.dumps(result['report']))

    def test_permanent_error_requires_an_explicit_override_after_fix(self):
        from searchify.ai.ollama_client import OllamaError
        self.embeddings.embed.side_effect=OllamaError('Ollama HTTP error 404.',http_status=404)
        self.service.sync(); calls=self.summarizer.summarize.call_count
        self.embeddings.embed.side_effect=None
        self.service.sync(retry_failed=True)
        self.assertEqual(self.summarizer.summarize.call_count,calls)
        self.assertEqual(self.service.sync(retry_failed=True,retry_permanent=True)['completed'],1)

    def test_only_unsupported_files_do_not_require_cloud_or_ollama(self):
        self.source.unlink(); (self.root/'photo.png').write_bytes(b'PNG')
        service=SyncService(db_path=self.registry.path,reports_dir=self.base/'reports')
        result=service.sync()
        self.assertEqual(result['skipped'],1); self.assertEqual(result['failed'],0)
        self.assertEqual(result['report']['outcomes'][0]['reason'],'unsupported_file_type')
        self.assertIsNone(service.files); self.assertIsNone(service.summarizer)

    def test_cloud_setup_failure_preserves_completions_and_classifies_pending_files(self):
        self.service.sync(); (self.root/'new.kt').write_text('fun newer() = 9')
        with patch.object(self.files,'list_directories',side_effect=ConnectionError('PRIVATE_TOKEN')):
            with self.assertRaises(ConnectionError): self.service.sync()
        report=self.service.last_result['report']
        self.assertEqual(report['counts']['failed'],1); self.assertEqual(report['counts']['skipped'],1)
        self.assertNotIn('PRIVATE_TOKEN',json.dumps(report))
        self.assertEqual(self.registry.states()[str(self.source)]['status'],'completed')
        self.assertEqual(self.service.sync()['completed'],0)
        self.assertEqual(self.service.sync(retry_failed=True)['completed'],1)

    def test_interrupted_claim_can_resume_after_a_verified_rename(self):
        self.embeddings.embed.side_effect=KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.service.sync()
        renamed=self.root/'renamed.kt'; self.source.rename(renamed)
        self.embeddings.embed.side_effect=None
        result=self.service.sync()
        self.assertEqual(result['completed'],1)
        self.assertEqual(next(iter(self.files.rows.values()))['relative_path'],'renamed.kt')

    def test_secret_directories_and_symlinks_never_reach_extraction(self):
        (self.root/'.aws').mkdir(); (self.root/'.aws'/'credentials.json').write_text('PRIVATE')
        outside=self.base/'outside.kt'; outside.write_text('OUTSIDE_PRIVATE')
        (self.root/'link.kt').symlink_to(outside)
        entries,_,_=Scanner(self.registry).scan()
        self.assertFalse(any('.aws' in e['path'] for e in entries))
        self.assertEqual(next(e['reason'] for e in entries if e['filename']=='link.kt'),'symlink')
