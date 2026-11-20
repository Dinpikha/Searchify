"""Explicit-only live acceptance runner, deliberately outside unittest discovery.

Requires BOTH --live and SEARCHIFY_RUN_LIVE_E2E=1. Only a disposable literal
loopback Supabase target is accepted. Application credentials/keyring are unused.
"""
import argparse
from contextlib import ExitStack
import ipaddress
import json
import os
import re
import secrets
import sys
import unittest
from urllib.parse import urlsplit
import uuid


class LiveConfigurationError(ValueError):
    pass


# Require both explicit opt-ins and distinct test-stack keys for a literal loopback HTTP target.
def live_configuration(environment, explicit=False):
    if not explicit or environment.get('SEARCHIFY_RUN_LIVE_E2E')!='1':
        raise LiveConfigurationError('Live tests require --live and SEARCHIFY_RUN_LIVE_E2E=1; normal tests are offline.')
    url=environment.get('SEARCHIFY_LIVE_SUPABASE_URL','').strip()
    try:
        parsed=urlsplit(url)
        address=ipaddress.ip_address(parsed.hostname)
        if (parsed.scheme!='http' or not address.is_loopback or not parsed.port
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ('','/') or parsed.query or parsed.fragment
                or any(character.isspace() for character in url)):
            raise ValueError
    except (ValueError, TypeError):
        raise LiveConfigurationError('Use a disposable local Supabase URL with a literal loopback IP and explicit port.') from None
    anon=environment.get('SEARCHIFY_LIVE_SUPABASE_ANON_KEY','')
    service=environment.get('SEARCHIFY_LIVE_SUPABASE_SERVICE_KEY','')
    if not anon.strip() or not service.strip() or anon==service:
        raise LiveConfigurationError('Supply distinct local test-stack anon and service-role keys through SEARCHIFY_LIVE_SUPABASE_*_KEY; application credentials are never used.')
    return url.rstrip('/'),anon,service


# Inspect owner rows and historical chunk tables for source markers or prohibited fields.
def assert_remote_privacy(case, workspace, client, owner):
    from pipeline_support import WRITE_FIELDS
    for table in WRITE_FIELDS:
        rows=client.table(table).select('*').eq('owner_id',owner).execute().data or []
        encoded=json.dumps(rows)
        case.assertFalse(any(marker in encoded for marker in workspace.canaries))
        case.assertFalse(any(content in encoded for content in workspace.sources))
        for row in rows:
            case.assertFalse({'content','chunks','extracted_content'} & set(row))
    # Legacy schema tables may exist, but this pipeline must never populate them.
    for table in ('file_chunks','file_embeddings'):
        case.assertEqual(client.table(table).select('*').eq('owner_id',owner).execute().data,[])


