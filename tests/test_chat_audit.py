import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import chat_audit


class AuditTests(unittest.TestCase):
    def test_greeting_count_comes_from_selected_history(self):
        with tempfile.TemporaryDirectory() as folder:
            delivered = [
                {'company': 'Synthetic One', 'greeting': 'Synthetic greeting for role number one'},
                {'company': 'Synthetic Two', 'greeting': 'Completely different synthetic greeting'},
            ]
            (Path(folder) / 'stream_progress.json').write_text(json.dumps({'delivered': delivered}), encoding='utf-8')
            rows = [{'text': 'HR One | Synthetic One | ' + delivered[0]['greeting']},
                    {'text': 'HR Two | Synthetic Two | ' + delivered[1]['greeting']},
                    {'text': 'HR Three | Synthetic Three | unrelated message'}]
            with patch.object(chat_audit, 'enumerate_conversations', return_value=rows):
                result, _ = chat_audit.audit(folder)
            self.assertEqual(result['conversations_with_our_greeting'], 2)
            self.assertEqual(result['A_greeting_is_latest'], 2)
