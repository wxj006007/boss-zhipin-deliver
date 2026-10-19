"""Background loading, explicit EOF and independent account-session regressions."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import shutil
import subprocess
import threading
import unittest
from unittest.mock import patch

from scripts import job_list_loading as loader, _live_driver as driver


def state(**changes):
    return dict(url='https://www.zhipin.com/web/geek/jobs?query=test', time_origin=1,
                on_list=True, ready=True, ids=['a'], count=1, page=1,
                has_more=True, loading=False, end_marker='', visibility='hidden', **changes)


class LoadingTests(unittest.TestCase):
    def run_load(self, states, **kwargs):
        clock = [0.0]
        values = iter(states)
        last = [states[-1]]
        def read(session):
            last[0] = next(values, last[0])
            return last[0]
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(loader, 'read_state', side_effect=read), \
             patch.object(loader.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(loader.time, 'sleep', side_effect=sleep), \
             patch.object(loader.wb, 'evaluate_once', return_value='{}') as trigger:
            result = loader.advance_once(session='account-a', **kwargs)
        return result, trigger

    def test_delayed_growth_waits_for_loaded_ids_in_hidden_tab(self):
        before = state()
        result, trigger = self.run_load([before, dict(before, loading=True),
                                        dict(before, ids=['a','b'], count=2, page=2)])
        self.assertEqual(result['status'], 'loaded')
        self.assertEqual(result['new_ids'], ['b'])
        self.assertFalse(result['confirmed_end'])
        self.assertEqual(trigger.call_args.kwargs['session'], 'account-a')
        self.assertEqual(trigger.call_count, 1)

    def test_stall_has_more_is_never_exhaustion_and_retries_are_bounded(self):
        result, trigger = self.run_load([state()], timeout=5)
        self.assertEqual(result['status'], 'stalled')
        self.assertFalse(result['confirmed_end'])
        self.assertEqual(trigger.call_count, 2)

    def test_pending_network_load_is_not_retriggered_or_reported_as_eof(self):
        result, trigger = self.run_load([dict(state(), loading=True)], timeout=1)
        self.assertEqual(result['status'], 'loading')
        trigger.assert_not_called()

    def test_terminal_response_retains_final_batch(self):
        before = state()
        result, _ = self.run_load([before, dict(before, ids=['a','b'], count=2, has_more=False)])
        self.assertEqual(result['status'], 'exhausted')
        self.assertEqual(result['new_ids'], ['b'])

    def test_duplicate_page_is_progress_not_eof(self):
        before = state()
        result, _ = self.run_load([before, dict(before, page=2)])
        self.assertEqual(result['status'], 'advanced')
        self.assertFalse(result['confirmed_end'])

    def test_explicit_end_requires_settled_and_ready_page(self):
        self.assertTrue(loader.is_exhausted(dict(state(), has_more=False)))
        self.assertFalse(loader.is_exhausted(dict(state(), has_more=False, loading=True)))
        self.assertFalse(loader.is_exhausted(dict(state(), has_more=False, ready=False)))
        self.assertFalse(loader.is_exhausted(dict(state(), has_more=True, end_marker='没有更多了')))
        self.assertTrue(loader.is_exhausted(dict(state(), has_more=None, end_marker='没有更多了')))
        self.assertFalse(loader.is_exhausted(dict(state(), has_more=None)))

    def test_document_or_search_change_aborts(self):
        before = state()
        for changed in [dict(before, time_origin=2), dict(before, url=before['url']+'&city=other')]:
            with self.subTest(changed=changed), self.assertRaises(loader.wb.AbortScriptError):
                self.run_load([before, changed])

    def test_invalid_bridge_response_is_not_empty_list_or_eof(self):
        for raw in ['', '{}', 'null', json.dumps(dict(state(), on_list=False))]:
            with self.subTest(raw=raw), patch.object(loader.wb, 'evaluate', return_value=raw), \
                 self.assertRaises(loader.wb.AbortScriptError):
                loader.read_state()

    def test_legacy_exhaustion_does_not_convert_stall_or_cap_to_success(self):
        for status, cap in [('stalled', 10), ('loaded', 1)]:
            outcome = dict(status=status, confirmed_end=False, after={'count': 15})
            with self.subTest(status=status), patch.object(driver, 'extract_jobs', return_value=[]), \
                 patch.object(driver, 'scroll_jobs_list', return_value=outcome):
                exhausted, total, _ = driver._exhaust_recommend_feed(cap=cap)
            self.assertFalse(exhausted)
            self.assertEqual(total, 15)

    def test_two_hidden_account_sessions_load_concurrently_without_focus_commands(self):
        states = {s: dict(state(), url='https://www.zhipin.com/web/geek/jobs?query='+s, ids=[s]) for s in ['a','b']}
        barrier = threading.Barrier(2)
        calls = []
        def evaluate(code, session):
            self.assertEqual(code, loader.STATE_JS)
            return json.dumps(deepcopy(states[session]))
        def trigger(code, session):
            self.assertIn('query='+session, code)
            calls.append(session)
            barrier.wait(timeout=2)
            states[session].update(ids=[session, session+'-next'], count=2, page=2)
            return '{}'
        with patch.object(loader.wb, 'evaluate', side_effect=evaluate), \
             patch.object(loader.wb, 'evaluate_once', side_effect=trigger), \
             patch.object(loader.wb, 'api') as api, ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda s: loader.advance_once(session=s), ['a','b']))
        self.assertEqual([r['new_ids'] for r in results], [['a-next'], ['b-next']])
        self.assertCountEqual(calls, ['a','b'])
        api.assert_not_called()  # No Page.bringToFront/Target.activateTarget/focus emulation.


@unittest.skipUnless(shutil.which('node'), 'Node is needed for DOM fixtures')
class ScrollEventTests(unittest.TestCase):
    def test_background_scroll_notifies_actual_container_and_page_even_at_bottom(self):
        fixture = r'''
const vm=require('vm'),fs=require('fs'),p=JSON.parse(fs.readFileSync(0,'utf8'));
let events=[];
const page={scrollTop:0,scrollHeight:3000,clientHeight:800,dispatchEvent:e=>events.push('page:'+e.type)};
const nested={scrollTop:1000,scrollHeight:1200,clientHeight:200,className:'nested',parentElement:null,dispatchEvent:e=>events.push('nested:'+e.type)};
const list={parentElement:p.nested?nested:null};
const document={scrollingElement:page,documentElement:page,body:{},querySelector:s=>s==='.job-card-wrap'?{parentElement:list}:list,dispatchEvent:e=>events.push('document:'+e.type)};
const sandbox={document,window:{dispatchEvent:e=>events.push('window:'+e.type)},
 location:{href:p.changed?'wrong':'https://www.zhipin.com/web/geek/jobs?query=test'},performance:{timeOrigin:1},
 getComputedStyle:e=>({overflowY:e===nested?'auto':'visible'}),Event:class{constructor(type){this.type=type}}};
let error=null;try{vm.runInNewContext(p.code,sandbox);vm.runInNewContext(p.code,sandbox)}catch(e){error=String(e)}
process.stdout.write(JSON.stringify({events,error,top:p.nested?nested.scrollTop:page.scrollTop}));
'''
        code = loader.SCROLL_JS.replace('EXPECTED', json.dumps({'url':state()['url'], 'time_origin':1}), 1)
        for nested in [False, True]:
            for changed in [False, True]:
                with self.subTest(nested=nested, changed=changed):
                    proc = subprocess.run(['node','-e',fixture], input=json.dumps(dict(code=code, nested=nested, changed=changed)), text=True, capture_output=True, check=True)
                    result = json.loads(proc.stdout)
                    if changed:
                        self.assertEqual(result['events'], [])
                        self.assertIn('DOCUMENT_CHANGED', result['error'])
                    else:
                        container = 'nested' if nested else 'page'
                        self.assertEqual(result['events'], [container+':scroll','document:scroll','window:scroll']*2)
                        self.assertEqual(result['top'], 1200 if nested else 3000)


if __name__ == '__main__':
    unittest.main()
