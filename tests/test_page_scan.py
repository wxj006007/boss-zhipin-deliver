"""Offline tests for same-page collection and reviewed batch execution."""
from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import execution_context as runtime_context
from scripts import page_scan as scan
from scripts import pipeline_batch as batch


def setUpModule():
    import os
    from scripts import accounts
    stack = ExitStack()
    unittest.addModuleCleanup(stack.close)
    folder = stack.enter_context(tempfile.TemporaryDirectory())
    stack.enter_context(patch.object(accounts, 'CONTROL_DIR', Path(folder)))
    stack.enter_context(patch.object(runtime_context, 'CONTROL_DIR', Path(folder)))
    stack.enter_context(patch.dict(os.environ, {k: v for k, v in os.environ.items() if not k.startswith('BOSS_')}, clear=True))


class SamePageTests(unittest.TestCase):
    def test_collection_does_not_filter_profession_grade_or_company_size(self):
        jobs = [dict(jobId='a', title='2027届市场运营', scale='20-99人'),
                dict(jobId='b', title='Java开发', scale='10000人以上'),
                dict(jobId='c', title='教师', scale='100-499人')]
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            path = Path(folder)
            (path / 'jd_old.json').write_text(json.dumps({
                'a': {'panel_ok': False, 'jd': ''},
                'b': {'panel_ok': True, 'jd': '完整旧JD'},
                'c': {'error': 'timing'},
            }), encoding='utf-8')
            stack.enter_context(patch.object(scan.sys, 'argv', ['page_scan.py', '工作', '', '', '0', 'jd']))
            stack.enter_context(patch.object(scan, 'resolve_workdir', return_value=folder))
            stack.enter_context(patch.object(scan, 'load_profile', return_value={}))
            stack.enter_context(patch.object(scan.wb, 'navigate'))
            stack.enter_context(patch.object(scan.wb, 'evaluate', return_value='/web/geek/jobs'))
            stack.enter_context(patch.object(scan.wb, 'detect_page_anomaly', return_value=(False, '')))
            stack.enter_context(patch.object(scan.wb, 'extract_jobs', return_value=jobs))
            stack.enter_context(patch.object(scan.time, 'sleep'))
            opening = stack.enter_context(patch.object(batch.drv, 'cmd_open', return_value={
                'panel_ok': True, 'detail': '新完整JD'}))
            stack.enter_context(redirect_stdout(io.StringIO()))
            self.assertEqual(scan.main(), 0)
            self.assertEqual([c.args[0] for c in opening.call_args_list], ['a', 'c'])
            saved = json.loads((path / 'jd_工作.json').read_text(encoding='utf-8'))
            self.assertEqual(set(saved), {'a', 'b', 'c'})
            self.assertEqual(saved['b']['jd'], '完整旧JD')
            pool = json.loads((path / 'pool.json').read_text(encoding='utf-8'))
            self.assertEqual(len(pool['jobs']), 3)

    def run_current_page(self, folder, keyword='python', extra_reject=None):
        decisions = Path(folder) / 'decisions.json'
        decisions.write_text(json.dumps({'deliver': ['a'], 'reject': extra_reject or []}), encoding='utf-8')
        with ExitStack() as stack:
            stack.enter_context(patch.object(batch, 'WORKDIR', folder))
            stack.enter_context(patch.object(batch, 'load_pool', return_value={'a': {'jobId': 'a', '_kw': 'python'}}))
            goto = stack.enter_context(patch.object(batch, 'goto'))
            stack.enter_context(patch.object(batch.wb, 'evaluate', return_value=f'https://www.zhipin.com/web/geek/jobs?query={keyword}'))
            stack.enter_context(patch.object(batch.wb, 'detect_page_anomaly', return_value=(False, '')))
            send = stack.enter_context(patch.object(batch.drv, 'cmd_deliver', return_value={'delivered_ok': True}))
            reject = stack.enter_context(patch.object(batch.drv, 'cmd_reject'))
            stack.enter_context(patch.object(batch.time, 'sleep'))
            stack.enter_context(redirect_stdout(io.StringIO()))
            if keyword != 'python' or extra_reject:
                with self.assertRaises(ValueError):
                    batch.cmd_go(str(decisions), current_page=True)
                send.assert_not_called()
                reject.assert_not_called()
            else:
                result = batch.cmd_go(str(decisions), current_page=True)
                self.assertEqual(len(result['delivered']), 1)
                send.assert_called_once()
            goto.assert_not_called()

    def test_current_page_batch_does_not_navigate(self):
        with tempfile.TemporaryDirectory() as folder:
            self.run_current_page(folder)

    def test_wrong_page_and_unknown_rejection_fail_before_actions(self):
        with tempfile.TemporaryDirectory() as folder:
            self.run_current_page(folder, keyword='java')
            self.run_current_page(folder, extra_reject=['unknown'])


if __name__ == '__main__':
    unittest.main()
