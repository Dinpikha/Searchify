# Validates the explicit live runner’s opt-ins, loopback target checks, and separate
# credentials.
# These tests exercise configuration guards without invoking the live acceptance workflow.

"""Only validate live-runner guards; never import SDKs or connect to services."""
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from live_pipeline import LiveConfigurationError, live_configuration, main


class LiveGateTests(unittest.TestCase):
    def environment(self):
        return {'SEARCHIFY_RUN_LIVE_E2E':'1','SEARCHIFY_LIVE_SUPABASE_URL':'http://127.0.0.1:54321',
                'SEARCHIFY_LIVE_SUPABASE_ANON_KEY':'synthetic-anon',
                'SEARCHIFY_LIVE_SUPABASE_SERVICE_KEY':'synthetic-service'}

    def test_both_opt_ins_are_required(self):
        environment=self.environment()
        with self.assertRaises(LiveConfigurationError): live_configuration(environment)
        del environment['SEARCHIFY_RUN_LIVE_E2E']
        with self.assertRaises(LiveConfigurationError): live_configuration(environment,True)

    def test_remote_ambiguous_and_credential_bearing_targets_are_rejected(self):
        for url in ('https://example.supabase.co','http://localhost:54321','http://192.168.1.1:54321',
                    'http://127.0.0.1','http://user:credential@127.0.0.1:54321',
                    'http://127.0.0.1:54321/path','http://127.0.0.1:54321/?key=private',
                    'http://127.0.0.1:54321/#private'):
            env=self.environment(); env['SEARCHIFY_LIVE_SUPABASE_URL']=url
            with self.subTest(url=url), self.assertRaises(LiveConfigurationError): live_configuration(env,True)

    def test_application_credentials_are_never_fallbacks(self):
        env={'SEARCHIFY_RUN_LIVE_E2E':'1','SUPABASE_URL':'http://127.0.0.1:54321',
             'SUPABASE_KEY':'must-not-be-used'}
        with self.assertRaises(LiveConfigurationError): live_configuration(env,True)
        env['SEARCHIFY_LIVE_SUPABASE_URL']='http://127.0.0.1:54321'
        with self.assertRaises(LiveConfigurationError): live_configuration(env,True)
        env=self.environment(); env['SEARCHIFY_LIVE_SUPABASE_ANON_KEY']=env['SEARCHIFY_LIVE_SUPABASE_SERVICE_KEY']
        with self.assertRaises(LiveConfigurationError): live_configuration(env,True)

    def test_valid_isolated_target_configuration(self):
        config=live_configuration(self.environment(),True)
        self.assertEqual(config,('http://127.0.0.1:54321','synthetic-anon','synthetic-service'))

    def test_unguarded_invocation_never_enters_live_code(self):
        with patch('live_pipeline.run_live',side_effect=AssertionError('must not run')) as run, \
             redirect_stderr(io.StringIO()):
            self.assertEqual(main([],self.environment()),2)
            self.assertEqual(main(['--live'],{}),2)
        run.assert_not_called()

    def test_live_errors_never_display_credentials(self):
        with patch('live_pipeline.run_live',side_effect=RuntimeError('PRIVATE_CREDENTIAL')), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['--live'],self.environment()),1)
        self.assertNotIn('PRIVATE_CREDENTIAL',output.getvalue())

    def test_script_default_does_not_import_runtime_or_authentication(self):
        script=Path(__file__).with_name('live_pipeline.py')
        code='''
import runpy, sys
path=sys.argv[1]
sys.argv=[path]
try:
    runpy.run_path(path,run_name='__main__')
except SystemExit as result:
    assert result.code == 2
for name in ('keyring','supabase','httpx','searchify.supabase_client','pipeline_support'):
    assert name not in sys.modules, name
'''
        result=subprocess.run([sys.executable,'-B','-c',code,str(script)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_generated_user_cleanup_is_scoped_even_when_a_later_login_fails(self):
        from types import ModuleType, SimpleNamespace
        from unittest.mock import MagicMock, Mock, call
        import live_pipeline
        sdk=ModuleType('supabase'); httpx=ModuleType('httpx')
        sdk.ClientOptions=Mock(side_effect=lambda **kwargs:SimpleNamespace(**kwargs))
        httpx.Client=MagicMock()
        admin=Mock(); first=Mock(); second=Mock()
        sdk.create_client=Mock(side_effect=[admin,first,second])
        admin.auth.admin.create_user.side_effect=[SimpleNamespace(user=SimpleNamespace(id='generated-one')),
                                                 SimpleNamespace(user=SimpleNamespace(id='generated-two'))]
        first.auth.sign_in_with_password.return_value=SimpleNamespace(session=object(),user=SimpleNamespace(id='generated-one'))
        second.auth.sign_in_with_password.side_effect=RuntimeError('PRIVATE_GENERATED_PASSWORD')
        query=admin.table.return_value; query.select.return_value=query; query.eq.return_value=query
        query.execute.return_value=SimpleNamespace(data=[])
        with patch.dict(sys.modules,{'supabase':sdk,'httpx':httpx}), \
             patch('searchify.ai.ollama_client.OllamaClient') as ollama, \
             patch('pipeline_support.PipelineWorkspace',side_effect=AssertionError('should not create workspace')):
            result=live_pipeline.run_live(*live_configuration(self.environment(),True))
        self.assertEqual(result['status'],'failed'); self.assertEqual(result['cleanup'],'passed')
        admin.auth.admin.delete_user.assert_has_calls([call('generated-two'),call('generated-one')])
        self.assertEqual(admin.auth.admin.delete_user.call_count,2)
        self.assertNotIn('PRIVATE_GENERATED_PASSWORD',str(result))
        for args in sdk.ClientOptions.call_args_list:
            self.assertFalse(args.kwargs['persist_session']); self.assertFalse(args.kwargs['auto_refresh_token'])
        for args in httpx.Client.call_args_list:
            self.assertFalse(args.kwargs['trust_env']); self.assertFalse(args.kwargs['follow_redirects'])
        ollama.return_value.check_available.assert_called_once()

    def test_uncertain_user_creation_is_reported_without_deleting_unknown_users(self):
        from types import ModuleType, SimpleNamespace
        from unittest.mock import MagicMock, Mock
        import live_pipeline
        sdk=ModuleType('supabase'); httpx=ModuleType('httpx')
        sdk.ClientOptions=lambda **kwargs:SimpleNamespace(**kwargs)
        admin=Mock(); sdk.create_client=Mock(return_value=admin); httpx.Client=MagicMock()
        admin.auth.admin.create_user.side_effect=TimeoutError('PRIVATE_SERVICE_KEY')
        with patch.dict(sys.modules,{'supabase':sdk,'httpx':httpx}), \
             patch('searchify.ai.ollama_client.OllamaClient'):
            result=live_pipeline.run_live(*live_configuration(self.environment(),True))
        self.assertEqual(result['status'],'failed'); self.assertEqual(result['cleanup'],'failed')
        admin.auth.admin.delete_user.assert_not_called()
        self.assertNotIn('PRIVATE_SERVICE_KEY',str(result))
        self.assertIn('uncertain',str(result['cleanup_errors']))
