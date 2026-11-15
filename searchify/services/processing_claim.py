"""Worker-owned, renewable claims using the existing remote timestamp CAS token.

Callers hold the local workspace lock. A remote token is never adopted: a new
worker must conditionally release an expired or proven-interrupted claim first.
"""
from datetime import datetime, timedelta, timezone
import threading
import uuid

LEASE_SECONDS = 3600
HEARTBEAT_SECONDS = 60


def expired(stamp):
    try:
        value = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        return value.tzinfo is not None and value < datetime.now(timezone.utc) - timedelta(seconds=LEASE_SECONDS)
    except (AttributeError, TypeError, ValueError):
        # An invalid timestamp does not establish that a worker has stopped.
        return False


def eligible_entry(files, registry, file):
    from pathlib import Path
    from searchify.indexer import resolve_indexed_file, LocalContentError
    from searchify.core.scanner import root_is_unredirected, signature, PROTECTED_DIRS
    directory = files.get_directory(file['directory_id'])
    if not directory or not directory.get('enabled', True):
        return None
    root = Path(directory['path']).expanduser().absolute()
    if not root_is_unredirected(root):
        return None
    try:
        path = resolve_indexed_file(registry.path, directory['path'], file['relative_path'], file.get('content_hash'))
        if any(part in PROTECTED_DIRS for part in path.parts):
            return None
        metadata = registry.metadata().get(str(path))
        if not metadata:
            return None
        rules = registry.rules(metadata['root_id'])
        return {**file, 'path':str(path), 'root_id':metadata['root_id'],
                'signature':signature(path.stat()), 'max_size':rules.max_size,
                'allowed_extensions':rules.extensions}
    except (LocalContentError, OSError):
        return None


def recoverable(registry, file):
    stamp = file.get('summary_updated_at')
    state = registry.states().get(file.get('path'), {})
    interrupted = (state.get('status') == 'processing' and state.get('claim_owner')
                   and state.get('remote_id') == file['id']
                   and state.get('root_id') == file.get('root_id')
                   and state.get('claim_timestamp') == stamp)
    return bool(stamp and (interrupted or expired(stamp)))


def recover_claim(files, registry, file):
    """Requires the workspace lock and a currently eligible local candidate."""
    if file.get('summary_status') != 'processing':
        return file
    file = eligible_entry(files, registry, file)
    if file is None:
        return None
    stamp = file.get('summary_updated_at')
    if not recoverable(registry, file):
        return None
    rows = files.update_summary_status(file['id'], 'pending', content_hash=file['content_hash'],
                expected_status='processing', expected_updated_at=stamp)
    return {**file, **rows[0], 'summary_status': 'pending'} if rows else None


class ProcessingClaim:
    def __init__(self, files, file, journal):
        self.files, self.file, self.journal = files, file, journal
        self.owner = uuid.uuid4().hex
        self.mutex = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.lost = False

    def acquire(self):
        if self.file.get('summary_status') not in {'pending', 'failed'}:
            return False
        options = dict(content_hash=self.file['content_hash'], expected_status=self.file['summary_status'])
        if self.file.get('summary_updated_at'):
            options['expected_updated_at'] = self.file['summary_updated_at']
        rows = self.files.update_summary_status(self.file['id'], 'processing', **options)
        if not rows:
            return False
        self.file['claim_timestamp'] = rows[0].get('summary_updated_at')
        self.journal('processing', self)
        self.thread = threading.Thread(target=self._heartbeat, name='searchify-claim-heartbeat', daemon=True)
        self.thread.start()
        return True

    def transition(self, status, error=None):
        with self.mutex:
            if self.lost:
                return []
            rows = self.files.update_summary_status(self.file['id'], status, error=error,
                content_hash=self.file['content_hash'], expected_status='processing',
                expected_updated_at=self.file['claim_timestamp'])
            if not rows:
                self.lost = True
            elif status == 'processing':
                self.file['claim_timestamp'] = rows[0].get('summary_updated_at')
                self.journal('processing', self)
            return rows

    def _heartbeat(self):
        while not self.stop.wait(HEARTBEAT_SECONDS):
            try:
                if not self.transition('processing'):
                    return
            except Exception:
                # Fail closed; no persistence after renewal becomes uncertain.
                self.lost = True
                return

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
