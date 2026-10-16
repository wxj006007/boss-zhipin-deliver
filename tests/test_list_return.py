"""No refresh fallback, no repeated history mutation, no false preserved-list claims."""
import unittest
from unittest.mock import patch
from scripts import list_return as lr


class ReturnTests(unittest.TestCase):
    def setUp(self):
        self.context = dict(token='saved', url='https://www.zhipin.com/web/geek/jobs?query=x',
                            ids=['a', 'b'], timeOrigin=100, historyLength=50)
        self.chat = dict(url='https://www.zhipin.com/web/geek/chat', referrer=self.context['url'],
                         historyLength=50, on_list=False, same_document=False)
        self.back = dict(url=self.context['url'], on_list=True, same_document=True,
                         chat_visible=False, missing_ids=[], order_preserved=True)

    def test_restored_document_succeeds_with_one_back(self):
        with patch.object(lr, 'probe_list', side_effect=[self.chat, self.back]), \
             patch.object(lr, 'evaluate_once', return_value=True) as action:
            result = lr.restore_list(self.context, 'a')
        self.assertTrue(result['preserved'])
        self.assertEqual(sum('history.back()' in c.args[0] for c in action.call_args_list), 1)

    def test_already_on_original_list_never_navigates(self):
        with patch.object(lr, 'probe_list', return_value=self.back), \
             patch.object(lr, 'evaluate_once', return_value=True) as action:
            self.assertTrue(lr.restore_list(self.context, 'a')['preserved'])
        self.assertFalse(any('history.back()' in c.args[0] for c in action.call_args_list))

    def test_url_alone_is_not_preservation_and_no_refresh_fallback(self):
        new_document = dict(self.back, same_document=False)
        with patch.object(lr, 'probe_list', side_effect=[self.chat, new_document]), \
             patch.object(lr, 'evaluate_once', return_value=True) as action:
            result = lr.restore_list(self.context, 'a', timeout=0)
        self.assertFalse(result['preserved'])
        self.assertEqual(action.call_count, 1)

    def test_unrelated_history_never_goes_back(self):
        for change in ({'referrer': 'https://other.invalid'}, {'historyLength': 49},
                       {'url': 'https://www.zhipin.com/web/geek/resume'}):
            with self.subTest(change=change), patch.object(lr, 'probe_list', return_value=dict(self.chat, **change)), \
                 patch.object(lr, 'evaluate_once') as action:
                self.assertFalse(lr.restore_list(self.context, 'a')['preserved'])
                action.assert_not_called()

    def test_remaining_job_missing_or_reordered_stops(self):
        for change in ({'missing_ids': ['b']}, {'order_preserved': False}, {'chat_visible': True}):
            with self.subTest(change=change), patch.object(lr, 'probe_list', return_value=dict(self.back, **change)), \
                 patch.object(lr, 'evaluate_once') as action:
                self.assertFalse(lr.restore_list(self.context, 'a')['preserved'])
                action.assert_not_called()

    def test_lost_back_response_is_not_replayed(self):
        with patch.object(lr, 'probe_list', return_value=self.chat), \
             patch.object(lr, 'evaluate_once', side_effect=TimeoutError()) as action:
            with self.assertRaises(TimeoutError):
                lr.restore_list(self.context, 'a')
            self.assertEqual(action.call_count, 1)


    def test_forward_history_tail_does_not_require_length_increase(self):
        context = dict(self.context, historyLength=3, historyKey='list-entry', historyIndex=1)
        chat = dict(self.chat, historyLength=3, history_key='chat-entry', history_index=2,
                    previous_entry={'key':'list-entry', 'index':1, 'url':context['url']})
        with patch.object(lr, 'probe_list', side_effect=[chat, self.back]), patch.object(lr, 'evaluate_once', return_value=True) as action:
            self.assertTrue(lr.restore_list(context, 'a')['preserved'])
        self.assertEqual(sum('history.back()' in c.args[0] for c in action.call_args_list), 1)

    def test_wrong_previous_entry_cannot_fall_back_to_length_heuristic(self):
        context = dict(self.context, historyKey='list-entry', historyIndex=1)
        chat = dict(self.chat, history_key='chat-entry', history_index=2,
                    previous_entry={'key':'different-entry', 'index':1, 'url':context['url']})
        with patch.object(lr, 'probe_list', return_value=chat), patch.object(lr, 'evaluate_once') as action:
            self.assertFalse(lr.restore_list(context, 'a')['preserved'])
            action.assert_not_called()


if __name__ == '__main__':
    unittest.main()
