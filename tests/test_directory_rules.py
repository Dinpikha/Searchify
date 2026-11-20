# Uses temporary registered roots to verify per-directory Scanner policies and CLI
# unregistration.
# Checks that profiles, size limits, and nested/protected paths cannot bypass extraction
# boundaries.

"""Directory-scoped policies, explicit unregistration and extraction limits."""
import io
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
from searchify.core.file_registry import FileRegistry
from searchify.core.scanner import DirectoryRules, Scanner
from searchify.cli import directories


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve(); self.registry=FileRegistry(self.base/'index.db')

    def test_android_and_document_rules_do_not_leak_between_directories(self):
        android=self.base/'android'; documents=self.base/'documents'; android.mkdir(); documents.mkdir()
        for root in (android,documents):
            (root/'build').mkdir(); (root/'build'/'notes.txt').write_text('notes')
        (android/'build.gradle.kts').write_text('plugins { }')
        (android/'local.properties').write_text('SECRET_LOCATION')
        (documents/'notes.pdf').write_bytes(b'PDF')
        self.registry.register(android,DirectoryRules.preset('android'))
        self.registry.register(documents,DirectoryRules.preset('documents'))
        entries,_,excluded=Scanner(self.registry).scan()
        by_name={row['path']:row for row in entries}
        self.assertNotIn(str(android/'build'/'notes.txt'),by_name)
        self.assertTrue(by_name[str(documents/'build'/'notes.txt')]['eligible'])
        self.assertTrue(by_name[str(android/'build.gradle.kts')]['eligible'])
        self.assertEqual(by_name[str(android/'local.properties')]['reason'],'protected_file')
        self.assertEqual(by_name[str(documents/'notes.pdf')]['reason'],'unsupported_file_type')

    def test_size_patterns_and_disabled_sync(self):
        root=self.base/'project'; root.mkdir()
        (root/'one.kt').write_text('longer than four')
        (root/'two.kt').write_text('yes')
        rules=DirectoryRules(include=['one.*'],max_size=4)
        root_id=self.registry.register(root,rules)
        entries,_,_=Scanner(self.registry).scan()
        reasons={row['filename']:row['reason'] for row in entries}
        self.assertEqual(reasons['one.kt'],'size_restriction')
        self.assertEqual(reasons['two.kt'],'excluded_pattern')
        rules.sync_enabled=False; self.registry.save_rules(root_id,rules)
        entries,_,_=Scanner(self.registry).scan(); self.assertEqual(entries[0]['reason'],'sync_disabled')

    def test_unregistration_requires_confirmation_and_never_removes_originals(self):
        root=self.base/'project'; root.mkdir(); source=root/'file.kt'; source.write_text('important')
        self.registry.register(root)
        args=types.SimpleNamespace(directory=str(root),yes=False)
        with patch('searchify.cli.directories.local_paths',return_value=(self.registry.path,self.base/'reports')), \
             patch('builtins.input',return_value='no'), redirect_stdout(io.StringIO()):
            self.assertEqual(directories.remove(args),0)
        self.assertEqual(len(self.registry.directories()),1)
        args.yes=True
        with patch('searchify.cli.directories.local_paths',return_value=(self.registry.path,self.base/'reports')),redirect_stdout(io.StringIO()):
            self.assertEqual(directories.remove(args),0)
        self.assertEqual(self.registry.directories(),[])
        self.assertEqual(source.read_text(),'important')

    def test_protected_registered_root_and_binary_spoofing_are_not_extracted(self):
        protected=self.base/'.aws'; protected.mkdir(); (protected/'config.json').write_text('PRIVATE_CREDENTIAL')
        self.registry.register(protected)
        source=self.base/'source'; source.mkdir(); (source/'binary.kt').write_bytes(b'\xff\xfe\xfa')
        self.registry.register(source)
        entries,_,_=Scanner(self.registry).scan()
        self.assertEqual(next(e['reason'] for e in entries if e.get('synthetic')),'protected_directory')
        self.assertEqual(next(e['reason'] for e in entries if e['filename']=='binary.kt'),'empty_or_binary')
        self.assertFalse(any(e['eligible'] for e in entries))

    def test_unsupported_extensions_cannot_be_enabled_without_extractor(self):
        with self.assertRaises(ValueError): DirectoryRules.from_dict({'extensions':['.png']})
