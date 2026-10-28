"""Actual CLI and subprocess tests with synthetic identities on local fake bridges."""
import ast
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ConnectionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        shutil.copytree(ROOT / 'scripts', self.root / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('BOSS_')}
        self.env.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        self.requests = []
        self.connections = {}
        self.states = {}
        for name, browser in [('first', 'chrome'), ('second', 'edge')]:
            state = {'browser': browser, 'identity': {'kind': 'user_id', 'value': 'synthetic-alpha'}}
            self.states[name] = state
            requests = self.requests
            def handler_factory(current):
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *args):
                        pass
                    def do_POST(self):
                        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                        requests.append(body)
                        self.send_response(200)
                        self.end_headers()
                        value = json.dumps(current) if body['action'] == 'evaluate' else None
                        self.wfile.write(json.dumps({'ok': True, 'data': {'value': value}}).encode())
                return Handler
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler_factory(state))
            self.stack.callback(server.server_close)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.stack.callback(server.shutdown)
            path = self.root / (name + '.json')
            self.write(path, {'browser': browser, 'bridge': {'port': server.server_port}, 'session': 'shared'})
            self.connections[name] = path
        self.configs = {}
        for alias in ('alpha', 'beta'):
            directory = self.root / 'accounts' / alias
            directory.mkdir(parents=True)
            self.write(directory / 'profile.json', {'role_summary': 'Synthetic test role'})
            config = directory / 'account.json'
            self.write(config, {'account': alias, 'profile': 'profile.json', 'workdir': 'data',
                                'expected_identity': {'kind': 'user_id', 'value': 'synthetic-' + alias}})
            self.configs[alias] = config

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def cli(self, alias, command, connection=None, run=None, manager=False, expected=0, extra=()):
        module = 'manage_accounts.py' if manager else 'run_account.py'
        args = [sys.executable, str(self.root / 'scripts' / module), '--config', str(self.configs[alias])]
        if connection:
            args += ['--connection', str(self.connections[connection])]
        if run:
            args += ['--run', run]
        result = subprocess.run([*args, *extra, command], cwd=self.root, env=self.env,
                                capture_output=True, text=True, encoding='utf-8', timeout=12)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def begin(self, alias='alpha', connection='first'):
        return json.loads(self.cli(alias, 'begin', connection).stdout)['run']

    def test_account_changes_browser_without_new_identity_or_history(self):
        run = self.begin()
        history = self.root / 'accounts/alpha/data/stream_progress.json'
        original = {'delivered': [{'jobId': 'synthetic-job', 'title': 'Synthetic role', 'verify': True}],
                    'uncertain': [{'jobId': 'unknown-job'}], 'failed': [], 'blocked': []}
        self.write(history, original)
        self.cli('alpha', 'end', run=run)
        second = self.begin(connection='second')
        self.assertNotEqual(run, second)
        self.assertEqual(json.loads(history.read_text()), original)
        # A real business subprocess consumes the captured connection/context.
        self.cli('alpha', 'report', run=second)
        registry = json.loads((self.root / '.account-runtime/registered.json').read_text())
        self.assertEqual(set(registry['alpha']), {'account', 'expected_identity', 'workdir'})
        self.cli('alpha', 'end', run=second)

    def test_endpoint_lease_blocks_other_account_then_allows_reuse(self):
        run = self.begin()
        before = len(self.requests)
        for command, manager in [('inspect', True), ('bridge-start', True), ('bind', True), ('begin', False)]:
            result = self.cli('beta', command, 'first', manager=manager, expected=2)
            self.assertIn('运行占用', result.stderr)
        self.assertEqual(len(self.requests), before)
        self.cli('alpha', 'end', run=run)
        self.states['first']['identity']['value'] = 'synthetic-beta'
        beta_run = self.begin('beta')
        self.assertEqual(json.loads(self.cli('beta', 'status', manager=True).stdout)['active']['run'], beta_run)
        self.cli('beta', 'end', run=beta_run)

    def test_wrong_identity_still_rejects_after_connection_switch(self):
        run = self.begin()
        self.cli('alpha', 'end', run=run)
        self.states['second']['identity']['value'] = 'synthetic-beta'
        result = self.cli('alpha', 'begin', 'second', expected=2)
        self.assertIn('绑定身份不符', result.stderr)
        self.assertFalse((self.root / '.account-runtime/accounts/alpha/active.json').exists())

    def test_run_keeps_route_when_connection_file_changes(self):
        run = self.begin()
        self.write(self.root / 'accounts/alpha/data/stream_progress.json', {'delivered': []})
        self.connections['first'].write_text(self.connections['second'].read_text(), encoding='utf-8')
        self.cli('alpha', 'report', run=run)
        self.cli('alpha', 'report', 'first', run=run, expected=2)
        self.cli('alpha', 'end', run=run)

    def test_same_browser_type_on_separate_endpoints_runs_concurrently(self):
        route = json.loads(self.connections['second'].read_text())
        route['browser'] = 'chrome'
        self.write(self.connections['second'], route)
        self.states['second'].update(browser='chrome', identity={'kind': 'user_id', 'value': 'synthetic-beta'})
        a = self.begin()
        b = self.begin('beta', 'second')
        self.cli('alpha', 'end', run=a)
        self.cli('beta', 'end', run=b)

    def test_existing_registry_and_mixed_config_can_change_connection(self):
        raw = json.loads(self.configs['alpha'].read_text())
        raw.update(json.loads(self.connections['first'].read_text()))
        self.write(self.configs['alpha'], raw)
        run = self.begin()
        self.cli('alpha', 'end', run=run)
        path = self.root / '.account-runtime/registered.json'
        registry = json.loads(path.read_text())
        registry['alpha'].update(browser='chrome', host='127.0.0.1', port=12345, session='old-session')
        self.write(path, registry)
        other = self.begin(connection='second')
        self.write(self.root / 'accounts/alpha/data/stream_progress.json', {'delivered': []})
        # Old inline defaults cannot override the explicitly selected run route.
        self.cli('alpha', 'report', run=other)
        self.cli('alpha', 'end', run=other)

    def test_blank_template_binds_only_with_observed_identity_confirmation(self):
        raw = json.loads(self.configs['alpha'].read_text())
        raw['expected_identity'] = None
        self.write(self.configs['alpha'], raw)
        observed = json.loads(self.cli('alpha', 'inspect', 'first', manager=True).stdout)
        self.cli('alpha', 'bind', 'first', manager=True, extra=('--confirm', observed['confirm']))
        saved = json.loads(self.configs['alpha'].read_text())
        self.assertEqual(saved['expected_identity'], {'kind': 'user_id', 'value': 'synthetic-alpha'})
        self.assertNotIn('browser', saved)
        run = self.begin()
        self.cli('alpha', 'end', run=run)

    def test_account_management_operates_without_delivery_modules(self):
        keep = {'__init__.py', 'manage_accounts.py', 'accounts.py', 'connections.py',
                'execution_context.py', 'browser_session.py', 'runtime.py'}
        for path in (self.root / 'scripts').glob('*.py'):
            if path.name not in keep:
                path.unlink()
        observed = json.loads(self.cli('alpha', 'inspect', 'first', manager=True).stdout)
        self.cli('alpha', 'bind', 'first', manager=True, extra=('--confirm', observed['confirm']))
        self.assertIsNone(json.loads(self.cli('alpha', 'status', manager=True).stdout)['active'])

    def test_split_config_can_end_legacy_run_without_a_connection(self):
        run = self.begin()
        path = self.root / '.account-runtime/accounts/alpha/active.json'
        legacy = self.root / '.account-runtime/active.json'
        legacy.write_bytes(path.read_bytes())
        path.unlink()
        self.cli('alpha', 'end', run=run)
        self.assertFalse(legacy.exists())


class DependencyBoundaryTests(unittest.TestCase):
    def test_delivery_modules_do_not_import_account_management(self):
        management = {'accounts', 'manage_accounts', 'run_account', 'account_worker', 'bridge_holder'}
        for path in (ROOT / 'scripts').glob('*.py'):
            if path.stem in management:
                continue
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    names = {node.module or ''}
                    if node.module == 'scripts':
                        names.update('scripts.' + alias.name for alias in node.names)
                elif isinstance(node, ast.Import):
                    names = {alias.name for alias in node.names}
                else:
                    continue
                self.assertFalse(names & {'scripts.' + name for name in management}, (path.name, names))


if __name__ == '__main__':
    unittest.main()
