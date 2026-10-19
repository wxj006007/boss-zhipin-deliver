"""Regression coverage: contact must never navigate the retained search tab."""
from contextlib import ExitStack
import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

from scripts import delivery_tab as dt, greeting as g, phase_stream as ps


class DeliveryTabTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.context = dict(tab_id=10, chat_tab_id=None, chat_url='https://www.zhipin.com/web/geek/chat')
        self.source = dict(href='https://www.zhipin.com/job_detail/job-a.html', title='后端实习',
                           description='完整的岗位描述', own_uid='123', browser='edge')
        self.stack.enter_context(patch.object(dt, 'verify_identity'))
        self.stack.enter_context(patch.object(dt, 'guarded_script', side_effect=lambda code: code))
        self.stack.enter_context(patch.object(dt.time, 'sleep'))
        self.stack.enter_context(patch.object(dt, 'activity_decision', return_value={'allowed': True}))

    def test_opens_observed_exact_job_in_new_tab_before_any_contact(self):
        with patch.object(dt, 'evaluate', return_value=json.dumps(self.source)), \
             patch.object(dt, 'tab_command', return_value={'tabId': 20}) as command, \
             patch.object(dt, 'read_delivery_detail', return_value={'ready': True, 'activity': {}}) as read:
            dt.prepare_delivery_tab(self.context, 'job-a')
        command.assert_called_once_with('navigate', {'url': self.source['href'], 'newTab': True}, dt.SESSION)
        self.assertEqual(self.context['tab_id'], 10)
        self.assertEqual(self.context['delivery_tab_id'], 20)
        self.assertFalse(read.call_args.kwargs.get('click', False))

    def test_source_tab_returned_as_worker_is_rejected_before_contact(self):
        with patch.object(dt, 'evaluate', return_value=json.dumps(self.source)), \
             patch.object(dt, 'tab_command', return_value={'tabId': 10}), \
             patch.object(dt, 'read_delivery_detail') as read, self.assertRaises(RuntimeError):
            dt.prepare_delivery_tab(self.context, 'job-a')
        read.assert_not_called()

    def test_unobserved_wrong_or_foreign_url_never_navigates(self):
        for href in ['', 'https://other.invalid/job_detail/job-a.html',
                     'https://www.zhipin.com/job_detail/other.html',
                     'https://x@www.zhipin.com/job_detail/job-a.html']:
            with self.subTest(href=href), patch.object(dt, 'evaluate', return_value=json.dumps(dict(self.source, href=href))), \
                 patch.object(dt, 'tab_command') as command, self.assertRaises(ValueError):
                dt.prepare_delivery_tab(dict(self.context), 'job-a')
            command.assert_not_called()

    def test_changed_jd_requires_review_and_identity_mismatch_stops(self):
        for result, error in [({'ready': True, 'review_required': True, 'reason': 'changed'}, dt.DeliveryReviewRequired),
                              ({'identity_mismatch': True}, ValueError), ({'target_mismatch': True}, ValueError),
                              ({'ready': True, 'already_contacted': True}, ValueError)]:
            with self.subTest(result=result), patch.object(dt, 'evaluate', return_value=json.dumps(self.source)), \
                 patch.object(dt, 'tab_command', return_value={'tabId': 20}), \
                 patch.object(dt, 'read_delivery_detail', return_value=result), self.assertRaises(error):
                dt.prepare_delivery_tab(dict(self.context), 'job-a')

    def test_reused_chat_with_draft_is_not_navigated(self):
        self.context['chat_tab_id'] = 20
        with patch.object(dt, 'evaluate', side_effect=[json.dumps(self.source), False]), \
             patch.object(dt, 'tab_command') as command, self.assertRaises(ValueError):
            dt.prepare_delivery_tab(self.context, 'job-a')
        command.assert_called_once_with('attach_tab', {'tabId': 20}, dt.SESSION)

    def test_reuses_empty_worker_without_touching_source(self):
        self.context['chat_tab_id'] = 20
        with patch.object(dt, 'evaluate', side_effect=[json.dumps(self.source), True]), \
             patch.object(dt, 'tab_command', side_effect=[{}, {'tabId': 20}]) as command, \
             patch.object(dt, 'read_delivery_detail', return_value={'ready': True, 'activity': {}}):
            dt.prepare_delivery_tab(self.context, 'job-a')
        self.assertEqual(command.call_args_list[0].args[1], {'tabId': 20})
        self.assertEqual(command.call_args_list[1].args[1], {'url': self.source['href'], 'newTab': False})


