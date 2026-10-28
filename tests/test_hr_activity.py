"""Calendar-day activity rules and no-send regression checks; no real browser writes."""
from contextlib import ExitStack, redirect_stdout
from datetime import datetime
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import execution_context as runtime_context
from scripts import hr_activity as hr
from scripts import webbridge_client as wb
from scripts import _live_driver as drv
from scripts import greeting, phase_stream as ps, pipeline_batch as batch, accounts

PROFILE = {'hr_activity_filter': {'mode': 'today', 'unknown': 'allow'}}
NOW = datetime(2026, 10, 14, 13, 0, tzinfo=hr.SHANGHAI)


def setUpModule():
    stack = ExitStack()
    unittest.addModuleCleanup(stack.close)
    folder = stack.enter_context(tempfile.TemporaryDirectory())
    stack.enter_context(patch.object(accounts, 'CONTROL_DIR', Path(folder)))
    stack.enter_context(patch.object(runtime_context, 'CONTROL_DIR', Path(folder)))
    stack.enter_context(patch.dict(os.environ, {k:v for k,v in os.environ.items() if not k.startswith('BOSS_')}, clear=True))


class ActivityRules(unittest.TestCase):
    def test_online_and_today_labels_pass(self):
        for text in ('在线', '当前在线', '刚刚活跃', '今天活跃', '今日活跃', '今天08:30活跃',
                     '10分钟前活跃', '2小时前活跃', '2026-10-14活跃'):
            with self.subTest(text=text):
                result = hr.decide({'online': False, 'text': text}, PROFILE, NOW)
                self.assertTrue(result['allowed'])
                self.assertEqual(result['status'], 'today')
        self.assertTrue(hr.decide({'online': True}, PROFILE, NOW)['allowed'])

    def test_older_or_broader_labels_do_not_meet_today_requirement(self):
        for text in ('昨天活跃', '昨日活跃', '前天活跃', '3日内活跃', '本周活跃', '本月活跃',
                     '近期活跃', '7天前活跃', '很久未活跃', '2026-10-13活跃'):
            with self.subTest(text=text):
                self.assertFalse(hr.decide({'text': text}, PROFILE, NOW)['allowed'])
        # Fresh detail is stronger evidence than a stale card online flag.
        self.assertFalse(hr.decide({'online': True, 'text': '昨日活跃'}, PROFILE, NOW)['allowed'])

    def test_offline_missing_and_unrecognized_are_allowed_as_unknown(self):
        for observation in ({'online': False}, {'online': None}, {}, {'text': '活跃度未展示'}):
            result = hr.decide(observation, PROFILE, NOW)
            self.assertTrue(result['allowed'])
            self.assertEqual(result['status'], 'unknown')
        self.assertFalse(hr.decide({}, {'hr_activity_filter': {'mode': 'today', 'unknown': 'skip'}}, NOW)['allowed'])

    def test_relative_time_uses_china_calendar_boundary(self):
        just_after_midnight = datetime(2026, 10, 14, 0, 30, tzinfo=hr.SHANGHAI)
        self.assertFalse(hr.decide({'text': '1小时前活跃'}, PROFILE, just_after_midnight)['allowed'])
        self.assertTrue(hr.decide({'text': '10分钟前活跃'}, PROFILE, just_after_midnight)['allowed'])

    def test_disabled_is_compatible_and_bad_config_is_rejected(self):
        self.assertTrue(hr.decide({'text': '很久未活跃'}, {}, NOW)['allowed'])
        with patch.object(wb, 'evaluate') as read:
            self.assertTrue(hr.check_current('jid', 's', {})['allowed'])
            read.assert_not_called()
        for rule in ('today', {'mode': 'typo'}, {'mode': 'today', 'unknown': 'typo'}):
            with self.assertRaises(ValueError):
                hr.policy({'hr_activity_filter': rule})

    def test_unknown_activity_does_not_excuse_wrong_target_or_failed_read(self):
        with patch.object(wb, 'evaluate', return_value='{"target_verified":false,"activity":{}}'):
            with self.assertRaises(wb.AbortScriptError):
                hr.check_current('jid', 's', PROFILE)
        with patch.object(wb, 'evaluate', side_effect=ConnectionError('lost')):
            with self.assertRaises(ConnectionError):
                hr.check_current('jid', 's', PROFILE)


