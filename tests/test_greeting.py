"""Offline checks: never connect to BOSS or send a real message."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import greeting as g
from scripts import phase_stream as ps
from scripts import _live_driver as drv
from scripts import pipeline_batch as batch
from scripts.webbridge_client import ProgressManager, evaluate_once


class GreetingTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(g, "evaluate", return_value="https://www.zhipin.com/web/geek/jobs?query=test"),
            patch.object(g, "evaluate_once", return_value="CLICKED:A"),
            patch.object(g, "navigate"),
            patch.object(g.time, "sleep"),
            patch.object(ps, "_panel_shows_job", return_value=True),
            patch.object(ps, "read_popup_type", return_value=""),
            patch.object(ps, "detect_page_anomaly", return_value=(False, "")),
        ]
        self.mocks = [p.start() for p in self.patches]
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])

    def run_flow(self, states, timeout=15):
        with patch.object(g, "chat_action", side_effect=states) as action:
            result = g.deliver_greeting({"jobId": "job-a"}, "你好，相关项目经历", timeout=timeout)
        return result, action

    def send_ready(self, **changes):
        return dict({"ready": True, "target": True, "input": "你好，相关项目经历",
                     "messages": [], "send_enabled": True}, **changes)

    def test_confirms_new_outgoing_delivery_and_returns_to_list(self):
        result, action = self.run_flow([
            {"ready": True, "target": True, "messages": [{"id": "old", "text": "旧消息"}]},
            {"ok": True}, self.send_ready(), {"ok": True},
            {"ready": True, "target": True, "input": "", "messages": [{"id": "new", "text": "你好，相关项目经历", "sent": True}]},
        ])
        self.assertTrue(result["delivered_ok"])
        self.assertEqual(result["message_id"], "new")
        self.assertEqual(sum(c.args[1:2] == ("send",) for c in action.call_args_list), 1)
        self.mocks[2].assert_called_once()

    def test_existing_identical_message_is_not_sent_twice(self):
        result, action = self.run_flow([{"ready": True, "target": True, "messages": [
            {"id": "old", "text": "你好，相关项目经历", "sent": True}]}])
        self.assertTrue(result["uncertain"])
        self.assertEqual(action.call_count, 1)

    def test_unrelated_draft_stops_before_send(self):
        result, action = self.run_flow([
            {"ready": True, "target": True, "messages": []},
            {"ok": False, "reason": "已有草稿"},
        ])
        self.assertTrue(result["stopped"])
        self.assertEqual(action.call_count, 2)

    def test_changed_conversation_after_send_is_uncertain(self):
        result, _ = self.run_flow([
            {"ready": True, "target": True, "messages": []},
            {"ok": True}, self.send_ready(), {"ok": True}, {"target": False},
        ])
        self.assertTrue(result["uncertain"])
        self.mocks[2].assert_not_called()

    def test_click_without_delivery_feedback_is_not_success(self):
        with patch.object(g.time, "monotonic", side_effect=[0, 0, 0, 0, 0, 100]):
            result, _ = self.run_flow([
                {"ready": True, "target": True, "messages": []},
                {"ok": True}, self.send_ready(), {"ok": True},
            ])
        self.assertTrue(result["uncertain"])
        self.assertFalse(result["delivered_ok"])

    def test_restore_failure_preserves_confirmed_send(self):
        self.mocks[2].side_effect = RuntimeError("navigation lost")
        result, _ = self.run_flow([
            {"ready": True, "target": True, "messages": []}, {"ok": True}, self.send_ready(), {"ok": True},
            {"ready": True, "target": True, "input": "", "messages": [{"id": "new", "text": "你好，相关项目经历", "sent": True}]},
        ])
        self.assertTrue(result["delivered_ok"])
        self.assertTrue(result["stopped"])

    def test_wrong_target_never_fills_or_sends(self):
        with patch.object(g.time, "monotonic", side_effect=[0, 0, 100]), patch.object(ps, "_verify_delivered_on_page", return_value=False):
            result, action = self.run_flow([{"ready": True, "target": False}])
        self.assertTrue(result["uncertain"])
        self.assertEqual(action.call_count, 1)

    def test_success_popup_opens_chat_but_does_not_count_as_custom_send(self):
        self.mocks[5].side_effect = ["", "greet_sent"]
        with patch.object(g, "continue_chat", return_value=True) as cont, patch.object(g.time, "monotonic", side_effect=[0, 0, 100]):
            result, _ = self.run_flow([{"ready": False}])
        cont.assert_called_once()
        self.assertFalse(result["delivered_ok"])

    def test_delayed_button_is_polled_then_clicked_only_once(self):
        result, action = self.run_flow([
            self.send_ready(input="", send_enabled=False), {"ok": True},
            self.send_ready(send_enabled=False), self.send_ready(send_enabled=False),
            self.send_ready(), {"ok": True},
            self.send_ready(input="", messages=[{"id": "new", "text": "你好，相关项目经历", "sent": True}]),
        ])
        self.assertTrue(result["delivered_ok"])
        self.assertEqual(sum(c.args[1:2] == ("send",) for c in action.call_args_list), 1)
        self.assertEqual(self.mocks[3].call_args_list[0].args, (2.0,))
        self.assertEqual([c.args[0] for c in self.mocks[3].call_args_list], [2.0, .5, .5])

    def test_disabled_button_timeout_never_clicks_send(self):
        with patch.object(g.time, "monotonic", side_effect=[0, 0, 0, 0, 100]):
            result, action = self.run_flow([
                self.send_ready(input=""), {"ok": True}, self.send_ready(send_enabled=False),
            ])
        self.assertTrue(result["uncertain"])
        self.assertIn("超时", result["reason"])
        self.assertFalse(any(c.args[1:2] == ("send",) for c in action.call_args_list))

    def test_changed_draft_target_or_duplicate_during_wait_never_sends(self):
        changes = [{"input": "用户新草稿"}, {"target": False}, {"ready": False},
                   {"messages": [{"id": "other", "text": "你好，相关项目经历"}]}]
        for change in changes:
            with self.subTest(change=change):
                result, action = self.run_flow([
                    self.send_ready(input=""), {"ok": True}, self.send_ready(**change),
                ])
                self.assertTrue(result["uncertain"])
                self.assertFalse(any(c.args[1:2] == ("send",) for c in action.call_args_list))

    def test_button_disabling_again_at_click_is_not_retried(self):
        result, action = self.run_flow([
            self.send_ready(input=""), {"ok": True}, self.send_ready(),
            {"ok": False, "reason": "发送按钮不可用或不唯一"},
        ])
        self.assertTrue(result["uncertain"])
        self.assertEqual(sum(c.args[1:2] == ("send",) for c in action.call_args_list), 1)

    def test_delivered_message_without_cleared_editor_is_not_success(self):
        with patch.object(g.time, "monotonic", side_effect=[0, 0, 0, 0, 0, 0, 100]):
            result, _ = self.run_flow([
                self.send_ready(input=""), {"ok": True}, self.send_ready(), {"ok": True},
                self.send_ready(messages=[{"id": "new", "text": "你好，相关项目经历", "sent": True}]),
            ])
        self.assertTrue(result["uncertain"])
        self.assertFalse(result["delivered_ok"])

    def test_cleared_editor_without_delivery_status_is_not_success(self):
        with patch.object(g.time, "monotonic", side_effect=[0, 0, 0, 0, 0, 0, 100]):
            result, _ = self.run_flow([
                self.send_ready(input=""), {"ok": True}, self.send_ready(), {"ok": True},
                self.send_ready(input="", messages=[{"id": "new", "text": "你好，相关项目经历", "sent": False}]),
            ])
        self.assertTrue(result["uncertain"])
        self.assertFalse(result["delivered_ok"])


class IntegrationTests(unittest.TestCase):
    def test_file_rejects_wrong_job_before_browser_operation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "greeting.json"
            path.write_text(json.dumps({"jobId": "other", "greeting": "你好"}), encoding="utf-8-sig")
            with self.assertRaises(ValueError):
                g.load_greeting(path, "job-a")

    def test_invalid_greetings(self):
        for value in (None, "", "  ", "x" * 501, "hello\x00", {"text": "hi"}):
            with self.subTest(value=type(value).__name__), self.assertRaises(ValueError):
                g.validate_greeting(value)

    def test_mutation_transport_never_retries(self):
        from scripts import webbridge_client as wb
        with patch.object(wb.http.client, "HTTPConnection") as connection:
            connection.return_value.request.side_effect = TimeoutError("lost response")
            with self.assertRaises(TimeoutError):
                evaluate_once("send()")
        self.assertEqual(connection.call_count, 1)

    def test_driver_persists_pending_before_send_and_deduplicates_after_crash(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(drv, "WORKDIR", folder), \
             patch.object(drv, "walk_state", return_value={}), patch.object(drv, "save_state"), \
             patch.object(drv, "cmd_open", return_value={"panel_ok": True, "detail": "完整JD", "title": "后端实习"}):
            path = os.path.join(folder, "stream_progress.json")
            def interrupted(*args, **kwargs):
                self.assertTrue(ProgressManager(path).load().is_done("job-a"))
                raise KeyboardInterrupt()
            with patch.object(drv, "deliver_greeting", side_effect=interrupted):
                with self.assertRaises(KeyboardInterrupt):
                    drv.cmd_deliver("job-a", greeting="你好")
            with patch.object(drv, "deliver_greeting") as send:
                self.assertTrue(drv.cmd_deliver("job-a", greeting="你好")["already_done"])
                send.assert_not_called()

    def test_driver_commits_message_and_clears_pending(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(drv, "WORKDIR", folder), \
             patch.object(drv, "walk_state", return_value={}), patch.object(drv, "save_state"), \
             patch.object(drv, "evaluate", return_value="另一个岗位"), \
             patch.object(drv, "cmd_open", return_value={"panel_ok": True, "detail": "完整JD", "title": "正确岗位"}), \
             patch.object(drv, "deliver_greeting", return_value={"delivered_ok": True, "verify": True, "message_id": "m1"}):
            drv.cmd_deliver("job-a", greeting="你好")
            data = ProgressManager(os.path.join(folder, "stream_progress.json")).load().data
            self.assertEqual(data["uncertain"], [])
            self.assertEqual(data["delivered"][0]["title"], "正确岗位")
            self.assertEqual(data["delivered"][0]["greeting"], "你好")
            self.assertEqual(data["delivered"][0]["message_id"], "m1")

    def test_legacy_delivery_still_uses_existing_path(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(drv, "WORKDIR", folder), \
             patch.object(drv, "walk_state", return_value={}), patch.object(drv, "save_state"), \
             patch.object(drv, "cmd_open", return_value={"panel_ok": True}), \
             patch.object(drv, "deliver_click", return_value={"delivered_ok": False}) as old, \
             patch.object(drv, "deliver_greeting") as custom:
            drv.cmd_deliver("job-a")
            old.assert_called_once()
            custom.assert_not_called()

    def test_batch_missing_greeting_fails_before_navigation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "decisions.json"
            path.write_text(json.dumps({"deliver": ["a", "b"], "greetings": {"a": "hi"}}), encoding="utf-8")
            with patch.object(batch, "load_pool", return_value={"a": {}, "b": {}}), patch.object(batch, "goto") as nav:
                with self.assertRaises(ValueError):
                    batch.cmd_go(path)
                nav.assert_not_called()


# Execute the actual browser JavaScript against a minimal DOM fixture with Node.
# This verifies DOM guards and escaping without touching a live chat.
DOM_FIXTURE = r"""
const fs = require('fs'), vm = require('vm');
const p = JSON.parse(fs.readFileSync(0,'utf8'));
let clicks = 0, events = 0;
const input = {tagName:'DIV',innerText:p.draft||'',getClientRects:()=>[1],focus(){},
    dispatchEvent(){events++},closest(){return root}};
