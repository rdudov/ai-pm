#!/usr/bin/env python3
"""Regressions for the gate on mail to the user.

Every test here is one of the four rules the complaint of 2026-08-07 produced,
or the measurement behind it. The measurement: 58 letters in 21 hours, 21 of
them with the literally identical subject «Продакт: MOEX Strategy Lab». The
rules: a threshold, knowledge of what was said in chat, coalescing by content,
and a question that none of the three may swallow.

    python3 -m unittest discover -s scripts -p 'test_*.py'
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import outbound
import product_memory
import thread_tick as tick

AT = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)

# One writer of the ledger, in a process of its own, using production `Ledger`
# with nothing patched. It holds the critical section for a named time so the
# next writer is provably inside the lock's queue rather than merely near it.
WRITER = f'''
import sys, time
sys.path.insert(0, {str(Path(outbound.__file__).parent)!r})
import outbound
path, thread, hold = sys.argv[1], sys.argv[2], float(sys.argv[3])
with outbound.Ledger(path) as ledger:
    entry = ledger.thread(thread)
    time.sleep(hold)
    entry["pending"].append({{"at": "2026-08-09T12:00:00+00:00", "subject": thread,
                             "kind": "verdict", "body": "held for " + thread,
                             "reason": "regression"}})
'''


def a_report(**over) -> dict:
    """A direction as `thread_state.build` hands it over, cut to what is read."""
    report = {"title": "Процессный контур", "products": [], "waiting_user": [],
              "undelivered": [], "live_runs": [], "can_pick_up": [],
              "ready_to_start": [], "decided_not_done": []}
    report.update(over)
    return report


def a_snapshot(home: str, paths: str, work: str = "- 1") -> Path:
    """One product snapshot in the durable store, where the gateway now reads."""
    record = Path(home) / "products" / "task-agent" / product_memory.SNAPSHOT
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(f"# t\n## Пользовательские пути\n{paths}\n## В работе\n{work}\n",
                      encoding="utf-8")
    return record


def an_entry(**over) -> dict:
    entry = {"letters": [], "pending": [], "marks": None}
    entry.update(over)
    return entry


def marks(**over) -> dict:
    base = {"waiting_user": [], "undelivered": [], "value": {}}
    base.update(over)
    return base


def a_letter(body: str, at: datetime, subject: str = "Продакт: Процессный контур",
             kind: str = "verdict") -> dict:
    return {"at": at.isoformat(), "subject": subject, "kind": kind,
            "excerpt": body[:400], "reason": "тест",
            "fingerprint": outbound.fingerprint(subject, body)}


class QuestionIsNeverLost(unittest.TestCase):
    """«Склейка не имеет права проглотить вопрос пользователю.»

    The rule that outranks the other three, so it is tested against each of
    them: a question goes out when the threshold refuses, when the same words
    went out a minute ago, and when the conversation already covered them.
    """

    def test_a_question_goes_out_although_nothing_warrants_a_letter(self):
        entry = an_entry(marks=marks())
        decision = outbound.decide("process", "verdict", "Продакт: контур",
                                   "Запускать 861 сейчас или после 856?",
                                   a_report(), AT, entry, outbound.no_chat())
        self.assertEqual(decision["action"], "send")
        self.assertIn("вопрос", decision["reason"])

    def test_a_question_is_not_coalesced_into_the_letter_before_it(self):
        body = "Запускать 861 сейчас или после 856?"
        entry = an_entry(marks=marks(),
                         letters=[a_letter(body, AT - timedelta(minutes=5))])
        decision = outbound.decide("process", "verdict", "Продакт: контур", body,
                                   a_report(), AT, entry, outbound.no_chat())
        self.assertEqual(decision["action"], "send")

    def test_a_question_survives_having_been_discussed_in_chat(self):
        body = "Запускать 861 сейчас или после 856?"
        chat = {"sessions": ["s"], "tasks": [861, 856], "pairs": outbound.pairs(body),
                "chars": 100, "src": "тест"}
        decision = outbound.decide("process", "verdict", "Продакт: контур", body,
                                   a_report(), AT, an_entry(marks=marks()), chat)
        self.assertEqual(decision["action"], "send")

    def test_a_declared_mechanical_reason_does_not_silence_a_question_mark(self):
        # The owner's prose may not be the thing that loses a question: the
        # question mark is read directly and decided before the label.
        body = "ПОВОД: механика\nЗапускать 861 сейчас или после 856?"
        decision = outbound.decide("process", "verdict", "Продакт: контур", body,
                                   a_report(), AT, an_entry(marks=marks()),
                                   outbound.no_chat())
        self.assertEqual(decision["action"], "send")

    def test_held_matter_rides_out_with_the_question(self):
        entry = an_entry(marks=marks(), pending=[
            {"at": (AT - timedelta(hours=1)).isoformat(), "subject": "s",
             "kind": "verdict", "body": "прогон 830 завершился", "reason": "повтор"}])
        decision = outbound.decide("process", "verdict", "Продакт: контур",
                                   "Запускать 861 сейчас?", a_report(), AT, entry,
                                   outbound.no_chat())
        self.assertEqual(decision["action"], "send")
        self.assertIn("прогон 830 завершился", decision["body"])
        self.assertIn("Накопилось с прошлого письма", decision["body"])


class TheThreshold(unittest.TestCase):
    """«Письмо уходит, когда есть что сказать.»

    Three warrants and nothing else: a choice for the user, a change in what the
    product does for them, and ordered work that finished holding a document.
    «Прогон стартовал», «прогон закончился», «репозиторий двинулся» are not
    letters — the push and the board still carry them.
    """

    def test_a_finished_run_alone_is_not_a_letter(self):
        entry = an_entry(marks=marks())
        decision = outbound.decide(
            "process", "verdict", "Продакт: контур",
            "Прогон задачи 830 завершился, репозиторий двинулся на новый коммит.",
            a_report(), AT, entry, outbound.no_chat())
        self.assertEqual(decision["action"], "drop")
        self.assertIn("порог отправки", decision["reason"])

    def test_a_new_question_standing_on_a_task_warrants_a_letter(self):
        entry = an_entry(marks=marks())
        report = a_report(waiting_user=[{"id": 861, "title": "склейка писем"}])
        self.assertTrue(outbound.warrant(report, entry["marks"]))
        decision = outbound.decide("process", "verdict", "Продакт: контур",
                                   "По 861 нужен ваш выбор из двух вариантов.",
                                   report, AT, entry, outbound.no_chat())
        self.assertEqual(decision["action"], "send")

    def test_ordered_work_that_finished_holding_a_document_warrants_a_letter(self):
        entry = an_entry(marks=marks())
        report = a_report(undelivered=[{"id": 743, "title": "отчёт", "age_seconds": 60}])
        decision = outbound.decide("process", "verdict", "Продакт: контур",
                                   "Отчёт по 743 готов и лежит в задаче.",
                                   report, AT, entry, outbound.no_chat())
        self.assertEqual(decision["action"], "send")

    def test_a_question_that_was_already_standing_is_not_a_second_letter(self):
        entry = an_entry(marks=marks(waiting_user=[861]))
        report = a_report(waiting_user=[{"id": 861, "title": "склейка писем"}])
        self.assertEqual(outbound.warrant(report, entry["marks"]), [])

    def test_a_changed_user_path_is_a_change_of_usefulness(self):
        with tempfile.TemporaryDirectory() as home:
            record = a_snapshot(home, "| путь | работает |")
            with mock.patch.object(product_memory, "ROOT", Path(home)):
                report = a_report(products=["task-agent"])
                before = outbound.marks_of(report)
                self.assertEqual(outbound.warrant(report, before), [])
                record.write_text("# t\n## Пользовательские пути\n| путь | сломан |\n",
                                  encoding="utf-8")
                reasons = outbound.warrant(report, before)
        self.assertEqual(len(reasons), 1)
        self.assertIn("изменилась польза", reasons[0])

    def test_an_unreadable_snapshot_is_not_a_path_that_did_not_move(self):
        """A failed read may neither fire the threshold nor record «не менялось»."""
        with tempfile.TemporaryDirectory() as home:
            record = a_snapshot(home, "| путь | работает |")
            with mock.patch.object(product_memory, "ROOT", Path(home)):
                report = a_report(products=["task-agent"])
                before = outbound.marks_of(report)
                record.unlink()
                self.assertEqual(outbound.warrant(report, before), [])
                self.assertNotIn("task-agent", outbound.marks_of(report)["value"])

    def test_a_change_outside_the_user_paths_section_is_not_usefulness(self):
        with tempfile.TemporaryDirectory() as home:
            record = a_snapshot(home, "| путь |", work="- 1")
            with mock.patch.object(product_memory, "ROOT", Path(home)):
                report = a_report(products=["task-agent"])
                before = outbound.marks_of(report)
                record.write_text("# t\n## Пользовательские пути\n| путь |\n## В работе\n- 2\n",
                                  encoding="utf-8")
                self.assertEqual(outbound.warrant(report, before), [])

    def test_the_first_observation_is_a_baseline_and_not_news(self):
        # Otherwise installing this gate would itself produce a letter per
        # direction naming everything that already stood.
        report = a_report(waiting_user=[{"id": 861, "title": "t"}],
                          undelivered=[{"id": 743, "title": "r", "age_seconds": 1}])
        self.assertEqual(outbound.warrant(report, None), [])

    def test_a_declared_mechanical_reason_drops_a_letter_the_threshold_allowed(self):
        entry = an_entry(marks=marks())
        report = a_report(waiting_user=[{"id": 861, "title": "t"}])
        body = "ПОВОД: механика\nПрогон 830 завершился."
        decision = outbound.decide("process", "verdict", "Продакт: контур", body,
                                   report, AT, entry, outbound.no_chat())
        self.assertEqual(decision["action"], "drop")


class CoalescingByContent(unittest.TestCase):
    """«Признак „то же самое“ — содержание, а не тема.»

    The 21 identical subjects were never the defect and matching on them would
    not have caught it either: the letters that must merge are the ones about
    the same run of the same task, whatever they are titled.
    """

    def test_the_same_matter_under_a_different_subject_is_held(self):
        body = "Прогон задачи 830 завершился, ревью 861 ушло на Codex."
        entry = an_entry(marks=marks(),
                         letters=[a_letter(body, AT - timedelta(hours=1),
                                           subject="Продакт: совсем другое")])
        report = a_report(undelivered=[{"id": 743, "title": "r", "age_seconds": 1}])
        decision = outbound.decide("process", "verdict", "Продакт: контур",
                                   body, report, AT, entry, outbound.no_chat())
        self.assertEqual(decision["action"], "hold")
        self.assertIn("то же самое по содержанию", decision["reason"])

    def test_a_new_task_makes_it_new_matter_however_it_is_worded(self):
        before = "Прогон задачи 830 завершился, ревью ушло на Codex."
        after = "Прогон задачи 830 завершился, ревью ушло на Codex, встала 975."
        entry = an_entry(marks=marks(),
                         letters=[a_letter(before, AT - timedelta(hours=1))])
        report = a_report(undelivered=[{"id": 743, "title": "r", "age_seconds": 1}])
        decision = outbound.decide("process", "verdict", "Продакт: контур",
                                   after, report, AT, entry, outbound.no_chat())
        self.assertEqual(decision["action"], "send")

    def test_a_letter_older_than_the_window_no_longer_makes_a_repeat(self):
        body = "Прогон задачи 830 завершился, ревью 861 ушло на Codex."
        old = AT - timedelta(seconds=outbound.COALESCE_SECONDS + 60)
        entry = an_entry(marks=marks(), letters=[a_letter(body, old)])
        report = a_report(undelivered=[{"id": 743, "title": "r", "age_seconds": 1}])
        decision = outbound.decide("process", "verdict", "Продакт: контур",
                                   body, report, AT, entry, outbound.no_chat())
        self.assertEqual(decision["action"], "send")

    def test_held_matter_leaves_as_one_letter_and_not_as_a_stream(self):
        entry = an_entry(marks=marks(), pending=[
            {"at": (AT - timedelta(hours=2)).isoformat(), "subject": "s",
             "kind": "verdict", "body": "первое накопленное", "reason": "повтор"},
            {"at": (AT - timedelta(hours=1)).isoformat(), "subject": "s",
             "kind": "verdict", "body": "второе накопленное", "reason": "повтор"}])
        report = a_report(waiting_user=[{"id": 861, "title": "t"}])
        decision = outbound.decide("process", "verdict", "Продакт: контур",
                                   "По 861 изменилось вот что.", report, AT, entry,
                                   outbound.no_chat())
        self.assertEqual(decision["action"], "send")
        self.assertIn("первое накопленное", decision["body"])
        self.assertIn("второе накопленное", decision["body"])
        outbound.apply(entry, decision, "Продакт: контур", AT, report, "verdict")
        self.assertEqual(entry["pending"], [])

    def test_held_matter_that_waited_too_long_leaves_on_its_own(self):
        # Coalescing is a merge. A merge that never lands is silence, which is
        # the defect on the other side of this one.
        old = AT - timedelta(seconds=outbound.HOLD_MAX_SECONDS + 60)
        entry = an_entry(marks=marks(), pending=[
            {"at": old.isoformat(), "subject": "s", "kind": "verdict",
             "body": "накопленное с ночи", "reason": "повтор"}])
        decision = outbound.decide("process", "verdict", "Продакт: контур",
                                   "Прогон 830 завершился.", a_report(), AT, entry,
                                   outbound.no_chat())
        self.assertEqual(decision["action"], "send")
        self.assertIn("накопленное с ночи", decision["body"])

    def test_holding_records_the_matter_instead_of_dropping_it(self):
        entry = an_entry(marks=marks())
        decision = {"action": "hold", "reason": "повтор", "body": "тело", "flush": [],
                    "fingerprint": outbound.fingerprint("s", "тело")}
        outbound.apply(entry, decision, "Продакт: контур", AT, a_report(), "verdict")
        self.assertEqual(len(entry["pending"]), 1)
        self.assertEqual(entry["letters"], [])


class KnowledgeOfTheConversation(unittest.TestCase):
    """«Прежде чем писать „мы сделали то-то“, продакт проверяет стенограммы.»"""

    def test_a_letter_whose_every_claim_was_said_in_chat_does_not_go(self):
        said = ("Ревью 861 ушло на Codex и вернулось с вердиктом rework. "
                "Прогон задачи 830 завершился в шесть утра.")
        chat = {"sessions": ["s"], "tasks": [861, 830], "pairs": outbound.pairs(said),
                "chars": len(said), "src": "тест"}
        report = a_report(waiting_user=[{"id": 861, "title": "t"}])
        decision = outbound.decide("process", "verdict", "Продакт: контур", said,
                                   report, AT, an_entry(marks=marks()), chat)
        self.assertEqual(decision["action"], "drop")
        self.assertIn("уже проговорено в чате", decision["reason"])

    def test_one_genuinely_new_sentence_makes_the_letter_worth_sending(self):
        said = "Ревью 861 ушло на Codex и вернулось с вердиктом rework."
        chat = {"sessions": ["s"], "tasks": [861], "pairs": outbound.pairs(said),
                "chars": len(said), "src": "тест"}
        body = said + " Отдельно выяснилось, что зеркало отправленного тела не хранит."
        report = a_report(waiting_user=[{"id": 861, "title": "t"}])
        decision = outbound.decide("process", "verdict", "Продакт: контур", body,
                                   report, AT, an_entry(marks=marks()), chat)
        self.assertEqual(decision["action"], "send")

    def test_a_letter_about_a_task_the_conversation_never_named_still_goes(self):
        said = "Ревью 861 ушло на Codex."
        chat = {"sessions": ["s"], "tasks": [861], "pairs": outbound.pairs(said),
                "chars": len(said), "src": "тест"}
        report = a_report(waiting_user=[{"id": 975, "title": "t"}])
        decision = outbound.decide("process", "verdict", "Продакт: контур",
                                   "Ревью 861 ушло на Codex.".replace("861", "975"),
                                   report, AT, an_entry(marks=marks()), chat)
        self.assertEqual(decision["action"], "send")

    def test_an_empty_conversation_cannot_make_anything_already_heard(self):
        self.assertFalse(outbound.already_heard("что угодно", outbound.no_chat()))


class TheConversationReader(unittest.TestCase):
    """Which sessions count as «проговорили в чате».

    The directory holds both, and the background wake-ups outnumber the real
    conversations by roughly nineteen to one — 375 sessions on 2026-08-09, 19 of
    them with a human in them. Counting a wake-up as something the user heard
    would let the contour talk itself out of writing to a user who was never
    there.
    """

    def _session(self, root: Path, name: str, turns: list[tuple[str, str]]) -> None:
        lines = []
        for role, text in turns:
            lines.append(json.dumps({
                "type": role, "timestamp": AT.isoformat().replace("+00:00", "Z"),
                "message": {"role": role, "content": text}}, ensure_ascii=False))
        (root / f"{name}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_a_background_wake_up_is_not_something_the_user_heard(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            self._session(root, "tick", [
                ("user", "Ты продакт-агент на фоновом пробуждении треда «Процессный контур»."),
                ("assistant", "Запустил 861 на Claude.")])
            chat = outbound.heard_in_chat(AT, root)
        self.assertEqual(chat["sessions"], [])
        self.assertEqual(chat["chars"], 0)

    def test_a_session_a_human_typed_in_is_what_the_user_heard(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            self._session(root, "real", [
                ("user", "Ты работаешь как самостоятельный продакт-владелец."),
                ("user", "что там с 861?"),
                ("assistant", "861 стоит и ждёт свободного дерева.")])
            chat = outbound.heard_in_chat(AT, root)
        self.assertEqual(chat["sessions"], ["real"])
        self.assertIn(861, chat["tasks"])
        self.assertIn("ждет свободного", " ".join(sorted(chat["pairs"])))

    def test_a_conversation_older_than_the_window_is_not_read(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            self._session(root, "old", [("user", "что там с 861?")])
            later = AT + timedelta(seconds=outbound.CHAT_LOOKBACK_SECONDS + 3600)
            chat = outbound.heard_in_chat(later, root)
        self.assertEqual(chat["sessions"], [])

    def test_an_unreadable_directory_is_an_empty_conversation_not_a_crash(self):
        chat = outbound.heard_in_chat(AT, Path("/nonexistent-transcripts"))
        self.assertEqual(chat["sessions"], [])


class StandingIdle(unittest.TestCase):
    """Two statements of the user meet here and neither may be dropped.

    «Панель показывает, что в работе ничего нет… тогда почему ничего не делаешь?»
    made silence about idling a defect on 2026-08-07, and «раздражают письма
    примерно про одно и то же» made an hourly letter about it one on the same
    day. The push keeps its own cadence; the letter is rarer.
    """

    def test_standing_idle_is_worth_a_letter_when_it_has_not_been_said(self):
        decision = outbound.decide("process", "idle", "Продакт: простой",
                                   "живых прогонов нет, к запуску 8",
                                   a_report(), AT, an_entry(marks=marks()),
                                   outbound.no_chat())
        self.assertEqual(decision["action"], "send")

    def test_the_same_idleness_is_not_a_letter_every_hour(self):
        entry = an_entry(marks=marks(), letters=[
            a_letter("живых прогонов нет", AT - timedelta(hours=1), kind="idle")])
        decision = outbound.decide("process", "idle", "Продакт: простой",
                                   "живых прогонов нет, к запуску 9",
                                   a_report(), AT, entry, outbound.no_chat())
        self.assertEqual(decision["action"], "drop")
        self.assertIn("пуше и на табло", decision["reason"])

    def test_a_verdict_letter_does_not_reset_the_idle_interval(self):
        entry = an_entry(marks=marks(), letters=[
            a_letter("что-то другое", AT - timedelta(minutes=5), kind="verdict")])
        decision = outbound.decide("process", "idle", "Продакт: простой",
                                   "живых прогонов нет, к запуску 8",
                                   a_report(), AT, entry, outbound.no_chat())
        self.assertEqual(decision["action"], "send")


class BreakageIsNotNews(unittest.TestCase):
    """A broken contour is a fault, and a fault is never coalesced into silence."""

    def test_an_alarm_goes_out_whatever_the_threshold_says(self):
        for kind in outbound.ALWAYS:
            with self.subTest(kind=kind):
                entry = an_entry(marks=marks(), letters=[
                    a_letter("разошлось", AT - timedelta(minutes=1), kind=kind)])
                decision = outbound.decide("process", kind, "Продакт: сбой",
                                           "разошлось", a_report(), AT, entry,
                                           outbound.no_chat())
                self.assertEqual(decision["action"], "send")


class TheLedger(unittest.TestCase):
    """What was said to the user, held where the next tick can read it."""

    def test_a_letter_is_written_down_and_survives_a_reopen(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "outbound.json"
            with outbound.Ledger(path) as ledger:
                entry = ledger.thread("process")
                outbound.apply(entry, {"action": "send", "reason": "тест",
                                       "body": "тело письма", "flush": [],
                                       "fingerprint": outbound.fingerprint("s", "тело")},
                               "Продакт: контур", AT, a_report(), "verdict")
            with outbound.Ledger(path) as ledger:
                letters = ledger.thread("process")["letters"]
        self.assertEqual(len(letters), 1)
        self.assertEqual(letters[0]["kind"], "verdict")
        self.assertEqual(letters[0]["excerpt"], "тело письма")

    def test_a_corrupt_ledger_is_rebuilt_rather_than_fatal(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "outbound.json"
            path.write_text("{не json", encoding="utf-8")
            with outbound.Ledger(path) as ledger:
                self.assertEqual(ledger.thread("process")["letters"], [])
            self.assertEqual(json.loads(path.read_text())["version"], 1)

    def test_only_the_kept_tail_of_letters_is_carried(self):
        entry = an_entry(marks=marks())
        for index in range(outbound.KEEP_LETTERS + 5):
            outbound.apply(entry, {"action": "send", "reason": "тест",
                                   "body": f"письмо {index}", "flush": [],
                                   "fingerprint": outbound.fingerprint("s", str(index))},
                           "Продакт: контур", AT, a_report(), "verdict")
        self.assertEqual(len(entry["letters"]), outbound.KEEP_LETTERS)


class AChoiceRequestIsAQuestion(unittest.TestCase):
    """«Просьба выбрать — это вопрос, чем бы она ни кончалась.»

    Until 2026-08-09 the only text signal was a question mark, so a plain
    Russian request to choose — which ends with a full stop — fell through to
    the threshold and to coalescing. These are that missed case, from both
    sides: what must now be recognized, and what must still not be.
    """

    def a_choice(self) -> str:
        return ("ПОВОД: механика\n"
                "Пожалуйста, выберите: запускать задачу 861 сейчас или после ревью.")

    def test_a_request_to_choose_ending_in_a_full_stop_is_a_question(self):
        self.assertTrue(outbound.asks_user(self.a_choice()))

    def test_an_indirect_request_is_a_question(self):
        self.assertTrue(outbound.asks_user("Нужно ваше решение по задаче 861."))
        self.assertTrue(outbound.asks_user("Жду вашего ответа по 861."))
        self.assertTrue(outbound.asks_user("Оставляю на ваше усмотрение."))

    def test_the_structured_line_makes_a_question_the_text_would_miss(self):
        # The composer knows it is asking; the wording happens to carry none of
        # the forms above. The declared line is enough on its own.
        body = "ПОВОД: польза\nВОПРОС: да\nПорядок работ по 861 стоит поменять."
        self.assertTrue(outbound.asks_user(body))

    def test_a_mislabelled_reason_does_not_lose_the_declared_question(self):
        body = "ПОВОД: механика\nВОПРОС: да\nПорядок работ по 861 стоит поменять."
        decision = outbound.decide("process", "verdict", "Продакт: контур", body,
                                   a_report(), AT, an_entry(marks=marks()),
                                   outbound.no_chat())
        self.assertEqual(decision["action"], "send")

    def test_a_declared_no_cannot_silence_a_question_in_the_text(self):
        # The composer is a language model and it does mislabel — that is the
        # whole reason the text is read independently. «Нет» may not lose a
        # question, only fail to add one.
        body = "ПОВОД: механика\nВОПРОС: нет\nПожалуйста, выберите: 861 сейчас или позже."
        self.assertFalse(outbound.declared_question(body))
        self.assertTrue(outbound.asks_user(body))

    def test_a_choice_request_goes_out_although_nothing_warrants_a_letter(self):
        decision = outbound.decide("process", "verdict", "Продакт: контур",
                                   self.a_choice(), a_report(), AT,
                                   an_entry(marks=marks()), outbound.no_chat())
        self.assertEqual(decision["action"], "send")
        self.assertIn("вопрос", decision["reason"])

    def test_a_choice_request_is_not_coalesced_into_the_letter_before_it(self):
        body = self.a_choice()
        entry = an_entry(marks=marks(),
                         letters=[a_letter(body, AT - timedelta(minutes=5))])
        decision = outbound.decide("process", "verdict", "Продакт: контур", body,
                                   a_report(), AT, entry, outbound.no_chat())
        self.assertEqual(decision["action"], "send")

    def test_a_choice_request_survives_having_been_discussed_in_chat(self):
        body = self.a_choice()
        chat = {"sessions": ["s"], "tasks": [861], "pairs": outbound.pairs(body),
                "chars": 100, "src": "тест"}
        decision = outbound.decide("process", "verdict", "Продакт: контур", body,
                                   a_report(), AT, an_entry(marks=marks()), chat)
        self.assertEqual(decision["action"], "send")

    def test_held_matter_rides_out_with_a_choice_request(self):
        entry = an_entry(marks=marks(), pending=[
            {"at": (AT - timedelta(hours=1)).isoformat(), "subject": "s",
             "kind": "verdict", "body": "прогон 830 завершился", "reason": "повтор"}])
        decision = outbound.decide("process", "verdict", "Продакт: контур",
                                   self.a_choice(), a_report(), AT, entry,
                                   outbound.no_chat())
        self.assertEqual(decision["action"], "send")
        self.assertIn("прогон 830 завершился", decision["body"])

    def test_an_ordinary_report_is_still_not_a_question(self):
        # The negative control. Recognizing more must not turn every letter into
        # one nothing may hold, or the threshold stops existing.
        for body in ("ПОВОД: механика\nПрогон 830 завершился, репозиторий двинулся.",
                     "ПОВОД: польза\nПользователь может выбрать стратегию на табло.",
                     "ПОВОД: готово\nЗадача 861 закрыта, документ приложен."):
            with self.subTest(body=body):
                self.assertFalse(outbound.asks_user(body))

    def test_the_wake_up_prompt_asks_for_the_structured_line(self):
        text = tick.prompt(a_report(), ["прогон 830 завершился"], [], [],
                           outbound.no_chat())
        self.assertIn("ВОПРОС: да|нет", text)


class ThePlanReachesTheBackgroundOwner(unittest.TestCase):
    """«Решение в CLI управляет фоновым тиком.»

    A direction the user paused in an interactive chat stays paused after that
    session ends, and a `planned` task is not permission to start it. The tick
    is woken by a timer with no memory of the conversation, so the only way the
    decision reaches it is by being read from the current revision on disk.
    """

    def a_plan(self, home: str) -> None:
        product_memory.publish_plan(
            {"headline": "цель пользователя", "now": ["1095 — идёт сейчас"],
             "paused": ["MOEX Strategy Lab — остановлен по слову пользователя"]},
            base=Path(home))

    def test_the_prompt_carries_the_current_revision(self):
        with tempfile.TemporaryDirectory() as home:
            self.a_plan(home)
            with mock.patch.object(product_memory, "ROOT", Path(home)):
                text = tick.prompt(a_report(), ["прогон 830 завершился"], [], [],
                                   outbound.no_chat())
        self.assertIn("Редакция: 1", text)
        self.assertIn("MOEX Strategy Lab — остановлен по слову пользователя", text)
        self.assertIn("Направление на паузе не запускается", text)

    def test_a_missing_plan_is_not_an_order_inferred_from_task_statuses(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.object(product_memory, "ROOT", Path(home)):
                text = tick.prompt(a_report(), ["прогон 830 завершился"], [], [],
                                   outbound.no_chat())
        self.assertIn("Портфельного плана нет", text)
        self.assertNotIn("Редакция: ", text)


class LockingAcrossProcesses(unittest.TestCase):
    """«Два таймера не теряют записи друг друга.»

    Real processes and production `Ledger`, because the defect this covers was
    invisible to anything smaller: the lock was taken on the ledger file while
    the commit replaced that same pathname, so a waiter resumed on an inode that
    had already been replaced and wrote its stale copy back over the winner. A
    held question is `pending` of exactly this shape.
    """

    def writers(self, path: Path, source: Path, plan: list[tuple[str, float, float]]):
        started = []
        for thread, delay, hold in plan:
            if delay:
                time.sleep(delay)
            started.append(subprocess.Popen(
                [sys.executable, str(source), str(path), thread, str(hold)]))
        return [process.wait() for process in started]

    def run_plan(self, plan: list[tuple[str, float, float]]) -> dict:
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "outbound.json"
            source = Path(home) / "writer.py"
            source.write_text(WRITER, encoding="utf-8")
            codes = self.writers(path, source, plan)
            self.assertEqual(codes, [0] * len(plan))
            data = json.loads(path.read_text(encoding="utf-8"))
        return {name: len(entry.get("pending", []))
                for name, entry in data["threads"].items()}

    def test_the_writer_that_waited_does_not_erase_the_one_that_held(self):
        held = self.run_plan([("process", 0.0, 1.0), ("moex", 0.3, 0.1)])
        self.assertEqual(held, {"process": 1, "moex": 1})

    def test_the_same_holds_with_the_two_writers_swapped(self):
        # Both directions, because a lock that happens to favour one order is
        # not a lock.
        held = self.run_plan([("moex", 0.0, 1.0), ("process", 0.3, 0.1)])
        self.assertEqual(held, {"process": 1, "moex": 1})

    def test_all_four_direction_timers_survive_one_another(self):
        held = self.run_plan([("process", 0.0, 0.8), ("moex", 0.2, 0.4),
                              ("companion", 0.2, 0.2), ("deep-research", 0.2, 0.1)])
        self.assertEqual(held, {"process": 1, "moex": 1,
                                "companion": 1, "deep-research": 1})

    def test_the_lock_is_not_the_file_that_gets_replaced(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "outbound.json"
            ledger = outbound.Ledger(path)
            with ledger:
                ledger.thread("process")
            self.assertTrue(ledger.lock_path.exists())
            self.assertNotEqual(ledger.lock_path, path)
            # No temporary left behind, and none of them shared a name.
            self.assertEqual(sorted(p.name for p in Path(home).glob("*.tmp")), [])


class ADeletedLedgerIsRecoverable(unittest.TestCase):
    """«Удаление реестра не должно уносить удержанное письмо без следа.»"""

    def hold_one(self, path: Path) -> None:
        with outbound.Ledger(path) as ledger:
            ledger.thread("process")["pending"].append(
                {"at": AT.isoformat(), "subject": "s", "kind": "verdict",
                 "body": "удержанный вопрос", "reason": "тест"})

    def test_a_deleted_ledger_comes_back_from_the_copy(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "outbound.json"
            self.hold_one(path)
            path.unlink()
            with outbound.Ledger(path) as ledger:
                pending = list(ledger.thread("process")["pending"])
        self.assertEqual([item["body"] for item in pending], ["удержанный вопрос"])

    def test_the_recovery_is_visible_and_not_silent(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "outbound.json"
            self.hold_one(path)
            path.unlink()
            with outbound.Ledger(path) as ledger:
                self.assertIn("recovered_at", ledger.data)
            self.assertIn("recovered_at", json.loads(path.read_text(encoding="utf-8")))

    def test_a_deliberate_reset_removes_both_and_stays_a_reset(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "outbound.json"
            self.hold_one(path)
            path.unlink()
            Path(home, "outbound.backup.json").unlink()
            with outbound.Ledger(path) as ledger:
                self.assertEqual(ledger.thread("process")["pending"], [])
                self.assertNotIn("recovered_at", ledger.data)

    def test_a_corrupt_ledger_prefers_the_copy_over_starting_empty(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "outbound.json"
            self.hold_one(path)
            path.write_text("{не json", encoding="utf-8")
            with outbound.Ledger(path) as ledger:
                pending = list(ledger.thread("process")["pending"])
        self.assertEqual([item["body"] for item in pending], ["удержанный вопрос"])


class TheGatewayJournal(unittest.TestCase):
    """«Проход через шлюз должен остаться на диске дольше одного тика.»"""

    def a_tick(self, path: Path, body: str, report: dict) -> dict:
        with mock.patch.object(outbound, "LEDGER", path), \
                mock.patch.object(tick, "send_mail", return_value=True):
            return tick.deliver("process", "verdict", "Продакт: контур", body,
                                report, AT, outbound.no_chat())

    def test_every_decision_is_appended_and_none_overwrites_another(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "outbound.json"
            with outbound.Ledger(path) as ledger:
                ledger.thread("process")["marks"] = marks()
            self.a_tick(path, "Прогон 830 завершился.", a_report())
            self.a_tick(path, "По 861 нужен ваш выбор.",
                        a_report(waiting_user=[{"id": 861, "title": "t"}]))
            lines = [json.loads(line) for line in
                     Path(home, "outbound-journal.jsonl").read_text(
                         encoding="utf-8").splitlines()]
        self.assertEqual([item["action"] for item in lines], ["drop", "send"])
        self.assertEqual([item["asks_user"] for item in lines], [False, True])
        self.assertEqual({item["thread"] for item in lines}, {"process"})

    def test_the_record_returned_to_the_board_names_the_journal(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "outbound.json"
            with outbound.Ledger(path) as ledger:
                ledger.thread("process")["marks"] = marks()
            record = self.a_tick(path, "Прогон 830 завершился.", a_report())
        self.assertIn("outbound-journal.jsonl", record["src"])
        self.assertEqual(record["action"], "drop")


class TheTickUsesTheGate(unittest.TestCase):
    """The gate is only worth what it is wired to."""

    def test_mail_leaves_this_file_through_one_door(self):
        source = Path(tick.__file__).read_text(encoding="utf-8")
        body = source[source.index("def deliver("):]
        # `deliver` is the only caller of `send_mail`; every other mention of it
        # is its own definition, which sits above `deliver`.
        after = source[source.index("def deliver("):]
        self.assertEqual(after.count("send_mail("), 1)
        self.assertIn("outbound.decide", body)

    def test_the_push_is_not_gated(self):
        # What the gate turns away must still reach the user somewhere.
        source = Path(tick.__file__).read_text(encoding="utf-8")
        self.assertNotIn("outbound", source[source.index("def notify("):
                                            source.index("def runner_contract_alarm(")])

    def test_a_failed_send_is_held_rather_than_recorded_as_said(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "outbound.json"
            report = a_report(waiting_user=[{"id": 861, "title": "t"}])
            with outbound.Ledger(path) as ledger:
                ledger.thread("process")["marks"] = marks()
            with mock.patch.object(outbound, "LEDGER", path), \
                    mock.patch.object(tick, "send_mail", return_value=False) as mailed:
                record = tick.deliver("process", "verdict", "Продакт: контур",
                                      "По 861 нужен ваш выбор.", report, AT,
                                      outbound.no_chat())
            self.assertEqual(mailed.call_count, 1)
            self.assertEqual(record["action"], "hold")
            with outbound.Ledger(path) as ledger:
                entry = ledger.thread("process")
        self.assertEqual(entry["letters"], [])
        self.assertEqual(len(entry["pending"]), 1)

    def test_a_failed_send_does_not_duplicate_what_it_had_merged_in(self):
        # The merged text carries the held items, and nothing was flushed
        # because nothing left — holding the merged text would put every one of
        # them in the ledger a second time, and the next letter would say them
        # twice.
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "outbound.json"
            report = a_report(waiting_user=[{"id": 861, "title": "t"}])
            with outbound.Ledger(path) as ledger:
                entry = ledger.thread("process")
                entry["marks"] = marks()
                entry["pending"] = [{"at": (AT - timedelta(hours=1)).isoformat(),
                                     "subject": "s", "kind": "verdict",
                                     "body": "накопленное раньше", "reason": "повтор"}]
            with mock.patch.object(outbound, "LEDGER", path), \
                    mock.patch.object(tick, "send_mail", return_value=False):
                tick.deliver("process", "verdict", "Продакт: контур",
                             "По 861 нужен ваш выбор.", report, AT, outbound.no_chat())
            with outbound.Ledger(path) as ledger:
                pending = ledger.thread("process")["pending"]
        bodies = [item["body"] for item in pending]
        self.assertEqual(len(pending), 2)
        self.assertEqual(bodies.count("накопленное раньше"), 1)
        self.assertNotIn("Накопилось с прошлого письма", "\n".join(bodies))

    def test_a_failed_flush_of_held_matter_adds_no_empty_item(self):
        # The overdue flush has nothing of its own to say — its body *is* the
        # held items, and they are still held. Holding it again would leave an
        # empty item riding out with every letter from then on.
        old = AT - timedelta(seconds=outbound.HOLD_MAX_SECONDS + 60)
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "outbound.json"
            with outbound.Ledger(path) as ledger:
                entry = ledger.thread("process")
                entry["marks"] = marks()
                entry["pending"] = [{"at": old.isoformat(), "subject": "s",
                                     "kind": "verdict", "body": "накопленное с ночи",
                                     "reason": "повтор"}]
            with mock.patch.object(outbound, "LEDGER", path), \
                    mock.patch.object(tick, "send_mail", return_value=False):
                tick.deliver("process", "verdict", "Продакт: контур",
                             "Прогон 830 завершился.", a_report(), AT,
                             outbound.no_chat())
            with outbound.Ledger(path) as ledger:
                pending = ledger.thread("process")["pending"]
        self.assertEqual([item["body"] for item in pending], ["накопленное с ночи"])

    def test_a_dropped_letter_is_written_where_the_board_reads_it(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "outbound.json"
            with outbound.Ledger(path) as ledger:
                ledger.thread("process")["marks"] = marks()
            with mock.patch.object(outbound, "LEDGER", path), \
                    mock.patch.object(tick, "send_mail") as mailed:
                record = tick.deliver("process", "verdict", "Продакт: контур",
                                      "Прогон 830 завершился.", a_report(), AT,
                                      outbound.no_chat())
        self.assertEqual(mailed.call_count, 0)
        self.assertEqual(record["action"], "drop")
        self.assertIn("порог отправки", record["reason"])

    def test_the_wake_up_prompt_carries_what_the_user_already_heard(self):
        said = [{"at": AT.isoformat(), "subject": "Продакт: контур",
                 "excerpt": "ревью 861 ушло на Codex"}]
        chat = {"sessions": ["s"], "tasks": [861], "pairs": set(), "chars": 42,
                "src": "стенограммы"}
        text = tick.prompt(a_report(), ["прогон 830 завершился"], [], said, chat)
        self.assertIn("Что пользователь уже слышал", text)
        self.assertIn("ревью 861 ушло на Codex", text)
        self.assertIn("Пиши только разницу", text)
        self.assertIn("ПОВОД:", text)

    def test_a_direction_that_said_nothing_yet_gets_no_empty_heading(self):
        text = tick.prompt(a_report(), ["прогон 830 завершился"], [], [],
                           outbound.no_chat())
        self.assertNotIn("Что пользователь уже слышал", text)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
