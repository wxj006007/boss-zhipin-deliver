"""Account isolation tests; all browser operations use mocks or local fake bridges."""
from contextlib import ExitStack, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts import execution_context as runtime_context
from scripts import accounts as ac, execution_context, manage_accounts
from scripts import browser_session as browser
from scripts import run_account as runner
from scripts import webbridge_client as wb
from scripts import config_v3, phase_stream, greeting


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(ac, 'CONTROL_DIR', self.root / 'control'))
        self.stack.enter_context(patch.object(execution_context, 'CONTROL_DIR', self.root / 'control'))
        self.stack.enter_context(patch.dict(os.environ, {k: v for k, v in os.environ.items() if not k.startswith('BOSS_')}, clear=True))
        self.a = self.make_config('a', 'edge', 10087)
        self.b = self.make_config('b', 'chrome', 10086)
        self.stack.enter_context(patch.object(runner, 'verify_identity', return_value={}))

    def make_config(self, alias, browser_name, port):
        directory = self.root / alias
        directory.mkdir()
        (directory / 'profile.json').write_text(json.dumps({'role_summary': alias + '岗位', 'name': alias}), encoding='utf-8')
        path = directory / 'account.json'
        path.write_text(json.dumps(dict(account=alias, browser=browser_name, bridge={'port': port},
                        profile='profile.json', workdir='data', expected_identity={'kind': 'user_id', 'value': alias})), encoding='utf-8')
        return ac.load_account(path)

    def begin(self, config):
        with ac.command_lock(config):
            return runner.begin(config)

    def use_context(self):
        state = runner.active_run(self.a)
        self.stack.enter_context(patch.dict(os.environ, {ac.CONTEXT_ENV: json.dumps(state)}))
        return state

    def test_parallel_a_b_and_profile_snapshots_are_independent(self):
        start_a = self.begin(self.a)
        start_b = self.begin(self.b)
        with self.assertRaisesRegex(RuntimeError, '尚未结束'):
            self.begin(self.a)
        state = self.use_context()
        Path(self.a['profile']).write_text('{"role_summary":"changed"}', encoding='utf-8')
        self.assertEqual(config_v3.load_profile()['role_summary'], 'a岗位')
        self.assertEqual(ac.read_json(self.b['profile'])['role_summary'], 'b岗位')
        with ac.command_lock(self.a):
            runner.end(self.a, start_a['run'])
        os.environ.pop(ac.CONTEXT_ENV)
        self.assertEqual(runner.active_run(self.b)['run'], start_b['run'])
        self.assertNotEqual(start_b['run'], state['run'])
        with ac.command_lock(self.b):
            runner.end(self.b, start_b['run'])
        start_a2 = self.begin(self.a)
        self.assertNotEqual(start_a2['run'], start_a['run'])

    def test_command_lock_rejects_overlapping_process(self):
        # A real second process, not a thread-local mock.
        code = f'from pathlib import Path\nfrom scripts import accounts as a\na.CONTROL_DIR=Path({str(ac.CONTROL_DIR)!r})\nwith a.command_lock({self.a!r}):\n print("unexpected")'
        with ac.command_lock(self.a):
            result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('unexpected', result.stdout)

    def test_endpoint_and_directory_cannot_be_shared(self):
        self.begin(self.a)
        with self.assertRaises(ValueError):
            ac.register(dict(self.b, workdir=self.a['workdir']))
        with self.assertRaises(ValueError):
            ac.register(dict(self.b, workdir=str(Path(self.a['workdir']) / 'nested')))
        with self.assertRaises(ValueError):
            ac.register(dict(self.b, port=self.a['port']))

    def test_unattributed_history_is_not_adopted(self):
        Path(self.a['workdir']).mkdir()
        (Path(self.a['workdir']) / 'stream_progress.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, '未归属'):
            self.begin(self.a)
        self.assertIsNone(runner.active_run(self.a))

    def test_wrong_browser_account_and_missing_identity_are_rejected(self):
        for state in [dict(browser='chrome', identity=self.a['expected_identity']),
                      dict(browser='edge', identity=self.b['expected_identity']),
                      dict(browser='edge', identity=None)]:
            with patch.object(browser, 'request', return_value={'value': json.dumps(state)}), self.assertRaises(RuntimeError):
                browser.verify_identity(self.a)

    def test_old_entrypoint_and_wrong_route_are_blocked_after_activation(self):
        self.begin(self.a)
        with self.assertRaises(RuntimeError):
            wb.api('evaluate', {'code': '1'})
        self.use_context()
        with self.assertRaises(RuntimeError):
            wb.api('evaluate', {'code': '1'}, host='127.0.0.1', port=10086, session='boss-b')

    def test_config_override_and_changed_config_fail(self):
        info = self.begin(self.a)
        with patch.dict(os.environ, {'BOSS_WORKDIR': self.b['workdir']}), self.assertRaises(ValueError):
            ac.check_environment(self.a)
        with self.assertRaises(ValueError):
            runner.require_active(dict(self.a, session='changed'), info['run'])
        with self.assertRaises(ValueError):
            runner.child_command(self.a, 'harvest', ['keywords.json', '', '', 'other'])
        with self.assertRaises(ValueError):
            runner.child_command(self.a, 'cleanup', ['--workdir=other'])

    def test_identity_mismatch_prevents_custom_send(self):
        with patch.object(greeting, 'verify_identity', side_effect=RuntimeError('wrong account')), \
             patch.object(greeting, 'evaluate_once') as send:
            with self.assertRaises(RuntimeError):
                greeting.chat_action('jid', 'send', 'hello')
            send.assert_not_called()

    def test_report_and_progress_keep_account_and_run(self):
        self.begin(self.a)
        state = self.use_context()
        pm = wb.ProgressManager(str(Path(self.a['workdir']) / 'stream_progress.json'))
        pm.add_delivered('jid', {'title': 'Job', 'verify': True})
        pm.save()
        self.assertEqual(pm.data['delivered'][0]['account'], 'a')
        output = phase_stream.write_summary_page(self.a['workdir'])
        self.assertIn(state['run'], Path(output).read_text(encoding='utf-8'))
        with self.assertRaises(ValueError):
            phase_stream.write_summary_page(self.b['workdir'])

    def test_orphan_command_blocks_end_until_it_exits(self):
        info = self.begin(self.a)
        runner.atomic_write_json(ac.account_dir(self.a) / 'inflight.json', {'pid': os.getpid(), 'run': info['run']})
        with self.assertRaises(RuntimeError):
            runner.end(self.a, info['run'])
        with patch.object(ac, 'process_alive', return_value=False):
            runner.end(self.a, info['run'])
        self.assertIsNone(runner.active_run(self.a))

    def test_bind_requires_confirmation_for_exact_identity(self):
        config = dict(self.a, expected_identity=None)
        state = dict(browser='edge', identity=self.a['expected_identity'])
        with patch.object(manage_accounts, 'inspect_identity', return_value=state):
            with self.assertRaises(ValueError):
                runner.bind(config, 'wrong')
            runner.bind(config, ac.fingerprint(state))
        self.assertEqual(ac.load_account(config['config_path'])['expected_identity'], state['identity'])

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for JS account guard tests')
    def test_account_guard_runs_in_the_same_evaluation_as_the_mutation(self):
        self.begin(self.a)
        self.use_context()
        code = browser.guarded_script('globalThis.clicked=true; "sent"')
        fixture = r'''
const fs=require('fs'),vm=require('vm'); const p=JSON.parse(fs.readFileSync(0,'utf8'));
const own={__vue__:{$data:{userInfo:{userId:p.id}}},parentElement:null};
const sandbox={clicked:false,navigator:{userAgent:p.ua},location:{hostname:'www.zhipin.com'},
 document:{querySelector:()=>own}};
let error='';try{vm.runInNewContext(p.code,sandbox)}catch(e){error=e.message}
process.stdout.write(JSON.stringify({clicked:sandbox.clicked,error}));
'''
        for identity, ua, expected in [('a', 'Chrome/1 Edg/1', True),
                                        ('b', 'Chrome/1 Edg/1', False),
                                        ('a', 'Chrome/1', False)]:
            result = subprocess.run(['node', '-e', fixture], input=json.dumps({'id': identity, 'ua': ua, 'code': code}),
                                    capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout)['clicked'], expected)

    def test_same_identity_cannot_have_separate_dedup_histories(self):
        self.begin(self.a)
        with self.assertRaises(ValueError):
            ac.register(dict(self.b, expected_identity=self.a['expected_identity']))

    def test_idle_foreign_endpoint_is_rejected_before_browser_navigation(self):
        self.begin(self.a)
        foreign = dict(self.b, port=self.a['port'])
        with patch.object(runner, 'verify_identity') as verify:
            with ac.command_lock(foreign), self.assertRaises(ValueError):
                runner.begin(foreign)
            verify.assert_not_called()
        with self.assertRaises(ValueError):
            ac.check_registration(foreign)

    def test_changed_config_can_end_original_run_without_touching_other_account(self):
        a, b = self.begin(self.a), self.begin(self.b)
        changed = dict(self.a, port=12345, workdir=str(self.root / 'different'), profile='missing')
        with ac.command_lock(changed):
            runner.end(changed, a['run'])
        self.assertIsNone(ac.active_run(self.a))
        self.assertEqual(ac.active_run(self.b)['run'], b['run'])
        self.assertTrue(ac.read_json(Path(self.a['workdir']) / 'runs' / (a['run'] + '.json'))['ended'])

    def test_orphan_is_account_local_and_stale_context_is_rejected(self):
        a, b = self.begin(self.a), self.begin(self.b)
        state = ac.active_run(self.a)
        atomic = runner.atomic_write_json
        atomic(ac.account_dir(self.a) / 'inflight.json', {'pid': os.getpid(), 'run': a['run']})
        with ac.command_lock(self.b):
            runner.end(self.b, b['run'])
        with self.assertRaises(RuntimeError):
            runner.end(self.a, a['run'])
        with patch.object(ac, 'process_alive', return_value=False):
            runner.end(self.a, a['run'])
        with self.assertRaises(RuntimeError):
            ac.assert_run(state)


