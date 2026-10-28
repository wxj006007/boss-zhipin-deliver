"""Offline regression: rotation, stale approvals and crash recovery must not send twice."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import execution_context as runtime_context
from scripts import immediate as im, accounts


class ImmediateTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        import os
        self.stack.enter_context(patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                                       if not k.startswith('BOSS_')}, clear=True))
        self.stack.enter_context(patch.object(accounts, 'CONTROL_DIR', self.root / 'control'))
        self.stack.enter_context(patch.object(runtime_context, 'CONTROL_DIR', self.root / 'control'))
        self.stack.enter_context(patch.object(im, 'resolve_workdir', return_value=str(self.root)))
        self.stack.enter_context(patch.object(im.drv, 'load_profile_cached', return_value={}))
        self.stack.enter_context(patch.object(im.drv, 'walk_state', return_value={}))
        self.stack.enter_context(patch.object(im.drv, 'verify_identity'))
        self.flow = im.Immediate()
        self.state = dict(run='single', profile_key=self.flow.profile_key, keyword='Python',
                          url='https://www.zhipin.com/web/geek/jobs?query=Python', pending=None, handled={})
        self.flow.save(self.state)
        self.stack.enter_context(patch.object(im.wb, 'evaluate', return_value=self.state['url']))
        self.stack.enter_context(patch.object(im.wb, 'detect_page_anomaly', return_value=(False, '')))
        self.stack.enter_context(patch.object(im.ps, 'card_exists', return_value=True))
        self.scroll = self.stack.enter_context(patch.object(im.ps, 'scroll_jobs_list',
            return_value={'status': 'exhausted', 'confirmed_end': True}))
        self.cards = self.stack.enter_context(patch.object(im.wb, 'extract_jobs', return_value=[{'jobId': 'a'}]))
        self.open = self.stack.enter_context(patch.object(im.drv, 'cmd_open', return_value={
            'panel_ok': True, 'detail': '完整JD', 'hr_activity': {'allowed': True}}))
        self.send = self.stack.enter_context(patch.object(im.drv, 'cmd_deliver', return_value={'delivered_ok': True}))

    def decision(self, item, action='approve'):
        return dict(jobId=item['job']['jobId'], review_id=item['review_id'], action=action,
                    reason='智能体根据画像及完整JD审核', greeting='您好，相关经历')

    def test_reads_only_one_and_repeated_next_keeps_same_review(self):
        self.cards.return_value = [{'jobId': 'a'}, {'jobId': 'b'}]
        first = self.flow.next()
        self.assertEqual(first, self.flow.next())
        self.assertEqual(self.open.call_count, 1)
        self.send.assert_not_called()
        with self.assertRaises(ValueError):
            self.flow.start('Java')

    def test_after_send_uses_current_cards_not_old_queue(self):
        first = self.flow.next()
        self.flow.decide(self.decision(first))
        self.cards.return_value = [{'jobId': 'a'}, {'jobId': 'new'}]
        second = self.flow.next()
        self.assertEqual(second['job']['jobId'], 'new')
        self.assertEqual([x.args[0] for x in self.open.call_args_list], ['a', 'new'])
        self.assertEqual(self.send.call_args.kwargs['reviewed_detail'], '完整JD')

    def test_reject_is_persistent_and_does_not_send(self):
        self.flow.decide(self.decision(self.flow.next(), 'reject'))
        self.cards.return_value = [{'jobId': 'a'}, {'jobId': 'b'}]
        self.assertEqual(self.flow.next()['job']['jobId'], 'b')
        self.send.assert_not_called()

    def test_wrong_or_old_review_never_sends(self):
        decision = self.decision(self.flow.next())
        for key in ['jobId', 'review_id']:
            with self.assertRaises(ValueError):
                self.flow.decide(dict(decision, **{key: 'wrong'}))
        self.send.assert_not_called()

    def test_changed_panel_forces_fresh_review(self):
        item = self.flow.next()
        self.send.return_value = {'review_required': True}
        self.assertTrue(self.flow.decide(self.decision(item))['review_required'])
        fresh = self.flow.next()
        self.assertNotEqual(fresh['review_id'], item['review_id'])
        with self.assertRaises(ValueError):
            self.flow.decide(self.decision(item))

    def test_uncertain_blocks_next_and_start(self):
        (self.root / 'stream_progress.json').write_text(json.dumps({'uncertain': [{'jobId': 'a'}]}))
        with self.assertRaises(ValueError):
            self.flow.next()
        with self.assertRaises(ValueError):
            self.flow.start('Python')
        self.open.assert_not_called()

    def test_crash_after_committed_send_recovers_without_resending(self):
        self.flow.next()
        (self.root / 'stream_progress.json').write_text(json.dumps({'delivered': [{'jobId': 'a'}]}))
        self.cards.return_value = [{'jobId': 'a'}, {'jobId': 'b'}]
        self.assertEqual(self.flow.next()['job']['jobId'], 'b')
        self.send.assert_not_called()

    def test_search_filters_must_still_match(self):
        item = self.flow.next()
        with patch.object(im.wb, 'evaluate', return_value=self.state['url'] + '&city=other'):
            with self.assertRaises(ValueError):
                self.flow.decide(self.decision(item))
        self.send.assert_not_called()

    def test_stalled_loading_never_reports_exhausted(self):
        self.state['handled'] = {'a': 'rejected'}
        self.flow.save(self.state)
        self.scroll.return_value = {'status': 'stalled', 'confirmed_end': False}
        result = self.flow.next()
        self.assertFalse(result['view_exhausted'])
        self.assertIn('next_deferred', result)
        self.open.assert_not_called()
        events = [json.loads(s) for s in (self.root / 'immediate_events.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(events[-1]['stage'], 'load_deferred')

    def test_last_batch_is_reviewed_before_confirmed_end_is_returned(self):
        self.state['handled'] = {'a': 'rejected'}
        self.flow.save(self.state)
        self.cards.side_effect = [[{'jobId': 'a'}], [{'jobId': 'a'}, {'jobId': 'last'}]]
        result = self.flow.next()
        self.assertEqual(result['job']['jobId'], 'last')
        self.assertTrue(result['awaiting_decision'])
        self.scroll.assert_called_once()

    def test_only_explicit_terminal_loading_result_reports_exhaustion(self):
        self.state['handled'] = {'a': 'rejected'}
        self.flow.save(self.state)
        result = self.flow.next()
        self.assertTrue(result['view_exhausted'])
        self.assertTrue(result['search_exhausted'])
        self.open.assert_not_called()

    def test_scan_budget_is_not_exhaustion(self):
        self.state['handled'] = {'a': 'rejected'}
        self.flow.save(self.state)
        self.scroll.return_value = {'status': 'advanced', 'confirmed_end': False}
        result = self.flow.next()
        self.assertFalse(result['view_exhausted'])
        self.assertIn('next_deferred', result)
        self.assertEqual(self.scroll.call_count, 2)

    def test_changed_jd_driver_never_starts_communication(self):
        # Exercise the saved actual driver with browser primitives mocked.
        with patch.object(im.drv, 'WORKDIR', str(self.root)), patch.object(im.drv, '_panel_shows_job', return_value=True), \
             patch.object(im.drv, 'deliver_greeting') as send:
            result = self.original_deliver('a', greeting='您好', reviewed_detail='另一份JD')
            self.assertTrue(result['review_required'])
            send.assert_not_called()

    original_deliver = staticmethod(im.drv.cmd_deliver)


if __name__ == '__main__':
    unittest.main()
