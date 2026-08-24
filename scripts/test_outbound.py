#!/usr/bin/env python3
"""Regressions for the gate on mail to the user.

Every test here is one of the four rules the complaint of 2026-08-07 produced,
or the measurement behind it. The measurement: 58 letters in 21 hours, 21 of
them with the literally identical subject «Продакт: Продукт». The
rules: a threshold, knowledge of what was said in chat, coalescing by content,
and a question that none of the three may swallow.

    python3 -m unittest discover -s scripts -p 'test_*.py'
"""
from __future__ import annotations

import contextlib
import hashlib
import io
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
import process_map_state as pms
import product_memory
import thread_state
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
            # Written down at the moment a letter goes, because the excerpt is
            # cut to 400 characters and the choice usually falls below the cut.
            "asks_user": outbound.asks_user(body),
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
        # A letter that was not itself a question may not hold one back, however
        # much of the same matter it covered. This is the half of the 2026-08-09
        # rule the user did not narrow: the *first* question on a matter still
        # outranks the threshold, the conversation and coalescing.
        body = "Запускать 861 сейчас или после 856?"
        told = "Задача 861 прошла ревью, 856 ждёт своего круга."
        entry = an_entry(marks=marks(),
                         letters=[a_letter(told, AT - timedelta(minutes=5))])
        decision = outbound.decide("process", "verdict", "Продакт: контур", body,
                                   a_report(), AT, entry, outbound.no_chat())
        self.assertEqual(decision["action"], "send")
        self.assertIn("вопрос", decision["reason"])

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


# The two letters the user called «2 письма про codex-centered практически
# идентичного содержания», verbatim as they were sent on 2026-08-23 — Gmail
# `1a02dc9438edb490` at 08:40 UTC and `1a02e006586680d8` at 09:40 UTC. They are
# the same choice about the same three files, retold in other words an hour
# apart, and they are here rather than paraphrased because a measure of sameness
# is worth only the real pair it was checked against.
ASKED_AT_0840 = """\
ПОВОД: вопрос
ВОПРОС: да

## Над чем работаем

Делаем dev-pipeline действительно универсальным для Codex, Claude Code и Cursor.

Проверено: GitHub-описание и package docstring уже исправлены, но независимый review нашёл три остатка:

- metadata пакета всё ещё называет только Codex и Claude;
- первые абзацы README описывают работу как Codex-only;
- общая исполняемая инструкция называет владельца любой сессии `Codex owner`, включая Claude и Cursor.

Рекомендую исправить все три места сейчас в той же минимальной правке. Цена — изменение ещё одной runtime-строки и её повторная независимая проверка; срок по артефактам неизвестен.

Выберите:

1. **Исправить runtime-инструкцию тоже.** Тогда Codex обновит metadata, README и эту строку, после чего Claude повторно проверит весь результат.
2. **Исправить только публичное описание.** Тогда metadata и README станут универсальными, но известное противоречие в инструкции останется отдельным долгом.

Пока ответ не получен, код не меняется. Из-за незакрытых гейтов этого результата также не может стартовать следующая правка review lifecycle.

Риск/долг: общий многорепозиторный review ещё не прошёл обязательные живые пробы, а основной путь независимой проверки и полного письма для каждой задачи остаётся `GAP/BLOCKED`.
"""

ASKED_AT_0940 = """\
ПОВОД: вопрос
ВОПРОС: да

## Над чем работаем

Делаем dev-pipeline действительно универсальным для Codex, Claude Code и Cursor — не только в описании GitHub, но во всех местах, которые определяют представление и поведение продукта.

Свежий независимый Claude-review вернул результат на доработку:

- GitHub description и package docstring уже исправлены;
- package metadata всё ещё называет только Codex и Claude;
- две строки первого экрана README остаются Codex-only;
- общее runtime-правило называет владельца Claude или Cursor словами `Codex owner`.

Metadata и README точно входят в минимальную правку. Нужен ваш выбор только по исполняемому runtime-правилу.

Моя рекомендация — исправить его сейчас в том же кандидате. Это даст единое нейтральное правило всем трём runtime без нового механизма.

Выберите:

1. **Исправить runtime-правило сейчас.** Codex-автор согласует metadata, README и одну строку существующего правила; затем Claude повторно проверит весь точный кандидат.
2. **Оставить runtime-правило отдельной работой.** Сейчас исправятся только metadata и README; Claude и Cursor пока продолжат получать инструкцию, называющую их Codex-владельцем, а позже понадобится отдельный кандидат и ещё один review.

До ответа код не менялся. Зависимые процессные работы не запущены: одна удерживается незакрытой этой правкой, другая — отдельным непроверенным write-scope Deep Research.

Риск/долг: свежий reviewer не смог независимо открыть живую страницу GitHub из своей сетевой среды; правильное описание подтверждено прежней квитанцией автора, но не повторным внешним чтением reviewer.
"""