class ActivityIntegration(unittest.TestCase):
    def test_driver_skips_before_pending_record_or_send(self):
        check = hr.decide({'text': '昨天活跃'}, PROFILE, NOW)
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(drv, 'WORKDIR', folder), \
             patch.object(drv, 'walk_state', return_value={'consecutive_failures': 2}), \
             patch.object(drv, 'save_state') as save, \
             patch.object(drv, 'cmd_open', return_value={'panel_ok': True, 'detail': 'JD', 'hr_activity': check}), \
             patch.object(drv, 'deliver_greeting') as send:
            result = drv.cmd_deliver('jid', greeting='你好')
            self.assertTrue(result['activity_filtered'])
            send.assert_not_called()
            save.assert_not_called()
            data = json.loads((Path(folder) / 'stream_progress.json').read_text(encoding='utf-8'))
            self.assertEqual(data['activity_skipped'][0]['jobId'], 'jid')
            self.assertFalse(data.get('uncertain'))
            self.assertFalse(wb.ProgressManager(str(Path(folder) / 'stream_progress.json')).load().is_done('jid'))

    def test_fresh_pre_contact_skip_clears_pending_without_failure_breaker(self):
        check = hr.decide({'text': '昨天活跃'}, PROFILE, NOW)
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(drv, 'WORKDIR', folder), \
             patch.object(drv, 'walk_state', return_value={'consecutive_failures': 2}), \
             patch.object(drv, 'save_state') as save, \
             patch.object(drv, 'cmd_open', return_value={'panel_ok': True, 'detail': 'JD', 'hr_activity': hr.decide({'online':True}, PROFILE, NOW)}), \
             patch.object(drv, 'deliver_greeting', return_value=hr.skip_result(check)):
            result = drv.cmd_deliver('jid', greeting='你好')
            self.assertTrue(result['skipped'])
            data = json.loads((Path(folder) / 'stream_progress.json').read_text(encoding='utf-8'))
            self.assertFalse(data.get('uncertain'))
            self.assertFalse(data['failed'])
            self.assertEqual(data['activity_skipped'][0]['stage'], 'before_contact')
            save.assert_not_called()

    def test_custom_and_default_paths_recheck_before_click(self):
        check = hr.decide({'text': '昨天活跃'}, PROFILE, NOW)
        with ExitStack() as stack:
            stack.enter_context(patch.object(greeting, 'evaluate', return_value='https://www.zhipin.com/web/geek/jobs'))
            stack.enter_context(patch.object(ps, '_panel_shows_job', return_value=True))
            stack.enter_context(patch.object(ps, 'read_popup_type', return_value=''))
            stack.enter_context(patch.object(greeting, 'check_activity', return_value=check))
            custom_send = stack.enter_context(patch.object(greeting, 'evaluate_once'))
            self.assertTrue(greeting.deliver_greeting({'jobId':'jid'}, '你好')['activity_filtered'])
            custom_send.assert_not_called()
            stack.enter_context(patch.object(ps, 'check_activity', return_value=check))
            stack.enter_context(patch.object(ps, '_ensure_recommend'))
            stack.enter_context(patch.object(ps, '_try_locate_card', return_value='ok'))
            stack.enter_context(patch.object(ps, 'safe_close_popup'))
            stack.enter_context(patch.object(ps, 'detect_page_anomaly', return_value=(False, '')))
            default_send = stack.enter_context(patch.object(ps, 'evaluate_once'))
            self.assertTrue(ps.deliver_click({'jobId':'jid'})['activity_filtered'])
            default_send.assert_not_called()

    def test_batch_reports_activity_as_skip_instead_of_failure(self):
        check = hr.decide({'text': '本周活跃'}, PROFILE, NOW)
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(batch, 'WORKDIR', folder), \
             patch.object(batch, 'load_pool', return_value={'jid': {'jobId':'jid', '_kw':'Python'}}), \
             patch.object(batch, 'goto'), patch.object(batch.time, 'sleep'), \
             patch.object(batch.drv, 'cmd_deliver', return_value=hr.skip_result(check)), \
             redirect_stdout(io.StringIO()):
            path = Path(folder) / 'decision.json'
            path.write_text('{"deliver":["jid"]}', encoding='utf-8')
            result = batch.cmd_go(path)
            self.assertEqual(result['skipped'], ['jid'])
            self.assertFalse(result['failed'])
            self.assertFalse(result['stopped'])

    def test_walk_skips_known_old_activity_but_presents_unknown(self):
        jobs = [{'jobId':'old', 'hr_activity': {'text':'昨日活跃'}},
                {'jobId':'unknown', 'hr_activity': {'online':False}}]
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(drv, 'WORKDIR', folder), patch.object(drv, 'load_profile_cached', return_value=PROFILE), \
             patch.object(drv, 'walk_state', return_value={}), patch.object(drv, 'save_state'), \
             patch.object(drv, 'extract_jobs', return_value=jobs), patch.object(drv, '_recommend_end_marker', return_value=''), \
             patch.object(drv, '_delivered_jids', return_value=set()), redirect_stdout(io.StringIO()) as output:
            drv._present_next()
            result = json.loads(output.getvalue())
            self.assertEqual(result['job']['jobId'], 'unknown')
            self.assertEqual(result['auto_skipped_mech'][0]['jobId'], 'old')


