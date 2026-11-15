"""Read-only dashboard diagnostics with deterministic provider responses."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock, MagicMock
from searchify.core.file_registry import FileRegistry
from searchify.services.health_service import HealthService
from searchify.cli.doctor import run
import sys
import os


def make_settings(supabase_url="", supabase_key="", embedding_dimensions=768, dotenv_available=True):
    """Create a mock settings object."""
    settings = MagicMock()
    settings.SUPABASE_URL = supabase_url
    settings.SUPABASE_KEY = supabase_key
    settings.EMBEDDING_DIMENSIONS = embedding_dimensions
    settings.SUMMARY_MODEL = "qwen2.5-coder:7b"
    settings.EMBEDDING_MODEL = "embeddinggemma-2:270m"
    settings.DOTENV_AVAILABLE = dotenv_available
    settings.PROJECT_ROOT = Path("/")
    settings.REVIEWS_DIR = Path("/tmp/reviews")
    settings.REPORTS_DIR = Path("/tmp/reports")
    return settings


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve(); self.root=self.base/'source'; self.root.mkdir()
        self.registry=FileRegistry(self.base/'index.db'); self.registry.register(self.root)

    def _service(self, settings_mock=None):
        return HealthService(self.base/'index.db', self.base/'reports', settings=settings_mock)

    def test_metadata_scan_never_extracts_or_authenticates(self):
        service = self._service(make_settings())
        (self.root/'new.kt').write_text('PRIVATE_CONTENT')
        before=self.registry.path.read_bytes()
        with patch('searchify.core.scanner.extract_content', side_effect=AssertionError('content read')), \
             patch.object(service,'authentication',return_value={'status':'not checked'}) as auth:
            snapshot=service.snapshot(scan=True,offline=True)
        self.assertEqual(snapshot['workspace']['new'],1)
        auth.assert_called_once_with(False)
        self.assertEqual(self.registry.path.read_bytes(),before)
        self.assertFalse((self.base/'reports').exists())

    def test_default_does_not_claim_new_file_counts_without_scan(self):
        service = self._service(make_settings())
        snapshot=service.snapshot(offline=True)
        self.assertIsNone(snapshot['workspace']['new'])
        self.assertIn('not checked',snapshot['providers']['ollama']['status'])
        self.assertIn('not checked',snapshot['authentication']['status'])

    def test_missing_database_is_not_created(self):
        path=self.base/'missing.db'
        service=HealthService(path,self.base/'reports', settings=make_settings())
        self.assertEqual(service.snapshot(offline=True)['local_database'],'not created')
        self.assertFalse(path.exists())

    def test_corrupt_database_reports_unknown_counts_without_repair(self):
        broken=self.base/'broken.db'; broken.write_bytes(b'not a database')
        before=broken.read_bytes()
        service = HealthService(broken,self.base/'reports', settings=make_settings())
        snapshot=service.snapshot(offline=True)
        self.assertEqual(snapshot['local_database'],'unavailable')
        self.assertIsNone(snapshot['indexed_files']); self.assertIsNone(snapshot['remaining'])
        self.assertEqual(broken.read_bytes(),before)

    def test_provider_health_records_no_payload_or_credentials(self):
        service = self._service(make_settings())
        from searchify.config.settings import settings
        response={'status':'available','http_status':200,'data':{'models':[{'name':settings.SUMMARY_MODEL},{'name':settings.EMBEDDING_MODEL}]}}
        with patch('searchify.services.health_service.request_json',return_value=response):
            providers=service._providers()
        self.assertEqual(providers['ollama']['status'],'available')
        self.assertTrue(providers['ollama']['embedding_model_installed'])
        self.assertNotIn('data',providers['ollama'])

    def test_offline_mode_supabase_config_missing_is_informational(self):
        """Offline mode: missing Supabase config is informational, not fatal."""
        settings_mock = make_settings(supabase_url="", supabase_key="")
        service = HealthService(self.base/'index.db', self.base/'reports', settings=settings_mock)
        snapshot=service.snapshot(offline=True)
        supabase_errors=[e for e in snapshot['errors'] if 'Supabase' in e]
        self.assertEqual(len(supabase_errors),1)
        self.assertIn('not checked (offline mode)',supabase_errors[0])
        # Exit code should be 0 (no fatal errors)
        fatal=[e for e in snapshot['errors'] if 'not checked' not in e]
        self.assertEqual(fatal,[])

    def test_offline_mode_supabase_config_invalid_is_informational(self):
        """Offline mode: invalid Supabase config is informational, not fatal."""
        settings_mock = make_settings(supabase_url="invalid", supabase_key="")
        service = HealthService(self.base/'index.db', self.base/'reports', settings=settings_mock)
        snapshot=service.snapshot(offline=True)
        supabase_errors=[e for e in snapshot['errors'] if 'Supabase' in e]
        self.assertEqual(len(supabase_errors),1)
        self.assertIn('not checked (offline mode)',supabase_errors[0])
        fatal=[e for e in snapshot['errors'] if 'not checked' not in e]
        self.assertEqual(fatal,[])

    def test_online_mode_missing_supabase_config_is_fatal(self):
        """Online mode: missing Supabase config is a fatal error."""
        settings_mock = make_settings(supabase_url="", supabase_key="")
        service = HealthService(self.base/'index.db', self.base/'reports', settings=settings_mock)
        snapshot=service.snapshot(offline=False)
        supabase_errors=[e for e in snapshot['errors'] if 'Supabase' in e]
        self.assertEqual(len(supabase_errors),1)
        self.assertIn('incomplete or invalid',supabase_errors[0])
        fatal=[e for e in snapshot['errors'] if 'not checked' not in e]
        self.assertIn(supabase_errors[0],fatal)

    def test_online_mode_invalid_supabase_config_is_fatal(self):
        """Online mode: invalid Supabase config is a fatal error."""
        settings_mock = make_settings(supabase_url="invalid", supabase_key="")
        service = HealthService(self.base/'index.db', self.base/'reports', settings=settings_mock)
        snapshot=service.snapshot(offline=False)
        supabase_errors=[e for e in snapshot['errors'] if 'Supabase' in e]
        self.assertEqual(len(supabase_errors),1)
        self.assertIn('incomplete or invalid',supabase_errors[0])
        fatal=[e for e in snapshot['errors'] if 'not checked' not in e]
        self.assertIn(supabase_errors[0],fatal)

    def test_offline_cli_exit_code_zero(self):
        """Offline doctor CLI exits 0 even with missing Supabase config."""
        with patch('searchify.cli.doctor.HealthService') as mock_service_class:
            mock_snapshot = {
                'dependencies': {'dotenv': True, 'keyring': True, 'supabase': True, 'rich': True},
                'local_database': 'available',
                'providers': {'ollama': {'status': 'not checked (offline)'}, 'supabase': {'status': 'not checked (offline)'}},
                'authentication': {'status': 'not checked (doctor --auth inspects the secure keyring)'},
                'models': {'dimensions': 768},
                'errors': ['Supabase configuration not checked (offline mode)']
            }
            mock_service_class.return_value.snapshot.return_value = mock_snapshot
            args=type('Args',(),{'offline':True,'auth':False})()
            result=run(args)
        self.assertEqual(result,0)

    def test_online_cli_exit_code_one_on_missing_supabase(self):
        """Online doctor CLI exits 1 with missing Supabase config."""
        with patch('searchify.cli.doctor.HealthService') as mock_service_class:
            mock_snapshot = {
                'dependencies': {'dotenv': True, 'keyring': True, 'supabase': True, 'rich': True},
                'local_database': 'available',
                'providers': {'ollama': {'status': 'available'}, 'supabase': {'status': 'not configured'}},
                'authentication': {'status': 'not checked (doctor --auth inspects the secure keyring)'},
                'models': {'dimensions': 768},
                'errors': ['Supabase configuration incomplete or invalid', 'supabase unavailable or not configured']
            }
            mock_service_class.return_value.snapshot.return_value = mock_snapshot
            args=type('Args',(),{'offline':False,'auth':False})()
            result=run(args)
        self.assertEqual(result,1)


class HealthCliTests(unittest.TestCase):
    def test_doctor_offline_prints_banner_and_status(self):
        with patch('searchify.cli.doctor.line') as mock_line:
            with patch('searchify.cli.doctor.HealthService') as mock_service_class:
                mock_snapshot = {
                    'dependencies': {'dotenv': True, 'keyring': True, 'supabase': True, 'rich': True},
                    'local_database': 'available',
                    'providers': {'ollama': {'status': 'not checked (offline)'}, 'supabase': {'status': 'not checked (offline)'}},
                    'authentication': {'status': 'not checked (doctor --auth inspects the secure keyring)'},
                    'models': {'dimensions': 768},
                    'errors': ['Supabase configuration not checked (offline mode)']
                }
                mock_service_class.return_value.snapshot.return_value = mock_snapshot
                args=type('Args',(),{'offline':True,'auth':False})()
                run(args)
            calls=[c[0][0] for c in mock_line.call_args_list]
            self.assertTrue(any('SEARCHIFY DOCTOR' in c for c in calls))
            self.assertTrue(any('Python runtime' in c for c in calls))
            self.assertTrue(any('Local database' in c for c in calls))