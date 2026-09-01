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

    def test_a_letter_separates_the_current_user_change_from_future_work(self):
        text = " ".join(tick.verdict_block().split())
        self.assertIn("что уже изменилось для пользователя сейчас", text)
        self.assertIn("в работающем продукте пока ничего не изменилось", text)
        self.assertIn("будущую установку или проверку назови отдельно", text)

    def test_a_letter_says_in_plain_words_what_it_wants_from_the_reader(self):
        text = tick.verdict_block()
        self.assertIn("что нужно от пользователя", text)
        self.assertIn("«От вас ничего не требуется»", text)
        self.assertIn("Поле `kind` технического конверта", text)

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


class HeldDocumentDoor(unittest.TestCase):
    def report(self, home: str, *, registered: bool = True) -> tuple[dict, Path, str]:
        task = Path(home) / "tasks" / "1316-ready-document"
        box = task / "deliverables"
        box.mkdir(parents=True)
        document = box / "fanera.html"
        document.write_text("<html>готовый ответ</html>\n", encoding="utf-8")
        names = [document.name] if registered else []
        (box / "manifest.json").write_text(
            json.dumps({"deliverables": names}), encoding="utf-8")
        digest = hashlib.sha256(document.read_bytes()).hexdigest()
        return ({
            "title": "Процессный контур",
            "undelivered": [{
                "id": 1316, "title": "Ответ про фанеру",
                "path": "tasks/1316-ready-document",
                "age_seconds": tick.UNDELIVERED_SECONDS + 1,
                "document": "deliverables/fanera.html",
                "src": "квитанции нет",
            }],
        }, document, digest)

    def test_silent_composer_cannot_suppress_the_mechanical_document_letter(self):
        with tempfile.TemporaryDirectory() as home:
            report, document, digest = self.report(home)
            ledger = Path(home) / "outbound.json"
            with mock.patch.object(tick, "REPO", Path(home)), \
                    mock.patch.object(outbound, "LEDGER", ledger), \
                    mock.patch.object(tick, "send_mail", return_value="gmail-document") as send:
                receipts = tick.deliver_undelivered(
                    "process", report["title"], report, AT)
                composed_message = tick.parse_composed_message("SILENT")
        self.assertIsNone(composed_message)
        self.assertEqual(receipts[0]["action"], "send")
        self.assertEqual(receipts[0]["event_id"], f"document:process:1316:{digest}")
        self.assertIn(document.name, send.call_args.args[1])
        self.assertIn(digest, send.call_args.args[1])
        self.assertEqual(send.call_args.kwargs["attachments"], [str(document.resolve())])

    def test_the_same_document_revision_is_not_mailed_twice(self):
        with tempfile.TemporaryDirectory() as home:
            report, _document, _digest = self.report(home)
            ledger = Path(home) / "outbound.json"
            with mock.patch.object(tick, "REPO", Path(home)), \
                    mock.patch.object(outbound, "LEDGER", ledger), \
                    mock.patch.object(tick, "send_mail", return_value="gmail-document") as send:
                first = tick.deliver_undelivered("process", report["title"], report, AT)
                second = tick.deliver_undelivered(
                    "process", report["title"], report, AT + timedelta(minutes=20))
        self.assertEqual(first[0]["action"], "send")
        self.assertEqual(second[0]["action"], "drop")
        self.assertEqual(send.call_count, 1)

    def test_an_unregistered_draft_does_not_leave(self):
        with tempfile.TemporaryDirectory() as home:
            report, _document, _digest = self.report(home, registered=False)
            with mock.patch.object(tick, "REPO", Path(home)):
                self.assertEqual(tick.registered_undelivered(report), [])

    def test_the_document_door_runs_before_the_composer(self):
        source = Path(tick.__file__).read_text(encoding="utf-8")
        main = source[source.index("def main("):]
        self.assertLess(
            main.index("mail.extend(deliver_undelivered"),
            main.index("[str(CLAUDE_PRODUCT_OWNER), \"--entry\", \"print\"]"))


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
