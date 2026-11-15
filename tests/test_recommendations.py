"""Evidence-based recommendations and organization plans never execute actions."""
import json
import os
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from searchify.core.file_registry import FileRegistry
from searchify.services.review_service import ReviewService, git_metadata
from searchify.services.organization_service import OrganizationService


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve(); self.root=self.base/'project'; self.root.mkdir()
        self.registry=FileRegistry(self.base/'index.db'); self.registry.register(self.root)

    def test_inactivity_is_evidence_not_a_deletion_recommendation(self):
        marker=self.root/'pyproject.toml'; marker.write_text('PROJECT_SECRET_CONTENT')
        source=self.root/'code.py'; source.write_text('SOURCE_CODE_MUST_STAY_LOCAL')
        old=time.time()-100*86400
        for path in (marker,source): os.utime(path,(old,old))
        review=ReviewService(self.registry).review()
        inactive=[r for r in review['recommendations'] if r['category']=='inactive_project']
        self.assertEqual(len(inactive),1); self.assertTrue(inactive[0]['approval_required'])
        self.assertIn('does not imply',inactive[0]['reason'])
        self.assertNotIn('SOURCE_CODE_MUST_STAY_LOCAL',json.dumps(review))
        source.write_text('recent work')
        self.assertFalse(any(r['category']=='inactive_project' for r in ReviewService(self.registry).review()['recommendations']))

    def test_uninspected_git_worktree_does_not_produce_an_inactivity_claim(self):
        (self.root/'.git').write_text('gitdir: /outside/not-inspected')
        source=self.root/'code.py'; source.write_text('old source'); old=time.time()-100*86400; os.utime(source,(old,old))
        review=ReviewService(self.registry).review()
        self.assertFalse(any(item['category']=='inactive_project' for item in review['recommendations']))
        self.assertIn('not inspected',review['projects'][0]['git']['status'])

    def test_partial_coverage_never_asserts_project_inactivity(self):
        (self.root/'pyproject.toml').write_text('old')
        for i in range(3): (self.root/f'{i}.txt').write_text('old')
        old=time.time()-100*86400
        for path in self.root.iterdir(): os.utime(path,(old,old))
        review=ReviewService(self.registry,max_files=1).review()
        self.assertFalse(review['coverage'][0]['complete'])
        self.assertFalse(any(r['category']=='inactive_project' for r in review['recommendations']))

    def test_large_generated_folders_and_verified_duplicate_candidates(self):
        (self.root/'build').mkdir(); (self.root/'build'/'artifact.bin').write_bytes(b'A'*20)
        for name in ('a.txt','b.txt'): (self.root/name).write_text('DUPLICATE_PRIVATE_TEXT')
        review=ReviewService(self.registry,large_generated_bytes=10).review()
        categories={r['category'] for r in review['recommendations']}
        self.assertIn('large_generated_folder',categories); self.assertIn('duplicate_files',categories)
        self.assertNotIn('DUPLICATE_PRIVATE_TEXT',json.dumps(review))
        self.assertTrue(all(not r['backup']['verified'] for r in review['recommendations']))

    def test_organization_is_proposal_only_and_protects_config(self):
        (self.root/'photo.png').write_bytes(b'PNG_PRIVATE')
        (self.root/'.env').write_text('API_KEY=PRIVATE')
        (self.root/'README.md').write_text('important configuration')
        before={path.name:path.read_bytes() for path in self.root.iterdir()}
        plan=OrganizationService(self.registry).propose()
        self.assertEqual(len(plan['operations']),1)
        operation=plan['operations'][0]
        self.assertTrue(operation['approval_required']); self.assertFalse(operation['executable'])
        self.assertIn('/Images/photo.png',operation['destination'])
        self.assertTrue(operation['verification']); self.assertTrue(operation['rollback'])
        self.assertFalse(plan['execution_supported'])
        self.assertEqual(before,{path.name:path.read_bytes() for path in self.root.iterdir()})
        self.assertNotIn('PNG_PRIVATE',json.dumps(plan)); self.assertNotIn('API_KEY=PRIVATE',json.dumps(plan))

    def test_dirty_git_and_missing_backup_are_explicit_blockers(self):
        (self.root/'notes.txt').write_text('notes')
        with patch('searchify.services.organization_service.git_metadata',return_value={'status':'checked','dirty':True,'last_commit':None}):
            plan=OrganizationService(self.registry).propose()
        blockers=' '.join(plan['operations'][0]['blockers'])
        self.assertIn('uncommitted',blockers); self.assertIn('Backup',blockers)

    def test_git_inspection_disables_optional_writes_and_never_stores_status_paths(self):
        (self.root/'.git').mkdir()
        responses=[types.SimpleNamespace(returncode=0,stdout=b'?? PRIVATE_FILENAME\x00'),
                   types.SimpleNamespace(returncode=0,stdout=b'1234567890\n')]
        with patch('searchify.services.review_service.subprocess.run',side_effect=responses) as run:
            info=git_metadata(self.root)
        self.assertTrue(info['dirty']); self.assertEqual(info['last_commit'],1234567890)
        self.assertNotIn('PRIVATE_FILENAME',str(info))
        self.assertEqual(run.call_args.kwargs['env']['GIT_OPTIONAL_LOCKS'],'0')
        self.assertIn('--no-optional-locks',run.call_args.args[0])

    def test_project_source_layout_and_symlinks_are_not_reorganized(self):
        (self.root/'package.json').write_text('{}'); (self.root/'image.png').write_bytes(b'png')
        outside=self.base/'outside'; outside.mkdir(); (outside/'outside.txt').write_text('outside')
        (self.root/'linked').symlink_to(outside,target_is_directory=True)
        plan=OrganizationService(self.registry).propose()
        self.assertEqual(plan['operations'],[])
        self.assertTrue((outside/'outside.txt').exists())
