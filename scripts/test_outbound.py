#!/usr/bin/env python3
"""Regressions for channel selection before product-message composition."""
from __future__ import annotations

import contextlib
import hashlib
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

sys.path.insert(0, str(Path(__file__).resolve().parent))

import outbound
import plain_russian
import process_map_state as pms
import thread_state
import thread_tick as tick

AT = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def composed(**changes) -> str:
    value = {"channel": "gmail", "kind": "report",
             "event_id": "report:task-1280:accepted", "subject": "Готово",
             "body": "Пользователь получил результат.", "attachments": []}
    value.update(changes)
    return json.dumps(value, ensure_ascii=False)


class ComposerSelectsBeforeText(unittest.TestCase):
    def test_silent_creates_no_message(self):
        self.assertIsNone(tick.parse_composed_message("SILENT"))

    def test_paired_bold_markers_do_not_destroy_silence(self):
        self.assertIsNone(tick.parse_composed_message(" **SILENT**\n"))

    def test_an_outer_markdown_fence_does_not_destroy_composed_json(self):
        message = tick.parse_composed_message(f"```json\n{composed()}\n```")
        self.assertEqual(message["event_id"], "report:task-1280:accepted")

    def test_one_unambiguous_fenced_envelope_after_prose_is_parsed(self):
        message = tick.parse_composed_message(
            f"Составитель записал служебный итог.\n```json\n{composed()}\n```")
        self.assertEqual(message["channel"], "gmail")
        self.assertEqual(message["event_id"], "report:task-1280:accepted")

    def test_different_fenced_blocks_are_an_explicit_failure(self):
        first = f"```json\n{composed()}\n```"
        second = f"```json\n{composed(body='Другой текст')}\n```"
        response = f"{first}\n{second}"
        with self.assertRaisesRegex(ValueError, "multiple different fenced blocks"):
            tick.parse_composed_message(response)

    def test_identical_fenced_blocks_have_one_unambiguous_envelope(self):
        block = f"```json\n{composed()}\n```"
        message = tick.parse_composed_message(f"{block}\nповтор\n{block}")
        self.assertEqual(message["event_id"], "report:task-1280:accepted")

    def test_malformed_fenced_json_is_refused(self):
        with self.assertRaisesRegex(ValueError, "neither SILENT nor JSON"):
            tick.parse_composed_message("пояснение\n```json\n{not-json}\n```")

    def test_prose_and_punctuation_are_not_interpreted_as_the_envelope(self):
        for response in ("SILENT.", f"Письмо:\n{composed()}"):
            with self.subTest(response=response), self.assertRaises(ValueError):
                tick.parse_composed_message(response)

    def test_channel_and_identity_precede_text(self):
        message = tick.parse_composed_message(composed())
        self.assertEqual(message["channel"], "gmail")
        self.assertEqual(message["event_id"], "report:task-1280:accepted")

    def test_key_order_does_not_destroy_an_already_composed_message(self):
        value = json.loads(composed())
        reordered = {"body": value.pop("body"), **value}
        message = tick.parse_composed_message(
            json.dumps(reordered, ensure_ascii=False))
        self.assertEqual(message["event_id"], "report:task-1280:accepted")
        self.assertEqual(message["body"], "Пользователь получил результат.")

    def test_background_composer_cannot_select_telegram(self):
        with self.assertRaisesRegex(ValueError, "only gmail"):
            tick.parse_composed_message(composed(channel="telegram"))

    def test_question_is_an_explicit_kind_not_a_guess_from_prose(self):
        message = tick.parse_composed_message(composed(
            kind="question", event_id="question:task-1280:choose-path",
            body="Выберите один из двух вариантов."))
        self.assertEqual(message["kind"], "question")

    def test_attachments_are_absolute_or_refused(self):
        with self.assertRaisesRegex(ValueError, "absolute"):
            tick.parse_composed_message(composed(attachments=["report.html"]))

    def test_malformed_composer_response_stays_in_the_existing_thread_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory, "process.json")
            ledger_path = Path(directory, "outbound.json")
            with mock.patch.object(outbound, "LEDGER", ledger_path):
                tick.persist_composer_failure(
                    state_path,
                    {"thread": "process", "updated_at": AT.isoformat(),
                     "snapshot": {"ready": [1286]},
                     "check": {"woke_owner": True}},
                    "Готовый текст без конверта", ValueError("expected JSON"),
                    retry_snapshot={"ready": []})
            saved = json.loads(state_path.read_text())
            journal = json.loads(Path(directory, "outbound-journal.jsonl").read_text())
        self.assertEqual(saved["composer_failure"]["raw_response"],
                         "Готовый текст без конверта")
        self.assertEqual(saved["composer_failure"]["error"], "expected JSON")
        self.assertTrue(saved["check"]["woke_owner"])
        self.assertIn("без допустимого конверта", saved["check"]["outcome"])
        self.assertEqual(saved["snapshot"], {"ready": []})
        self.assertEqual(journal["kind"], "composer_failure")
        self.assertEqual(journal["raw_response"], "Готовый текст без конверта")

    def test_idle_fallback_uses_gmail_delivery_with_the_observed_reason(self):
        report = {"ready_to_start": [{"id": 1280}], "decided_not_done": [],
                  "can_pick_up": []}
        with mock.patch.object(tick, "deliver", return_value={"delivered": True}) as send:
            receipt = tick.deliver_idle(
                "process", "Контур", report,
                [{"text": "не найден живой прогон"}], AT)
        self.assertTrue(receipt["delivered"])
        args = send.call_args.args
        self.assertEqual(args[:2], ("process", "idle"))
        self.assertIn("не найден живой прогон", args[3])

    def test_prompt_selects_or_stays_silent_before_writing(self):
        text = tick.prompt({"title": "Контур", "live_runs": [],
                            "ready_to_start": [], "decided_not_done": [],
                            "can_pick_up": []}, ["событие"], [], [])
        self.assertIn("сначала выбери канал", text)
        self.assertIn('"channel":"gmail"', text)
        self.assertIn("CLI-диалоге не", text)
        self.assertNotIn("ПОВОД: вопрос", text)
        self.assertNotIn("ВОПРОС: да|нет", text)

    def test_the_same_contract_asks_every_letter_to_stand_on_its_own(self):
        text = tick.verdict_block()
        self.assertNotIn("обычный отчёт остаётся коротким", text)
        self.assertIn("любое письмо самодостаточно", text)
        self.assertIn("Над чем работаем", text)
        self.assertIn("номер задачи", text)
        self.assertIn("верни ровно `SILENT`", text)

    def test_a_letter_says_in_plain_words_what_it_wants_from_the_reader(self):
        text = tick.verdict_block()
        self.assertIn("что нужно от пользователя", text)
        self.assertIn("«От вас ничего не требуется»", text)
        self.assertIn("Поле `kind` технического конверта", text)

    def test_letter_contract_carries_price_review_and_night_rules(self):
        text = tick.verdict_block()
        self.assertIn("Продукт не усложнился, денег не потратили", text)
        self.assertIn("Круги правок и замечаний в письмо не пересказывай", text)
        self.assertIn("С 23:00 до 07:00 по Москве", text)
        self.assertIn(
            "Ответ на письмо\n  пользователя уходит сразу, как готов, в любой час",
            text)

    def test_late_russian_text_rules_are_gone(self):
        source = Path(outbound.__file__).read_text(encoding="utf-8")
        for old_owner in ("def asks_user", "def already_heard", "def same_question",
                          "def same_matter", "def warrant", "def decide",
                          "MACHINE_PREAMBLES", "CHOICE_REQUEST", "REASON_LINE"):
            with self.subTest(old_owner=old_owner):
                self.assertNotIn(old_owner, source)


