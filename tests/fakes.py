"""In-memory owner-scoped provider fixtures; never access network or Keychain."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import uuid


class Files:
    owner_id = 'test-owner'

    def __init__(self):
        self.directories, self.rows, self.payloads = {}, {}, []
        self.counter = 0

    def list_directories(self):
        return deepcopy(list(self.directories.values()))

    def ensure_directory(self, path):
        from searchify.services.sync_service import display_path
        key = display_path(path)
        for row in self.directories.values():
            if row['path']==key:
                return deepcopy(row)
        self.counter += 1
        row = dict(id='dir-'+str(self.counter), path=key, enabled=True)
        self.directories[row['id']] = row
        return deepcopy(row)

    def get_directory(self, identifier):
        return deepcopy(self.directories.get(identifier))

    def list_files(self, identifier):
        return deepcopy([row for row in self.rows.values() if row['directory_id']==identifier])

    def get_file(self, identifier):
        return deepcopy(self.rows.get(identifier))

    def upsert_metadata(self, records):
        self.payloads.extend(deepcopy(records))
        result = []
        for value in records:
            row = next((row for row in self.rows.values() if row['directory_id']==value['directory_id'] and row['relative_path']==value['relative_path']), None)
            value = dict(value)
            expected = value.pop('_expected', None)
            if expected is not None:
                if not row or any(row.get(key) != expected.get(key) for key in ('id','content_hash','summary_status','summary_updated_at')):
                    continue
            elif row is not None:
                continue
            row = {**(row or {'id': str(uuid.uuid4())}), **value}
            self.rows[row['id']] = row
            result.append(deepcopy(row))
        return result

    def update_summary_status(self, identifier, status, error=None, content_hash=None,
                              expected_status=None, expected_updated_at=None):
        row = self.rows.get(identifier)
        if (not row or ((status=='processing' or expected_status=='processing') and row.get('status') not in {'indexed','metadata_only'})
                or (content_hash is not None and row['content_hash']!=content_hash)
                or (expected_status is not None and row['summary_status']!=expected_status)
                or (expected_updated_at is not None and row.get('summary_updated_at')!=expected_updated_at)):
            return []
        row.update(summary_status=status, summary_error=error,
                   summary_updated_at=datetime.now(timezone.utc).isoformat())
        return [deepcopy(row)]

    def mark_skipped(self, identifier, reason):
        self.rows[identifier].update(status='skipped', summary_error=reason)

    def mark_unavailable(self, identifier):
        self.rows[identifier].update(status='error')

    def restore_available(self, identifier, content_hash, previous_status):
        row=self.rows[identifier]
        if row['content_hash']!=content_hash or row['status']!=previous_status or row['summary_status']!='completed': return []
        row.update(status='indexed',summary_error=None)
        return [deepcopy(row)]

    def move_metadata(self, identifier, relative_path, filename, size_bytes, modified_ns, content_hash):
        row=self.rows[identifier]
        if row['content_hash']!=content_hash:
            return []
        row.update(relative_path=relative_path, filename=filename, extension=Path(filename).suffix.lower(), size_bytes=size_bytes, modified_ns=modified_ns)
        return [deepcopy(row)]


class Summaries:
    def __init__(self):
        self.rows, self.vectors = {}, {}

    def upsert_summary(self, file_id, content_hash, summary, summary_data, model_name, version=1):
        row = dict(id=self.rows.get(file_id,{}).get('id', str(uuid.uuid4())), file_id=file_id,
                   source_content_hash=content_hash, summary=summary, model_name=model_name, summary_version=version)
        self.rows[file_id] = row
        return deepcopy(row)

    def save_embedding(self, summary_id, embedding, model_name):
        row = dict(summary_id=summary_id, embedding=embedding, model_name=model_name)
        self.vectors[summary_id]=row
        return [deepcopy(row)]

    def get_summary(self, file_id):
        return deepcopy(self.rows.get(file_id))

    def get_summary_embedding(self, summary_id):
        return deepcopy(self.vectors.get(summary_id))
