#!/usr/bin/env python3
"""Regressions for the board's data contract.

The renderer's only guarantee — that it cannot show what it never saw — rests on
the shape of two documents. These tests hold that shape, the anonymisation that
goes with it and the layout decisions the picture depends on.

    python3 -m unittest discover -s scripts -p 'test_*.py'
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import process_map_recorder as recorder
import process_map_render as render
import process_map_schema as schema
import process_map_serve as serve
import process_map_state as state
import product_memory
import runner_contract
import thread_state as thread
import thread_tick as tick

# One fixed instant for every rate-limit test: a reminder is a frequency, and a
# frequency cannot be asserted against a clock that moves under the assertion.
AT = datetime(2026, 8, 7, 15, 0, 0, tzinfo=timezone.utc)


def a_question(**over) -> dict:
    """One open question as the collector hands it over: text plus its owner.

    Never a bare string on this boundary. Whose question it is decides which area
    it stands in, and a board that guesses the owner is the defect this shape
    exists to prevent — «ждёт решения человека» held sixteen entries and the user
    owned three of them.
    """
    question = {"text": "Публиковать ли перенос?", "owner": "product",
                "asked_at": None, "channel": None, "ref": None,
                "asked_src": None, "answer_src": None,
                "note": "не помечено как спрошенное у пользователя — это наш вопрос"}
    question.update(over)
    return question


def asked_question(**over) -> dict:
    """A question that was actually put to the user, with the mark that says so."""
    return a_question(owner="user", asked_at="2026-08-05", channel="email",
                      ref="18f0000000000001",
                      asked_src="пометка «спрошено у пользователя 2026-08-05, "
                                "письмо 18f0000000000001» в самой строке",
                      note=None, **over)


def a_task(**over) -> dict:
    board = over.pop("board", {})
    run = over.pop("run", None)
    detail = over.pop("detail", {})
    questions = over.pop("questions", [])
    task = {"id": 1, "title": "Задача", "status": "planned", "status_detail": None,
            "dir": "001-task", "gates": [], "flags": [],
            "questions": questions,
            "asked_user": [q for q in questions if q["owner"] == "user"],
            "our_questions": [q for q in questions if q["owner"] == "product"],
            "run": {"state": None, "current_step": None, "runner": None,
                    "workflow": None, "sandbox": None,
                    "stop_reason": None, "exit_code": None, "pid": None,
                    "alive": False, "alive_src": None, "progress": None,
                    "refusal": None, "refusal_summary": None, "repo": None},
            "detail": {"summary": None, "review": None, "delivery": None, "files": [],
                       "moved": None, "moved_age_seconds": None, "moved_src": None,
                       "handoff": None},
            "board": {"area": "queued", "actor": None, "actor_src": None,
                      "role": None, "role_src": None, "happening": None,
                      "why": None, "why_src": None, "since": None, "since_src": None,
                      "age_seconds": None, "attempt": 0,
                      "blocked_by": None, "blocked_by_src": None,
                      "start_condition": None, "decision": None,
                      "plan_place": None}}
    task.update(over)
    task["board"].update(board)
    task["detail"].update(detail)
    if run:
        task["run"].update(run)
    return task


def a_promise(**over) -> dict:
    """One line of «В работе» as the collector hands it over: text plus the sverka.

    A promise is never a bare string on this boundary. The line is shown because
    a comparison failed, and the comparison travels with it — otherwise the area
    is back to printing «задачи нет» from a test that never looked for a task.
    """
    promise = {"text": "2026-08-04 — ревью кода клиента силами Claude",
               "link": "unknown",
               "checked": "сверено с каталогом задач (710 задач) по номеру-ссылке, "
                          "по названию и по слагу"}
    promise.update(over)
    return promise


def a_next_check(**over) -> dict:
    """When a direction looks again, as systemd answered while the board built.

    Separate from the wake-up record on purpose: this one is about the present,
    and the tick — being the service paired with the timer — can only ever ask
    it at the instant the answer is missing.
    """
    value = {"at": "2026-08-07T15:20:00+00:00",
             "src": "NextElapseUSecRealtime таймера product-thread@process.timer"}
    value.update(over)
    return value


def a_wake(**over) -> dict:
    """Whether a check of this direction is running, as systemd answered.

    Asked at the same instant as `a_next_check`, and about the same unit the
    twenty-minute timer starts — which is why «продолжить сейчас» cannot produce
    a second product owner.
    """
    value = {"unit": "product-thread@process.service", "running": False,
             "src": "ActiveState=inactive единицы product-thread@process.service"}
    value.update(over)
    return value


def a_check(**over) -> dict:
    """One direction's wake-up, as the tick recorded it at the moment it looked.

    Never assembled by the board: «когда проверял в прошлый раз и чем то
    кончилось» is an observation taken at one instant, and a reader that rebuilt
    it later would be answering about a different one.
    """
    check = {"at": "2026-08-07T15:00:00+00:00",
             "outcome": "запустил 1 — задачи 871",
             "outcome_src": "живые прогоны треда, наблюдённые до и после пробуждения",
             "woke_owner": True,
             "started": [871],
             "events": ["прогон задачи 866 завершился"],
             "reasons": [],
             "queue": {"live": 1, "pickup": 9, "ready": 0, "decided": 0,
                       "undelivered": 10, "waiting_user": 0},
             "src": "state/threads/process.json — запись тика в момент проверки"}
    check.update(over)
    return check


def a_reason(**over) -> dict:
    reason = {"code": "awake_owner",
              "text": "занят другой продакт: session «консоль»",
              "src": "командные строки и рабочие каталоги процессов в /proc"}
    reason.update(over)
    return reason


def an_owner(**over) -> dict:
    """Another instance of the product owner, as `/proc` shows it.

    `worktrees` is the load-bearing field: yielding is about a working tree two
    children would land in, and nothing else.
    """
    owner = {"pid": 1, "kind": "tick", "thread": "product", "worktrees": [],
             "since": "2026-08-06T20:05:00+00:00", "age_seconds": 60,
             "src": "командная строка и исполняемый файл процесса в /proc"}
    owner.update(over)
    return owner


def a_plan_place(**over) -> dict:
    """Что действующая редакция плана говорит об одной задаче.

    Владелец очереди — план, и место в ней никогда не выводится из статуса: сам
    план пишет об этом последней своей строкой — «не является очередью: все
    прочие задачи со статусом planned».
    """
    place = {"role": "queue", "position": 1, "line": "1121 — первый живой запуск",
             "field": "next", "ahead": [], "conflict": [],
             "src": "строка 1 очереди (поле next); действующая редакция 31 "
                    "портфельного плана, файл content/plan/revisions/000031.json"}
    place.update(over)
    return place


def a_plan(**over) -> dict:
    """Проекция плана в том виде, в каком её видит рендер."""
    plan = {"revision": 31, "accepted_at": "2026-08-13T20:48:53+00:00",
            "src": "действующая редакция 31 портфельного плана, файл "
                   "content/plan/revisions/000031.json",
            "outcomes": [], "queue": [a_plan_entry()],
            "backlog": [a_plan_entry(**BACKLOG_ENTRY)]}
    plan.update(over)
    return plan


def a_plan_entry(**over) -> dict:
    entry = {"field": "next", "text": "1121 — первый живой запуск Продукт",
             "tasks": [{"id": 1121, "title": "Продукт: первый живой запуск",
                        "status": "planned"}],
             "also": [],
             "checked": "сверено с каталогом задач (1050 задач) по числам строки; "
                        "каталог знает как задачи: 1121"}
    entry.update(over)
    return entry


BACKLOG_ENTRY = {
    "field": "paused", "kind": "paused",
    "text": "Переделка памяти клиента — на паузе с 2026-08-13 по слову пользователя",
    "tasks": [], "also": [],
    "checked": "сверено с каталогом задач (1050 задач) по числам строки; "
               "ни одно число строки каталог задачей не знает",
}


def a_snapshot(tasks=None, products=None, plan=None) -> dict:
    return {
        "schema_version": schema.SCHEMA_VERSION,
        "mode": "real",
        "threads": [{
            "key": "process", "title": "Процессный контур", "products": ["task-agent"],
            "task_count": len(tasks or [a_task()]), "tasks": tasks or [a_task()],
            "repos": [{"name": "task-agent", "present": True, "branch": "main",
                       "head": "abc1234", "head_subject": "…", "head_at": "2026-08-06T09:00:00+00:00",
                       "tracked_dirty": 1, "unpushed": 2}],
            "channels": [{"channel": "telegram", "direction": "out", "count": 3},
                         {"channel": "email", "direction": "in", "count": 2}],
            "check": a_check(),
            "next_check": a_next_check(),
            "wake": a_wake(),
        }],
        "products": products or [{"slug": "task-agent",
                                  "questions": [asked_question()],
                                  "own_questions": [],
                                  "effect": ["2026-08-06 — карта показывает ленту"],
                                  "promises": [a_promise()]}],
        "owners_awake": [],
        "task_index": [{"id": task["id"], "task": task["dir"],
                        "title": task["title"], "status": task["status"],
                        "updated_at": task["board"]["since"],
                        "updated_src": task["board"]["since_src"]}
                       for task in (tasks or [a_task()])],
        # Порядок работ и паузы приходят от их владельца — плана, — а не выводятся
        # из статусов задач снимка.
        "plan": a_plan() if plan is None else plan,
        # На чём собран снимок: что процесс прочитал при старте и что лежит в
        # дереве сейчас. Расхождение этих двух и есть находка 8 задачи 1163.
        "revision": a_code_revision(),
    }


# Не `a_revision`: так ниже в этом файле называется редакция портфельного плана,
# и это другая вещь. Здесь — ревизия кода, на которой собрана страница.
def a_code_revision(**over) -> dict:
    revision = {"running": "abc1234", "disk": "abc1234",
                "src": "git log -1: первое — при старте процесса, собравшего снимок, "
                       "второе — в рабочем дереве в момент сборки"}
    revision.update(over)
    return revision


def a_record(**over) -> dict:
    record = {"schema_version": schema.SCHEMA_VERSION, "at": "2026-08-06T09:26:40+00:00",
              "kind": "artifact", "label": "analysis.md",
              "observed_by": "появление файла", "station": "analysis"}
    record.update(over)
    return record


class SnapshotContract(unittest.TestCase):
    def test_a_well_formed_snapshot_passes(self):
        schema.validate_snapshot(a_snapshot())

    def test_missing_field_is_refused(self):
        broken = a_snapshot()
        del broken["products"]
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(broken)

    def test_unknown_flag_is_refused(self):
        # A flag the renderer has no shape for would be invisible on the map,
        # which is worse than a crash: the picture would quietly lie.
        broken = a_snapshot([a_task(flags=["glowing"])])
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(broken)

    def test_every_declared_flag_is_accepted(self):
        for flag in schema.TASK_FLAGS:
            schema.validate_snapshot(a_snapshot([a_task(flags=[flag])]))

    def test_wrong_schema_version_is_refused(self):
        broken = a_snapshot()
        broken["schema_version"] = 2
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(broken)


class RecordContract(unittest.TestCase):
    def test_a_well_formed_record_passes(self):
        schema.validate_record(a_record())

    def test_record_without_observation_is_refused(self):
        # The task is explicit: a caption has to say what the transition was
        # observed by. A record that cannot say it must not reach the map.
        with self.assertRaises(schema.ContractError):
            schema.validate_record(a_record(observed_by="  "))

    def test_unknown_kind_station_channel_are_refused(self):
        for bad in ({"kind": "vibes"}, {"station": "deployment"}, {"channel": "carrier-pigeon"}):
            with self.assertRaises(schema.ContractError):
                schema.validate_record(a_record(**bad))

    def test_non_iso_time_is_refused(self):
        with self.assertRaises(schema.ContractError):
            schema.validate_record(a_record(at="вчера вечером"))


# The negative control, in one place: a real path, a real address and a real
# chat identifier, in the shape they actually leaked in.
SECRET = ("/opt/projects/example-product/report.md пишет owner@example.com "
          "в чат 100200300")


class Anonymisation(unittest.TestCase):
    """The negative control: what must not survive, and what must."""

    SECRET = SECRET

    def test_paths_mail_and_numeric_ids_do_not_survive(self):
        clean = schema.scrub({"detail": self.SECRET, "dir": "316-identify-max-user-100200300"})
        self.assertNotIn("/opt/projects", clean["detail"])
        self.assertNotIn("@gmail.com", clean["detail"])
        self.assertNotIn("100200300", clean["detail"])
        self.assertNotIn("100200300", clean["dir"])

    def test_real_task_titles_survive_on_purpose(self):
        # The user asked to recognise a specific task by its real name among all
        # the shown work. Content privacy of titles stays a human step.
        clean = schema.scrub({"title": "Карта процессной работы: вид сверху",
                             "task_title": "Пересчитать 18 строк калькулятора"})
        self.assertEqual(clean["title"], "Карта процессной работы: вид сверху")
        self.assertEqual(clean["task_title"], "Пересчитать 18 строк калькулятора")

    def test_the_recorder_cleans_the_title_it_saves(self):
        # This test used to put the secret in `detail` and assert that
        # `task_title` came back untouched — so it passed while the scribe wrote
        # the raw title back over the cleaned one, and the review's negative
        # control walked straight through it (finding HIGH-3 of review 786). The
        # secret now goes exactly where the bypass lived.
        clean = schema.scrub(a_record(
            kind="artifact", task_title=self.SECRET, label=self.SECRET,
            detail=self.SECRET))
        for field in ("task_title", "label", "detail"):
            self.assertNotIn("/opt/projects", clean[field], field)
            self.assertNotIn("@gmail.com", clean[field], field)
            self.assertNotIn("100200300", clean[field], field)

    def test_the_scribe_has_no_second_cleaner_to_put_values_back_with(self):
        source = Path(recorder.__file__).read_text()
        self.assertNotIn("scrub_record", source)
        self.assertFalse(hasattr(recorder, "scrub_record"))


class RecorderObservation(unittest.TestCase):
    def test_artifacts_map_to_the_station_they_are_evidence_of(self):
        cases = {"analysis.md": "analysis", "plan.md": "analysis", "findings.md": "review",
                 "claude-review-2.md": "review", "verification.md": "report",
                 "trace.md": "report", "test_thing.py": "tests", "engine.py": "development",
                 "notes.txt": None, "progress.json": None}
        for name, station in cases.items():
            self.assertEqual(recorder.Scribe.station_of(name), station, name)

    def test_every_station_a_recorder_can_emit_is_declared(self):
        stations = set(recorder.ARTIFACT_STATIONS.values()) | set(
            s for s in recorder.EVENT_STATIONS.values() if s)
        stations |= {"tests", "development", "commit"}
        self.assertTrue(stations <= set(schema.STATIONS), stations - set(schema.STATIONS))


class Rendering(unittest.TestCase):
    def test_the_page_carries_its_data_and_asks_for_nothing(self):
        data = {"snapshot": a_snapshot(), "timeline": [a_record()], "live_url": None}
        html = render.render(data)
        # «Доска процессной работы» было неверно: направлений четыре (1163).
        self.assertIn("<title>Доска работ</title>", html)
        self.assertNotIn("__DATA__", html)
        # No fetch, no XHR, no external origin: the recording opens off-line.
        for forbidden in ("http://", "https://", "XMLHttpRequest", "src=\"//"):
            self.assertNotIn(forbidden, html.replace('lang="ru"', ""))

    def test_a_retired_repair_is_not_printed_as_an_accepted_one(self):
        """Снятый ремонт доставлен не был, и доска не говорит, что был."""
        page = (Path(__file__).parent / "process_map_template.html").read_text()
        self.assertIn('settled.kind === "accepted" ? " принята" : " снята"', page)
        self.assertIn('"снята: " + settled.reason', page)
        self.assertIn('"\\nнаблюдено: " + settled.src', page)


class TaskIndexTab(unittest.TestCase):
    def test_the_index_carries_lookup_fields_and_no_directory_detail(self):
        with mock.patch.object(state, "state_age", return_value=(
                "2026-08-15T08:30:00+00:00", 60, "mtime task.md")):
            rows = state.task_index([{"id": 1054, "path": "tasks/1054-board-task-index-tab",
                                      "title": "Вкладка списка задач", "status": "planned",
                                      "status_detail": "not index data"}])
        self.assertEqual(rows, [{"id": 1054, "task": "1054-board-task-index-tab",
                                 "title": "Вкладка списка задач", "status": "planned",
                                 "updated_at": "2026-08-15T08:30:00+00:00",
                                 "updated_src": "mtime task.md"}])

    def test_number_and_title_search_and_clickable_rows_are_on_the_page(self):
        page = (Path(__file__).parent / "process_map_template.html").read_text()
        self.assertIn('id="totasks"', page)
        self.assertIn('id="tasksearch"', page)
        self.assertIn("searchesNumber && String(item.id).includes(number)", page)
        self.assertIn("number.length > 0", page)
        self.assertIn("item.title).toLocaleLowerCase", page)
        self.assertIn("asButton(row", page)
        self.assertIn("openIndexedTask(item)", page)

    def test_statuses_can_be_combined_and_the_main_pair_is_one_click(self):
        page = (Path(__file__).parent / "process_map_template.html").read_text()
        self.assertIn('role="group" aria-labelledby="taskfilterlabel"', page)
        self.assertIn('id="taskfilterready"', page)
        self.assertIn('for (const status of ["planned", "in_progress"])', page)
        self.assertIn("!taskStatuses.size || taskStatuses.has(item.status)", page)
        self.assertIn('option.setAttribute("aria-pressed"', page)
        self.assertIn('el("taskfilterready").classList.toggle("on", ready)', page)

    def test_the_global_catalogue_does_not_silently_cap_all_tasks(self):
        completed = mock.Mock(returncode=0, stdout="[]")
        with mock.patch.object(state.subprocess, "run", return_value=completed) as run:
            self.assertEqual(state.task_catalogue(), [])
        command = run.call_args.args[0]
        self.assertNotIn("--limit", command)

    def test_a_directory_backed_card_can_travel_with_the_index_row(self):
        indexed = {"id": 1054, "path": "tasks/1054-board-task-index-tab",
                   "title": "Вкладка", "status": "planned"}
        entry = a_task(id=1054, dir="1054-board-task-index-tab", title="Вкладка")
        row = state.task_index([indexed], {entry["dir"]: entry})[0]
        self.assertIs(row["entry"], entry)
        snapshot = a_snapshot([entry])
        snapshot["task_index"] = [row]
        schema.validate_snapshot(snapshot)

    def test_an_index_row_cannot_carry_another_tasks_card(self):
        snapshot = a_snapshot()
        snapshot["task_index"][0]["entry"] = a_task(id=2, dir="002-other")
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(snapshot)

    def test_a_direction_only_observation_does_not_carry_the_global_index(self):
        source = Path(state.__file__).read_text()
        self.assertIn('task_index(catalogue, indexed_entries) if only is None else []', source)

    def test_an_index_card_reuses_fresh_board_data_on_a_live_poll(self):
        page = (Path(__file__).parent / "process_map_template.html").read_text()
        self.assertIn('boardAgain || (indexAgain && (indexAgain.card', page)
        self.assertIn('openCard(again, true, cardReturn)', page)

    def test_a_closing_script_tag_in_the_data_cannot_end_the_tag(self):
        data = {"snapshot": a_snapshot([a_task(title="</script><b>сломать</b>")]),
                "timeline": [], "live_url": None}
        html = render.render(data)
        self.assertNotIn("</script><b>", html)

    def test_records_are_ordered_by_instant_not_by_string(self):
        # git stamps commits with a local offset; everything else is UTC.
        path = Path(state.HOME / "state" / "test-timeline.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [
            # 12:05+03:00 is 09:05 UTC — earlier than 09:20 UTC, though a string
            # comparison says the opposite.
            a_record(at="2026-08-06T12:05:00+03:00", kind="commit", label="коммит 09:05 UTC",
                     observed_by="git log", station="commit", channel="git"),
            a_record(at="2026-08-06T09:20:00+00:00", label="артефакт 09:20 UTC"),
        ]))
        try:
            order = [r["label"] for r in render.load_timeline(path)]
        finally:
            path.unlink()
        self.assertEqual(order, ["коммит 09:05 UTC", "артефакт 09:20 UTC"])


class PrivacyNegativeControl(unittest.TestCase):
    """The bypasses the previous suite walked past (finding HIGH-1 of review 780)."""

    SECRET = SECRET

    def test_a_secret_inside_a_kept_title_does_not_survive(self):
        # The old scrub excluded titles from cleaning altogether, so a real chat
        # identifier went out inside a real task name, in a file stamped
        # «ОБЕЗЛИЧЕНО». The title keeps its meaning; its content gets cleaned.
        clean = schema.scrub({"title": "Identify Max telegram_user_100200300",
                             "task_title": "Отчёт в /opt/projects/example-product на owner@example.com"})
        self.assertNotIn("100200300", clean["title"])
        self.assertIn("Identify Max", clean["title"])
        self.assertNotIn("/opt/projects", clean["task_title"])
        self.assertNotIn("@gmail.com", clean["task_title"])
        self.assertIn("Отчёт", clean["task_title"])

    def test_an_integer_identifier_does_not_survive(self):
        # `scrub` used to return every int untouched, which is how 298 PIDs
        # reached a public artifact: a regex never sees a number that is not text.
        clean = schema.scrub({"run": {"pid": 2065651, "alive": True}, "task_count": 73})
        self.assertIsNone(clean["run"]["pid"])
        self.assertTrue(clean["run"]["alive"])
        # A count is a measurement, not an identifier, and has to stay.
        self.assertEqual(clean["task_count"], 73)

    def test_the_written_timeline_of_an_anonymous_scribe_carries_no_secret(self):
        # End to end through the scribe, with the secret in the task title —
        # which is what the review's independent probe did, and what survived.
        secret = "telegram_user_100200300 /opt/projects/private owner@example.com"
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "001-task"
            task_dir.mkdir()
            (task_dir / "task.md").write_text(f'---\nstatus: "planned"\ntitle: "{secret}"\n---\n')
            (task_dir / "analysis.md").write_text("анализ")
            out, cursor = Path(tmp) / "t.jsonl", Path(tmp) / "c.json"
            scribe = recorder.Scribe(out, cursor, anonymize=True)
            scribe.seeding = False
            scribe.observe_task(task_dir)
            written = out.read_text()
        self.assertTrue(written.strip(), "писец ничего не записал")
        for forbidden in ("100200300", "/opt/projects", "owner@example.com"):
            self.assertNotIn(forbidden, written, forbidden)
        # The record is still a record: the observation survives the cleaning.
        for record in (json.loads(line) for line in written.splitlines()):
            schema.validate_record(record)
            self.assertTrue(record["task_title"])

    def test_showing_anonymously_cleans_a_timeline_written_without_the_flag(self):
        # `--serve --anonymize` used to trust that the file on disk had been
        # written safely, so a scribe started without the flag leaked through a
        # server started with it (finding HIGH-3 of review 786).
        secret = "telegram_user_100200300 в /opt/projects/private, owner@example.com"
        with tempfile.TemporaryDirectory() as tmp:
            timeline = Path(tmp) / "timeline.jsonl"
            timeline.write_text(json.dumps(a_record(task_title=secret, label=secret,
                                                    detail=secret), ensure_ascii=False))
            snapshot = Path(tmp) / "snapshot.json"
            snapshot.write_text(json.dumps(
                a_snapshot([a_task(title=secret)]), ensure_ascii=False))
            dirty = render.payload(timeline, snapshot)
            clean = render.payload(timeline, snapshot, anonymize=True)
        self.assertIn("100200300", json.dumps(dirty, ensure_ascii=False))
        blob = json.dumps(clean, ensure_ascii=False)
        for forbidden in ("100200300", "/opt/projects", "owner@example.com"):
            self.assertNotIn(forbidden, blob, forbidden)

    def test_cleaning_an_already_clean_document_changes_nothing(self):
        # Live mode cleans on the way to the screen even when the scribe already
        # cleaned on the way to disk, so the second pass has to be a no-op.
        once = schema.scrub(a_record(task_title=self.SECRET, detail=self.SECRET))
        self.assertEqual(schema.scrub(once), once)

    def test_a_built_anonymised_document_carries_no_pid_and_no_long_number(self):
        snapshot = a_snapshot([a_task(title="Задача telegram_user_100200300",
                                      run={"state": "running", "runner": "codex",
                                           "workflow": "dev-pipeline", "sandbox": None,
                                           "stop_reason": None, "exit_code": None,
                                           "pid": 2065651, "alive": True,
                                           "alive_src": "pid и стартовый тик ядра",
                                           "progress": None})])
        blob = json.dumps(schema.scrub(snapshot), ensure_ascii=False)
        self.assertNotIn("2065651", blob)
        self.assertNotIn("100200300", blob)


class ObservedAttribution(unittest.TestCase):
    """Nobody is named unless something on disk named them (MEDIUM-3)."""

    def test_a_named_actor_without_a_source_is_refused_in_a_record(self):
        with self.assertRaises(schema.ContractError):
            schema.validate_record(a_record(actor="исполнитель"))
        schema.validate_record(a_record(actor="codex", actor_src="поле runtime события"))

    def test_a_named_actor_without_a_source_is_refused_on_a_plate(self):
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(a_snapshot([a_task(board={"actor": "codex"})]))
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(a_snapshot([a_task(board={"role": "review"})]))

    def test_an_artifact_record_no_longer_claims_an_author(self):
        # The presence of a file says a file exists. The scribe used to sign every
        # one of them «исполнитель», which is a guess wearing an observation's
        # clothes — and on a plate it becomes a caption that lies.
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "001-task"
            (task_dir).mkdir()
            (task_dir / "task.md").write_text('---\nstatus: "planned"\ntitle: "Задача"\n---\n')
            (task_dir / "analysis.md").write_text("анализ")
            out, cursor = Path(tmp) / "t.jsonl", Path(tmp) / "c.json"
            scribe = recorder.Scribe(out, cursor, anonymize=False)
            scribe.seeding = False
            scribe.observe_task(task_dir)
            records = [json.loads(line) for line in out.read_text().splitlines()]
        artifacts = [r for r in records if r["kind"] == "artifact"]
        self.assertTrue(artifacts)
        for record in artifacts:
            self.assertNotIn("actor", record)
            self.assertTrue(record["observed_by"])

    def test_a_legacy_record_keeps_its_observation_and_loses_the_guess(self):
        # Records written before the fix carry `actor: исполнитель` with nothing
        # behind it. Throwing the record away would lose a real observation.
        cleaned = render.without_unsourced_actor(a_record(actor="исполнитель"))
        self.assertNotIn("actor", cleaned)
        schema.validate_record(cleaned)

    def test_a_plate_with_no_observed_executor_shows_no_executor(self):
        board = render.build_board(a_snapshot([a_task()]))
        # By key, never by position: the areas are a contract that grew twice
        # already, and an index silently pointed this test at another area.
        area = next(a for a in board["panels"][0]["areas"] if a["key"] == "queued")
        plate = area["plates"][0]
        self.assertIsNone(plate["actor"])
        self.assertIsNone(plate["role"])


class JournalCursor(unittest.TestCase):
    """Reading a journal from a byte offset, and surviving truncation (MEDIUM-2)."""

    def write(self, path: Path, count: int, start: int = 0) -> None:
        with path.open("a") as handle:
            for n in range(start, start + count):
                handle.write(json.dumps({"sequence": n, "kind": "event"}) + "\n")

    def test_only_what_was_appended_is_read_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            self.write(path, 100)
            first, cursor = state.tail_lines(path, None)
            self.assertEqual(len(first), 100)
            again, cursor = state.tail_lines(path, cursor)
            self.assertEqual(again, [])
            self.write(path, 3, start=100)
            fresh, cursor = state.tail_lines(path, cursor)
            self.assertEqual(len(fresh), 3)
            self.assertEqual(json.loads(fresh[0])["sequence"], 100)

    def test_the_cost_of_a_look_does_not_grow_with_the_journal(self):
        # The property the contour actually needs: watching must not get more
        # expensive as more work accumulates. Bytes read is the honest measure —
        # a wall clock on a shared machine is not.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            self.write(path, 50_000)
            _, cursor = state.tail_lines(path, None)
            size_before = path.stat().st_size
            self.write(path, 1, start=50_000)
            lines, cursor = state.tail_lines(path, cursor)
            self.assertEqual(len(lines), 1)
            # Read from the offset, so the 50 000 records already seen cost zero.
            self.assertGreater(size_before, 1_000_000)
            self.assertLess(cursor["offset"] - size_before, 200)

    def test_a_truncated_journal_is_read_from_the_start_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            self.write(path, 50)
            _, cursor = state.tail_lines(path, cursor=None)
            path.write_text("")
            self.write(path, 4)
            lines, _ = state.tail_lines(path, cursor)
            self.assertEqual(len(lines), 4)

    def test_a_rotated_journal_is_recognised_by_its_inode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            self.write(path, 60)
            _, cursor = state.tail_lines(path, None)
            path.rename(Path(tmp) / "events.jsonl.1")
            self.write(path, 60)          # a new file at the same name and size
            lines, _ = state.tail_lines(path, cursor)
            self.assertEqual(len(lines), 60)

    def test_a_half_written_record_waits_for_the_next_look(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(json.dumps({"sequence": 0}) + "\n" + '{"sequence": 1, "ki')
            lines, cursor = state.tail_lines(path, None)
            self.assertEqual(len(lines), 1)
            with path.open("a") as handle:
                handle.write('nd": "event"}\n')
            rest, _ = state.tail_lines(path, cursor)
            self.assertEqual(json.loads(rest[0])["kind"], "event")


class LiveUpdate(unittest.TestCase):
    """A change that appends nothing to the timeline still reaches the screen (MEDIUM-1)."""

    def digest(self, snapshot, timeline=()):
        return render.digest_of({"snapshot": snapshot, "timeline": list(timeline),
                                 "board": render.build_board(snapshot)})

    def test_a_status_change_with_an_unchanged_timeline_changes_the_digest(self):
        before = self.digest(a_snapshot([a_task(status="planned")]))
        after = self.digest(a_snapshot([a_task(status="blocked")]))
        self.assertNotEqual(before, after)

    def test_a_run_that_died_changes_the_digest(self):
        alive = a_task(flags=["live"], board={"area": "running"},
                       run={"state": "running", "runner": "codex", "workflow": "dev-pipeline",
                            "sandbox": None, "stop_reason": None, "exit_code": None,
                            "pid": None, "alive": True,
                            "alive_src": "pid и стартовый тик ядра", "progress": None})
        dead = a_task(flags=["stale_label"], board={"area": "stuck"},
                      run={**alive["run"], "alive": False})
        self.assertNotEqual(self.digest(a_snapshot([alive])), self.digest(a_snapshot([dead])))

    def test_an_unpushed_commit_appearing_changes_the_digest(self):
        snapshot = a_snapshot()
        before = self.digest(snapshot)
        snapshot["threads"][0]["repos"][0]["unpushed"] = 5
        self.assertNotEqual(before, self.digest(snapshot))

    def test_the_same_state_gives_the_same_digest(self):
        self.assertEqual(self.digest(a_snapshot()), self.digest(a_snapshot()))

    def test_time_merely_passing_does_not_change_the_digest(self):
        # An age ticks every second whether or not anything happened. If it
        # counted, live mode would reload every ten seconds and say nothing.
        still = a_snapshot([a_task(board={"age_seconds": 60,
                                          "since": "2026-08-06T12:00:00+00:00"})])
        later = a_snapshot([a_task(board={"age_seconds": 3600,
                                          "since": "2026-08-06T12:00:00+00:00"})])
        self.assertEqual(self.digest(still), self.digest(later))

    def test_a_state_that_changed_at_a_new_instant_still_counts(self):
        # The instant behind the age is in the digest, so a real transition is
        # never hidden by dropping the derived number.
        before = a_snapshot([a_task(board={"since": "2026-08-06T12:00:00+00:00"})])
        after = a_snapshot([a_task(board={"since": "2026-08-06T12:30:00+00:00"})])
        self.assertNotEqual(self.digest(before), self.digest(after))

    def test_the_page_reloads_on_the_digest_not_on_the_timeline_length(self):
        html = render.render({"snapshot": a_snapshot(), "timeline": [],
                              "board": render.build_board(a_snapshot()),
                              "built_at": "2026-08-06T12:00:00+00:00",
                              "live_url": None, "digest": "x"})
        self.assertIn("fresh.digest !== DATA.digest", html)
        self.assertNotIn("fresh.timeline.length !== RECORDS.length", html)


class RendererBoundary(unittest.TestCase):
    """The renderer cannot reach the disk, by import graph rather than by habit (HIGH-2)."""

    def test_the_renderer_does_not_import_the_collector(self):
        source = (Path(render.__file__)).read_text()
        self.assertNotIn("import process_map_state", source)
        self.assertFalse(hasattr(render, "state"))

    def test_the_renderer_refuses_to_run_without_a_snapshot(self):
        out = subprocess.run([sys.executable, render.__file__, "--out", "/dev/null"],
                             capture_output=True, text=True, cwd=Path(render.__file__).parent)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("--snapshot", out.stderr)

    def test_the_adapter_is_the_one_component_that_sees_both_sides(self):
        source = (Path(__file__).parent / "process_map_serve.py").read_text()
        self.assertIn("import process_map_render", source)
        self.assertIn("import process_map_state", source)


class BoardLayout(unittest.TestCase):
    def test_the_areas_stand_in_the_order_of_urgency(self):
        board = render.build_board(a_snapshot())
        order = [area["key"] for area in board["panels"][0]["areas"]]
        self.assertEqual(order, ["waiting_human", "running", "stuck", "decision_unmet",
                                 "undelivered", "product_owner", "ready_to_start",
                                 "pickup", "queued", "backlog", "plan", "done"])

    def test_what_can_be_picked_up_stands_above_what_is_held(self):
        """The first question of a wake-up outranks the reference list below it.

        «В очереди» used to be one area and answered neither «что можно
        подхватить прямо сейчас» nor «за чем стоит остальное». Order is urgency
        here, so the startable work stands above the held work, and both above
        the promises nobody has made a task of yet.
        """
        order = list(schema.BOARD_AREAS)
        self.assertLess(order.index("pickup"), order.index("queued"))
        # А ниже очереди — то, что само не поедет: бэклог не работа на выбор, и
        # предлагать его между «подхватить» и «в очереди» значило бы звать
        # запускать остановленное.
        self.assertLess(order.index("queued"), order.index("backlog"))
        self.assertLess(order.index("backlog"), order.index("plan"))
        self.assertLess(order.index("plan"), order.index("done"))

    def test_the_area_waiting_for_a_person_is_present_even_when_empty(self):
        # It is the whole answer to one of the three acceptance questions: an
        # empty area answers it, a missing one does not.
        snapshot = a_snapshot([a_task(board={"area": "done"})])
        snapshot["products"][0]["questions"] = []
        board = render.build_board(snapshot)
        waiting = board["panels"][0]["areas"][0]
        self.assertEqual(waiting["key"], "waiting_human")
        self.assertEqual(waiting["count"], 0)
        self.assertFalse(waiting["collapsed"])

    def test_done_and_queued_arrive_folded(self):
        # Бэклог остаётся раскрытым: он один из четырёх вопросов, названных
        # пользователем прямо, и в нём стоит только то, что держит сам план.
        board = render.build_board(a_snapshot())
        folded = {a["key"] for a in board["panels"][0]["areas"] if a["collapsed"]}
        self.assertEqual(folded, {"queued", "done"})

    def test_what_the_cap_dropped_is_counted_out_loud(self):
        many = [a_task(id=n, dir=f"{n:03d}-t", board={"area": "done"})
                for n in range(1, render.PER_BOARD_AREA + 12)]
        area = render.build_board(a_snapshot(many))["panels"][0]["areas"][-1]
        self.assertEqual(len(area["plates"]), render.PER_BOARD_AREA)
        self.assertEqual(area["hidden"], 11)
        self.assertEqual(area["count"], render.PER_BOARD_AREA + 11)

    def test_the_oldest_plate_of_an_area_is_shown_first(self):
        young = a_task(id=1, dir="001-t", board={"area": "stuck", "age_seconds": 60,
                                                 "since": "2026-08-06T12:00:00+00:00"})
        old = a_task(id=2, dir="002-t", board={"area": "stuck", "age_seconds": 200000,
                                               "since": "2026-08-04T04:26:40+00:00"})
        area = render.build_board(a_snapshot([young, old]))["panels"][0]["areas"][2]
        self.assertEqual([p["id"] for p in area["plates"]], [2, 1])
        self.assertEqual(area["oldest"], 200000)

    def test_plates_a_fraction_of_a_second_apart_do_not_swap_places(self):
        # Sorting on the rounded age made three plates trade places between two
        # collections, which live mode then read as a change and reloaded on.
        near = [a_task(id=n, dir=f"{n:03d}-t",
                       board={"area": "queued", "age_seconds": 900,
                              "since": f"2026-07-28T15:55:3{n}.2{n}0000+00:00"})
                for n in (1, 2, 3)]
        area_of = lambda board: next(a for a in board["panels"][0]["areas"]
                                     if a["key"] == "queued")
        first = area_of(render.build_board(a_snapshot(near)))
        # The same tasks, collected again with the ages rounded differently.
        for task in near:
            task["board"]["age_seconds"] += 1
        second = area_of(render.build_board(a_snapshot(near)))
        self.assertEqual([p["id"] for p in first["plates"]], [p["id"] for p in second["plates"]])

    def test_a_live_process_and_a_lying_label_are_two_fields_on_the_plate(self):
        task = a_task(flags=["stale_label"], board={"area": "stuck"},
                      run={"state": "running", "runner": "codex", "workflow": "dev-pipeline",
                           "sandbox": None, "stop_reason": None, "exit_code": None,
                           "pid": None, "alive": False, "progress": None})
        plate = render.build_board(a_snapshot([task]))["panels"][0]["areas"][2]["plates"][0]
        self.assertFalse(plate["alive"])
        self.assertTrue(plate["stale_label"])

    def test_the_areas_a_collector_can_produce_are_the_areas_the_board_draws(self):
        drawn = {area["key"] for area in render.build_board(a_snapshot())["panels"][0]["areas"]}
        self.assertEqual(drawn, set(schema.BOARD_AREAS))
        for status, flags, questions in (("completed", [], False), (None, ["live"], False),
                                         ("blocked", [], False), ("planned", [], True),
                                         ("planned", ["gap"], False), ("planned", [], False)):
            self.assertIn(state.board_area(status, flags, questions), schema.BOARD_AREAS)
        self.assertIn(state.board_area("planned", [], False, None, ours=True), schema.BOARD_AREAS)
        self.assertIn(state.board_area("completed", [], False, None, undelivered=True),
                      schema.BOARD_AREAS)


class BoardArea(unittest.TestCase):
    def test_a_person_owing_an_answer_comes_before_everything_else(self):
        self.assertEqual(state.board_area("planned", ["live"], True), "waiting_human")

    def test_blocked_is_a_jam_and_not_a_person_owing_an_answer(self):
        # It used to be a silent synonym for «ждёт решения человека», which put
        # technical blockages under the one heading a person is meant to act on
        # (finding HIGH-1 of review 786). Work that stands is a jam; the reason
        # stands next to it.
        self.assertEqual(state.board_area("blocked", ["blocked"], False), "stuck")

    def test_a_dead_run_under_a_living_label_is_a_jam(self):
        self.assertEqual(state.board_area("planned", ["stale_label"], False), "stuck")
        self.assertEqual(state.board_area("planned", ["killed"], False), "stuck")

    def test_a_finished_task_is_done_whatever_its_flags(self):
        self.assertEqual(state.board_area("completed", ["gap"], False), "done")

    def test_nothing_holding_it_means_it_can_be_picked_up(self):
        # This used to answer «в очереди», which is what the single queue area
        # said about every idle task and is why «что можно подхватить прямо
        # сейчас» had no answer at all. Nothing observably holding a task is the
        # observation that it can be started now.
        self.assertEqual(state.board_area("planned", ["idle"], False, None), "pickup")

    def test_a_named_holder_puts_it_in_the_queue_behind_that_holder(self):
        self.assertEqual(
            state.board_area("planned", ["idle"], False,
                             "репозиторий example-product занят живым прогоном задачи 783"),
            "queued")


class WaitingForAPerson(unittest.TestCase):
    """The six plates review 786 named, and the questions the board never showed."""

    # Verbatim from the tasks the review inspected. Each one used to be counted
    # as work waiting on a person, and none of them is (finding HIGH-1).
    NOT_WAITING = {
        "760/727 — блокировка без вопроса": [],
        "747 — ремонт вынесен в другую задачу": [],
        "779 — прямо сказано, что выбора нет": [
            "Задача поставлена по прямому письму пользователя; выбора, ожидающего "
            "его ответа, в ней нет."],
        "722 — инструкция к трактовке будущего результата": [
            "Если после честного расчёта окажется, что часть пар не имеет достаточного "
            "общего минутного покрытия — это результат («пара неизмерима на этих данных»), "
            "а не ошибка. Такие случаи выделить отдельно, а не прятать в общий итог."],
        "723 — вопрос и ответ на него в одном пункте": [
            "Если выбран путь «read-only с записью в свой каталог» — считается ли такой "
            "прогон по-прежнему read-only для целей правила кросс-ревью? Продуктовый "
            "ответ: да, потому что ограничение защищает предмет ревью, а не блокнот "
            "ревьювера."],
    }

    STILL_WAITING = [
        "Публиковать ли перенос коммитов на уровень продукта?",
        "Кто четыре внешних пользователя Max и что им нужно?",
    ]

    def test_none_of_the_six_named_tasks_waits_for_a_person(self):
        for label, bullets in self.NOT_WAITING.items():
            with self.subTest(label):
                self.assertEqual(state.unsettled_questions(bullets), [])

    def test_a_task_blocked_without_a_question_is_not_waiting_for_a_person(self):
        # 760, 727 and 747 carry `- none` under the heading and reached the area
        # purely through their status.
        self.assertEqual(state.unsettled_questions([]), [])
        self.assertEqual(state.board_area("blocked", ["blocked"], False), "stuck")

    def test_an_open_question_is_still_recognised_as_a_question(self):
        # The other half of the old acceptance criterion: a real question may not
        # disappear while the false positives go. Whose it is, is a second
        # question and has its own tests below.
        self.assertEqual(state.unsettled_questions(self.STILL_WAITING), self.STILL_WAITING)

    def test_a_struck_through_question_is_settled(self):
        self.assertEqual(state.unsettled_questions(["~~Перезапускать ли 035?~~ Закрыт 2026-08-04."]), [])

    def test_the_products_own_questions_are_in_the_same_count_and_the_same_area(self):
        # Seventeen canonical product questions sat in the same payload and were
        # not part of the board's answer at all (finding HIGH-1).
        board = render.build_board(a_snapshot([a_task()]))
        area = board["panels"][0]["areas"][0]
        self.assertEqual(area["key"], "waiting_human")
        self.assertEqual([q["text"] for q in area["questions"]], ["Публиковать ли перенос?"])
        self.assertEqual(area["count"], 1)
        self.assertEqual(board["waiting"], 1)
        self.assertEqual(board["waiting_questions"], 1)
        self.assertEqual(board["waiting_tasks"], 0)

    def test_the_headline_number_is_the_sum_of_its_two_named_parts(self):
        waiting = a_task(id=9, dir="009-t",
                         questions=[asked_question(text="Брать все девятнадцать?")],
                         board={"area": "waiting_human"})
        board = render.build_board(a_snapshot([waiting]))
        self.assertEqual(board["waiting"], board["waiting_tasks"] + board["waiting_questions"])
        self.assertEqual(board["waiting"], 2)

    def test_a_question_of_a_product_no_direction_owns_is_not_invented_onto_a_panel(self):
        snapshot = a_snapshot()
        snapshot["products"].append({"slug": "unowned",
                                     "questions": [asked_question(text="Ничей вопрос?")],
                                     "own_questions": [], "effect": []})
        board = render.build_board(snapshot)
        shown = [q["text"] for a in board["panels"][0]["areas"] for q in a["questions"]]
        self.assertNotIn("Ничей вопрос?", shown)


class WaitingMeansWaitingOnTheUser(unittest.TestCase):
    """Part 1 of task 817: the area is bounded by what was asked of the user.

    The user counted it by hand on the live state of 2026-08-06: sixteen entries
    stood under «ждёт решения человека», and three of them were his. The rest
    were our own product decisions, questions to an executor about the Max
    environment, a repair shipped in `e12e511`, and questions he had already
    answered in writing — while the contour's own letters told him nothing was
    required of him. These tests hold the two observations that now bound it.
    """

    NO_MAILBOX = {"threads": {}, "replies": {}, "sent_known": False}

    # Verbatim from the thirteen the product owner took apart, one per reason.
    OURS = [
        "Какие из находок 706 (AR-001..AR-004) по гейтам и ревью превращаем в работу.",
        "`repo-health` сканирует старые каталоги задач и валит гейт на снапшотах "
        "255/586/587/683. Рекомендация продакта: сузить область гейта. Решение за пользователем.",
        "Миграция с остановкой приемлема?",
        "Сеть у ребёнка включается всем прогонам подряд или только тем, кому она нужна?",
    ]

    def test_a_question_nobody_put_to_the_user_is_ours(self):
        for text in self.OURS:
            with self.subTest(text[:40]):
                entry = state.question_entry(text, self.NO_MAILBOX)
                self.assertEqual(entry["owner"], "product")
                self.assertIsNone(entry["asked_at"])

    def test_the_recommendation_addressed_to_the_user_is_still_ours(self):
        """«Решение за пользователем» is a sentence, not an observation.

        This is the exact shape that filled the area: a product owner writing
        that the decision is the user's does not make the user aware of it. Only
        a message that went out does, and that is what the mark records.
        """
        entry = state.question_entry(
            "Сузить область гейта или чистить артефакты? Решение за пользователем.",
            self.NO_MAILBOX)
        self.assertEqual(entry["owner"], "product")

    def test_a_question_marked_as_asked_belongs_to_the_user(self):
        entry = state.question_entry(
            "Сколько ставок брать в первый заход? Спрошено у пользователя 2026-08-05, "
            "письмо `18f0000000000001`.", self.NO_MAILBOX)
        self.assertEqual(entry["owner"], "user")
        self.assertEqual(entry["asked_at"], "2026-08-05")
        self.assertEqual(entry["channel"], "email")
        self.assertEqual(entry["ref"], "18f0000000000001")
        self.assertIn("18f0000000000001", entry["asked_src"])

    def test_a_telegram_question_carries_its_message_too(self):
        entry = state.question_entry(
            "Пересобирать ли портфель? Спрошено у пользователя 2026-08-06, Telegram 18479.",
            self.NO_MAILBOX)
        self.assertEqual((entry["owner"], entry["channel"], entry["ref"]),
                         ("user", "telegram", "18479"))

    def test_an_answer_in_the_same_thread_takes_the_question_out_by_itself(self):
        """The user's own words: an answered question must leave on its own.

        «Вопрос, на который пользователь ответил, обязан исчезать из области сам,
        по наблюдаемому признаку ответа, а не потому, что кто-то вспомнил
        вычеркнуть строку.» The observation is a letter of his in the same thread,
        dated no earlier than the question.
        """
        mail = {"threads": {"18f0000000000001": "продакт продукт"},
                "replies": {"продакт продукт":
                            state.datetime(2026, 8, 6, 8, 31, tzinfo=state.timezone.utc)},
                "sent_known": True}
        entry = state.question_entry(
            "Сколько ставок брать? Спрошено у пользователя 2026-08-05, письмо 18f0000000000001.",
            mail)
        self.assertEqual(entry["owner"], "product")
        self.assertIn("в том же треде", entry["answer_src"])
        self.assertIn("незаписанным", entry["note"])

    def test_a_letter_older_than_the_question_is_not_an_answer_to_it(self):
        mail = {"threads": {"18f0000000000001": "продакт продукт"},
                "replies": {"продакт продукт":
                            state.datetime(2026, 8, 1, 8, 31, tzinfo=state.timezone.utc)},
                "sent_known": True}
        entry = state.question_entry(
            "Сколько ставок брать? Спрошено у пользователя 2026-08-05, письмо 18f0000000000001.",
            mail)
        self.assertEqual(entry["owner"], "user")

    def test_a_letter_earlier_the_same_day_is_not_an_answer_to_a_later_question(self):
        """The exact live case of finding HIGH-1 of review 826.

        The three questions went out in `18f0000000000003` at 2026-08-06
        16:28:48 UTC. The letter counted as their answer, `18f0000000000002`,
        was sent at 14:00:02 UTC — 2 hours 28 minutes *earlier*, and it asked
        about a forgotten document. A day is not fine enough to tell those
        apart; the send instant is.
        """
        mail = {"threads": {"18f0000000000003": "продакт продукт"},
                "replies": {"продакт продукт":
                            state.datetime(2026, 8, 6, 14, 0, 2, tzinfo=state.timezone.utc)},
                "sent_at": {"18f0000000000003":
                            state.datetime(2026, 8, 6, 16, 28, 48, tzinfo=state.timezone.utc)},
                "sent_known": True}
        entry = state.question_entry(
            "Сколько ставок брать? Спрошено у пользователя 2026-08-06, письмо 18f0000000000003.",
            mail)
        self.assertEqual(entry["owner"], "user")
        self.assertIsNone(entry["answer_src"])
        self.assertIn("не позже самого вопроса", entry["note"])
        self.assertIn("2026-08-06 16:28 UTC", entry["note"])

    def test_a_letter_later_the_same_day_is_the_answer(self):
        # The positive control of the same pair: same day, same thread, but
        # afterwards — that one really does take the question out of the area.
        mail = {"threads": {"18f0000000000003": "продакт продукт"},
                "replies": {"продакт продукт":
                            state.datetime(2026, 8, 6, 18, 5, tzinfo=state.timezone.utc)},
                "sent_at": {"18f0000000000003":
                            state.datetime(2026, 8, 6, 16, 28, 48, tzinfo=state.timezone.utc)},
                "sent_known": True}
        entry = state.question_entry(
            "Сколько ставок брать? Спрошено у пользователя 2026-08-06, письмо 18f0000000000003.",
            mail)
        self.assertEqual(entry["owner"], "product")
        self.assertIn("позже вопроса", entry["answer_src"])

    def test_a_letter_at_the_very_instant_of_the_question_is_not_an_answer(self):
        # Equal is not later. An answer written before it could be read is not
        # an answer, and the boundary is where a day-wide rule used to swallow it.
        instant = state.datetime(2026, 8, 6, 16, 28, 48, tzinfo=state.timezone.utc)
        mail = {"threads": {"18f0000000000003": "продакт продукт"},
                "replies": {"продакт продукт": instant},
                "sent_at": {"18f0000000000003": instant},
                "sent_known": True}
        entry = state.question_entry(
            "Сколько ставок брать? Спрошено у пользователя 2026-08-06, письмо 18f0000000000003.",
            mail)
        self.assertEqual(entry["owner"], "user")

    def test_a_question_asked_in_reply_to_the_users_morning_letter_stays_his(self):
        """The ordinary shape of the defect, read from files rather than a dict.

        The user writes in the morning; the product owner answers that letter
        and asks something new in the same thread that evening. The newest
        incoming letter of the thread is then the user's morning one — older
        than the question it would be counted against. This is how the board
        would hide a question the user has never seen an obligation for.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._store(root / "inbox" / "18f0000000000002", {
                "message_id": "18f0000000000002",
                "subject": "Продакт: Продукт",
                "from": "user@example.com", "to": "owner@example.com",
                "date": "Thu, 06 Aug 2026 17:00:02 +0300", "attachments": []})
            self._store(root / "sent" / "18f0000000000003", {
                "message_id": "18f0000000000003",
                "subject": "Re: Продакт: Продукт",
                "from": "owner@example.com", "to": "user@example.com",
                "date": "Thu, 06 Aug 2026 12:28:48 -0400", "attachments": []})
            original = (state.MAIL_INBOX, state.MAIL_SENT)
            state.MAIL_INBOX, state.MAIL_SENT = root / "inbox", root / "sent"
            try:
                mail = state.mailbox()
                entry = state.question_entry(
                    "Сколько ставок брать? Спрошено у пользователя 2026-08-06, "
                    "письмо 18f0000000000003.", mail)
            finally:
                state.MAIL_INBOX, state.MAIL_SENT = original

        # The reply prefix must not split the thread, or the two letters would
        # never be compared at all and the question would stay the user's for
        # the wrong reason.
        self.assertEqual(mail["threads"]["18f0000000000003"],
                         mail["threads"]["18f0000000000002"])
        self.assertEqual(mail["sent_at"]["18f0000000000003"],
                         state.datetime(2026, 8, 6, 16, 28, 48, tzinfo=state.timezone.utc))
        self.assertEqual(entry["owner"], "user")
        self.assertIsNone(entry["answer_src"])

    def test_without_a_stored_outgoing_letter_the_marked_day_still_decides(self):
        # The fallback is unchanged behaviour, not a new rule: with no letter on
        # disk the mark carries a date and nothing finer, and the board says so.
        mail = {"threads": {"18f0000000000001": "продакт продукт"},
                "replies": {"продакт продукт":
                            state.datetime(2026, 8, 6, 8, 31, tzinfo=state.timezone.utc)},
                "sent_at": {},
                "sent_known": True}
        entry = state.question_entry(
            "Сколько ставок брать? Спрошено у пользователя 2026-08-05, письмо 18f0000000000001.",
            mail)
        self.assertEqual(entry["owner"], "product")
        self.assertIn("не раньше вопроса", entry["answer_src"])

    def test_an_unresolvable_thread_says_so_instead_of_claiming_silence(self):
        entry = state.question_entry(
            "Спрошено у пользователя 2026-08-05, письмо 18f0000000000001. Так что решаем?",
            self.NO_MAILBOX)
        self.assertEqual(entry["owner"], "user")
        self.assertIn("не найдено в почтовом хранилище", entry["note"])

    def test_a_stored_outgoing_letter_makes_its_thread_readable_from_disk(self):
        """The seam with the mail client, read as files rather than as intent.

        `mailbox()` has always been written to read `sent`, but nothing wrote
        that directory, so every question asked by mail stayed in the user's area
        with «письмо не найдено в почтовом хранилище» — including two he had
        already answered. This holds the file shape the sender now writes:
        `sent/<id>/metadata.json` with the identifier, the subject that names the
        thread and the date, and no body.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._store(root / "sent" / "18f0000000000003", {
                "message_id": "18f0000000000003",
                "subject": "Продакт: Продукт",
                "from": "owner@example.com", "to": "user@example.com",
                "date": "Thu, 06 Aug 2026 09:12:00 +0300", "attachments": []})
            self._store(root / "inbox" / "18f0000000000004", {
                "message_id": "18f0000000000004",
                "subject": "Re: Продакт: Продукт",
                "from": "user@example.com", "to": "owner@example.com",
                "date": "Thu, 06 Aug 2026 10:40:00 +0300", "attachments": []})
            original = (state.MAIL_INBOX, state.MAIL_SENT)
            state.MAIL_INBOX, state.MAIL_SENT = root / "inbox", root / "sent"
            try:
                mail = state.mailbox()
                entry = state.question_entry(
                    "Сколько ставок брать? Спрошено у пользователя 2026-08-06, "
                    "письмо 18f0000000000003.", mail)
            finally:
                state.MAIL_INBOX, state.MAIL_SENT = original

        self.assertTrue(mail["sent_known"])
        self.assertEqual(mail["threads"]["18f0000000000003"],
                         state.thread_key("Продакт: Продукт"))
        self.assertEqual(entry["owner"], "product")
        self.assertIn("в том же треде", entry["answer_src"])

    @staticmethod
    def _store(directory: Path, metadata: dict) -> None:
        directory.mkdir(parents=True)
        (directory / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def test_a_thread_is_one_thread_whatever_prefix_a_client_added(self):
        self.assertEqual(state.thread_key("Re: Продакт: Продукт"),
                         state.thread_key("Продакт: Продукт"))
        self.assertEqual(state.thread_key("Fwd: Re: Сводка"), state.thread_key("Сводка"))

    def test_our_question_puts_the_task_in_its_own_area_and_not_in_the_user_s(self):
        self.assertEqual(state.board_area("planned", [], False, None, ours=True),
                         "product_owner")
        self.assertEqual(state.board_area("planned", [], True, None, ours=True),
                         "waiting_human")

    def test_our_question_does_not_outrank_a_live_run_or_a_jam(self):
        self.assertEqual(state.board_area("planned", ["live"], False, None, ours=True),
                         "running")
        self.assertEqual(state.board_area("blocked", ["blocked"], False, None, ours=True),
                         "stuck")

    def test_nothing_is_hidden_by_the_split(self):
        """Everything that left the user's area stands in ours, counted there."""
        ours = a_task(id=5, dir="005-t", board={"area": "product_owner"},
                      questions=[a_question(text="Какие находки 706 берём в работу?")])
        snapshot = a_snapshot([ours])
        snapshot["products"][0]["questions"] = []
        snapshot["products"][0]["own_questions"] = [a_question(text="Сужать ли гейт repo-health?")]
        board = render.build_board(snapshot)
        area = next(a for a in board["panels"][0]["areas"] if a["key"] == "product_owner")
        self.assertEqual(area["count"], 2)          # one plate and one product question
        self.assertEqual([q["text"] for q in area["questions"]],
                         ["Сужать ли гейт repo-health?"])
        self.assertEqual(board["waiting"], 0)
        self.assertEqual(board["ours"], 2)

    def test_a_user_question_without_the_mark_that_asked_it_is_refused(self):
        # The same rule as `actor_src`: an area that tells a person they are
        # blocking work has to say what put the question in front of them.
        with self.assertRaises(schema.ContractError):
            schema.validate_question(a_question(owner="user", asked_src=None), "вопрос")

    def test_the_split_in_the_document_cannot_drift_from_the_questions(self):
        task = a_task(questions=[asked_question()])
        task["our_questions"] = task["asked_user"]          # a reader's mistake
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(a_snapshot([task]))

    def test_the_page_prints_the_mark_next_to_the_question(self):
        page = (Path(__file__).parent / "process_map_template.html").read_text()
        self.assertIn("спрошено \" + q.asked_at", page)
        self.assertIn("Решает продакт", (Path(__file__).parent
                                         / "process_map_schema.py").read_text())


