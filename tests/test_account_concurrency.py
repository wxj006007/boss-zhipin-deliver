"""Real subprocess concurrency, in a disposable workspace with fake browser I/O.

No real account config, browser daemon, or delivery operation is used.
"""
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
import time
import unittest
from unittest.mock import patch

from scripts import accounts as ac, run_account as runner
from scripts.runtime import atomic_write_json


LAUNCHER = '''
import sys
from scripts import run_account as runner
# Every identity read is offline; the fake job only evaluates against test servers.
runner.verify_identity = lambda *a, **kw: {}
runner.child_command = lambda config, command, args: [
    sys.executable, '-u', '-m', 'fake_account_job', *args]
sys.exit(runner.main())
'''

FAKE_JOB = '''
import json, os, sys, time
from pathlib import Path
from scripts import accounts as ac, config_v3, webbridge_client as wb, phase_stream
from scripts.runtime import atomic_write_json
ctx = ac.assert_run()
root = Path(sys.argv[1])
alias = ctx['account']
wb.api('evaluate', {'code': '1'})
pm = wb.ProgressManager(str(Path(ctx['workdir']) / 'stream_progress.json')).load()
atomic_write_json(root / (alias + '.ready'), {
    'pid': os.getpid(), 'run': ctx['run'], 'account': alias,
    'profile': config_v3.load_profile()['role_summary'],
    'port': wb.DEFAULT_PORT, 'session': wb.SESSION,
    'processed': sorted(pm.all_done_ids)})
deadline = time.monotonic() + 15
while not (root / 'release').exists():
    if time.monotonic() > deadline:
        raise RuntimeError('Test barrier timed out')
    time.sleep(.02)
# Synthetic local history only; this module never calls a sending function.
pm.add_delivered('done-' + alias, {'title': 'Offline fixture', 'verify': True})
pm.add_uncertain('pending-' + alias, {}, 'offline fixture')
pm.save()
phase_stream.write_summary_page(ctx['workdir'])
'''


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        shutil.copytree(Path(runner.ROOT) / 'scripts', self.root / 'scripts',
                        ignore=shutil.ignore_patterns('__pycache__'))
        (self.root / 'fake_account_job.py').write_text(FAKE_JOB, encoding='utf-8')
        self.stack.enter_context(patch.object(ac, 'CONTROL_DIR', self.root / '.account-runtime'))
        self.stack.enter_context(patch.object(runner, 'verify_identity', return_value={}))
        self.stack.enter_context(patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                                       if not k.startswith('BOSS_')}, clear=True))
        self.env = dict(os.environ, PYTHONPATH=str(self.root), PYTHONIOENCODING='utf-8')
        self.received = []
        received = self.received

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append((self.server.server_port, data))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true,"data":{"value":"1"}}')

        self.configs = {}
        for alias, browser in [('a', 'edge'), ('b', 'chrome')]:
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            self.stack.callback(server.server_close)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.stack.callback(server.shutdown)
            directory = self.root / alias
            directory.mkdir()
            atomic_write_json(directory / 'profile.json', {'role_summary': alias + '-profile'})
            path = directory / 'account.json'
            atomic_write_json(path, dict(account=alias, browser=browser,
                bridge={'port': server.server_port}, session='boss-' + alias,
                profile='profile.json', workdir='data',
                expected_identity={'kind': 'user_id', 'value': alias}))
            self.configs[alias] = ac.load_account(path)
        self.procs = []
        self.stack.callback(self.finish_processes)

    def finish_processes(self):
        (self.root / 'release').touch()
        for proc in self.procs:
            try:
                proc.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=5)

    def launch(self, alias, command, run=None, args=(), code=LAUNCHER):
        argv = [sys.executable, '-u', '-c', code, '--config', self.configs[alias]['config_path']]
        if run:
            argv.extend(['--run', run])
        proc = subprocess.Popen([*argv, command, *args], cwd=self.root, env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding='utf-8')
        self.procs.append(proc)
        return proc

    def completed(self, proc, expected=0):
        out, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, expected, out + err)
        return out, err

    def begin_both(self):
        # Both registrations start without either registry entry already existing.
        procs = [self.launch(alias, 'begin') for alias in ('a', 'b')]
        for proc in procs:
            self.completed(proc)
        registry = ac.read_json(ac.CONTROL_DIR / 'registered.json')
        self.assertEqual(set(registry), {'a', 'b'})
        return {alias: ac.active_run(config)['run'] for alias, config in self.configs.items()}

    def wait_ready(self, *aliases):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if all((self.root / (alias + '.ready')).exists() for alias in aliases):
                return {alias: ac.read_json(self.root / (alias + '.ready')) for alias in aliases}
            time.sleep(.02)
        self.fail('Workers did not reach the concurrent barrier')

    def test_commands_overlap_with_isolated_routes_profiles_history_and_reports(self):
        runs = self.begin_both()
        procs = [self.launch(alias, 'live', runs[alias], [str(self.root)]) for alias in ('a', 'b')]
        ready = self.wait_ready('a', 'b')
        # Neither worker can finish before both are here: no timing-based speed assertion.
        self.assertTrue(all(proc.poll() is None for proc in procs))
        for alias in ('a', 'b'):
            config = self.configs[alias]
            self.assertEqual(ready[alias]['profile'], alias + '-profile')
            self.assertEqual(ready[alias]['port'], config['port'])
            self.assertEqual(ready[alias]['session'], config['session'])
            self.assertEqual(ready[alias]['run'], runs[alias])
            self.assertEqual(ready[alias]['processed'], [])
            # Status is usable even when the account's worker owns the execution lock.
            out, _ = self.completed(self.launch(alias, 'status'))
            self.assertEqual(json.loads(out)['active']['run'], runs[alias])
        self.completed(self.launch('a', 'live', runs['a'], [str(self.root)]), expected=2)
        self.completed(self.launch('a', 'end', runs['a']), expected=2)
        (self.root / 'release').touch()
        for proc in procs:
            self.completed(proc)
        for port, request in self.received:
            self.assertEqual(request['action'], 'evaluate')
            self.assertEqual(request['args'], {'code': '1'})
            owner = next(c for c in self.configs.values() if c['port'] == port)
            self.assertEqual(request['session'], owner['session'])
        self.assertEqual(len(self.received), 2)
        for alias, config in self.configs.items():
            directory = Path(config['workdir'])
            history = ac.read_json(directory / 'stream_progress.json')
            self.assertEqual([x['jobId'] for x in history['delivered']], ['done-' + alias])
            self.assertEqual([x['jobId'] for x in history['uncertain']], ['pending-' + alias])
            self.assertEqual(history['delivered'][0]['account'], alias)
            logs = [json.loads(line) for line in (directory / 'command_timings.jsonl').read_text().splitlines()]
            self.assertEqual({row['account'] for row in logs}, {alias})
            htmls = list(directory.glob('*.html'))
            self.assertTrue(htmls)
            self.assertIn(runs[alias], htmls[0].read_text(encoding='utf-8'))
        self.completed(self.launch('a', 'end', runs['a']))
        self.assertEqual(ac.active_run(self.configs['b'])['run'], runs['b'])
        self.completed(self.launch('a', 'begin'))
        new_run = ac.active_run(self.configs['a'])['run']
        self.assertNotEqual(new_run, runs['a'])
        self.completed(self.launch('a', 'live', new_run, [str(self.root)]))
        ready_a = ac.read_json(self.root / 'a.ready')
        self.assertEqual(ready_a['processed'], ['done-a', 'pending-a'])
        self.completed(self.launch('a', 'live', runs['a'], [str(self.root)]), expected=2)
        self.completed(self.launch('a', 'live', runs['b'], [str(self.root)]), expected=2)
        # Exercise the real immediate guard after a new begin, with every
        # browser request forbidden. Pending history must block before I/O.
        code = '''
import json, os
from scripts import accounts as ac
config = ac.load_account(%r)
os.environ[ac.CONTEXT_ENV] = json.dumps(ac.active_run(config))
from scripts import immediate as im
from unittest.mock import patch
with patch.object(im.wb, 'api', side_effect=AssertionError('Unexpected browser I/O')):
    try:
        im.Immediate().start('offline')
    except ValueError:
        pass
    else:
        raise AssertionError('Uncertain history did not block restart')
''' % self.configs['a']['config_path']
        self.completed(self.launch('a', 'status', code=code))

    def test_parent_crash_keeps_orphan_blocked_but_other_account_continues(self):
        runs = self.begin_both()
        parent = self.launch('a', 'live', runs['a'], [str(self.root)])
        child = self.wait_ready('a')['a']['pid']
        parent.kill()
        parent.wait(timeout=5)
        self.assertTrue(ac.process_alive(child))
        self.completed(self.launch('a', 'end', runs['a']), expected=2)
        self.completed(self.launch('a', 'live', runs['a'], [str(self.root)]), expected=2)
        other = self.launch('b', 'live', runs['b'], [str(self.root)])
        self.wait_ready('b')
        (self.root / 'release').touch()
        self.completed(other)
        parent.communicate(timeout=10)
        deadline = time.monotonic() + 5
        while ac.process_alive(child) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertFalse(ac.process_alive(child))
        self.completed(self.launch('a', 'end', runs['a']))
        self.assertEqual(ac.active_run(self.configs['b'])['run'], runs['b'])
        history = ac.read_json(Path(self.configs['a']['workdir']) / 'stream_progress.json')
        self.assertEqual(history['uncertain'][0]['jobId'], 'pending-a')

    def test_endpoint_and_alias_locks_cannot_be_bypassed(self):
        a, b = self.configs['a'], self.configs['b']
        cases = [dict(b, port=a['port'], session='different-session'),
                 dict(a, port=b['port']), dict(a, account='A', port=b['port'])]
        with ac.command_lock(a):
            for config in cases:
                code = ('from scripts import accounts as ac\n'
                        f'with ac.command_lock({config!r}):\n print("unexpected")')
                proc = self.launch('b', 'status', code=code)
                self.completed(proc, expected=1)

    def test_shared_identity_or_nested_history_registration_race_rejects_one(self):
        for field in ('identity', 'workdir'):
            with self.subTest(field=field):
                # Independent registry for each conflict case, no active browser operations.
                registry = ac.CONTROL_DIR / 'registered.json'
                if registry.exists():
                    registry.unlink()
                b = dict(self.configs['b'])
                if field == 'identity':
                    b['expected_identity'] = self.configs['a']['expected_identity']
                else:
                    b['workdir'] = str(Path(self.configs['a']['workdir']) / 'nested')
                procs = []
                for config in [self.configs['a'], b]:
                    code = f'from scripts import accounts as ac\nac.register({config!r})'
                    procs.append(self.launch('a', 'status', code=code))
                outputs = [p.communicate(timeout=10) for p in procs]
                self.assertEqual(sorted(p.returncode for p in procs), [0, 1], outputs)
                self.assertEqual(len(ac.read_json(registry)), 1)

    def test_legacy_run_requires_explicit_end_and_preserves_history(self):
        config = self.configs['a']
        old = dict(config, run='legacy-run', started='old', profile_snapshot={})
        atomic_write_json(ac.CONTROL_DIR / 'active.json', old)
        history = Path(config['workdir']) / 'stream_progress.json'
        atomic_write_json(history, {'uncertain': [{'jobId': 'pending'}]})
        self.completed(self.launch('b', 'begin'), expected=2)
        out, _ = self.completed(self.launch('b', 'status'))
        self.assertEqual(json.loads(out)['legacy_active']['run'], 'legacy-run')
        self.completed(self.launch('b', 'end', 'legacy-run'), expected=2)
        atomic_write_json(ac.CONTROL_DIR / 'inflight.json', {'pid': os.getpid(), 'run': 'legacy-run'})
        self.completed(self.launch('a', 'end', 'legacy-run'), expected=2)
        (ac.CONTROL_DIR / 'inflight.json').unlink()
        self.completed(self.launch('a', 'end', 'legacy-run'))
        self.assertFalse((ac.CONTROL_DIR / 'active.json').exists())
        self.assertEqual(ac.read_json(history), {'uncertain': [{'jobId': 'pending'}]})
        self.completed(self.launch('b', 'begin'))

    def test_worker_without_launch_permit_never_runs_job(self):
        proc = subprocess.run([sys.executable, '-m', 'scripts.account_worker', 'fake_account_job', str(self.root)],
                              cwd=self.root, env=self.env, input='', capture_output=True, text=True,
                              encoding='utf-8', timeout=5)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(self.received)
        self.assertFalse((self.root / 'a.ready').exists())

    def test_simultaneous_begin_same_account_has_only_one_winner(self):
        procs = [self.launch('a', 'begin') for _ in range(2)]
        outputs = [p.communicate(timeout=10) for p in procs]
        self.assertEqual(sorted(p.returncode for p in procs), [0, 2], outputs)
        self.assertEqual(len(list((Path(self.configs['a']['workdir']) / 'runs').glob('*.json'))), 1)

    def test_failed_pid_persistence_prevents_worker_operations(self):
        runs = self.begin_both()
        code = LAUNCHER.replace('sys.exit(runner.main())', '''
original_write = runner.atomic_write_json
def fail_inflight(path, value):
    if path.name == 'inflight.json':
        raise OSError('simulated disk failure')
    return original_write(path, value)
runner.atomic_write_json = fail_inflight
sys.exit(runner.main())
''')
        self.completed(self.launch('a', 'live', runs['a'], [str(self.root)], code=code), expected=2)
        self.assertFalse(self.received)
        self.assertFalse((self.root / 'a.ready').exists())
        self.assertEqual(ac.active_run(self.configs['a'])['run'], runs['a'])


if __name__ == '__main__':
    unittest.main()
