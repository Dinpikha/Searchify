"""Credential isolation using exclusively fake keyring and authentication objects."""
import base64
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from searchify.services.session_service import canonical_target, session_account, KEYRING_SERVICE

A = 'https://a.supabase.co'
B = 'https://b.supabase.co'


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.storage = {}
        self.keyring = ModuleType('keyring')
        class PasswordDeleteError(Exception):
            pass
        self.keyring.errors = SimpleNamespace(PasswordDeleteError=PasswordDeleteError)
        self.keyring.get_password = Mock(side_effect=lambda service, account: self.storage.get((service, account)))
        self.keyring.set_password = Mock(side_effect=lambda service, account, value: self.storage.__setitem__((service, account), value))
        def delete(service, account):
            if (service, account) not in self.storage:
                raise PasswordDeleteError()
            del self.storage[service, account]
        self.keyring.delete_password = Mock(side_effect=delete)
        self.sdk = ModuleType('supabase'); self.sdk.Client = object
        self.created = []
        def create(url, key):
            client = Mock()
            response = SimpleNamespace(session=self.session(url), user=SimpleNamespace(id=url))
            client.auth.set_session.return_value = response
            client.auth.sign_in_with_password.return_value = response
            self.created.append((url, key, client))
            return client
        self.sdk.create_client = Mock(side_effect=create)
        spec = importlib.util.spec_from_file_location('searchify._isolated_session_test',
                    Path(__file__).resolve().parents[1] / 'searchify/supabase_client.py')
        self.client = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'keyring': self.keyring, 'supabase': self.sdk}):
            spec.loader.exec_module(self.client)
        self.client.settings = SimpleNamespace(SUPABASE_URL=A, SUPABASE_KEY='synthetic-key')
        self.output = io.StringIO()
        self.stdout = redirect_stdout(self.output); self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)
        input_patch = patch('builtins.input', return_value='test@example.invalid')
        self.input = input_patch.start()
        self.addCleanup(input_patch.stop)
        self.client.getpass = Mock(return_value='synthetic-password')

    def session(self, suffix):
        return SimpleNamespace(access_token='access-' + suffix, refresh_token='refresh-' + suffix)

    def slot(self, url):
        return KEYRING_SERVICE, session_account(url)

    def seed(self, url):
        self.client.save_session(self.session(url), url)
        return self.storage[self.slot(url)]

    def test_canonical_identity_and_distinct_targets(self):
        self.assertEqual(session_account(A), session_account('HTTPS://A.SUPABASE.CO:443/'))
        for url in (B, 'http://a.supabase.co', A + ':444', A + '/project'):
            self.assertNotEqual(session_account(A), session_account(url))
        self.assertNotIn(A, session_account(A))
        for url in ('', None, 'https://user:secret@a.supabase.co', A + '?key=secret', A + '#secret'):
            with self.assertRaises(ValueError): canonical_target(url)

    def test_restore_and_save_refreshed_session(self):
        self.seed(A)
        original = self.sdk.create_client.side_effect
        def create(url, key):
            client = original(url, key)
            client.auth.set_session.return_value = SimpleNamespace(
                session=self.session('refreshed'), user=SimpleNamespace(id=A))
            return client
        self.sdk.create_client.side_effect = create
        client, owner = self.client.authenticate()
        client.auth.set_session.assert_called_once_with('access-' + A, 'refresh-' + A)
        self.assertEqual(owner, A)
        self.input.assert_not_called()
        saved = json.loads(self.storage[self.slot(A)])
        self.assertEqual(saved['target'], A)
        self.assertEqual(saved['access_token'], 'access-refreshed')
        self.assertEqual(saved['refresh_token'], 'refresh-refreshed')

    def test_switch_without_session_requires_own_login(self):
        saved = self.seed(A)
        self.client.settings.SUPABASE_URL = B
        client, owner = self.client.authenticate()
        client.auth.set_session.assert_not_called()
        client.auth.sign_in_with_password.assert_called_once()
        self.assertEqual(owner, B)
        self.assertEqual(self.storage[self.slot(A)], saved)
        self.assertIn(self.slot(B), self.storage)

    def test_switch_and_return_reuses_only_matching_cached_client(self):
        self.seed(A); self.seed(B)
        a = self.client.get_client()
        self.client.settings.SUPABASE_URL = B
        b = self.client.get_client()
        self.assertIsNot(a, b)
        self.assertEqual(self.client.get_owner_id(), B)
        self.client.settings.SUPABASE_URL = A
        self.assertIs(self.client.get_client(), a)
        self.assertEqual(self.client.get_owner_id(), A)
        self.assertEqual(len(self.created), 2)

    def test_invalid_restore_only_clears_current_target(self):
        self.seed(A); saved = self.seed(B)
        original = self.sdk.create_client.side_effect
        def create(url, key):
            client = original(url, key)
            client.auth.set_session.side_effect = RuntimeError('SECRET_TOKEN')
            return client
        self.sdk.create_client.side_effect = create
        self.client.authenticate()
        self.keyring.delete_password.assert_called_once_with(*self.slot(A))
        self.assertEqual(self.storage[self.slot(B)], saved)
        self.assertNotIn('SECRET_TOKEN', self.output.getvalue())

    def test_mismatched_session_never_reaches_auth(self):
        saved = self.seed(A)
        self.storage[self.slot(B)] = saved
        self.client.settings.SUPABASE_URL = B
        client, _ = self.client.authenticate()
        client.auth.set_session.assert_not_called()
        self.assertEqual(self.storage[self.slot(A)], saved)

    def test_legacy_unscoped_session_is_never_read_or_deleted(self):
        self.storage[KEYRING_SERVICE, 'default'] = 'legacy-secret'
        self.client.authenticate(); self.client.logout()
        self.assertEqual(self.storage[KEYRING_SERVICE, 'default'], 'legacy-secret')
        self.assertTrue(all(call.args[1] != 'default' for call in self.keyring.get_password.call_args_list))
        self.assertTrue(all(call.args[1] != 'default' for call in self.keyring.delete_password.call_args_list))

    def test_logout_isolates_memory_keyring_and_remote_signout(self):
        saved = self.seed(A); self.seed(B)
        a = self.client.get_client()
        self.client.settings.SUPABASE_URL = B
        b = self.client.get_client()
        b.auth.sign_out.side_effect = RuntimeError('offline')
        self.client.logout()
        b.auth.sign_out.assert_called_once(); a.auth.sign_out.assert_not_called()
        self.assertNotIn(self.slot(B), self.storage)
        self.assertEqual(self.storage[self.slot(A)], saved)
        self.client.settings.SUPABASE_URL = A
        self.assertIs(self.client.get_client(), a)

    def test_logout_uncached_target_does_not_sign_out_previous_target(self):
        self.seed(A); a = self.client.get_client()
        self.client.settings.SUPABASE_URL = B
        self.client.logout()
        a.auth.sign_out.assert_not_called()
        self.assertEqual(len(self.created), 1)
        self.assertIn(self.slot(A), self.storage)

    def test_missing_configuration_never_uses_keyring_or_cached_client(self):
        self.seed(A); self.client.get_client()
        for url, key in ((None, 'key'), ('', 'key'), (B, None), (B, '')):
            self.client.settings.SUPABASE_URL = url; self.client.settings.SUPABASE_KEY = key
            self.keyring.get_password.reset_mock(); self.sdk.create_client.reset_mock()
            with self.assertRaisesRegex(RuntimeError, 'Missing Supabase configuration'):
                self.client.get_client()
            self.keyring.get_password.assert_not_called(); self.sdk.create_client.assert_not_called()
        self.client.settings.SUPABASE_URL = None
        with self.assertRaises(ValueError): self.client.logout()
        self.keyring.delete_password.assert_not_called()

    def test_logout_needs_target_but_not_api_key(self):
        self.seed(A); a = self.client.get_client()
        self.client.settings.SUPABASE_KEY = None
        self.client.logout()
        a.auth.sign_out.assert_called_once()
        self.assertNotIn(self.slot(A), self.storage)

    def test_key_rotation_recreates_client_with_same_target_session(self):
        self.seed(A); first = self.client.get_client()
        self.client.settings.SUPABASE_KEY = 'rotated-key'
        second = self.client.get_client()
        self.assertIsNot(first, second)
        second.auth.set_session.assert_called_once()
        self.assertEqual(self.created[-1][1], 'rotated-key')

    def test_configuration_change_during_auth_does_not_redirect_saved_tokens(self):
        original = self.sdk.create_client.side_effect
        def create(url, key):
            client = original(url, key)
            def login(_):
                self.client.settings.SUPABASE_URL = B
                return SimpleNamespace(session=self.session(A), user=SimpleNamespace(id=A))
            client.auth.sign_in_with_password.side_effect = login
            return client
        self.sdk.create_client.side_effect = create
        self.client.authenticate()
        self.assertIn(self.slot(A), self.storage)
        self.assertNotIn(self.slot(B), self.storage)

    def test_incomplete_login_is_not_saved(self):
        original = self.sdk.create_client.side_effect
        def create(url, key):
            client = original(url, key)
            client.auth.sign_in_with_password.return_value = SimpleNamespace(session=None, user=None)
            return client
        self.sdk.create_client.side_effect = create
        with self.assertRaisesRegex(RuntimeError, 'Authentication failed'): self.client.authenticate()
        self.assertFalse(self.storage)

    def test_incomplete_tokens_are_not_saved(self):
        with self.assertRaises(ValueError):
            self.client.save_session(SimpleNamespace(access_token='', refresh_token='fake'))
        self.keyring.set_password.assert_not_called()

    def doctor(self):
        from searchify.services import health_service
        settings = self.client.settings
        service = health_service.HealthService('/tmp/not-opened-searchify.db', '/tmp/not-opened-reports')
        with patch.object(health_service, 'settings', settings), \
             patch.dict(sys.modules, {'keyring': self.keyring}), \
             patch.object(health_service, 'request_json', return_value={'status':'available', 'http_status':200}) as request:
            result = service.authentication(True)
        return result, request

    def seed_jwt(self, url):
        claims = {'sub':'12345678-1234-1234-1234-123456789012', 'exp':9999999999}
        encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
        self.client.save_session(SimpleNamespace(access_token='header.'+encoded+'.signature', refresh_token='fake'), url)

    def test_doctor_reads_only_target_session_and_never_writes(self):
        self.seed_jwt(A); self.keyring.set_password.reset_mock()
        result, request = self.doctor()
        self.assertIn('authenticated', result['status'])
        self.assertTrue(request.call_args.args[0].startswith(A + '/rest/'))
        self.client.settings.SUPABASE_URL = B
        result, request = self.doctor()
        self.assertEqual(result['status'], 'no cached session'); request.assert_not_called()
        self.keyring.set_password.assert_not_called(); self.keyring.delete_password.assert_not_called()

    def test_doctor_rejects_mismatched_session(self):
        self.seed_jwt(A)
        self.storage[self.slot(B)] = self.storage[self.slot(A)]
        self.client.settings.SUPABASE_URL = B
        _, request = self.doctor()
        request.assert_not_called(); self.keyring.delete_password.assert_not_called()

    def test_doctor_missing_config_does_not_read_credentials(self):
        self.client.settings.SUPABASE_KEY = None
        _, request = self.doctor()
        request.assert_not_called(); self.keyring.get_password.assert_not_called()

    def test_doctor_captures_target_before_reading_keyring(self):
        self.seed_jwt(A)
        def get(service, account):
            self.client.settings.SUPABASE_URL = B
            self.client.settings.SUPABASE_KEY = 'other-key'
            return self.storage.get((service, account))
        self.keyring.get_password.side_effect = get
        _, request = self.doctor()
        self.assertTrue(request.call_args.args[0].startswith(A + '/rest/'))
        self.assertEqual(request.call_args.args[1]['apikey'], 'synthetic-key')
