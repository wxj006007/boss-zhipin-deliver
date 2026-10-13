"""Offline regressions for combining the installed fixes with JD greetings."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from scripts import _live_driver as drv
from scripts import greeting as g
from scripts import phase_stream as ps
from scripts import pipeline_batch as batch


class MergeTests(unittest.TestCase):
    def test_open_recovers_card_outside_initial_dom_and_reads_target_jd(self):
        with patch.object(drv, "evaluate", side_effect=[False, "正确岗位JD", "岗位标题"]), \
             patch.object(drv, "click", return_value=False), patch.object(drv.time, "sleep"), \
             patch.object(drv, "_panel_shows_job", side_effect=[False, True]), \
             patch.object(drv, "_try_locate_card", return_value="ok") as locate:
            result = drv.cmd_open("job-a")
        locate.assert_called_once_with("job-a", "", session=drv.SESSION)
        self.assertTrue(result["panel_ok"])
        self.assertEqual(result["detail"], "正确岗位JD")

    def test_absent_card_never_returns_stale_jd(self):
        with patch.object(drv, "evaluate", return_value=False) as read, \
             patch.object(drv, "click", return_value=False), patch.object(drv.time, "sleep"), \
             patch.object(drv, "_panel_shows_job", return_value=False), \
             patch.object(drv, "_try_locate_card", return_value="absent"):
            result = drv.cmd_open("job-a")
        self.assertTrue(result["not_found"])
        self.assertEqual(result["detail"], "")
        self.assertEqual(read.call_count, 1)

    def driver_failure(self, opening, greeting):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(drv, "WORKDIR", folder), \
             patch.object(drv, "cmd_open", return_value=opening), \
             patch.object(drv, "walk_state", return_value={"consecutive_failures": 2}), \
             patch.object(drv, "save_state") as save, \
             patch.object(drv, "deliver_greeting") as custom, \
             patch.object(drv, "deliver_click", return_value={"not_found": True}) as legacy:
            result = drv.cmd_deliver("job-a", greeting=greeting)
            pending_exists = (Path(folder) / "stream_progress.json").exists()
        return result, save, custom, legacy, pending_exists

    def test_absent_custom_job_skips_without_failure_breaker_or_pending_record(self):
        result, save, custom, legacy, pending = self.driver_failure(
            {"panel_ok": False, "not_found": True}, "你好")
        self.assertTrue(result["not_found"])
        save.assert_not_called()
        custom.assert_not_called()
        legacy.assert_not_called()
        self.assertFalse(pending)

    def test_unconfirmed_panel_never_sends_custom_greeting_and_stops_on_third_failure(self):
        result, save, custom, legacy, _ = self.driver_failure({"panel_ok": False}, "你好")
        self.assertTrue(result["stopped"])
        self.assertTrue(save.call_args.args[0]["delivery_halted"])
        custom.assert_not_called()
        legacy.assert_not_called()

    def test_empty_jd_never_sends_custom_greeting(self):
        result, _, custom, legacy, pending = self.driver_failure(
            {"panel_ok": True, "detail": ""}, "你好")
        self.assertFalse(result["delivered_ok"])
        custom.assert_not_called()
        legacy.assert_not_called()
        self.assertFalse(pending)

    def test_legacy_fallback_is_preserved_and_not_found_does_not_trip_breaker(self):
        result, save, custom, legacy, _ = self.driver_failure({"panel_ok": False}, None)
        self.assertTrue(result["not_found"])
        legacy.assert_called_once()
        custom.assert_not_called()
        save.assert_not_called()

    def test_batch_navigation_remembers_current_keyword_page(self):
        with patch.dict(ps.CURRENT_WORK_URL, {"url": ""}), \
             patch.object(batch.wb, "navigate"), patch.object(batch.time, "sleep"), \
             patch.object(batch, "kw_url", return_value="https://www.zhipin.com/web/geek/jobs?query=python"), \
             patch.object(batch.wb, "detect_page_anomaly", return_value=(False, "")), \
             patch.object(batch.wb, "evaluate", return_value="/web/geek/jobs"), \
             patch.object(batch.wb, "extract_jobs", return_value=[]):
            batch.goto("python")
            self.assertEqual(ps.CURRENT_WORK_URL["url"], batch.kw_url("python"))

    def test_unexpected_chat_returns_to_current_search_instead_of_recommendations(self):
        url = "https://www.zhipin.com/web/geek/jobs?query=python"
        with patch.dict(ps.CURRENT_WORK_URL, {"url": url}), \
             patch.object(ps, "snapshot_page", return_value=("/web/geek/chat", "", "")), \
             patch.object(ps, "navigate") as nav, patch.object(ps.time, "sleep"):
            ps._ensure_recommend()
        self.assertEqual(nav.call_args.args[0], url)
        self.assertEqual(nav.call_args.kwargs["expect_url_contains"], "query=")

    def popup_result(self, readings):
        with ExitStack() as stack:
            stack.enter_context(patch.object(ps, "read_popup_type", side_effect=readings))
            close = stack.enter_context(patch.object(ps, "safe_close_popup"))
            for name in ("_ensure_recommend", "click_popup_button"):
                stack.enter_context(patch.object(ps, name))
            stack.enter_context(patch.object(ps, "_try_locate_card", return_value="ok"))
            stack.enter_context(patch.object(ps, "detect_page_anomaly", return_value=(False, "")))
            stack.enter_context(patch.object(ps, "evaluate", return_value="CLICKED:A"))
            stack.enter_context(patch.object(ps, "_feedback", return_value=("/web/geek/jobs", "", "", False)))
            stack.enter_context(patch.object(ps.time, "sleep"))
            result = ps.deliver_click({"jobId": "job-a"})
        return result, close

    def test_fading_success_dialog_does_not_stop_batch(self):
        result, close = self.popup_result(["", "greet_sent", "greet_sent", "tip", ""])
        self.assertTrue(result["delivered_ok"])
        self.assertFalse(result["stopped"])
        self.assertEqual(close.call_count, 3)

    def test_unknown_dialog_after_success_still_stops(self):
        result, _ = self.popup_result(["", "greet_sent", "unknown"])
        self.assertTrue(result["stopped"])

    @unittest.skipUnless(shutil.which("node"), "Node is required for the dialog DOM fixture")
    def test_custom_flow_enters_chat_from_new_greet_container(self):
        with patch.object(g, "evaluate_once", return_value=True) as evaluate:
            g.continue_chat()
        code = evaluate.call_args.args[0]
        fixture = r"""
const fs = require('fs'), vm = require('vm');
let clicks = 0;
const button = {textContent:'继续沟通',getClientRects:()=>[1],click(){clicks++}};
const dialog = {getClientRects:()=>[1],querySelectorAll:()=>[button]};
const document = {
  querySelector:()=>null,
  querySelectorAll:s=>s.split(',').includes('.greet-boss-container')?[dialog]:[]
};
const result = vm.runInNewContext(fs.readFileSync(0,'utf8'), {document});
process.stdout.write(JSON.stringify({result,clicks}));
"""
        result = subprocess.run(["node", "-e", fixture], input=code, text=True,
                                encoding="utf-8", capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), {"result": True, "clicks": 1})


if __name__ == "__main__":
    unittest.main()