class JamReason(unittest.TestCase):
    """«Где затор» without «почему» is two thirds of an acceptance question."""

    NO_RUN = {"stop_reason": None, "alive": False, "alive_src": None}

    def test_the_frontmatter_reason_is_used_and_its_source_named(self):
        # Exactly the case of task 686: `happening` is null because no live child
        # writes progress, while the reason sits in the frontmatter next to it.
        why, src = state.jam_reason("queued_behind_active_worktree_writers_669_689",
                                    self.NO_RUN, [], [])
        self.assertEqual(why, "queued_behind_active_worktree_writers_669_689")
        self.assertIn("status_detail", src)

    def test_a_stopped_watcher_names_the_stop(self):
        why, src = state.jam_reason(None, {**self.NO_RUN, "stop_reason": "memory_limit"}, [], [])
        self.assertEqual(why, "memory_limit")
        self.assertIn("runner.json", src)

    def test_a_completion_refusal_names_the_unfinished_closing_step(self):
        run = {**self.NO_RUN, "refusal": "pipeline_refused_to_close",
               "refusal_summary": "Не завершены review и live-проверка"}
        why, src = state.jam_reason(None, run, [], [])
        self.assertEqual(why, "Не завершены review и live-проверка")
        self.assertIn("completion_refusal", src)

    def test_a_specific_status_detail_stays_above_a_general_completion_refusal(self):
        run = {**self.NO_RUN, "refusal": "pipeline_refused_to_close",
               "refusal_summary": "Не завершены review и live-проверка"}
        why, src = state.jam_reason("queued_behind_old_writer", run, [], [])
        self.assertEqual(why, "queued_behind_old_writer")
        self.assertIn("status_detail", src)

    def test_a_failed_gate_stays_above_a_general_completion_refusal(self):
        run = {**self.NO_RUN, "refusal": "pipeline_refused_to_close",
               "refusal_summary": "Rejected premature completion"}
        why, src = state.jam_reason(None, run,
                                    [{"gate": "live_surface", "result": "FAIL"}], [])
        self.assertEqual(why, "не пройдено гейтов: 1")
        self.assertIn("verification.md", src)

    def test_a_failed_gate_is_a_reason(self):
        why, src = state.jam_reason(None, self.NO_RUN,
                                    [{"gate": "live_surface", "result": "FAIL"},
                                     {"gate": "tests", "result": "OK"}], [])
        self.assertEqual(why, "не пройдено гейтов: 1")
        # Имя гейта — аппарат, и стоит оно там же, где остальной аппарат: за
        # источником. Ответ читателю — счёт, а не `f_003_closure_records`.
        self.assertNotIn("live_surface", why)
        self.assertIn("live_surface", src)
        self.assertNotIn("tests", src.split(":")[-1])
        self.assertIn("verification.md", src)

    def test_a_lying_label_is_a_reason(self):
        why, src = state.jam_reason(None, self.NO_RUN, [], ["stale_label"])
        self.assertIn("running", why)
        self.assertTrue(src)

    def test_silence_where_the_disk_says_nothing(self):
        self.assertEqual(state.jam_reason(None, self.NO_RUN, [], []), (None, None))

    def test_a_reason_without_a_source_cannot_reach_the_board(self):
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(a_snapshot([a_task(board={"why": "просто так"})]))

    def test_the_reason_reaches_the_plate_and_the_strip(self):
        stuck = a_task(id=686, dir="686-t", flags=["blocked"],
                       status_detail="queued_behind_active_worktree_writers_669_689",
                       board={"area": "stuck", "why": "queued_behind_active_worktree_writers_669_689",
                              "why_src": "поле status_detail во frontmatter task.md"})
        board = render.build_board(a_snapshot([stuck]))
        self.assertEqual(board["panels"][0]["areas"][2]["plates"][0]["why"],
                         "queued_behind_active_worktree_writers_669_689")
        self.assertEqual(board["jams"][0]["why"],
                         "queued_behind_active_worktree_writers_669_689")

    def test_the_page_prints_the_reason_rather_than_hiding_it_in_a_tooltip(self):
        html = render.render({"snapshot": a_snapshot(), "timeline": [],
                              "board": render.build_board(a_snapshot()),
                              "built_at": "2026-08-06T12:00:00+00:00",
                              "live_url": None, "digest": "d"})
        # The reason is text on the plate and text on the strip. It runs through
        # `human()` on the way — `decision=deliver` is a reason a person has to
        # read too — and `human()` returns unknown text unchanged. Задача 1163
        # добавила складку: длинная причина стоит тремя строками и раскрывается
        # кнопкой рядом — но всё по-прежнему текстом на экране, а не подсказкой.
        # Английская причина заменяется русской фразой, а сама уезжает за
        # переключатель источников — тоже текстом, а не подсказкой.
        self.assertIn('foldable(node, "why", (holds ? "стоит за: " : "почему: ")'
                      ' + reason.shown);', html)
        self.assertIn('if (why) foldable(s, p.why ? "why" : "doing", " — " + reason.shown,',
                      html)

    def test_the_strip_shows_only_the_jams_whose_reason_fits_whole(self):
        """Finding HIGH-1 of review 789: four jams named, one reason readable.

        The page no longer cuts a reason — no ellipsis, no line clamp — so the
        strip is kept short here instead: it takes names while their reasons fit
        the budget and leaves the rest to the columns. Geometry is measured in a
        browser; what this test pins is that the cap exists, bites and is counted.
        """
        short = {"title": "к", "why": "п", "why_src": "s"}
        long = {"title": "т" * 10, "why": "п" * (render.STRIP_GROUP_CHARS - 20),
                "why_src": "s"}
        shown, hidden = render.strip_group([short, long, long], cap=4)
        self.assertEqual([len(p["title"]) for p in shown], [1, 10])
        self.assertEqual(hidden, 1)
        # Nothing named at all is not an answer to «где затор»: the first jam is
        # shown even when its own reason is longer than the whole budget.
        alone = {"title": "т", "why": "п" * (render.STRIP_GROUP_CHARS * 2), "why_src": "s"}
        shown, hidden = render.strip_group([alone, short], cap=4)
        self.assertEqual(len(shown), 1)
        self.assertEqual(hidden, 1)
        # The count of names still bounds the group before the budget does.
        shown, hidden = render.strip_group([short] * 6, cap=4)
        self.assertEqual((len(shown), hidden), (4, 2))

    def test_the_page_neither_elides_nor_clamps_a_reason_and_counts_what_it_dropped(self):
        html = render.render({"snapshot": a_snapshot(), "timeline": [],
                              "board": render.build_board(a_snapshot()),
                              "built_at": "2026-08-06T12:00:00+00:00",
                              "live_url": None, "digest": "d"})
        strip_css = html.split("#strip .item")[1].split("}")[0]
        self.assertNotIn("text-overflow", strip_css)
        self.assertNotIn("nowrap", strip_css)
        self.assertNotIn("line-clamp", html.split("#strip .item")[-1].split("}")[0])
        self.assertIn('"ещё " + hidden + " — в колонках ниже"', html)

    def test_the_board_says_how_many_names_the_strip_left_out(self):
        jams = [a_task(id=700 + n, dir=f"{700 + n}-t", flags=["blocked"],
                       status_detail="п" * 200,
                       board={"area": "stuck", "why": "п" * 200, "why_src": "s"})
                for n in range(5)]
        board = render.build_board(a_snapshot(jams))
        self.assertLess(len(board["jams"]), 5)
        self.assertEqual(board["jams_hidden"], 5 - len(board["jams"]))
        self.assertIn("now_hidden", board)


class Liveness(unittest.TestCase):
    """A run is this task's run, or it is not one (finding MEDIUM-2 of review 786)."""

    def test_the_owner_of_the_question_is_the_runner_that_recorded_the_process(self):
        # One concept, one implementation: the collector asks, it does not decide.
        self.assertIsNotNone(state.RUNNER)
        self.assertTrue(hasattr(state.RUNNER, "process_is_live"))
        # And keeps no answer of its own to put back in its place.
        self.assertFalse(hasattr(state, "pid_alive"))

    def test_this_very_process_is_live_under_its_recorded_identity(self):
        pid = os.getpid()
        runner = {"pid": pid, "process_identity": state.RUNNER.process_identity(pid)}
        alive, src = state.run_alive(runner)
        self.assertTrue(alive)
        self.assertTrue(src)

    def test_a_reused_pid_is_not_this_task_s_run(self):
        # The whole finding: the number still exists, so `os.kill(pid, 0)` said
        # «alive» and a stranger's process was drawn as the task working.
        alive, src = state.run_alive({"pid": os.getpid(),
                                      "process_identity": "identity-of-a-process-long-gone"})
        self.assertFalse(alive)
        self.assertIsNone(src)

    def test_a_recorded_pid_with_no_recorded_identity_is_not_live(self):
        # This compatibility assertion and its failure both predate the 839
        # repair: the identical assertion is in parent 5b20364, and the two
        # earlier product_owner_full_regression sections in task 839's
        # verification.md record the same sole GAP before candidate 5c6ede1.
        # Task 839 neither accepts nor changes that separate runner policy.
        self.assertEqual(state.run_alive({"pid": os.getpid()}), (False, None))
        self.assertEqual(state.run_alive({}), (False, None))

    def test_a_pid_of_another_live_namespace_is_unobservable_not_dead(self):
        # The namespace still holds processes, so nothing here may claim the run
        # ended; the plate says «ненаблюдаема» rather than «мертва».
        with mock.patch.object(state.RUNNER, "runner_pid_namespace_state",
                               return_value="foreign_live"):
            alive, src = state.run_alive({"pid": 1, "process_identity": "x",
                                          "pid_namespace": "pid:[4026500000]"})
        self.assertFalse(alive)
        self.assertIn("ненаблюдаема", src)

    def test_a_namespace_whose_absence_cannot_be_proved_is_also_unobservable(self):
        with mock.patch.object(state.RUNNER, "runner_pid_namespace_state",
                               return_value="different_pid_namespace"):
            alive, src = state.run_alive({"pid": 1, "process_identity": "x",
                                          "pid_namespace": "pid:[4026500000]"})
        self.assertFalse(alive)
        self.assertIn("ненаблюдаема", src)

    def test_a_run_recorded_in_a_vanished_namespace_is_dead_not_unobservable(self):
        # Task 938's distinction, from this side: a namespace proven gone holds
        # no processes, so the run is over and the board must stop reserving a
        # «ненаблюдаема» plate for it.
        with mock.patch.object(state.RUNNER, "runner_pid_namespace_state",
                               return_value="recorded_namespace_absent"):
            self.assertEqual(
                state.run_alive({"pid": 1, "process_identity": "x",
                                 "pid_namespace": "pid:[4026500000]"}),
                (False, None))

    def test_a_live_run_without_a_stated_observation_is_refused(self):
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(a_snapshot([a_task(run={"alive": True})]))
        schema.validate_snapshot(a_snapshot([a_task(
            run={"alive": True, "alive_src": "pid и стартовый тик ядра"})]))


