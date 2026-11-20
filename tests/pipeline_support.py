# Offline/live acceptance harness sharing production repositories and AI clients.
# Audited wrappers enforce allowed persistence fields and canary exclusion.
# In-memory queries and fake Ollama isolate offline runs; PipelineWorkspace owns
# disposable source/index/report files and lifecycle assertions shared with live tests.

"""Shared E2E fixtures. No default authentication, keyring or live service access."""
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import importlib
import io
import json
import math
from pathlib import Path
import tempfile
import threading
from types import ModuleType, SimpleNamespace
import uuid
from unittest.mock import patch

from searchify.ai.ollama_client import OllamaClient
from searchify.ai.summarizer import Summarizer
from searchify.ai.embeddings import Embeddings
from searchify.config.settings import settings
from searchify.core.file_registry import FileRegistry
from searchify.core.scanner import Scanner, DirectoryRules
from searchify.intelligence.embeddings import QueryEmbeddings
from searchify.retrieval.semantic_search import SemanticSearch
from searchify.services.search_service import SearchService
from searchify.services.sync_service import SyncService

# Strict write contract: a privacy regression fails before reaching a live DB.
WRITE_FIELDS = {
    'directories': {'id','owner_id','path','name','enabled'},
    'files': {'id','owner_id','directory_id','relative_path','filename','extension',
              'size_bytes','modified_ns','content_hash','status','summary_status',
              'summary_error','summary_updated_at','error_message'},
    'file_summaries': {'owner_id','file_id','source_content_hash','summary','summary_data',
                       'model_name','summary_version','generated_at','updated_at'},
    'file_summary_embeddings': {'owner_id','summary_id','embedding','model_name'},
}


# Inject the supplied owner/client into real repositories while explicitly forbidding
# application authentication.
def repositories(client, owner, db):
    # Import the actual repositories without importing application authentication.
    auth = ModuleType('searchify.database.supabase_client')
    def forbidden():
        raise AssertionError('Application credentials must never be used by E2E tests.')
    auth.get_client = auth.get_owner_id = forbidden
    import sys
    with patch.dict(sys.modules, {'searchify.database.supabase_client': auth}):
        files_type = importlib.import_module('searchify.repositories.files').FileRepository
        summaries_type = importlib.import_module('searchify.repositories.summaries').SummaryRepository
    files = files_type.__new__(files_type)
    files.client, files.owner_id, files.db_path, files._directory_cache = client, owner, db, {}
    summaries = summaries_type.__new__(summaries_type)
    summaries.client, summaries.owner_id = client, owner
    return files, summaries


# Wrap a real or fake client and reject disallowed fields/source markers before every write.
class AuditedClient:
    def __init__(self, client, canaries):
        self.client, self.canaries, self.writes = client, canaries, []
    def table(self, name):
        return AuditedQuery(self, name, self.client.table(name))
    def rpc(self, *args, **kwargs):
        return self.client.rpc(*args, **kwargs)
    def inspect(self, table, payload):
        rows = payload if isinstance(payload, list) else [payload]
        if table not in WRITE_FIELDS or any(set(row)-WRITE_FIELDS[table] for row in rows):
            raise AssertionError('Unexpected persistence fields or source/chunk table write.')
        encoded = json.dumps(payload)
        if any(marker in encoded for marker in self.canaries):
            raise AssertionError('Source canary reached the persistence boundary.')
        if any(row.get('summary_data') for row in rows):
            raise AssertionError('Nonempty structured summary data requires explicit privacy review.')
        self.writes.append((table, deepcopy(payload)))


# Intercept mutating query-chain calls, then delegate response behavior to the wrapped provider.
class AuditedQuery:
    def __init__(self, audit, table, query):
        self.audit, self.table, self.query = audit, table, query
    def __getattr__(self, name):
        def call(*args, **kwargs):
            if name in {'insert','upsert','update'}:
                self.audit.inspect(self.table, args[0])
            result = getattr(self.query, name)(*args, **kwargs)
            if name == 'execute': return result
            self.query = result
            return self
        return call