class DirectDelivery(unittest.TestCase):
    def send(self, ledger: Path, *, event_id: str = "report:task-1280:accepted",
             body: str = "Готово", result: str | bool | None = "gmail-1",
             selected_by: str = "composer", thread: str = "process"):
        with mock.patch.object(outbound, "LEDGER", ledger), \
                mock.patch.object(tick, "send_mail", return_value=result) as mailed:
            record = tick.deliver(thread, "report", "Продакт: результат", body,
                                  AT, event_id=event_id, selected_by=selected_by)
        return record, mailed

    def test_composer_selected_message_goes_directly_to_gmail(self):
        with tempfile.TemporaryDirectory() as home:
            record, mailed = self.send(Path(home) / "outbound.json")
        mailed.assert_called_once_with("Продакт: результат", "Готово",
                                       reply_to_message_id=None, attachments=None)
        self.assertEqual(record["action"], "send")
        self.assertEqual(record["channel"], "gmail")
        self.assertTrue(record["composer_selected"])
        self.assertEqual(record["decision_owner"], "composer")

    def test_mechanical_door_does_not_claim_a_composer_selected_its_message(self):
        with tempfile.TemporaryDirectory() as home:
            record, _mailed = self.send(
                Path(home) / "outbound.json", selected_by="instruction_door")
        self.assertFalse(record["composer_selected"])
        self.assertEqual(record["decision_owner"], "instruction_door")

    def test_the_same_event_is_not_sent_twice(self):
        with tempfile.TemporaryDirectory() as home:
            ledger = Path(home) / "outbound.json"
            first, first_mail = self.send(ledger)
            second, second_mail = self.send(ledger, body="Та же новость другими словами")
        self.assertEqual(first["action"], "send")
        self.assertEqual(first_mail.call_count, 1)
        self.assertEqual(second["action"], "drop")
        self.assertEqual(second_mail.call_count, 0)

    def test_the_same_event_is_not_sent_by_a_second_direction(self):
        with tempfile.TemporaryDirectory() as home:
            ledger = Path(home) / "outbound.json"
            event_id = "report:task-1272:analysis-approved-round16"
            first, first_mail = self.send(
                ledger, event_id=event_id, thread="deep-research")
            second, second_mail = self.send(
                ledger, event_id=event_id, thread="process")
        self.assertEqual(first["action"], "send")
        self.assertEqual(first_mail.call_count, 1)
        self.assertEqual(second["action"], "drop")
        self.assertEqual(second["reason"], "это событие уже доставлено")
        self.assertEqual(second_mail.call_count, 0)

    def test_different_events_with_the_same_text_both_go(self):
        with tempfile.TemporaryDirectory() as home:
            ledger = Path(home) / "outbound.json"
            first, _ = self.send(ledger, event_id="report:task-1280:reviewed")
            second, second_mail = self.send(ledger, event_id="report:task-1280:installed")
        self.assertEqual(first["action"], "send")
        self.assertEqual(second["action"], "send")
        self.assertEqual(second_mail.call_count, 1)

    def test_event_identity_outlives_the_short_prompt_history(self):
        with tempfile.TemporaryDirectory() as home:
            ledger = Path(home) / "outbound.json"
            self.send(ledger, event_id="report:task-1280:first")
            for number in range(outbound.KEEP_LETTERS + 5):
                self.send(ledger, event_id=f"report:task-1280:later-{number}")
            repeated, repeated_mail = self.send(
                ledger, event_id="report:task-1280:first", body="Перефразировано")
        self.assertEqual(repeated["action"], "drop")
        self.assertEqual(repeated_mail.call_count, 0)

    def test_a_failed_send_does_not_consume_the_event(self):
        with tempfile.TemporaryDirectory() as home:
            ledger = Path(home) / "outbound.json"
            failed, _ = self.send(ledger, result=None)
            retried, retried_mail = self.send(ledger, result="gmail-2")
        self.assertEqual(failed["action"], "fail")
        self.assertEqual(retried["action"], "send")
        self.assertEqual(retried_mail.call_count, 1)

    def test_incoming_reply_uses_its_gmail_id_as_the_event(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.object(outbound, "LEDGER", Path(home) / "outbound.json"), \
                    mock.patch.object(tick, "send_mail", return_value="gmail-reply") as mailed:
                record = tick.deliver(
                    "process", "reply", "Re: вопрос", "Принял, делаю.", AT,
                    reply_to_message_id="gmail-incoming")
        mailed.assert_called_once_with("Re: вопрос", "Принял, делаю.",
                                       reply_to_message_id="gmail-incoming",
                                       attachments=None)
        self.assertEqual(record["event_id"], "reply:gmail-incoming")
        self.assertEqual(record["message_id"], "gmail-reply")

    def test_product_message_uses_only_the_selected_gmail_channel(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.object(outbound, "LEDGER", Path(home) / "outbound.json"), \
                mock.patch.object(tick, "send_mail", return_value="gmail-only") as mailed:
            record = tick.deliver(
                "process", "report", "Продакт: результат", "Готово", AT,
                event_id="report:task-1280:gmail-only")
        mailed.assert_called_once()
        self.assertEqual(record["channel"], "gmail")
        self.assertNotIn("telegram", record)

    def test_idle_letter_keeps_the_existing_six_hour_bound(self):
        entry = {"letters": [{"at": AT.isoformat(), "kind": "idle"}]}
        self.assertFalse(outbound.kind_due(
            entry, "idle", AT + timedelta(hours=5, minutes=59),
            outbound.IDLE_LETTER_SECONDS))
        self.assertTrue(outbound.kind_due(
            entry, "idle", AT + timedelta(hours=6),
            outbound.IDLE_LETTER_SECONDS))

    def test_journal_records_event_identity_and_channel(self):
        with tempfile.TemporaryDirectory() as home:
            ledger = Path(home) / "outbound.json"
            self.send(ledger)
            row = json.loads(Path(home, "outbound-journal.jsonl").read_text())
        self.assertEqual(row["event_id"], "report:task-1280:accepted")
        self.assertEqual(row["channel"], "gmail")


class NightDigest(unittest.TestCase):
    NIGHT = datetime(2026, 8, 27, 22, 0, tzinfo=timezone.utc)  # 01:00 Moscow
    MORNING = datetime(2026, 8, 28, 4, 1, tzinfo=timezone.utc)  # 07:01 Moscow

    def test_moscow_window_has_exact_boundaries(self):
        self.assertIsNone(outbound.night_window(
            datetime(2026, 8, 27, 19, 59, tzinfo=timezone.utc)))
        self.assertEqual(outbound.night_window(
            datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)), "2026-08-28")
        self.assertEqual(outbound.night_window(
            datetime(2026, 8, 28, 3, 59, tzinfo=timezone.utc)), "2026-08-28")
        self.assertIsNone(outbound.night_window(
            datetime(2026, 8, 28, 4, 0, tzinfo=timezone.utc)))

    def test_work_status_waits_but_reply_from_user_mail_goes_at_once(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.object(outbound, "LEDGER", Path(home) / "outbound.json"), \
                mock.patch.object(tick, "send_mail", return_value="gmail-reply") as send:
            held = tick.deliver(
                "process", "report", "Статус", "Работа закончена", self.NIGHT,
                event_id="report:task-1318:done", selected_by="composer")
            reply = tick.deliver(
                "process", "reply", "Re: письмо", "Принял, отвечаю", self.NIGHT,
                reply_to_message_id="gmail-incoming")
        self.assertEqual(held["action"], "defer")
        self.assertEqual(reply["action"], "send")
        send.assert_called_once()

    def test_task_letters_and_idle_go_at_once_during_the_night(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.object(outbound, "LEDGER", Path(home) / "outbound.json"), \
                mock.patch.object(tick, "send_mail", return_value="gmail-now") as send:
            receipts = [
                tick.deliver(
                    "process", kind, kind, "Готово", self.NIGHT,
                    event_id=f"{kind}:1:accepted")
                for kind in ("task_statement", "task_completion", "idle")
            ]
            with outbound.Ledger() as ledger:
                pending = outbound.pending_night_batch(ledger.thread("process"))
        self.assertEqual([item["action"] for item in receipts], ["send"] * 3)
        self.assertEqual(send.call_count, 3)
        self.assertIsNone(pending)

    def test_each_direction_keeps_its_own_window(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.object(outbound, "LEDGER", Path(home) / "outbound.json"), \
                mock.patch.object(tick, "send_mail") as send:
            tick.deliver("process", "report", "P", "one", self.NIGHT,
                         event_id="report:task-1318:process")
            tick.deliver("product", "report", "Q", "two", self.NIGHT,
                         event_id="report:task-1318:product")
            with outbound.Ledger() as ledger:
                process = outbound.pending_night_batch(ledger.thread("process"))
                product = outbound.pending_night_batch(ledger.thread("product"))
        send.assert_not_called()
        self.assertEqual([item["event_id"] for item in process["events"]],
                         ["report:task-1318:process"])
        self.assertEqual([item["event_id"] for item in product["events"]],
                         ["report:task-1318:product"])

    def test_one_digest_receipts_every_status_and_cannot_repeat(self):
        with tempfile.TemporaryDirectory() as home:
            ledger_path = Path(home) / "outbound.json"
            with mock.patch.object(outbound, "LEDGER", ledger_path), \
                    mock.patch.object(tick, "send_mail", return_value="gmail-night") as send:
                tick.deliver(
                    "process", "report", "Task 1", "Первое состояние",
                    self.NIGHT, attachments=["/tmp/task-1.html"],
                    event_id="report:task-1:accepted",
                    selected_by="composer")
                tick.deliver(
                    "process", "report", "Task 2", "Второе состояние",
                    self.NIGHT, event_id="report:task-2:accepted",
                    selected_by="composer")
                with outbound.Ledger() as ledger:
                    batch = outbound.pending_night_batch(ledger.thread("process"))
                digest_id = outbound.night_digest_event_id("process", batch["window"])
                sent = tick.deliver(
                    "process", "report", "Ночной итог",
                    "Задача 1 — принята. Задача 2 — принята.",
                    self.MORNING, attachments=["/tmp/task-1.html"],
                    event_id=digest_id, selected_by="composer", night_batch=batch)
                repeated = tick.deliver(
                    "process", "report", "Ночной итог", "Повтор",
                    self.MORNING, event_id=digest_id, selected_by="composer",
                    night_batch=batch)
                with outbound.Ledger() as ledger:
                    entry = ledger.thread("process")
                    remaining = outbound.pending_night_batch(entry)
                    delivered = entry["delivered_events"]
            self.assertEqual(sent["action"], "send")
            self.assertEqual(repeated["action"], "drop")
            self.assertEqual(send.call_count, 1)
            self.assertIsNone(remaining)
            self.assertEqual(delivered["report:task-1:accepted"]["message_id"],
                             "gmail-night")
            self.assertEqual(delivered["report:task-2:accepted"]["message_id"],
                             "gmail-night")
            rows = [json.loads(line) for line in
                    (Path(home) / "outbound-journal.jsonl").read_text().splitlines()]
            receipts = [row for row in rows if row.get("delivery_mode") == "night_digest"]
            self.assertEqual({row["event_id"] for row in receipts},
                             {"report:task-1:accepted", "report:task-2:accepted"})

    def test_digest_receipts_only_named_work_and_retains_the_rest(self):
        with tempfile.TemporaryDirectory() as home:
            ledger_path = Path(home) / "outbound.json"
            with mock.patch.object(outbound, "LEDGER", ledger_path), \
                    mock.patch.object(tick, "send_mail", return_value="gmail-partial"):
                for number in (1, 2):
                    tick.deliver(
                        "process", "report", f"Task {number}", "accepted",
                        self.NIGHT, event_id=f"report:task-{number}:accepted")
                with outbound.Ledger() as ledger:
                    batch = outbound.pending_night_batch(ledger.thread("process"))
                tick.deliver(
                    "process", "report", "Ночной итог", "Задача 1 — принята.",
                    self.MORNING,
                    event_id=outbound.night_digest_event_id("process", batch["window"]),
                    selected_by="composer", night_batch=batch)
                with outbound.Ledger() as ledger:
                    entry = ledger.thread("process")
                    pending = outbound.pending_night_batch(entry)
            self.assertIn("report:task-1:accepted", entry["delivered_events"])
            self.assertNotIn("report:task-2:accepted", entry["delivered_events"])
            self.assertEqual([item["event_id"] for item in pending["events"]],
                             ["report:task-2:accepted"])

    def test_dropped_digest_discards_events_already_delivered_elsewhere(self):
        with tempfile.TemporaryDirectory() as home:
            ledger_path = Path(home) / "outbound.json"
            with mock.patch.object(outbound, "LEDGER", ledger_path), \
                    mock.patch.object(tick, "send_mail", return_value="gmail-direct") as send:
                tick.deliver(
                    "process", "report", "Task 1", "done", self.NIGHT,
                    event_id="report:task-1:done")
                with outbound.Ledger() as ledger:
                    entry = ledger.thread("process")
                    batch = outbound.pending_night_batch(entry)
                    outbound.remember_delivery(
                        entry, event_id="report:task-1:done", subject="Task 1",
                        body="done", kind="report", now=self.MORNING,
                        message_id="gmail-direct")
                result = tick.deliver(
                    "process", "report", "Ночной итог", "Задача 1 — готова.",
                    self.MORNING,
                    event_id=outbound.night_digest_event_id("process", batch["window"]),
                    selected_by="composer", night_batch=batch)
                with outbound.Ledger() as ledger:
                    pending = outbound.pending_night_batch(ledger.thread("process"))
        self.assertEqual(result["action"], "drop")
        send.assert_not_called()
        self.assertIsNone(pending)

    def test_finishing_snapshot_does_not_remove_a_concurrent_event(self):
        with tempfile.TemporaryDirectory() as home:
            ledger_path = Path(home) / "outbound.json"
            with mock.patch.object(outbound, "LEDGER", ledger_path), \
                    mock.patch.object(tick, "send_mail", return_value="gmail-night"):
                tick.deliver("process", "report", "Task 1", "done", self.NIGHT,
                             event_id="report:task-1:done")
                with outbound.Ledger() as ledger:
                    batch = outbound.pending_night_batch(ledger.thread("process"))
                tick.deliver("process", "report", "Task 2", "done", self.NIGHT,
                             event_id="report:task-2:done")
                tick.deliver(
                    "process", "report", "Ночной итог", "Задача 1 — готова.",
                    self.MORNING,
                    event_id=outbound.night_digest_event_id("process", batch["window"]),
                    selected_by="composer", night_batch=batch)
                with outbound.Ledger() as ledger:
                    pending = outbound.pending_night_batch(ledger.thread("process"))
            self.assertEqual([item["event_id"] for item in pending["events"]],
                             ["report:task-2:done"])

    def test_second_composer_failure_is_recorded_in_the_same_window(self):
        entry = {"night_batches": {}}
        outbound.remember_night_event(
            entry, window="2026-08-28", event_id="report:task-1:done",
            subject="Task 1", body="done", kind="report", attachments=[],
            now=self.NIGHT, selected_by="composer")
        batch = outbound.pending_night_batch(entry)
        self.assertEqual(outbound.note_night_composer_failure(entry, batch), 1)
        self.assertEqual(outbound.note_night_composer_failure(entry, batch), 2)
        self.assertEqual(outbound.pending_night_batch(entry)["composer_failures"], 2)

    def test_mechanical_fallback_names_each_work_and_current_state(self):
        batch = {"window": "2026-08-28", "events": [
            {"event_id": "task-completion:1:accepted", "subject": "Task 1",
             "body": "Принята", "kind": "task_completion"},
            {"event_id": "report:task-2:done", "subject": "Task 2",
             "body": "Готова", "kind": "report"},
        ]}
        report = {"needs_attention": [{"id": 1, "title": "Первая",
                                         "status": "blocked", "run": None}],
                  "ready_to_start": [{"id": 2, "title": "Вторая",
                                       "status": "planned"}]}
        _subject, body = tick.mechanical_night_digest(
            "Контур", batch, report, self.MORNING,
            task_rows=[{"id": 1, "title": "Первая", "status": "completed",
                        "status_detail": "accepted"}])
        self.assertIn("Задача 1: Первая", body)
        self.assertIn("Финальное состояние: completed", body)
        self.assertIn("Текущий шаг: accepted", body)
        self.assertIn("Задача 2: Вторая", body)
        self.assertIn("Финальное состояние: planned", body)

    def test_numberless_work_has_a_prompt_name_and_real_fallback_state(self):
        subject = "Продакт: MOEX — торговля остановлена"
        status = "Что произошло\n\nОстановлена после обнаружения собственного выхода."
        event_id = "report:moex:halt-after-own-exit-cause-found"
        batch = {"window": "2026-08-28", "events": [{
            "event_id": event_id, "subject": subject, "body": status,
            "kind": "report", "attachments": [], "selected_by": "composer",
        }]}
        prompt = tick.prompt(
            {"thread": "moex", "title": "MOEX", "live_runs": [],
             "ready_to_start": [], "decided_not_done": [], "can_pick_up": []},
            [], [], [], night_batch=batch)
        self.assertIn("для события без номера задачи дословно назови его `subject`", prompt)
        self.assertEqual(outbound.named_night_event_ids(batch, subject), {event_id})
        _subject, body = tick.mechanical_night_digest(
            "MOEX", batch, {}, self.MORNING)
        self.assertIn(f"Работа: {subject} [{event_id}]", body)
        self.assertNotIn("Финальное состояние:", body)
        self.assertIn(f"Последний отчёт за ночь: {status}", body)
        self.assertEqual(body.count(status), 1)
        self.assertNotIn("финальное состояние из ночной записи", body)

    def test_mechanical_digest_uses_latest_production_shaped_event_per_work(self):
        numberless_id = "report:moex:halt-after-own-exit-cause-found"
        events = [
            {"event_id": "report:task-1305:halt", "subject": "1305",
             "body": "Что произошло\n\nНашли остановку.", "kind": "report"},
            {"event_id": "report:task-1305:accepted", "subject": "1305",
             "body": "Что изменилось\n\nЗащиты установлены.", "kind": "report"},
            {"event_id": numberless_id, "subject": "MOEX: торговля остановлена",
             "body": "Что произошло\n\nReplay начат.", "kind": "report"},
            {"event_id": "report:moex:halt-after-own-exit-cause-fixed",
             "subject": "MOEX: торговля остановлена",
             "body": "Что произошло\n\nReplay остановлен.", "kind": "report"},
        ]
        batch = {"window": "2026-08-28", "events": events,
                 "composer_failures": 0}
        digest_batch, superseded = tick.mechanical_night_batch(batch)
        _subject, body = tick.mechanical_night_digest(
            "MOEX", digest_batch, {}, self.MORNING,
            task_rows=[{"id": 1305, "title": "Защиты", "status": "completed"}])
        self.assertEqual(
            [item["event_id"] for item in digest_batch["events"]],
            ["report:task-1305:accepted",
             "report:moex:halt-after-own-exit-cause-fixed"])
        self.assertEqual(superseded,
                         {"report:task-1305:halt", numberless_id})
        self.assertNotIn("Нашли остановку", body)
        self.assertNotIn("Replay начат", body)
        self.assertEqual(body.count("Защиты установлены"), 1)
        self.assertEqual(body.count("Replay остановлен"), 1)
        self.assertNotIn("Финальное состояние: Что произошло", body)

    def test_mechanical_delivery_drops_superseded_without_receipting_it(self):
        report = {
            "thread": "product", "title": "Продукт", "live_runs": [],
            "needs_attention": [], "ready_to_start": [], "decided_not_done": [],
            "can_pick_up": [], "queued_by_plan": [], "backlog": [],
            "waiting_user": [],
        }
        snapshot = {"live": [], "pickup": [], "ready": [], "decided": [],
                    "plan_queue": []}
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            ledger_path = root / "outbound.json"
            state_dir = root / "threads"
            with mock.patch.object(outbound, "LEDGER", ledger_path), \
                    mock.patch.object(tick, "send_mail", return_value="gmail-night"):
                for event_id, body in (
                        ("report:task-1318:first", "Первый отчёт"),
                        ("report:task-1318:last", "Защиты установлены"),
                        ("report:product:release", "Релиз направления")):
                    tick.deliver("product", "report", event_id, body, self.NIGHT,
                                 event_id=event_id)
            frozen = type("FrozenDatetime", (datetime,),
                          {"now": staticmethod(lambda tz=None: self.MORNING)})
            patches = (
                mock.patch.object(outbound, "LEDGER", ledger_path),
                mock.patch.object(tick, "STATE_DIR", state_dir),
                mock.patch.object(tick, "datetime", frozen),
                mock.patch.object(sys, "argv", ["thread_tick.py", "product"]),
                mock.patch.object(tick, "runner_contract_alarm", return_value=([], None)),
                mock.patch.object(tick, "build", return_value=report),
                mock.patch.object(tick, "snapshot", return_value=snapshot),
                mock.patch.object(tick, "persisted_process_inventory", return_value=[]),
                mock.patch.object(tick, "process_observation", return_value={}),
                mock.patch.object(tick, "goal_watch", return_value={
                    "transitions": [], "standing": [], "reminder": None,
                    "panel": [], "objects": []}),
                mock.patch.object(tick, "standing_events", return_value=([], None, None)),
                mock.patch.object(tick, "startable", return_value=0),
                mock.patch.object(tick, "idle_reasons", return_value=[]),
                mock.patch.object(tick, "yielded", return_value=None),
                mock.patch.object(tick, "queue", return_value={}),
                mock.patch.object(tick, "outcome", return_value="проверено"),
                mock.patch.object(tick, "started_runs", return_value=[]),
                mock.patch.object(tick.goal_session, "watchdog", return_value={
                    "mode": "session", "holds": True, "detail": "живая сессия"}),
                mock.patch.object(outbound, "instruction_letter", return_value=None),
                mock.patch.object(tick, "query_tasks", return_value=[{
                    "id": 1318, "title": "Ночная почта", "status": "completed"}]),
                mock.patch.object(tick, "send_mail", return_value="gmail-night"),
            )
            with contextlib.ExitStack() as stack:
                entered = [stack.enter_context(patch) for patch in patches]
                rc = tick.main()
                sent_body = entered[-1].call_args.args[1]
            with mock.patch.object(outbound, "LEDGER", ledger_path), \
                    outbound.Ledger() as ledger:
                entry = ledger.thread("product")
                pending = outbound.pending_night_batch(entry)
            rows = [json.loads(line) for line in
                    (root / "outbound-journal.jsonl").read_text().splitlines()]
        self.assertEqual(rc, 0)
        self.assertIsNone(pending)
        self.assertNotIn("Первый отчёт", sent_body)
        self.assertEqual(sent_body.count("Защиты установлены"), 1)
        self.assertEqual(sent_body.count("Релиз направления"), 1)
        self.assertNotIn("report:task-1318:first", entry["delivered_events"])
        self.assertIn("report:task-1318:last", entry["delivered_events"])
        self.assertIn("report:product:release", entry["delivered_events"])
        superseded = [row for row in rows
                      if row.get("event_id") == "report:task-1318:first"]
        self.assertEqual(superseded[-1]["action"], "drop")
        self.assertIsNone(superseded[-1]["delivered"])

    def test_digest_attaches_files_only_for_work_named_in_its_body(self):
        batch = {"window": "2026-08-28", "events": [
            {"event_id": "report:task-1:done", "attachments": ["/tmp/one.html"]},
            {"event_id": "report:task-2:done", "attachments": ["/tmp/two.html"]},
        ]}
        files = tick.night_digest_attachments(
            {"body": "Задача 1 — готова.",
             "attachments": ["/tmp/cover.html", "/tmp/two.html"]},
            batch)
        self.assertEqual(files, ["/tmp/cover.html", "/tmp/one.html"])

    def test_live_goal_session_flushes_pending_window_mechanically(self):
        report = {
            "thread": "product", "title": "Продукт", "live_runs": [],
            "needs_attention": [], "ready_to_start": [], "decided_not_done": [],
            "can_pick_up": [], "queued_by_plan": [], "backlog": [],
            "waiting_user": [],
        }
        snapshot = {"live": [], "pickup": [], "ready": [], "decided": [],
                    "plan_queue": []}
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            ledger_path = root / "outbound.json"
            state_dir = root / "threads"
            with mock.patch.object(outbound, "LEDGER", ledger_path), \
                    mock.patch.object(tick, "send_mail", return_value="gmail-night"):
                tick.deliver(
                    "product", "report", "Задача 1318 — правки", "готова к ревью",
                    self.NIGHT, event_id="report:task-1318:rework")

            frozen = type(
                "FrozenDatetime", (datetime,),
                {"now": staticmethod(lambda tz=None: self.MORNING)})
            patches = (
                mock.patch.object(outbound, "LEDGER", ledger_path),
                mock.patch.object(tick, "STATE_DIR", state_dir),
                mock.patch.object(tick, "datetime", frozen),
                mock.patch.object(sys, "argv", ["thread_tick.py", "product"]),
                mock.patch.object(tick, "runner_contract_alarm",
                                  return_value=([], None)),
                mock.patch.object(tick, "build", return_value=report),
                mock.patch.object(tick, "snapshot", return_value=snapshot),
                mock.patch.object(tick, "persisted_process_inventory", return_value=[]),
                mock.patch.object(tick, "process_observation", return_value={}),
                mock.patch.object(tick, "goal_watch", return_value={
                    "transitions": [], "standing": [], "reminder": None,
                    "panel": [], "objects": []}),
                mock.patch.object(tick, "standing_events", return_value=([], None, None)),
                mock.patch.object(tick, "startable", return_value=0),
                mock.patch.object(tick, "idle_reasons", return_value=[]),
                mock.patch.object(tick, "yielded", return_value=None),
                mock.patch.object(tick, "queue", return_value={}),
                mock.patch.object(tick, "outcome", return_value="проверено"),
                mock.patch.object(tick, "started_runs", return_value=[]),
                mock.patch.object(tick.goal_session, "watchdog", return_value={
                    "mode": "session", "holds": True, "detail": "живая сессия"}),
                mock.patch.object(outbound, "instruction_letter", return_value=None),
                mock.patch.object(tick, "query_tasks", return_value=[{
                    "id": 1318, "title": "Ночная почта", "status": "in_review",
                    "status_detail": "кандидат готов"}]),
                mock.patch.object(tick, "send_mail", return_value="gmail-night"),
            )
            with contextlib.ExitStack() as stack:
                entered = [stack.enter_context(patch) for patch in patches]
                rc = tick.main()
                send = entered[-1]
            with mock.patch.object(outbound, "LEDGER", ledger_path), \
                    outbound.Ledger() as ledger:
                entry = ledger.thread("product")
                pending = outbound.pending_night_batch(entry)
        self.assertEqual(rc, 0)
        send.assert_called_once()
        self.assertIsNone(pending)
        self.assertIn("report:task-1318:rework", entry["delivered_events"])

    def test_failed_digest_keeps_the_window_for_retry(self):
        with tempfile.TemporaryDirectory() as home:
            ledger_path = Path(home) / "outbound.json"
            with mock.patch.object(outbound, "LEDGER", ledger_path), \
                    mock.patch.object(tick, "send_mail", return_value=None):
                tick.deliver(
                    "process", "report", "Task", "Состояние", self.NIGHT,
                    event_id="report:task-1:done", selected_by="composer")
                with outbound.Ledger() as ledger:
                    batch = outbound.pending_night_batch(ledger.thread("process"))
                result = tick.deliver(
                    "process", "report", "Ночной итог", "Финальное состояние",
                    self.MORNING,
                    event_id=outbound.night_digest_event_id("process", batch["window"]),
                    selected_by="composer", night_batch=batch)
                with outbound.Ledger() as ledger:
                    pending = outbound.pending_night_batch(ledger.thread("process"))
        self.assertEqual(result["action"], "fail")
        self.assertEqual(pending["events"][0]["event_id"], "report:task-1:done")

    def test_morning_prompt_demands_one_final_state_digest(self):
        batch = {"window": "2026-08-28", "events": [{
            "event_id": "report:task-1:done", "subject": "Task 1",
            "body": "Промежуточный статус", "kind": "report",
            "attachments": [], "selected_by": "composer",
        }]}
        text = tick.prompt(
            {"thread": "process", "title": "Контур", "live_runs": [],
             "ready_to_start": [], "decided_not_done": [], "can_pick_up": []},
            [], [], [], night_batch=batch)
        self.assertIn("event_id=night:process:2026-08-28", text)
        self.assertIn("финальное\nсостояние", text)
        self.assertIn("открой её карточку", text)
        self.assertIn("Не склеивай события разных направлений", text)


class LedgerRecovery(unittest.TestCase):
    def remember(self, path: Path) -> None:
        with outbound.Ledger(path) as ledger:
            outbound.remember_delivery(
                ledger.thread("process"), event_id="report:task-1280:accepted",
                subject="Готово", body="Готово", kind="report", now=AT,
                message_id="gmail-1")

    def test_a_corrupt_ledger_is_rebuilt_rather_than_fatal(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "outbound.json"
            path.write_text("{не json", encoding="utf-8")
            with outbound.Ledger(path) as ledger:
                self.assertEqual(ledger.thread("process")["letters"], [])
            self.assertEqual(json.loads(path.read_text())["version"], 1)

    def test_a_deleted_ledger_comes_back_from_the_copy(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "outbound.json"
            self.remember(path)
            path.unlink()
            with outbound.Ledger(path) as ledger:
                self.assertTrue(outbound.event_delivered(
                    ledger.thread("process"), "report:task-1280:accepted"))

    def test_a_corrupt_ledger_prefers_the_copy_over_starting_empty(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "outbound.json"
            self.remember(path)
            path.write_text("{не json", encoding="utf-8")
            with outbound.Ledger(path) as ledger:
                self.assertTrue(outbound.event_delivered(
                    ledger.thread("process"), "report:task-1280:accepted"))


class ExternalInstruction(unittest.TestCase):
    def a_deliverable(self, home: str, number: int, name: str, text: str,
                      *, registered: bool = True) -> Path:
        box = Path(home) / "tasks" / f"{number}-external" / "deliverables"
        box.mkdir(parents=True, exist_ok=True)
        path = box / name
        path.write_text(text, encoding="utf-8")
        if registered:
            (box / "manifest.json").write_text(
                json.dumps({"deliverables": [name]}), encoding="utf-8")
        self.readiness(home, number, "approved", "completed")
        return path

    def readiness(self, home: str, number: int, decision: str, state: str,
                  recorded_at: datetime | None = None) -> None:
        task = Path(home) / "tasks" / f"{number}-external"
        (task / "status.json").write_text(
            json.dumps({"state": state}), encoding="utf-8")
        reviews = task / "reviews"
        reviews.mkdir(exist_ok=True)
        recorded_at = recorded_at or datetime.now(timezone.utc)
        (reviews / "rounds.jsonl").write_text(
            json.dumps({"round": 1, "decision": decision,
                        "recorded_at": recorded_at.isoformat()}) + "\n",
            encoding="utf-8")

    def direction(self, home: str, *tasks: tuple[int, str]):
        rows = [{"id": number, "path": f"tasks/{number}-external", "status": status,
                 "title": f"Цель внешней задачи {number}"}
                for number, status in tasks]
        return (mock.patch.object(pms, "REPO", Path(home)),
                mock.patch.object(thread_state, "load_thread", return_value={}),
                mock.patch.object(pms, "thread_tasks", return_value=rows))

    def letter(self, home: str, tasks: tuple[tuple[int, str], ...], entry=None):
        repo, direction, listing = self.direction(home, *tasks)
        with repo, direction, listing:
            return outbound.instruction_letter("deep-research", entry)

    def test_approved_registered_file_without_a_return_is_ready(self):
        with tempfile.TemporaryDirectory() as home:
            path = self.a_deliverable(home, 1272, "a100-run-instruction.md", "run\n")
            letter = self.letter(home, ((1272, "blocked"),))
        digest = hashlib.sha256(b"run\n").hexdigest()
        self.assertIn(str(path), letter["body"])
        self.assertIn(digest, letter["body"])
        self.assertIn("Цель внешней задачи 1272", letter["body"])
        self.assertIn("результат внешнего выполнения ещё не возвращён", letter["body"])
        self.assertIn("Точное действие", letter["body"])
        self.assertEqual(letter["event_id"], f"instruction:deep-research:1272:{digest}")

    def test_rework_rounds_emit_nothing_until_the_latest_round_is_approved(self):
        with tempfile.TemporaryDirectory() as home:
            self.a_deliverable(home, 1286, "instruction.md", "v5\n")
            self.readiness(home, 1286, "rework", "blocked")
            self.assertIsNone(self.letter(home, ((1286, "blocked"),)))
            self.readiness(home, 1286, "approved", "completed")
            letter = self.letter(home, ((1286, "completed"),))
        self.assertIsNotNone(letter)

    def test_approved_round_emits_nothing_while_its_child_is_running(self):
        with tempfile.TemporaryDirectory() as home:
            self.a_deliverable(home, 1286, "instruction.md", "ready soon\n")
            self.readiness(home, 1286, "approved", "running")
            self.assertIsNone(self.letter(home, ((1286, "completed"),)))

    def test_file_rewritten_after_approval_waits_for_the_next_approval(self):
        with tempfile.TemporaryDirectory() as home:
            path = self.a_deliverable(home, 1286, "instruction.md", "approved v1\n")
            approved_at = datetime.now(timezone.utc)
            self.readiness(home, 1286, "approved", "completed", approved_at)
            os.utime(path, (approved_at.timestamp() - 1,) * 2)
            first = self.letter(home, ((1286, "completed"),))
            path.write_text("unreviewed v2\n", encoding="utf-8")
            os.utime(path, (approved_at.timestamp() + 1,) * 2)
            self.assertIsNone(self.letter(home, ((1286, "blocked"),)))
            self.readiness(home, 1286, "approved", "completed",
                           approved_at + timedelta(seconds=2))
            second = self.letter(home, ((1286, "completed"),))
        self.assertNotEqual(first["event_id"], second["event_id"])

    def test_rewrite_during_hashing_is_not_sent_under_the_old_approval(self):
        with tempfile.TemporaryDirectory() as home:
            path = self.a_deliverable(home, 1286, "instruction.md", "approved\n")
            approved_at = datetime.now(timezone.utc)
            self.readiness(home, 1286, "approved", "completed", approved_at)
            os.utime(path, (approved_at.timestamp() - 1,) * 2)

            def rewrite_while_hashing(candidate: Path) -> str:
                digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
                candidate.write_text("rewritten during hash\n", encoding="utf-8")
                os.utime(candidate, (approved_at.timestamp() + 1,) * 2)
                return digest

            repo, direction, listing = self.direction(home, (1286, "completed"))
            with repo, direction, listing, \
                    mock.patch.object(pms, "_file_sha256", side_effect=rewrite_while_hashing):
                letter = outbound.instruction_letter("deep-research")
        self.assertIsNone(letter)

    def test_task_1272_returned_result_suppresses_its_registered_instruction(self):
        with tempfile.TemporaryDirectory() as home:
            self.a_deliverable(home, 1272, "a100-run-instruction.md", "run\n")
            returned = Path(home, "tasks", "1272-external", "from-external-agent")
            returned.mkdir()
            (returned / "aggregation-pro.json").write_text("{}\n", encoding="utf-8")
            self.assertIsNone(self.letter(home, ((1272, "blocked"),)))

    def test_unregistered_file_is_not_ready(self):
        with tempfile.TemporaryDirectory() as home:
            self.a_deliverable(home, 1272, "a100-run-instruction.md", "draft\n",
                               registered=False)
            self.assertIsNone(self.letter(home, ((1272, "completed"),)))

    def test_newest_ready_registered_instruction_wins_regardless_of_catalogue_status(self):
        with tempfile.TemporaryDirectory() as home:
            new = self.a_deliverable(home, 1272, "instruction.md", "new\n")
            old = self.a_deliverable(home, 1233, "instruction.md", "old\n")
            letter = self.letter(home, ((1272, "blocked"), (1233, "completed")))
        self.assertIn(str(new), letter["body"])
        self.assertNotIn(str(old), letter["body"])

    def test_already_named_bytes_do_not_get_a_message_built(self):
        with tempfile.TemporaryDirectory() as home:
            path = self.a_deliverable(home, 1272, "instruction.md", "same\n")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entry = {"letters": [], "instructions": [{"sha256": digest}]}
            self.assertIsNone(self.letter(home, ((1272, "blocked"),), entry))

    def test_changed_approved_bytes_are_a_new_event(self):
        with tempfile.TemporaryDirectory() as home:
            path = self.a_deliverable(home, 1272, "instruction.md", "v1\n")
            first = self.letter(home, ((1272, "blocked"),))
            path.write_text("v2\n", encoding="utf-8")
            self.readiness(home, 1272, "approved", "completed")
            second = self.letter(home, ((1272, "blocked"),))
        self.assertNotEqual(first["event_id"], second["event_id"])

    def test_unreadable_digest_builds_no_partial_letter(self):
        noise = io.StringIO()
        with tempfile.TemporaryDirectory() as home:
            self.a_deliverable(home, 1272, "instruction.md", "run\n")
            repo, direction, listing = self.direction(home, (1272, "blocked"))
            with repo, direction, listing, \
                    mock.patch.object(pms, "_file_sha256", return_value=None), \
                    contextlib.redirect_stderr(noise):
                letter = outbound.instruction_letter("deep-research")
        self.assertIsNone(letter)
        self.assertIn("не читается", noise.getvalue())


class Sender(unittest.TestCase):
    def test_reply_mode_reaches_the_existing_gmail_client(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="Email sent. Message ID: gmail-sent\n", stderr="")
        installed = Path(__file__)
        with mock.patch.object(tick, "MAIL_TO", "user@example.com"), \
                mock.patch.object(tick, "MAIL_SCRIPT", installed), \
                mock.patch.object(tick, "MAIL_PYTHON", installed), \
                mock.patch.object(tick.subprocess, "run", return_value=completed) as run:
            receipt = tick.send_mail("Re: ignored", "Ответ",
                                     reply_to_message_id="gmail-incoming")
        command = run.call_args.args[0]
        self.assertEqual(receipt, "gmail-sent")
        self.assertIn("--reply-to-message-id", command)
        self.assertNotIn("--subject", command)


if __name__ == "__main__":
    unittest.main()