class RunnerInterface(unittest.TestCase):
    """What this repository borrows from task-agent's runner still exists.

    `process_map_state` deliberately does not own «is this recorded process still
    this run»; it imports `task_runner` and asks. That borrowing is a contract
    across two repositories with no shared test run, and on 2026-08-08 it broke
    silently: task 938 renamed `runner_pid_namespace_visible` to
    `runner_pid_namespace_state`, nothing here changed, and the first thing to
    notice was the user's board — every thread had been observing nothing for
    about ten hours behind an `AttributeError`.

    The check itself is not written here: it belongs to `runner_contract`, which
    the wake-up of every direction also calls (finding HIGH-1 of review 954 —
    a guard that only a test run performs is a guard nothing performs). These
    tests drive that one owner, so the regression and the operational alarm
    cannot answer differently.
    """

    def fixture(self, tmp: str, consumer: str, runner: str,
                registry: str | None = None) -> tuple[Path, Path]:
        """A pair of repositories, one borrowing and one defining.

        The negative controls need a *real* divergence between two files, not a
        patched attribute: the outage was a real rename in a real file, and a
        guard that only survives monkeypatching proves nothing about it. The
        installed runner is never touched — the other repository is not this
        task's to edit.
        """
        scripts = Path(tmp) / "consumer"
        scripts.mkdir()
        (scripts / "collector.py").write_text(consumer)
        runner_scripts = Path(tmp) / "runner"
        runner_scripts.mkdir()
        (runner_scripts / "task_runner.py").write_text(runner)
        if registry is not None:
            (runner_scripts / f"{self.REGISTRY_MODULE}.py").write_text(registry)
        return scripts, runner_scripts

    # The module the fixture installation names as its live-run registry. Named
    # by the test rather than taken from `product_memory`, because which module
    # answers that is a setting of whichever machine happens to run this, and a
    # test that borrows the machine's answer stops being a test of the guard.
    REGISTRY_MODULE = "example_run_registry"

    CONSUMER = ("def run_alive(runner):\n"
                "    namespace_state = RUNNER.runner_pid_namespace_state(runner)\n"
                "    if namespace_state == 'recorded_namespace_absent':\n"
                "        return False\n"
                "    if namespace_state != 'local':\n"
                "        return False\n"
                "    return RUNNER.process_is_live(runner['pid'], runner['identity'])\n")
    RUNNER_OK = ("def runner_pid_namespace_state(runner):\n"
                 "    if runner.get('gone'):\n"
                 "        return 'recorded_namespace_absent'\n"
                 "    return 'local'\n"
                 "def process_is_live(pid, identity):\n"
                 "    return True\n")
    REGISTRY_CONSUMER = ("def registered(task):\n"
                         "    return RUN_REGISTRY.live_run_processes(task)\n")
    REGISTRY_OK = ("def live_run_processes(task):\n"
                   "    return []\n")

    def test_the_scan_sees_the_borrowings_it_is_supposed_to_guard(self):
        # A scan that quietly matches nothing would pass every other test here.
        borrowed = runner_contract.borrowed_names()
        self.assertIn("process_map_state.py", borrowed)
        self.assertLessEqual({"process_is_live", "runner_pid_namespace_state"},
                             borrowed["process_map_state.py"])
        registry = runner_contract.borrowed_names(owner_name="RUN_REGISTRY")
        self.assertIn("process_map_state.py", registry)
        self.assertIn("live_run_processes", registry["process_map_state.py"])

    def test_the_installed_contract_holds(self):
        self.assertIsNotNone(state.RUNNER)
        self.assertEqual(runner_contract.check(), [])

    def test_a_renamed_runner_function_is_a_violation(self):
        # Exactly the 938 rename, reproduced as two files that disagree: the
        # consumer still asks for `runner_pid_namespace_state`, the runner has
        # gone back to the pre-938 name.
        renamed = self.RUNNER_OK.replace("runner_pid_namespace_state",
                                         "runner_pid_namespace_visible")
        with tempfile.TemporaryDirectory() as tmp:
            scripts, runner_scripts = self.fixture(tmp, self.CONSUMER, renamed)
            violations = runner_contract.check(runner_scripts, scripts)
        self.assertTrue(any(item["kind"] == "name"
                            and "runner_pid_namespace_state" in item["text"]
                            for item in violations), violations)

    def test_a_renamed_live_run_registry_function_is_a_violation(self):
        renamed = self.REGISTRY_OK.replace("live_run_processes", "live_processes")
        with tempfile.TemporaryDirectory() as tmp:
            scripts, runner_scripts = self.fixture(
                tmp, self.REGISTRY_CONSUMER, self.RUNNER_OK, renamed)
            violations = runner_contract.check(runner_scripts, scripts,
                                               self.REGISTRY_MODULE)
        self.assertTrue(any(item["kind"] == "name"
                            and f"{self.REGISTRY_MODULE}.live_run_processes" in item["text"]
                            for item in violations), violations)

    def test_a_missing_live_run_registry_module_is_named_not_alarmed(self):
        # An installation that names a registry it does not have is a working
        # installation: the collector suppresses the process inventory by name.
        # Ringing the contract alarm on every wake-up of every direction there
        # would train the reader to ignore it — and the alarm's whole worth is
        # that it is never ignored.
        with tempfile.TemporaryDirectory() as tmp:
            scripts, runner_scripts = self.fixture(
                tmp, self.REGISTRY_CONSUMER, self.RUNNER_OK)
            violations = runner_contract.check(runner_scripts, scripts,
                                               self.REGISTRY_MODULE)
            said = runner_contract.report(violations, runner_scripts,
                                          self.REGISTRY_MODULE)
        self.assertEqual(violations, [])
        self.assertIn(self.REGISTRY_MODULE, said)
        self.assertIn("не установлено", said)

    def test_an_installation_that_names_no_registry_is_not_a_violation(self):
        # The fresh clone: nobody has named a live-run registry, so there is no
        # borrowing to check and no alarm to ring — and the report still says the
        # inventory is suppressed rather than leaving the reader to assume it works.
        with tempfile.TemporaryDirectory() as tmp:
            scripts, runner_scripts = self.fixture(
                tmp, self.CONSUMER, self.RUNNER_OK)
            violations = runner_contract.check(runner_scripts, scripts, "")
            said = runner_contract.report(violations, runner_scripts, "")
        self.assertEqual(violations, [])
        self.assertIn("не называет", said)

    def test_the_matching_live_run_registry_pair_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            scripts, runner_scripts = self.fixture(
                tmp, self.REGISTRY_CONSUMER, self.RUNNER_OK, self.REGISTRY_OK)
            self.assertEqual(runner_contract.check(runner_scripts, scripts,
                                                   self.REGISTRY_MODULE), [])

    def test_a_renamed_answer_is_a_violation_even_when_the_name_survives(self):
        # The quiet version of the same blindness: the function keeps its name,
        # so every borrowed name exists, but `local` is gone and every run would
        # silently become «ненаблюдаемым».
        renamed = self.RUNNER_OK.replace("'local'", "'same_namespace'")
        with tempfile.TemporaryDirectory() as tmp:
            scripts, runner_scripts = self.fixture(tmp, self.CONSUMER, renamed)
            violations = runner_contract.check(runner_scripts, scripts)
        self.assertTrue(any(item["kind"] == "vocabulary" and "'local'" in item["text"]
                            for item in violations), violations)

    def test_the_matching_pair_is_clean_so_the_controls_mean_something(self):
        with tempfile.TemporaryDirectory() as tmp:
            scripts, runner_scripts = self.fixture(tmp, self.CONSUMER, self.RUNNER_OK)
            self.assertEqual(runner_contract.check(runner_scripts, scripts), [])

    def test_a_scan_that_sees_nothing_is_itself_a_violation(self):
        with tempfile.TemporaryDirectory() as tmp:
            scripts, runner_scripts = self.fixture(tmp, "x = 1\n", self.RUNNER_OK)
            violations = runner_contract.check(runner_scripts, scripts)
        self.assertEqual([item["kind"] for item in violations], ["scan"])

    def test_an_unreadable_runner_is_a_violation_not_a_silence(self):
        with tempfile.TemporaryDirectory() as tmp:
            violations = runner_contract.check(Path(tmp) / "nowhere")
        self.assertEqual({item["kind"] for item in violations}, {"module"})
        # The task runner itself is the borrowing nothing here can do without.
        self.assertEqual({Path(item["src"]).name for item in violations},
                         {"task_runner.py"})

    def test_the_command_line_says_so_with_its_exit_code(self):
        # What a hook, a timer or a CI step actually reads. The healthy run is
        # part of the control: an entry point that always failed would be as
        # useless as one that never did.
        renamed = self.RUNNER_OK.replace("runner_pid_namespace_state",
                                         "runner_pid_namespace_visible")
        entry = str(Path(runner_contract.__file__))
        healthy = subprocess.run([sys.executable, entry], capture_output=True, text=True)
        self.assertEqual(healthy.returncode, 0, healthy.stdout + healthy.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            _, runner_scripts = self.fixture(tmp, self.CONSUMER, renamed)
            broken = subprocess.run([sys.executable, entry,
                                     "--runner-scripts", str(runner_scripts)],
                                    capture_output=True, text=True)
        self.assertEqual(broken.returncode, 1, broken.stdout + broken.stderr)
        self.assertIn("runner_pid_namespace_state", broken.stdout)


class RunnerContractIsWatched(unittest.TestCase):
    """The guard runs from something that runs by itself (finding HIGH-1 of 954).

    The names were guarded by a test, and nothing ran the test: no CI job, no
    hook, no cron entry, no unit. The only live mechanisms in this contour are
    the four direction timers and the board service, so the wake-up of every
    direction asks before it observes anything.

    The alarm leaves through `deliver` like every other letter since 861, which
    means it touches the outbound ledger. A test that touched the real one would
    be writing the record of what the user was told, so it is pointed at a
    temporary file for the duration.
    """

    def setUp(self):
        self._ledger = tempfile.TemporaryDirectory()
        patch = mock.patch.object(tick.outbound, "LEDGER",
                                  Path(self._ledger.name) / "outbound.json")
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self._ledger.cleanup)

    def test_the_wake_up_asks_before_it_observes(self):
        # Ordering is the whole point: `build` is what dies on a renamed name,
        # so the alarm cannot be downstream of it.
        source = Path(tick.__file__).read_text()
        body = source[source.index("def main("):]
        self.assertLess(body.index("runner_contract_alarm("), body.index("build(args.thread)"))

    def test_a_divergence_leaves_through_the_product_gmail_channel(self):
        violation = [{"kind": "name", "text": "разошлось", "src": "источник"}]
        moment = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
        with mock.patch.object(tick.runner_contract, "check", return_value=violation), \
                mock.patch.object(tick, "product_review_boundary_violations", return_value=[]), \
                mock.patch.object(tick, "send_mail") as mailed:
            found, reminder = tick.runner_contract_alarm("process", {}, moment, announce=True)
        self.assertEqual(found, violation)
        self.assertEqual(mailed.call_count, 1)
        subject, body = mailed.call_args.args[:2]
        self.assertEqual(subject, "Продакт: контракт с task_runner разошёлся")
        self.assertIn("наблюдение продакта держится на именах из task-agent", body)
        self.assertIn("разошлось", body)
        self.assertEqual(reminder["at"], moment.isoformat())

    def test_the_same_divergence_is_not_repeated_every_twenty_minutes(self):
        # Twelve wake-ups an hour saying the same thing is a mute button by
        # other means; the rate is the one the standing reminders already use.
        violation = [{"kind": "name", "text": "разошлось", "src": "источник"}]
        moment = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
        stored = {"runner_contract_reminder": {
            "at": (moment - timedelta(seconds=60)).isoformat(),
            "signature": json.dumps(["разошлось"])}}
        with mock.patch.object(tick.runner_contract, "check", return_value=violation), \
                mock.patch.object(tick, "product_review_boundary_violations", return_value=[]), \
                mock.patch.object(tick, "send_mail") as mailed:
            tick.runner_contract_alarm("process", stored, moment, announce=True)
        self.assertEqual(mailed.call_count, 0)

    def test_a_new_divergence_is_news_at_once(self):
        violation = [{"kind": "name", "text": "другое расхождение", "src": "источник"}]
        moment = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
        stored = {"runner_contract_reminder": {
            "at": (moment - timedelta(seconds=60)).isoformat(),
            "signature": json.dumps(["разошлось"])}}
        with mock.patch.object(tick.runner_contract, "check", return_value=violation), \
                mock.patch.object(tick, "product_review_boundary_violations", return_value=[]), \
                mock.patch.object(tick, "send_mail") as mailed:
            tick.runner_contract_alarm("process", stored, moment, announce=True)
        self.assertEqual(mailed.call_count, 1)

    def test_a_healthy_contract_says_nothing_and_forgets_the_old_alarm(self):
        moment = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
        stored = {"runner_contract_reminder": {"at": moment.isoformat(), "signature": "x"}}
        with mock.patch.object(tick.runner_contract, "check", return_value=[]), \
                mock.patch.object(tick, "product_review_boundary_violations", return_value=[]), \
                mock.patch.object(tick, "send_mail") as mailed:
            found, reminder = tick.runner_contract_alarm("process", stored, moment, announce=True)
        self.assertEqual((found, reminder), ([], None))
        self.assertEqual(mailed.call_count, 0)

    def test_a_divergence_makes_the_unit_fail_even_if_the_tick_still_observed(self):
        # An exit code is the one signal that survives a wake-up nobody reads.
        # Before this, the four units failed on a traceback and the observation
        # simply stopped; a tick that manages to observe with a broken contract
        # must still not be recorded as a success.
        source = Path(tick.__file__).read_text()
        self.assertIn("verdict = 1 if contract or daily_failure else 0", source)
        self.assertNotIn("\n    return 0\n", source[source.index("    verdict = 1"):])

    def test_unavailable_product_review_boundary_reuses_the_process_alarm(self):
        tasks = Path(self._ledger.name) / "tasks"
        policy = tasks / "1246-example" / ".runner" / "companion-application-policy.json"
        policy.parent.mkdir(parents=True)
        policy.write_text(json.dumps({
            "product_review_boundary": {
                "mode": "unavailable",
                "detail": (
                    "enforcement requested before its owners are ready: "
                    "task product-review mail owner is missing: /missing/owner.py"
                ),
            }
        }), encoding="utf-8")
        moment = datetime(2026, 8, 27, 3, 0, tzinfo=timezone.utc)
        with mock.patch.object(tick.product_memory, "tasks_repo", return_value=tasks.parent), \
                mock.patch.object(tick.runner_contract, "check", return_value=[]), \
                mock.patch.object(tick, "send_mail") as mailed:
            found, reminder = tick.runner_contract_alarm(
                "process", {}, moment, announce=True
        )
        self.assertEqual(found[0]["kind"], "product_review_boundary")
        self.assertIn("программа отправки писем о проверке не найдена", found[0]["text"])
        self.assertEqual(mailed.call_count, 1)
        subject, body = mailed.call_args.args[:2]
        self.assertIn("обязательная проверка", subject.lower())
        self.assertIn("обязательная проверка", body.lower())
        self.assertNotIn("enforcement requested", body)
        self.assertNotIn("mail owner is missing", body)
        self.assertNotIn("контракт с task_runner разошёлся", subject)
        self.assertIsNotNone(reminder)

    def test_a_terminal_task_does_not_keep_the_boundary_alarm_alive(self):
        tasks = Path(self._ledger.name) / "tasks"
        task = tasks / "1246-example"
        policy = task / ".runner" / "companion-application-policy.json"
        policy.parent.mkdir(parents=True)
        policy.write_text(json.dumps({
            "product_review_boundary": {
                "mode": "unavailable",
                "detail": "mail owner timed out",
            }
        }), encoding="utf-8")
        (task / "task.md").write_text(
            '---\nid: 1246\nstatus: "completed"\n---\n# Done\n', encoding="utf-8"
        )
        with mock.patch.object(tick.product_memory, "tasks_repo", return_value=tasks.parent):
            self.assertEqual(tick.product_review_boundary_violations(), [])

    def test_the_direction_state_file_carries_the_result_of_the_check(self):
        # «Проверка была и прошла» and «проверки никто не делал» are different
        # claims, and the board may only print the one it observed.
        source = Path(tick.__file__).read_text()
        self.assertIn('"runner_contract": {', source)
        self.assertIn('"violations": contract', source)


class StateAge(unittest.TestCase):
    """What «без изменений N» measures, and that it says so (finding MEDIUM-1)."""

    def test_the_newest_input_of_the_area_wins_over_the_one_that_used_to_be_picked(self):
        # The review's control: a task.md rewritten now, a status.json from
        # before. The old rule preferred status.json whenever it held any state
        # and reported the older instant for the newer area.
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "001-task"
            task_dir.mkdir()
            (task_dir / "status.json").write_text('{"state": "running"}')
            os.utime(task_dir / "status.json", (1_000_000, 1_000_000))
            (task_dir / "task.md").write_text('---\nstatus: "blocked"\n---\n')
            since, age, src = state.state_age(task_dir)
        self.assertIn("task.md", src)
        self.assertLess(age, 60 * 60 * 24)

    def test_every_file_the_area_depends_on_is_an_input(self):
        # If the area is derived from a file, that file has to be able to move
        # the age; otherwise the caption measures something else again.
        self.assertEqual(set(state.AREA_INPUTS),
                         {"task.md", "status.json", "verification.md", ".runner/runner.json"})

    def test_a_task_with_no_inputs_at_all_says_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(state.state_age(Path(tmp)), (None, None, None))

    def test_the_caption_says_what_it_measures(self):
        html = render.render({"snapshot": a_snapshot(), "timeline": [],
                              "board": render.build_board(a_snapshot()),
                              "built_at": "2026-08-06T12:00:00+00:00",
                              "live_url": None, "digest": "d"})
        self.assertIn('function ageCaption(age) { return "без изменений " + age; }', html)
        self.assertNotIn("в этом состоянии \" + age", html)
        # One wording, built in one place: the plate and the ticker that
        # refreshes it every half minute used to spell the caption separately.
        self.assertEqual(html.count('"без изменений "'), 1)
        self.assertEqual(html.count("ageCaption("), 3)


class OfflineRecording(unittest.TestCase):
    def test_the_delivered_recording_cannot_reach_the_network(self):
        # The previous test looked for the string `fetch` and never found the one
        # in the template. The live branch has to exist — without it a status
        # change never reaches the screen — so what is asserted is that the
        # delivered file has no reachable call: `live_url` is null, and every
        # `fetch` in the page sits behind the derived false `LIVE` value.
        data = {"snapshot": a_snapshot(), "timeline": [a_record()],
                "board": render.build_board(a_snapshot()),
                "built_at": "2026-08-06T12:00:00+00:00", "live_url": None, "digest": "d"}
        html = render.render(data)
        self.assertIn('"live_url":null', html.replace(" ", ""))
        for forbidden in ("http://", "https://", "XMLHttpRequest", 'src="//', "WebSocket",
                          "EventSource", "importScripts", "navigator.sendBeacon"):
            self.assertNotIn(forbidden, html.replace('lang="ru"', ""))
        body = html[html.index("if (LIVE)"):]
        self.assertEqual(html.count("fetch("), body.count("fetch("))

    def test_the_board_is_the_screen_that_opens(self):
        data = {"snapshot": a_snapshot(), "timeline": [],
                "board": render.build_board(a_snapshot()),
                "built_at": "2026-08-06T12:00:00+00:00", "live_url": None, "digest": "d"}
        html = render.render(data)
        self.assertIn("<title>Доска работ</title>", html)
        # And it is the only screen besides the task index: the map, its switch
        # and its playhead are gone (task 1067). What it played back is not —
        # the timeline is read by the feed above the columns.
        for gone in ("Карта во времени", 'getElementById("map")', "mapmode",
                     "requestAnimationFrame", "<canvas"):
            self.assertNotIn(gone, html, f"экран карты вернулся: {gone}")
        self.assertIn('sessionStorage.getItem("screen") !== "tasks"', html)
        self.assertIn("Последние изменения", html)


def code_only(source: str) -> str:
    """Python source with its comments and docstrings removed.

    Several claims here are about what the code does, and this file explains at
    length what the code deliberately stopped doing — so a literal search finds
    the explanation and passes or fails on prose. Tokenising first makes the
    assertion about the code it claims to be about.
    """
    import io
    import tokenize
    kept = []
    previous = tokenize.INDENT
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            continue
        if token.type == tokenize.STRING and previous in (
                tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE, tokenize.NL):
            continue                                    # a docstring, not a value
        kept.append(token.string)
        if token.type not in (tokenize.NL, tokenize.COMMENT):
            previous = token.type
    return " ".join(kept)


def thread_of(cmdline):
    """The direction a wake-up process names, from its own argv."""
    return state.ticked_thread(cmdline)


def a_page(snapshot=None) -> str:
    """The page as it is delivered, built from one snapshot."""
    snapshot = snapshot or a_snapshot()
    return render.render({"snapshot": snapshot, "timeline": [],
                          "board": render.build_board(snapshot),
                          "built_at": "2026-08-06T12:00:00+00:00",
                          "live_url": None, "live_page_url": None, "digest": "d"})