class BridgeRoutingTests(unittest.TestCase):
    def test_instances_route_helpers_and_health_to_their_own_ports(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                received.append((self.server.server_port, 'health', None))
                self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append((self.server.server_port, data['action'], data['session']))
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps({'ok': True, 'data': {'value': '{}', 'success': True}}).encode())
        with ExitStack() as stack, tempfile.TemporaryDirectory() as folder:
            stack.enter_context(patch.object(ac, 'CONTROL_DIR', Path(folder)))
            stack.enter_context(patch.object(runtime_context, 'CONTROL_DIR', Path(folder)))
            stack.enter_context(patch.dict(os.environ, {k: v for k, v in os.environ.items() if not k.startswith('BOSS_')}, clear=True))
            stack.enter_context(patch.object(wb.time, 'sleep'))
            for name in ('a', 'b'):
                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                stack.callback(server.server_close)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                stack.callback(server.shutdown)
                client = wb.WebBridge(port=server.server_port, session='boss-' + name)
                start = len(received)
                client.evaluate('1')
                client.click('#x')
                client.navigate('https://www.zhipin.com', wait_range=(0, 0))
                client.scroll_page()
                client.extract_jobs()
                client.handle_popup()
                client.health_check()
                for port, action, session in received[start:]:
                    self.assertEqual(port, server.server_port)
                    if action != 'health': self.assertEqual(session, 'boss-' + name)


if __name__ == '__main__':
    unittest.main()