class TheSameQuestionIsNotAskedTwice(unittest.TestCase):
    """«Зачем слать один и тот же вопрос?» — пользователь, 2026-08-23.

    The narrowing of the rule above, and the only one: the first question on a
    matter still goes out against everything, and the second asking of it goes
    out only when the letter names a fact the first one did not.
    """

    def sent(self, body: str, at: datetime) -> dict:
        """The 08:40 question as the production ledger writes it down."""
        entry = an_entry(marks=marks())
        subject = "Продакт: Процессный контур"
        decision = outbound.decide("process", "verdict", subject, body,
                                   a_report(), at, entry, outbound.no_chat())
        self.assertEqual(decision["action"], "send")
        outbound.apply(entry, decision, subject, at, a_report(), "verdict")
        return entry

    def test_the_same_choice_retold_an_hour_later_is_not_a_second_letter(self):
        entry = self.sent(ASKED_AT_0840, AT)
        decision = outbound.decide("process", "verdict", "Продакт: Процессный контур",
                                   ASKED_AT_0940, a_report(), AT + timedelta(hours=1),
                                   entry, outbound.no_chat())
        self.assertEqual(decision["action"], "drop")
        self.assertIn("этот вопрос уже задан письмом", decision["reason"])

    def test_the_measure_catches_the_retelling_the_older_one_could_not(self):
        # The number, on that exact pair. `same_matter` reads 18% of word pairs
        # and calls it new matter; what is named is 71% the same.
        first = outbound.fingerprint("Продакт: Процессный контур", ASKED_AT_0840)
        second = outbound.fingerprint("Продакт: Процессный контур", ASKED_AT_0940)
        self.assertLess(outbound.overlap_percent(set(second["pairs"]),
                                                 set(first["pairs"])), 30)
        percent, unit = outbound.same_question(second, first)
        self.assertGreaterEqual(percent, outbound.SAME_QUESTION_PERCENT)
        self.assertEqual(unit, "названного")

    def test_a_reminder_carrying_a_new_fact_still_goes_out(self):
        entry = self.sent(ASKED_AT_0840, AT)
        reminder = ASKED_AT_0940.replace(
            "## Над чем работаем",
            "## Над чем работаем\n\nНОВОЕ: живая проба установки у стороннего "
            "стенда показала, что pyproject публикует пакет с чужим owner.", 1)
        decision = outbound.decide("process", "verdict", "Продакт: Процессный контур",
                                   reminder, a_report(), AT + timedelta(hours=1),
                                   entry, outbound.no_chat())
        self.assertEqual(decision["action"], "send")

    def test_a_new_fact_written_in_bold_still_releases_the_reminder(self):
        # У строки `НОВОЕ:` запасного источника нет: не увидела дверь строку —
        # напоминание с настоящим новым фактом молча не уходит. На настоящих
        # байтах 09:40 жирная строка давала `drop` и «нового факта письмо не
        # называет» (`live-evidence/markers-before.txt` задачи 1260).
        entry = self.sent(ASKED_AT_0840, AT)
        reminder = ASKED_AT_0940.replace(
            "## Над чем работаем",
            "## Над чем работаем\n\n**НОВОЕ: живая проба установки у стороннего "
            "стенда показала, что pyproject публикует пакет с чужим owner.**", 1)
        decision = outbound.decide("process", "verdict", "Продакт: Процессный контур",
                                   reminder, a_report(), AT + timedelta(hours=1),
                                   entry, outbound.no_chat())
        self.assertEqual(decision["action"], "send")
        self.assertNotIn("**", outbound.new_fact(
            reminder, entry["letters"][-1]["fingerprint"]))

    def test_a_reminder_that_names_nothing_new_is_still_a_repeat(self):
        # The composer is a language model, so the `НОВОЕ:` line is checked
        # rather than believed: it has to name something the sent question did
        # not name. Retelling the same three files under a new heading is not it.
        entry = self.sent(ASKED_AT_0840, AT)
        reminder = ASKED_AT_0940.replace(
            "## Над чем работаем",
            "## Над чем работаем\n\nНОВОЕ: напоминаю, что metadata и README всё "
            "ещё называют только Codex.", 1)
        decision = outbound.decide("process", "verdict", "Продакт: Процессный контур",
                                   reminder, a_report(), AT + timedelta(hours=1),
                                   entry, outbound.no_chat())
        self.assertEqual(decision["action"], "drop")

    def test_a_different_question_of_the_same_direction_is_not_a_repeat(self):
        entry = self.sent(ASKED_AT_0840, AT)
        decision = outbound.decide(
            "process", "verdict", "Продакт: Процессный контур",
            "ПОВОД: вопрос\nВОПРОС: да\n\n## Над чем работаем\n\n"
            "1251 — исследование Deep Research, ждёт запуска.\n\n"
            "Запускать 1251 сейчас или после того, как освободится дерево?",
            a_report(), AT + timedelta(hours=1), entry, outbound.no_chat())
        self.assertEqual(decision["action"], "send")

    def test_a_new_fact_about_a_thing_already_named_still_releases_the_letter(self):
        # The failure of a run everybody already knows the number of is new, and
        # naming it uses no new name. Novelty is measured in the words of the
        # claim, not in what it is about.
        entry = self.sent(ASKED_AT_0840, AT)
        reminder = ASKED_AT_0940.replace(
            "## Над чем работаем",
            "## Над чем работаем\n\nНОВОЕ: сборка упала на живой проверке "
            "стороннего стенда, пока ответа не было.", 1)
        decision = outbound.decide("process", "verdict", "Продакт: Процессный контур",
                                   reminder, a_report(), AT + timedelta(hours=1),
                                   entry, outbound.no_chat())
        self.assertEqual(decision["action"], "send")

    def test_one_task_number_in_common_is_not_the_same_question(self):
        # «Запускать 1257 сейчас?» и «1257 упала, чинить или откатить?» называют
        # ровно одно и то же — номер задачи — и по названному совпадают на 100%.
        # Такой вопрос меряется словами, и по ним это 50%: второй вопрос уходит.
        entry = self.sent("Запускать 1257 сейчас или после ревью?", AT)
        decision = outbound.decide("process", "verdict", "Продакт: контур",
                                   "1257 упала на живой проверке. Чинить сейчас "
                                   "или откатить?", a_report(),
                                   AT + timedelta(hours=1), entry, outbound.no_chat())
        self.assertEqual(decision["action"], "send")

    def test_a_short_question_asked_again_in_the_same_words_is_a_repeat(self):
        # Дословный повтор короткого вопроса — тот самый случай, который до
        # 2026-08-23 уходил вторым письмом без сравнения: у такого письма меньше
        # шести названных вещей, и мера возвращала 0 не считая.
        entry = self.sent("Запускать сейчас или подождать?", AT)
        decision = outbound.decide("process", "verdict", "Продакт: контур",
                                   "Запускать сейчас или подождать?", a_report(),
                                   AT + timedelta(hours=1), entry, outbound.no_chat())
        self.assertEqual(decision["action"], "drop")
        self.assertIn("сказанного", decision["reason"])

    def test_a_short_question_retold_about_the_same_choice_is_a_repeat(self):
        # Приёмочное условие пользователя: мера обязана ловить пересказ, а не
        # только дословный повтор. Тот же выбор другими словами — 67% сказанного.
        entry = self.sent("Запускать сейчас или подождать?", AT)
        for retelling in ("Подождать или всё-таки запускать сейчас?",
                          "Начинать сейчас или позже?"):
            with self.subTest(retelling=retelling):
                decision = outbound.decide("process", "verdict", "Продакт: контур",
                                           retelling, a_report(),
                                           AT + timedelta(hours=1), entry,
                                           outbound.no_chat())
                self.assertEqual(decision["action"], "drop")

    def test_a_short_question_about_another_task_is_not_a_repeat(self):
        # Соседнее правило `same_matter`: письмо, назвавшее задачу, которой в
        # прошлом письме не было, — новый предмет при любой доле совпадения.
        # Без него «Запускать 1259?» тонет в «Запускать 1257 сейчас?» на 75%.
        entry = self.sent("Запускать 1257 сейчас или после ревью?", AT)
        decision = outbound.decide("process", "verdict", "Продакт: контур",
                                   "Запускать 1259?", a_report(),
                                   AT + timedelta(hours=1), entry, outbound.no_chat())
        self.assertEqual(decision["action"], "send")

    def test_a_short_reminder_with_a_new_fact_still_goes_out(self):
        # Вторая половина правила пользователя действует и на коротком вопросе:
        # повтор с настоящим новым фактом уходит и называет, что нового.
        entry = self.sent("Запускать сейчас или подождать?", AT)
        decision = outbound.decide("process", "verdict", "Продакт: контур",
                                   "НОВОЕ: очередь встала, свободных исполнителей "
                                   "нет.\n\nЗапускать сейчас или подождать?",
                                   a_report(), AT + timedelta(hours=1), entry,
                                   outbound.no_chat())
        self.assertEqual(decision["action"], "send")

    def test_the_letter_that_was_not_a_question_does_not_hold_one_back(self):
        told = ("ПОВОД: польза\nВОПРОС: нет\n\n## Над чем работаем\n\n"
                "Делаем dev-pipeline универсальным для Codex, Claude Code и "
                "Cursor.\n\nGitHub description и package docstring уже "
                "исправлены; metadata, README и общее runtime-правило пока "
                "называют только Codex.")
        entry = an_entry(marks=marks(), letters=[a_letter(told, AT)])
        self.assertFalse(entry["letters"][0]["asks_user"])
        decision = outbound.decide("process", "verdict", "Продакт: Процессный контур",
                                   ASKED_AT_0940, a_report(), AT + timedelta(hours=1),
                                   entry, outbound.no_chat())
        self.assertEqual(decision["action"], "send")

    def test_a_day_of_other_letters_does_not_make_the_repeat_new_again(self):
        # Пользователь не ставил вопросу срока годности, а до 2026-08-24 срок был:
        # `apply` резал историю до `KEEP_LETTERS`, и повтор, разделённый двадцатью
        # письмами, уходил вторым письмом. По журналу двери процессное направление
        # набирает двадцать писем за 18.8 ч (`live-evidence/horizon-before.txt`).
        entry = self.sent(ASKED_AT_0840, AT)
        for index in range(outbound.KEEP_LETTERS + 5):
            outbound.apply(entry, {"action": "send", "reason": "обычное письмо",
                                   "body": f"Прогон {index} закончился.", "flush": [],
                                   "fingerprint": outbound.fingerprint("s", str(index))},
                           "Продакт: Процессный контур",
                           AT + timedelta(minutes=20 * (index + 1)), a_report(),
                           "verdict")
        decision = outbound.decide("process", "verdict", "Продакт: Процессный контур",
                                   ASKED_AT_0940, a_report(), AT + timedelta(days=1),
                                   entry, outbound.no_chat())
        self.assertEqual(decision["action"], "drop")
        self.assertIn("этот вопрос уже задан письмом", decision["reason"])

    def test_the_kept_tail_still_forgets_everything_that_asked_nothing(self):
        # Обратная половина: помнить дольше положено вопросу, а не всей почте.
        # «Что пользователь уже слышал» остаётся окном последних писем.
        entry = self.sent(ASKED_AT_0840, AT)
        for index in range(outbound.KEEP_LETTERS + 5):
            outbound.apply(entry, {"action": "send", "reason": "обычное письмо",
                                   "body": f"Прогон {index} закончился.", "flush": [],
                                   "fingerprint": outbound.fingerprint("s", str(index))},
                           "Продакт: Процессный контур",
                           AT + timedelta(minutes=20 * (index + 1)), a_report(),
                           "verdict")
        kept = entry["letters"]
        self.assertEqual(len(kept), outbound.KEEP_LETTERS + 1)
        self.assertEqual([letter["asks_user"] for letter in kept].count(True), 1)
        self.assertTrue(kept[0]["asks_user"])
        self.assertEqual(kept[-1]["excerpt"],
                         f"Прогон {outbound.KEEP_LETTERS + 4} закончился.")

    def a_row_of_the_installed_ledger(self, **fields) -> dict:
        """Строка ровно того вида, что лежит в боевом `state/outbound.json`.

        Восемьдесят из восьмидесяти девяти строк на 2026-08-24 написаны кодом
        старше полей `asks_user` и `names`: у них есть `at`, `subject`, `kind`,
        `excerpt`, `reason` и отпечаток из `tasks` и `pairs`.
        """
        row = {"at": AT.isoformat(), "subject": "Продакт: Процессный контур",
               "kind": "verdict", "excerpt": ASKED_AT_0840[:400],
               "reason": outbound.QUESTION_REASON,
               "fingerprint": {"tasks": [], "pairs": sorted(outbound.pairs(
                   f"Продакт: Процессный контур\n{ASKED_AT_0840}"))}}
        row.update(fields)
        return row

    def test_a_question_asked_before_the_field_existed_still_stops_its_repeat(self):
        # Жалоба пользователя началась с двух строк, которых новое поле не
        # застало. Читать их как «не вопрос» значит не защитить ровно те
        # вопросы, ради которых правка делалась. Каждого прежнего признака
        # хватает по отдельности, поэтому проверяются оба.
        by_reason = self.a_row_of_the_installed_ledger(
            excerpt="Продолжаю работу по процессному контуру.")
        by_excerpt = self.a_row_of_the_installed_ledger(reason="изменилась польза")
        for row in (by_reason, by_excerpt):
            with self.subTest(reason=row["reason"]):
                self.assertNotIn("asks_user", row)
                self.assertNotIn("names", row["fingerprint"])
                entry = an_entry(marks=marks(), letters=[row])
                decision = outbound.decide(
                    "process", "verdict", "Продакт: Процессный контур",
                    ASKED_AT_0940, a_report(), AT + timedelta(hours=1), entry,
                    outbound.no_chat())
                self.assertEqual(decision["action"], "drop")
                self.assertIn("этот вопрос уже задан письмом", decision["reason"])
                self.assertIn("названного", decision["reason"])

    def test_an_old_row_that_shows_no_question_silences_nothing(self):
        # Обратная половина: прежний признак должен быть, а не подразумеваться.
        # Строка обычного вердикта вопросом не становится, иначе сама правка
        # проглотила бы вопрос — тот же запрет, что и у `same_matter`.
        told = ("## Над чем работаем\n\nДелаем dev-pipeline универсальным для "
                "Codex, Claude Code и Cursor.\n\nGitHub description и package "
                "docstring исправлены; metadata, README и общее runtime-правило "
                "пока называют только Codex.")
        row = self.a_row_of_the_installed_ledger(
            excerpt=told, reason="изменилась польза",
            fingerprint={"tasks": [], "pairs": sorted(outbound.pairs(told))})
        entry = an_entry(marks=marks(), letters=[row])
        self.assertFalse(outbound.was_question(row))
        decision = outbound.decide("process", "verdict", "Продакт: Процессный контур",
                                   ASKED_AT_0940, a_report(), AT + timedelta(hours=1),
                                   entry, outbound.no_chat())
        self.assertEqual(decision["action"], "send")

    def test_an_old_question_row_outlives_the_kept_tail_too(self):
        # Срок памяти и узнавание прежней строки — две разные вещи, и обе нужны:
        # узнанный вопрос, вытесненный хвостом, сравнивать не с чем.
        row = self.a_row_of_the_installed_ledger()
        entry = an_entry(marks=marks(), letters=[row])
        for index in range(outbound.KEEP_LETTERS + 5):
            outbound.apply(entry, {"action": "send", "reason": "обычное письмо",
                                   "body": f"Прогон {index} закончился.", "flush": [],
                                   "fingerprint": outbound.fingerprint("s", str(index))},
                           "Продакт: Процессный контур",
                           AT + timedelta(minutes=20 * (index + 1)), a_report(),
                           "verdict")
        self.assertEqual(len(entry["letters"]), outbound.KEEP_LETTERS + 1)
        self.assertIs(entry["letters"][0], row)
        decision = outbound.decide("process", "verdict", "Продакт: Процессный контур",
                                   ASKED_AT_0940, a_report(), AT + timedelta(days=1),
                                   entry, outbound.no_chat())
        self.assertEqual(decision["action"], "drop")

    def test_an_old_question_row_still_lets_a_new_fact_through(self):
        # Вторая половина слова пользователя действует и на прежней строке:
        # напоминание с настоящим новым фактом уходит.
        entry = an_entry(marks=marks(), letters=[self.a_row_of_the_installed_ledger()])
        decision = outbound.decide(
            "process", "verdict", "Продакт: Процессный контур",
            "НОВОЕ: прогон 1257 упал на живой проверке.\n\n" + ASKED_AT_0940,
            a_report(), AT + timedelta(hours=1), entry, outbound.no_chat())
        self.assertEqual(decision["action"], "send")

    def test_the_repeat_is_recorded_and_the_standing_question_is_not_erased(self):
        # A question that does not go out may not disappear quietly: the ledger
        # keeps the letter that asked it, the gateway journal keeps the refusal
        # with its number, and nothing is added to `pending` to ride out later
        # as a second copy.
        entry = self.sent(ASKED_AT_0840, AT)
        at = AT + timedelta(hours=1)
        decision = outbound.decide("process", "verdict", "Продакт: Процессный контур",
                                   ASKED_AT_0940, a_report(), at, entry,
                                   outbound.no_chat())
        outbound.apply(entry, decision, "Продакт: Процессный контур", at,
                       a_report(), "verdict")
        self.assertEqual(entry["pending"], [])
        self.assertEqual(len(entry["letters"]), 1)
        self.assertTrue(entry["letters"][0]["asks_user"])
        self.assertIn("71%", decision["reason"])