class WorkOutsideDeadOwner(unittest.TestCase):
    """Task 788, absorbed here: work that carried on after its owner died.

    Observed twice before it was believed. On 757 the owner run ended at 08:28
    with «2 of 20» published, the measurement finished in another process at
    08:48, and the product owner found out at 12:00 by going to look by hand —
    three and a half hours. On 712 the same thing. Both observers watched
    `status.json` and `progress.json`, and both files stopped moving at the
    moment there was nobody left to move them, so a silent tick was honest and
    useless at once.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def touch(self, name: str, when: float, body: str = "x") -> Path:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        os.utime(path, (when, when))
        return path

    def test_an_artifact_moving_is_observed_and_the_file_is_named(self):
        self.touch("status.json", 1000, "{}")
        self.touch("evidence/matrix-execution.json", 2000)
        moved, age, src = state.artifact_movement(self.dir)
        self.assertIsNotNone(moved)
        self.assertIn("matrix-execution.json", src)
        self.assertGreater(age, 0)

    def test_the_run_s_own_bookkeeping_is_not_artifact_movement(self):
        """Accepting a finished task rewrites `task.md`, and that is not work.

        The first cut of this rule counted any mtime in the directory and fired
        on eleven tasks, eight of which had simply been accepted by a person
        afterwards. `status.json`, `progress.json`, `task.md` and `.runner/` are
        precisely the files the old observer already watched, so counting them
        would restate the old observation rather than add one.
        """
        self.touch("verification.md", 1000)
        for bookkeeping in ("status.json", "progress.json", "task.md",
                            "task_contract.json", ".runner/runner.json"):
            self.touch(bookkeeping, 9000, "{}")
        moved, _, src = state.artifact_movement(self.dir)
        self.assertIn("verification.md", src)

    def test_no_transcript_is_ever_looked_at(self):
        self.touch("notes.md", 1000)
        self.touch("transcripts/child.jsonl", 9000)
        self.touch("session-transcript.txt", 9000)
        _, _, src = state.artifact_movement(self.dir)
        self.assertIn("notes.md", src)

    def test_a_cloned_subject_does_not_make_one_task_fifty_times_dearer(self):
        """A look must cost the same whatever happened inside the task.

        Review 734 cloned its subject into the task directory and left 16 250
        entries there, nearly all of them git objects and pytest caches. Walking
        them would give that one task a look fifty times the price of any other,
        which is the one property the contour requires a look not to have.
        """
        self.touch("findings.md", 1000)
        for i in range(200):
            self.touch(f"subject/.git/objects/{i:02x}/{i}", 9000)
            self.touch(f"subject/.pytest_cache/v/{i}", 9000)
        _, _, src = state.artifact_movement(self.dir)
        self.assertIn("findings.md", src)

    def test_the_refusal_the_contour_writes_itself_is_read(self):
        # `completion_refusal.kind` has been in `status.json` all along, with
        # the words «Work it started may have continued outside its process» in
        # its summary, and nothing read it.
        self.touch("status.json", 1000, json.dumps({
            "state": "blocked",
            "completion_refusal": {"kind": "owner_ended_after_incomplete_published_progress",
                                   "summary": "Owner run ended before its closing step"}}))
        run = state.run_state(self.dir)
        self.assertEqual(run["refusal"], "owner_ended_after_incomplete_published_progress")
        self.assertIn("closing step", run["refusal_summary"])

    def test_the_flag_is_declared_and_puts_the_task_in_the_jam_area(self):
        self.assertIn("work_outside_owner", schema.TASK_FLAGS)
        self.assertEqual(state.board_area("planned", ["work_outside_owner"], False, None), "stuck")

    def test_an_accepted_task_is_not_dragged_back_for_a_second_look(self):
        # Somebody already looked at a terminal task and accepted it, so
        # «сходите посмотрите артефакты» would ask for a decision to be retaken.
        self.assertEqual(state.board_area("completed", ["work_outside_owner"], False, None), "done")


class WhatCanBePickedUp(unittest.TestCase):
    """The first question of every wake-up, which the board could not answer."""

    def test_a_repository_held_by_a_live_run_is_the_named_holder(self):
        entries = [{"id": 783, "title": "Живая", "flags": ["live"],
                    "run": {"repo": "/opt/projects/example-product"}}]
        busy = state.busy_repository_map(entries)
        why, src = queue_why({"id": 722}, "/opt/projects/example-product", busy)
        self.assertIn("example-product", why)
        self.assertIn("783", why)
        self.assertTrue(src.strip())

    def test_a_task_is_not_held_by_its_own_live_run(self):
        entries = [{"id": 783, "title": "Живая", "flags": ["live"],
                    "run": {"repo": "/opt/projects/example-product"}}]
        busy = state.busy_repository_map(entries)
        why, _ = queue_why({"id": 783}, "/opt/projects/example-product", busy)
        self.assertIsNone(why)

    def test_a_dead_run_does_not_hold_a_repository(self):
        entries = [{"id": 783, "title": "Мёртвая", "flags": ["idle"],
                    "run": {"repo": "/opt/projects/example-product"}}]
        self.assertEqual(state.busy_repository_map(entries), {})

    def test_the_frontmatter_reason_is_a_holder_and_names_its_source(self):
        why, src = state.queue_reason(
            {"id": 686, "status_detail": "queued_behind_active_worktree_writers_669_689"},
            {"repo": None}, {})
        self.assertEqual(why, "queued_behind_active_worktree_writers_669_689")
        self.assertIn("status_detail", src)

    def test_a_holder_named_without_a_source_cannot_reach_the_board(self):
        broken = a_snapshot([a_task(board={"blocked_by": "занят репозиторий",
                                           "blocked_by_src": None})])
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(broken)

    def test_what_can_be_picked_up_stands_in_the_column_of_its_direction(self):
        """Полоса-сводка эту группу больше не несёт (задача 1163).

        «Что подхватить» — наш собственный список дел: пользователь запустить
        оттуда ничего не может, и действия за элементом для него нет, а место на
        первом экране он занимал. Список никуда не делся — он стоит областью
        «Можно подхватить» в колонке своего направления, и ближайшую работу
        каждого направления называет строка его состояния.
        """
        board = render.build_board(a_snapshot([a_task(board={"area": "pickup"})]))
        self.assertEqual([p["id"] for p in board["pickup"]], [1])
        page = a_page()
        self.assertIn("Можно подхватить", page)
        self.assertNotIn('["Что подхватить", board.pickup', page)

    def test_the_area_says_by_what_rule_it_chose_its_contents(self):
        # A heading is a claim; the rule behind it is narrower than the heading,
        # and a reader who cannot see the rule reads the heading as a verdict.
        page = a_page()
        self.assertIn("ничто на диске эту задачу не держит", page)
        self.assertIn("не удалось наблюсти задачу", page)


def queue_why(task, repo, busy):
    """`queue_reason` with the run reduced to the one field it reads."""
    return state.queue_reason(task, {"repo": repo}, busy)


# The real sentence 831 carried, and the field it should have carried instead.
# The scenario is not invented: 830 held /opt/projects/example-engine, 831
# was the next step in the same tree, and the sentence saying so was invisible.
LIVE_831 = "starts_after=830 worktree=/opt/projects/example-engine"


def conditioned(task_id, field, **over):
    """A task whose `status_detail` carries a condition, as the collector hands it."""
    return a_task(id=task_id, dir=f"{task_id}-t", status_detail=field, **over)


class StartConditionIsAField(unittest.TestCase):
    """831 stood forty minutes because its condition was a sentence.

    The condition existed and was correct — «после завершения прогона 830, то же
    рабочее дерево» — but it lived in the last line of `## Summary`, so the
    observer could not tell that 830 had closed and the tree was free. «Условие
    снялось» could be neither a state nor a transition, and the queue moved only
    when the user asked how it is tracked.
    """

    def test_the_condition_is_read_out_of_the_field_it_is_written_in(self):
        condition = state.start_condition(LIVE_831)
        self.assertEqual(condition["after"], [830])
        self.assertEqual(condition["worktrees"], ["/opt/projects/example-engine"])
        self.assertIn("status_detail", condition["src"])

    def test_prose_is_not_a_condition(self):
        # The old field is full of sentences, and reading one as a condition
        # would let the board clear a hold nobody wrote in a checkable form.
        self.assertIsNone(state.start_condition(
            "Запускать только после завершения прогона 830, то же рабочее дерево"))
        self.assertIsNone(state.start_condition(None))

    def test_prose_keeps_holding_exactly_as_before(self):
        why, src = state.queue_reason(
            {"id": 686, "status_detail": "queued_behind_active_worktree_writers_669_689"},
            {"repo": None}, {})
        self.assertEqual(why, "queued_behind_active_worktree_writers_669_689")
        self.assertIn("status_detail", src)

    def test_an_open_predecessor_holds_the_task_and_is_named(self):
        condition = state.start_condition(LIVE_831)
        verdict = state.condition_state(condition, {830: "in_progress"}, {}, 831)
        self.assertFalse(verdict["satisfied"])
        self.assertIn("830", verdict["holding"][0])

    def test_a_busy_working_tree_holds_the_task_even_after_the_predecessor_closed(self):
        # The two halves of the real condition are independent: 830 closing does
        # not free the tree if something else is writing in it, and putting a
        # second child there is the collision the sentence was written to stop.
        condition = state.start_condition(LIVE_831)
        busy = {"/opt/projects/example-engine": {"id": 775, "title": "Живая"}}
        verdict = state.condition_state(condition, {830: "completed"}, busy, 831)
        self.assertFalse(verdict["satisfied"])
        self.assertIn("775", verdict["holding"][0])

    def test_a_predecessor_the_index_does_not_know_keeps_holding(self):
        # An unobservable predecessor is not a finished one. A typo in a number
        # must never promote work to «готово к запуску».
        verdict = state.condition_state(state.start_condition("starts_after=9999"), {}, {}, 1)
        self.assertFalse(verdict["satisfied"])
        self.assertIn("не наблюдается", verdict["holding"][0])

    def test_the_condition_clears_itself_when_what_it_named_is_gone(self):
        condition = state.start_condition(LIVE_831)
        verdict = state.condition_state(condition, {830: "completed"}, {}, 831)
        self.assertTrue(verdict["satisfied"])
        self.assertEqual(state.queue_reason({"id": 831}, {"repo": None}, {}, verdict),
                         (None, None))
        self.assertIn("задача 830 закрыта (completed)", verdict["met"])

    def test_ready_to_start_is_not_the_same_answer_as_pickup(self):
        # Both say nothing is holding the task. Only one says a condition was
        # written down in advance and has since been met, and that is the whole
        # difference between «можно подхватить» and «пора запускать».
        self.assertEqual(state.board_area("planned", ["idle"], False, None), "pickup")
        self.assertEqual(state.board_area("planned", ["idle"], False, None, ready=True),
                         "ready_to_start")

    def test_the_whole_pass_moves_the_task_from_the_queue_to_ready(self):
        """The real 830→831 pair, driven through the pass the board really uses."""
        live = a_task(id=830, dir="830-t", flags=["live"], status="in_progress",
                      run={"repo": "/opt/projects/example-engine", "alive": True,
                           "alive_src": "проверено по .runner/runner.json"})
        waiting = conditioned(831, LIVE_831)
        entries, source = [live, waiting], [{"id": 830}, {"id": 831}]
        state.assign_areas(entries, source, {830: "in_progress", 831: "planned"})
        self.assertEqual(waiting["board"]["area"], "queued")
        self.assertIn("830", waiting["board"]["blocked_by"])
        self.assertTrue(waiting["board"]["blocked_by_src"].strip())

        # 830 closes and its process goes: exactly the 19:52 UTC of 2026-08-06.
        live["flags"] = []
        live["status"] = "completed"
        live["run"]["alive"] = False
        waiting = conditioned(831, LIVE_831)
        state.assign_areas([live, waiting], source, {830: "completed", 831: "planned"})
        self.assertEqual(waiting["board"]["area"], "ready_to_start")
        self.assertIsNone(waiting["board"]["blocked_by"])
        self.assertTrue(waiting["board"]["start_condition"]["satisfied"])

    def test_a_task_that_named_no_condition_says_so_rather_than_saying_met(self):
        plain = a_task(id=1, dir="001-t")
        state.assign_areas([plain], [{"id": 1}], {1: "planned"})
        self.assertIsNone(plain["board"]["start_condition"])
        self.assertEqual(plain["board"]["area"], "pickup")

    def test_the_board_draws_the_area_and_says_by_what_rule(self):
        ready = a_task(board={"area": "ready_to_start"})
        area = next(a for a in render.build_board(a_snapshot([ready]))["panels"][0]["areas"]
                    if a["key"] == "ready_to_start")
        self.assertEqual([p["id"] for p in area["plates"]], [1])
        self.assertIn("машиночитаемое условие запуска", a_page())


class DecisionTakenAndNotCarriedOut(unittest.TestCase):
    """The same hole one step earlier, and the same evening.

    «Решение продакта принято здесь и вслух: из девяти живых документов человеку
    идут три» was written at 20:1x on 2026-08-06. At 20:5x none of the three had
    gone out, and all three moved only after the user asked. A decision in a
    sentence moves nobody, so it becomes a field checked against the delivery
    evidence the contour already writes.
    """

    DELIVERED = {"delivered": True, "delivered_src": "delivery.md в каталоге задачи"}
    NOT_DELIVERED = {"delivered": False, "delivered_src": "квитанции несут только "
                                                          "события жизненного цикла прогона"}

    def decided(self, handoff):
        task = conditioned(835, "decision=deliver", status="completed",
                           detail={"handoff": handoff})
        state.assign_areas([task], [{"id": 835}], {835: "completed"})
        return task

    def test_a_decision_nothing_carried_out_stands_in_its_own_area(self):
        task = self.decided(self.NOT_DELIVERED)
        self.assertEqual(task["board"]["area"], "decision_unmet")
        self.assertFalse(task["board"]["decision"]["done"])
        self.assertTrue(task["board"]["decision"]["src"].strip())

    def test_the_existing_delivery_evidence_closes_it_and_no_new_sign_is_invented(self):
        task = self.decided(self.DELIVERED)
        self.assertEqual(task["board"]["area"], "done")
        self.assertTrue(task["board"]["decision"]["done"])
        self.assertIn("delivery.md", task["board"]["decision"]["src"])

    def test_a_decision_outranks_the_passive_undelivered_area(self):
        # «Не доставлено» is work nobody looked at; this is work somebody decided
        # must go out. They must not be the same plate.
        undecided = a_task(id=783, dir="783-t", status="completed",
                           detail={"handoff": self.NOT_DELIVERED})
        state.assign_areas([undecided], [{"id": 783}], {783: "completed"})
        self.assertEqual(undecided["board"]["area"], "undelivered")

    def test_an_unknown_decision_is_not_recorded_as_one(self):
        # An area that cannot say «исполнено» would be a second list of prose.
        self.assertIsNone(state.start_condition("decision=подумать"))

    def test_a_decision_alone_does_not_make_a_task_ready_to_start(self):
        task = conditioned(835, "decision=deliver", detail={"handoff": self.DELIVERED})
        state.assign_areas([task], [{"id": 835}], {835: "planned"})
        self.assertEqual(task["board"]["area"], "pickup")


CATALOGUE = [
    {"id": 736, "title": "надо исправить task_index — она присылает задачи task-agent",
     "slug": "736-max-task-index"},
    {"id": 713, "title": "Ревью кода клиента силами Claude: старый код и кандидаты на рефакторинг",
     "slug": "713-client"},
    {"id": 394, "title": "Move `/task` workflow ownership into the client",
     "slug": "394-task-workflow-ownership"},
    {"id": 811, "title": "Закрыть доступ: /codex только в своём боте и только у владельца",
     "slug": "811-delivery-bot-codex-owner-only"},
    {"id": 266, "title": "Remove TTS cleanup attempt prefix", "slug": "266-tts-prefix"},
]


def unplanned(*items) -> list[dict]:
    """`unplanned` against a fixed catalogue, so the fixture is the whole input."""
    return state.unplanned(list(items), CATALOGUE)


class ProductContentLivesOutsideGit(unittest.TestCase):
    """The board reads the durable store, and says so when it cannot.

    Live product content was taken out of git on 2026-08-12 so that saving a
    discussion stops changing the evidence base of a code review — the failure
    that refused review of 839. The board followed the content, and the one
    thing it may not do is answer from a reading that never happened: an
    unobservable root is not a root without products, exactly as an unavailable
    live-run registry is not an empty list of runs.
    """

    def test_the_products_area_is_read_from_the_durable_store(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home) / "content"
            record = root / "products" / "task-agent" / "snapshot.md"
            record.parent.mkdir(parents=True)
            record.write_text(
                "# t\n## Открытые вопросы\n- вопрос продукта\n"
                "## Журнал эффекта\n- 2026-08-12 — эффект\n## В работе\n",
                encoding="utf-8")
            with mock.patch.object(product_memory, "ROOT", root), \
                 mock.patch.object(state, "PRODUCTS", root / "products"):
                entries = state.products(catalogue=[], mail=state.no_mail()
                                         if hasattr(state, "no_mail") else None)
        slugs = [entry["slug"] for entry in entries]
        self.assertEqual(slugs, ["task-agent"])
        self.assertEqual(entries[0]["effect"], ["2026-08-12 — эффект"])

    def test_an_unobservable_store_is_refused_not_shown_as_no_products(self):
        with tempfile.TemporaryDirectory() as home:
            absent = Path(home) / "never-created"
            with mock.patch.object(product_memory, "ROOT", absent), \
                 mock.patch.object(state, "PRODUCTS", absent / "products"):
                with self.assertRaises(schema.ContractError) as raised:
                    state.products(catalogue=[])
        self.assertIn("недоступен", str(raised.exception))


class WhatNeedsPlanning(unittest.TestCase):
    """The fourth question, and the two days it already cost.

    «ревью кода клиента силами Claude… Запрошено пользователем» stood in the
    `## В работе` section of the product record for two days and never became a
    task, because the flow had nowhere to put «надо запланировать».

    The first version of the area answered it with «в строке нет отдельно
    стоящего трёхзначного числа», and that признак was wrong in both directions
    at once (finding HIGH-1 of review 814): three of the four lines it printed
    already had tasks, and a real promise carrying an unrelated число was
    suppressed. The tests below are that finding, both directions of it.
    """

    def test_a_line_referencing_a_task_number_is_not_an_unkept_promise(self):
        self.assertEqual(unplanned(
            "2026-08-05 — **`/task_index` в Max: код починен, но не выкачен (736)**"), [])

    def test_a_line_naming_an_existing_task_in_words_is_not_an_unkept_promise(self):
        """First direction of the finding: the area invented work already planned.

        The line carries no number at all, and задача 713 stands under almost
        exactly its words. The area printed it as «надо запланировать» and sent
        the person to plan a task that was already in the catalogue.
        """
        line = ("2026-08-04 — ревью кода клиента силами Claude: код старый, вдумчиво "
                "его никто не смотрел, ищем кандидатов на рефакторинг.")
        self.assertEqual(unplanned(line), [])
        self.assertEqual(state.promise_link(line, CATALOGUE)["task"], 713)

    def test_a_line_with_a_number_that_counts_something_is_not_suppressed(self):
        """Second direction: a real promise lost to a число that named no task.

        «394 пройденных теста» is a quantity standing mid-sentence, and задача
        394 — about `/task` workflow ownership — has nothing to do with the line.
        The old rule read the digits and dropped the line off the board.
        """
        line = ("2026-08-05 — живой проверкой считается настоящая задача, а не юнит-набор: "
                "в наборе 394 пройденных теста, а поверхность так и не проверена")
        self.assertEqual([p["text"] for p in unplanned(line)], [line])
        self.assertIsNone(state.promise_link(line, CATALOGUE))

    def test_a_number_counting_a_noun_inside_brackets_is_not_a_reference(self):
        # «(266 тестов)» is written exactly like «(736)» and means a count. The
        # word right after the число is the difference, and it is the difference
        # the rule reads.
        self.assertEqual(state.task_references("регрессии зелёные (266 тестов)"), [])
        self.assertEqual(state.task_references("не выкачен (736)"), [736])

    def test_a_fragment_of_a_commit_hash_is_not_a_task_number(self):
        # `2a8e061` used to contribute «061» and suppress the line around it.
        self.assertEqual(state.task_references("коммит `2a8e061`, SHA-256 `4a0ede88`"), [])

    def test_a_number_written_as_a_quantity_still_names_a_task_when_the_words_agree(self):
        """The contour writes «(811 идёт, 812 ждёт её)», which reads like a count.

        The число alone cannot decide it, so the task's own name decides: half of
        the significant words of задача 811 stand in the line. The evidence names
        those words, so the link can be judged rather than believed.
        """
        line = ("2026-08-06 — **работа поставлена (811 идёт)**: /codex остаётся только "
                "в своём боте и только у владельца, доступ второго закрываем")
        link = state.promise_link(line, CATALOGUE)
        self.assertEqual(link["task"], 811)
        self.assertIn("codex", link["how"])

    def test_a_number_naming_no_known_task_is_reported_with_the_line(self):
        # The area may say what it compared and what it found; it may not turn a
        # number it could not resolve into silence.
        line = "2026-08-06 — **999 обещано и не заведено**"
        shown = unplanned(line)
        self.assertEqual(len(shown), 1)
        self.assertIn("999", shown[0]["checked"])

    def test_a_shown_line_says_what_it_was_compared_against(self):
        shown = unplanned("2026-08-04 — исследование продуктового решения по памяти")
        self.assertEqual(shown[0]["link"], "unknown")
        self.assertTrue(shown[0]["checked"].strip())
        # And never the claim the observation cannot carry.
        self.assertNotIn("задачи нет", shown[0]["checked"])

    def test_a_struck_through_line_is_settled(self):
        self.assertEqual(unplanned("~~сделано и закрыто~~"), [])


class TaskNumbersPastTheThousand(unittest.TestCase):
    """Номера задач переходят через тысячу, и признак ссылки это переживает.

    Задача 841. Дошли до 840 за сутки по шестьдесят, тысяча — вопрос дней. Пока
    признаком была длина «ровно три цифры», «заведена задачей 1002» не совпадало
    ни разу: строка с уже заведённой работой уходила в «надо запланировать»
    молча, без единой ошибки — то есть человека посылали заводить заново то, что
    заведено.

    Расширить длину и оставить всё остальное было нельзя. Проверка на 3323
    строках настоящих продуктовых записей показала, что тогда ссылками
    становятся `$0,2758`, «убытке 7920», «(390 и 1440)», «(1056/1056, exit 0)» и
    «годы (2026)» — и 1056 с 1440 сами станут номерами задач через недели, после
    чего счётчик прогонов молча превратится бы в ссылку на чужую задачу.
    """

    def test_a_four_digit_task_named_as_a_task_is_a_reference(self):
        self.assertEqual(state.task_references("Работа заведена задачей 1002."), [1002])
        self.assertEqual(state.task_references("задача №1002 закрыта"), [1002])
        self.assertEqual(state.task_references("task 1002 in flight"), [1002])

    def test_a_four_digit_task_at_the_head_of_a_dated_claim_is_a_reference(self):
        self.assertEqual(
            state.task_references("2026-08-07 00:31 — **1002, 1003 приняты**"),
            [1002, 1003])

    def test_a_four_digit_task_behind_an_arrow_is_a_reference(self):
        self.assertEqual(state.task_references("переведено на новую (1002 → 1005)"),
                         [1002, 1005])

    def test_a_year_never_becomes_a_reference(self):
        # Ровно ловушка постановки: дата стоит в той же позиции, что и номер.
        self.assertEqual(state.task_references("2026-08-07 — сборка зелёная"), [])
        self.assertEqual(
            state.task_references("годы (2026) и прочие четырёхзначные числа"), [])

    def test_a_four_digit_quantity_never_becomes_a_reference(self):
        # Все четыре — настоящие строки продуктовых записей.
        self.assertEqual(state.task_references("останов при убытке 7920)"), [])
        self.assertEqual(
            state.task_references("рендер в Firefox на 1440 и 390 без ошибок"), [])
        self.assertEqual(
            state.task_references("lock обещал 1056 оценок (1056/1056, exit 0)"), [])
        self.assertEqual(
            state.task_references("матрица валидна (159 вызовов, $0,2758, ~22 минуты"), [])

    def test_a_decimal_fraction_is_not_a_task_number(self):
        # `$0,268` и «медиана 0,117…0,200,» ссылались на живые задачи 268 и 200.
        self.assertEqual(state.task_references("(159 вызовов, $0,268, ~22 минуты"), [])
        self.assertEqual(state.task_references("медиана |Δz| 0,117…0,200, 2,5% решений"), [])

    def test_a_three_digit_reference_keeps_working_unchanged(self):
        # Прежние задачи продолжают жить без переименования.
        self.assertEqual(state.task_references("не выкачен (736)"), [736])
        self.assertEqual(state.task_references("(805, 806, идут)"), [805, 806])
        self.assertEqual(state.task_references("2026-08-06 — **806 принята**"), [806])
        self.assertEqual(state.task_references("регрессии зелёные (266 тестов)"), [])

    def test_a_four_digit_task_is_shown_on_the_board_when_the_index_knows_it(self):
        """Полный путь: строка с четырёхзначной ссылкой не уходит в «запланировать»."""
        catalogue = CATALOGUE + [{"id": 1002, "title": "Четырёхзначная задача",
                                  "slug": "1002-four-digit"}]
        line = "2026-08-07 — **работа заведена задачей 1002**"
        self.assertEqual(state.unplanned([line], catalogue), [])
        self.assertEqual(state.promise_link(line, catalogue)["task"], 1002)

    def test_a_four_digit_quantity_is_not_shown_as_a_task_even_when_that_task_exists(self):
        """1056 и 1440 станут номерами задач; счётчик прогонов ссылкой не станет."""
        catalogue = CATALOGUE + [{"id": 1056, "title": "Совсем про другое",
                                  "slug": "1056-unrelated"}]
        line = "2026-08-04 — долг измерения закрыт (1056/1056, exit 0)"
        self.assertIsNone(state.promise_link(line, catalogue))
        self.assertEqual([p["text"] for p in state.unplanned([line], catalogue)], [line])

    def test_the_promises_stand_in_their_own_area_and_are_counted_there(self):
        board = render.build_board(a_snapshot())
        area = next(a for a in board["panels"][0]["areas"] if a["key"] == "plan")
        self.assertEqual(len(area["promises"]), 1)
        self.assertEqual(area["count"], 1)
        # No task can stand here: a promise with a task behind it is a task.
        self.assertEqual(area["plates"], [])

    def test_a_promise_of_a_product_no_direction_owns_is_not_invented_onto_a_panel(self):
        snapshot = a_snapshot(products=[{"slug": "example-product", "questions": [],
                                         "effect": [],
                                         "promises": [a_promise(text="чужое обещание")]}])
        board = render.build_board(snapshot)
        area = next(a for a in board["panels"][0]["areas"] if a["key"] == "plan")
        self.assertEqual(area["promises"], [])

    def test_the_line_reaches_the_page_with_its_comparison(self):
        board = render.build_board(a_snapshot())
        area = next(a for a in board["panels"][0]["areas"] if a["key"] == "plan")
        self.assertTrue(area["promises"][0]["checked"].strip())
        self.assertEqual(area["promises"][0]["link"], "unknown")

    def test_the_page_says_the_comparison_failed_and_not_that_no_task_exists(self):
        page = Path(render.TEMPLATE).read_text()
        self.assertIn("связь с задачей не установлена", page)
        rule = page.split("const AREA_RULE")[1].split("};")[0]
        self.assertIn("не удалось наблюсти задачу", rule)
        self.assertNotIn("в которых не названо ни одной задачи", rule)

    def test_a_line_shown_without_its_comparison_is_refused(self):
        # The same rule the plates live under: a caption the board cannot back
        # with a named observation must not reach the page.
        snapshot = a_snapshot()
        snapshot["products"][0]["promises"] = [a_promise(checked="  ")]
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(snapshot)

    def test_a_bare_string_promise_is_refused(self):
        snapshot = a_snapshot()
        snapshot["products"][0]["promises"] = ["строка без сверки"]
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(snapshot)


class TheOtherProductOwner(unittest.TestCase):
    """The sixth question: two instances of the product owner, one queue.

    On 2026-08-06 the owner in the chat and the owner woken by
    `product-thread@<тред>.timer` created two pairs of duplicate tasks —
    790/792 and 791/793 — within the hour, because neither could see the
    other's queue.
    """

    def test_a_named_instance_without_an_observation_is_refused(self):
        broken = a_snapshot()
        broken["owners_awake"] = [an_owner(src="  ")]
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(broken)

    def test_an_instance_that_cannot_say_which_trees_it_could_take_is_refused(self):
        # «Уступить всем» is how three of four directions went mute on
        # 2026-08-07, so an instance with no answer to «какое дерево» may not
        # reach the strip at all.
        broken = a_snapshot()
        broken["owners_awake"] = [an_owner(worktrees=None)]
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(broken)

    def test_it_is_observed_from_a_command_line_and_never_from_a_transcript(self):
        source = Path(state.__file__).read_text()
        body = code_only(source[source.index("def owner_wakeups"):source.index("def build(")])
        self.assertIn("cmdline", body)
        self.assertEqual(state.PROC, Path("/proc"))
        # The prose above this function says «no transcript» in as many words,
        # so the claim is checked against the code with the prose removed.
        for forbidden in ("transcript", "session.jsonl", ".claude/projects"):
            self.assertNotIn(forbidden, body)

    def test_the_thread_is_read_from_the_tick_s_own_argv(self):
        """Found by driving the page, not by reading the code.

        The first cut searched every argument for the substring
        `thread_tick.py` and then guessed the thread from the last non-flag
        token. A `bash -c` wrapper carries the whole command in one argument, so
        it matched the shell and put «/bin/bash» on the strip as the name of a
        direction. The script has to be an argument of its own, and the thread
        is the argument after it — which is exactly what the timer passes.
        """
        self.assertEqual(thread_of(["/usr/bin/python3", "scripts/thread_tick.py",
                                    "platform"]), "platform")
        self.assertEqual(thread_of(["python3", "/opt/x/scripts/thread_tick.py",
                                    "--force", "product"]), "product")
        self.assertIsNone(thread_of(["/bin/bash", "-c",
                                     "cd /opt && python3 scripts/thread_tick.py client"]))

    def test_this_very_wake_up_mechanism_is_the_one_observed(self):
        # Whatever else changes, the thing being looked for is the tick script,
        # because that is what the timer starts.
        self.assertIn("thread_tick.py", Path(state.__file__).read_text())

    def test_the_strip_warns_before_a_task_is_created_not_after(self):
        snapshot = a_snapshot()
        snapshot["owners_awake"] = [an_owner(worktrees=["/opt/projects/example-product"])]
        board = render.build_board(snapshot)
        self.assertEqual(len(board["owners_awake"]), 1)
        self.assertIn("сверьтесь с его очередью, прежде чем заводить задачу", a_page(snapshot))

    def test_the_console_owner_is_observed_too(self):
        """Why the field was empty on every board the user ever opened.

        `ticked_thread` matches the tick script, and an interactive `claude`
        never runs it — so the only instance that could ever appear was one that
        lives about a second per twenty minutes. The console owner, which is the
        *other* half of the pair that created 790/792 and 791/793, ran for hours
        and matched nothing. Observed on 2026-08-07 against the real process
        table: pid 2581045 was `claude --name product-owner` in the owner's own
        tree, and `owner_wakeups()` returned an empty list.
        """
        self.assertEqual(state.session_owner(
            ["claude", "--name", "product-owner", "--add-dir", "/path/to/projects"],
            state.HOME), "session")
        self.assertEqual(state.session_owner(
            ["node", "/usr/local/bin/codex", "-C", str(state.HOME),
             state.OWNER_PROMPT_MARKER], Path("/root")), "session")
        # The owner agent a tick started runs the same CLI from the same
        # directory. It is a second owner, but calling it «продакт в консоли»
        # would name the wrong thing, and this board is built on not doing that.
        self.assertEqual(state.session_owner(
            ["/usr/local/bin/claude", "--print", "--name", "product-owner-background"],
            state.HOME, [["python3", "scripts/thread_tick.py", "process"]]), "woken")
        self.assertEqual(state.session_owner(
            ["/usr/local/bin/claude", "--print", "--name", "product-owner-background"],
            state.HOME, [["python3", "mail_product_owner.py", "_worker"]]), "mail")
        # A child run of a task sits in its task's repository. It is a run, is
        # already on the board as one, and is not a second owner.
        self.assertIsNone(state.session_owner(
            ["claude", "--print"], Path("/path/to/task-agent")))
        self.assertIsNone(state.session_owner(
            ["node", "/usr/local/bin/codex", "exec", "--json"], state.HOME))
        self.assertIsNone(state.session_owner(["bash", "-lc", "claude"], state.HOME))

    def test_a_wrapper_that_merely_carries_the_tick_is_not_the_tick(self):
        """`timeout 600 python3 scripts/thread_tick.py process` is `timeout`.

        The script is an argument of its own there, so argv alone says yes —
        and that wrapper was seen counted as a second owner while this change
        was being made. The executable has to be a Python interpreter too, which
        is what `product-thread@<тред>.service` actually starts.
        """
        argv = ["timeout", "600", "python3", "scripts/thread_tick.py", "process"]
        self.assertEqual(thread_of(argv), "process")
        self.assertFalse(state.runs_the_tick(argv, Path("/usr/bin/timeout")))
        self.assertTrue(state.runs_the_tick(
            ["/usr/bin/python3", "/opt/x/scripts/thread_tick.py", "process"],
            Path("/usr/bin/python3.12")))

    def test_each_instance_says_which_trees_it_could_occupy(self):
        # The strip is read before a task is created, and «может занять» is the
        # part that decides whether the reader has to stop at all.
        snapshot = a_snapshot()
        snapshot["owners_awake"] = [an_owner(kind="session", thread=None,
                                             worktrees=["/opt/product-owner"])]
        page = a_page(snapshot)
        self.assertIn("продакт в консоли", page)
        self.assertIn("может занять", page)

        snapshot["owners_awake"] = [an_owner(kind="mail", thread=None,
                                              worktrees=["/opt/product-owner"])]
        page = a_page(snapshot)
        self.assertIn("почтовое пробуждение продакта", page)


class ThreadTaskSelection(unittest.TestCase):
    """Направление забирает свои задачи, а не всё, что похоже по буквам."""

    CONFIG = {"threads": {
        "client": {"title": "Клиент (бот и приложение)", "task_search": ["max"]},
        "platform": {"title": "Платформа",
                          "projects": ["example-platform"]},
    }}

    def test_a_declared_project_outranks_another_direction_s_search_term(self):
        """#1172 «…забирает разумный максимум…» — задача Платформа.

        Её слаг `1172-…-takes-max-at-once` совпадает с поисковой страховкой
        «Клиента», и до круга 7 эта колонка называла живой прогон «Платформы»
        своей текущей работой (ревью круга 6). Связь с проектом одна, и она
        решает.
        """
        deep = {"id": 1172, "path": "tasks/1172-…-takes-max-at-once",
                "projects": ["data/projects/example-platform/status.md"]}
        unlinked = {"id": 1171, "path": "tasks/1171-max-runtime", "projects": []}

        def queried(args, limit=60):
            return [deep] if args[0] == "--project" else [deep, unlinked]

        with (mock.patch.object(state, "load_config", return_value=self.CONFIG),
              mock.patch.object(state, "query_tasks", side_effect=queried)):
            client = state.thread_tasks(self.CONFIG["threads"]["client"])
            owner = state.thread_tasks(self.CONFIG["threads"]["platform"])

        self.assertEqual([task["id"] for task in client], [1171])
        self.assertEqual([task["id"] for task in owner], [1172])

    def test_a_thread_keeps_a_search_hit_linked_to_its_own_project(self):
        """Страховка не отменяется связью: она отменяется только чужой связью."""
        thread = {"task_search": ["max"], "projects": ["example-platform"]}
        task = {"id": 1172, "path": "tasks/1172",
                "projects": ["data/projects/example-platform/status.md"]}
        with (mock.patch.object(state, "load_config", return_value=self.CONFIG),
              mock.patch.object(state, "query_tasks", return_value=[task])):
            self.assertEqual([row["id"] for row in state.thread_tasks(thread)], [1172])


class LongLivedTaskProcesses(unittest.TestCase):
    """A closed task's daemon is still work the thread must make visible."""

    def empty_registry(self):
        """A live-run registry that knows nothing, stated rather than installed.

        The registry is the personal assistant's own adapter and an installation
        may not have it at all; a test that borrows the installed one passes
        only where it is installed.
        """
        registry = mock.Mock()
        registry.live_run_processes.return_value = []
        return mock.patch.object(state, "RUN_REGISTRY", registry)

    def test_thread_membership_comes_from_the_task_s_linked_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = root / "task-agent"
            cwd = client / "tasks" / "692-product" / "artifacts"
            cwd.mkdir(parents=True)
            repo = root / "example-engine"
            proc = root / "proc"
            entry = proc / "101"
            (entry / "fd").mkdir(parents=True)
            (entry / "fdinfo").mkdir()
            (entry / "cmdline").write_bytes(
                (str(repo / ".venv/bin/python") + "\0field_probe.py\0").encode())
            (entry / "cwd").symlink_to(cwd)
            (entry / "exe").symlink_to("/usr/bin/python3")

            task = {"id": 692, "slug": "692-product", "title": "probe",
                    "status": "completed",
                    "projects": ["data/projects/example-product/project.md"]}
            with (mock.patch.object(state, "PROC", proc),
                  mock.patch.object(state, "REPO", client),
                  self.empty_registry(),
                  mock.patch.object(state, "thread_tasks", side_effect=[[], [task]]),
                  mock.patch.object(state.time, "time", return_value=300)):
                process_thread = state.long_lived_processes(
                    {"projects": ["process-visualization"],
                     "repos": [str(client)]})
                product_thread = state.long_lived_processes(
                    {"projects": ["example-product"], "repos": [str(repo)]})

            self.assertEqual(process_thread, [])
            self.assertEqual([(item["pid"], item["task"], item["repo"])
                              for item in product_thread], [(101, 692, str(repo))])

    def test_a_registered_live_chain_is_not_detached_when_the_task_is_terminal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = root / "task-agent"
            cwd = client / "tasks" / "839-long-lived-process-inventory"
            cwd.mkdir(parents=True)
            repo = root / "product-owner"
            proc = root / "proc"
            proc.mkdir()

            for pid, parent in ((101, 1), (102, 101)):
                entry = proc / str(pid)
                (entry / "fd").mkdir(parents=True)
                (entry / "fdinfo").mkdir()
                (entry / "cmdline").write_bytes(
                    (str(repo / ".venv/bin/python") + "\0owner.py\0" +
                     str(cwd) + "\0").encode())
                (entry / "cwd").symlink_to(repo)
                (entry / "exe").symlink_to("/usr/bin/python3")
                (entry / "status").write_text(f"Name:\towner\nPPid:\t{parent}\n")

            catalogue = [{"id": 839, "slug": cwd.name, "title": "owner",
                          "status": "completed"}]
            registry = mock.Mock()
            registry.live_run_processes.return_value = [
                {"role": "child", "pid": 101, "evidence": "identity_match"}]
            with (mock.patch.object(state, "PROC", proc),
                  mock.patch.object(state, "REPO", client),
                  mock.patch.object(state, "RUN_REGISTRY", registry),
                  mock.patch.object(state.time, "time", return_value=300)):
                processes = state.long_lived_processes(
                    {"repos": [str(repo)]}, catalogue)

            self.assertEqual(processes, [])
            registry.live_run_processes.assert_called_once_with(cwd)

    def test_missing_run_registry_suppresses_the_inventory(self):
        with mock.patch.object(state, "RUN_REGISTRY", None):
            with self.assertRaisesRegex(
                    state.ProcessInventoryUnavailable,
                    "реестр живых прогонов этой установки недоступен"):
                state.long_lived_processes({"repos": []}, [])

    def test_thread_state_names_an_unavailable_inventory_instead_of_claiming_zero(self):
        observed = {
            "threads": [{"title": "Process", "products": [], "tasks": [],
                         "repos": [], "task_count": 0}],
            "owners_awake": [],
        }
        with (mock.patch.object(thread, "load_thread", return_value={"repos": []}),
              mock.patch.object(thread.observer, "build", return_value=observed),
              mock.patch.object(
                  thread, "process_inventory",
                  side_effect=state.ProcessInventoryUnavailable("registry unavailable"))):
            report = thread.build("process")

        self.assertEqual(report["long_lived_processes"], [])
        self.assertEqual(report["long_lived_processes_observation"], {
            "available": False, "reason": "registry unavailable"})

    def test_external_observation_attributes_outputs_and_duplicate_instances(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = root / "task-agent"
            task_root = client / "tasks" / "692-product"
            cwd = task_root / "artifacts"
            cwd.mkdir(parents=True)
            output = cwd / "snapshots.jsonl"
            output.write_bytes(b"one\n")
            os.utime(output, (200, 200))
            repo = root / "example-engine"
            proc = root / "proc"
            proc.mkdir()

            for pid in (101, 102):
                entry = proc / str(pid)
                (entry / "fd").mkdir(parents=True)
                (entry / "fdinfo").mkdir()
                (entry / "cmdline").write_bytes(
                    (str(repo / ".venv/bin/python") + "\0field_probe.py\0").encode())
                (entry / "cwd").symlink_to(cwd)
                (entry / "exe").symlink_to("/usr/bin/python3")
                os.utime(entry, (100, 100))

            catalogue = [{"id": 692, "slug": "692-product", "title": "probe",
                          "status": "completed"}]
            with (mock.patch.object(state, "PROC", proc),
                  mock.patch.object(state, "REPO", client),
                  self.empty_registry(),
                  mock.patch.object(state.time, "time", return_value=300)):
                processes = state.long_lived_processes(
                    {"repos": [str(repo)]}, catalogue)

            self.assertEqual([item["pid"] for item in processes], [101, 102])
            self.assertTrue(all(item["duplicate"] for item in processes))
            self.assertEqual({item["duplicate_count"] for item in processes}, {2})
            self.assertEqual(processes[0]["command"], "field_probe.py")
            self.assertEqual(processes[0]["outputs"][0]["path"], str(output))
            self.assertIn("mtime", processes[0]["outputs"][0]["observed_by"])

    def test_growth_requires_two_observed_sizes(self):
        current = [{"pid": 7, "since": "start", "outputs": [{
            "path": "/tmp/out", "size": 15, "growing": None,
            "growth_bytes": None, "growth_src": "first", "direct": False}],
            "write_chars": 25}]
        previous = {"observed_at": "before", "processes": [{
            "pid": 7, "since": "start", "write_chars": 20,
            "outputs": [{"path": "/tmp/out", "size": 10}]}]}
        observed = state.process_growth(current, previous)
        self.assertTrue(observed[0]["outputs"][0]["growing"])
        self.assertEqual(observed[0]["outputs"][0]["growth_bytes"], 5)

    def test_age_uses_the_kernel_start_tick_not_proc_directory_mtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            entry = proc / "101"
            entry.mkdir()
            fields = ["S", "1", *(["0"] * 17), "250"]
            (entry / "stat").write_text("101 (worker name) " + " ".join(fields))
            (proc / "stat").write_text("cpu 1 2 3\nbtime 1000\n")
            os.utime(entry, (9000, 9000))
            with (mock.patch.object(state, "PROC", proc),
                  mock.patch.object(state.os, "sysconf", return_value=100)):
                self.assertEqual(state.process_started(entry), 1002.5)


class WhenTheOwnerLooksAgain(unittest.TestCase):
    """«Когда продакт проверит статус в следующий раз и чем кончилась прошлая.»

    A person who opens the board on a column of zeros has exactly one question,
    and until this line existed the board could not answer it. The source is
    what the tick wrote into `state/threads/<тред>.json` at the moment of the
    check — never the prose of the agent that check woke.
    """

    def test_the_panel_carries_the_check_through_untouched(self):
        board = render.build_board(a_snapshot())
        self.assertEqual(board["panels"][0]["check"], a_check())
        self.assertEqual(board["panels"][0]["next_check"], a_next_check())

    def test_a_direction_nobody_has_ever_checked_says_so(self):
        snapshot = a_snapshot()
        snapshot["threads"][0]["check"] = None
        schema.validate_snapshot(snapshot)
        self.assertIn("проверок этого направления ещё не было", a_page(snapshot))

    def test_a_direction_nobody_has_checked_still_says_when_it_will_be(self):
        """The two answers come from two observations, so one may be missing.

        Before the split «когда проверит в следующий раз» lived inside the
        wake-up record, so a direction with no record at all could not be told
        when its timer fires — even though systemd knew.
        """
        snapshot = a_snapshot()
        snapshot["threads"][0]["check"] = None
        page = a_page(snapshot)
        node = page[page.index("function checkNode"):page.index("function drawBoard")]
        self.assertIn("if (nextCheck) {", node)
        self.assertLess(node.index("nextCheck.at"), node.index("if (!check)"))

    def test_the_page_shows_the_next_check_the_last_one_and_its_outcome(self):
        page = a_page()
        self.assertIn("следующая проверка", page)
        self.assertIn("прошлая", page)
        self.assertIn("check.outcome", page)

    def test_an_outcome_without_an_observation_is_refused(self):
        broken = a_snapshot()
        broken["threads"][0]["check"] = a_check(outcome_src="  ")
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(broken)

    def test_an_empty_outcome_is_refused(self):
        broken = a_snapshot()
        broken["threads"][0]["check"] = a_check(outcome="")
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(broken)

    def test_an_unknown_next_check_still_owes_the_reason(self):
        # «Следующая проверка: —» is as much a claim as a time is.
        broken = a_snapshot()
        broken["threads"][0]["next_check"] = a_next_check(at=None, src="")
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(broken)
        broken["threads"][0]["next_check"] = a_next_check(
            at=None, src="systemd о расписании не ответил")
        schema.validate_snapshot(broken)

    def test_the_reason_for_an_unknown_next_check_is_readable_without_a_pointer(self):
        """Finding MEDIUM-1 of review 900.

        The reason used to be printed only into the `title` attribute, which a
        touch device never shows: the line promised an explanation and then
        handed it to pointer users only.
        """
        page = a_page()
        node = page[page.index("function checkNode"):page.index("function drawBoard")]
        why = node[node.index("if (!nextCheck.at)"):]
        self.assertIn('why.textContent = "наблюдено: " + nextCheck.src', why)
        self.assertIn("node.appendChild(why)", why)

    def test_the_next_check_is_what_systemd_armed_and_never_a_calendar_minute(self):
        """Кросс-ревью 881: календарная минута, показанная как проверка, — ложь.

        `NextElapseUSecRealtime` is empty exactly while the paired service runs,
        which is exactly when a computed `OnCalendar` minute may be wrong: a
        check that outlives the twenty-minute step has lost a firing systemd
        already knows about and the calendar does not. So the fallback is gone,
        and the answer there is «неизвестно» with what was seen instead.
        """
        asked = []

        def fake(command):
            asked.append(command)
            if "--property=NextElapseUSecRealtime" in command:
                return "Fri 2026-08-07 18:40:00 CEST\n"
            return ""

        with mock.patch.object(state, "systemd", side_effect=fake):
            answer = state.next_check("process")
        self.assertTrue(answer["at"].startswith("2026-08-07T18:40:00"))
        self.assertIn("NextElapseUSecRealtime", answer["src"])

        asked.clear()

        def unarmed(command):
            asked.append(command)
            if "--property=ActiveState" in command:
                return "activating\n"
            return ""

        with mock.patch.object(state, "systemd", side_effect=unarmed):
            answer = state.next_check("client")
        self.assertIsNone(answer["at"])
        self.assertIn("не взведён", answer["src"])
        self.assertFalse(self.calendar_asked(asked))

        # And the same when systemd says nothing at all about either unit: still
        # unknown, still with a reason, still without a computed minute.
        asked.clear()
        with mock.patch.object(state, "systemd", side_effect=lambda c: asked.append(c)):
            answer = state.next_check("product")
        self.assertIsNone(answer["at"])
        self.assertTrue(answer["src"].strip())
        self.assertFalse(self.calendar_asked(asked))

    @staticmethod
    def calendar_asked(commands: list[list[str]]) -> bool:
        """Whether the schedule was computed instead of read off the arming.

        Two shapes of the same substitution: `systemd-analyze calendar`, and the
        `next_elapse=` systemd prints inside `TimersCalendar`. Neither is what
        the timer is set to do.
        """
        flat = " ".join(" ".join(command) for command in commands)
        return "systemd-analyze" in flat or "TimersCalendar" in flat

    def test_the_next_check_is_asked_when_the_board_is_built_not_by_the_tick(self):
        """Finding HIGH-1 of review 900, held as a rule rather than as a value.

        The tick runs *as* `product-thread@<тред>.service`, so a
        `NextElapseUSecRealtime` it reads about its own timer is empty by
        construction — which is how all four live state files came to carry
        `next_at=null` while systemd was holding real future times. The
        observation belongs to whoever answers «что сейчас»: the collector.
        """
        written = tick.__file__ and Path(tick.__file__).read_text()
        self.assertNotIn("next_at", written)
        self.assertNotIn("NextElapseUSecRealtime", written)
        collector = Path(state.__file__).read_text()
        self.assertIn('"next_check": next_check(key)', collector)
        self.assertIn("NextElapseUSecRealtime", collector)

    def test_a_reason_without_an_observation_is_refused(self):
        broken = a_snapshot()
        broken["threads"][0]["check"] = a_check(reasons=[a_reason(src=" ")])
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(broken)

    def test_the_reasons_are_shown_when_the_direction_stood_still(self):
        # The rendered fact is measured in a browser by
        # `tasks/871-process-visualization-idle/shot.py`; what is held here is
        # the rule that decides it, and that a direction standing still for no
        # observable reason is not drawn as another grey line.
        #
        # Задача 1163 перенесла сами причины из строки проверки в строку
        # состояния направления над ней: «почему стоит» — один вопрос, и отвечать
        # на него дважды в одной колонке значит писать один ответ два раза.
        # Правило отбора причин осталось тем же и переехало вместе с ними.
        page = a_page()
        node = page[page.index("function stateNode"):page.index("function voidsNode")]
        self.assertIn("for (const reason of idleReasons(panel.check)) whys.push(reason)", node)
        self.assertIn('reason.code === "none_observed" ? " alarm" : ""', node)
        self.assertIn('"наблюдено: " + reason.src', node)

    def test_a_direction_that_started_work_owes_no_excuse(self):
        """Found by driving the page, not by reading the code.

        The first cut showed the reasons whenever the queue was non-empty and
        nothing was live *at the moment of the check* — which is every wake-up
        that then went on to start something. On the live board that printed
        «запустил 3 — задачи 823, 872, 873» and «причина простоя не
        наблюдается» in the same column.

        The gate is `started`: an empty list is «не стал запускать», `null` is
        «ещё не решил», and a non-empty one owes nobody an excuse. Asserted on
        the drawing code, because the payload carries every reason into the page
        whether or not the page draws it — the rendered fact is measured by
        `tasks/871-process-visualization-idle/shot.py` in a real browser.

        Правило живёт в `idleReasons` с задачи 1163: причины показывает строка
        состояния направления, а решает, показывать ли их вообще, по-прежнему это
        условие и только оно.
        """
        page = a_page()
        node = page[page.index("function idleReasons"):page.index("function continueNode")]
        self.assertIn("!Array.isArray(check.started) || check.started.length", node)

    def test_the_tick_says_what_it_started_and_when_it_has_not_decided_yet(self):
        # `None` while the owner is still running, a list once it has returned:
        # the two are different answers and the board acts on the difference.
        before = tick.snapshot(self.report(pickup=[861]))
        after = tick.snapshot(self.report(pickup=[861], live=[871]))
        self.assertEqual(tick.started_runs(before, after), [871])
        self.assertEqual(tick.started_runs(before, before), [])
        self.assertEqual(tick.started_runs(before, None), [])
        self.assertIn('"started": started_runs(current, final) if done and woke else None',
                      Path(tick.__file__).read_text())

    def report(self, pickup=(), live=()):
        return TheWakeUpSeesTheQueueMove.report(self, pickup=pickup, live=live)

    def test_a_stale_record_says_so_instead_of_taking_the_board_down(self):
        """A state file is runtime state and the next tick rebuilds it.

        Adding `started` to the contract instantly made four records on disk
        unreadable, and a collector that merely raised would have left the whole
        board dead for the twenty minutes until the next wake-up. Saying «запись
        не читается» is the third answer, and it is not «проверок не было»:
        that would be a false claim about the world.
        """
        with tempfile.TemporaryDirectory() as box:
            root = Path(box)
            stale = dict(a_check())
            del stale["started"]
            (root / "process.json").write_text(json.dumps(
                {"thread": "process", "updated_at": "2026-08-07T15:20:03+00:00",
                 "check": stale}, ensure_ascii=False))
            original = state.THREAD_STATE
            try:
                state.THREAD_STATE = root
                check = state.thread_check("process")
                missing = state.thread_check("product")
            finally:
                state.THREAD_STATE = original
        self.assertIsNone(missing)                     # nobody has ever checked
        self.assertIn("не читается", check["outcome"])  # somebody has, unreadably
        self.assertEqual(check["at"], "2026-08-07T15:20:03+00:00")
        snapshot = a_snapshot()
        snapshot["threads"][0]["check"] = check
        schema.validate_snapshot(snapshot)              # and it is still a legal record

    def test_the_board_reads_the_record_and_never_rebuilds_it(self):
        # The renderer may not reach a disk, and the collector may not recompute
        # an instant it is only carrying: both halves are one claim.
        self.assertIn('"check": thread["check"]', Path(render.__file__).read_text())
        collector = Path(state.__file__).read_text()
        self.assertIn('"check": thread_check(key)', collector)


class OnePlateHasNothingToBeOlderThan(unittest.TestCase):
    def test_the_oldest_caption_is_a_comparison_and_needs_two(self):
        """The user's words: «Если в группе одна задача, слово „старшая“ не
        показывать». The age of the only plate is on the plate itself."""
        page = a_page()
        node = page[page.index("function areaNode"):page.index("function panelNode")
                    if "function panelNode" in page else page.index("function channelsNode")]
        self.assertIn("area.count > 1", node)


class TheCardKeepsThePlaceOnARefresh(unittest.TestCase):
    def test_a_poll_does_not_send_the_reader_back_to_the_top_of_the_card(self):
        """Live mode polls every ten seconds and the card was redrawn from its
        top each time, which is the same interruption `location.reload()` was."""
        page = a_page()
        redraw = page[page.index("function applyFresh"):page.index("/* ---------- two screens")]
        self.assertIn("openCard(again, true, cardReturn)", redraw)
        opener = page[page.index("function openCard"):page.index("function closeCard")]
        self.assertIn("keepScroll ? wasAt : 0", opener)


class DrillDown(unittest.TestCase):
    """Opening a plate without leaving the page, and without a second door in."""

    def test_board_and_index_cards_travel_with_the_document(self):
        page = a_page()
        body = page[page.index("if (LIVE)"):]
        self.assertEqual(page.count("fetch("), body.count("fetch("))
        self.assertIn("function openCard", page)
        self.assertNotIn("fetch(", page[page.index("function openCard"):page.index("function closeCard")])
        self.assertIn("boardPlate(item.task) || item.card", page)

    def test_everything_the_user_asked_the_card_for_is_on_it(self):
        """The same answers, in the order a person reads them.

        The card used to be titled by where the fields came from — «Прогон»,
        «Последняя запись прогресса», «Движение артефактов» — with the runner and
        the sandbox standing second, above the review and the delivery, and four
        sections saying «нечего показать» before anything a reader had opened it
        for. Task 864 reordered it into «что происходит → почему → что дальше →
        результат», so the headings are these; every answer the user asked the
        card for is still on it, and this test names both halves.
        """
        page = a_page()
        for asked in ("Своими словами", "Что происходит", "Почему так",
                      "Что дальше", "Результат и доставка",
                      "Технические детали прогона"):
            self.assertIn(asked, page, asked)
        # The answers themselves, wherever the order put them.
        for answer in ("VERDICT_RU", "гейты verification.md", "Доставлено",
                       "Последнее движение артефактов", "Раннер", "Песочница",
                       "содержимое не читается"):
            self.assertIn(answer, page, answer)

    def test_the_card_shows_the_task_in_the_words_of_the_person_who_asked(self):
        """Everything else on the card is state; this is what the task is.

        The description was collected by nobody and shown by nothing, so a card
        could say the run was `standard`, the sandbox `danger-full-access` and
        the step `Dev-pipeline owner attempt completed` without ever saying what
        the task was about.
        """
        page = a_page()
        self.assertIn("d.summary", page)
        self.assertIn("раздел «Summary» в task.md", page)

    def test_an_empty_section_is_not_drawn_at_all(self):
        """«Нечего показать» four times, the first of them fourth on the card."""
        page = a_page()
        self.assertNotIn("нечего показать: на диске об этом ничего нет", page)
        self.assertIn("return build(s) ? s : null;", page)

    def test_values_of_our_own_enumerations_reach_the_person_in_russian(self):
        page = a_page()
        for pair in ('in_progress: "в работе"', '"danger-full-access": "полный доступ к диску"',
                     'rework: "на доработку"', 'standard: "обычный прогон одним ребёнком"'):
            self.assertIn(pair, page, pair)
        # A machine token inside otherwise human text, and a fixed phrase the
        # runner writes itself. Free text that matches neither is passed through
        # rather than guessed at.
        self.assertIn("decision=deliver", page)
        self.assertIn("Standard child completed and durable gates passed", page)
        self.assertIn("function human(text)", page)

    def test_the_card_lists_names_and_never_content(self):
        page = a_page()
        self.assertIn("содержимое не читается, транскрипты не показываются", page)

    def test_the_detail_a_collector_produces_is_the_detail_the_card_expects(self):
        detail = render.plate(a_task())["detail"]
        for field in schema.DETAIL_FIELDS:
            self.assertIn(field, detail)
        for field in ("gates", "repo", "progress", "refusal", "workflow"):
            self.assertIn(field, detail)

    def test_a_movement_named_without_a_source_is_refused(self):
        broken = a_snapshot([a_task(detail={"moved": "2026-08-06T12:00:00+00:00",
                                            "moved_src": None})])
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(broken)


class LiveWithoutLosingThePlace(unittest.TestCase):
    def test_a_refresh_redraws_in_place_instead_of_reloading(self):
        """`location.reload()` is right for a picture and wrong for a tool.

        Live mode polls every ten seconds, so a reload throws away the open
        card, the scroll position and the fold state of every area each time a
        repository somewhere else goes dirty — that is, it interrupts the person
        reading a task in order to show them something they were not looking at.
        """
        page = a_page()
        # The poll branch itself, not the prose around it: the old call is named
        # in a comment there on purpose, and a literal search would find it.
        poll = page[page.index("if (LIVE)"):]
        self.assertIn("applyFresh(fresh)", poll)
        self.assertNotIn("location.reload();", poll)

    def test_the_open_card_survives_a_refresh_on_the_same_task(self):
        page = a_page()
        redraw = page[page.index("function applyFresh"):page.index("/* ---------- two screens")]
        self.assertIn("p.task === wanted", redraw)
        self.assertIn("else closeCard()", redraw)

    def test_an_age_that_merely_ticks_does_not_trigger_a_redraw(self):
        # Every derived age has to be out of the digest or live mode redraws
        # every ten seconds forever and says nothing.
        self.assertIn("moved_age_seconds", render.TICKING)
        data = {"snapshot": a_snapshot([a_task(
            detail={"moved": "2026-08-06T12:00:00+00:00", "moved_src": "mtime",
                    "moved_age_seconds": 10})])}
        other = {"snapshot": a_snapshot([a_task(
            detail={"moved": "2026-08-06T12:00:00+00:00", "moved_src": "mtime",
                    "moved_age_seconds": 900})])}
        self.assertEqual(render.without_ticking(data), render.without_ticking(other))


class OneObserver(unittest.TestCase):
    """Two observers of one disk is why there was no trustworthy source.

    `thread_state.py` had its own `pid_alive`, its own reader of `status.json`
    and `progress.json`, its own `repo_state` and its own idea of «требует
    внимания». The wake-up and the board could therefore disagree about
    liveness, freshness and status, and neither could be checked against the
    other because neither was the original.
    """

    def source(self) -> str:
        return Path(state.HOME / "scripts" / "thread_state.py").read_text()

    def test_the_wake_up_reads_the_collector_rather_than_the_disk(self):
        source = self.source()
        self.assertIn("import process_map_state as observer", source)
        self.assertIn("observer.build(False, only=name)", source)

    def test_there_is_no_second_answer_to_whether_a_process_is_alive(self):
        # `os.kill(pid, 0)` answers «some process holds this number», and PIDs
        # are reused: after a wrap an unrelated process counted as a live run.
        source = code_only(self.source())
        self.assertNotIn("os.kill", source)
        self.assertNotIn("def pid_alive", source)

    def test_there_is_no_second_reader_of_the_run_files(self):
        source = self.source()
        for gone in ("status.json", "progress.json", "runner.json"):
            self.assertNotIn(f'/ "{gone}"', source)

    def test_there_is_no_second_git_reader(self):
        self.assertNotIn("subprocess.run", self.source())

    def test_one_direction_can_be_observed_without_paying_for_four(self):
        # A wake-up asks about one thread and must pay for one thread, or the
        # single observer would make every tick four times dearer.
        source = Path(state.__file__).read_text()
        self.assertIn("def build(anonymize: bool, only: str | None = None,", source)
        self.assertIn("include_task_cards: bool = False", source)
        self.assertIn("if only and key != only:", source)

    def test_an_unknown_thread_is_still_refused_loudly(self):
        with self.assertRaises(SystemExit):
            state.build(False, only="no-such-thread")


class DoneButNeverShown(unittest.TestCase):
    """Part 3 of task 817: finished work whose result nobody was shown.

    Task 783 finished at 16:14 with a 441 KB report in its `deliverables/`, and
    the report lay on the server for about an hour. Its receipts were
    `attempt_started` 18447 and `attempt_completed` 18479 — both about the life
    of the run, neither about the document. Nobody noticed until the user said
    «по выполненным задачам я не видел документов в почте или в телеграме».
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.task = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def like_783(self) -> Path:
        """The state 783 was actually in when its report was lying on the server."""
        box = self.task / "deliverables"
        box.mkdir()
        (box / "product-portfolio-history-sources-2026-08-06.html").write_text("x" * 441097)
        (box / "manifest.json").write_text("[]")
        pipeline = self.task / "dev-pipeline"
        pipeline.mkdir()
        (pipeline / "notification-receipts.jsonl").write_text(
            '{"event_id": "e1", "kind": "attempt_started", "message_id": 18447, '
            '"recorded_at": "2026-08-06T12:03:55+00:00", "schema_version": "1.0"}\n'
            '{"event_id": "e2", "kind": "attempt_completed", "message_id": 18479, '
            '"recorded_at": "2026-08-06T14:19:28+00:00", "schema_version": "1.0"}\n')
        return self.task

    def test_the_case_of_783_is_caught(self):
        hand = state.handoff(self.like_783())
        self.assertFalse(hand["delivered"])
        self.assertEqual(hand["name"], "deliverables/product-portfolio-history-sources-2026-08-06.html")
        self.assertEqual(hand["bytes"], 441097)
        self.assertEqual(state.board_area("completed", [], False, None, undelivered=True),
                         "undelivered")

    def test_lifecycle_receipts_are_not_delivery_of_a_document(self):
        # Every receipt in the whole repository is a lifecycle event, so «квитанция
        # есть» has never once meant «документ доставлен».
        self.assertIsNone(state.delivery_receipt(self.like_783()))

    def test_the_delivery_note_closes_it(self):
        task = self.like_783()
        (task / "delivery.md").write_text("# Доставка\nTelegram message_id=18491\n")
        hand = state.handoff(task)
        self.assertTrue(hand["delivered"])
        self.assertIn("delivery.md", hand["delivered_src"])

    def test_the_note_the_product_owner_writes_by_hand_closes_it_too(self):
        """Two names, one convention — channel, message identifier, sha256.

        The audit of 835 wrote `delivery.md` 71 times; the product owner writes
        `product-owner-delivery.md` when they send a document themselves, and on
        2026-08-06 they wrote six — including all three documents of the decision
        this observation exists to check. Knowing only the first name called
        three deliveries undelivered while the letters were in the mailbox.
        """
        task = self.like_783()
        (task / "product-owner-delivery.md").write_text(
            "# Доставка пользователю\n- 2026-08-06, письмо `18f0000000000005`\n")
        hand = state.handoff(task)
        self.assertTrue(hand["delivered"])
        self.assertIn("product-owner-delivery.md", hand["delivered_src"])

    def test_the_product_owner_decision_not_to_deliver_closes_it_too(self):
        task = self.like_783()
        (task / "product-owner-decision.md").write_text(
            "# Решение продакта\nОтдельным письмом не доставляется.\n")
        hand = state.handoff(task)
        self.assertTrue(hand["delivered"])
        self.assertIn("product-owner-decision.md", hand["delivered_src"])

    def test_the_absence_says_which_names_were_looked_for(self):
        # «Свидетельства нет» is only checkable if the reader is told what was
        # searched for; a bare «нет» is the claim that hid the six notes above.
        hand = state.handoff(self.like_783())
        for name in state.DELIVERY_NOTES:
            self.assertIn(name, hand["delivered_src"])

    def test_a_receipt_about_something_other_than_the_run_closes_it_too(self):
        task = self.like_783()
        with (task / "dev-pipeline" / "notification-receipts.jsonl").open("a") as handle:
            handle.write('{"kind": "deliverable_sent", "message_id": 18491, '
                         '"recorded_at": "2026-08-06T15:20:00+00:00"}\n')
        hand = state.handoff(task)
        self.assertTrue(hand["delivered"])
        self.assertIn("deliverable_sent", hand["delivered_src"])

    def two_documents(self, delivered: tuple[str, ...] = ("sent.txt",)) -> Path:
        """The fixture of review 843: one document sent, the larger one refused.

        The refused file is the bigger of the two, so it is the one the card
        names — and the board still called the task delivered on the strength of
        the receipt belonging to the other file.
        """
        box = self.task / "deliverables"
        box.mkdir()
        (box / "sent.txt").write_text("small")
        (box / "failed.txt").write_text("x" * 64)
        pipeline = self.task / "dev-pipeline"
        pipeline.mkdir()
        lines = ['{"kind": "attempt_completed", "message_id": 18479, '
                 '"recorded_at": "2026-08-06T14:19:28+00:00"}']
        for index, name in enumerate(delivered):
            digest = state._file_sha256(box / name)
            lines.append(f'{{"kind": "document_delivered", "document": "deliverables/{name}", '
                         f'"sha256": "{digest}", "message_id": {18480 + index}, '
                         f'"recorded_at": "2026-08-06T14:19:3{index}+00:00"}}')
        if "failed.txt" not in delivered:
            lines.append('{"kind": "document_delivery_refused", '
                         '"document": "deliverables/failed.txt", "sha256": "bb", '
                         '"message_id": null, "notice_message_id": 18481, '
                         '"recorded_at": "2026-08-06T14:19:31+00:00"}')
        (pipeline / "notification-receipts.jsonl").write_text("\n".join(lines) + "\n")
        return self.task

    def test_one_delivered_document_does_not_deliver_the_other_one(self):
        hand = state.handoff(self.two_documents())
        self.assertFalse(hand["delivered"])
        self.assertEqual(hand["missing"], ["deliverables/failed.txt"])
        self.assertIn("failed.txt", hand["delivered_src"])
        self.assertIn("1 из 2", hand["delivered_src"])
        self.assertEqual(state.board_area("completed", [], False, None, undelivered=True),
                         "undelivered")

    def test_a_receipt_for_every_document_closes_it(self):
        hand = state.handoff(self.two_documents(("sent.txt", "failed.txt")))
        self.assertTrue(hand["delivered"])
        self.assertEqual(hand["missing"], [])
        self.assertIn("2 из 2", hand["delivered_src"])

    def test_a_refusal_alone_is_not_delivery(self):
        task = self.like_783()
        with (task / "dev-pipeline" / "notification-receipts.jsonl").open("a") as handle:
            handle.write('{"kind": "document_delivery_refused", "document": '
                         '"deliverables/product-portfolio-history-sources-2026-08-06.html", '
                         '"message_id": null, "notice_message_id": 18491, '
                         '"recorded_at": "2026-08-06T15:20:00+00:00"}\n')
        hand = state.handoff(task)
        self.assertFalse(hand["delivered"])
        self.assertIsNone(state.delivery_receipt(task))
        # And the sentence says what was actually read: a journal that refused
        # the document is not a journal of lifecycle events only. The live probe
        # of task 1048 produced exactly this shape against the real bot.
        self.assertIn("document_delivery_refused", hand["delivered_src"])
        self.assertNotIn("несут только события", hand["delivered_src"])

    def test_a_document_rewritten_after_its_receipt_is_not_the_one_that_went(self):
        # The receipt carries the digest of what the sender actually sent, so a
        # file replaced afterwards is a document the person does not have.
        task = self.two_documents(("sent.txt", "failed.txt"))
        (task / "deliverables" / "failed.txt").write_text("y" * 64)
        hand = state.handoff(task)
        self.assertFalse(hand["delivered"])
        self.assertEqual(hand["missing"], ["deliverables/failed.txt"])

    def test_a_receipt_with_a_digest_and_no_name_still_belongs_to_one_document(self):
        # The sender keys its journal by digest, so a receipt carrying one is
        # correlatable even without the name and cannot close the whole set.
        task = self.two_documents(())
        digest = state._file_sha256(task / "deliverables" / "sent.txt")
        with (task / "dev-pipeline" / "notification-receipts.jsonl").open("a") as handle:
            handle.write(f'{{"kind": "document_delivered", "sha256": "{digest}", '
                         '"message_id": 18490, "recorded_at": "2026-08-06T14:20:00+00:00"}\n')
        hand = state.handoff(task)
        self.assertFalse(hand["delivered"])
        self.assertEqual(hand["missing"], ["deliverables/failed.txt"])

    def test_a_receipt_and_a_saved_attachment_can_close_a_set_together(self):
        # Two sources of truth for two documents: the contour sent one, the
        # product owner mailed the other by hand and the mirror recorded it.
        task = self.two_documents()
        refused = task / "deliverables" / "failed.txt"
        observed = {(refused.name, refused.stat().st_size): [
            {"channel": "email", "message_id": "gmail-2"}
        ]}
        hand = state.handoff(task, observed)
        self.assertTrue(hand["delivered"])
        self.assertEqual(hand["missing"], [])
        self.assertIn("email", hand["delivered_src"])

    def test_a_task_with_nothing_made_for_a_person_is_not_in_the_area(self):
        (self.task / "plan.md").write_text("# Plan\n")
        self.assertIsNone(state.handoff(self.task))
        self.assertEqual(state.board_area("completed", [], False, None, undelivered=False),
                         "done")

    def test_cancelled_work_is_not_done_but_not_delivered(self):
        """Finding MEDIUM-1 of review 826, on the live shape of task 669.

        669 is `status: cancelled` — «единственная незакрытая находка DR-001
        закрыта задачей 722… deliverables 669 заменены результатами 722» — and
        its `deliverables/` still holds `cycle112-report.html`. Files on disk
        plus a terminal status put it among 46 genuinely completed tasks as the
        47th entry of «Сделано, но не доставлено». Nobody is owed the document
        of work that was called off.
        """
        for status in ("cancelled", "superseded"):
            with self.subTest(status):
                self.assertEqual(
                    state.board_area(status, [], False, None, undelivered=True), "done")
        self.assertEqual(
            state.board_area("completed", [], False, None, undelivered=True), "undelivered")

    def test_a_cancelled_task_with_a_document_still_reads_as_terminal(self):
        # The narrowing touches one area only: cancelled work stands in «Сделано»
        # exactly where it stood before, and its files are still on the card.
        task = self.like_783()
        (task / "task.md").write_text(
            '---\nid: 669\nslug: "669-sv-ed-sr"\nstatus: "cancelled"\n'
            'status_detail: "Снято продактом 2026-08-04: закрыта задачей 722."\n---\n')
        hand = state.handoff(task)
        self.assertIsNotNone(hand)
        self.assertFalse(hand["delivered"])
        self.assertEqual(state.board_area("cancelled", [], False, None, undelivered=True),
                         "done")

    def test_a_report_written_straight_into_the_task_directory_counts(self):
        (self.task / "report.html").write_text("<html></html>")
        self.assertEqual(state.handoff(self.task)["name"], "report.html")

    def test_the_manifest_alone_is_not_a_document_for_a_person(self):
        box = self.task / "deliverables"
        box.mkdir()
        (box / "manifest.json").write_text("[]")
        self.assertIsNone(state.handoff(self.task))

    def test_internal_conclusions_and_review_handoffs_are_not_user_documents(self):
        box = self.task / "deliverables"
        box.mkdir()
        for name in ("conclusion-ru.md", "review-899-ru.md",
                     "product-owner-review.md", "cross-review-result.md",
                     "handoff-next-agent.md", "r3-verdict.md", "review-899-ru.html"):
            (box / name).write_text("internal")
        self.assertEqual(state.human_documents(self.task), [])
        self.assertIsNone(state.handoff(self.task, {}))

    def test_a_named_internal_audit_does_not_create_user_debt(self):
        box = self.task / "deliverables"
        box.mkdir()
        (box / "tail-audit-2026-08-06.md").write_text("audit")
        (self.task / "task.md").write_text(
            "# Audit\n\nНичего не отправлять пользователю самому: это список для продакта.\n")
        self.assertIsNone(state.handoff(self.task, {}))

    def test_review_word_in_an_ordinary_user_filename_is_not_enough(self):
        box = self.task / "deliverables"
        box.mkdir()
        (box / "market-review-2026.md").write_text("for the user")
        (self.task / "task.md").write_text("# Market report\n")
        self.assertEqual([path.name for path in state.human_documents(self.task)],
                         ["market-review-2026.md"])

    def test_persisted_attachment_observation_closes_without_a_manual_note(self):
        task = self.like_783()
        document = task / "deliverables" / "product-portfolio-history-sources-2026-08-06.html"
        observed = {(document.name, document.stat().st_size): [
            {"channel": "telegram", "message_id": 18491}
        ]}
        hand = state.handoff(task, observed)
        self.assertTrue(hand["delivered"])
        self.assertIn("telegram", hand["delivered_src"])

    def test_every_user_document_must_be_observed_before_the_task_closes(self):
        task = self.like_783()
        first = task / "deliverables" / "product-portfolio-history-sources-2026-08-06.html"
        (task / "deliverables" / "second-report.md").write_text("second")
        observed = {(first.name, first.stat().st_size): [
            {"channel": "email", "message_id": "gmail-1"}
        ]}
        hand = state.handoff(task, observed)
        self.assertFalse(hand["delivered"])
        self.assertIn("1 из 2", hand["delivered_src"])

    def test_digest_decides_when_the_observation_has_one(self):
        task = self.like_783()
        document = task / "deliverables" / "product-portfolio-history-sources-2026-08-06.html"
        key = (document.name, document.stat().st_size)
        wrong = {key: [{"channel": "email", "message_id": "wrong",
                        "sha256": "0" * 64, "at": None}]}
        self.assertFalse(state.handoff(task, wrong)["delivered"])
        right = {key: [{"channel": "email", "message_id": "right",
                        "sha256": state._file_sha256(document), "at": None}]}
        self.assertTrue(state.handoff(task, right)["delivered"])

    def test_an_older_same_name_revision_is_named_but_does_not_close(self):
        task = self.like_783()
        document = task / "deliverables" / "product-portfolio-history-sources-2026-08-06.html"
        observed = {(document.name, document.stat().st_size - 7): [{
            "channel": "telegram", "message_id": 11, "sha256": None,
            "at": "2026-07-31T11:00:24+00:00",
        }]}
        hand = state.handoff(task, observed)
        self.assertFalse(hand["delivered"])
        self.assertIn("одноимённые прежние версии", hand["delivered_src"])

    def test_a_document_disappearing_during_matching_does_not_raise(self):
        missing = self.task / "gone.md"
        self.assertEqual(state.matching_observations(missing, {}), [])

    def test_attachment_index_reads_live_mail_and_exported_telegram(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            mail = root / "mail" / "gmail-1"
            mail.mkdir(parents=True)
            (mail / "metadata.json").write_text(json.dumps({
                "message_id": "gmail-1",
                "attachments": [{"filename": "mail.html", "size": 17}],
            }))
            evidence = root / "tasks" / "835-audit" / "evidence"
            evidence.mkdir(parents=True)
            (evidence / "telegram-documents.jsonl").write_text(json.dumps({
                "message_id": 22, "file_name": "telegram.md", "size": 31,
                "dialog": "Delivery bot", "from_me": False,
            }) + "\n")
            # The delivery dialog is a name of the installation, so the test
            # states it instead of inheriting whichever one is installed here.
            with mock.patch.object(state.product_memory, "delivery_dialog",
                                   return_value="delivery bot"):
                observed = state.attachment_observations(
                    mail_sent=root / "mail", tasks_root=root / "tasks",
                    telegram_sent=root / "absent.jsonl")
        self.assertEqual(observed[("mail.html", 17)][0]["channel"], "email")
        self.assertEqual(observed[("telegram.md", 31)][0]["channel"], "telegram")

    def test_unrelated_telegram_dialog_does_not_establish_delivery(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            evidence = root / "tasks" / "835-audit" / "evidence"
            evidence.mkdir(parents=True)
            (evidence / "telegram-documents.jsonl").write_text(json.dumps({
                "message_id": 23, "file_name": "same.md", "size": 12,
                "dialog": "Unrelated person", "from_me": False,
            }) + "\n")
            with mock.patch.object(state.product_memory, "delivery_dialog",
                                   return_value="delivery bot"):
                observed = state.attachment_observations(
                    mail_sent=root / "mail", tasks_root=root / "tasks",
                    telegram_sent=root / "absent.jsonl")
        self.assertNotIn(("same.md", 12), observed)

    def test_the_claim_travels_with_what_was_read_to_make_it(self):
        task = self.like_783()
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(a_snapshot([a_task(
                detail={"handoff": {**state.handoff(task), "delivered_src": ""}})]))

    def test_the_area_reaches_the_board_and_the_strip(self):
        hand = state.handoff(self.like_783())
        task = a_task(id=783, dir="783-product", status="completed",
                      board={"area": "undelivered"}, detail={"handoff": hand})
        board = render.build_board(a_snapshot([task]))
        area = next(a for a in board["panels"][0]["areas"] if a["key"] == "undelivered")
        self.assertEqual(area["count"], 1)
        self.assertEqual([p["id"] for p in board["undelivered"]], [783])
        self.assertFalse(area["plates"][0]["detail"]["handoff"]["delivered"])

    def test_the_freshest_undelivered_document_stands_first(self):
        """The one area that reads newest first, and why.

        Everywhere else time in a state is the problem, so the oldest plate leads.
        Here an old finished task was either handed over some other way or has
        stopped mattering, and the document somebody is waiting for right now is
        the one that just appeared — 783's report was an hour old, not a month.
        """
        old = a_task(id=1, dir="001-t", status="completed",
                     board={"area": "undelivered", "since": "2026-07-01T10:00:00+00:00",
                            "age_seconds": 3000000})
        fresh = a_task(id=2, dir="002-t", status="completed",
                       board={"area": "undelivered", "since": "2026-08-06T14:19:00+00:00",
                              "age_seconds": 3600})
        board = render.build_board(a_snapshot([old, fresh]))
        area = next(a for a in board["panels"][0]["areas"] if a["key"] == "undelivered")
        self.assertEqual([p["id"] for p in area["plates"]], [2, 1])
        self.assertEqual([p["id"] for p in board["undelivered"]], [2, 1])

    def test_the_page_names_the_document_and_what_was_read(self):
        page = (Path(__file__).parent / "process_map_template.html").read_text()
        self.assertIn("документ человеку: ", page)
        self.assertIn("Сделано, но не доставлено", page)


class ReadableAtBothSizes(unittest.TestCase):
    """Part 2 of task 817: light, scrolling, and nothing cut.

    «Давай доску сделаем в светлой теме. И не надо стараться всё упихнуть в
    экран. Мне подойдёт, если будет скролл. А то так ничего не понятно:
    микроплашки с микроскроллом.» Measured in a browser as well — see
    `tasks/817-.../verification.md` — but the shapes that made it impossible are
    held here, because a stylesheet drifts back quietly.
    """

    def page(self) -> str:
        return (Path(__file__).parent / "process_map_template.html").read_text()

    def board_css(self) -> str:
        text = self.page()
        return text[text.index("---------- board"):text.index("---------- the card")]

    def test_the_board_is_light(self):
        text = self.page()
        head = text[text.index(":root"):text.index("* { box-sizing")]
        self.assertIn("--bg0: #f2f5f9", head)
        self.assertIn("--bg1: #ffffff", head)
        # And there is no second palette left at all: the dark one was the chrome
        # of the isometric map, which went with its switch (task 1067).
        self.assertNotIn("body.mapmode", text)

    def test_the_page_scrolls_instead_of_squeezing_everything_into_the_window(self):
        text = self.page()
        self.assertNotIn("html, body { margin: 0; height: 100%; overflow: hidden; }", text)
        # Nothing on this page pins itself to the window height any more: the one
        # screen that did was the canvas, and a canvas is not a document.
        self.assertNotIn("height: 100%; overflow: hidden;", text)
        # The board is in the flow of the document, not pinned to the viewport.
        self.assertNotIn("#board { position: fixed", text)

    def test_no_plate_area_or_column_scrolls_inside_itself(self):
        css = self.board_css()
        for gone in ("overflow-y: auto", "overflow: auto", "max-height: 24vh", "max-height: 17vh"):
            self.assertNotIn(gone, css, f"вложенный скролл вернулся: {gone}")

    def test_no_text_on_a_plate_is_cut_where_it_cannot_be_opened(self):
        """Свёртка — можно, обрезка — нельзя, и разница в том, есть ли к ней ключ.

        Каждый `-webkit-line-clamp`, стоявший здесь до задачи 817, защищал
        фиксированную высоту экрана, которой больше нет, и каждый резал фразу,
        которую доска уже обещала сказать. Их нет и сейчас: единственный клэмп в
        файле — общая механика `.fold`, у которой на каждый свёрнутый блок есть
        своя кнопка, а `.fold.open` снимает ограничение целиком.

        Требование задачи 1163, пункт 5: «Ни один блок текста, видимый без
        действия пользователя, не длиннее трёх строк. Полный текст открывается по
        нажатию». Троеточия по-прежнему нет нигде: текст не теряется, он сложен.
        """
        text = self.page()
        clamps = [line for line in text.splitlines() if "-webkit-line-clamp" in line]
        self.assertEqual(len(clamps), 2, clamps)
        self.assertIn(".fold {", text)
        self.assertIn(".fold.open { display: block; -webkit-line-clamp: none;", text)
        # Кнопка появляется у каждого свёрнутого блока и снимается только тогда,
        # когда текст и так уместился.
        self.assertIn('more.textContent = "показать целиком"', text)
        self.assertIn("folded.push([node, more])", text)
        # И ни одного многоточия: обрезка текста, которую нельзя раскрыть.
        self.assertNotIn("text-overflow: ellipsis", self.board_css())

    def test_every_question_of_a_plate_is_shown_and_not_the_first_two(self):
        text = self.page()
        self.assertIn("for (const question of p.questions) node.appendChild(askedNode(question))", text)
        self.assertNotIn("p.questions.slice(0, 2)", text)

    def test_the_card_takes_the_page_rather_than_scrolling_inside_a_drawer(self):
        text = self.page()
        self.assertIn("#cardbody { padding:", text)
        self.assertNotIn("#cardbody { overflow: auto", text)
        self.assertIn("window.scrollTo(0, boardScroll)", text)


class TheWakeUpSeesTheQueueMove(unittest.TestCase):
    """A timer wakes a process, never a conversation — so the process has to see it.

    Before this, the tick knew four transitions and «планируемая задача стала
    запускаемой» could not be among them: startability was not a state, so it had
    no edge. On 2026-08-06 the tick correctly reported «прогон 830 завершился»
    and nothing said what that made possible; 831 stood forty minutes.
    """

    def report(self, ready=(), decided=(), pickup=(), live=(), undelivered=(),
               waiting=(), worktrees=("/path/to/task-agent",),
               processes=(), process_observation=None):
        return {
            "title": "Процессный контур",
            "live_runs": [{"id": i, "title": "Задача", "status": "in_progress",
                           "path": "tasks/x",
                           "run": {"stale_running": False, "process_alive": True}}
                          for i in live],
            "needs_attention": [], "repos": [],
            "ready_to_start": [{"id": i, "title": "Задача", "condition": None,
                                "met": [], "met_src": None} for i in ready],
            "decided_not_done": [{"id": i, "title": "Задача", "decision": "deliver",
                                  "src": "квитанции несут только события прогона"}
                                 for i in decided],
            "can_pick_up": [{"id": i, "title": "Задача"} for i in pickup],
            "undelivered": [{"id": i, "age_seconds": age, "title": "Задача",
                             "document": "deliverables/report.html", "src": "записки нет"}
                            for i, age in undelivered],
            "waiting_user": [{"id": i, "title": "Задача"} for i in waiting],
            "worktrees": list(worktrees),
            "owners_awake": [],
            "long_lived_processes": list(processes),
            "long_lived_processes_observation": process_observation or {
                "available": True, "reason": None},
        }

    def test_the_tick_forwards_only_the_router_route_diagnostic(self):
        comparison = (
            "product-owner: route selected; Claude "
            "(weekly_remaining:claude=82%,codex=31%)"
        )
        unavailable = "product-owner: route selected; Claude (usage_unavailable)"
        self.assertEqual(
            tick.route_diagnostics(
                f"model warning\n{comparison}\n"
                "product-owner: quota check unavailable; keeping Opus (network)\n"
                f"{unavailable}\nother noise\n"
            ),
            [comparison, unavailable],
        )
        source = Path(tick.__file__).read_text()
        self.assertLess(source.index("def route_diagnostics"), source.index("def main"))

    def test_a_condition_that_cleared_is_a_transition_like_a_finished_run(self):
        before = tick.snapshot(self.report())
        after = tick.snapshot(self.report(ready=[831]))
        events = tick.transitions(before, after)
        self.assertEqual(len(events), 1)
        self.assertIn("831", events[0])
        self.assertIn("запускать", events[0])

    def test_standing_ready_is_a_state_and_only_becoming_ready_is_the_event(self):
        # Otherwise every tick would shout about the same task forever, and a
        # wake-up that cries every twenty minutes is a wake-up nobody reads.
        steady = tick.snapshot(self.report(ready=[831]))
        self.assertEqual(tick.transitions(steady, steady), [])

    def test_a_decision_nobody_carried_out_is_a_transition_too(self):
        events = tick.transitions(tick.snapshot(self.report()),
                                       tick.snapshot(self.report(decided=[835])))
        self.assertEqual(len(events), 1)
        self.assertIn("835", events[0])
        self.assertIn("не исполнено", events[0])

    def test_a_surviving_process_and_a_duplicate_wake_the_tick(self):
        process = {"pid": 7, "since": "start", "task": 692,
                   "command": "field_probe.py", "repo": "/opt/projects/example-product",
                   "duplicate_count": 1}
        before = tick.snapshot(self.report())
        one = tick.snapshot(self.report(processes=[process]))
        self.assertIn("у завершённой задачи 692 живёт процесс field_probe.py",
                      tick.transitions(before, one)[0])

        duplicate = {**process, "pid": 8, "duplicate_count": 2}
        first = {**process, "duplicate_count": 2}
        doubled = tick.snapshot(self.report(processes=[first, duplicate]))
        events = tick.transitions(one, doubled)
        self.assertTrue(any("ДУБЛЬ" in event and "2 раза" in event for event in events))

    def test_unavailable_registry_preserves_inventory_without_a_false_death(self):
        process = {"pid": 7, "since": "start", "task": 692,
                   "command": "field_probe.py", "repo": "/opt/projects/example-product",
                   "duplicate_count": 1, "outputs": [{"path": "/tmp/probe.jsonl"}]}
        observed_report = self.report(processes=[process])
        before = tick.snapshot(observed_report)
        unavailable_report = self.report(
            process_observation={"available": False,
                                 "reason": "registry unavailable"})

        current = tick.snapshot(unavailable_report, before)
        events = tick.transitions(before, current)

        self.assertEqual(current["long_lived"], before["long_lived"])
        self.assertEqual(events, [
            "опись долгоживущих процессов недоступна: registry unavailable"])
        self.assertFalse(any("больше не жив" in event for event in events))
        stored = {"long_lived_processes": [process]}
        self.assertEqual(
            tick.persisted_process_inventory(unavailable_report, stored), [process])

    def test_the_tick_persists_the_full_inventory_even_when_it_does_not_wake(self):
        source = Path(tick.__file__).read_text()
        self.assertIn('"long_lived_processes": long_lived_processes', source)
        self.assertIn('"long_lived_processes_observation": process_observation(report)',
                      source)

    def test_yielding_to_another_owner_leaves_the_list_on_disk(self):
        """Yielding is right; yielding silently is how the list reaches nobody."""
        report = self.report(ready=[831])
        report["owners_awake"] = [an_owner(worktrees=["/path/to/task-agent"])]
        left = tick.yielded(report)
        self.assertEqual([item["id"] for item in left["ready_to_start"]], [831])
        self.assertEqual(left["to"][0]["pid"], 1)
        self.assertEqual(left["to"][0]["shared_worktrees"], ["/path/to/task-agent"])
        self.assertTrue(left["src"].strip())

    def test_the_tick_does_not_count_itself_as_the_other_owner(self):
        # The second owner of 2026-08-06 was awake on the *same* direction, so
        # the exclusion has to be this process and not this thread.
        report = self.report(ready=[831])
        report["owners_awake"] = [an_owner(pid=os.getpid(),
                                           worktrees=["/path/to/task-agent"])]
        self.assertIsNone(tick.yielded(report))

    def test_an_owner_that_cannot_take_the_same_tree_is_not_yielded_to(self):
        """The whole of the 2026-08-07 mutism, in one assertion.

        Four timers fired at 16:06:56 and again at 16:47:00, and yielding meant
        «any other tick». `client` stood down for `process`, `platform`
        for `client` and `process`, `product` for all three, and `process` for
        nobody — so on every synchronous wake-up exactly one direction could act
        and it was always the same one. The four own four disjoint sets of
        repositories: not one of those collisions could have happened.
        """
        report = self.report(ready=[831], worktrees=["/path/to/task-agent"])
        report["owners_awake"] = [an_owner(thread="product",
                                           worktrees=["/opt/projects/example-product"])]
        self.assertIsNone(tick.yielded(report))

    def test_the_console_owner_is_yielded_to_when_it_stands_in_the_same_tree(self):
        # And only then. An owner is a person or a process that could put a
        # second child into one working tree, never merely another window.
        report = self.report(ready=[831], worktrees=["/path/to/task-agent"])
        report["owners_awake"] = [an_owner(kind="session", thread=None,
                                           worktrees=["/path/to/task-agent"])]
        self.assertEqual(tick.yielded(report)["to"][0]["kind"], "session")

    def test_the_list_is_written_whether_or_not_an_agent_is_woken(self):
        source = Path(state.HOME / "scripts" / "thread_tick.py").read_text()
        # Written into the thread's own state file, next to the snapshot, before
        # the agent runs — so it survives a tick that decides to say nothing.
        self.assertIn('"yielded_to_awake_owner": yielded(report)', source)
        self.assertIn("**standing", source)

    def test_idle_with_work_available_is_itself_the_event(self):
        """The defect the user wrote in and named.

        `transitions()` can only speak on an edge. A direction with ten startable
        tasks and no live run has no edge to offer, so `if not events: return 0`
        made it mute forever — «панель показывает, что в работе ничего нет. При
        этом не сделанных задач — вагон… тогда почему ничего не делаешь?». On
        2026-08-07 all four timers fired at 16:06:56, sixteen tasks stood
        startable, nothing was running, and no letter and no line appeared.
        """
        report = self.report(pickup=[861, 854, 839])
        events, reminder, _ = tick.standing_events(
            report, tick.snapshot(report), {}, AT)
        self.assertEqual(len(events), 1)
        self.assertIn("простой", events[0])
        self.assertIn("к запуску 3", events[0])
        self.assertTrue(reminder["signature"])

    def test_work_that_is_running_is_not_idleness(self):
        report = self.report(pickup=[861], live=[871])
        events, _, _ = tick.standing_events(report, tick.snapshot(report), {}, AT)
        self.assertEqual(events, [])

    def test_an_empty_queue_is_not_idleness_either(self):
        # Nothing to start is a legitimate zero, and it must not be alarmed at.
        report = self.report()
        events, _, _ = tick.standing_events(report, tick.snapshot(report), {}, AT)
        self.assertEqual(events, [])

    def test_the_same_queue_is_not_repeated_inside_the_interval(self):
        report = self.report(pickup=[861])
        snap = tick.snapshot(report)
        first, reminder, _ = tick.standing_events(report, snap, {}, AT)
        self.assertEqual(len(first), 1)
        again, _, _ = tick.standing_events(
            report, snap, {"idle_reminder": reminder},
            AT + timedelta(seconds=tick.IDLE_REMIND_SECONDS - 60))
        self.assertEqual(again, [])

    def test_the_interval_is_a_frequency_and_not_a_mute_button(self):
        """«Дребезг лечится не молчанием, а разумной частотой.»

        A queue that has not moved is said again once the interval has passed.
        The failure this whole file repairs is silence, so the standing case is
        the one that has to keep speaking.
        """
        report = self.report(pickup=[861])
        snap = tick.snapshot(report)
        _, reminder, _ = tick.standing_events(report, snap, {}, AT)
        later, _, _ = tick.standing_events(
            report, snap, {"idle_reminder": reminder},
            AT + timedelta(seconds=tick.IDLE_REMIND_SECONDS + 1))
        self.assertEqual(len(later), 1)

    def test_a_queue_that_moved_is_news_at_the_next_tick(self):
        # Not «once an hour whatever happens»: a new startable task is a
        # different queue and the reminder is about that queue.
        _, reminder, _ = tick.standing_events(
            self.report(pickup=[861]), tick.snapshot(self.report(pickup=[861])), {}, AT)
        moved = self.report(pickup=[861, 854])
        events, _, _ = tick.standing_events(
            moved, tick.snapshot(moved), {"idle_reminder": reminder},
            AT + timedelta(seconds=60))
        self.assertEqual(len(events), 1)

    def test_a_document_nobody_was_shown_wakes_the_owner_like_idleness(self):
        """«Сделано, не доставлено» на панели должно быть событием, а не украшением.

        The user saw one entry stand more than forty minutes, and then several
        at once. The area has existed since 783 and could wake nobody.
        """
        overdue = self.report(undelivered=[(864, tick.UNDELIVERED_SECONDS + 1)])
        events, _, held = tick.standing_events(
            overdue, tick.snapshot(overdue), {}, AT)
        self.assertEqual(len(events), 1)
        self.assertIn("864", events[0])
        self.assertTrue(held["signature"])

    def test_a_document_that_has_only_just_appeared_is_not_yet_an_alarm(self):
        fresh = self.report(undelivered=[(864, 60)])
        events, _, _ = tick.standing_events(fresh, tick.snapshot(fresh), {}, AT)
        self.assertEqual(events, [])

    def test_every_named_reason_carries_what_observed_it(self):
        report = self.report(waiting=[827])
        reasons = tick.idle_reasons(report, {"yielded_to_awake_owner": None})
        self.assertTrue(reasons)
        for reason in reasons:
            self.assertTrue(reason["text"].strip())
            self.assertTrue(reason["src"].strip())
        self.assertIn("waiting_user", [r["code"] for r in reasons])

    def test_a_question_on_one_task_does_not_explain_a_free_one(self):
        """Причина простоя обязана относиться к той работе, которую объясняет.

        Cross-review 881: with nine startable tasks next to one standing on the
        user, the board printed «ждём ответа пользователя» as the reason nothing
        was started. The areas are disjoint — a task in «ждёт решения
        пользователя» is never in the three startable ones — so that sentence
        explained the nine by the tenth, and it silenced «значит запускать надо»,
        which stands under `if not reasons`.
        """
        # The weekly Codex window is a live observation of this host, and a full
        # one would add a second reason to the list under assertion here.
        with mock.patch.object(tick, "codex_window", return_value=None):
            reasons = tick.idle_reasons(self.report(pickup=[861], waiting=[827]),
                                        {"yielded_to_awake_owner": None})
        self.assertEqual([r["code"] for r in reasons], ["none_observed"])
        self.assertNotIn("827", json.dumps(reasons, ensure_ascii=False))

    def test_a_question_is_the_reason_when_it_really_is_the_only_work(self):
        # The other half: suppressing it whenever anything else exists would
        # trade one wrong caption for silence on the case it does explain.
        with mock.patch.object(tick, "codex_window", return_value=None):
            reasons = tick.idle_reasons(self.report(waiting=[827]),
                                        {"yielded_to_awake_owner": None})
        self.assertEqual([r["code"] for r in reasons], ["waiting_user"])
        self.assertIn("827", reasons[0]["text"])

    def test_a_thread_wide_reason_still_stands_next_to_free_work(self):
        # Only `waiting_user` is tied to particular tasks. A busy working tree
        # holds the whole direction and must keep explaining free work.
        with mock.patch.object(tick, "codex_window", return_value=None):
            reasons = tick.idle_reasons(self.report(pickup=[861], live=[900], waiting=[827]),
                                        {"yielded_to_awake_owner": None})
        self.assertEqual([r["code"] for r in reasons], ["worktree_busy"])

    def test_no_observable_reason_is_itself_stated_out_loud(self):
        """«Везде нули» без причины — это дефект, so the absence is the finding.

        An empty list would be a caption nobody reads. The reader is owed the
        sentence «ни одной известной причины не сработало — значит запускать».
        """
        reasons = tick.idle_reasons(self.report(pickup=[861]),
                                    {"yielded_to_awake_owner": None})
        self.assertEqual([r["code"] for r in reasons], ["none_observed"])
        self.assertIn("запускать надо", reasons[0]["text"])

    def test_a_direction_with_nothing_to_start_is_owed_no_such_sentence(self):
        # «Значит запускать надо» about an empty queue would be a false claim,
        # and an empty list is the honest answer there.
        self.assertEqual(tick.idle_reasons(self.report(),
                                           {"yielded_to_awake_owner": None}), [])
        self.assertNotIn("простое", tick.prompt(self.report(), ["первый запуск треда"],
                                                [], []))

    def test_the_outcome_is_the_difference_in_live_runs_and_not_the_owner_s_prose(self):
        report = self.report(pickup=[861])
        before = tick.snapshot(report)
        after = tick.snapshot(self.report(pickup=[861], live=[871]))
        self.assertIn("871", tick.outcome(before, after, True, report))
        self.assertIn("не нашёл, что запустить",
                      tick.outcome(before, before, True, report))
        self.assertIn("не будился", tick.outcome(before, before, False, report))

    def test_the_wake_up_report_carries_both_areas_apart(self):
        source = Path(state.HOME / "scripts" / "thread_state.py").read_text()
        self.assertIn('area"] == "ready_to_start"', source)
        self.assertIn('area"] == "decision_unmet"', source)
        self.assertIn('area"] == "pickup"', source)


class PanelReadsAsOneThing(unittest.TestCase):
    """Task 864: the findings of the read-only review 860, closed.

    Every one of these was measured on the live board before it was written:
    twelve tooltip leak points at each of two window sizes, 1,09:1 contrast for
    the leaked tooltip's body text, 223 elements whose source lived only in a
    `title`, 160 task plates with neither a role nor a focus, and forty presses
    of Tab reaching two buttons on the whole board. What a browser measures a
    browser has to re-measure; what these tests hold is that the mechanism the
    measurement asked for is present and cannot quietly go away.
    """

    def test_the_map_and_its_tooltip_left_with_the_switch(self):
        """Task 1067: no screen switch, so no second screen and no tooltip of it.

        The two tests that stood here held the tooltip of the isometric map to
        one writer and to its own opaque colours. Both were about a screen that
        no longer exists, and what replaced the claim is that nothing of it came
        back quietly: a hidden canvas that still answers `pick()`, or a dark
        palette bound to a class nobody sets, is exactly the dead branch the
        removal was for.
        """
        page = a_page()
        for gone in ("mapOpen", "hideTip", "tipFor", "#tip {", "body.mapmode",
                     "<canvas", "requestAnimationFrame", "Карта во времени"):
            self.assertNotIn(gone, page, f"остаток экрана карты: {gone}")

    def test_a_source_is_shown_by_a_control_and_not_by_a_browser_tooltip(self):
        page = a_page()
        board = page[page.index("function plateNode"):page.index("function channelsNode")]
        # Not one `title = ` left on anything the board builds: on a phone that
        # channel does not exist, and 53 of the 223 texts were over 80 characters.
        self.assertNotIn(".title = ", board)
        self.assertIn("function sourceNote", page)
        self.assertIn('btn.setAttribute("aria-expanded", "false")', page)
        self.assertIn('btn.type = "button"', page)
        # The press must not reach the plate around it: the source of a task and
        # the details of a task are two different answers.
        self.assertIn("event.stopPropagation()", page)

    def test_a_plate_that_opens_a_card_says_so_and_can_be_reached(self):
        page = a_page()
        self.assertIn('node.setAttribute("role", "button")', page)
        self.assertIn("node.tabIndex = 0", page)
        self.assertIn("Открыть детали задачи ", page)
        self.assertIn('event.key === "Enter" || event.key === " "', page)

    def test_a_plate_that_opens_nothing_does_not_look_like_a_control(self):
        page = a_page()
        # The hand cursor is bound to the role rather than to the class, so a
        # plate without a card cannot borrow it: thirty-three product questions
        # and promises carried it and did nothing when pressed.
        self.assertIn('.plate[role="button"] { cursor: pointer; }', page)
        self.assertIn(".plate.q { cursor: default; }", page)
        self.assertNotIn(".plate { cursor: pointer; }", page)

    def test_what_folds_an_area_is_a_button_that_says_whether_it_is_open(self):
        page = a_page()
        self.assertIn("function asFolder", page)
        self.assertIn('btn.setAttribute("aria-expanded", shut ? "false" : "true")', page)
        self.assertIn('btn.setAttribute("aria-expanded", nowShut ? "false" : "true")', page)
        # Both folders of the board go through it: the area headings and the
        # «Репозитории и связь» summary.
        self.assertIn('head.className = "areahead"', page)
        self.assertIn('const sum = document.createElement("button")', page)


class TaskInItsOwnWords(unittest.TestCase):
    """`## Summary` of `task.md`, collected so the card can show it."""

    def collected(self, body: str) -> str | None:
        with tempfile.TemporaryDirectory() as home:
            task_dir = Path(home) / "864-task"
            task_dir.mkdir()
            (task_dir / "task.md").write_text(body)
            return state.summary(task_dir)

    def test_the_summary_section_is_read_whole(self):
        text = self.collected("---\nid: 864\n---\n# Заголовок\n\n"
                              "## Summary\nПервая строка.\nВторая строка.\n\n"
                              "## Откуда это взялось\nне это\n")
        self.assertEqual(text, "Первая строка.\nВторая строка.")

    def test_a_task_without_a_summary_says_nothing_rather_than_guessing(self):
        self.assertIsNone(self.collected("---\nid: 1\n---\n# Заголовок\n\n## Plan\nx\n"))
        self.assertIsNone(self.collected("---\nid: 1\n---\n## Summary\n\n## Plan\nx\n"))

    def test_a_missing_task_file_is_absence_and_not_an_error(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertIsNone(state.summary(Path(home)))

    def test_a_long_description_names_its_own_cut(self):
        text = self.collected("## Summary\n" + "я" * (state.SUMMARY_CHARS + 40) + "\n")
        self.assertTrue(text.endswith("… (описание длиннее, чем показано)"))
        self.assertLess(len(text), state.SUMMARY_CHARS + 40)

    def test_the_description_is_cleaned_when_the_showing_is_anonymous(self):
        # `scrub` walks every string of the document, so a path or an address
        # inside a description leaves with the rest. This holds that the field
        # was not added as an exemption.
        task = a_task(detail={"summary": "смотри /path/to/task-agent/tasks/864"})
        cleaned = schema.scrub(a_snapshot([task]))
        self.assertNotIn("/opt/projects", cleaned["threads"][0]["tasks"][0]["detail"]["summary"])

    def test_the_description_travels_to_the_plate_the_card_is_built_from(self):
        detail = render.plate(a_task(detail={"summary": "О чём задача"}))["detail"]
        self.assertEqual(detail["summary"], "О чём задача")


PLAN_CATALOGUE = [
    {"id": 1121, "title": "Продукт: первый живой запуск с реальными заявками",
     "slug": "1121-product-first-live", "status": "planned"},
    {"id": 1136, "title": "Продукт: ежедневный отчёт показывает результат за день",
     "slug": "1136-product-daily", "status": "planned"},
    {"id": 1138, "title": "Продукт: новый источник данных попадает в сбор сам",
     "slug": "1138-product-source", "status": "planned"},
    {"id": 1152, "title": "Бот: помощник участвует в служебной задаче",
     "slug": "1152-bot-helper", "status": "planned"},
    {"id": 1153, "title": "Продукт: одна тревога приходит один раз",
     "slug": "1153-product-alarm", "status": "planned"},
    {"id": 1082, "title": "Продукт: подготовить принятую версию",
     "slug": "1082-product-prepare", "status": "completed"},
    {"id": 2, "title": "Старая задача номер два", "slug": "002-old", "status": "completed"},
]


def a_revision(**over) -> dict:
    plan = {"revision": 31, "accepted_at": "2026-08-13T20:48:53+00:00",
            "headline": "Робот Продукт доходит до первой настоящей заявки",
            "now": ["1153 — заведена по наблюдению, в очередь не ставится"],
            "next": ["1082 engine-круг — первое условие цели 0002",
                     "1121 — первый живой запуск Продукт с реальными заявками",
                     "1136 и 1138 — числа по портфелю и новый контракт биржи",
                     "1152 — на полке по слову пользователя, очередью не является"],
            "parallel": [], "paused": ["Переделка памяти клиента — на паузе "
                                       "с 2026-08-13 по слову пользователя"],
            "grounds": [], "contradictions": []}
    plan.update(over)
    return plan


class ThePlanOwnsTheQueue(unittest.TestCase):
    """Порядок работ читается у плана, потому что наблюдение его не знает.

    До правки доска выводила очередь из наблюдения, а вывести её оттуда нельзя:
    `planned` — это статус, и план говорит об этом сам последней своей строкой.
    Цена была измерена на живом состоянии 2026-08-13: под «в очереди» у Продукт
    стояли 853 и 1136, тогда как действующая редакция ставила 1121 → 1136 → 1138,
    а 1121 и 1138 лежали под «можно подхватить» — то есть доска предлагала
    подхватить работу, у которой уже было место в очереди.
    """

    def projection(self, plan=None, catalogue=None):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home) / "content"
            (root / "products").mkdir(parents=True)
            if plan is not None:
                product_memory.publish_plan(plan, root)
            with mock.patch.object(product_memory, "ROOT", root):
                return state.plan_projection(catalogue or PLAN_CATALOGUE)

    def test_the_queue_keeps_the_order_of_the_revision(self):
        queue = self.projection(a_revision())["queue"]
        self.assertEqual([[t["id"] for t in entry["tasks"]] for entry in queue],
                         [[1082], [1121], [1136, 1138]])

    def test_each_now_result_keeps_its_tasks_and_next_transition(self):
        projection = self.projection(a_revision(
            now=["**Companion — результат принят (1121).** Всё доставлено.",
                 "**Продукт А — контроль по расписанию.** Наблюдение сохранено."],
            next=["**Companion:** дальше 1136 → 1138.",
                  "**Продукт А:** следующая проверка 16 августа."],
            outcome_links=[{"now": 1, "next": [1], "tasks": [1121]},
                           {"now": 2, "next": [2]}]))
        self.assertEqual([item["title"] for item in projection["outcomes"]],
                         ["Companion — результат принят (1121).",
                          "Продукт А — контроль по расписанию."])
        self.assertEqual([task["id"] for task in projection["outcomes"][0]["tasks"]],
                         [1121])
        self.assertIn("1136", projection["outcomes"][0]["next"][0])
        self.assertIn("16 августа", projection["outcomes"][1]["next"][0])

    def test_no_transition_is_guessed_without_an_explicit_plan_relation(self):
        outcome = self.projection(a_revision(
            now=["**Companion (1121).**"],
            next=["1121 — совпавший номер сам по себе не связь"]))["outcomes"][0]
        self.assertEqual(outcome["next"], [])
        self.assertIn("явной связи", outcome["checked"])

    def test_a_now_line_without_an_explicit_task_link_has_no_observation(self):
        outcome = self.projection(a_revision(
            now=["**1121 — результат.** Старые 1152 и 1136 только упомянуты"],
            outcome_links=[]))["outcomes"][0]
        self.assertEqual(outcome["tasks"], [])

    def test_an_explicit_empty_task_link_does_not_fall_back_to_prose(self):
        outcome = self.projection(a_revision(
            now=["**1121 — результат без наблюдаемой задачи.**"],
            outcome_links=[{"now": 1, "next": [], "tasks": []}]))["outcomes"][0]
        self.assertEqual(outcome["tasks"], [])

    def test_a_broken_explicit_relation_is_refused_not_silently_ignored(self):
        with self.assertRaises(product_memory.ContentError):
            self.projection(a_revision(outcome_links=[{"now": 1, "next": [99]}]))

    def test_a_queue_written_one_task_to_a_line_is_read_by_name(self):
        """Приёмочное условие 3: порядок совпадает с редакцией поимённо.

        Так план и пишется с редакции 34 — одна строка называет одну задачу, — и
        читать порядок из прозы внутри строки («после неё 1054, затем 1067») коду
        не приходится: этого он и не умеет, и уметь не должен.
        """
        queue = self.projection(a_revision(next=[
            "**1121 — идёт прямо сейчас.** Круг исправления автором.",
            "**1136 — числа по портфелю. Следующая после 1121.**",
            "**1138 — новый контракт биржи. После 1136.**"]))["queue"]
        self.assertEqual([[t["id"] for t in entry["tasks"]] for entry in queue],
                         [[1121], [1136], [1138]])

    def test_a_line_that_says_it_is_not_a_queue_stands_in_the_backlog(self):
        # План пишет это своими словами и в том же поле; признак — слова строки,
        # и они печатаются рядом, чтобы вывод можно было опровергнуть чтением.
        backlog = self.projection(a_revision())["backlog"]
        self.assertEqual([entry["field"] for entry in backlog], ["next", "paused"])
        self.assertEqual([t["id"] for t in backlog[0]["tasks"]], [1152])
        self.assertTrue(all(entry["kind"] == "paused" for entry in backlog))

    def test_a_goal_identifier_is_not_read_as_a_task_number(self):
        # «первое условие цели 0002» дало бы ссылку на задачу 2: номер задачи в
        # прозе с ведущим нулём не пишут, а идентификатор цели пишут.
        first = self.projection(a_revision())["queue"][0]
        self.assertEqual([t["id"] for t in first["tasks"]], [1082])

    def mixed_revision(self):
        """Редакция, в которой номера стоят и предметом, и ссылкой, и попутно."""
        return a_revision(next=[
            "754 — окончание прогона ведёт к следующему шагу; 1090 больше не держит",
            "Клиент — 1152 и переделка памяти остаются бэклогом: порядок "
            "1150 → 1151 → 1152",
        ], paused=["Клиент — пауза снята частично: исследование 1121 и 1136 разрешено"])

    def mixed(self):
        return self.projection(self.mixed_revision(), catalogue=PLAN_CATALOGUE + [
            {"id": 754, "title": "Окончание прогона", "status": "planned"},
            {"id": 1090, "title": "Держатель", "status": "planned"},
            {"id": 1150, "title": "Раз", "status": "planned"},
            {"id": 1151, "title": "Два", "status": "planned"}])

    def test_a_number_that_is_a_reference_is_not_the_subject_of_the_line(self):
        """Порядок очереди читается только из явной позиции — и ниоткуда больше.

        Признак «любое число строки, которое знает каталог» снят кругом 1
        независимого ревью. Здесь он остаётся снятым ровно там, где ложная связь
        переставляет работы: в очереди. «754 — …; 1090 больше не держит» отдаёт
        очереди 754 и не отдаёт 1090, а строка, начинающаяся со слова, не отдаёт
        очереди никого.
        """
        projection = self.mixed()
        self.assertEqual([[t["id"] for t in e["tasks"]] for e in projection["queue"]],
                         [[754]])
        self.assertEqual([[t["id"] for t in e["tasks"]] for e in projection["backlog"]],
                         [[], []])
        # 1090 названа строкой очереди — и это всё, что о ней сказано: места в
        # очереди у неё нет, области она не меняет и стоящей не объявлена.
        named_only = state.plan_place_of(projection, 1090)
        self.assertEqual(named_only["role"], "mentioned")
        self.assertIsNone(named_only["position"])

    def test_an_allowing_line_holds_nothing_it_merely_names(self):
        """Держит только предмет строки — во всех областях и во всех полях.

        Круг 2 сделал правило несимметричным: держащая строка держала любой
        названный ею номер. Круг 3 независимого ревью показал на живом снимке,
        чего это стоит: строка поля пауз «исследование 1150 и 1151 разрешено» —
        разрешающая, а не держащая, и правило объявляло остановленной прямо
        разрешённую пользователем работу, убирая её из доступной. Это тот же
        дефект круга 1 по другой ветви: числовая ссылка снова становилась
        утверждением о задаче.
        """
        projection = self.mixed()
        # Строка «…исследование 1121 и 1136 разрешено» не назначает ролей: обе
        # задачи она только называет, и держащей для них не становится.
        for number in (1121, 1136, 1152, 1150, 1151):
            place = state.plan_place_of(projection, number)
            self.assertEqual(place["role"], "mentioned", number)
            self.assertIn("места ей эта строка не назначает", place["src"])
            self.assertTrue(place["line"])
            # Область — обычная: упоминание не убирает работу из доступной.
            self.assertEqual(state.board_area("planned", ["idle"], False, None,
                                              plan_role=place["role"]), "pickup")

    def test_a_line_names_what_it_names_apart_from_its_subject(self):
        # Предмет и упоминание различимы в данных, а не в чтении глазами: по
        # первому решается место задачи, по второму — ничего, кроме того, что
        # план эту задачу называет.
        backlog = self.mixed()["backlog"]
        self.assertEqual([[t["id"] for t in e["tasks"]] for e in backlog], [[], []])
        self.assertEqual([[t["id"] for t in e["also"]] for e in backlog],
                         [[1152, 1150, 1151], [1121, 1136]])
        self.assertIn("названы в строке, но не её предметом", backlog[0]["checked"])

    def test_the_queue_beats_a_line_that_also_holds_the_same_task(self):
        """Противоречие внутри редакции не решается молча.

        Редакция 32 держала обе строки сразу — «после неё 1054, затем 1067» и
        «1054 и 1067 — на полке», — и доска молча выбрала вторую, показав
        бэклогом работу, которую пользователь только что снял с полки. Побеждает
        более конкретное утверждение, а вторая строка остаётся видна. Спорят
        только строки, для которых эта задача — предмет: упоминание ни с чем не
        спорит, потому что ничего и не утверждает.
        """
        plan = a_revision(next=[
            "1121 — первый живой запуск Продукт с реальными заявками",
            "1121 — остаётся бэклогом по слову пользователя, очередью не является",
            "Клиент — 1152 остаётся бэклогом: порядок 1121 → 1136",
        ])
        projection = self.projection(plan)
        place = state.plan_place_of(projection, 1121)
        self.assertEqual((place["role"], place["position"]), ("queue", 1))
        self.assertEqual([other["role"] for other in place["conflict"]], ["paused"])
        self.assertIn("остаётся бэклогом по слову пользователя",
                      place["conflict"][0]["line"])
        self.assertEqual([c["id"] for c in projection["conflicts"]], [1121])
        # 1136 третья строка называет, а очередь её не ставит: это упоминание, и
        # ни спором, ни паузой оно не становится.
        self.assertEqual(state.plan_place_of(projection, 1136)["role"], "mentioned")

    def test_a_task_the_plan_names_is_not_said_to_be_unmentioned(self):
        """«План о ней не говорит» — утверждение, и оно обязано быть верным.

        Редакция 33 называет 1054 и 1067 внутри строки очереди («1156 — идёт
        прямо сейчас; после неё 1054 …, затем 1067»). Порядок из этой прозы не
        читается — им распоряжается строгий предмет, — но и сказать, что план о
        них молчит, доска не может.
        """
        projection = self.projection(a_revision(next=[
            "1121 — идёт прямо сейчас; после неё 1136, затем 1138"]))
        for number in (1136, 1138):
            place = state.plan_place_of(projection, number)
            self.assertEqual(place["role"], "mentioned")
            self.assertIn("места ей эта строка не назначает", place["src"])
        self.assertEqual([[t["id"] for t in e["also"]] for e in projection["queue"]],
                         [[1136, 1138]])

    def test_a_line_without_a_subject_number_stands_by_its_text_alone(self):
        # «Статья „ИИ-продакт“ — раздел про расход токенов…» задачи не имеет, и
        # придумывать ей задачу нельзя: строка стоит своим текстом.
        entry = self.projection(a_revision(
            next=["Статья «ИИ-продакт» — раздел про расход токенов отправлен "
                  "пользователю 2026-08-13"]))["queue"][0]
        self.assertEqual(entry["tasks"], [])
        self.assertIn("строка начинается не с номера задачи", entry["checked"])

    def test_what_the_revision_never_names_is_said_to_be_unnamed_and_no_more(self):
        projection = self.projection(a_revision())
        self.assertEqual(state.plan_place_of(projection, 1152)["role"], "paused")
        self.assertEqual(state.plan_place_of(projection, 1153)["role"], "named")
        self.assertEqual(state.plan_place_of(projection, 999)["role"], "unnamed")
        self.assertIn("не называет эту задачу",
                      state.plan_place_of(projection, 999)["src"])

    def test_a_queued_task_stands_behind_the_living_ones_before_it_and_not_itself(self):
        projection = self.projection(a_revision())
        # 1082 закрыта, поэтому никого не держит и в списке «перед ней» её нет.
        self.assertEqual(state.plan_place_of(projection, 1121)["ahead"], [])
        self.assertEqual(state.plan_place_of(projection, 1136)["ahead"], [1121])
        self.assertEqual(state.plan_place_of(projection, 1138)["ahead"], [1121])

    def test_no_revision_means_the_board_says_nothing_about_the_queue(self):
        # «Плана нет» — честный ответ. Выводить очередь из статусов нельзя, и
        # молчание владельца порядка не заменяется догадкой.
        projection = self.projection(None)
        self.assertIsNone(projection["revision"])
        self.assertEqual((projection["queue"], projection["backlog"]), ([], []))
        self.assertIsNone(state.plan_place_of(projection, 1121))

    def test_an_unobservable_store_is_refused_not_shown_as_no_queue(self):
        with tempfile.TemporaryDirectory() as home:
            absent = Path(home) / "never-created"
            with mock.patch.object(product_memory, "ROOT", absent):
                with self.assertRaises(schema.ContractError):
                    state.plan_projection(PLAN_CATALOGUE)

    def test_every_entry_carries_what_it_was_compared_against(self):
        projection = self.projection(a_revision())
        for entry in projection["queue"] + projection["backlog"]:
            self.assertIn("с каталогом задач", entry["checked"])
        schema.validate_plan({key: value for key, value in projection.items()
                              if key != "places"})


class TheAreasThePlanOwns(unittest.TestCase):
    """Две области, которых наблюдение назначить не может.

    «В очереди» и «В бэклоге» решаются словом плана, а не состоянием диска, — и
    наблюдение всё равно старше: живой прогон, затор и вопрос пользователю стоят
    выше, потому что это факты о работе, а не решение о ней.
    """

    def test_a_task_the_plan_queued_stands_in_the_queue(self):
        self.assertEqual(state.board_area("planned", ["idle"], False, None,
                                          plan_role="queue"), "queued")

    def test_only_what_the_plan_holds_by_its_word_stands_in_the_backlog(self):
        # Молчание редакции задачу никуда не двигает: круг 1 независимого ревью
        # показал на 1091, 1093, 1130 и 1135, что «редакция её не называет» и «по
        # ней нет разбора» — разные утверждения, а область «сам не запустится»
        # ещё и убирала такую задачу из запуска.
        self.assertEqual(state.board_area("planned", ["idle"], False, None,
                                          plan_role="paused"), "backlog")
        self.assertEqual(state.board_area("planned", ["idle"], False, None,
                                          plan_role="unnamed"), "pickup")

    def test_a_task_the_plan_merely_names_can_still_be_picked_up(self):
        # План называет её, но в очередь не ставит и не держит: ничто на диске её
        # не держит тоже, и «можно подхватить» — это наблюдение, а не решение.
        # Так же и упоминание в любой строке — очереди, пауз или прозы: порядок
        # из прозы не читается, и придержать работу упоминание не может. Держит
        # только строка, предметом которой эта задача стоит.
        self.assertEqual(state.board_area("planned", ["idle"], False, None,
                                          plan_role="named"), "pickup")
        self.assertEqual(state.board_area("planned", ["idle"], False, None,
                                          plan_role="mentioned"), "pickup")

    def test_observation_outranks_the_plan(self):
        # План может отстать от жизни: он назвал очередь, а прогон уже идёт или
        # работа встала. Показывается наблюдаемое состояние.
        self.assertEqual(state.board_area("planned", ["live"], False, None,
                                          plan_role="queue"), "running")
        self.assertEqual(state.board_area("planned", ["gap"], False, None,
                                          plan_role="unnamed"), "stuck")
        self.assertEqual(state.board_area("completed", [], False, None,
                                          plan_role="queue"), "done")

    def test_the_word_that_holds_the_work_outranks_an_observed_holder(self):
        """«Сам не запустится» и «за чем стоит» — ответы на разные вопросы.

        Очередь однажды дойдёт до задачи сама, полка по слову пользователя — нет.
        У 1054 и 1067 наблюдается и то и другое, и ниже держателя область «В
        бэклоге» на живом состоянии оказывалась пустой на всех четырёх
        направлениях. Держатель при этом остаётся на плашке.
        """
        self.assertEqual(state.board_area("planned", ["idle"], False,
                                          "задача 1156 ещё не закрыта",
                                          plan_role="paused"), "backlog")

    def test_an_observed_holder_outranks_the_place_in_the_queue(self):
        # «За чем стоит» — вопрос пользователя, и живой прогон в том же дереве
        # конкретнее, чем место в очереди.
        self.assertEqual(state.board_area("planned", ["idle"], False,
                                          "репозиторий занят живым прогоном 1082",
                                          plan_role="queue"), "queued")

    def test_without_a_plan_the_areas_are_what_they_were(self):
        self.assertEqual(state.board_area("planned", ["idle"], False, None), "pickup")


class ThePlanOnThePlate(unittest.TestCase):
    """Место в плане доезжает до плашки вместе с тем, чем оно наблюдено."""

    def place(self, entry_id=1121, status="planned", detail=None, plan=None):
        task = {"id": entry_id, "title": "Задача", "status": status,
                "status_detail": detail, "dir": "t", "run": {"repo": None},
                "flags": ["idle"], "asked_user": [], "our_questions": [],
                "detail": {}, "board": {"area": None, "why": None, "why_src": None}}
        state.assign_areas([task], [{"id": entry_id, "status_detail": detail}],
                           {entry_id: status}, plan)
        return task

    def a_projection(self):
        return {"revision": 31, "src": "действующая редакция 31 портфельного плана",
                "queue": [], "backlog": [],
                "places": {1121: {"role": "queue", "position": 3, "ahead": [1085, 1086],
                                  "line": "1121 — первый живой запуск",
                                  "field": "next", "conflict": [],
                                  "src": "строка 3 очереди (поле next); "
                                         "действующая редакция 31 портфельного плана"}}}

    def test_the_plate_says_the_plan_put_it_there_and_what_stands_before_it(self):
        board = self.place(plan=self.a_projection())["board"]
        self.assertEqual(board["area"], "queued")
        self.assertEqual(board["plan_place"]["position"], 3)
        self.assertEqual(board["plan_place"]["ahead"], [1085, 1086])
        self.assertIn("редакция 31", board["plan_place"]["src"])

    def test_the_place_in_the_queue_is_not_written_into_what_holds_the_task(self):
        """Место в очереди и держатель — разные утверждения, и полей у них два.

        Держатель наблюдается: живой прогон в том же дереве, незакрытая
        предшественница. Место в очереди — решение владельца порядка, и тот же
        план разрешает направлениям идти параллельно, так что работа выше по
        списку никого не держит.
        """
        board = self.place(plan=self.a_projection())["board"]
        self.assertIsNone(board["blocked_by"])
        self.assertIsNone(board["why"])

    def test_a_closed_task_is_not_called_queued_however_the_plan_reads(self):
        # Расхождение не прячется — место в плане остаётся на плашке, — но
        # очередью закрытая работа не называется: наблюдение здесь конкретнее.
        board = self.place(status="completed", plan=self.a_projection())["board"]
        self.assertEqual(board["area"], "done")
        self.assertIsNone(board["blocked_by"])
        self.assertEqual(board["plan_place"]["role"], "queue")

    def test_an_observed_holder_is_still_the_answer_to_why_it_stands(self):
        board = self.place(detail="starts_after=1082", plan=self.a_projection())["board"]
        self.assertEqual(board["area"], "queued")
        self.assertIn("1082", board["blocked_by"])

    def test_the_second_thing_the_plan_says_travels_to_the_plate_too(self):
        # План, сказавший об одной работе двояко, показывает обе строки: место
        # выбрано более конкретным утверждением, а не молча.
        projection = self.a_projection()
        projection["places"][1121]["conflict"] = [
            {"role": "paused", "field": "next", "src": "строка поля next, которая держит работу",
             "line": "1121 — остаётся бэклогом по слову пользователя, очередью не является"}]
        board = self.place(plan=projection)["board"]
        self.assertEqual(board["area"], "queued")
        self.assertEqual(board["plan_place"]["conflict"][0]["role"], "paused")

    def test_a_task_outside_the_plan_carries_the_reading_that_found_nothing(self):
        # Прочитанная редакция её не называет — это и написано на плашке. Но
        # области её молчание не назначает: задача остаётся там, куда её ставит
        # наблюдение, потому что «план о ней не говорит» не значит ни «она не
        # разобрана», ни «её нельзя запускать» (круг 1, HIGH-2).
        board = self.place(entry_id=999, plan=self.a_projection())["board"]
        self.assertEqual(board["area"], "pickup")
        self.assertEqual(board["plan_place"]["role"], "unnamed")
        self.assertIn("не называет эту задачу", board["plan_place"]["src"])


class TheBoardShowsThePlan(unittest.TestCase):
    """Очередь и бэклог на экране: порядок плана и два вида бэклога.

    Пользователь назвал четыре вопроса — что в работе, что недавно закрыто, что
    в очереди, что в бэклоге и автоматом не запустится, — и два последних доска
    не отвечала вовсе.
    """

    def queued(self, *plates):
        board = render.build_board(a_snapshot(list(plates)))
        return next(a for a in board["panels"][0]["areas"] if a["key"] == "queued")

    def test_the_queue_stands_in_the_order_the_plan_set(self):
        late = a_task(id=1, dir="001-t", board={
            "area": "queued", "since": "2026-08-01T00:00:00+00:00",
            "plan_place": a_plan_place(position=7)})
        early = a_task(id=2, dir="002-t", board={
            "area": "queued", "since": "2026-08-09T00:00:00+00:00",
            "plan_place": a_plan_place(position=2)})
        self.assertEqual([p["id"] for p in self.queued(late, early)["plates"]], [2, 1])

    def test_a_task_the_plan_never_queued_stands_after_the_plan_order(self):
        # Она стоит за наблюдаемым держателем, и очередью её никто не называл:
        # смешивать её с установленным порядком значило бы придумать ей место.
        planned = a_task(id=1, dir="001-t", board={
            "area": "queued", "since": "2026-08-09T00:00:00+00:00",
            "plan_place": a_plan_place(position=9)})
        held = a_task(id=2, dir="002-t", board={
            "area": "queued", "since": "2026-08-01T00:00:00+00:00",
            "blocked_by": "задача 851 ещё не закрыта", "plan_place": None})
        self.assertEqual([p["id"] for p in self.queued(planned, held)["plates"]], [1, 2])

    def test_the_backlog_holds_only_what_the_plan_holds_by_its_words(self):
        """Область «сам не запустится» несёт ровно то, что план держит строкой.

        Второй вид, «неразобранное», отсюда снят кругом 1 независимого ревью: им
        стояли задачи, которых не называет текущая редакция, а они разобраны в
        собственных task.md и запускаются автоматом. Признака разбора контур не
        наблюдает, и отсутствие раздела честнее ложного утверждения.
        """
        paused = a_task(id=1, dir="001-t", board={
            "area": "backlog", "since": "2026-08-01T00:00:00+00:00",
            "plan_place": a_plan_place(role="paused", position=None,
                                       line="1152 — на полке по слову пользователя")})
        unnamed = a_task(id=2, dir="002-t", board={
            "area": "pickup", "since": "2026-08-09T00:00:00+00:00",
            "plan_place": a_plan_place(role="unnamed", position=None, line=None,
                                       field=None)})
        board = render.build_board(a_snapshot([paused, unnamed]))
        areas = {a["key"]: a for a in board["panels"][0]["areas"]}
        self.assertEqual([p["id"] for p in areas["backlog"]["plates"]], [1])
        self.assertEqual([p["id"] for p in areas["pickup"]["plates"]], [2])

    def test_the_section_shows_what_a_line_names_apart_from_its_subject(self):
        # «После неё 1054, затем 1067» — план об этих работах говорит, а порядок
        # из прозы не читается. Обе стороны видны: строка целиком, названные ею
        # задачи отдельно и наблюдаемое состояние каждой рядом.
        entry = a_plan_entry(text="1121 — идёт сейчас; после неё 1136",
                             also=[{"id": 1136, "title": "Отчёт", "status": "planned"}])
        task = a_task(id=1136, dir="1136-t", board={"area": "pickup"})
        board = render.build_board(a_snapshot([task], plan=a_plan(queue=[entry])))
        also = board["plan"]["queue"][0]["also"]
        self.assertEqual([(t["id"], t["area"]) for t in also], [(1136, "pickup")])

    def test_recently_closed_work_is_what_the_done_area_shows(self):
        """«Что недавно закрыто» — вопрос пользователя, и он был отвечён наоборот.

        Область читалась от старого к свежему, а показывает она двадцать пять
        плашек из двухсот двенадцати: на живом состоянии это были самые древние
        завершённые задачи, то есть ровно не то, о чём спрашивали.
        """
        old = a_task(id=1, dir="001-t", board={"area": "done",
                                               "since": "2026-07-21T00:00:00+00:00"})
        today = a_task(id=2, dir="002-t", board={"area": "done",
                                                 "since": "2026-08-13T00:00:00+00:00"})
        board = render.build_board(a_snapshot([old, today]))
        area = next(a for a in board["panels"][0]["areas"] if a["key"] == "done")
        self.assertEqual([p["id"] for p in area["plates"]], [2, 1])
        # И «когда» видно у каждой записи: возраст меряется от наблюдённого
        # мгновения, а не от сборки, поэтому сегодняшнее закрытие отличимо от
        # июльского без обращения к карточке.
        self.assertTrue(all(p["since"] for p in area["plates"]))

    def test_the_section_names_the_plan_as_the_owner_of_the_order(self):
        board = render.build_board(a_snapshot())
        self.assertEqual(board["plan"]["revision"], 31)
        self.assertEqual([t["id"] for t in board["plan"]["queue"][0]["tasks"]], [1121])

    def test_a_plan_result_joins_observed_state_time_reason_and_transition(self):
        outcome = {"title": "Результат", "text": "**Результат (1121).**",
                   "tasks": [{"id": 1121, "title": "Работа", "status": "planned"}],
                   "goals": [],
                   "next": ["1121 — проверить live"], "checked": "current_plan now/next"}
        task = a_task(id=1121, dir="1121-t", flags=["blocked"], board={
            "area": "stuck", "since": "2026-08-15T09:00:00+00:00",
            "why": "Не завершена live-проверка", "why_src": "completion_refusal"})
        board = render.build_board(a_snapshot([task], plan=a_plan(outcomes=[outcome])))
        shown = board["plan"]["outcomes"][0]
        self.assertEqual((shown["state"], shown["updated_at"]),
                         ("stuck", "2026-08-15T09:00:00+00:00"))
        self.assertEqual(shown["reason"], "Не завершена live-проверка")
        self.assertEqual(shown["next"], ["1121 — проверить live"])

    def test_a_result_without_observation_is_not_called_stuck(self):
        outcome = {"title": "Продукт А", "text": "**Продукт А — по расписанию.**",
                   "tasks": [], "goals": [], "next": [], "checked": "current plan"}
        shown = render.build_board(a_snapshot(plan=a_plan(outcomes=[outcome])))["plan"]["outcomes"][0]
        self.assertEqual(shown["state"], "unknown")
        self.assertIsNone(shown["updated_at"])
        self.assertIsNone(shown["reason"])

    def test_a_linked_task_outside_every_thread_uses_the_catalogue_observation(self):
        outcome = {"title": "Статья", "text": "**Статья (1192).**",
                   "tasks": [{"id": 1192, "title": "Черновик", "status": "completed"}],
                   "goals": [], "next": [], "checked": "current plan outcome_links"}
        snapshot = a_snapshot(plan=a_plan(outcomes=[outcome]))
        snapshot["task_index"] = [{"id": 1192, "task": "1192-article",
                                   "title": "Черновик", "status": "completed",
                                   "updated_at": "2026-08-15T22:42:18+00:00",
                                   "updated_src": "mtime task.md"}]

        shown = render.build_board(snapshot)["plan"]["outcomes"][0]

        self.assertEqual((shown["state"], shown["updated_at"], shown["updated_src"]),
                         ("done", "2026-08-15T22:42:18+00:00", "mtime task.md"))

    def test_a_direction_only_snapshot_tolerates_the_missing_catalogue(self):
        outcome = {"title": "Статья", "text": "**Статья (1192).**",
                   "tasks": [{"id": 1192, "title": "Черновик", "status": "completed"}],
                   "goals": [], "next": [], "checked": "current plan outcome_links"}
        snapshot = a_snapshot(plan=a_plan(outcomes=[outcome]))
        snapshot["task_index"] = []

        shown = render.build_board(snapshot)["plan"]["outcomes"][0]

        self.assertEqual(shown["state"], "unknown")
        self.assertIsNone(shown["updated_at"])

    def test_a_done_result_does_not_carry_a_stale_jam_reason(self):
        outcome = {"title": "Результат", "text": "**Результат.**",
                   "tasks": [{"id": 1121, "title": "Работа", "status": "completed"}],
                   "goals": [], "next": [], "checked": "current plan"}
        task = a_task(id=1121, dir="1121-t", status="completed", board={
            "area": "done", "why": "не пройдено гейтов: 7",
            "why_src": "verification.md"})
        board = render.build_board(a_snapshot([task], plan=a_plan(outcomes=[outcome])))
        shown = board["plan"]["outcomes"][0]
        self.assertEqual(shown["state"], "done")
        self.assertIsNone(shown["reason"])
        self.assertIsNone(shown["reason_src"])

    def test_an_explicit_goal_link_supplies_observed_state_time_and_reason(self):
        outcome = {"title": "Продукт А", "text": "**Продукт А — по расписанию.**",
                   "tasks": [], "goals": ["0002"], "next": [],
                   "checked": "current plan outcome_links"}
        goal = {"id": "0002", "state": "active", "control": "reinforced",
                "outcome": "штатный отчёт приходит", "observable": [],
                "main_task": None, "correctives": [], "gap": "ждёт timer 00:15",
                "next_transition": "наблюдать receipt", "pause": None, "signals": [],
                "waiting_on": [], "updated_at": "2026-08-15T10:00:00+00:00",
                "src": "долговечная запись цели 0002"}
        snapshot = a_snapshot(plan=a_plan(outcomes=[outcome]))
        snapshot["threads"][0]["goals"] = [goal]
        shown = render.build_board(snapshot)["plan"]["outcomes"][0]
        self.assertEqual((shown["state"], shown["reason"]),
                         ("stuck", "ждёт timer 00:15"))
        self.assertEqual(shown["updated_at"], "2026-08-15T10:00:00+00:00")

    def test_cancelled_or_superseded_is_not_presented_as_done(self):
        for status in ("cancelled", "superseded"):
            outcome = {"title": "Результат", "text": "**Результат (1121).**",
                       "tasks": [{"id": 1121, "title": "Работа", "status": status}],
                       "goals": [], "next": [], "checked": "current plan"}
            task = a_task(id=1121, dir="1121-t", status=status, board={"area": "done"})
            shown = render.build_board(a_snapshot([task], plan=a_plan(outcomes=[outcome])))["plan"]["outcomes"][0]
            self.assertEqual(shown["state"], "unknown", status)

    def test_every_task_of_a_multi_task_result_must_be_observed_before_done(self):
        outcome = {"title": "Цепочка", "text": "**Цепочка (1121, 1136).**",
                   "tasks": [{"id": 1121, "title": "Один", "status": "completed"},
                             {"id": 1136, "title": "Два", "status": "completed"}],
                   "goals": [], "next": [], "checked": "current plan"}
        only_one = a_task(id=1121, dir="1121-t", status="completed", board={"area": "done"})
        shown = render.build_board(a_snapshot([only_one], plan=a_plan(outcomes=[outcome])))["plan"]["outcomes"][0]
        self.assertEqual(shown["state"], "unknown")

    def test_the_section_shows_the_observed_state_beside_the_plan_order(self):
        # Расхождение не прячется: план назвал очередь, наблюдение видит задачу
        # живой, и обе стороны стоят рядом.
        task = a_task(id=1121, dir="1121-t", board={"area": "running"})
        board = render.build_board(a_snapshot([task]))
        self.assertEqual(board["plan"]["queue"][0]["tasks"][0]["area"], "running")

    def test_the_section_says_nothing_about_a_task_the_revision_leaves_out(self):
        """Молчание плана не становится разделом и не двигает задачу.

        Раздел «неразобрано» здесь был и снят кругом 1 независимого ревью: в нём
        стояли задачи, которых просто не называет текущая редакция, а они
        разобраны в собственных task.md (1091, 1093, 1130, 1135) и запускаются
        автоматом. Наблюдаемого признака разбора у контура нет.
        """
        task = a_task(id=1135, dir="1135-t", title="Публичный адаптер", board={
            "area": "pickup", "plan_place": a_plan_place(role="unnamed", position=None,
                                                         line=None, field=None)})
        board = render.build_board(a_snapshot([task]))
        self.assertEqual(set(board["plan"]), {"revision", "accepted_at", "src",
                                              "outcomes", "queue", "backlog"})
        areas = {a["key"]: a for a in board["panels"][0]["areas"]}
        self.assertEqual([p["id"] for p in areas["pickup"]["plates"]], [1135])
        self.assertEqual(areas["backlog"]["plates"], [])


class TheWakeUpSeesTheQueueAndTheBacklog(unittest.TestCase):
    """Смежный потребитель того же наблюдателя — пробуждение направления.

    Раньше «можно подхватить» было для него всей работой на выбор, и в нём
    вперемешку стояли задачи из очереди плана и работа, остановленная словом
    пользователя. Разделение — это не потеря: то, что план ставит в очередь и
    что ничем не держится, простоем по-прежнему не считается, а остановленное
    больше не предлагается к запуску.
    """

    def report(self, *tasks):
        observed = {"threads": [{"title": "Процессный контур", "products": [],
                                 "tasks": list(tasks), "repos": [], "task_count": len(tasks)}],
                    "owners_awake": []}
        with (mock.patch.object(thread, "load_thread", return_value={"repos": []}),
              mock.patch.object(thread.observer, "build", return_value=observed),
              mock.patch.object(thread, "process_inventory", return_value=[]),
              mock.patch.object(thread.observer, "write_owner_observations")):
            return thread.build("process")

    def test_a_dead_run_of_a_closed_task_is_not_live_or_attention(self):
        closed = a_task(id=1098, dir="1098-t", status="completed",
                        flags=["stale_label"], run={"state": "running"})
        report = self.report(closed)
        self.assertEqual(report["live_runs"], [])
        self.assertEqual(report["needs_attention"], [])

    def test_a_dead_run_of_an_open_task_stays_live_and_attention(self):
        open_task = a_task(id=1098, dir="1098-t", status="planned",
                           flags=["stale_label"], run={"state": "running"})
        report = self.report(open_task)
        self.assertEqual([task["id"] for task in report["live_runs"]], [1098])
        self.assertEqual([task["id"] for task in report["needs_attention"]], [1098])

    def test_a_live_process_remains_live_even_after_its_task_closed(self):
        closing = a_task(id=1098, dir="1098-t", status="completed",
                         run={"state": "running", "alive": True})
        self.assertEqual([task["id"] for task in self.report(closing)["live_runs"]],
                         [1098])

    def test_the_queue_of_the_plan_is_its_own_list_in_the_order_of_the_plan(self):
        second = a_task(id=1136, dir="1136-t", board={
            "area": "queued", "plan_place": a_plan_place(position=10)})
        first = a_task(id=1121, dir="1121-t", board={
            "area": "queued", "plan_place": a_plan_place(position=9)})
        report = self.report(second, first)
        self.assertEqual([t["id"] for t in report["queued_by_plan"]], [1121, 1136])
        self.assertEqual(report["can_pick_up"], [])

    def test_an_observed_holder_keeps_a_queued_task_out_of_what_could_start(self):
        held = a_task(id=746, dir="746-t", board={
            "area": "queued", "blocked_by": "задача 803 ещё не закрыта",
            "plan_place": a_plan_place(position=18)})
        self.assertEqual(self.report(held)["queued_by_plan"], [])

    def test_what_the_plan_holds_by_its_word_is_not_offered_to_start(self):
        # И только оно: задача, которой редакция не называет, остаётся работой на
        # выбор — «план о ней не говорит» не то же самое, что «она не разобрана»
        # (круг 1 независимого ревью, HIGH-2).
        paused = a_task(id=1152, dir="1152-t", board={
            "area": "backlog", "plan_place": a_plan_place(role="paused", position=None)})
        unnamed = a_task(id=1135, dir="1135-t", board={
            "area": "pickup", "plan_place": a_plan_place(role="unnamed", position=None,
                                                         line=None, field=None)})
        # И работа, которую держащая строка только называет, остаётся доступной:
        # круг 3 независимого ревью показал, что иначе строка «исследование 1150
        # и 1151 разрешено» убирает из запуска прямо разрешённую пользователем
        # работу.
        allowed = a_task(id=1150, dir="1150-t", board={
            "area": "pickup", "plan_place": a_plan_place(role="mentioned", position=None)})
        report = self.report(paused, unnamed, allowed)
        self.assertEqual([(t["id"], bool(t["src"])) for t in report["backlog"]],
                         [(1152, True)])
        self.assertEqual([t["id"] for t in report["can_pick_up"]], [1135, 1150])

    def test_the_plan_queue_counts_as_work_this_direction_could_start(self):
        # Иначе направление, чью очередь установил план, читалось бы как «делать
        # нечего» — то самое молчание, ради которого тик и написан.
        report = {"can_pick_up": [], "ready_to_start": [], "decided_not_done": [],
                  "queued_by_plan": [{"id": 1121}]}
        self.assertEqual(tick.startable(report), 1)

    def test_a_moved_queue_is_news_at_the_next_tick(self):
        before = tick.snapshot(_a_tick_report())
        after = tick.snapshot(_a_tick_report(queued_by_plan=[{"id": 1121}]))
        self.assertNotEqual(before["plan_queue"], after["plan_queue"])


def _a_tick_report(**over) -> dict:
    report = {"live_runs": [], "needs_attention": [], "repos": [],
              "ready_to_start": [], "decided_not_done": [], "can_pick_up": [],
              "undelivered": [], "waiting_user": [], "worktrees": [],
              "owners_awake": [], "long_lived_processes": [],
              "long_lived_processes_observation": {"available": True, "reason": None},
              "queued_by_plan": []}
    report.update(over)
    return report


class ThePlanContract(unittest.TestCase):
    """Что снимок обязан сказать о плане, чтобы доска имела право это показать."""

    def test_a_well_formed_plan_passes(self):
        schema.validate_snapshot(a_snapshot())

    def test_an_entry_without_the_comparison_is_refused(self):
        # То же правило, под которым живёт «Надо запланировать»: запись, которая
        # называет задачу или сообщает, что не смогла, обязана сказать, с чем
        # сверялась. Иначе очереди можно только верить.
        broken = a_snapshot(plan=a_plan(queue=[a_plan_entry(checked="  ")]))
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(broken)

    def test_a_backlog_entry_without_its_kind_is_refused(self):
        entry = dict(BACKLOG_ENTRY)
        del entry["kind"]
        broken = a_snapshot(plan=a_plan(backlog=[a_plan_entry(**entry)]))
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(broken)

    def test_a_queue_without_a_revision_is_refused(self):
        broken = a_snapshot(plan=a_plan(revision=None, accepted_at=None))
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(broken)

    def test_no_revision_at_all_is_a_valid_answer(self):
        schema.validate_snapshot(a_snapshot(plan=a_plan(
            revision=None, accepted_at=None, queue=[], backlog=[],
            src="редакций плана нет в content/plan/revisions")))

    def test_a_place_without_its_observation_is_refused(self):
        broken = a_snapshot([a_task(board={"plan_place": a_plan_place(src="  ")})])
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(broken)

    def test_a_place_in_the_queue_without_a_number_is_refused(self):
        broken = a_snapshot([a_task(board={"plan_place": a_plan_place(position=None)})])
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(broken)

    def test_an_unknown_role_is_refused(self):
        broken = a_snapshot([a_task(board={"plan_place": a_plan_place(role="soon")})])
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(broken)

    def test_a_task_that_is_both_the_subject_and_a_mention_is_refused(self):
        # По различию решается, назначает ли строка задаче место; если задача
        # стоит с обеих сторон, различие ничего не значит.
        broken = a_snapshot(plan=a_plan(queue=[a_plan_entry(
            also=[{"id": 1121, "title": "Продукт: первый живой запуск", "status": "planned"}])]))
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(broken)

    def test_a_holding_place_without_its_line_is_refused(self):
        # Задача снята с «можно подхватить» строкой плана — значит, строка обязана
        # быть показана: иначе доска держит работу утверждением, которого нет.
        broken = a_snapshot([a_task(board={"plan_place": a_plan_place(
            role="paused", position=None, line=None)})])
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(broken)

    def test_a_second_line_of_the_plan_without_its_text_is_refused(self):
        broken = a_snapshot([a_task(board={"plan_place": a_plan_place(
            conflict=[{"role": "paused", "line": "  ", "field": "next", "src": "строка"}])})])
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(broken)

    def test_the_plan_is_cleaned_when_the_showing_is_anonymous(self):
        # Строка плана — обычный текст документа, и путь внутри неё уезжает с
        # остальными: исключений из очистки нет ни у кого.
        cleaned = schema.scrub(a_snapshot(plan=a_plan(
            queue=[a_plan_entry(text="1121 — см. /opt/projects/example-engine")])))
        self.assertNotIn("/opt/projects", cleaned["plan"]["queue"][0]["text"])


class TheTopOfTheBoard(unittest.TestCase):
    """Задача 1067, первая половина: сверху лента изменений, а не те же счётчики.

    «Пользователь сверху доски видит недавнюю ленту изменений с временем, а не
    повторную сводку тех же количеств, которые уже показаны ниже.» Полная
    разбивка никуда не делась — она в колонках, где число стоит рядом с теми
    задачами, которые считает.
    """

    def page(self) -> str:
        return (Path(__file__).parent / "process_map_template.html").read_text()

    def test_the_old_repeated_counts_are_gone_from_the_head(self):
        page = self.page()
        self.assertNotIn("boardsub", page)
        self.assertNotIn('" · ждёт решения пользователя "', page)
        self.assertNotIn('" · надо запланировать "', page)
        self.assertNotIn('" · можно подхватить "', page)
        # И из шапки колонки они ушли тоже (задача 1163): шесть равновесных
        # чисел, из которых большинство нули, заменены одной строкой состояния —
        # «идёт / стоит / ждёт пользователя / на паузе» и ближайшая работа.
        self.assertNotIn('" · заторов " + stuck', page)
        self.assertIn("function stateNode(panel)", page)

    def test_the_head_no_longer_counts_what_the_strip_names(self):
        """«В работе 1» — число без подлежащего, и тот же факт с именем задачи,
        автором и возрастом стоит на двести пикселей ниже, в полосе-сводке.
        Дубль снят задачей 1163; «ждёт решения пользователя» осталось — оно
        единственное обращено к человеку.
        """
        head = self.top("function drawHead")
        self.assertIn('at.textContent = "обновлено "', head)
        self.assertNotIn('live.textContent = "В работе " + running', head)
        # «Ждёт решения пользователя» — только когда есть что ждать, и элемента
        # при нуле нет вовсе, а не «есть, но серый».
        self.assertIn("if (board.waiting) {", head)
        self.assertIn('waiting.className = "count waiting"', head)
        # И режим доски человеку не сообщается: «живой режим» — наш режим.
        self.assertNotIn('el("boardmode").textContent = "живой режим"', self.page())

    def test_the_colours_are_the_ones_the_user_named(self):
        css = self.page()
        self.assertIn("#boardstate .count.waiting { color: var(--blocked); }", css)
        # Жёлтое при нуле и зелёное при ненулевом — теперь у слова состояния
        # направления, которое и называет, что именно идёт.
        self.assertIn(".state .mark.live { color: var(--live); }", css)
        self.assertIn(".state .mark.idle { color: var(--undelivered); }", css)

    def test_the_feed_is_fresh_first_with_the_exact_instant_of_each_change(self):
        feed = self.top("function drawFeed")
        self.assertIn("when.textContent = stamp(record.at)", feed)
        self.assertIn('kind.textContent = ru(KIND_RU, record.kind)', feed)
        # Подпись записи проходит через `said`: это тот же `human`, а английская
        # запись заменяется русской фразой и уезжает за переключатель (пункт 4).
        self.assertIn('what.textContent = label.shown;', feed)
        self.assertIn('const label = said(record.label,', feed)
        # Чем наблюдено — рядом с записью, как и у всего остального на доске.
        self.assertIn('sourceNote(row, [["Наблюдено", record.observed_by]', feed)
        # И сколько записей осталось за пределом — вслух.
        self.assertIn('" из " + total', feed)

    def test_the_page_gets_the_last_records_freshest_first_and_not_the_whole_file(self):
        old = a_record(at="2026-08-06T09:00:00+00:00", label="старая")
        new = a_record(at="2026-08-06T10:00:00+00:00", label="свежая")
        with tempfile.TemporaryDirectory() as home:
            timeline = Path(home) / "timeline.jsonl"
            timeline.write_text("\n".join(
                json.dumps(r, ensure_ascii=False)
                for r in [old] + [a_record(label=f"шум {n}") for n in range(30)] + [new]))
            snapshot = Path(home) / "snapshot.json"
            snapshot.write_text(json.dumps(a_snapshot(), ensure_ascii=False))
            data = render.payload(timeline, snapshot)
        self.assertEqual(data["timeline_total"], 32)
        self.assertEqual(len(data["timeline"]), render.TIMELINE_SHOWN)
        self.assertEqual(data["timeline"][0]["label"], "свежая")
        self.assertNotIn("старая", [r["label"] for r in data["timeline"]])

    def top(self, marker: str) -> str:
        page = self.page()
        start = page.index(marker)
        return page[start:page.index("\n}\n", start)]


class ContinueNow(unittest.TestCase):
    """Задача 1067, вторая половина: попросить продакта посмотреть сейчас.

    Кнопка будит управляющий цикл — тот самый юнит, который заводит
    двадцатиминутный таймер, — и не запускает выбранную задачу. Двух продактов
    не получается не потому, что мы что-то сторожим, а потому что systemd не
    держит двух экземпляров одной единицы.
    """

    def page(self) -> str:
        return (Path(__file__).parent / "process_map_template.html").read_text()

    # Which directions exist is a setting of the installation, not of the core,
    # so the tests of the button state one instead of borrowing whichever
    # `threads.json` happens to stand beside them.
    CONFIG = {"threads": {"process": {"title": "Процессный контур"}}}

    def test_the_request_starts_the_same_unit_the_timer_starts(self):
        commands = []

        def fake(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(state, "load_config", return_value=self.CONFIG), \
                mock.patch.object(serve.subprocess, "run", side_effect=fake), \
                mock.patch.object(state, "systemd", return_value="activating\n"):
            code, answer = serve.wake("process")
        self.assertEqual(code, 200)
        self.assertTrue(answer["accepted"])
        self.assertEqual(commands, [["systemctl", "start", "--no-block",
                                     "product-thread@process.service"]])
        # Та же единица, которую запускает таймер направления.
        self.assertEqual(answer["unit"], state.wake_unit("process"))
        self.assertTrue(answer["wake"]["running"])

    def test_a_direction_the_configuration_does_not_know_is_refused(self):
        with mock.patch.object(state, "load_config", return_value=self.CONFIG), \
                mock.patch.object(serve.subprocess, "run") as run:
            code, answer = serve.wake("../../etc/passwd")
        self.assertEqual(code, 400)
        self.assertFalse(answer["accepted"])
        run.assert_not_called()

    def test_systemd_refusing_is_said_out_loud_and_not_swallowed(self):
        def refused(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "Failed to start unit")

        with mock.patch.object(state, "load_config", return_value=self.CONFIG), \
                mock.patch.object(serve.subprocess, "run", side_effect=refused), \
                mock.patch.object(state, "systemd", return_value="failed\n"):
            code, answer = serve.wake("process")
        self.assertEqual(code, 502)
        self.assertFalse(answer["accepted"])
        self.assertIn("Failed to start unit", answer["detail"])

    def test_the_button_never_starts_a_task_of_its_own(self):
        """Оно будит владельца, который читает план и решает сам.

        Ни номера задачи, ни `task_runner`, ни выбора работы на стороне доски:
        разница между «продолжи план» и «запусти вот это» — предметная, и
        нарушить её значило бы завести второй порядок работ рядом с планом.
        """
        source = code_only(Path(serve.__file__).read_text())
        for forbidden in ("task_runner", "tasks_index", "--task", "RUN_REGISTRY"):
            self.assertNotIn(forbidden, source)
        # И на странице запрос несёт только направление.
        self.assertIn('body: JSON.stringify({ thread })', self.page())

    def test_a_request_from_another_page_is_refused(self):
        """Право доступа к действию не шире права доступа к самой доске.

        Доска слушает только петлю и никого не спрашивает, кто он. Значит,
        сторонняя страница, открытая в том же браузере, не должна уметь будить
        продакта: браузер обязан прислать `Origin` при кросс-запросе, а форма —
        единственная форма запроса без `Origin` — не может выставить этот тип
        содержимого без предварительного запроса, на который сервер не отвечает.
        """
        handler = serve.LiveHandler.__new__(serve.LiveHandler)
        handler.server = mock.Mock(server_address=("127.0.0.1", 8791))
        for headers, allowed in (
                ({}, True),
                ({"Origin": "http://127.0.0.1:8791"}, True),
                ({"Origin": "http://localhost:8791"}, True),
                ({"Origin": "http://example.com"}, False),
                ({"Origin": "http://127.0.0.1:9999"}, False),
                ({"Sec-Fetch-Site": "cross-site"}, False),
                ({"Sec-Fetch-Site": "same-origin"}, True)):
            handler.headers = headers
            self.assertEqual(handler._same_origin(), allowed, headers)

    def test_the_button_stands_in_every_column_and_in_every_state(self):
        """Слово пользователя: кнопки «Продолжить» там нет.

        Кнопка рисовалась при `!panel.live && panel.startable > 0`, и на живой
        доске в 10:41 CEST это означало ровно одну колонку из четырёх. Условие
        снято: нажатие не запускает задачу, оно будит владельца направления,
        который читает план и решает, — а это осмысленно в любом состоянии.
        Приёмочный пункт 12 задачи 1163.
        """
        node = self.function("function continueNode")
        self.assertNotIn("const worth =", node)
        self.assertNotIn("if (!worth && !running && !request) return null;", node)
        # Пока проверка идёт, второй запрос не отправить — и это сказано словами.
        self.assertIn("button.disabled = running || (request && (request.pending "
                      "|| request.accepted));", node)
        self.assertIn('said.textContent = "проверка идёт', node)
        # А там, где начинать нечего, кнопка не исчезает молча: она говорит, чего
        # не хватает (приёмочный пункт 10).
        self.assertIn('said.textContent = "начинать нечего: свободных задач '
                      'у направления нет — "', node)

    def test_the_board_says_the_request_was_accepted(self):
        asked = self.function("async function askToContinue")
        self.assertIn('"запрос принят "', asked)
        # Отказ говорит по-человечески, а ответ systemd — «Failed to connect to
        # bus: Operation not permitted» — стоит рядом за переключателем
        # источников: наблюдено на нажатии всех четырёх кнопок 2026-08-14.
        self.assertIn('"продолжить не удалось: проверку запускает systemd, и он отказал"',
                      asked)
        self.assertIn('detail: String(said.detail || answer.status)', asked)
        self.assertIn('"продолжить не удалось: доска сейчас без сервера"', asked)
        # И перестаёт это говорить, когда та самая проверка уже записана: с этого
        # момента на тот же вопрос лучше отвечает строка «прошлая … — …».
        node = self.function("function continueNode")
        self.assertIn("Date.parse(panel.check.at) > answered.at", node)

    def test_what_is_startable_means_the_same_to_the_board_and_to_the_wake_up(self):
        """Один предикат на два вопроса: «будить ли» и «предлагать ли продолжить».

        Раньше правило очереди плана было написано в `thread_state`, и доска о
        нём не знала вовсе. Два ответа на «есть ли что начать» — это две очереди.
        """
        queued = a_task(id=1, dir="001-t", board={
            "area": "queued", "blocked_by": None,
            "plan_place": a_plan_place(role="queue", position=1)})
        held = a_task(id=2, dir="002-t", board={
            "area": "queued", "blocked_by": "прогон 1 в том же дереве",
            "plan_place": a_plan_place(role="queue", position=2)})
        paused = a_task(id=3, dir="003-t", board={
            "area": "backlog", "blocked_by": None,
            "plan_place": a_plan_place(role="paused", position=None)})
        free = a_task(id=4, dir="004-t", board={"area": "pickup"})
        self.assertEqual([schema.startable(t) for t in (queued, held, paused, free)],
                         [True, False, False, True])
        panel = render.build_board(a_snapshot([queued, held, paused, free]))["panels"][0]
        self.assertEqual(panel["startable"], 2)
        self.assertEqual(panel["live"], 0)
        # И то же самое видит пробуждение, через свою проекцию того же снимка:
        # очередь плана — та же самая задача, а не другая.
        observed = {"threads": [{"title": "Процессный контур", "products": [],
                                 "tasks": [queued, held, paused, free], "repos": [],
                                 "task_count": 4}],
                    "owners_awake": []}
        with (mock.patch.object(thread, "load_thread", return_value={"repos": []}),
              mock.patch.object(thread.observer, "build", return_value=observed),
              mock.patch.object(thread, "process_inventory", return_value=[]),
              mock.patch.object(thread.observer, "write_owner_observations")):
            report = thread.build("process")
        self.assertEqual([item["id"] for item in report["queued_by_plan"]], [1])
        self.assertEqual([item["id"] for item in report["can_pick_up"]], [4])
        self.assertEqual(tick.startable(report), panel["startable"])

    def test_the_wake_observation_carries_what_observed_it(self):
        with mock.patch.object(state, "systemd", return_value="inactive\n"):
            answer = state.wake_state("process")
        self.assertFalse(answer["running"])
        self.assertIn("ActiveState=inactive", answer["src"])
        # А host без systemd отвечает «неизвестно», а не «проверка не идёт».
        with mock.patch.object(state, "systemd", return_value=None):
            answer = state.wake_state("process")
        self.assertIsNone(answer["running"])
        self.assertTrue(answer["src"].strip())
        with self.assertRaises(schema.ContractError):
            schema.validate_wake({"unit": "u", "running": "да", "src": "s"}, "пробуждение")
        with self.assertRaises(schema.ContractError):
            schema.validate_wake({"unit": "u", "running": True, "src": " "}, "пробуждение")

    def test_the_timer_stays_the_safety_net(self):
        """Кнопка не делает браузер условием автономной работы.

        Проверяется тем же способом, каким проверяется всё остальное про юниты:
        расписание принадлежит таймеру, и ничего в этой правке его не трогает.
        """
        unit = Path(__file__).parents[1] / "systemd"
        self.assertTrue(unit.is_dir())
        source = code_only(Path(serve.__file__).read_text())
        for forbidden in ("systemctl stop", "disable", "timer"):
            self.assertNotIn(forbidden, source)

    def function(self, marker: str) -> str:
        page = self.page()
        start = page.index(marker)
        return page[start:page.index("\n}\n", start)]


class TheFeedIsKeptFresh(unittest.TestCase):
    """Лента, которую никто не пишет, показывает позапрошлую неделю.

    Писец существует, его контракт проверен, и запускать его отдельной
    долгоживущей единицей ради данных, нужных только пока доска открыта, дороже,
    чем звать его из самой доски. Условия ровно два: не чаще, чем раз в интервал,
    и после ответа, чтобы опрос за это не платил.
    """

    def test_a_look_is_taken_at_most_once_per_interval(self):
        with tempfile.TemporaryDirectory() as home:
            timeline = Path(home) / "timeline.jsonl"
            cursor = timeline.with_suffix(".cursor.json")
            cursor.write_text("{}")
            with mock.patch.object(serve.recorder, "Scribe") as scribe:
                self.assertEqual(serve.record_timeline(timeline), 0)
                scribe.assert_not_called()
            os.utime(cursor, (0, 0))
            with mock.patch.object(serve.recorder, "Scribe") as scribe:
                scribe.return_value.tick.return_value = 3
                self.assertEqual(serve.record_timeline(timeline), 3)

    def test_a_look_that_fails_does_not_take_the_board_down(self):
        with tempfile.TemporaryDirectory() as home:
            timeline = Path(home) / "timeline.jsonl"
            cursor = timeline.with_suffix(".cursor.json")
            cursor.write_text("{}")
            os.utime(cursor, (0, 0))
            with mock.patch.object(serve.recorder, "Scribe", side_effect=OSError("нет диска")):
                self.assertEqual(serve.record_timeline(timeline), 0)

    def test_the_look_is_taken_after_the_answer_has_gone_out(self):
        source = code_only(Path(serve.__file__).read_text())
        answer = source.index("self . _send ( json . dumps ( data")
        self.assertLess(answer, source.index("record_timeline ( self . timeline )"))


class AChangeIsStampedWhenItHappened(unittest.TestCase):
    """Позднее наблюдение — не время изменения (HIGH-1 независимого ревью).

    Писец зовётся с доски, поэтому между взглядами бывает неделя, и первый
    взгляд после перерыва находит все переходы этой недели разом. Пока смена
    статуса помечалась временем взгляда, недельной давности завершения
    поднимались наверх ленты одной секундой и вытесняли действительно свежее.
    """

    WEEK_AGO = "2026-08-06T09:00:00+00:00"

    def a_task_whose_status_changed_long_ago(self, home: Path) -> Path:
        """Задача, у которой статус сменился неделю назад, а курсор устарел."""
        task_dir = home / "775-capture-portfolio-instruments"
        task_dir.mkdir()
        (task_dir / "task.md").write_text('---\nstatus: "completed"\ntitle: "Задача"\n---\n')
        stale = datetime.fromisoformat(self.WEEK_AGO).timestamp()
        os.utime(task_dir / "task.md", (stale, stale))
        return task_dir

    def a_scribe_that_last_looked_before_the_change(self, home: Path) -> tuple:
        out, cursor = home / "t.jsonl", home / "c.json"
        cursor.write_text(json.dumps(
            {"status": {"775-capture-portfolio-instruments": "planned"}}))
        scribe = recorder.Scribe(out, cursor, anonymize=False)
        scribe.seeding = False
        return scribe, out

    def test_a_transition_found_late_carries_the_instant_it_was_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            task_dir = self.a_task_whose_status_changed_long_ago(home)
            scribe, out = self.a_scribe_that_last_looked_before_the_change(home)
            scribe.observe_task(task_dir)
            records = [json.loads(line) for line in out.read_text().splitlines()]
        changes = [r for r in records if r["kind"] == "task_status"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["label"], "planned → completed")
        # Ровно то время, когда был написан файл, из frontmatter которого этот
        # переход и прочитан, — а не время взгляда.
        self.assertEqual(render.instant(changes[0]),
                         datetime.fromisoformat(self.WEEK_AGO))
        self.assertIn("mtime", changes[0]["observed_by"])

    def test_a_week_of_late_transitions_does_not_push_out_what_is_really_fresh(self):
        # То же самое, но глазами пользователя: наверху доски должно остаться
        # сегодняшнее, а не пачка позапрошлонедельных завершений.
        today = datetime.now(timezone.utc).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            task_dir = self.a_task_whose_status_changed_long_ago(home)
            scribe, out = self.a_scribe_that_last_looked_before_the_change(home)
            for number in range(render.TIMELINE_SHOWN + 1):
                scribe.cursor["status"][task_dir.name] = f"planned-{number}"
                scribe.observe_task(task_dir)
            with out.open("a") as handle:
                handle.write(json.dumps(a_record(at=today, label="сегодняшнее"),
                                        ensure_ascii=False) + "\n")
            snapshot = home / "snapshot.json"
            snapshot.write_text(json.dumps(a_snapshot(), ensure_ascii=False))
            data = render.payload(out, snapshot)
        self.assertEqual(data["timeline"][0]["label"], "сегодняшнее")


class TheFirstScreenAnswersFourQuestions(unittest.TestCase):
    """Задача 1163: доска отвечает адресату, а не проверяющему.

    Приёмка этой работы — живое открытие доски в браузере и чтение экрана; она
    снята на настоящем состоянии и лежит в
    `tasks/1163-board-answers-four-questions-first-screen/verification.md`.
    Здесь держатся правила, которые стеклу не видны и тихо уползают обратно:
    порядок блоков, отбор строк, переключатель источников и то, чем доска
    отвечает, когда не идёт ничего.
    """

    def page(self) -> str:
        return (Path(__file__).parent / "process_map_template.html").read_text()

    def function(self, name: str) -> str:
        text = self.page()
        start = text.index(name)
        return text[start:text.index("\nfunction ", start + 1)]

    def test_the_answers_stand_above_the_history_and_the_queue(self):
        # Порядок в разметке — это порядок на экране, и он же был предметом:
        # ответы, потом подробности. Лента наблюдений и очередь плана занимали
        # весь первый экран, а колонки начинались на 2300-м пикселе.
        text = self.page()
        order = [text.index('<div id="' + name + '">')
                 for name in ("strip", "columns", "plan", "feed", "foot")]
        self.assertEqual(order, sorted(order), "порядок блоков доски переставлен")

    def test_a_direction_says_its_state_in_one_line(self):
        node = self.function("function stateNode")
        for word in ("идёт", "ждёт вашего решения", "стоит", "на паузе",
                     "простаивает", "свободно"):
            self.assertIn('mark = "' + word + '"', node)
        # И ближайшую работу — это четвёртая часть первого вопроса.
        self.assertIn("next.textContent = NEXT_RU[key] + \": \";", node)

    def test_nothing_running_is_answered_with_a_reason_and_a_next_step(self):
        """Пункт 2: «Когда ничего не идёт, доска говорит почему, а не „пусто“».

        Причины — те самые слова, которые назвал пользователь: занят другой
        продакт, занято рабочее дерево, исчерпан бюджет, ждём ответа
        пользователя. Их пишет тик, доска их не выдумывает.
        """
        node = self.function("function stateNode")
        self.assertIn("if (!running.count) for (const reason of idleReasons(panel.check))",
                      node)
        self.assertIn('what = "работу держит слово в плане, запускать нечего"', node)
        self.assertIn('what = "всё, что стоит, чем-то занято, и начать нечего"', node)

    def test_an_empty_area_does_not_take_the_screen(self):
        # Пункт 7. Около тридцати пяти областей из сорока восьми на живом
        # состоянии печатали заголовок и слово «пусто».
        text = self.page()
        self.assertIn("if (area.count) body.appendChild(areaNode(panel, area));", text)
        self.assertIn('foldable(node, "voids", "пусто в " + empty.length + " областях: "',
                      self.function("function voidsNode"))
        self.assertNotIn('empty.textContent = area.key === "waiting_human"', text)

    def test_the_apparatus_of_provenance_is_behind_one_switch(self):
        """Пункт 6: «Источник», «Поле редакции», «Сверка», «также названы» — за
        одним переключателем на всю страницу, и ничего из этого не удалено."""
        text = self.page()
        self.assertIn("body:not(.prov) .srcbtn,", text)
        self.assertIn("body:not(.prov) .prov { display: none; }", text)
        # Выключено по умолчанию: `sessionStorage` пуст — источников нет.
        self.assertIn('showSources(sessionStorage.getItem("sources") === "1");', text)
        # И сам аппарат никуда не делся: кнопка источника по-прежнему строится.
        self.assertIn("function sourceNote(node, pairs)", text)

    def test_the_queue_holds_no_finished_rows_and_opens_with_a_button(self):
        # Пункт 8. Две из четырёх видимых строк очереди были помечены
        # «наблюдение: сделано» и занимали её верх.
        text = self.page()
        self.assertIn("function planDone(entry)", text)
        self.assertIn("const open = numbered_entries.filter(([entry]) => !planDone(entry));",
                      text)
        self.assertIn('more.textContent = shut ? "показать все " + entries.length', text)
        # Направление — на самой строке очереди, а не в источнике.
        self.assertIn('where.textContent = " · " + task.thread;', text)

    def test_markdown_is_taken_off_the_text_the_board_prints(self):
        # Пункт 4. Страница печатает текстовые узлы, поэтому `**жирный**`
        # доезжал до экрана символами. Снимается одной строкой внутри `human`,
        # отдельной механики у этого нет.
        self.assertIn(r'return out.replace(/\*\*|~~|`/g, "")', self.page())

    def test_the_line_a_child_writes_itself_is_behind_the_switch(self):
        # Пункт 4. Строку прогресса пишет ребёнок-исполнитель, и она бывает
        # целиком английской: «Adjudicated all 33 machine-flagged cases…». На
        # вопрос «что происходит» она не отвечает, поэтому на плашке стоит за тем
        # же переключателем, что и остальной аппарат, а не вместо ответа.
        self.assertIn("body:not(.prov) .doing,", self.page())

    def test_an_english_line_is_replaced_by_one_russian_phrase(self):
        # Пункт 4, круг 3. Там, где строка стоит ответом на вопрос — причина
        # затора в полосе и в колонке, подпись записи в ленте, — её нельзя ни
        # перевести (это было бы выдумыванием факта), ни оставить: доска говорит
        # одной русской фразой, что это, а сама строка уезжает за переключатель
        # источников. Порог — три латинских слова подряд, поэтому `Платформа`
        # и `progress.json` остаются на месте.
        text = self.page()
        self.assertIn("function said(text, instead)", text)
        self.assertIn("запись сделана не для этого экрана — целиком в источнике", text)
        # Английское решается до склейки: в собранной строке колонки латиница
        # утонула бы в русском начале и осталась бы на экране (круг 2, HIGH-2).
        self.assertIn('const reason = said(jam.why, insteadOfWords(jam, "на плашке ниже"));',
                      text)

    def test_a_name_written_for_a_machine_needs_a_russian_phrase_around_it(self):
        # Пункт 4, круги 4 и 6. Имя через подчёркивание пишут машине, и порог
        # английской прозы его не видит: подчёркивание для него — буква, поэтому
        # список гейтов он считает одним словом. Круг 4 из-за этого удерживал
        # такое имя всегда; круг 6 спрашивает у него перевес латиницы внутри
        # своего предложения — «Нужно ли закрыть verification_gap задачи 495?»
        # это вопрос о предмете, а «Кандидат: pass36_repaired_git_…» — имя с
        # русской наклейкой.
        text = self.page()
        self.assertIn("const MACHINE = /[A-Za-z0-9]+_[A-Za-z0-9_]*[A-Za-z]/;", text)
        self.assertIn("if (!MACHINE.test(one)) return true;", text)
        self.assertIn("return (one.match(/[А-Яа-яЁё]/g) || []).length"
                      " >= (one.match(/[A-Za-z]/g) || []).length;", text)
        # Имя остаётся именем: слаг и путь через дефис порогом не задеты.
        self.assertIn("const ENGLISH = /[A-Za-z]{3,}", text)

    def test_the_border_decides_about_a_sentence_and_not_about_the_whole_line(self):
        # Пункт 4, круги 5 и 6. Перевес латиницы по всей строке решал в пользу
        # русского начала, и английская проза оставалась на первом экране
        # (замечание 2 приёмки продакта); снятый перевес уносил за источник
        # целый читаемый абзац из-за одного имени в середине, и на экране
        # оставалась заглушка (приёмка продакта, круг 5). Единица решения —
        # предложение: вводная не голосует за чужое предложение, а чужое не
        # уносит с собой русские.
        text = self.page()
        self.assertIn("function toReader(one)", text)
        self.assertIn(r"for (const piece of String(out).split(/(?<=[.!?…])\s+|\n+/))", text)
        # Обрыв на первом чужом предложении, а не выборка читаемых по всему
        # тексту: показать второе вместо первого значило бы переставить чужие
        # слова местами.
        self.assertIn("if (!toReader(one)) { whole = false; break; }", text)
        # И о том, что за источником есть ещё, говорит многоточие.
        self.assertIn('shown: kept.length ? kept.join(" ") + " …" : instead', text)

    def test_a_reason_that_cannot_be_shown_is_replaced_by_what_was_observed(self):
        # Приёмка продакта 2026-08-14, круг 5: «причина написана не для этого
        # экрана» — это то же «пусто», только вежливое. Пользователь спрашивает
        # «где затор и почему», и на месте ответа стоит наблюдённое — вердикт
        # проверяющего, непройденные гейты, ярлык, записанное состояние, — а
        # отсылка к источнику остаётся только хвостом после настоящей фразы.
        text = self.page()
        self.assertIn("function insteadOfWords(p, where)", text)
        self.assertIn('what = "вердикт проверяющего: " + ru(VERDICT_RU, review.verdict)', text)
        self.assertIn('} else if (failed) what = "не пройдено гейтов: " + failed;', text)
        self.assertIn('return (what ? what + "; " : "") + "как записано — " + where;', text)
        # И все четыре места, где стояла заглушка, спрашивают одно и то же:
        # плашка, полоса «Где затор», очередь и затор в строке состояния
        # направления — четыре вызова при одном определении.
        self.assertEqual(5, text.count("insteadOfWords("))
        self.assertIn('const reason = said(p.why, insteadOfWords(p, "в источнике"));', text)

    def test_our_own_question_goes_through_the_same_border_as_a_stranger(self):
        # Пункт 4, круг 5. Вопрос области «Решает продакт» пишем мы сами и как
        # придётся; на первом экране он стоял английской прозой. Граница у него
        # та же, что у вердикта и причины затора, и полный текст открывается тем
        # же переключателем источников.
        text = self.page()
        self.assertIn('said(q.text, "вопрос записан не для этого экрана'
                      ' — целиком в источнике")', text)
        self.assertIn('sources.push(["Вопрос, как записан", asked.full])', text)
        # А вопрос к человеку через неё не идёт: «что требуется лично от вас» —
        # первый вопрос доски, и замена его фразой была бы отказом отвечать.
        self.assertIn('const asked = q.owner === "user"', text)
        self.assertIn("? { shown: human(q.text), full: null }", text)

    def test_the_button_does_not_repeat_the_state_line_of_its_own_column(self):
        # Замечание 3 приёмки продакта 2026-08-14: «живых прогонов нет, а ничто
        # не держит задач: 10» стояло в колонке дважды — строкой состояния и под
        # кнопкой. Число осталось в одном месте, а кнопка отвечает на свой
        # вопрос: что сделает нажатие.
        text = self.page()
        self.assertEqual(1, text.count('"живых прогонов нет, а ничто не держит задач: "'))
        self.assertIn('said.textContent = "нажатие попросит продакта посмотреть'
                      ' состояние сейчас";', text)

    def test_the_board_names_the_revision_it_runs_on(self):
        # Пункт 11 и находка 8: служба держит свой Python в памяти с запуска, а
        # шаблон читает с диска на каждый запрос, поэтому 14 августа полчаса
        # показывалась смесь двух версий.
        snapshot = a_snapshot()
        snapshot["revision"] = a_code_revision(running="aaaaaaa", disk="bbbbbbb")
        page = a_page(snapshot)
        self.assertIn('"Живая доска · установлена ревизия "', page)
        self.assertIn('" · доступно обновление до "', page)
        self.assertIn("перезапустите product-owner-board.service", page)

    def test_a_saved_page_says_it_is_a_snapshot_and_points_back_to_live(self):
        page = render.render({"snapshot": a_snapshot(), "timeline": [],
                              "board": render.build_board(a_snapshot()),
                              "built_at": "2026-08-15T12:00:00+00:00",
                              "live_url": "/data.json",
                              "live_page_url": "http://127.0.0.1:8791/",
                              "digest": "d"})
        self.assertIn('window.location.protocol !== "file:"', page)
        self.assertIn('"Сохранённый снимок от "', page)
        self.assertIn('"Открыть живую доску"', page)
        self.assertIn('"http://127.0.0.1:8791/"', page)

    def test_a_revision_named_without_its_observation_is_refused(self):
        broken = a_snapshot()
        broken["revision"] = a_code_revision(src="")
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(broken)

    def test_a_live_run_outranks_a_status_that_says_the_work_is_over(self):
        """Пункт 13. 14 августа в 10:41 CEST колонка «Клиент» показывала «в
        работе 0 — пусто», тогда как прогон круга 5 по 1151 был жив: во
        frontmatter оставался `completed` от прежнего преждевременного закрытия.
        """
        self.assertEqual(state.board_area("completed", ["live"], False), "running")
        self.assertEqual(state.board_area("cancelled", ["live"], False), "running")
        # И сам признак живого прогона наблюдается независимо от статуса: он
        # выставлялся только при нетерминальном статусе, то есть та же ошибка
        # стояла на шаг раньше и `board_area` до неё не доходил. Живая проверка
        # настоящим процессом — `evidence/live_run_over_status.py`.
        collector = (Path(__file__).parent / "process_map_state.py").read_text()
        self.assertNotIn('if run["alive"] and status not in TERMINAL:', collector)
        self.assertIn('    if run["alive"]:\n        flags.append("live")', collector)
        # И ровно это: терминальный статус без живого прогона решает, как решал.
        self.assertEqual(state.board_area("completed", [], False), "done")
        # А вопрос, заданный пользователю, по-прежнему выше живого прогона.
        self.assertEqual(state.board_area("in_progress", ["live"], True), "waiting_human")

    def test_the_divergence_is_named_on_the_plate(self):
        page = a_page()
        self.assertIn('"статус говорит «" + ru(STATUS_RU, p.status)', page)
        self.assertIn('"», а прогон идёт: запись устарела"', page)

    def test_our_own_test_tasks_are_not_in_the_list_a_person_searches(self):
        # Пункт 9. `TEST [1140] Живая проверка фоновых уведомлений` и две её
        # сестры стояли среди 1059 строк каталога.
        catalogue = [{"id": 1, "path": "/t/001-real", "title": "Настоящая работа",
                      "status": "planned"},
                     {"id": 2, "path": "/t/002-test", "title": "TEST [1140] Фоновые уведомления",
                      "status": "completed"}]
        rows = state.task_index(catalogue)
        self.assertEqual([row["id"] for row in rows], [1])

    def test_every_number_of_tasks_says_what_it_counts(self):
        # Пункт 9: счётчик колонки, сумма колонок и счётчик вкладки считают
        # разное, и на живой доске это были 228, 453 и 1055.
        page = a_page()
        self.assertIn('"задач этого направления в каталоге за всё время: "', page)
        self.assertIn('"всего задач в каталоге: " + all.length', page)


class ARefusedNotificationIsAnObservation(unittest.TestCase):
    """Task 1255: nine days of pushes that never reached the user.

    From 2026-08-13T20:50:48Z the transport could not import `aiogram`, and it
    wrote `notification_delivery_unresolved` 168 times across 41 tasks. Every
    one of those tasks finished cleanly, so nothing about their status said
    anything was wrong, and the reason lived only in each task's own `trace.md`.
    The board counted the receipts and the wake-up looked past them.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.task = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.pipeline = self.task / "dev-pipeline"
        self.pipeline.mkdir()

    def journal(self, *rows: dict) -> Path:
        (self.pipeline / "notification-receipts.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows))
        return self.task

    def refusal(self, at: datetime, **over) -> dict:
        row = {"event_id": "e-ref", "kind": "notification_delivery_unresolved",
               "notification_kind": "standard_run_started", "message_id": None,
               "reason": "notification transport raised before its outcome was "
                         "recorded: ModuleNotFoundError: No module named 'aiogram'",
               "recorded_at": at.isoformat(), "schema_version": "1.0"}
        row.update(over)
        return row

    def sent(self, at: datetime, **over) -> dict:
        row = {"event_id": "e-ok", "kind": "standard_run_completed",
               "message_id": 22004, "recorded_at": at.isoformat(),
               "schema_version": "1.0"}
        row.update(over)
        return row

    def test_a_refusal_is_named_with_its_cause_and_not_only_counted(self):
        now = datetime.now(timezone.utc)
        silent = state.delivery(self.journal(self.refusal(now)))["unresolved"]
        self.assertEqual(silent["notification"], "standard_run_started")
        self.assertIn("No module named 'aiogram'", silent["reason"])
        self.assertTrue(silent["current"])
        self.assertTrue(silent["src"].strip())

    def test_a_message_that_did_get_through_afterwards_closes_it(self):
        # The observation has to be able to end by itself, or the board keeps
        # ringing about a transport that was repaired days ago.
        now = datetime.now(timezone.utc)
        task = self.journal(self.refusal(now - timedelta(minutes=5)), self.sent(now))
        self.assertNotIn("unresolved", state.delivery(task))

    def test_a_later_refusal_reopens_it(self):
        now = datetime.now(timezone.utc)
        task = self.journal(self.refusal(now - timedelta(hours=2)),
                            self.sent(now - timedelta(hours=1)),
                            self.refusal(now, notification_kind="standard_run_completed"))
        self.assertEqual(state.delivery(task)["unresolved"]["notification"],
                         "standard_run_completed")

    def test_an_old_refusal_is_kept_but_no_longer_current(self):
        # Nine days on a closed task is history. It stays readable on the card
        # and stops competing with live work for the attention list.
        old = datetime.now(timezone.utc) - timedelta(
            seconds=state.DELIVERY_UNRESOLVED_SECONDS + 60)
        self.assertFalse(state.delivery(self.journal(self.refusal(old)))["unresolved"]["current"])

    def test_a_lifecycle_push_is_not_evidence_that_a_document_arrived(self):
        """The hole 1142 opened here by adding kinds this observer never learnt.

        `standard_run_started` carries a real message id, names no document and
        was outside `LIFECYCLE_RECEIPTS`, so `handoff` read it as an
        uncorrelatable delivery receipt and called the task delivered.
        """
        box = self.task / "deliverables"
        box.mkdir()
        (box / "report.html").write_text("x" * 4096)
        self.journal({"event_id": "e1", "kind": "standard_run_started",
                      "message_id": 22002, "recorded_at": "2026-08-22T23:21:13+00:00",
                      "schema_version": "1.0"})
        self.assertFalse(state.handoff(self.task)["delivered"])
        self.assertEqual(state.delivery_receipts(self.task), [])

    def test_every_kind_the_sender_writes_is_known_to_be_about_the_run(self):
        # Measured, not remembered: the last time this set was written down by
        # hand it went two years' worth of receipts out of date.
        for kind in ("standard_run_started", "standard_run_completed",
                     "pipeline_stopped", "notification_delivery_unresolved",
                     "review_rework_required", "review_waiting", "review_refused"):
            self.assertIn(kind, state.LIFECYCLE_RECEIPTS)
        for kind in ("document_delivery_started", "document_delivered",
                     "document_delivery_refused", "document_delivery_unresolved"):
            self.assertNotIn(kind, state.LIFECYCLE_RECEIPTS)

    def report(self, *tasks):
        observed = {"threads": [{"title": "Процессный контур", "products": [],
                                 "tasks": list(tasks), "repos": [], "task_count": len(tasks)}],
                    "owners_awake": []}
        with (mock.patch.object(thread, "load_thread", return_value={"repos": []}),
              mock.patch.object(thread.observer, "build", return_value=observed),
              mock.patch.object(thread, "process_inventory", return_value=[]),
              mock.patch.object(thread.observer, "write_owner_observations")):
            return thread.build("process")

    def a_silent_task(self, current: bool, status: str = "completed") -> dict:
        return a_task(id=1142, dir="1142-t", status=status, detail={"delivery": {
            "sent": 2, "refused": 1, "last_at": "2026-08-13T20:44:11+00:00",
            "last_kind": "standard_run_started",
            "src": "dev-pipeline/notification-receipts.jsonl",
            "unresolved": {"notification": "standard_run_started",
                           "at": "2026-08-13T20:50:48+00:00",
                           "reason": "ModuleNotFoundError: No module named 'aiogram'",
                           "current": current, "src": "квитанция"}}})

    def test_the_wake_up_is_told_about_a_finished_task_that_never_spoke(self):
        # 1142's own goal was «человек видит старт и конец обычного прогона»,
        # and it is `completed`: a rule that only looks at open work sees none
        # of the nine days.
        report = self.report(self.a_silent_task(current=True))
        self.assertEqual([task["id"] for task in report["needs_attention"]], [1142])
        self.assertIn("aiogram",
                      report["needs_attention"][0]["undelivered_notification"]["reason"])

    def test_an_old_refusal_does_not_stand_in_the_attention_list(self):
        report = self.report(self.a_silent_task(current=False))
        self.assertEqual(report["needs_attention"], [])

    def test_the_card_shows_what_never_reached_the_person(self):
        page = a_page()
        self.assertIn("d.delivery.unresolved", page)
        self.assertIn('"Не доставлено"', page)

    def test_the_card_counts_messages_and_not_receipts(self):
        now = datetime.now(timezone.utc)
        task = self.journal(self.sent(now - timedelta(minutes=10)),
                            self.refusal(now - timedelta(minutes=5)),
                            self.sent(now))
        observed = state.delivery(task)
        self.assertEqual(observed["sent"], 2)
        self.assertEqual(observed["refused"], 1)

    def test_a_refusal_is_not_counted_as_a_message_even_when_it_is_the_only_row(self):
        observed = state.delivery(self.journal(self.refusal(datetime.now(timezone.utc))))
        self.assertEqual(observed["sent"], 0)
        self.assertEqual(observed["refused"], 1)
        self.assertIsNone(observed["last_at"])

    def test_the_last_one_is_the_last_message_and_not_the_last_line(self):
        now = datetime.now(timezone.utc)
        went = now - timedelta(minutes=5)
        observed = state.delivery(self.journal(self.sent(went), self.refusal(now)))
        self.assertEqual(observed["last_at"], went.isoformat())
        self.assertEqual(observed["last_kind"], "standard_run_completed")

    def test_a_claim_written_before_the_transport_is_not_a_refusal(self):
        now = datetime.now(timezone.utc)
        observed = state.delivery(self.journal(
            {"event_id": "e-claim", "kind": "document_delivery_started",
             "message_id": None, "recorded_at": now.isoformat(),
             "schema_version": "1.0"},
            self.sent(now)))
        self.assertEqual(observed["sent"], 1)
        self.assertNotIn("refused", observed)

    def test_the_page_reads_the_message_count_and_not_the_receipt_count(self):
        page = a_page()
        self.assertIn("d.delivery.sent", page)
        self.assertIn('"Отказов"', page)
        self.assertNotIn("d.delivery.count", page)

    def test_outgoing_traffic_counts_messages_and_not_receipts(self):
        # «Одна квитанция на каждое отправленное сообщение» is what the channel
        # count promises; a refusal writes a receipt too, and 168 of them were
        # being reported as messages the contour had sent.
        repo = self.task / "repo"
        pipeline = repo / "tasks" / "001-t" / "dev-pipeline"
        pipeline.mkdir(parents=True)
        now = datetime.now(timezone.utc)
        (pipeline / "notification-receipts.jsonl").write_text(
            json.dumps(self.refusal(now)) + "\n" + json.dumps(self.sent(now)) + "\n")
        with mock.patch.object(state, "REPO", repo):
            channels = state.thread_channels([{"dir": "001-t"}], is_owner=False)
        self.assertEqual(channels, [{"channel": "telegram", "direction": "out", "count": 1}])


if __name__ == "__main__":
    unittest.main()