class MemorySupabase:
    """PostgREST-shaped fake; owner isolation/RPC here do NOT verify real RLS/SQL."""
    def __init__(self, owner=None, store=None, mutex=None):
        self.owner = owner or str(uuid.uuid4())
        self.store = store if store is not None else {name:[] for name in WRITE_FIELDS}
        self.mutex = mutex or threading.RLock()
        self.rpc_calls = []
        self.failures = []
    def table(self, name): return MemoryQuery(self, name)
    def rpc(self, name, params):
        if name != 'match_file_summaries': raise AssertionError('Unexpected RPC.')
        self.rpc_calls.append((name, deepcopy(params)))
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=self.match(params)))
    def match(self, params):
        from searchify.ai.embeddings import validate_embedding
        from searchify.retrieval.semantic_search import validate_limit
        query = validate_embedding(params['query_embedding']); limit = validate_limit(params['match_count'])
        results = []
        with self.mutex:
            for vector in self.store['file_summary_embeddings']:
                if vector['owner_id']!=self.owner or vector['model_name']!=params['embedding_model']: continue
                summary = next((s for s in self.store['file_summaries'] if s['id']==vector['summary_id'] and s['owner_id']==self.owner), None)
                file = next((f for f in self.store['files'] if summary and f['id']==summary['file_id'] and f['owner_id']==self.owner), None)
                directory = next((d for d in self.store['directories'] if file and d['id']==file['directory_id'] and d['owner_id']==self.owner), None)
                if not directory or not directory['enabled'] or file['status'] not in {'indexed','metadata_only'}: continue
                if file['summary_status']!='completed' or file['content_hash']!=summary['source_content_hash']: continue
                embedding = validate_embedding(vector['embedding'])
                score = sum(a*b for a,b in zip(query, embedding)) / (math.sqrt(sum(a*a for a in query))*math.sqrt(sum(b*b for b in embedding)))
                results.append(dict(file_id=file['id'], summary_id=summary['id'], summary=summary['summary'],
                    directory_id=directory['id'], filename=file['filename'], relative_path=file['relative_path'],
                    path=directory['path'].rstrip('/')+'/'+file['relative_path'], similarity=score))
        return sorted(results, key=lambda row:(-row['similarity'],row['file_id']))[:limit]


# Implement the PostgREST subset needed by acceptance tests, serialized under the client mutex.
class MemoryQuery:
    def __init__(self, client, table):
        self.client, self.table = client, table
        self.filters, self.sorts = [], []
        self.bounds, self.single, self.columns = (0, 2**31), False, '*'
        self.operation, self.payload, self.options = 'select', None, {}
    def select(self, columns, **kwargs): self.columns=columns; return self
    def eq(self, key, value): self.filters.append(lambda row:row.get(key)==value); return self
    def neq(self, key, value): self.filters.append(lambda row:row.get(key)!=value); return self
    def in_(self, key, values): self.filters.append(lambda row:row.get(key) in values); return self
    def order(self, key): self.sorts.append(key); return self
    def range(self, start, end): self.bounds=(start,end+1); return self
    def maybe_single(self): self.single=True; return self
    def update(self, payload): self.operation='update'; self.payload=payload; return self
    def upsert(self, payload, **options): self.operation='upsert'; self.payload=payload; self.options=options; return self
    def execute(self):
        with self.client.mutex:
            if self.client.failures and self.client.failures[0][:2]==(self.table,self.operation):
                _,_,error=self.client.failures.pop(0)
                raise error
            table = self.client.store[self.table]
            selected = [row for row in table if row['owner_id']==self.client.owner and all(check(row) for check in self.filters)]
            if self.operation=='update':
                for row in selected: row.update(deepcopy(self.payload))
            elif self.operation=='upsert':
                selected=[]
                for value in self.payload if isinstance(self.payload,list) else [self.payload]:
                    if value['owner_id']!=self.client.owner: raise PermissionError('Owner mismatch.')
                    conflict=self.options.get('on_conflict','id').split(',')
                    old=next((row for row in table if all(row.get(key)==value.get(key) for key in conflict)),None)
                    if old is not None and self.options.get('ignore_duplicates'): continue
                    if old is None:
                        old={'id':str(uuid.uuid4()),'created_at':datetime.now(timezone.utc).isoformat()}
                        if self.table=='directories': old['enabled']=True
                        table.append(old)
                    elif old['owner_id']!=self.client.owner: raise PermissionError('Owner mismatch.')
                    old.update(deepcopy(value)); selected.append(old)
            else:
                for key in reversed(self.sorts): selected.sort(key=lambda row:row.get(key,''))
                selected=selected[self.bounds[0]:self.bounds[1]]
                if self.columns!='*':
                    keys=[key.strip() for key in self.columns.split(',')]
                    selected=[{key:row[key] for key in keys if key in row} for row in selected]
            data=deepcopy(selected)
            return SimpleNamespace(data=(data[0] if data else None) if self.single else data)


