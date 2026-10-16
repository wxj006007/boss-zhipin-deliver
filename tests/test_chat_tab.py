"""Offline safety checks for retaining the list and selecting exact chat jobs."""
from contextlib import ExitStack
import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

from scripts import chat_tab as ct, list_return as lr, account_browser as identity


class ChatTabTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.context = dict(url='https://www.zhipin.com/web/geek/jobs?query=x', token='t',
                            tab_id=10, chat_url='https://www.zhipin.com/web/geek/chat', chat_tab_id=None)
        self.stack.enter_context(patch.object(ct, 'verify_identity'))
        self.stack.enter_context(patch.object(ct, 'guarded_script', side_effect=lambda code: code))
        self.stack.enter_context(patch.object(ct.time, 'sleep'))
        self.stack.enter_context(patch('scripts.phase_stream.detect_page_anomaly', return_value=(False, '')))
        self.stack.enter_context(patch.object(lr, 'probe_list', return_value={'on_list':True, 'same_document':True}))

    def test_preflight_scopes_tabs_and_observes_message_link(self):
        tabs = [{'tabId':10,'url':self.context['url']}, {'tabId':20,'url':self.context['chat_url']}]
        with patch.object(ct, 'tab_command', return_value={'tabs':tabs}) as command, patch.object(ct, 'evaluate', return_value=self.context['chat_url']):
            result = ct.prepare_chat_tab(dict(self.context))
        self.assertEqual(result['chat_tab_id'], 20)
        self.assertEqual([c.args[0] for c in command.call_args_list], ['list_tabs'])

    def test_ambiguous_source_or_untrusted_link_stops_before_contact(self):
        for tabs, url in [([{'tabId':10,'url':self.context['url']}]*2,self.context['chat_url']),
                          ([{'tabId':10,'url':self.context['url']}],'https://other.invalid/web/geek/chat')]:
            with self.subTest(url=url), patch.object(ct, 'tab_command', return_value={'tabs':tabs}), patch.object(ct, 'evaluate', return_value=url), self.assertRaises(ValueError):
                ct.prepare_chat_tab(dict(self.context))

    def test_new_chat_keeps_original_tab_and_selects_only_once(self):
        with patch.object(ct, 'tab_command', return_value={'tabId':20}) as command, patch.object(ct, 'evaluate', return_value=True), patch.object(ct, 'evaluate_once', return_value='{"selected":true}') as action:
            self.assertTrue(ct.enter_chat_tab(self.context, 'job-a', ''))
        command.assert_called_once_with('navigate', {'url':self.context['chat_url'], 'newTab':True}, ct.SESSION)
        self.assertEqual(action.call_count, 1)
        self.assertEqual(self.context['tab_id'], 10)

    def test_existing_chat_is_reused_without_navigation(self):
        self.context['chat_tab_id'] = 20
        with patch.object(ct, 'tab_command', side_effect=[{'tabs':[{'tabId':20,'url':self.context['chat_url']}]},{}]) as command, patch.object(ct, 'evaluate', return_value=True), patch.object(ct, 'evaluate_once', return_value='{"selected":true}'):
            self.assertTrue(ct.enter_chat_tab(self.context, 'job-a', ''))
        self.assertEqual([c.args[0] for c in command.call_args_list], ['list_tabs', 'attach_tab'])

    def test_existing_draft_or_ambiguous_recipient_stops(self):
        for result in ({'draft':True}, {'ambiguous':True}):
            with self.subTest(result=result), patch.object(ct, 'tab_command', return_value={'tabId':20}), patch.object(ct, 'evaluate', return_value=True), patch.object(ct, 'evaluate_once', return_value=json.dumps(result)), self.assertRaises(RuntimeError):
                ct.enter_chat_tab(dict(self.context), 'job-a', '')

    def test_lost_tab_creation_response_is_not_replayed(self):
        with patch.object(ct, 'api', side_effect=TimeoutError()) as api, self.assertRaises(TimeoutError):
            ct.tab_command('navigate', {'url':self.context['chat_url'], 'newTab':True})
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.kwargs['retries'], 1)

    def test_return_selects_retained_tab_without_back_or_reload(self):
        state = dict(on_list=True,same_document=True,same_nodes=True,order_preserved=True,missing_ids=[])
        with patch.object(ct, 'tab_command', side_effect=[{'tabs':[{'tabId':10,'url':self.context['url']}]},{}]) as command, patch.object(identity, 'verify_identity'), patch.object(lr, 'probe_list', return_value=state), patch.object(lr, 'evaluate_once', return_value=True) as action:
            result = lr.restore_list(self.context, 'job-a')
        self.assertTrue(result['preserved'])
        self.assertEqual(result['method'], 'retained_tab')
        self.assertEqual([c.args[0] for c in command.call_args_list], ['list_tabs','attach_tab'])
        self.assertFalse(any('history.back' in c.args[0] for c in action.call_args_list))

    def test_missing_or_reloaded_original_never_claims_preservation(self):
        for tabs, state in [([],{}), ([{'tabId':10,'url':self.context['url']}], {'on_list':True,'same_document':False})]:
            with self.subTest(tabs=tabs), patch.object(ct, 'tab_command', return_value={'tabs':tabs}), patch.object(identity, 'verify_identity'), patch.object(lr, 'probe_list', return_value=state), patch.object(lr, 'evaluate_once') as action:
                self.assertFalse(lr.restore_list(self.context, 'job-a', timeout=0)['preserved'])
                action.assert_not_called()

    def test_replaced_nodes_stop_even_when_ids_and_url_match(self):
        state = dict(on_list=True,same_document=True,same_nodes=False,order_preserved=True,missing_ids=[])
        with patch.object(ct, 'tab_command', return_value={'tabs':[{'tabId':10,'url':self.context['url']}]}), patch.object(identity, 'verify_identity'), patch.object(lr, 'probe_list', return_value=state), patch.object(lr, 'evaluate_once') as action:
            self.assertFalse(lr.restore_list(self.context, 'job-a')['preserved'])
            action.assert_not_called()


@unittest.skipUnless(shutil.which('node'), 'Node is needed for DOM fixtures')
class ConversationSelectionTests(unittest.TestCase):
    def test_exact_job_id_required_and_draft_never_discarded(self):
        fixture = r'''
const vm=require('vm'),fs=require('fs'),p=JSON.parse(fs.readFileSync(0,'utf8'));
let clicked=[];
const rows=p.jobs.map((id,i)=>({__vue__:{$props:{source:{encryptJobId:id}}},querySelector(){return {getClientRects(){return [1]},click(){clicked.push(i)}}}}));
const box={scrollTop:0,querySelectorAll(){return rows}};
const inputs=p.draft?[{tagName:'TEXTAREA',value:p.draft,getClientRects(){return [1]}}]:[];
const result=vm.runInNewContext(p.code,{document:{querySelector(){return box},querySelectorAll(){return inputs}}});
process.stdout.write(JSON.stringify({result:JSON.parse(result),clicked}));
'''
        for jobs, draft, selected in [(['wrong','job-a'],'',[1]),(['wrong'],'',[]), (['job-a','job-a'],'',[]),(['job-a'],'unsent draft',[])]:
            with self.subTest(jobs=jobs,draft=draft):
                p = subprocess.run(['node','-e',fixture], input=json.dumps({'code':ct.SELECT_CHAT_JS.replace('JOB_ID','"job-a"'), 'jobs':jobs,'draft':draft}), capture_output=True,text=True,check=True)
                self.assertEqual(json.loads(p.stdout)['clicked'], selected)


if __name__ == '__main__':
    unittest.main()
