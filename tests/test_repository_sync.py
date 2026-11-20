# Checks FileRepository remote metadata updates against fake provider query chains.
# Conflicting metadata or worker timestamps must not be overwritten by stale sync observations.

"""Owner filtering, paginated reads, conditional claims and metadata-only writes."""
import importlib
import sys
import types
import unittest
from unittest.mock import Mock, patch, call


def repository():
    auth=types.ModuleType('searchify.database.supabase_client'); auth.get_client=Mock(); auth.get_owner_id=Mock()
    with patch.dict(sys.modules,{'searchify.database.supabase_client':auth}):
        module=importlib.import_module('searchify.repositories.files')
    value=module.FileRepository.__new__(module.FileRepository)
    value.owner_id='owner'; value._directory_cache={}; value.client=Mock()
    query=Mock()
    for method in ('select','eq','in_','order','range','upsert','update','maybe_single'):
        getattr(query,method).return_value=query
    value.client.table.return_value=query
    return value,query


class RepositorySyncTests(unittest.TestCase):
    def test_pages_remain_owner_and_directory_scoped(self):
        value,query=repository()
        query.execute.side_effect=[types.SimpleNamespace(data=[{'id':str(i)} for i in range(500)]),types.SimpleNamespace(data=[{'id':'last'}])]
        self.assertEqual(len(value.list_files('directory')),501)
        query.range.assert_has_calls([call(0,499),call(500,999)])
        self.assertEqual(query.eq.call_args_list.count(call('owner_id','owner')),2)
        self.assertEqual(query.eq.call_args_list.count(call('directory_id','directory')),2)

    def test_claim_uses_hash_status_timestamp_and_owner(self):
        value,query=repository(); query.execute.return_value=types.SimpleNamespace(data=[{'id':'f'}])
        value.update_summary_status('f','completed',content_hash='HASH',expected_status='processing',expected_updated_at='STAMP')
        for args in [('id','f'),('owner_id','owner'),('content_hash','HASH'),('summary_status','processing'),('summary_updated_at','STAMP')]:
            query.eq.assert_any_call(*args)

    def test_directory_lookup_is_cached_within_a_run(self):
        value,query=repository(); query.execute.return_value=types.SimpleNamespace(data={'path':'~/project'})
        self.assertEqual(value.get_directory('d'),value.get_directory('d'))
        self.assertEqual(query.execute.call_count,1)
        query.eq.assert_any_call('owner_id','owner')

    def test_metadata_batches_use_one_consistent_conflict_key(self):
        value,query=repository()
        query.execute.return_value=types.SimpleNamespace(data=[{'id':'f'}])
        records=[{'owner_id':'owner','directory_id':'d','relative_path':str(i)+'.kt','content_hash':'HASH'} for i in range(51)]
        value.upsert_metadata(records)
        self.assertEqual(query.upsert.call_count,2)
        self.assertEqual([len(c.args[0]) for c in query.upsert.call_args_list],[50,1])
        for c in query.upsert.call_args_list:
            self.assertEqual(c.kwargs['on_conflict'],'directory_id,relative_path')
            self.assertFalse(any('content' in row or 'chunks' in row for row in c.args[0]))

    def test_unavailable_metadata_retains_completed_artifacts_and_owner_scope(self):
        value,query=repository(); query.execute.return_value=types.SimpleNamespace(data=[{'id':'f'}])
        value.mark_unavailable('f')
        self.assertEqual(query.update.call_args.args[0]['status'],'error')
        self.assertNotIn('summary_status',query.update.call_args.args[0])
        query.eq.assert_any_call('owner_id','owner')

    def test_existing_metadata_refresh_is_conditional_and_never_uploads_expected_fields(self):
        value, query = repository(); query.execute.return_value=types.SimpleNamespace(data=[])
        expected = dict(id='f', content_hash='OLD', summary_status='pending', summary_updated_at='STAMP')
        self.assertEqual(value.upsert_metadata([dict(owner_id='owner', directory_id='d',
            relative_path='file.kt', content_hash='NEW', _expected=expected)]), [])
        query.upsert.assert_not_called()
        for args in [('owner_id','owner'),('id','f'),('content_hash','OLD'),
                     ('summary_status','pending'),('summary_updated_at','STAMP')]:
            query.eq.assert_any_call(*args)
        self.assertNotIn('_expected', query.update.call_args.args[0])

    def test_processing_acquisition_requires_eligible_remote_status(self):
        value, query = repository(); query.in_.return_value=query
        query.execute.return_value=types.SimpleNamespace(data=[])
        value.update_summary_status('f','processing',content_hash='HASH',expected_status='pending')
        query.in_.assert_called_once_with('status',['indexed','metadata_only'])