class ReplyIsNeverLost(unittest.TestCase):
    """The inbound-mail producer, not prose, names an answer with kind=reply."""

    def test_a_reply_bypasses_the_proactive_threshold(self):
        decision = outbound.decide(
            "process", "reply", "Re: пользовательское письмо", "Принял, делаю.",
            a_report(), AT, an_entry(marks=marks()), outbound.no_chat())
        self.assertEqual(decision["action"], "send")
        self.assertIn("ответ на входящее письмо", decision["reason"])

    def test_a_reply_is_not_deduplicated_or_merged(self):
        body = "Принял, делаю задачу 1141."
        entry = an_entry(
            marks=marks(),
            letters=[a_letter(body, AT - timedelta(minutes=5), kind="reply")],
            pending=[{"at": (AT - timedelta(hours=1)).isoformat(), "subject": "s",
                      "kind": "verdict", "body": "другая новость", "reason": "повтор"}],
        )
        chat = {"sessions": ["s"], "tasks": [1141], "pairs": outbound.pairs(body),
                "chars": len(body), "src": "test"}
        decision = outbound.decide(
            "process", "reply", "Re: пользовательское письмо", body,
            a_report(), AT, entry, chat)
        self.assertEqual(decision["action"], "send")
        self.assertEqual(decision["body"], body)
        self.assertEqual(decision["flush"], [])


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
        self.assertIn("остаётся на табло", decision["reason"])

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

    def test_a_marker_written_in_bold_still_declares_the_question(self):
        # Живое наблюдение 23 августа 2026: два письма из двадцати девяти пришли
        # со строками `**ПОВОД: …**` и `**ВОПРОС: …**`, и дверь не видела ни
        # одной из них. Оба тех письма оказались не вопросами, но потерять так
        # можно именно вопрос: `ВОПРОС: да` — это тот источник, который ставили
        # ради письма, чей текст вопросом не читается.
        for body in ("**ПОВОД: вопрос**\n**ВОПРОС: да**\nПорядок работ по 861 "
                     "стоит поменять.",
                     "**ПОВОД:** вопрос\n**ВОПРОС:** да\nПорядок работ по 861 "
                     "стоит поменять."):
            with self.subTest(body=body.splitlines()[0]):
                self.assertEqual(outbound.declared_reason(body), "вопрос")
                self.assertTrue(outbound.declared_question(body))
                self.assertTrue(outbound.asks_user(body))

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
        told = "ПОВОД: механика\nЗадача 861 прошла ревью и ждёт своего круга."
        entry = an_entry(marks=marks(),
                         letters=[a_letter(told, AT - timedelta(minutes=5))])
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

    def test_the_saved_1238_event_requires_a_self_contained_choice(self):
        report = a_report(
            title="Deep Research",
            ready_to_start=[{
                "id": 1238,
                "title": "Объяснить отсутствие прироста качества",
                "status": "completed",
                "summary": "88 разрешимых открытий потеряны в 31 из 108 случаев",
            }],
        )
        text = tick.prompt(
            report,
            ["1238 принята; resolver не изменён и ждёт выбора пользователя"],
            [], [], outbound.no_chat(),
        )
        for required in (
            "Над чем работаем", "исходная потребность", "что именно проверено",
            "ключевые наблюдения", "рекомендация", "реальная альтернатива",
            "что произойдёт при каждом", "что пока не изменилось", "существенный риск",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)
        self.assertNotIn("Верни короткий текст", text)
        self.assertIn("Не выдумывай", text)
        self.assertIn("примеры из общих правил не являются текущим состоянием", text)
        self.assertIn("не синтезируй сочетание вариантов", text)

    def test_the_same_contract_asks_every_letter_to_stand_on_its_own(self):
        # Until 2026-08-23 this block asked a question to be self-contained and
        # told an ordinary verdict to stay short, and the short one won: the
        # verdict of 07:20 UTC that day had no heading, no task number and eight
        # untranslated internal terms. The fork is what the user's «надо системно
        # исправить проблему непонятных писем» removed; `SILENT` is not part of
        # it and stays exactly as it was.
        text = tick.verdict_block()
        self.assertNotIn("обычный вердикт остаётся коротким", text)
        self.assertIn("любое письмо самодостаточно", text)
        self.assertIn("Над чем работаем", text)
        self.assertIn("номер задачи", text)
        self.assertIn("ни одного внутреннего термина без расшифровки", text)
        self.assertIn("разворачивай предложением", text)
        self.assertIn("ровно словом `SILENT`", text)

    def test_the_answer_is_either_silence_or_the_letter_itself(self):
        # 2026-08-24, наблюдение на сохранённом событии 09:40: составитель
        # ответил `SILENT`, тут же передумал строкой «Нет — стоп. Блокер по цели
        # 0016 назвать обязан, поэтому письмо, а не молчание:» и дальше написал
        # правильное письмо. Дверь пропускает такой ответ, потому что он не
        # равен ровно `SILENT`, и пользователь читает эти две служебные строки
        # первыми. Молчание при этом остаётся ровно там же, где было.
        text = tick.verdict_block()
        self.assertIn("либо ровно `SILENT`, либо само письмо", text)
        self.assertIn("Не рассуждай", text)
        self.assertIn("ровно словом `SILENT`", text)

    def test_a_letter_says_in_plain_words_what_it_wants_from_the_reader(self):
        # 2026-08-24: читателю дали только текст письма 22:36 и спросили, что от
        # него хотят. Он четыре раза подряд ответил «нельзя ответить из письма».
        # Письмо и правда ничего не просило, но сказало об этом одной служебной
        # строкой `ВОПРОС: нет`, которую человек читает как часть письма.
        text = tick.verdict_block()
        self.assertIn("что нужно от пользователя", text)
        self.assertIn("«От вас ничего не требуется»", text)
        self.assertIn("`ВОПРОС: нет` человеку", text)

    def test_the_contract_asks_for_plain_engineering_russian(self):
        # 2026-08-23, четвёртое подряд замечание о языке: «мне реально сложно
        # читать, что ты пишешь… я трачу больше времени и быстрее устаю».
        # Запрет стоит там же, где остальной контракт письма, а не только в
        # AGENTS.md: правило, которого нет в промпте, модель не выполняет.
        text = tick.verdict_block()
        self.assertIn("простым инженерным русским", text)
        self.assertIn("Активный залог", text)
        self.assertIn("прямой порядок слов", text)
        for banned in ("вводных оборотов", "сказуемое в конец",
                       "отглагольным", "метафорой"):
            with self.subTest(banned=banned):
                self.assertIn(banned, text)


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
             "paused": ["Продукт — остановлен по слову пользователя"]},
            base=Path(home))

    def test_the_prompt_carries_the_current_revision(self):
        with tempfile.TemporaryDirectory() as home:
            self.a_plan(home)
            with mock.patch.object(product_memory, "ROOT", Path(home)):
                text = tick.prompt(a_report(), ["прогон 830 завершился"], [], [],
                                   outbound.no_chat())
        self.assertIn("Редакция: 1", text)
        self.assertIn("Продукт — остановлен по слову пользователя", text)
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
        held = self.run_plan([("process", 0.0, 1.0), ("product", 0.3, 0.1)])
        self.assertEqual(held, {"process": 1, "product": 1})

    def test_the_same_holds_with_the_two_writers_swapped(self):
        # Both directions, because a lock that happens to favour one order is
        # not a lock.
        held = self.run_plan([("product", 0.0, 1.0), ("process", 0.3, 0.1)])
        self.assertEqual(held, {"process": 1, "product": 1})

    def test_all_four_direction_timers_survive_one_another(self):
        held = self.run_plan([("process", 0.0, 0.8), ("product", 0.2, 0.4),
                              ("client", 0.2, 0.2), ("platform", 0.2, 0.1)])
        self.assertEqual(held, {"process": 1, "product": 1,
                                "client": 1, "platform": 1})

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

    def test_the_mail_wake_instructions_set_reply_explicitly(self):
        instructions = (Path(tick.__file__).parent.parent / "AGENTS.md").read_text(
            encoding="utf-8")
        self.assertIn('kind="reply"', instructions)
        self.assertIn("reply_to_message_id", instructions)

    def test_reply_delivery_reuses_the_door_and_records_the_gmail_receipt(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "outbound.json"
            baseline = marks(waiting_user=[42])
            with outbound.Ledger(path) as ledger:
                ledger.thread("process")["marks"] = baseline
            with mock.patch.object(outbound, "LEDGER", path), \
                    mock.patch.object(tick, "send_mail", return_value="gmail-sent-1") as mailed:
                record = tick.deliver(
                    "process", "reply", "Re: письмо", "Принял, делаю.", a_report(), AT,
                    reply_to_message_id="gmail-incoming-1",
                )
            mailed.assert_called_once_with(
                "Re: письмо", "Принял, делаю.",
                reply_to_message_id="gmail-incoming-1",
                attachments=None,
            )
            with outbound.Ledger(path) as ledger:
                self.assertEqual(ledger.thread("process")["marks"], baseline)
            self.assertEqual(record["action"], "send")
            self.assertEqual(record["kind"], "reply")
            self.assertEqual(record["message_id"], "gmail-sent-1")
            self.assertEqual(record["reply_to_message_id"], "gmail-incoming-1")

    def open_door(self):
        """The three conditions of the mail door, stated rather than inherited.

        They are settings of an installation — an address, a mail client, an
        interpreter — and a test that borrows whichever ones this machine has
        passes here and fails on a fresh clone.
        """
        installed = Path(__file__)  # any path that is a file, and always is one
        return (mock.patch.object(tick, "MAIL_TO", "user@example.com"),
                mock.patch.object(tick, "MAIL_SCRIPT", installed),
                mock.patch.object(tick, "MAIL_PYTHON", installed))

    def test_reply_sender_uses_gmail_reply_mode_and_returns_its_receipt(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="Email sent. Message ID: gmail-sent-2\n", stderr="")
        address, script, python = self.open_door()
        with address, script, python, \
                mock.patch.object(tick.subprocess, "run", return_value=completed) as run:
            receipt = tick.send_mail(
                "Re: ignored by Gmail", "Ответ", reply_to_message_id="gmail-incoming-2",
            )
        command = run.call_args.args[0]
        self.assertEqual(receipt, "gmail-sent-2")
        self.assertIn("--reply-to-message-id", command)
        self.assertIn("gmail-incoming-2", command)
        self.assertNotIn("--subject", command)

    def test_a_finished_report_leaves_through_the_door_as_a_file(self):
        # An approved research report once sat on disk for fourteen hours
        # because this door carried text and nothing else, and the user had to
        # ask where the reports they were told about actually were.
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="Email sent. Message ID: gmail-sent-3\n", stderr="")
        address, script, python = self.open_door()
        with address, script, python, \
                mock.patch.object(tick.subprocess, "run", return_value=completed) as run:
            receipt = tick.send_mail(
                "Отчёт", "Тело", attachments=["/tmp/report.md", "/tmp/conclusion.md"],
            )
        command = run.call_args.args[0]
        self.assertEqual(receipt, "gmail-sent-3")
        self.assertEqual(command.count("--attach"), 2)
        self.assertIn("/tmp/report.md", command)
        self.assertIn("/tmp/conclusion.md", command)

    def test_a_failed_reply_is_not_put_in_the_merge_queue(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "outbound.json"
            with mock.patch.object(outbound, "LEDGER", path), \
                    mock.patch.object(tick, "send_mail", return_value=None):
                record = tick.deliver(
                    "process", "reply", "Re: письмо", "Ответ", {}, AT,
                    reply_to_message_id="gmail-incoming-3",
                )
            with outbound.Ledger(path) as ledger:
                pending = ledger.thread("process")["pending"]
        self.assertEqual(record["action"], "fail")
        self.assertFalse(record["delivered"])
        self.assertEqual(pending, [])

    def test_reply_kind_and_origin_id_cannot_diverge(self):
        with self.assertRaisesRegex(ValueError, "must be supplied together"):
            tick.deliver("process", "reply", "Re: письмо", "Ответ", {}, AT)
        with self.assertRaisesRegex(ValueError, "must be supplied together"):
            tick.deliver("process", "verdict", "Продакт", "Новость", {}, AT,
                         reply_to_message_id="gmail-incoming-1")

    def test_the_push_is_not_gated(self):
        # Сбой контура не проходит порог отправки: его пуш решается здесь, а не
        # правилами о новостях. Что дверь отбросила порогом или повтором, видно
        # на табло и в журнале шлюза.
        source = Path(tick.__file__).read_text(encoding="utf-8")
        self.assertNotIn("outbound", source[source.index("def notify("):
                                            source.index("def require_notification_profile(")])

    def test_the_push_reuses_the_server_owned_bot_transport(self):
        with mock.patch.object(tick, "send_bot_message") as send:
            tick.notify("status")
        send.assert_called_once_with("status")

    def test_a_standing_goal_left_without_an_outcome_is_not_pushed(self):
        # «Я не просил присылать отчёты по задачам в телеграм, только в почту» —
        # пользователь, 23 августа 2026. Сообщение контроля цели он назвал прямо;
        # оно остаётся в снимке направления и в ненулевом коде выхода тика.
        import goal_session

        goal = {"id": 1, "waiting_on": [861], "outcome": "о", "gap": "g"}
        with mock.patch.object(tick, "send_bot_message") as send:
            checked = goal_session.post_check("process", [goal], [], "SILENT", AT)
        send.assert_not_called()
        self.assertFalse(checked["resolved"])
        self.assertIn("told", checked)

    def test_only_a_broken_contour_is_pushed_at_all(self):
        # Продуктовый текст уходит одной почтовой дверью. В Telegram остаются два
        # случая, и оба означают, что контур сломан и сам о себе не расскажет:
        # разошедшийся контракт с task_runner и не отработавшее пробуждение.
        # Отбивки dev-pipeline о фоновых прогонах живут в системе задач и этой
        # проверки не касаются.
        import ast

        callers = []
        for path in sorted(Path(tick.__file__).parent.glob("*.py")):
            if path.name.startswith("test_"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for call in ast.walk(node):
                    if not isinstance(call, ast.Call):
                        continue
                    name = call.func.attr if isinstance(call.func, ast.Attribute) else \
                        getattr(call.func, "id", None)
                    if name == "notify":
                        callers.append((path.name, node.name))
        self.assertEqual(sorted(callers), [("thread_tick.py", "main"),
                                           ("thread_tick.py", "runner_contract_alarm")])

    def test_notification_profile_preflight_uses_server_owner(self):
        with mock.patch.object(
            tick, "resolve_bot_target", return_value=("secret", "destination")
        ) as resolve:
            self.assertEqual(tick.require_notification_profile(), "destination")
        resolve.assert_called_once_with()

    def test_missing_notification_profile_has_a_product_error(self):
        with mock.patch.object(tick, "resolve_bot_target", side_effect=ValueError("missing")):
            with self.assertRaisesRegex(SystemExit, "server-owned notification profile"):
                tick.require_notification_profile()

    def test_background_owner_receives_no_code_first_order(self):
        text = tick.prompt(a_report(), ["прогон завершился"], [], [], {"sessions": []})
        self.assertIn("ничего не делать", text)
        self.assertIn("убрать или отключить", text)
        self.assertIn("настроить или переиспользовать", text)
        self.assertIn("минимально необходимый код", text)

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
        self.assertIn("Новостью может быть только то, чего в этом списке нет", text)
        # …and the anti-repeat rule may not turn back into «пиши покороче»: the
        # heading and the plain-words explanation are owed in every letter.
        self.assertIn("не пересказ новости, а то, без чего", text)
        self.assertIn("ПОВОД:", text)

    def test_a_direction_that_said_nothing_yet_gets_no_empty_heading(self):
        text = tick.prompt(a_report(), ["прогон 830 завершился"], [], [],
                           outbound.no_chat())
        self.assertNotIn("Что пользователь уже слышал", text)


class TheExternalInstructionIsNamedByPath(unittest.TestCase):
    """«Пришло письмо, что нужно передать инструкцию, но ссылки на неё нет.»

    2026-08-20 18:15 UTC письмо `1a02063a8c4aa0c3` попросило передать внешнему
    A100-агенту «инструкцию редакции 3» и не сказало, где она лежит. Файл в эту
    минуту существовал, был принят независимым ревью, и его sha256 считался
    одной командой. Пользователь ответил: «Не надо так присылать».

    Правило в `AGENTS.md` к тому моменту уже стояло больше двух недель, поэтому
    проверяется здесь не текст продакта. Письмо о принятой инструкции пишет сама
    дверь, из наблюдения: путь и полный sha256 отправляемых байтов есть в нём по
    построению, а не потому, что кто-то не забыл. Две прошлые редакции этой
    правки дописывали ту же строку в чужое письмо и обе разбились об одно и то
    же — у двери не было связи между готовым файлом и конкретным сообщением.
    Отсюда две половины набора: что дверь говорит сама и чего она не делает с
    чужими письмами.
    """

    def a_deliverable(self, home: str, number: int, name: str, text: str) -> Path:
        path = Path(home) / "tasks" / f"{number}-external" / "deliverables" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def a_direction(self, home: str, *tasks: tuple[int, str]):
        """Направление и его задачи — от новой к старой, как отдаёт `thread_tasks`."""
        rows = [{"id": number, "path": f"tasks/{number}-external", "status": status}
                for number, status in tasks]
        return (mock.patch.object(pms, "REPO", Path(home)),
                mock.patch.object(thread_state, "load_thread",
                                  return_value={"projects": ["greenfield"]}),
                mock.patch.object(pms, "thread_tasks", return_value=rows))

    def letter(self, home: str, tasks: tuple, thread: str) -> dict | None:
        repo, direction, listing = self.a_direction(home, *tasks)
        with repo, direction, listing:
            return outbound.instruction_letter(thread)

    def send(self, ledger: Path, letter: dict, moment: datetime = AT,
             result: str | bool = "gmail-1"):
        """Письмо двери — через саму дверь, как его отправляет тик."""
        with mock.patch.object(outbound, "LEDGER", ledger), \
                mock.patch.object(tick, "send_mail", return_value=result) as mailed:
            record = tick.deliver("deep-research", "instruction",
                                  "Продакт: Deep Research — путь к принятой инструкции",
                                  letter["body"], a_report(), moment,
                                  names_instructions=letter["names"])
        return record, mailed

    # --- что дверь говорит сама -------------------------------------------

    def test_the_letter_the_door_writes_carries_the_path_and_the_full_digest(self):
        with tempfile.TemporaryDirectory() as home:
            path = self.a_deliverable(home, 1233, "a100-instruction.md", "редакция 3\n")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            letter = self.letter(home, ((1233, "completed"),), "deep-research")
        self.assertIn(str(path), letter["body"])
        self.assertIn(digest, letter["body"])
        self.assertEqual(len(digest), 64)
        self.assertEqual([item["sha256"] for item in letter["names"]], [digest])
        # Самодостаточное сообщение: его пересылают исполнителю как есть.
        self.assertIn(outbound.HANDOFF_NOTE, letter["body"])

    def test_it_owes_nothing_to_the_prose_of_the_owner(self):
        # Живой случай 2026-08-20 19:05 UTC: продакт написал «внешний
        # A100-прогон Deep Research готов» и не назвал номера задачи вовсе.
        # Зацепка за прозу — та же просьба к модели, прочитанная с другой
        # стороны; здесь прозу писем не читают ни для чего.
        body = "Инструкция для внешнего A100-прогона Deep Research готова."
        self.assertEqual(outbound.task_ids(body), set())
        source = Path(outbound.__file__).read_text(encoding="utf-8")
        start = source.index("def instruction_letter(")
        self.assertIn("(thread: str)", source[start:start + 120])
        with tempfile.TemporaryDirectory() as home:
            path = self.a_deliverable(home, 1233, "a100-instruction.md", "редакция 3\n")
            letter = self.letter(home, ((1233, "completed"),), "deep-research")
        self.assertIn(str(path), letter["body"])

    def test_an_unaccepted_instruction_is_not_handed_over_as_ready(self):
        # 1216 на 2026-08-20 стоит `blocked` со своей инструкцией в
        # `deliverables/`. Черновик, названный принятым, стоит дороже молчания.
        with tempfile.TemporaryDirectory() as home:
            self.a_deliverable(home, 1216, "a100-frozen-108-baseline-instruction.md",
                               "черновик\n")
            letter = self.letter(home, ((1216, "blocked"),), "deep-research")
        self.assertIsNone(letter)

    def test_a_newer_draft_does_not_displace_the_accepted_edition(self):
        with tempfile.TemporaryDirectory() as home:
            draft = self.a_deliverable(home, 1240, "a100-instruction.md", "черновик\n")
            ready = self.a_deliverable(home, 1233, "a100-instruction.md", "редакция 3\n")
            letter = self.letter(home, ((1240, "blocked"), (1233, "completed")),
                                 "deep-research")
        self.assertIn(str(ready), letter["body"])
        self.assertNotIn(str(draft), letter["body"])

    def test_only_the_current_edition_goes_not_the_whole_shelf(self):
        # 953, 1098, 1162 и 1233 — все приняты и все с инструкцией. Перечень из
        # четырёх файлов человеку не «переслать исполнителю», а выбрать.
        with tempfile.TemporaryDirectory() as home:
            old = [self.a_deliverable(home, number, "instruction.md", f"{number}\n")
                   for number in (1162, 1098, 953)]
            current = self.a_deliverable(home, 1233, "a100-instruction.md", "редакция 3\n")
            letter = self.letter(
                home,
                ((1233, "completed"), (1162, "completed"),
                 (1098, "completed"), (953, "completed")),
                "deep-research")
        self.assertIn(str(current), letter["body"])
        for path in old:
            self.assertNotIn(str(path), letter["body"])

    def test_a_deliverable_that_is_not_an_instruction_is_not_offered(self):
        # Соседние файлы той же задачи: `1233-candidate.patch` — самый большой в
        # каталоге, а `…-instruction-adherence-findings.md` лишь называет слово.
        with tempfile.TemporaryDirectory() as home:
            self.a_deliverable(home, 1233, "1233-candidate.patch", "diff --git a b\n")
            self.a_deliverable(home, 1233, "gigachat-instruction-adherence-findings.md", "x\n")
            letter = self.letter(home, ((1233, "completed"),), "deep-research")
        self.assertIsNone(letter)

    def test_a_direction_without_one_says_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            self.a_deliverable(home, 830, "report.md", "итоги\n")
            self.assertIsNone(self.letter(home, ((830, "completed"),), "process"))

    def test_the_digest_is_of_the_bytes_being_sent_not_of_a_remembered_edition(self):
        # Ревью переписывает инструкцию по тому же пути. Цифра, взятая из памяти
        # плана, назвала бы редакцию, которой на диске уже нет.
        with tempfile.TemporaryDirectory() as home:
            path = self.a_deliverable(home, 1233, "a100-instruction.md", "редакция 3\n")
            tasks = ((1233, "completed"),)
            before = self.letter(home, tasks, "deep-research")["body"]
            path.write_text("редакция 4\n", encoding="utf-8")
            after = self.letter(home, tasks, "deep-research")["body"]
        self.assertIn(hashlib.sha256("редакция 3\n".encode()).hexdigest(), before)
        self.assertIn(hashlib.sha256("редакция 4\n".encode()).hexdigest(), after)

    def test_an_unknown_direction_costs_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.object(pms, "REPO", Path(home)), \
                    mock.patch.object(thread_state, "load_thread",
                                      side_effect=SystemExit("unknown thread")):
                self.assertIsNone(outbound.instruction_letter("нет такого"))

    def test_an_unreadable_instruction_never_becomes_a_letter_without_its_digest(self):
        """Ревью 2026-08-20: «при сбое наблюдения дверь повторяет исходный дефект».

        Инструкция видна и не читается. Письма о ней без её цифры не бывает —
        это ровно просьба передать неназванный файл. Ничего не теряется:
        следующий тик посмотрит снова, а причина названа в журнале службы.
        """
        noise = io.StringIO()
        with tempfile.TemporaryDirectory() as home:
            path = self.a_deliverable(home, 1233, "a100-instruction.md", "редакция 3\n")
            repo, direction, listing = self.a_direction(home, (1233, "completed"))
            with repo, direction, listing, \
                    mock.patch.object(pms, "_file_sha256", return_value=None), \
                    contextlib.redirect_stderr(noise):
                letter = outbound.instruction_letter("deep-research")
        self.assertIsNone(letter)
        self.assertIn(str(path), noise.getvalue())

    def test_an_unobservable_task_index_costs_the_letter_and_nothing_else(self):
        # Установка, где систему задач не видно. Прошлые редакции в этом месте
        # отправляли handoff без пути и цифры; теперь письма просто нет, а
        # остальная почта контура идёт как шла.
        noise = io.StringIO()
        with mock.patch.object(pms, "thread_tasks",
                               side_effect=pms.ContractError("система задач не наблюдается")), \
                mock.patch.object(thread_state, "load_thread", return_value={}), \
                contextlib.redirect_stderr(noise):
            self.assertIsNone(outbound.instruction_letter("deep-research"))
        self.assertIn("не наблюдается", noise.getvalue())

    # --- что дверь делает с этим письмом и чего не делает с чужими ---------

    def test_the_door_sends_it_and_names_the_edition_only_then(self):
        with tempfile.TemporaryDirectory() as home:
            path = self.a_deliverable(home, 1233, "a100-instruction.md", "редакция 3\n")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            ledger = Path(home) / "outbound.json"
            letter = self.letter(home, ((1233, "completed"),), "deep-research")
            record, mailed = self.send(ledger, letter)
            with outbound.Ledger(ledger) as book:
                entry = book.thread("deep-research")
                named = list(entry["instructions"])
                said = entry["letters"][-1]
        self.assertEqual(record["action"], "send")
        self.assertEqual(record["message_id"], "gmail-1")
        sent = mailed.call_args.args[1]
        self.assertIn(str(path), sent)
        self.assertIn(digest, sent)
        self.assertEqual([item["sha256"] for item in named], [digest])
        self.assertEqual(named[0]["path"], str(path))
        # Реестр говорит, что было сказано пользователю: разбуженный продакт
        # прочитает в промпте, что путь уже у человека.
        self.assertIn(outbound.HANDOFF_HEADING, said["excerpt"])

    def test_a_failed_send_names_nothing_and_queues_no_stale_digest(self):
        with tempfile.TemporaryDirectory() as home:
            self.a_deliverable(home, 1233, "a100-instruction.md", "редакция 3\n")
            ledger = Path(home) / "outbound.json"
            letter = self.letter(home, ((1233, "completed"),), "deep-research")
            record, _ = self.send(ledger, letter, result=False)
            with outbound.Ledger(ledger) as book:
                entry = book.thread("deep-research")
                named = list(entry.get("instructions") or [])
                held = list(entry["pending"])
        self.assertEqual(record["action"], "fail")
        self.assertEqual(named, [])
        # Не в очередь: текст, пролежавший полдня, назвал бы редакцию, которой на
        # диске уже может не быть. Следующий тик соберёт письмо заново.
        self.assertEqual(held, [])

    def test_the_same_edition_is_not_named_twice(self):
        with tempfile.TemporaryDirectory() as home:
            self.a_deliverable(home, 1233, "a100-instruction.md", "редакция 3\n")
            ledger = Path(home) / "outbound.json"
            letter = self.letter(home, ((1233, "completed"),), "deep-research")
            self.send(ledger, letter)
            again, mailed = self.send(ledger, letter, moment=AT + timedelta(hours=8))
        self.assertEqual(again["action"], "drop")
        mailed.assert_not_called()

    def test_a_new_edition_of_the_same_path_is_named_again(self):
        # Ревью переписало файл по тому же пути: другие байты — другая редакция —
        # то, чего пользователю ещё не называли.
        with tempfile.TemporaryDirectory() as home:
            path = self.a_deliverable(home, 1233, "a100-instruction.md", "редакция 3\n")
            ledger = Path(home) / "outbound.json"
            tasks = ((1233, "completed"),)
            self.send(ledger, self.letter(home, tasks, "deep-research"))
            path.write_text("редакция 4\n", encoding="utf-8")
            record, mailed = self.send(ledger, self.letter(home, tasks, "deep-research"),
                                       moment=AT + timedelta(hours=8), result="gmail-2")
            sent = mailed.call_args.args[1]
        self.assertEqual(record["action"], "send")
        self.assertIn(hashlib.sha256("редакция 4\n".encode()).hexdigest(), sent)
        self.assertNotIn(hashlib.sha256("редакция 3\n".encode()).hexdigest(), sent)

    def test_no_letter_of_the_owner_is_rewritten_whatever_it_says(self):
        """Ревью 2026-08-20 и 2026-08-21: соседнее письмо направления не трогается.

        Ни то, которое про сводку качества, ни то, которое про внешний прогон:
        дверь не дописывает в чужой текст вообще ничего, поэтому порядок писем
        здесь ничего не решает.
        """
        with tempfile.TemporaryDirectory() as home:
            path = self.a_deliverable(home, 1233, "a100-instruction.md", "редакция 3\n")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            ledger = Path(home) / "outbound.json"
            repo, direction, listing = self.a_direction(home, (1233, "completed"))
            bodies = ["Опубликована новая сводка качества, вопросов нет.",
                      "Внешний прогон готов. Передайте принятую инструкцию исполнителю."]
            with mock.patch.object(outbound, "LEDGER", ledger), repo, direction, listing, \
                    mock.patch.object(tick, "send_mail",
                                      side_effect=["gmail-1", "gmail-2"]) as mailed:
                for number, body in enumerate(bodies):
                    tick.deliver("deep-research", "reply", "Re: Deep Research", body,
                                 a_report(), AT + timedelta(minutes=number),
                                 outbound.no_chat(), reply_to_message_id=f"mail-{number}")
            sent = [call.args[1] for call in mailed.call_args_list]
        self.assertEqual(sent, bodies)
        for text in sent:
            self.assertNotIn(digest, text)
            self.assertNotIn(str(path), text)

    def test_an_unrelated_letter_first_does_not_leave_the_handoff_bare(self):
        """Точная проба ревью 2026-08-21, в её собственном порядке.

        Раньше первое же несвязанное письмо уносило событие, и настоящее письмо
        про готовый внешний прогон уходило следом без пути и sha256. Теперь
        порядок не решает ничего: путь и цифру несёт собственное письмо двери, и
        оно уходит независимо от того, что и когда написал продакт.
        """
        with tempfile.TemporaryDirectory() as home:
            path = self.a_deliverable(home, 1233, "a100-instruction.md", "редакция 3\n")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            ledger = Path(home) / "outbound.json"
            repo, direction, listing = self.a_direction(home, (1233, "completed"))
            letter = self.letter(home, ((1233, "completed"),), "deep-research")
            report = a_report(title="Deep Research", undelivered=[{"id": 1233}])
            with outbound.Ledger(ledger) as book:
                book.thread("deep-research")["marks"] = marks()
            with mock.patch.object(outbound, "LEDGER", ledger), repo, direction, listing, \
                    mock.patch.object(tick, "send_mail",
                                      side_effect=["gmail-1", "gmail-2", "gmail-3"]) as mailed:
                tick.deliver("deep-research", "reply", "Re: сводка качества",
                             "Опубликована новая сводка качества, вопросов нет.",
                             report, AT, outbound.no_chat(),
                             reply_to_message_id="mail-77")
                tick.deliver("deep-research", "instruction",
                             "Продакт: Deep Research — путь к принятой инструкции",
                             letter["body"], report, AT + timedelta(minutes=1),
                             names_instructions=letter["names"])
                handoff_record = tick.deliver(
                    "deep-research", "verdict", "Продакт: Deep Research",
                    "Внешний прогон готов. Передайте принятую инструкцию исполнителю.",
                    report, AT + timedelta(minutes=2), outbound.no_chat())
            first, own, handoff = (call.args[1] for call in mailed.call_args_list)
        self.assertEqual(handoff_record["action"], "send")
        self.assertNotIn(digest, first)
        self.assertIn(str(path), own)
        self.assertIn(digest, own)
        # Письмо продакта осталось его письмом; самодостаточное сообщение у
        # пользователя уже есть, и переслать исполнителю можно именно его.
        self.assertNotIn(digest, handoff)

    def test_an_unreadable_instruction_does_not_touch_an_unrelated_reply(self):
        """Вторая проба ревью 2026-08-21: нечитаемый файл ломал чужой ответ.

        Отрицательная ветка была шире предмета — удержание касалось любого письма
        направления. Теперь она касается ровно одного письма: того, которого не
        будет.
        """
        with tempfile.TemporaryDirectory() as home:
            self.a_deliverable(home, 1233, "a100-instruction.md", "редакция 3\n")
            ledger = Path(home) / "outbound.json"
            repo, direction, listing = self.a_direction(home, (1233, "completed"))
            with mock.patch.object(outbound, "LEDGER", ledger), repo, direction, listing, \
                    mock.patch.object(pms, "_file_sha256", return_value=None), \
                    mock.patch.object(tick, "send_mail", return_value="gmail-1") as mailed:
                record = tick.deliver("deep-research", "reply", "Re: Deep Research",
                                      "Спасибо, сводка качества принята.", a_report(), AT,
                                      outbound.no_chat(), reply_to_message_id="mail-80")
        self.assertEqual(record["action"], "send")
        self.assertEqual(mailed.call_args.args[1], "Спасибо, сводка качества принята.")

    def test_the_threshold_never_judges_it(self):
        # «Новость ли это» решается о новостях направления. Путь к принятой
        # инструкции пользователь просил дважды, и порог о нём не спрашивают.
        with tempfile.TemporaryDirectory() as home:
            self.a_deliverable(home, 1233, "a100-instruction.md", "редакция 3\n")
            ledger = Path(home) / "outbound.json"
            with outbound.Ledger(ledger) as book:
                book.thread("deep-research")["marks"] = marks()
            letter = self.letter(home, ((1233, "completed"),), "deep-research")
            record, mailed = self.send(ledger, letter)
        self.assertEqual(record["action"], "send")
        self.assertEqual(mailed.call_count, 1)

    def test_it_does_not_move_the_baseline_the_owners_letter_needs(self):
        # Письмо двери не говорит о новостях направления, поэтому не смеет
        # засчитать их как рассказанные: иначе оно само заглушило бы письмо
        # продакта о той самой приёмке.
        with tempfile.TemporaryDirectory() as home:
            self.a_deliverable(home, 1233, "a100-instruction.md", "редакция 3\n")
            ledger = Path(home) / "outbound.json"
            with outbound.Ledger(ledger) as book:
                book.thread("deep-research")["marks"] = marks()
            report = a_report(title="Deep Research", undelivered=[{"id": 1233}])
            letter = self.letter(home, ((1233, "completed"),), "deep-research")
            with mock.patch.object(outbound, "LEDGER", ledger), \
                    mock.patch.object(tick, "send_mail", return_value="gmail-1"):
                tick.deliver("deep-research", "instruction", "Продакт: путь",
                             letter["body"], report, AT,
                             names_instructions=letter["names"])
            with mock.patch.object(outbound, "LEDGER", ledger), \
                    mock.patch.object(tick, "send_mail", return_value="gmail-2"):
                record = tick.deliver("deep-research", "verdict", "Продакт: Deep Research",
                                      "Кандидат 1233 принят, прогон ждёт внешнего стенда.",
                                      report, AT + timedelta(minutes=1), outbound.no_chat())
        self.assertEqual(record["action"], "send")
        self.assertIn("1233", record["reason"])

    def test_the_tick_sends_it_before_it_decides_to_wake_anybody(self):
        # Письмо собрано из наблюдения, поэтому не зависит ни от прозы продакта,
        # ни от того, будили ли его в этот тик вообще.
        source = Path(tick.__file__).read_text(encoding="utf-8")
        body = source[source.index("def main("):]
        self.assertIn('names_instructions=letter["names"]', body)
        self.assertLess(body.index("outbound.instruction_letter("),
                        body.index("if not woke:"))

    def test_the_rule_in_the_instructions_names_the_mechanism(self):
        instructions = (Path(tick.__file__).parent.parent / "AGENTS.md").read_text(
            encoding="utf-8")
        self.assertIn("instruction_letter", instructions)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