@unittest.skipUnless(shutil.which('node'), 'Node is needed for DOM fixtures')
class DetailBrowserTests(unittest.TestCase):
    def run_detail(self, **options):
        fixture = r'''
const vm=require('vm'),fs=require('fs'),o=JSON.parse(fs.readFileSync(0,'utf8'));
let clicks=0;
const button={innerText:o.contacted?'继续沟通':'立即沟通',getClientRects:()=>[1],
 getAttribute(k){return k==='redirect-url'?'https://www.zhipin.com/web/geek/chat?jobId='+(o.buttonId||'job-a'):null},click(){clicks++}};
const document={querySelector(s){if(s==='#header .user-nav')return {};
 if(s==='.job-banner h1')return {innerText:'后端实习'};
 if(s==='.job-detail .job-sec-text')return {innerText:o.changed?'另一份JD':'完整的岗位描述'};
 if(s==='.job-detail .boss-active-time')return {innerText:o.activity||'刚刚活跃'};
},querySelectorAll(){return o.ambiguous?[button,button]:[button]}};
const result=vm.runInNewContext(o.code,{document,URL,window:{_PAGE:{uid:o.uid||123}},
 navigator:{userAgent:o.browser||'Chrome/1 Edg/1'},getComputedStyle:()=>({visibility:'visible'}),
 location:{hostname:'www.zhipin.com',origin:'https://www.zhipin.com',pathname:o.path||'/job_detail/job-a.html'}});
process.stdout.write(JSON.stringify({result:JSON.parse(result),clicks}));
'''
        payload = dict(jid='job-a', source=dict(title='后端实习', description='完整的岗位描述', own_uid='123', browser='edge'), click=True, activity_text='刚刚活跃')
        code = dt.DETAIL_JS.replace('PAYLOAD', json.dumps(payload), 1)
        proc = subprocess.run(['node', '-e', fixture], input=json.dumps(dict(options, code=code)),
                              text=True, encoding='utf-8', capture_output=True, check=True)
        return json.loads(proc.stdout)

    def test_only_exact_account_job_and_reviewed_jd_can_click(self):
        self.assertEqual(self.run_detail()['clicks'], 1)
        for options in [dict(uid=456), dict(browser='Chrome/1'), dict(buttonId='other'),
                        dict(path='/web/geek/jobs'), dict(changed=True), dict(ambiguous=True),
                        dict(contacted=True), dict(activity='昨日活跃')]:
            with self.subTest(options=options):
                self.assertEqual(self.run_detail(**options)['clicks'], 0)


class PreContactFailureTests(unittest.TestCase):
    def test_changed_worker_jd_returns_to_list_without_contact_or_message(self):
        with ExitStack() as stack:
            for module, name, value in [(g, 'evaluate', 'https://www.zhipin.com/web/geek/jobs'),
                                        (g, 'capture_list', {'tab_id': 10}), (g, 'prepare_chat_tab', None),
                                        (g, 'verify_identity', None), (g, 'check_activity', {'allowed': True}),
                                        (ps, '_panel_shows_job', True), (ps, 'read_popup_type', '')]:
                stack.enter_context(patch.object(module, name, return_value=value))
            stack.enter_context(patch.object(g, 'prepare_delivery_tab', side_effect=dt.DeliveryReviewRequired('changed')))
            contact = stack.enter_context(patch.object(g, 'click_delivery_contact'))
            message = stack.enter_context(patch.object(g, 'chat_action'))
            restore = stack.enter_context(patch.object(g, 'restore_list', return_value={'preserved': True}))
            result = g.deliver_greeting({'jobId': 'job-a'}, '真实话术')
        self.assertTrue(result['review_required'])
        self.assertTrue(result['list_return']['preserved'])
        restore.assert_called_once()
        contact.assert_not_called()
        message.assert_not_called()


if __name__ == '__main__':
    unittest.main()
