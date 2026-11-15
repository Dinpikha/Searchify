"""Offline E2E: real pipeline/repositories, fake Ollama HTTP and Supabase boundary."""
from copy import deepcopy
import json
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

from pipeline_support import (
    FakeOllama, MemorySupabase, OllamaClient, PipelineWorkspace,
    exercise_lifecycle, repositories, WRITE_FIELDS,
)
from searchify.retrieval.semantic_search import SemanticSearch
from searchify.services.search_service import SearchService
from searchify.intelligence.embeddings import QueryEmbeddings


class OfflinePipelineTests(unittest.TestCase):
    def setUp(self):
        def forbidden(*args, **kwargs):
            raise AssertionError('Offline E2E attempted network or keyring access.')
        for target in ('socket.create_connection','socket.socket.connect','socket.socket.connect_ex'):
            guard=patch(target,side_effect=forbidden); guard.start(); self.addCleanup(guard.stop)
        keyring=ModuleType('keyring')
        keyring.get_password=keyring.set_password=keyring.delete_password=forbidden
        guard=patch.dict(sys.modules,{'keyring':keyring}); guard.start(); self.addCleanup(guard.stop)
        self.cloud=MemorySupabase(); self.http=FakeOllama()
        self.addCleanup(lambda: [error.close() for error in self.http.errors])
        self.ollama=OllamaClient(retries=0); self.ollama.opener=self.http
        self.workspace=PipelineWorkspace(self.cloud,self.cloud.owner,self.ollama)
        self.addCleanup(self.workspace.close)

    def test_full_pipeline_incremental_lifecycle_and_privacy(self):
        exercise_lifecycle(self,self.workspace)
        self.assertEqual(sum(name=='generate' for name,_ in self.http.calls),3)
        from searchify.config.settings import settings
        for endpoint,payload in self.http.calls:
            if endpoint=='generate': self.assertEqual(payload['model'],settings.SUMMARY_MODEL)
            if endpoint=='embed':
                self.assertEqual(payload['model'],settings.EMBEDDING_MODEL)
                self.assertEqual(payload['dimensions'],768)
                self.assertFalse(any(marker in payload['input'] for marker in self.workspace.canaries))
        self.assertEqual(set(self.cloud.store),set(WRITE_FIELDS))
        self.assertTrue(self.cloud.rpc_calls)

    def test_failure_report_and_retry_preserve_completed_work(self):
        w=self.workspace; w.register_and_index(); self.http.fail_embedding_for='weather'
        first=w.sync.sync()
        self.assertEqual(first['completed'],1); self.assertEqual(first['failed'],1)
        failure=next(row for row in first['report']['outcomes'] if row['outcome']=='failed')
        self.assertEqual(failure['filename'],'weather.py')
        self.assertEqual(failure['error']['stage'],'embedding')
        self.assertEqual(failure['error']['exception_type'],'OllamaError')
        self.assertEqual(failure['error']['http_status'],429)
        self.assertEqual(failure['error']['provider_request_id'],'req_e2e_request_429')
        self.assertTrue(failure['error']['retryable'])
        self.assertEqual(json.loads(next((w.base/'reports').glob('*.json')).read_text())['counts']['failed'],1)
        completed=next(deepcopy(row) for row in self.cloud.store['files'] if row['summary_status']=='completed')
        before=sum(endpoint=='generate' for endpoint,_ in self.http.calls)
        self.assertEqual(w.sync.sync()['completed'],0)
        self.http.fail_embedding_for=None
        self.assertEqual(w.sync.sync(retry_failed=True)['completed'],1)
        self.assertEqual(w.files.get_file(completed['id'])['summary_updated_at'],completed['summary_updated_at'])
        self.assertEqual(sum(endpoint=='generate' for endpoint,_ in self.http.calls),before+1)
        self.assertEqual(len(w.search_without_source_reads('invoice billing')),2)
        w.assert_private(self)

    def test_rpc_contract_isolates_owners_and_filters_incompatible_artifacts(self):
        w=self.workspace; w.register_and_index(); w.sync.sync()
        peer=MemorySupabase(store=self.cloud.store,mutex=self.cloud.mutex)
        _,summaries=repositories(peer,peer.owner,w.registry.path)
        peer_search=SearchService(QueryEmbeddings(self.ollama),SemanticSearch(summaries))
        self.assertEqual(peer_search.search('invoice'),[])
        mine=w.search_without_source_reads('invoice',limit=1)[0]
        # A peer's otherwise identical artifacts must not enter this owner's result set.
        directory=peer.table('directories').upsert(dict(owner_id=peer.owner,path='/isolated/peer'),on_conflict='owner_id,path').execute().data[0]
        file=peer.table('files').upsert(dict(owner_id=peer.owner,directory_id=directory['id'],relative_path='peer.py',filename='peer.py',
            content_hash='synthetic-hash',status='indexed',summary_status='completed'),on_conflict='directory_id,relative_path').execute().data[0]
        summary=summaries.upsert_summary(file['id'],'synthetic-hash','Calculates invoice amounts.',{},w.summarizer.model_name)
        summaries.save_embedding(summary['id'],[1.]+[0.]*767,w.embeddings.model_name)
        self.assertEqual({row['file_id'] for row in peer_search.search('invoice')},{file['id']})
        self.assertNotIn(file['id'],{row['file_id'] for row in w.search_without_source_reads('invoice')})
        target=next(row for row in self.cloud.store['files'] if row['id']==mine['file_id'])
        target['content_hash']='new-hash-without-new-summary'
        self.assertNotIn(target['id'],{row['file_id'] for row in w.search_without_source_reads('invoice')})
        target['content_hash']=w.registry.metadata()[str(w.root/'invoice.py')]['sha256']
        vector=next(row for row in self.cloud.store['file_summary_embeddings'] if row['summary_id']==mine['summary_id'])
        vector['model_name']='incompatible-test-model'
        self.assertNotIn(target['id'],{row['file_id'] for row in w.search_without_source_reads('invoice')})

    def test_privacy_boundary_rejects_chunk_writes_and_source_echoes(self):
        for table,payload in (
            ('file_chunks',{'content':'synthetic'}),
            ('files',{'content':'synthetic'}),
            ('file_summaries',{'summary':self.workspace.canaries[0]}),
        ):
            with self.assertRaises(AssertionError):
                self.workspace.audit.inspect(table,payload)
        self.assertFalse(self.workspace.audit.writes)

    def test_persistence_failure_recovers_via_standalone_without_regenerating_summary(self):
        class SyntheticPostgrestError(Exception):
            code='42501'
        w=self.workspace; w.register_and_index()
        self.cloud.failures.append(('file_summary_embeddings','upsert',
            SyntheticPostgrestError({'code':'42501','message':w.sources[0]})))
        first=w.sync.sync()
        self.assertEqual(first['completed'],1); self.assertEqual(first['failed'],1)
        error=next(row['error'] for row in first['report']['outcomes'] if row['outcome']=='failed')
        self.assertEqual(error['stage'],'persistence')
        self.assertEqual(error['category'],'database_permission')
        self.assertEqual(error['exception_type'],'SyntheticPostgrestError')
        self.assertFalse(error['retryable'])
        completed=next(deepcopy(row) for row in self.cloud.store['files'] if row['summary_status']=='completed')
        generated=sum(endpoint=='generate' for endpoint,_ in self.http.calls)
        service=w.sync.summary_service
        self.assertEqual(service.process_pending(retry_failed=True)['completed'],0)
        result=service.process_pending(retry_failed=True,retry_permanent=True)
        self.assertEqual(result['completed'],1)
        self.assertEqual(sum(endpoint=='generate' for endpoint,_ in self.http.calls),generated)
        self.assertEqual(w.files.get_file(completed['id'])['summary_updated_at'],completed['summary_updated_at'])
        self.assertTrue(all(row['status']=='completed' for row in w.registry.states().values()
                            if row['path'].endswith('.py')))
        self.assertEqual(len(w.search_without_source_reads('invoice')),2)
        w.assert_private(self)

    def test_multichunk_summary_processing_never_persists_source_chunks(self):
        w=self.workspace
        expanded=w.sources[0]+''.join('# synthetic explanatory source line number '+str(i)+'\n' for i in range(600))
        w.sources.append(expanded)
        (w.root/'invoice.py').write_text(expanded)
        w.register_and_index()
        self.assertEqual(w.sync.sync()['completed'],2)
        generated=[payload for endpoint,payload in self.http.calls if endpoint=='generate']
        self.assertGreater(len(generated),3)
        for payload in generated:
            self.assertLessEqual(len(payload['prompt'].split('Input:\n',1)[1]),12000)
        self.assertFalse({'file_chunks','file_embeddings'} & {table for table,_ in w.audit.writes})
        w.assert_private(self)
