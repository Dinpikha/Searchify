"""Read-only local and provider diagnostics. Never signs in or refreshes tokens."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import base64
import importlib
import importlib.util
import json
from pathlib import Path
import time
import sqlite3
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
import uuid

from searchify.config.settings import settings, DOTENV_AVAILABLE, PROJECT_ROOT
from searchify.services.session_service import KEYRING_SERVICE, canonical_target, decode_session, session_account
from searchify.core.file_registry import FileRegistry
from searchify.core.scanner import Scanner, matches, PROTECTED, signature


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def request_json(url, headers=None, timeout=1.5):
    try:
        opener=build_opener(ProxyHandler({}),NoRedirects())
        with opener.open(Request(url,headers=headers or {}),timeout=timeout) as response:
            return {'status':'available','http_status':response.status,'data':json.loads(response.read(1024*1024))}
    except HTTPError as error:
        code=error.code; error.close()
        return {'status':'unavailable','http_status':code,'data':None}
    except (OSError,ValueError):
        return {'status':'unavailable','http_status':None,'data':None}


def valid_supabase_url(value):
    try:
        url=urlsplit(value)
        return bool(url.hostname and not url.username and not url.password and not url.query and not url.fragment
                    and url.path in ('','/') and (url.scheme=='https' or
                         (url.scheme=='http' and url.hostname in {'localhost','127.0.0.1','::1'})))
    except ValueError:
        return False


def dependencies():
    result={}
    for name in ('dotenv','keyring','supabase','rich'):
        try:
            module=importlib.import_module(name)
            result[name]=callable(getattr(module,'create_client',None)) if name=='supabase' else True
        except ImportError:
            result[name]=False
    return result


class HealthService:
    def __init__(self, db_path=None, reports_dir=None):
        self.registry=FileRegistry(db_path or settings.LOCAL_DB_PATH)
        self.reports_dir=Path(reports_dir or settings.REPORTS_DIR)

    def _providers(self, offline=False):
        if offline:
            return {'ollama':{'status':'not checked (offline)'},'supabase':{'status':'not checked (offline)'}}
        # Validate the loopback host through the existing transport without making AI calls.
        from searchify.ai.ollama_client import OllamaClient
        jobs={}
        try:
            host=OllamaClient().host
            jobs['ollama']=(host+'/api/tags',{})
        except ValueError:
            pass
        if settings.SUPABASE_KEY and valid_supabase_url(settings.SUPABASE_URL):
            jobs['supabase']=(settings.SUPABASE_URL.rstrip('/')+'/auth/v1/settings',{'apikey':settings.SUPABASE_KEY})
        output={name:{'status':'not configured'} for name in ('ollama','supabase')}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures={name:pool.submit(request_json,url,headers) for name,(url,headers) in jobs.items()}
            for name,future in futures.items():
                value=future.result()
                output[name]={'status':value['status'],'http_status':value.get('http_status')}
                if name=='ollama' and value['status']=='available':
                    data=value.get('data') or {}
                    if not isinstance(data,dict) or not isinstance(data.get('models'),list):
                        output[name]['status']='unavailable'
                        continue
                    models=[row.get('name') for row in data.get('models',[]) if isinstance(row,dict)]
                    output[name]['summary_model_installed']=settings.SUMMARY_MODEL in models
                    output[name]['embedding_model_installed']=settings.EMBEDDING_MODEL in models
        return output

    def authentication(self, inspect_keyring=False):
        if not inspect_keyring:
            return {'status':'not checked (doctor --auth inspects the secure keyring)','database':'not checked'}
        try:
            target=canonical_target(settings.SUPABASE_URL)
            api_key=settings.SUPABASE_KEY
            if not valid_supabase_url(target) or not api_key:
                return {'status':'invalid Supabase configuration'}
            import keyring
            stored=keyring.get_password(KEYRING_SERVICE,session_account(target))
            if not stored:
                return {'status':'no cached session'}
            token=decode_session(stored,target)['access_token']
            encoded=token.split('.')[1]
            claims=json.loads(base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4)))
            owner=str(uuid.UUID(claims['sub']))
            if claims.get('exp',0)<=time.time():
                return {'status':'expired cached session; sign in through an explicit cloud operation'}
            url=target+'/rest/v1/files?'+urlencode({'select':'id','limit':1,'owner_id':'eq.'+owner})
            checked=request_json(url,{'apikey':api_key,'Authorization':'Bearer '+token})
            return {'status':'authenticated; owner-scoped database read succeeded' if checked['status']=='available' else 'cached session could not access database',
                    'database':checked['status'],'http_status':checked.get('http_status')}
        except Exception:
            return {'status':'unable to inspect cached session; check dependencies and Keychain access'}

    def snapshot(self, scan=False, offline=False, auth=False):
        database_error=False
        try:
            dirs=self.registry.directories(); metadata=self.registry.metadata(); states=self.registry.states()
        except (sqlite3.Error,OSError,ValueError):
            dirs,metadata,states=[],{},{}; database_error=True
        directory_map={directory['id']:directory for directory in dirs}
        rule_map={directory['id']:self.registry.rules(directory['id']) for directory in dirs}
        statuses=Counter(); supported=unsupported=modified=deleted=0
        for path,row in metadata.items():
            root=directory_map.get(row['root_id'])
            if root is None: continue
            rules=rule_map[root['id']]; source=Path(path)
            state=states.get(path,{})
            try:
                if not source.is_file():
                    deleted+=1; statuses['unavailable']+=1; continue
                rel=source.relative_to(root['path']).as_posix()
                eligible=(rules.sync_enabled and not source.is_symlink() and source.suffix.lower() in rules.extensions and
                          source.stat().st_size<=rules.max_size and matches(rel,rules.include)
                          and not matches(rel,rules.exclude) and not matches(source.name,PROTECTED))
                if source.suffix.lower() in rules.extensions: supported+=1
                else: unsupported+=1
                changed=signature(source.stat())!=state.get('signature')
                if changed: modified+=1
                status='skipped' if not eligible else ('pending' if changed else state.get('status','pending'))
                statuses[status]+=1
            except OSError:
                statuses['stale']+=1
        metrics={'new':None,'modified':modified,'deleted':deleted,'total_discovered':None,'excluded_directories':None}
        if scan and not database_error:
            _,metrics,_=Scanner(self.registry).scan(hash_contents=False)
        from searchify.reporting.history import load_reports
        reports,invalid=load_reports(self.reports_dir)
        latest=reports[0][1] if reports else None
        successful=next((report for _,report in reports if not report.get('dry_run') and not report.get('run_errors')
                         and not report['counts']['failed'] and not report['counts']['stale'] and not report.get('remaining')),None)
        recommendations=None
        review_paths=sorted(Path(settings.REVIEWS_DIR).glob('review-*.json'),key=lambda p:p.stat().st_mtime,reverse=True) if Path(settings.REVIEWS_DIR).is_dir() else []
        for path in review_paths:
            try:
                if path.is_symlink() or path.stat().st_size>8*1024*1024: continue
                with path.open() as source: saved_review=json.load(source)
                items=saved_review.get('recommendations')
                if isinstance(items,list): recommendations=len(items); break
            except (OSError,ValueError,AttributeError): continue
        deps=dependencies()
        providers=self._providers(offline)
        errors=['Local metadata database or journal is unavailable; check permissions and integrity before explicit repair.'] if database_error else []
        if not DOTENV_AVAILABLE and (PROJECT_ROOT/'.env').exists(): errors.append('python-dotenv unavailable: project .env was not loaded')
        if not all(deps.values()): errors.append('Missing or incompatible runtime dependencies')
        if not offline and (not valid_supabase_url(settings.SUPABASE_URL) or not settings.SUPABASE_KEY):
            errors.append('Supabase configuration incomplete or invalid')
        if settings.EMBEDDING_DIMENSIONS!=768: errors.append('Embedding dimensions do not match the 768-dimensional schema')
        if invalid: errors.append('Unreadable local reports')
        for name,value in providers.items():
            if value['status'] in {'unavailable','not configured'}: errors.append(name+' unavailable or not configured')
        if providers['ollama'].get('summary_model_installed') is False or providers['ollama'].get('embedding_model_installed') is False:
            errors.append('A configured Ollama model is not installed')
        return dict(pending_recommendations=recommendations,timestamp=datetime.now(timezone.utc).isoformat(),directories=[{**d,'available':Path(d['path']).is_dir(),
                    'rules':self.registry.rules(d['id']).to_dict()} for d in dirs],
                    indexed_files=None if database_error else len(metadata),supported_files=None if database_error else supported,unsupported_files=None if database_error else unsupported,
                    workspace=metrics,statuses={key:None if database_error else statuses[key] for key in ('completed','pending','failed','stale','skipped','processing','unavailable')},
                    remaining=None if database_error else sum(statuses[key] for key in ('pending','failed','stale','processing')),
                    last_sync=latest.get('finished_at') if latest else None,
                    last_successful_sync=successful.get('finished_at') if successful else None,
                    providers=providers,authentication=self.authentication(auth and not offline),dependencies=deps,
                    models={'summarization':settings.SUMMARY_MODEL,'embedding':settings.EMBEDDING_MODEL,'provider':'local Ollama',
                            'dimensions':settings.EMBEDDING_DIMENSIONS,'runtime_dimensions':'not tested (would require an embedding call)'},
                    local_database='unavailable' if database_error else 'available' if self.registry.path.is_file() else 'not created',errors=errors)
