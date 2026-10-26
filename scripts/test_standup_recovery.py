"""Unread identity conservation and real process wake/delivery recovery edges."""
import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import daily_standup as daily
import outbound
import startup_context as startup
import thread_tick as tick

AT = datetime(2026, 8, 24, 8, tzinfo=timezone.utc)
DATA = {"intro": "Планы дня", "plans": [{"product": "Demo", "today": "Continue",
        "state": "Ready", "blocker": ""}], "questions": [], "initiatives": []}


def expanded(value):
    return sorted(({"source": str(Path(group["directory"]) / file["name"]),
                    **{k: v for k, v in file.items() if k != "name"}}
                   for group in value["directories"] for file in group["files"]),
                  key=lambda r: r["source"])


class UnreadPages(unittest.TestCase):
    def test_huge_inventory_is_complete_ordered_bounded_and_change_invalidates_resume(self):
        records = [{"source": f"/sources/event-{i // 10:06}/file-{i:06}.md",
                    "sha256": startup.digest_text(str(i)), "bytes": i + 10,
                    "chars": i + 5, "manifested": True, "reason": "open original"}
                   for i in range(10000)]
        original = copy.deepcopy(records)
        seen, continuation, pages = [], {}, 0
        while True:
            page = startup.record_page(records, AT, **continuation)
            pages += 1
            self.assertLessEqual(len(startup.render(page)), 160000)
            seen.extend(expanded(page))
            if not page["continuation"]:
                break
            continuation = {k: v for k, v in page["continuation"].items() if k != "frozen_at"}
        self.assertGreater(pages, 1)
        self.assertEqual([r["source"] for r in seen], [r["source"] for r in records])
        self.assertEqual(seen, [{k: v for k, v in r.items() if k not in {"manifested", "reason"}}
                                for r in records])
        self.assertEqual(records, original)
        for changed in [records[1:], records + [{**records[-1], "source": "/sources/new.md"}],
                        [{**records[0], "sha256": "changed"}] + records[1:]]:
            with self.subTest(kind=len(changed)), self.assertRaisesRegex(
                    startup.InputLimitError, "sources changed"):
                startup.record_page(changed, AT, **continuation)
        with self.assertRaises(startup.InputLimitError):
            startup.record_page(records, AT, after=records[1]["source"])

    def test_unicode_identity_and_one_oversized_identity_refuse_without_slicing(self):
        record = {"source": "/источники/полные слова.md", "sha256": "a" * 64,
                  "bytes": 100, "chars": 50}
        page = startup.record_page([record], AT)
        self.assertEqual(expanded(page), [record])
        with mock.patch.dict(startup.SECTION_CHARS, post_cursor_records=100):
            with self.assertRaises(startup.InputLimitError):
                startup.record_page([record], AT)