class FakeOllama:
    """Fake only the HTTP boundary; production clients construct/parse requests."""
    def __init__(self):
        self.calls=[]; self.fail_embedding_for=None; self.errors=[]
    def open(self, request, timeout=None):
        from urllib.error import HTTPError
        data=json.loads(request.data) if request.data else None
        endpoint=request.full_url.rsplit('/api/',1)[-1]
        self.calls.append((endpoint, deepcopy(data)))
        if endpoint=='tags': response={'models':[{'name':settings.SUMMARY_MODEL},{'name':settings.EMBEDDING_MODEL}]}
        elif endpoint=='generate':
            if data['model']!=settings.SUMMARY_MODEL: raise AssertionError('Summary model changed.')
            summary=('Computes invoice totals and tax amounts for billing.' if 'invoice.py' in data['prompt']
                     else 'Interprets daily weather forecasts and temperature readings.')
            response={'response':summary,'done':True,'done_reason':'stop'}
        elif endpoint=='embed':
            if data['model']!=settings.EMBEDDING_MODEL or data['dimensions']!=768: raise AssertionError('Embedding configuration changed.')
            if self.fail_embedding_for and self.fail_embedding_for in data['input'].lower():
                error=HTTPError(request.full_url,429,'synthetic rate limit',{'x-request-id':'req_e2e_request_429'},None)
                self.errors.append(error)
                raise error
            invoice='invoice' in data['input'].lower() or 'billing' in data['input'].lower()
            response={'embeddings':[[1.,0.] + [0.]*766 if invoice else [0.,1.] + [0.]*766]}
        else: raise AssertionError('Unexpected Ollama endpoint.')
        return io.BytesIO(json.dumps(response).encode())


class CountedOllama:
    """Count AI calls without retaining prompts or embedding inputs."""
    def __init__(self, client):
        from collections import Counter
        self.client=client; self.host=client.host; self.calls=Counter()
    def check_available(self): return self.client.check_available()
    def post(self, endpoint, payload):
        self.calls[endpoint]+=1
        return self.client.post(endpoint,payload)


# Own disposable files, canaries, repositories, AI providers, and report directories for one
# scenario.
class PipelineWorkspace:
    def __init__(self, client, owner, ollama):
        self.temp=tempfile.TemporaryDirectory(prefix='searchify-e2e-')
        self.base=Path(self.temp.name).resolve(); self.root=self.base/'registered'; self.root.mkdir()
        self.registry=FileRegistry(self.base/'metadata.db')
        self.canaries=['SOURCE_CANARY_'+uuid.uuid4().hex for _ in range(3)]
        self.sources=[
            '# '+self.canaries[0]+'\ndef invoice_total(items, tax):\n    return sum(items) * (1 + tax)\n',
            '# '+self.canaries[1]+'\ndef forecast_temperature(readings):\n    return sum(readings) / len(readings)\n',
        ]
        (self.root/'invoice.py').write_text(self.sources[0])
        (self.root/'weather.py').write_text(self.sources[1])
        (self.root/'unsupported.png').write_bytes(b'\x89PNG\x00not-a-text-file')
        outside=self.base/'not-registered'; outside.mkdir(); (outside/'outside.py').write_text(self.canaries[2])
        self.audit=AuditedClient(client,self.canaries)
        self.files,self.summaries=repositories(self.audit,owner,self.registry.path)
        self.ollama=CountedOllama(ollama)
        self.summarizer=Summarizer(self.ollama); self.embeddings=Embeddings(self.ollama)
        self.sync=SyncService(db_path=self.registry.path,files=self.files,summaries=self.summaries,
            summarizer=self.summarizer,embeddings=self.embeddings,reports_dir=self.base/'reports')
        self.search=SearchService(QueryEmbeddings(self.ollama),SemanticSearch(self.summaries))
    def close(self): self.temp.cleanup()
    def register_and_index(self):
        from searchify import indexer
        self.registry.register(self.root,DirectoryRules.preset('source'))
        with patch.object(indexer,'DB_PATH',self.registry.path), redirect_stdout(io.StringIO()):
            indexer.index_all()
    def search_without_source_reads(self, query, limit=10):
        with patch('searchify.core.extractor.extract_content',side_effect=AssertionError('Search extracted source.')), \
             patch('pathlib.Path.open',side_effect=AssertionError('Search opened a local file.')), \
             patch('sqlite3.connect',side_effect=AssertionError('Search accessed local source index.')):
            return self.search.search(query,limit=limit)
    def assert_private(self, case):
        with self.registry.connection() as conn:
            tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            case.assertFalse({'file_search','file_chunks'} & tables)
            for table in tables:
                columns={row[1] for row in conn.execute('PRAGMA table_info("'+table+'")')}
                case.assertFalse({'content','chunks','extracted_content'} & columns)
            encoded='\n'.join(conn.iterdump())
            case.assertFalse(any(marker in encoded for marker in self.canaries))
        for artifact in self.base.glob('metadata.db*'):
            if artifact.is_file():
                value=artifact.read_bytes()
                case.assertFalse(any(marker.encode() in value for marker in self.canaries))
        for report in (self.base/'reports').glob('*.json'):
            value=report.read_text()
            case.assertFalse(any(marker in value for marker in self.canaries))
        for table,payload in self.audit.writes:
            case.assertIn(table,WRITE_FIELDS)
            value=json.dumps(payload)
            case.assertFalse(any(marker in value for marker in self.canaries))
            case.assertFalse(any(content in value for content in self.sources))


