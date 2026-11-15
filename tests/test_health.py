"""Read-only dashboard diagnostics with deterministic provider responses."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from searchify.core.file_registry import FileRegistry
from searchify.services.health_service import HealthService


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve(); self.root=self.base/'source'; self.root.mkdir()
        self.registry=FileRegistry(self.base/'index.db'); self.registry.register(self.root)
        self.service=HealthService(self.registry.path,self.base/'reports')

    def test_metadata_scan_never_extracts_or_authenticates(self):
        (self.root/'new.kt').write_text('PRIVATE_CONTENT')
        before=self.registry.path.read_bytes()
        with patch('searchify.core.scanner.extract_content', side_effect=AssertionError('content read')), \
             patch.object(self.service,'authentication',return_value={'status':'not checked'}) as auth:
            snapshot=self.service.snapshot(scan=True,offline=True)
        self.assertEqual(snapshot['workspace']['new'],1)
        auth.assert_called_once_with(False)
        self.assertEqual(self.registry.path.read_bytes(),before)
        self.assertFalse((self.base/'reports').exists())

    def test_default_does_not_claim_new_file_counts_without_scan(self):
        snapshot=self.service.snapshot(offline=True)
        self.assertIsNone(snapshot['workspace']['new'])
        self.assertIn('not checked',snapshot['providers']['ollama']['status'])
        self.assertIn('not checked',snapshot['authentication']['status'])

    def test_missing_database_is_not_created(self):
        path=self.base/'missing.db'
        service=HealthService(path,self.base/'reports')
        self.assertEqual(service.snapshot(offline=True)['local_database'],'not created')
        self.assertFalse(path.exists())

    def test_corrupt_database_reports_unknown_counts_without_repair(self):
        broken=self.base/'broken.db'; broken.write_bytes(b'not a database')
        before=broken.read_bytes()
        snapshot=HealthService(broken,self.base/'reports').snapshot(offline=True)
        self.assertEqual(snapshot['local_database'],'unavailable')
        self.assertIsNone(snapshot['indexed_files']); self.assertIsNone(snapshot['remaining'])
        self.assertEqual(broken.read_bytes(),before)

    def test_provider_health_records_no_payload_or_credentials(self):
        from searchify.config.settings import settings
        response={'status':'available','http_status':200,'data':{'models':[{'name':settings.SUMMARY_MODEL},{'name':settings.EMBEDDING_MODEL}]}}
        with patch('searchify.services.health_service.request_json',return_value=response):
            providers=self.service._providers()
        self.assertEqual(providers['ollama']['status'],'available')
        self.assertTrue(providers['ollama']['embedding_model_installed'])
        self.assertNotIn('data',providers['ollama'])