@unittest.skipUnless(shutil.which('node'), 'Node is required for DOM fixture tests')
class ActivityBrowserScripts(unittest.TestCase):
    def test_vue_card_preserves_online_false_and_missing_distinction(self):
        fixture = r'''
const fs=require('fs'),vm=require('vm');const code=fs.readFileSync(0,'utf8');
const cards=[true,false,undefined].map((online,i)=>({__vue__:{$props:{data:{encryptJobId:String(i),bossOnline:online}}},querySelectorAll:()=>[]}));
const document={querySelectorAll:()=>cards};
process.stdout.write(vm.runInNewContext(code,{document}));
'''
        result = subprocess.run(['node','-e',fixture], input=wb._EXTRACT_JOBS_VUE_JS, text=True, encoding='utf-8', capture_output=True, check=True)
        rows = json.loads(result.stdout)['list']
        self.assertEqual([r['hr_activity']['online'] for r in rows], [True, False, None])

    def test_detail_reader_ignores_job_description_and_checks_target(self):
        fixture = r'''
const fs=require('fs'),vm=require('vm');const p=JSON.parse(fs.readFileSync(0,'utf8'));
const card={__vue__:{$props:{data:{bossOnline:false}}},querySelectorAll:()=>[]};
const link={getAttribute:()=>'/job_detail/jid.html',closest:()=>card};
const panel={textContent:'今天活跃（岗位描述，不能作为HR证据）',querySelectorAll:s=>s==='button,a'?[{getAttribute:()=>p.target?'job_jid':''}]:[{textContent:'昨日活跃'}]};
const document={querySelectorAll:()=>[link],querySelector:()=>panel};
process.stdout.write(vm.runInNewContext(p.code,{document}));
'''
        code = hr.READ_ACTIVITY_JS.replace('JOB_ID', '"jid"')
        for target in (True, False):
            result = subprocess.run(['node','-e',fixture], input=json.dumps({'code':code,'target':target}), text=True, encoding='utf-8', capture_output=True, check=True)
            data = json.loads(result.stdout)
            self.assertEqual(data['target_verified'], target)
            self.assertEqual(data['activity']['text'], '昨日活跃' if target else '')


if __name__ == '__main__':
    unittest.main()