# Shared acceptance scenario verifies initial sync, reuse, changes, deletion, ranking, and
# source privacy.
def exercise_lifecycle(case, workspace):
    """Shared real service/repository lifecycle, run offline or by explicit live runner."""
    w=workspace
    w.register_and_index()
    case.assertEqual(len(w.registry.directories()),1)
    entries,metrics,_=Scanner(w.registry).scan()
    case.assertEqual(metrics['total_discovered'],3)
    case.assertEqual(sum(bool(entry['eligible']) for entry in entries),2)
    case.assertEqual(len(w.registry.metadata()),3)
    case.assertTrue(all(Path(path).is_relative_to(w.root) for path in w.registry.metadata()))
    first=w.sync.sync()
    case.assertEqual(first['completed'],2); case.assertEqual(first['failed'],0)
    case.assertEqual(first['skipped'],1)
    rows=w.files.list_files(w.files.list_directories()[0]['id'])
    case.assertEqual(len(rows),2)
    invoice=next(row for row in rows if row['filename']=='invoice.py')
    weather=next(row for row in rows if row['filename']=='weather.py')
    summary=w.summaries.get_summary(invoice['id'])
    vector=w.summaries.get_summary_embedding(summary['id'])
    from searchify.ai.embeddings import validate_embedding
    embedding=json.loads(vector['embedding']) if isinstance(vector['embedding'],str) else vector['embedding']
    case.assertEqual(len(validate_embedding(embedding)),768)
    case.assertEqual(vector['model_name'],settings.EMBEDDING_MODEL)
    # Query identical derived prose so ranking tests aren't language-model quality guesses.
    ranked=w.search_without_source_reads(summary['summary'],limit=1)
    case.assertEqual(len(ranked),1); case.assertEqual(ranked[0]['file_id'],invoice['id'])
    case.assertEqual(ranked[0]['path'],str(w.root/'invoice.py'))
    case.assertGreater(ranked[0]['similarity'],0.98)
    for key in ('file_id','summary','path','similarity'): case.assertIn(key,ranked[0])
    ai_calls=dict(w.ollama.calls)
    unchanged=w.sync.sync()
    case.assertEqual(unchanged['completed'],0); case.assertEqual(unchanged['report']['metrics']['unchanged'],3)
    case.assertEqual(dict(w.ollama.calls),ai_calls)
    case.assertEqual(w.files.get_file(invoice['id'])['summary_updated_at'],invoice['summary_updated_at'])
    before_hash=invoice['content_hash']
    modified=w.sources[0]+'\n# '+w.canaries[2]+'\ndef discount_total(total, discount):\n    return total - discount\n'
    (w.root/'invoice.py').write_text(modified)
    w.sources.append(modified)
    changed=w.sync.sync()
    case.assertEqual(changed['completed'],1); case.assertEqual(changed['report']['metrics']['modified'],1)
    case.assertEqual(w.ollama.calls['/api/generate'],ai_calls['/api/generate']+1)
    case.assertEqual(w.ollama.calls['/api/embed'],ai_calls['/api/embed']+1)
    refreshed=w.files.get_file(invoice['id'])
    case.assertNotEqual(refreshed['content_hash'],before_hash)
    case.assertEqual(w.summaries.get_summary(invoice['id'])['source_content_hash'],refreshed['content_hash'])
    case.assertEqual(w.files.get_file(weather['id'])['summary_updated_at'],weather['summary_updated_at'])
    (w.root/'weather.py').unlink()
    before_deletion=dict(w.ollama.calls)
    deleted=w.sync.sync()
    case.assertEqual(dict(w.ollama.calls),before_deletion)
    case.assertEqual(deleted['report']['metrics']['deleted'],1)
    case.assertEqual(w.files.get_file(weather['id'])['status'],'error')
    ranked=w.search_without_source_reads(summary['summary'])
    case.assertEqual({row['file_id'] for row in ranked},{invoice['id']})
    reports=list((w.base/'reports').glob('*.json'))
    case.assertEqual(len(reports),4)
    for path in reports:
        report=json.loads(path.read_text())
        case.assertTrue(report['started_at']); case.assertTrue(report['finished_at'])
        case.assertEqual(report['counts']['total'],len(report['outcomes']))
    w.assert_private(case)
    return invoice['id']
