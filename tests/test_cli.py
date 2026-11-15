"""Unified command discovery and side-effect-free startup."""
from contextlib import closing, redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from searchify.cli.app import main
from searchify.cli.workspace import read_workspace


class CommandTests(unittest.TestCase):
    def invoke(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(argv)
            except SystemExit as error:
                code = error.code
        return code, out.getvalue(), err.getvalue()

    def test_root_and_help_command_show_all_commands(self):
        for argv in (['-h'], ['--help'], ['help']):
            with self.subTest(argv=argv):
                code, output, _ = self.invoke(argv)
                self.assertEqual(code, 0)
                for command in ('sync', 'summarize', 'status', 'report', 'doctor', 'dirs',
                                'search', 'review', 'organize', 'add', 'index'):
                    self.assertIn(command, output)

    def test_every_command_has_help(self):
        for argv in (['sync', '-h'], ['summarize', '-h'], ['report', '-h'], ['status', '-h'],
                     ['doctor', '-h'], ['dirs', '-h'], ['dirs', 'add', '-h'],
                     ['dirs', 'remove', '-h'], ['search', '-h'], ['review', '-h'], ['organize', '-h']):
            code, output, _ = self.invoke(argv)
            self.assertEqual(code, 0, argv)
            self.assertIn('usage:', output)

    def test_nested_help_matches_subcommand_help(self):
        first = self.invoke(['help', 'dirs', 'add'])
        second = self.invoke(['dirs', 'add', '-h'])
        self.assertEqual(first, second)

    def test_invalid_commands_arguments_and_limits(self):
        for argv in (['unknown'], ['help', 'unknown'], ['dirs', 'unknown'], ['dirs', 'add'],
                     ['search'], ['search', 'query', '--limit', '0'], ['sync', '--batch-size', '-1']):
            code, _, error = self.invoke(argv)
            self.assertEqual(code, 2, argv)
            self.assertIn('error:', error)

    def test_recommendation_commands_route_to_plan_only_services(self):
        for name in ('review','organize'):
            with patch('searchify.cli.'+name+'.run', return_value=0) as run:
                self.assertEqual(self.invoke([name])[0],0)
                run.assert_called_once()
        self.assertEqual(self.invoke(['organize','--apply'])[0],2)

    def test_sync_dispatch_preserves_existing_options(self):
        with patch('searchify.cli.sync.run', return_value=1) as run:
            code, _, _ = self.invoke(['sync', '--batch-size', '7', '--retry-failed', '--verbose'])
        self.assertEqual(code, 1)
        args = run.call_args.args[0]
        self.assertEqual(args.batch_size, 7)
        self.assertTrue(args.retry_failed)
        self.assertTrue(args.verbose)

    def test_summarize_dispatch_preserves_existing_options(self):
        with patch('searchify.cli.summarize.run', return_value=0) as run:
            self.assertEqual(self.invoke(['summarize', '--limit', '3', '--retry-failed'])[0], 0)
        self.assertEqual(run.call_args.args[0].limit, 3)
        self.assertTrue(run.call_args.args[0].retry_failed)

    def test_legacy_commands_reuse_existing_functions(self):
        with patch('searchify.indexer.index_all') as index:
            self.assertEqual(self.invoke(['index'])[0], 0)
            index.assert_called_once()
        with patch('searchify.indexer.search_files') as search:
            self.assertEqual(self.invoke(['search', 'some text', '--limit', '4'])[0], 0)
            search.assert_called_once_with('some text', 4)
        with tempfile.TemporaryDirectory() as folder, patch('searchify.indexer.register_directory') as register:
            self.assertEqual(self.invoke(['add', folder])[0], 0)
            register.assert_called_once_with(Path(folder).resolve())
        with patch('searchify.cli.directories.FileRegistry.register', return_value=1) as register:
            self.assertEqual(self.invoke(['dirs','add','/registered'])[0],0)
            register.assert_called_once()

    def test_semantic_search_reuses_existing_search_service(self):
        module = types.ModuleType('searchify.services.search_service')
        module.SearchService = Mock()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve(); path=root/'file.kt'; path.write_text('local source')
            module.SearchService.return_value.search.return_value = [
                {'path':str(path), 'summary':'Derived description','similarity':0.9}
            ]
            with patch.dict(sys.modules, {'searchify.services.search_service':module}), \
                 patch('searchify.core.file_registry.FileRegistry.directories',return_value=[{'id':1,'path':str(root)}]), \
                 patch('searchify.core.file_registry.FileRegistry.rules',return_value=__import__('searchify.core.scanner',fromlist=['DirectoryRules']).DirectoryRules()):
                code,output,_=self.invoke(['search','Kotlin helper','--semantic','--limit','2'])
        self.assertEqual(code,0)
        module.SearchService.return_value.search.assert_called_once_with('Kotlin helper',limit=2)
        self.assertIn('similarity=0.9000',output)

    def test_invalid_directory_has_failure_exit_code(self):
        with tempfile.TemporaryDirectory() as folder:
            code, _, error = self.invoke(['dirs', 'add', str(Path(folder) / 'missing')])
        self.assertEqual(code, 1)
        self.assertIn('does not exist', error)

    def test_interrupt_and_dependency_error_exit_codes(self):
        with patch('searchify.cli.sync.run', side_effect=KeyboardInterrupt):
            self.assertEqual(self.invoke(['sync'])[0], 130)
        with patch('searchify.cli.sync.run', side_effect=ImportError('PRIVATE_DATA')):
            code, _, error = self.invoke(['sync'])
        self.assertEqual(code, 1)
        self.assertIn('python -m pip install -e .', error)
        self.assertNotIn('PRIVATE_DATA', error)

    def test_discovery_does_not_import_operational_dependencies(self):
        script = '''
import sys
from searchify.cli.app import main
assert main(['help']) == 0
for name in ('searchify.services.sync_service', 'searchify.supabase_client',
             'searchify.ai.ollama_client', 'keyring', 'dotenv', 'rich'):
    assert name not in sys.modules, name
'''
        result = subprocess.run([sys.executable, '-B', '-c', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_summarize_retry_override_and_interrupt_exit_codes(self):
        with patch('searchify.services.summary_service.SummaryService') as service:
            process = service.return_value.process_pending
            self.assertEqual(self.invoke(['summarize','--retry-permanent'])[0], 2)
            process.assert_not_called()
            process.return_value = dict(completed=1, failed=0, stale=0, skipped=0, journal_errors=0)
            self.assertEqual(self.invoke(['summarize','--retry-failed','--retry-permanent','--recover-processing'])[0], 0)
            process.assert_called_once_with(50, True, True, retry_permanent=True)
            process.side_effect = KeyboardInterrupt()
            self.assertEqual(self.invoke(['summarize'])[0], 130)



class WorkspaceTests(unittest.TestCase):
    invoke = CommandTests.invoke

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.db = self.root / 'index.db'
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript('CREATE TABLE directories(id INTEGER, path TEXT); CREATE TABLE files(path TEXT, root_id INTEGER);')
            conn.execute('INSERT INTO directories VALUES(1, ?)', (str(self.root),))
            conn.execute('INSERT INTO files VALUES(?, 1)', (str(self.root / 'file.kt'),))
        self.paths = patch('searchify.cli.workspace.local_paths', return_value=(self.db, self.root / 'reports'))
        providers=patch('searchify.services.health_service.HealthService._providers', return_value={'ollama':{'status':'not checked (test)'},'supabase':{'status':'not checked (test)'}})
        providers.start(); self.addCleanup(providers.stop)
        self.paths.start()
        self.addCleanup(self.paths.stop)

    def test_startup_is_read_only_and_never_processes_files(self):
        import searchify.indexer as indexer
        before = self.db.read_bytes()
        with patch.object(indexer, 'initialize', side_effect=AssertionError('initialization')), \
             patch.object(indexer, 'connect', side_effect=AssertionError('write connection')), \
             patch.object(indexer, 'extract_content', side_effect=AssertionError('extraction')):
            code, output, _ = self.invoke([])
        self.assertEqual(code, 0)
        self.assertIn('SEARCHIFY', output)
        self.assertIn('Registered directories: 1', output)
        self.assertIn('Indexed file records: 1', output)
        self.assertIn('not checked', output)
        self.assertNotIn('LOCAL AI READY', output)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse((self.root / 'reports').exists())

    def test_status_and_dirs_use_actual_local_counts(self):
        code, output, _ = self.invoke(['status'])
        self.assertEqual(code, 0)
        self.assertIn('Registered directories: 1', output)
        self.assertIn('Indexed file records: 1', output)
        self.assertEqual(self.invoke(['dirs'])[0], 0)

    def test_missing_index_is_not_created(self):
        missing = self.root / 'missing.db'
        snapshot = read_workspace(missing)
        self.assertEqual(snapshot['database'], 'not created')
        self.assertIsNone(snapshot['indexed'])
        self.assertFalse(missing.exists())

    def test_report_history_reads_existing_json_without_writes(self):
        directory = self.root / 'reports'
        directory.mkdir()
        counts = dict(total=1, completed=0, failed=1, stale=0, skipped=0)
        for number in (1, 2):
            (directory / f'sync-20261114-{number}.json').write_text(json.dumps({'counts': counts}))
        code, output, _ = self.invoke(['report'])
        self.assertEqual(code, 0)
        self.assertIn('sync-20261114-2.json', output)
        self.assertNotIn('sync-20261114-1.json', output)
        code, output, _ = self.invoke(['report', '--history'])
        self.assertEqual(code, 0)
        self.assertIn('sync-20261114-1.json', output)
        self.assertIn('failed: 1', output)
        self.assertEqual(len(list(directory.iterdir())), 2)

    def test_no_reports_and_corrupt_report(self):
        code, output, _ = self.invoke(['report'])
        self.assertEqual(code, 0)
        self.assertIn('No saved', output)
        directory = self.root / 'reports'
        directory.mkdir()
        (directory / 'sync-corrupt.json').write_text('PRIVATE_CONTENT_NOT_JSON')
        code, output, _ = self.invoke(['report'])
        self.assertEqual(code, 1)
        self.assertNotIn('PRIVATE_CONTENT_NOT_JSON', output)


if __name__ == '__main__':
    unittest.main()