const button = {className:p.disabled?'btn-send disabled':'btn-send',disabled:!!p.nativeDisabled,
    getClientRects:()=>[1],getAttribute:n=>n==='aria-disabled'&&p.ariaDisabled?'true':null,click(){clicks++}};
const root = {querySelector(s){
    if(s==='.chat-record')return {__vue__:{$data:{loading:!!p.loading,boss:{encryptJobId:p.activeId||'job-a'}}}};
    return null;
},querySelectorAll(s){
    if(s==='.message-item.item-myself')return [];
    return p.multipleButtons?[button,button]:[button];
}};
const sandbox={document:{querySelectorAll:()=>[input]},getComputedStyle:()=>({visibility:'visible'}),Event:class{}};
const result = JSON.parse(vm.runInNewContext(p.code,sandbox));
process.stdout.write(JSON.stringify({result,clicks,events,text:input.innerText}));
"""


@unittest.skipUnless(shutil.which("node"), "Node is needed for browser JS fixture tests")
class BrowserScriptTests(unittest.TestCase):
    def run_js(self, action, message="你好", **options):
        payload = {"jid": "job-a", "action": action, "message": message}
        code = g.CHAT_JS.replace("PAYLOAD", json.dumps(payload), 1)
        result = subprocess.run(["node", "-e", DOM_FIXTURE], input=json.dumps(dict(options, code=code)),
                                text=True, encoding="utf-8", capture_output=True, check=True)
        return json.loads(result.stdout)

    def test_wrong_conversation_cannot_send(self):
        result = self.run_js("send", activeId="another-job", draft="你好")
        self.assertFalse(result["result"]["ok"])
        self.assertEqual(result["clicks"], 0)

    def test_waits_for_history_before_filling_or_sending(self):
        result = self.run_js("send", draft="你好", loading=True)
        self.assertFalse(result["result"]["ready"])
        self.assertEqual(result["clicks"], 0)

    def test_existing_draft_is_not_overwritten(self):
        result = self.run_js("fill", draft="用户自己的草稿")
        self.assertFalse(result["result"]["ok"])
        self.assertEqual(result["text"], "用户自己的草稿")

    def test_message_escaping_and_input_event(self):
        text = '你好 "AI"\n` ${notExecuted} </script> \\ hello'
        result = self.run_js("fill", message=text)
        self.assertEqual(result["text"], text)
        self.assertTrue(result["result"]["ok"])
        self.assertEqual(result["events"], 1)

    def test_disabled_send_button_is_not_clicked(self):
        result = self.run_js("send", draft="你好", disabled=True)
        self.assertFalse(result["result"]["ok"])
        self.assertEqual(result["clicks"], 0)

    def test_read_exposes_button_readiness_without_clicking(self):
        for options in ({}, {"disabled": True}, {"nativeDisabled": True},
                        {"ariaDisabled": True}, {"multipleButtons": True}):
            with self.subTest(options=options):
                result = self.run_js("read", draft="你好", **options)
                self.assertEqual(result["result"]["send_enabled"], not bool(options))
                self.assertEqual(result["clicks"], 0)

    def test_valid_input_is_sent_once(self):
        result = self.run_js("send", draft="你好")
        self.assertTrue(result["result"]["ok"])
        self.assertEqual(result["clicks"], 1)


if __name__ == "__main__":
    unittest.main()
