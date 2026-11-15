"""Sequential, resumable synchronization of explicitly registered local roots."""
from pathlib import Path
import logging

from searchify.ai.ollama_client import check_processing_runtime, OllamaUnavailableError
from searchify.config.settings import settings
from searchify.core.file_registry import FileRegistry, RegistryBusy
from searchify.core.scanner import Scanner
from searchify.reporting.sync_report import SyncReport, describe_error, make_outcome, now, safe_filename
from searchify.services.summary_service import SummaryService, StaleContentError
from searchify.services.processing_claim import recover_claim

logger = logging.getLogger(__name__)


class SyncError(RuntimeError):
    pass


def _notify(callback, value, event):
    if callback:
        try:
            callback(value, event)
        except Exception:
            logger.warning('Progress display failed; synchronization state was preserved.')


def display_path(path):
    path = Path(path).expanduser().resolve()
    try:
        return '~/' + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


class SyncService:
    def __init__(self, batch_size=5, db_path=None, files=None, summaries=None,
                 summarizer=None, embeddings=None, reports_dir=None):
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError('Batch size must be positive.')
        self.batch_size = batch_size
        self.registry = FileRegistry(db_path or settings.LOCAL_DB_PATH)
        self.db_path = self.registry.path
        self.reports_dir = Path(reports_dir or settings.REPORTS_DIR)
        self.files, self.summaries = files, summaries
        self.summarizer, self.embeddings = summarizer, embeddings
        self.summary_service = (SummaryService(files, summaries, summarizer, embeddings, self.db_path)
                                if all(value is not None for value in (files,summaries,summarizer,embeddings)) else None)
        self.report, self.last_result = SyncReport(), None
        self._queued_files = []
        self._dry_run = False
        self._views = {}

    def _repositories(self):
        if self.files is None:
            from searchify.repositories.files import FileRepository
            self.files = FileRepository(db_path=self.db_path)
        if self.summaries is None:
            from searchify.repositories.summaries import SummaryRepository
            self.summaries = SummaryRepository()

    def _providers(self):
        from searchify.ai.summarizer import Summarizer
        from searchify.ai.embeddings import Embeddings
        if self.summarizer is None:
            self.summarizer = Summarizer()
        if self.embeddings is None:
            self.embeddings = Embeddings()
        if self.summary_service is None:
            self.summary_service = SummaryService(self.files, self.summaries, self.summarizer, self.embeddings, self.db_path)

    def _view(self, entry, outcome, stage=None, error=None, reason=None):
        row=make_outcome(entry,outcome,stage,error,reason)
        key=entry.get('path')
        if key is not None and key in self._views:
            self._views[key].update(row)
            return self._views[key]
        self.report.outcomes.append(row)
        if key is not None: self._views[key]=row
        return row

    def _classify_local(self, entries):
        states=self.registry.states()
        for entry in entries:
            if entry.get('eligible'): continue
            state=states.get(entry['path'],{})
            remote_id=state.get('remote_id')
            reason=entry.get('reason')
            if entry.get('scan_error'):
                row=self._record(entry,'stale',reason=reason,remote_id=remote_id)
                row.update(outcome='failed',error=entry['scan_error'])
                self.registry.save_state(entry,'failed',error=row['error'],remote_id=remote_id)
            elif entry['change']=='deleted':
                if state.get('status')=='unavailable': self._view(entry,'skipped',reason='known_unavailable')
                else: self._record(entry,'stale',StaleContentError('Local file content is missing or changed.'),remote_id=remote_id)
            elif reason=='missing_directory':
                self._record(entry,'stale',StaleContentError('Directory metadata unavailable.'),remote_id=remote_id)
            elif reason in {'incomplete_scan','changed_during_scan'}:
                self._record(entry,'stale',reason=reason,remote_id=remote_id)
            else:
                self._record(entry,'skipped',reason=reason,remote_id=remote_id)

    def _record(self, entry, outcome, error=None, stage='extraction', reason=None, remote_id=None):
        row = self._view({**entry, 'file_id': remote_id or entry.get('file_id')}, outcome, stage, error, reason)
        if not self._dry_run and entry.get('root_id') is not None:
            status = 'unavailable' if entry.get('change')=='deleted' or entry.get('reason')=='missing_directory' else outcome
            self.registry.save_state(entry, status, error=row.get('error'), remote_id=remote_id,
                                     summary_model=settings.SUMMARY_MODEL if outcome=='completed' else None,
                                     embedding_model=settings.EMBEDDING_MODEL if outcome=='completed' else None,
                                     summary_version=settings.SUMMARY_VERSION if outcome=='completed' else None)
        return row

    def _valid_remote_summary(self, remote, entry, state):
        if remote.get('summary_status') != 'completed' or remote.get('content_hash') != entry['content_hash']:
            return False
        if (state.get('status')=='completed' and state.get('content_hash')==entry['content_hash']
                and state.get('summary_model')==settings.SUMMARY_MODEL
                and state.get('embedding_model')==settings.EMBEDDING_MODEL and state.get('summary_version')==settings.SUMMARY_VERSION):
            return True
        summary = self.summaries.get_summary(remote['id'])
        if not summary or summary.get('source_content_hash') != entry['content_hash'] or summary.get('model_name') != settings.SUMMARY_MODEL or summary.get('summary_version',1)!=settings.SUMMARY_VERSION:
            return False
        vector = self.summaries.get_summary_embedding(summary['id'])
        if not vector or vector.get('model_name')!=settings.EMBEDDING_MODEL: return False
        try:
            import json
            from searchify.ai.embeddings import validate_embedding
            validate_embedding(json.loads(vector['embedding']) if isinstance(vector['embedding'],str) else vector['embedding'])
        except (KeyError,TypeError,ValueError): return False
        return True

    def _prepare(self, entries, retry_failed, retry_permanent=False):
        """One paginated metadata fetch per registered remote directory."""
        remote_dirs = {row['path']: row for row in self.files.list_directories()}
        states = self.registry.states()
        registered=self.registry.directories()
        roots={Path(d['path']) for d in registered}
        queue = []
        for directory in registered:
            local = [entry for entry in entries if entry['root_id']==directory['id']]
            remote_dir = remote_dirs.get(display_path(directory['path']))
            if remote_dir is None:
                if not local: continue
                remote_dir = self.files.ensure_directory(directory['path'])
            rows = self.files.list_files(remote_dir['id'])
            root=Path(directory['path'])
            for row in rows:
                candidate=(root/row['relative_path']).resolve()
                if any(other!=root and other.is_relative_to(root) and candidate.is_relative_to(other) for other in roots):
                    if row.get('status')!='error': self.files.mark_unavailable(row['id'])
            remote = {row['relative_path']: row for row in rows}
            updates, waiting, moved = [], [], set()
            # A unique content hash can identify a rename without regenerating AI artifacts.
            removed = {e['relative_path']: remote[e['relative_path']] for e in local
                       if e['change']=='deleted' and e['relative_path'] in remote}
            missing_hashes = {}
            for rel, record in removed.items():
                missing_hashes.setdefault(record.get('content_hash'), []).append((rel, record))
            new_hash_counts = {}
            for entry in local:
                if entry.get('eligible') and entry['relative_path'] not in remote:
                    h = entry['content_hash']; new_hash_counts[h] = new_hash_counts.get(h,0)+1
            for entry in local:
                row = remote.get(entry['relative_path'])
                previous = states.get(entry['path'], {})
                file_id = row['id'] if row else None
                if row and row.get('summary_status')=='processing':
                    # Never adopt a token from a journal as this worker's claim.
                    recovered = recover_claim(self.files, self.registry, {**row, **entry, 'id':row['id'],
                        'content_hash':row['content_hash'], 'summary_status':'processing',
                        'summary_updated_at':row.get('summary_updated_at')}) if entry.get('eligible') else None
                    if recovered is None:
                        self._view({**entry,'file_id':file_id}, 'skipped', reason='processing_elsewhere')
                        continue
                    row = {**row, **recovered}
                if not remote_dir.get('enabled', True):
                    self._record(entry, 'skipped', reason='remote_directory_disabled', remote_id=file_id)
                    continue
                if entry.get('synthetic'):
                    if entry['reason']=='missing_directory':
                        for missing in rows:
                            if missing.get('status') != 'error':
                                self.files.mark_unavailable(missing['id'])
                        if entry['path'] not in self._views: self._record(entry, 'stale', StaleContentError('Directory metadata unavailable.'))
                    else:
                        self._record(entry, 'skipped' if entry['reason'] in {'sync_disabled','internal_metadata','protected_directory'} else 'stale',
                                     reason=entry['reason'])
                    continue
                if entry['change']=='deleted':
                    # Handle removals after potential rename detection below.
                    continue
                if not entry['eligible']:
                    outcome = 'failed' if entry.get('scan_error') else 'stale' if entry['reason']=='changed_during_scan' else 'skipped'
                    if row and row.get('status') != 'skipped':
                        self.files.mark_skipped(row['id'], entry['reason'])
                    result=self._views.get(entry['path']) or self._record(entry, 'stale' if outcome=='failed' else outcome, reason=entry['reason'], remote_id=file_id)
                    if file_id:
                        result['file_id']=file_id
                        self.registry.save_state(entry,result['outcome'],error=result.get('error'),remote_id=file_id)
                    if outcome=='failed':
                        result['outcome']='failed'; result['error']=entry['scan_error']
                        self.registry.save_state(entry,'failed',error=result['error'],remote_id=file_id)
                    continue
                if row is None:
                    candidates = missing_hashes.get(entry['content_hash'], [])
                    if len(candidates)==1 and new_hash_counts[entry['content_hash']]==1 and candidates[0][1].get('summary_status')!='processing':
                        old_rel, old_row = candidates[0]
                        result = self.files.move_metadata(old_row['id'], entry['relative_path'], entry['filename'],
                                                         entry['size_bytes'], entry['modified_ns'], entry['content_hash'])
                        if result:
                            row = {**old_row, 'relative_path': entry['relative_path'], 'filename': entry['filename']}
                            file_id = row['id']; moved.add(old_rel)
                            previous=states.get(str(root/old_rel),previous)
                if row and self._valid_remote_summary(row, entry, previous):
                    if row.get('status') not in {'indexed','metadata_only'} and not self.files.restore_available(row['id'],entry['content_hash'],row['status']):
                        self._view({**entry,'file_id':file_id},'skipped',reason='concurrent_claim_lost')
                        continue
                    self.registry.save_state(entry, 'completed', remote_id=file_id,
                                             summary_model=settings.SUMMARY_MODEL, embedding_model=settings.EMBEDDING_MODEL,summary_version=settings.SUMMARY_VERSION)
                    self._view({**entry,'file_id':file_id}, 'skipped', reason='already_synchronized')
                    continue
                changed = row is None or row.get('content_hash') != entry['content_hash']
                failed = previous.get('status')=='failed' or (row and row.get('summary_status')=='failed')
                retryable = (previous.get('error') or {}).get('retryable')
                same_failed_content=(previous.get('status')=='failed' and previous.get('content_hash')==entry['content_hash']) or (row is not None and row.get('summary_status')=='failed' and not changed)
                if failed and same_failed_content and (not retry_failed or (retryable is False and not retry_permanent)):
                    self._view({**entry,'file_id':file_id}, 'skipped', reason='retry_required' if retryable is not False else 'nonretryable_failure')
                    continue
                waiting.append((entry, row, previous))
                if changed or (row and row.get('summary_status')=='completed'):
                    updates.append(dict(owner_id=self.files.owner_id, directory_id=remote_dir['id'],
                        relative_path=entry['relative_path'], filename=entry['filename'], extension=entry['extension'],
                        size_bytes=entry['size_bytes'], modified_ns=entry['modified_ns'], content_hash=entry['content_hash'],
                        status='indexed', summary_status='pending', summary_error=None,
                        **({'_expected': row} if row else {})))
            returned = {(row['directory_id'],row['relative_path']):row for row in self.files.upsert_metadata(updates)} if updates else {}
            for entry, old_row, previous in waiting:
                row = returned.get((remote_dir['id'], entry['relative_path'])) or old_row
                if row is None or ((old_row is None or old_row.get('content_hash') != entry['content_hash']
                        or old_row.get('summary_status')=='completed')
                        and (remote_dir['id'], entry['relative_path']) not in returned):
                    self._view({**entry,'file_id':old_row['id'] if old_row else None},'skipped',reason='concurrent_claim_lost')
                    continue
                file = {**entry, **row, 'path':entry['path'], 'root_id':entry['root_id'],
                        'max_size':entry['max_size'], 'allowed_extensions':entry['allowed_extensions']}
                queue.append(file)
            for entry in local:
                if entry['change']!='deleted':
                    continue
                row = remote.get(entry['relative_path'])
                if row and row.get('summary_status')=='processing':
                    continue
                if entry['relative_path'] in moved:
                    self._record(entry, 'skipped', reason='moved_file')
                elif row:
                    if row.get('status')!='error':
                        self.files.mark_unavailable(row['id'])
                    if entry['path'] in self._views:
                        continue
                    if states.get(entry['path'],{}).get('status')=='unavailable':
                        self._view({**entry,'file_id':row['id']}, 'skipped', reason='known_unavailable')
                    else:
                        self._record(entry, 'stale', StaleContentError('Local file content is missing or changed.'), remote_id=row['id'])
                else:
                    self._record(entry, 'skipped', reason='missing_file')
        return queue

    def _process_batch(self, file_batch, progress_callback=None):
        self._providers()
        if file_batch:
            check_processing_runtime(self.summarizer, self.embeddings)
        counts = dict(completed=0, failed=0, stale=0, skipped=0)
        for file in file_batch:
            _notify(progress_callback, safe_filename(file['filename']), 'processing')
            outcome = self.summary_service.process_file(file)
            self.report.outcomes.append(outcome)
            if outcome.get('journal_error'):
                self.report.run_errors.append(outcome['journal_error'])
            if file.get('path') is not None: self._views[file['path']]=outcome
            counts[outcome['outcome']] += 1
            if outcome['error'] and outcome['outcome']=='failed':
                _notify(progress_callback, outcome['error']['message'], 'error')
            _notify(progress_callback, safe_filename(file['filename']), outcome['outcome'])
        return counts

    def sync(self, progress_callback=None, retry_failed=False, dry_run=False, retry_permanent=False):
        if retry_permanent and not retry_failed:
            raise ValueError('retry_permanent requires retry_failed.')
        self.report, self.last_result = SyncReport(), None
        self._dry_run, self._queued_files = dry_run, []
        self._views = {}
        self.report.dry_run = dry_run
        result = {}
        lock, locked = None, False
        try:
            if not dry_run:
                lock = self.registry.lock(); lock.__enter__(); locked = True
                self.registry.initialize()
            _notify(progress_callback, None, 'starting')
            entries, metrics, _ = Scanner(self.registry,exclude_paths=(self.reports_dir,settings.REVIEWS_DIR,settings.PLANS_DIR)).scan()
            self.report.metrics = metrics
            self._queued_files = entries
            if dry_run:
                states = self.registry.states()
                for entry in entries:
                    state = states.get(entry['path'], {})
                    needed = entry.get('eligible') and not (entry['change']=='unchanged' and state.get('status')=='completed'
                              and state.get('summary_model')==settings.SUMMARY_MODEL and state.get('embedding_model')==settings.EMBEDDING_MODEL
                              and state.get('summary_version')==settings.SUMMARY_VERSION)
                    reason='dry_run_would_process' if needed else entry.get('reason') or 'already_synchronized'
                    if state.get('status')=='failed' and entry['change']=='unchanged':
                        retryable=(state.get('error') or {}).get('retryable')
                        if not retry_failed or (retryable is False and not retry_permanent):
                            reason='nonretryable_failure' if retryable is False else 'retry_required'
                    self._view(entry, 'skipped', reason=reason)
                return result
            self.registry.refresh(entries)
            self._classify_local(entries)
            if not entries:
                return result
            states=self.registry.states()
            if not any(entry.get('eligible') for entry in entries) and not any(states.get(entry['path'],{}).get('remote_id') for entry in entries):
                self.report.metrics['cloud_reconciliation']='not attempted: no eligible or previously linked local files'
                return result
            entries=[entry for entry in entries if entry.get('reason') not in {'sync_disabled','protected_directory','internal_metadata'}]
            if not entries: return result
            self._repositories()
            _notify(progress_callback, len(entries), 'scanned')
            self._queued_files = self._prepare(entries, retry_failed, retry_permanent)
            _notify(progress_callback,self.report.snapshot()['counts'],'classified')
            if self._queued_files:
                self._providers()
            for start in range(0, len(self._queued_files), self.batch_size):
                self._process_batch(self._queued_files[start:start+self.batch_size], progress_callback)
                _notify(progress_callback, self.report.snapshot()['counts'], 'batch_completed')
            return result
        except RegistryBusy:
            self.report.run_errors.append(describe_error('persistence', RegistryBusy('Workspace locked.')))
            raise
        except BaseException as error:
            stage = getattr(error, 'processing_stage', 'summarization') if isinstance(error,OllamaUnavailableError) else 'persistence'
            self.report.run_errors.append(describe_error(stage, error))
            recorded = set(self._views)
            try: previous_states=self.registry.states()
            except Exception: previous_states={}
            for entry in self._queued_files:
                if entry.get('path') in recorded:
                    continue
                if not isinstance(error, Exception):
                    self._view(entry,'skipped',reason='run_aborted_before_processing')
                    continue
                previous=previous_states.get(entry['path'],{})
                verified=(previous.get('status')=='completed' and previous.get('content_hash')==entry.get('content_hash')
                          and previous.get('root_id')==entry.get('root_id')
                          and previous.get('summary_model')==settings.SUMMARY_MODEL
                          and previous.get('embedding_model')==settings.EMBEDDING_MODEL
                          and previous.get('summary_version')==settings.SUMMARY_VERSION)
                if verified:
                    self._view({**entry,'file_id':previous.get('remote_id')},'skipped',reason='cloud_validation_unavailable')
                else:
                    try: self._record(entry,'failed',error,stage,reason='batch_setup_failed',remote_id=entry.get('id') or previous.get('remote_id'))
                    except Exception as status_error:
                        self._views[entry['path']]['status_error']=describe_error('persistence',status_error)
            raise
        finally:
            if locked:
                lock.__exit__(None,None,None)
            self.report.finished_at = now()
            snapshot = self.report.snapshot()
            result.update(snapshot['counts'], report=snapshot, report_path=None)
            if not dry_run:
                try:
                    result['report_path'] = str(self.report.save(self.reports_dir))
                except OSError as error:
                    result['report_save_error'] = describe_error('persistence', error)
            self.last_result = result
