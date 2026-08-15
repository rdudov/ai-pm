from __future__ import annotations

from email import policy
from email.parser import BytesParser
from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import daily_standup as daily
import outbound
import thread_tick


SAMPLE = {
    "intro": "Доброе утро. Сегодня есть четыре ясных фокуса.",
    "plans": [{"product": "Companion", "today": "Закончить выравнивание",
               "state": "идёт", "blocker": "нет"}],
    "questions": [{"question": "Удалять архив?", "recommendation": "Пока нет",
                   "tradeoff": "место против обратимости"}],
    "initiatives": [{"idea": "Показать первый результат", "effect": "быстрее понять пользу",
                     "first_step": "одна живая проба"}],
}


class DailyStandupRendering(unittest.TestCase):
    def test_thread_dry_run_cannot_call_the_daily_sender(self):
        source = Path(thread_tick.__file__).read_text(encoding="utf-8")
        daily_block = source[source.index("daily_result = None"):
                             source.index("# Before anything is observed")]
        self.assertIn('if args.thread == "process" and not args.dry_run:', daily_block)

    def test_one_source_makes_plain_and_real_html_table(self):
        plain = daily.render_plain(SAMPLE)
        html = daily.render_html(SAMPLE)
        self.assertIn("Companion", plain)
        self.assertIn("<table>", html)
        self.assertIn('data-label="Продукт">Companion</td>', html)
        self.assertNotIn("ПОВОД:", plain + html)
        self.assertNotIn("ВОПРОС:", plain + html)
        self.assertNotIn("мешает: нет", plain)
        self.assertNotIn('data-label="Что мешает">нет', html)

    def test_mobile_scale_is_explicit_and_headings_are_bounded(self):
        html = daily.render_html(SAMPLE)
        self.assertIn('name=viewport content="width=device-width, initial-scale=1"', html)
        self.assertIn("h1{font-size:24px", html)
        self.assertIn("@media(max-width:520px)", html)
        self.assertIn("content:attr(data-label)", html)

    def test_raw_message_contains_both_alternatives(self):
        raw = daily.raw_message("user@example.test", "Оперативка", "plain", "<b>html</b>")
        parsed = BytesParser(policy=policy.default).parsebytes(raw)
        parts = {part.get_content_type(): part.get_content() for part in parsed.walk()
                 if part.get_content_type() in {"text/plain", "text/html"}}
        self.assertEqual(parts["text/plain"].strip(), "plain")
        self.assertIn("<b>html</b>", parts["text/html"])

    def test_parser_ignores_router_notice_before_json(self):
        parsed = daily.parse_composition("route notice\n" + __import__("json").dumps(SAMPLE))
        self.assertEqual(parsed["intro"], SAMPLE["intro"])

    def test_empty_sources_cost_no_model_call(self):
        packet = {"plan": "", "snapshots": {"empty": ""},
                  "threads": {"process": {"thread": "process", "title": "p"}}}
        self.assertTrue(daily.mechanically_empty(packet))

    def test_first_tick_after_eight_catches_up(self):
        at = datetime(2026, 8, 15, 10, 20, tzinfo=timezone.utc)
        packet = {"plan": "plan", "snapshots": {}, "threads": {}}
        with mock.patch.object(daily, "sent_today", return_value=False), \
                mock.patch.object(daily, "source_packet", return_value=packet), \
                mock.patch.object(daily, "compose", return_value=SAMPLE), \
                mock.patch.object(thread_tick, "MAIL_TO", "user@example.test"), \
                mock.patch.object(thread_tick, "MAIL_SCRIPT", Path(__file__)), \
                mock.patch.object(thread_tick, "MAIL_PYTHON", Path(__file__)), \
                mock.patch.object(thread_tick, "deliver", return_value={"action": "send"}) as deliver:
            result = daily.maybe_send(at)
        self.assertEqual(result["action"], "send")
        deliver.assert_called_once()

    def test_missing_mail_door_costs_no_model_call(self):
        at = datetime(2026, 8, 15, 6, 0, tzinfo=timezone.utc)
        with mock.patch.object(daily, "sent_today", return_value=False), \
                mock.patch.object(thread_tick, "MAIL_TO", ""), \
                mock.patch.object(daily, "compose") as compose:
            result = daily.maybe_send(at)
        self.assertEqual(result["action"], "fail")
        compose.assert_not_called()

    def test_failed_delivery_is_retried_no_more_than_hourly(self):
        at = datetime(2026, 8, 15, 6, 20, tzinfo=timezone.utc)
        previous = {"action": "fail",
                    "at": datetime(2026, 8, 15, 6, 0,
                                   tzinfo=timezone.utc).isoformat(),
                    "reason": "gmail unavailable"}
        with mock.patch.object(daily, "sent_today", return_value=False), \
                mock.patch.object(daily, "compose") as compose:
            result = daily.maybe_send(at, previous=previous)
        self.assertEqual(result["action"], "fail")
        self.assertTrue(result["deferred"])
        compose.assert_not_called()

    def test_daily_kind_bypasses_the_proactive_threshold_and_carries_raw_mime(self):
        at = datetime(2026, 8, 15, 6, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.object(outbound, "LEDGER", Path(home) / "outbound.json"), \
                mock.patch.object(thread_tick, "send_mail", return_value="gmail-daily") as send:
            record = thread_tick.deliver(
                "portfolio", "daily", "Оперативка", "Доброе утро", None, at,
                raw_message=b"To: user@example.test\n\nbody")
        self.assertEqual(record["action"], "send")
        self.assertEqual(record["message_id"], "gmail-daily")
        self.assertEqual(send.call_args.kwargs["raw_message"], b"To: user@example.test\n\nbody")


if __name__ == "__main__":
    unittest.main()