# Create disposable users, run real provider acceptance, then attempt session/user/workspace
# cleanup.
def run_live(url, anon_key, service_key):
    # Imported only after BOTH opt-ins and target/credential isolation validation.
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    import httpx
    from supabase import create_client, ClientOptions
    from pipeline_support import PipelineWorkspace, exercise_lifecycle, repositories
    from searchify.ai.ollama_client import OllamaClient
    from searchify.config.settings import settings
    from searchify.ai.embeddings import validate_embedding
    from searchify.retrieval.semantic_search import SemanticSearch
    from searchify.services.search_service import SearchService
    from searchify.intelligence.embeddings import QueryEmbeddings

    run_id=uuid.uuid4().hex
    users=[]; clients=[]; workspace=None; cleanup_errors=[]
    admin=None; creation_uncertain=False
    status={'mode':'live','run_id':run_id,'status':'failed','stages':{},'cleanup':'not started'}
    with ExitStack() as stack:
        def client(key):
            transport=stack.enter_context(httpx.Client(trust_env=False,follow_redirects=False,timeout=30))
            options=ClientOptions(persist_session=False,auto_refresh_token=False,httpx_client=transport)
            return create_client(url,key,options=options)
        try:
            admin=client(service_key)
            ollama=OllamaClient()
            ollama.check_available()
            status['stages']['ollama_connectivity']='real'
            # Each account/session is created for this run; never restore a saved session.
            for number in range(2):
                email=f'searchify-e2e-{run_id}-{number}@example.invalid'
                password=secrets.token_urlsafe(36)
                creation_uncertain=True
                response=admin.auth.admin.create_user({'email':email,'password':password,
                    'email_confirm':True,'user_metadata':{'searchify_e2e_run':run_id}})
                owner=response.user.id
                users.append(owner); creation_uncertain=False
                authenticated=client(anon_key); clients.append(authenticated)
                logged_in=authenticated.auth.sign_in_with_password({'email':email,'password':password})
                del password
                if not logged_in.session or not logged_in.user or logged_in.user.id!=owner:
                    raise RuntimeError('Temporary integration-user authentication failed.')
            status['stages']['temporary_user_authentication']='real'
            workspace=PipelineWorkspace(clients[0],users[0],ollama)
            case=unittest.TestCase()
            invoice_id=exercise_lifecycle(case,workspace)
            for name in ('registration','scanning','indexing','summarization','summary_embeddings',
                         'metadata_persistence','summary_persistence','query_embeddings','pgvector_retrieval',
                         'modified_unchanged_deleted','sqlite_privacy'):
                status['stages'][name]='real'
            assert_remote_privacy(case,workspace,clients[0],users[0])
            status['stages']['supabase_privacy']='real'
            # Real second-user RPC/table requests verify RLS rather than a fake owner filter.
            _,peer_summaries=repositories(clients[1],users[1],workspace.registry.path)
            peer_search=SearchService(QueryEmbeddings(ollama),SemanticSearch(peer_summaries))
            mine=workspace.summaries.get_summary(invoice_id)
            case.assertEqual(peer_search.search(mine['summary']),[])
            case.assertEqual(clients[1].table('files').select('id').eq('owner_id',users[0]).execute().data,[])
            peer_dir=clients[1].table('directories').upsert({'owner_id':users[1],
                'path':'/searchify-isolated-peer/'+run_id},on_conflict='owner_id,path').execute().data[0]
            peer_file=clients[1].table('files').upsert({'owner_id':users[1],'directory_id':peer_dir['id'],
                'relative_path':'peer.py','filename':'peer.py','content_hash':'synthetic-peer-hash',
                'status':'indexed','summary_status':'completed'},on_conflict='directory_id,relative_path').execute().data[0]
            peer_summary=peer_summaries.upsert_summary(peer_file['id'],'synthetic-peer-hash',mine['summary'],{},settings.SUMMARY_MODEL)
            stored=workspace.summaries.get_summary_embedding(mine['id'])['embedding']
            vector=validate_embedding(json.loads(stored) if isinstance(stored,str) else stored)
            peer_summaries.save_embedding(peer_summary['id'],vector,settings.EMBEDDING_MODEL)
            case.assertEqual({row['file_id'] for row in peer_search.search(mine['summary'])},{peer_file['id']})
            case.assertNotIn(peer_file['id'],{row['file_id'] for row in workspace.search_without_source_reads(mine['summary'])})
            assert_remote_privacy(case,workspace,clients[1],users[1])
            status['stages']['rls_and_owner_isolation']='real'
            status['status']='passed'
        except (Exception, KeyboardInterrupt) as error:
            # Never print SDK exception bodies, JWTs, prompts or assertion values.
            from searchify.reporting.sync_report import describe_error
            status['error']=describe_error(getattr(error,'processing_stage','persistence'),error)
            if workspace is not None:
                status['last_sync_report']=workspace.sync.last_result
        finally:
            for authenticated in clients:
                try: authenticated.auth.sign_out()
                except Exception: cleanup_errors.append('temporary session sign-out failed')
            if admin is not None:
                for owner in reversed(users):
                    try:
                        # Cascade deletion is scoped exclusively to users created above.
                        admin.auth.admin.delete_user(owner)
                        for table in ('directories','files','file_summaries','file_summary_embeddings','file_chunks','file_embeddings'):
                            if admin.table(table).select('owner_id').eq('owner_id',owner).execute().data:
                                raise RuntimeError('Test-owned records remain.')
                    except Exception:
                        cleanup_errors.append('temporary user/record cleanup failed: '+owner)
            if workspace is not None: workspace.close()
            if creation_uncertain: cleanup_errors.append('user creation response uncertain; inspect the run marker before rerunning')
            status['cleanup']='passed' if not cleanup_errors else 'failed'
            if cleanup_errors:
                status['cleanup_errors']=cleanup_errors; status['status']='failed'
    return status


# Parse explicit live opt-in and emit a sanitized status report with an appropriate exit code.
def main(argv=None, environment=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',help='Explicitly permit synthetic test data on a disposable local Supabase stack')
    args=parser.parse_args(argv)
    try:
        config=live_configuration(os.environ if environment is None else environment,args.live)
    except LiveConfigurationError as error:
        print(str(error),file=sys.stderr); return 2
    try:
        result=run_live(*config)
    except Exception as error:
        # Missing/incompatible SDK dependencies must also avoid arbitrary error bodies.
        name=type(error).__name__
        name=name if re.fullmatch('[A-Za-z_][A-Za-z_0-9]{0,79}',name) else 'Exception'
        print(json.dumps({'mode':'live','status':'failed','exception_type':name,
            'action':'Install the project runtime dependencies; verify disposable-stack migrations and model availability.'}))
        return 1
    print(json.dumps(result,indent=2))
    return 0 if result['status']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(main())
