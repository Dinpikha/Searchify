"""Root-retargeting regressions; every filesystem change is in a temporary fixture."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from searchify.core.file_registry import FileRegistry
from searchify.core.scanner import Scanner
from searchify.services.review_service import ReviewService, git_metadata, inventory


class RootScopingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.parent = self.base / 'container'
        self.root = self.parent / 'project'
        self.root.mkdir(parents=True)
        (self.root / 'source.kt').write_text('fun source() = 1')
        self.registry = FileRegistry(self.base / 'index.db')
        self.root_id = self.registry.register(self.root)
        entries, _, _ = Scanner(self.registry).scan()
        self.registry.refresh(entries)
        self.original_metadata = self.registry.metadata()

    def assert_rejected_without_traversal(self):
        with patch('searchify.core.scanner.os.walk', side_effect=AssertionError('unsafe traversal')), \
             patch('searchify.core.scanner.extract_content', side_effect=AssertionError('unsafe extraction')):
            entries, metrics, _ = Scanner(self.registry).scan()
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]['synthetic'])
        self.assertFalse(entries[0]['eligible'])
        self.assertEqual(entries[0]['reason'], 'incomplete_scan')
        self.assertEqual(entries[0]['root_error'], 'registered_root_boundary_unverified')
        self.assertEqual(metrics['total_discovered'], 0)
        self.assertEqual(metrics['deleted'], 0)
        self.assertEqual(self.registry.metadata(), self.original_metadata)

        with patch('searchify.services.review_service.os.walk', side_effect=AssertionError('unsafe inventory')), \
             patch('searchify.services.review_service.byte_hash', side_effect=AssertionError('unsafe read')), \
             patch('searchify.services.review_service.subprocess.run', side_effect=AssertionError('unsafe Git access')):
            files, projects, generated, complete = inventory(self.root)
            self.assertEqual(files, [])
            self.assertEqual(projects, set())
            self.assertEqual(dict(generated), {})
            self.assertFalse(complete)
            review = ReviewService(self.registry).review()
            self.assertFalse(review['coverage'][0]['complete'])
            self.assertEqual(review['recommendations'], [])
            self.assertEqual(git_metadata(self.root)['status'], 'Git root boundary unverified')

    def test_root_replaced_with_external_directory_symlink(self):
        saved = self.parent / 'saved-project'
        self.root.rename(saved)
        external = self.base / 'external'
        external.mkdir()
        (external / 'outside.kt').write_text('OUTSIDE_CONTENT_MUST_NOT_BE_READ')
        (external / '.git').mkdir()
        self.root.symlink_to(external, target_is_directory=True)
        self.assert_rejected_without_traversal()

    def test_root_replaced_with_link_into_original_directory_is_also_rejected(self):
        inside = self.root / 'inside'
        inside.mkdir()
        (inside / 'inside.kt').write_text('fun inside() = 2')
        saved = self.parent / 'saved-project'
        self.root.rename(saved)
        self.root.symlink_to(saved / 'inside', target_is_directory=True)
        self.assert_rejected_without_traversal()

    def test_dangling_replacement_root_is_rejected(self):
        self.root.rename(self.parent / 'saved-project')
        self.root.symlink_to(self.base / 'does-not-exist', target_is_directory=True)
        self.assert_rejected_without_traversal()

    def test_replaced_ancestor_cannot_redirect_a_non_symlink_root(self):
        self.parent.rename(self.base / 'saved-container')
        external = self.base / 'external-container'
        (external / 'project').mkdir(parents=True)
        (external / 'project' / 'outside.kt').write_text('OUTSIDE_CONTENT_MUST_NOT_BE_READ')
        self.parent.symlink_to(external, target_is_directory=True)
        self.assertFalse(self.root.is_symlink())
        self.assert_rejected_without_traversal()

    def test_normal_registered_root_and_existing_child_symlink_protections(self):
        entries, _, _ = Scanner(self.registry).scan()
        self.assertEqual([entry['filename'] for entry in entries if entry['eligible']], ['source.kt'])
        files, _, _, complete = inventory(self.root)
        self.assertTrue(complete)
        self.assertEqual([row['path'] for row in files], [self.root / 'source.kt'])

        external = self.base / 'external'
        external.mkdir()
        (external / 'outside.kt').write_text('OUTSIDE_CONTENT_MUST_NOT_BE_READ')
        (self.root / 'linked-directory').symlink_to(external, target_is_directory=True)
        (self.root / 'linked.kt').symlink_to(external / 'outside.kt')
        entries, _, _ = Scanner(self.registry).scan()
        self.assertEqual([entry['filename'] for entry in entries if entry['eligible']], ['source.kt'])
        linked = next(entry for entry in entries if entry['filename'] == 'linked.kt')
        self.assertEqual(linked['reason'], 'symlink')
        files, _, _, complete = inventory(self.root)
        self.assertTrue(complete)
        self.assertEqual([row['path'] for row in files], [self.root / 'source.kt'])

    def test_link_to_another_registered_root_does_not_duplicate_its_inventory(self):
        external = self.base / 'other-registered'
        external.mkdir()
        (external / 'other.kt').write_text('fun other() = 2')
        external_id = self.registry.register(external)
        self.root.rename(self.parent / 'saved-project')
        self.root.symlink_to(external, target_is_directory=True)
        entries, _, _ = Scanner(self.registry).scan()
        eligible = [entry for entry in entries if entry['eligible']]
        self.assertEqual(len(eligible), 1)
        self.assertEqual(eligible[0]['root_id'], external_id)
        self.assertEqual(eligible[0]['path'], str(external / 'other.kt'))
        self.assertFalse(inventory(self.root)[3])
        self.assertTrue(inventory(external)[3])

    def test_explicit_registration_still_stores_the_canonical_directory(self):
        external = self.base / 'explicit-target'
        external.mkdir()
        (external / 'explicit.kt').write_text('fun explicit() = 3')
        alias = self.base / 'registration-alias'
        alias.symlink_to(external, target_is_directory=True)
        identifier = self.registry.register(alias)
        registered = next(row for row in self.registry.directories() if row['id'] == identifier)
        self.assertEqual(registered['path'], str(external))
        entries, _, _ = Scanner(self.registry).scan()
        self.assertEqual(sum(entry.get('path') == str(external / 'explicit.kt') and entry['eligible'] for entry in entries), 1)