class DailyRecovery(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.state = self.home / "threads"
        self.state.mkdir()
        self.report = {"thread": "process", "title": "Process", "live_runs": [],
                       "needs_attention": [], "repos": [], "ready_to_start": [],
                       "decided_not_done": [], "can_pick_up": [], "undelivered": [],
                       "waiting_user": [], "worktrees": [], "owners_awake": []}
        patches = [mock.patch.object(outbound, "LEDGER", self.home / "outbound.json"),
                   mock.patch.object(tick, "STATE_DIR", self.state),
                   mock.patch.object(tick, "MAIL_TO", "user@example.test"),
                   mock.patch.object(tick, "MAIL_SCRIPT", Path(__file__)),
                   mock.patch.object(tick, "MAIL_PYTHON", Path(__file__)),
                   mock.patch.object(tick, "build", return_value=self.report),
                   mock.patch.object(tick, "runner_contract_alarm", return_value=([], None)),
                   mock.patch.object(tick, "goal_watch", return_value={"objects": [], "panel": [],
                       "reminder": None, "transitions": [], "standing": []}),
                   mock.patch.object(tick.goal_session, "watchdog", return_value={"mode": "none"}),
                   mock.patch.object(tick, "deliver_undelivered", return_value=[]),
                   mock.patch.object(outbound, "instruction_letter", return_value=None),
                   mock.patch.object(tick, "prompt", return_value="wake for actual daily event"),
                   mock.patch.object(startup, "packet", return_value={}),
                   mock.patch.object(daily, "source_packet", return_value={"plan": "Continue"}),
                   mock.patch.object(daily, "wake_health", return_value=[]),
                   mock.patch.object(daily, "compose", side_effect=lambda *args: copy.deepcopy(DATA))]
        for patch in patches:
            self.enterContext(patch)
        self.mail = self.enterContext(mock.patch.object(tick, "send_mail", return_value="mail-test"))
        self.engine = self.enterContext(mock.patch.object(tick.subprocess, "run", return_value=
            subprocess.CompletedProcess([], 0, "SILENT", "")))
        (self.state / "process.json").write_text(json.dumps({"snapshot": tick.snapshot(self.report)}))

    def process(self, at):
        with mock.patch.object(tick, "datetime", wraps=datetime) as clock, \
                mock.patch.object(sys, "argv", ["thread_tick.py", "process"]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            clock.now.return_value = at
            code = tick.main()
        return code, json.loads((self.state / "process.json").read_text())

    def test_source_refusal_wakes_without_news_deduplicates_then_success_resolves(self):
        with mock.patch.object(daily, "source_packet", side_effect=startup.InputLimitError(
                "post_cursor_records: 201330 symbols > 160000")):
            code, state = self.process(AT)
            self.assertEqual(code, 1)
            self.assertEqual(self.engine.call_count, 1)
            self.assertTrue(state["check"]["woke_owner"])
            self.assertIn("Оперативка", state["check"]["events"][0])
            self.assertEqual(state["daily_standup"]["action"], "fail")
            self.assertFalse(daily.sent_today(AT))
            self.process(AT + timedelta(minutes=20))
            self.process(AT + timedelta(hours=1))
            self.assertEqual(self.engine.call_count, 1)
            self.assertEqual(self.mail.call_count, 1)
        code, state = self.process(AT + timedelta(hours=2))
        self.assertEqual(code, 0)
        self.assertIsNone(state["snapshot"]["daily_failure"])
        self.assertEqual(state["daily_standup"]["action"], "send")
        self.assertTrue(daily.sent_today(AT))
        self.assertEqual(self.mail.call_count, 2)
        self.process(AT + timedelta(hours=3))
        self.assertEqual(self.mail.call_count, 2)
        journal = outbound.LEDGER.with_name("outbound-journal.jsonl").read_text()
        self.assertIn('"stage": "sources"', journal)
        self.assertIn('"action": "send"', journal)

    def test_parse_engine_and_delivery_refusal_all_wake_existing_process(self):
        cases = [(mock.patch.object(daily, "compose", side_effect=ValueError("malformed JSON")), "composition"),
                 (mock.patch.object(daily, "compose", side_effect=RuntimeError("Engine failed")), "composition"),
                 (mock.patch.object(tick, "MAIL_TO", ""), "delivery"),
                 (mock.patch.object(tick, "send_mail", return_value=False), "delivery")]
        for index, (patch, stage) in enumerate(cases):
            with self.subTest(stage=stage, case=index):
                (self.state / "process.json").write_text(json.dumps({"snapshot": tick.snapshot(self.report)}))
                # Distinct dates keep the real receipt owner active for each case.
                with patch:
                    code, state = self.process(AT + timedelta(days=index))
                self.assertEqual(code, 1)
                self.assertTrue(state["check"]["woke_owner"])
                self.assertEqual(state["daily_standup"]["stage"], stage)

    def test_failed_wake_does_not_consume_daily_failure_event(self):
        self.engine.return_value = subprocess.CompletedProcess([], 1, "", "failure")
        with mock.patch.object(tick, "MAIL_TO", ""):
            self.process(AT)
            self.process(AT + timedelta(minutes=20))
        self.assertEqual(self.engine.call_count, 2)

    def test_force_corrects_legacy_receipt_once_and_preserves_ordinary_dedup(self):
        # Old versions receipted their refusal notice as the day's normal daily.
        tick.deliver("portfolio", "daily", "Old refusal", "Plans unavailable", AT,
                     event_id=f"daily:{AT.date().isoformat()}")
        self.assertEqual(daily.maybe_send(AT)["action"], "skip")
        repaired = daily.maybe_send(AT, force=True)
        self.assertEqual(repaired["action"], "send")
        self.assertTrue(repaired["event_id"].endswith(":correction"))
        self.assertEqual(daily.maybe_send(AT, force=True)["action"], "skip")
        self.assertEqual(daily.maybe_send(AT)["action"], "skip")
        self.assertEqual(self.mail.call_count, 2)

    def test_failed_force_does_not_consume_correction_identity(self):
        tick.deliver("portfolio", "daily", "Old refusal", "Plans unavailable", AT,
                     event_id=f"daily:{AT.date().isoformat()}")
        with mock.patch.object(daily, "compose", side_effect=ValueError("bad JSON")):
            self.assertEqual(daily.maybe_send(AT, force=True)["action"], "fail")
        self.assertEqual(daily.maybe_send(AT, force=True)["action"], "send")
        self.assertEqual(daily.maybe_send(AT, force=True)["action"], "skip")


if __name__ == "__main__":
    unittest.main()
