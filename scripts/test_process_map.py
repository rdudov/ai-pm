#!/usr/bin/env python3
"""Regressions for the map's data contract.

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
import process_map_state as state
import runner_contract
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
                      ref="19fd2a19c92afcc5",
                      asked_src="пометка «спрошено у пользователя 2026-08-05, "
                                "письмо 19fd2a19c92afcc5» в самой строке",
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
                      "start_condition": None, "decision": None}}
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
    promise = {"text": "2026-08-04 — ревью кода companion силами Claude",
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
    owner = {"pid": 1, "kind": "tick", "thread": "moex", "worktrees": [],
             "since": "2026-08-06T20:05:00+00:00", "age_seconds": 60,
             "src": "командная строка и исполняемый файл процесса в /proc"}
    owner.update(over)
    return owner


def a_snapshot(tasks=None, products=None) -> dict:
    return {
        "schema_version": schema.SCHEMA_VERSION,
        "mode": "real",
        "threads": [{
            "key": "process", "title": "Процессный контур", "products": ["task-agent"],
            "task_count": len(tasks or [a_task()]), "tasks": tasks or [a_task()],
            "repos": [{"name": "companion-agent", "present": True, "branch": "main",
                       "head": "abc1234", "head_subject": "…", "head_at": "2026-08-06T09:00:00+00:00",
                       "tracked_dirty": 1, "unpushed": 2}],
            "channels": [{"channel": "telegram", "direction": "out", "count": 3},
                         {"channel": "email", "direction": "in", "count": 2}],
            "check": a_check(),
            "next_check": a_next_check(),
        }],
        "products": products or [{"slug": "task-agent",
                                  "questions": [asked_question()],
                                  "own_questions": [],
                                  "effect": ["2026-08-06 — карта показывает ленту"],
                                  "promises": [a_promise()]}],
        "owners_awake": [],
    }


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
SECRET = ("/opt/projects/moex-strategy-lab/report.md пишет elfiona.sea.girl@gmail.com "
          "в чат 433978200")


class Anonymisation(unittest.TestCase):
    """The negative control: what must not survive, and what must."""

    SECRET = SECRET

    def test_paths_mail_and_numeric_ids_do_not_survive(self):
        clean = schema.scrub({"detail": self.SECRET, "dir": "316-identify-max-user-433978200"})
        self.assertNotIn("/opt/projects", clean["detail"])
        self.assertNotIn("@gmail.com", clean["detail"])
        self.assertNotIn("433978200", clean["detail"])
        self.assertNotIn("433978200", clean["dir"])

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
            self.assertNotIn("433978200", clean[field], field)

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


class WorldLayout(unittest.TestCase):
    def test_a_task_that_moves_is_on_the_map_whatever_its_rank(self):
        boring = [a_task(id=n, dir=f"{n:03d}-task", flags=["idle"]) for n in range(1, 30)]
        moving = a_task(id=726, dir="726-process-map-top-down-view",
                        title="Карта процессной работы", flags=["live"])
        snapshot = a_snapshot(boring + [moving])
        timeline = [a_record(task="726-process-map-top-down-view")]
        world = render.build_world(snapshot, timeline)
        placed = {t["task"] for t in world["areas"][0]["tasks"]}
        self.assertIn("726-process-map-top-down-view", placed)
        self.assertLessEqual(len(placed), render.PER_AREA)

    def test_what_the_cap_dropped_is_said_out_loud(self):
        many = [a_task(id=n, dir=f"{n:03d}-t", flags=["idle"]) for n in range(1, 40)]
        world = render.build_world(a_snapshot(many), [])
        self.assertIn("скрыто", world["areas"][0]["note"])

    def test_both_panels_are_filled_from_the_products(self):
        world = render.build_world(a_snapshot(), [])
        self.assertEqual(world["waiting"][0]["text"], "Публиковать ли перенос?")
        self.assertEqual(world["done"][0]["src"], "task-agent")


class Rendering(unittest.TestCase):
    def test_the_page_carries_its_data_and_asks_for_nothing(self):
        data = {"snapshot": a_snapshot(), "timeline": [a_record()],
                "world": render.build_world(a_snapshot(), []), "live_url": None}
        html = render.render(data)
        self.assertIn("Карта процессной работы", html)
        self.assertNotIn("__DATA__", html)
        # No fetch, no XHR, no external origin: the recording opens off-line.
        for forbidden in ("http://", "https://", "XMLHttpRequest", "src=\"//"):
            self.assertNotIn(forbidden, html.replace('lang="ru"', ""))

    def test_a_closing_script_tag_in_the_data_cannot_end_the_tag(self):
        data = {"snapshot": a_snapshot([a_task(title="</script><b>сломать</b>")]),
                "timeline": [], "world": render.build_world(
                    a_snapshot([a_task(title="</script>")]), []), "live_url": None}
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
        clean = schema.scrub({"title": "Identify Max telegram_user_433978200",
                             "task_title": "Отчёт в /opt/projects/moex-strategy-lab на elfiona.sea.girl@gmail.com"})
        self.assertNotIn("433978200", clean["title"])
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
        secret = "telegram_user_433978200 /opt/projects/private owner@example.com"
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
        for forbidden in ("433978200", "/opt/projects", "owner@example.com"):
            self.assertNotIn(forbidden, written, forbidden)
        # The record is still a record: the observation survives the cleaning.
        for record in (json.loads(line) for line in written.splitlines()):
            schema.validate_record(record)
            self.assertTrue(record["task_title"])

    def test_showing_anonymously_cleans_a_timeline_written_without_the_flag(self):
        # `--serve --anonymize` used to trust that the file on disk had been
        # written safely, so a scribe started without the flag leaked through a
        # server started with it (finding HIGH-3 of review 786).
        secret = "telegram_user_433978200 в /opt/projects/private, owner@example.com"
        with tempfile.TemporaryDirectory() as tmp:
            timeline = Path(tmp) / "timeline.jsonl"
            timeline.write_text(json.dumps(a_record(task_title=secret, label=secret,
                                                    detail=secret), ensure_ascii=False))
            snapshot = Path(tmp) / "snapshot.json"
            snapshot.write_text(json.dumps(
                a_snapshot([a_task(title=secret)]), ensure_ascii=False))
            dirty = render.payload(timeline, snapshot)
            clean = render.payload(timeline, snapshot, anonymize=True)
        self.assertIn("433978200", json.dumps(dirty, ensure_ascii=False))
        blob = json.dumps(clean, ensure_ascii=False)
        for forbidden in ("433978200", "/opt/projects", "owner@example.com"):
            self.assertNotIn(forbidden, blob, forbidden)

    def test_cleaning_an_already_clean_document_changes_nothing(self):
        # Live mode cleans on the way to the screen even when the scribe already
        # cleaned on the way to disk, so the second pass has to be a no-op.
        once = schema.scrub(a_record(task_title=self.SECRET, detail=self.SECRET))
        self.assertEqual(schema.scrub(once), once)

    def test_a_built_anonymised_document_carries_no_pid_and_no_long_number(self):
        snapshot = a_snapshot([a_task(title="Задача telegram_user_433978200",
                                      run={"state": "running", "runner": "codex",
                                           "workflow": "dev-pipeline", "sandbox": None,
                                           "stop_reason": None, "exit_code": None,
                                           "pid": 2065651, "alive": True,
                                           "alive_src": "pid и стартовый тик ядра",
                                           "progress": None})])
        blob = json.dumps(schema.scrub(snapshot), ensure_ascii=False)
        self.assertNotIn("2065651", blob)
        self.assertNotIn("433978200", blob)


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
                                 "board": render.build_board(snapshot),
                                 "world": render.build_world(snapshot, list(timeline))})

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
                              "world": render.build_world(a_snapshot(), []),
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
                                 "pickup", "queued", "plan", "done"])

    def test_what_can_be_picked_up_stands_above_what_is_held(self):
        """The first question of a wake-up outranks the reference list below it.

        «В очереди» used to be one area and answered neither «что можно
        подхватить прямо сейчас» nor «за чем стоит остальное». Order is urgency
        here, so the startable work stands above the held work, and both above
        the promises nobody has made a task of yet.
        """
        order = list(schema.BOARD_AREAS)
        self.assertLess(order.index("pickup"), order.index("queued"))
        self.assertLess(order.index("queued"), order.index("plan"))
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
                             "репозиторий moex-strategy-lab занят живым прогоном задачи 783"),
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
            "письмо `19fd2a19c92afcc5`.", self.NO_MAILBOX)
        self.assertEqual(entry["owner"], "user")
        self.assertEqual(entry["asked_at"], "2026-08-05")
        self.assertEqual(entry["channel"], "email")
        self.assertEqual(entry["ref"], "19fd2a19c92afcc5")
        self.assertIn("19fd2a19c92afcc5", entry["asked_src"])

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
        mail = {"threads": {"19fd2a19c92afcc5": "продакт moex strategy lab"},
                "replies": {"продакт moex strategy lab":
                            state.datetime(2026, 8, 6, 8, 31, tzinfo=state.timezone.utc)},
                "sent_known": True}
        entry = state.question_entry(
            "Сколько ставок брать? Спрошено у пользователя 2026-08-05, письмо 19fd2a19c92afcc5.",
            mail)
        self.assertEqual(entry["owner"], "product")
        self.assertIn("в том же треде", entry["answer_src"])
        self.assertIn("незаписанным", entry["note"])

    def test_a_letter_older_than_the_question_is_not_an_answer_to_it(self):
        mail = {"threads": {"19fd2a19c92afcc5": "продакт moex strategy lab"},
                "replies": {"продакт moex strategy lab":
                            state.datetime(2026, 8, 1, 8, 31, tzinfo=state.timezone.utc)},
                "sent_known": True}
        entry = state.question_entry(
            "Сколько ставок брать? Спрошено у пользователя 2026-08-05, письмо 19fd2a19c92afcc5.",
            mail)
        self.assertEqual(entry["owner"], "user")

    def test_a_letter_earlier_the_same_day_is_not_an_answer_to_a_later_question(self):
        """The exact live case of finding HIGH-1 of review 826.

        The three questions went out in `19fd7e7ea2c3f7fb` at 2026-08-06
        16:28:48 UTC. The letter counted as their answer, `19fd75ff10dd7605`,
        was sent at 14:00:02 UTC — 2 hours 28 minutes *earlier*, and it asked
        about a forgotten document. A day is not fine enough to tell those
        apart; the send instant is.
        """
        mail = {"threads": {"19fd7e7ea2c3f7fb": "продакт moex strategy lab"},
                "replies": {"продакт moex strategy lab":
                            state.datetime(2026, 8, 6, 14, 0, 2, tzinfo=state.timezone.utc)},
                "sent_at": {"19fd7e7ea2c3f7fb":
                            state.datetime(2026, 8, 6, 16, 28, 48, tzinfo=state.timezone.utc)},
                "sent_known": True}
        entry = state.question_entry(
            "Сколько ставок брать? Спрошено у пользователя 2026-08-06, письмо 19fd7e7ea2c3f7fb.",
            mail)
        self.assertEqual(entry["owner"], "user")
        self.assertIsNone(entry["answer_src"])
        self.assertIn("не позже самого вопроса", entry["note"])
        self.assertIn("2026-08-06 16:28 UTC", entry["note"])

    def test_a_letter_later_the_same_day_is_the_answer(self):
        # The positive control of the same pair: same day, same thread, but
        # afterwards — that one really does take the question out of the area.
        mail = {"threads": {"19fd7e7ea2c3f7fb": "продакт moex strategy lab"},
                "replies": {"продакт moex strategy lab":
                            state.datetime(2026, 8, 6, 18, 5, tzinfo=state.timezone.utc)},
                "sent_at": {"19fd7e7ea2c3f7fb":
                            state.datetime(2026, 8, 6, 16, 28, 48, tzinfo=state.timezone.utc)},
                "sent_known": True}
        entry = state.question_entry(
            "Сколько ставок брать? Спрошено у пользователя 2026-08-06, письмо 19fd7e7ea2c3f7fb.",
            mail)
        self.assertEqual(entry["owner"], "product")
        self.assertIn("позже вопроса", entry["answer_src"])

    def test_a_letter_at_the_very_instant_of_the_question_is_not_an_answer(self):
        # Equal is not later. An answer written before it could be read is not
        # an answer, and the boundary is where a day-wide rule used to swallow it.
        instant = state.datetime(2026, 8, 6, 16, 28, 48, tzinfo=state.timezone.utc)
        mail = {"threads": {"19fd7e7ea2c3f7fb": "продакт moex strategy lab"},
                "replies": {"продакт moex strategy lab": instant},
                "sent_at": {"19fd7e7ea2c3f7fb": instant},
                "sent_known": True}
        entry = state.question_entry(
            "Сколько ставок брать? Спрошено у пользователя 2026-08-06, письмо 19fd7e7ea2c3f7fb.",
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
            self._store(root / "inbox" / "19fd75ff10dd7605", {
                "message_id": "19fd75ff10dd7605",
                "subject": "Продакт: MOEX Strategy Lab",
                "from": "rdudov@gmail.com", "to": "elfiona.sea.girl@gmail.com",
                "date": "Thu, 06 Aug 2026 17:00:02 +0300", "attachments": []})
            self._store(root / "sent" / "19fd7e7ea2c3f7fb", {
                "message_id": "19fd7e7ea2c3f7fb",
                "subject": "Re: Продакт: MOEX Strategy Lab",
                "from": "elfiona.sea.girl@gmail.com", "to": "rdudov@gmail.com",
                "date": "Thu, 06 Aug 2026 12:28:48 -0400", "attachments": []})
            original = (state.MAIL_INBOX, state.MAIL_SENT)
            state.MAIL_INBOX, state.MAIL_SENT = root / "inbox", root / "sent"
            try:
                mail = state.mailbox()
                entry = state.question_entry(
                    "Сколько ставок брать? Спрошено у пользователя 2026-08-06, "
                    "письмо 19fd7e7ea2c3f7fb.", mail)
            finally:
                state.MAIL_INBOX, state.MAIL_SENT = original

        # The reply prefix must not split the thread, or the two letters would
        # never be compared at all and the question would stay the user's for
        # the wrong reason.
        self.assertEqual(mail["threads"]["19fd7e7ea2c3f7fb"],
                         mail["threads"]["19fd75ff10dd7605"])
        self.assertEqual(mail["sent_at"]["19fd7e7ea2c3f7fb"],
                         state.datetime(2026, 8, 6, 16, 28, 48, tzinfo=state.timezone.utc))
        self.assertEqual(entry["owner"], "user")
        self.assertIsNone(entry["answer_src"])

    def test_without_a_stored_outgoing_letter_the_marked_day_still_decides(self):
        # The fallback is unchanged behaviour, not a new rule: with no letter on
        # disk the mark carries a date and nothing finer, and the board says so.
        mail = {"threads": {"19fd2a19c92afcc5": "продакт moex strategy lab"},
                "replies": {"продакт moex strategy lab":
                            state.datetime(2026, 8, 6, 8, 31, tzinfo=state.timezone.utc)},
                "sent_at": {},
                "sent_known": True}
        entry = state.question_entry(
            "Сколько ставок брать? Спрошено у пользователя 2026-08-05, письмо 19fd2a19c92afcc5.",
            mail)
        self.assertEqual(entry["owner"], "product")
        self.assertIn("не раньше вопроса", entry["answer_src"])

    def test_an_unresolvable_thread_says_so_instead_of_claiming_silence(self):
        entry = state.question_entry(
            "Спрошено у пользователя 2026-08-05, письмо 19fd2a19c92afcc5. Так что решаем?",
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
            self._store(root / "sent" / "19fd7e7ea2c3f7fb", {
                "message_id": "19fd7e7ea2c3f7fb",
                "subject": "Продакт: MOEX Strategy Lab",
                "from": "elfiona.sea.girl@gmail.com", "to": "rdudov@gmail.com",
                "date": "Thu, 06 Aug 2026 09:12:00 +0300", "attachments": []})
            self._store(root / "inbox" / "19fd7f2481303262", {
                "message_id": "19fd7f2481303262",
                "subject": "Re: Продакт: MOEX Strategy Lab",
                "from": "rdudov@gmail.com", "to": "elfiona.sea.girl@gmail.com",
                "date": "Thu, 06 Aug 2026 10:40:00 +0300", "attachments": []})
            original = (state.MAIL_INBOX, state.MAIL_SENT)
            state.MAIL_INBOX, state.MAIL_SENT = root / "inbox", root / "sent"
            try:
                mail = state.mailbox()
                entry = state.question_entry(
                    "Сколько ставок брать? Спрошено у пользователя 2026-08-06, "
                    "письмо 19fd7e7ea2c3f7fb.", mail)
            finally:
                state.MAIL_INBOX, state.MAIL_SENT = original

        self.assertTrue(mail["sent_known"])
        self.assertEqual(mail["threads"]["19fd7e7ea2c3f7fb"],
                         state.thread_key("Продакт: MOEX Strategy Lab"))
        self.assertEqual(entry["owner"], "product")
        self.assertIn("в том же треде", entry["answer_src"])

    @staticmethod
    def _store(directory: Path, metadata: dict) -> None:
        directory.mkdir(parents=True)
        (directory / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def test_a_thread_is_one_thread_whatever_prefix_a_client_added(self):
        self.assertEqual(state.thread_key("Re: Продакт: MOEX Strategy Lab"),
                         state.thread_key("Продакт: MOEX Strategy Lab"))
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

    def test_a_failed_gate_is_a_reason(self):
        why, src = state.jam_reason(None, self.NO_RUN,
                                    [{"gate": "live_surface", "result": "FAIL"},
                                     {"gate": "tests", "result": "OK"}], [])
        self.assertIn("live_surface", why)
        self.assertNotIn("tests", why)
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
                              "world": render.build_world(a_snapshot(), []),
                              "built_at": "2026-08-06T12:00:00+00:00",
                              "live_url": None, "digest": "d"})
        # The reason is text on the plate and text on the strip. It runs through
        # `human()` on the way — `decision=deliver` is a reason a person has to
        # read too — and `human()` returns unknown text unchanged.
        self.assertIn('p.why === p.blocked_by ? "стоит за: " : "почему: ")', html)
        self.assertIn("+ human(p.why)", html)
        self.assertIn('w.textContent = " — " + human(why)', html)

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
                              "world": render.build_world(a_snapshot(), []),
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
    """What this repository borrows from companion-agent's runner still exists.

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

    def fixture(self, tmp: str, consumer: str, runner: str) -> tuple[Path, Path]:
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
        return scripts, runner_scripts

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

    def test_the_scan_sees_the_borrowings_it_is_supposed_to_guard(self):
        # A scan that quietly matches nothing would pass every other test here.
        borrowed = runner_contract.borrowed_names()
        self.assertIn("process_map_state.py", borrowed)
        self.assertLessEqual({"process_is_live", "runner_pid_namespace_state"},
                             borrowed["process_map_state.py"])

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
            violations = runner_contract.check(Path(tmp) / "nowhere", Path(tmp))
        self.assertEqual([item["kind"] for item in violations], ["module"])

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

    def test_a_divergence_leaves_through_the_channels_a_verdict_leaves_through(self):
        violation = [{"kind": "name", "text": "разошлось", "src": "источник"}]
        moment = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
        with mock.patch.object(tick.runner_contract, "check", return_value=violation), \
                mock.patch.object(tick, "notify") as notified, \
                mock.patch.object(tick, "send_mail") as mailed:
            found, reminder = tick.runner_contract_alarm("process", {}, moment, announce=True)
        self.assertEqual(found, violation)
        self.assertEqual(notified.call_count, 1)
        self.assertEqual(mailed.call_count, 1)
        self.assertIn("разошлось", notified.call_args[0][0])
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
                mock.patch.object(tick, "notify") as notified, \
                mock.patch.object(tick, "send_mail") as mailed:
            tick.runner_contract_alarm("process", stored, moment, announce=True)
        self.assertEqual(notified.call_count, 0)
        self.assertEqual(mailed.call_count, 0)

    def test_a_new_divergence_is_news_at_once(self):
        violation = [{"kind": "name", "text": "другое расхождение", "src": "источник"}]
        moment = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
        stored = {"runner_contract_reminder": {
            "at": (moment - timedelta(seconds=60)).isoformat(),
            "signature": json.dumps(["разошлось"])}}
        with mock.patch.object(tick.runner_contract, "check", return_value=violation), \
                mock.patch.object(tick, "notify") as notified, \
                mock.patch.object(tick, "send_mail"):
            tick.runner_contract_alarm("process", stored, moment, announce=True)
        self.assertEqual(notified.call_count, 1)

    def test_a_healthy_contract_says_nothing_and_forgets_the_old_alarm(self):
        moment = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
        stored = {"runner_contract_reminder": {"at": moment.isoformat(), "signature": "x"}}
        with mock.patch.object(tick.runner_contract, "check", return_value=[]), \
                mock.patch.object(tick, "notify") as notified, \
                mock.patch.object(tick, "send_mail") as mailed:
            found, reminder = tick.runner_contract_alarm("process", stored, moment, announce=True)
        self.assertEqual((found, reminder), ([], None))
        self.assertEqual((notified.call_count, mailed.call_count), (0, 0))

    def test_a_divergence_makes_the_unit_fail_even_if_the_tick_still_observed(self):
        # An exit code is the one signal that survives a wake-up nobody reads.
        # Before this, the four units failed on a traceback and the observation
        # simply stopped; a tick that manages to observe with a broken contract
        # must still not be recorded as a success.
        source = Path(tick.__file__).read_text()
        self.assertIn("verdict = 1 if contract else 0", source)
        self.assertNotIn("\n    return 0\n", source[source.index("    verdict = 1"):])

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
                              "world": render.build_world(a_snapshot(), []),
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
        # `fetch` in the page sits behind that null.
        data = {"snapshot": a_snapshot(), "timeline": [a_record()],
                "board": render.build_board(a_snapshot()),
                "world": render.build_world(a_snapshot(), []),
                "built_at": "2026-08-06T12:00:00+00:00", "live_url": None, "digest": "d"}
        html = render.render(data)
        self.assertIn('"live_url":null', html.replace(" ", ""))
        for forbidden in ("http://", "https://", "XMLHttpRequest", 'src="//', "WebSocket",
                          "EventSource", "importScripts", "navigator.sendBeacon"):
            self.assertNotIn(forbidden, html.replace('lang="ru"', ""))
        # Every `fetch` in the page is inside the branch guarded by `live_url`.
        body = html[html.index("if (DATA.live_url)"):]
        self.assertEqual(html.count("fetch("), body.count("fetch("))

    def test_the_board_is_the_screen_that_opens(self):
        data = {"snapshot": a_snapshot(), "timeline": [],
                "board": render.build_board(a_snapshot()),
                "world": render.build_world(a_snapshot(), []),
                "built_at": "2026-08-06T12:00:00+00:00", "live_url": None, "digest": "d"}
        html = render.render(data)
        self.assertIn("Доска процессной работы", html)
        # The timeline from the previous version stays, one button away.
        self.assertIn("Карта во времени", html)
        self.assertIn('sessionStorage.getItem("screen") !== "map"', html)


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
                          "world": render.build_world(snapshot, []),
                          "built_at": "2026-08-06T12:00:00+00:00",
                          "live_url": None, "digest": "d"})


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
                    "run": {"repo": "/opt/projects/moex-strategy-lab"}}]
        busy = state.busy_repository_map(entries)
        why, src = queue_why({"id": 722}, "/opt/projects/moex-strategy-lab", busy)
        self.assertIn("moex-strategy-lab", why)
        self.assertIn("783", why)
        self.assertTrue(src.strip())

    def test_a_task_is_not_held_by_its_own_live_run(self):
        entries = [{"id": 783, "title": "Живая", "flags": ["live"],
                    "run": {"repo": "/opt/projects/moex-strategy-lab"}}]
        busy = state.busy_repository_map(entries)
        why, _ = queue_why({"id": 783}, "/opt/projects/moex-strategy-lab", busy)
        self.assertIsNone(why)

    def test_a_dead_run_does_not_hold_a_repository(self):
        entries = [{"id": 783, "title": "Мёртвая", "flags": ["idle"],
                    "run": {"repo": "/opt/projects/moex-strategy-lab"}}]
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

    def test_the_strip_answers_it_on_the_first_screen(self):
        board = render.build_board(a_snapshot([a_task(board={"area": "pickup"})]))
        self.assertEqual([p["id"] for p in board["pickup"]], [1])
        self.assertIn("Что подхватить", a_page())

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
# The scenario is not invented: 830 held /opt/projects/moex-trading-engine, 831
# was the next step in the same tree, and the sentence saying so was invisible.
LIVE_831 = "starts_after=830 worktree=/opt/projects/moex-trading-engine"


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
        self.assertEqual(condition["worktrees"], ["/opt/projects/moex-trading-engine"])
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
        busy = {"/opt/projects/moex-trading-engine": {"id": 775, "title": "Живая"}}
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
                      run={"repo": "/opt/projects/moex-trading-engine", "alive": True,
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
    {"id": 736, "title": "надо исправить task_index — она присылает задачи companion-agent",
     "slug": "736-max-task-index"},
    {"id": 713, "title": "Ревью кода companion силами Claude: старый код и кандидаты на рефакторинг",
     "slug": "713-companion"},
    {"id": 394, "title": "Move `/task` workflow ownership into companion",
     "slug": "394-task-workflow-ownership"},
    {"id": 811, "title": "Закрыть доступ: /codex только в Calypso и только у владельца",
     "slug": "811-calypso-codex-owner-only"},
    {"id": 266, "title": "Remove TTS cleanup attempt prefix", "slug": "266-tts-prefix"},
]


def unplanned(*items) -> list[dict]:
    """`unplanned` against a fixed catalogue, so the fixture is the whole input."""
    return state.unplanned(list(items), CATALOGUE)


class WhatNeedsPlanning(unittest.TestCase):
    """The fourth question, and the two days it already cost.

    «ревью кода companion силами Claude… Запрошено пользователем» stood in the
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
        line = ("2026-08-04 — ревью кода companion силами Claude: код старый, вдумчиво "
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
                "в Calypso и только у владельца, доступ второго закрываем")
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
        snapshot = a_snapshot(products=[{"slug": "moex-strategy-lab", "questions": [],
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
                                    "deep-research"]), "deep-research")
        self.assertEqual(thread_of(["python3", "/opt/x/scripts/thread_tick.py",
                                    "--force", "moex"]), "moex")
        self.assertIsNone(thread_of(["/bin/bash", "-c",
                                     "cd /opt && python3 scripts/thread_tick.py companion"]))

    def test_this_very_wake_up_mechanism_is_the_one_observed(self):
        # Whatever else changes, the thing being looked for is the tick script,
        # because that is what the timer starts.
        self.assertIn("thread_tick.py", Path(state.__file__).read_text())

    def test_the_strip_warns_before_a_task_is_created_not_after(self):
        snapshot = a_snapshot()
        snapshot["owners_awake"] = [an_owner(worktrees=["/opt/projects/moex-strategy-lab"])]
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
            ["claude", "--name", "product-owner", "--add-dir", "/opt/projects"],
            state.HOME), "session")
        self.assertEqual(state.session_owner(
            ["node", "/usr/local/bin/codex", "exec"], state.HOME), "session")
        # The owner agent a tick started runs the same CLI from the same
        # directory. It is a second owner, but calling it «продакт в консоли»
        # would name the wrong thing, and this board is built on not doing that.
        self.assertEqual(state.session_owner(
            ["/usr/local/bin/claude", "--print", "--add-dir", "/opt/projects"],
            state.HOME), "woken")
        # A child run of a task sits in its task's repository. It is a run, is
        # already on the board as one, and is not a second owner.
        self.assertIsNone(state.session_owner(
            ["claude", "--print"], Path("/opt/projects/companion-agent")))
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
                                             worktrees=["/opt/projects/product-owner"])]
        page = a_page(snapshot)
        self.assertIn("продакт в консоли", page)
        self.assertIn("может занять", page)


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
            answer = state.next_check("companion")
        self.assertIsNone(answer["at"])
        self.assertIn("не взведён", answer["src"])
        self.assertFalse(self.calendar_asked(asked))

        # And the same when systemd says nothing at all about either unit: still
        # unknown, still with a reason, still without a computed minute.
        asked.clear()
        with mock.patch.object(state, "systemd", side_effect=lambda c: asked.append(c)):
            answer = state.next_check("moex")
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
        page = a_page()
        node = page[page.index("function checkNode"):page.index("function drawBoard")]
        self.assertIn("for (const reason of check.reasons)", node)
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
        """
        page = a_page()
        node = page[page.index("function checkNode"):page.index("function drawBoard")]
        self.assertIn("Array.isArray(check.started) && !check.started.length", node)

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
                missing = state.thread_check("moex")
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
        self.assertIn("openCard(again, true)", redraw)
        opener = page[page.index("function openCard"):page.index("function closeCard")]
        self.assertIn("keepScroll ? wasAt : 0", opener)


class DrillDown(unittest.TestCase):
    """Opening a plate without leaving the page, and without a second door in."""

    def test_the_card_travels_with_the_plate_and_fetches_nothing(self):
        # The page has no way to reach a disk and must not grow one: a card that
        # fetched its own detail would be a second door past the boundary the
        # whole split exists to hold.
        page = a_page()
        body = page[page.index("if (DATA.live_url)"):]
        self.assertEqual(page.count("fetch("), body.count("fetch("))
        self.assertIn("function openCard", page)
        self.assertNotIn("fetch(", page[page.index("function openCard"):page.index("function closeCard")])

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
        poll = page[page.index("if (DATA.live_url)"):]
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
        self.assertIn("def build(anonymize: bool, only: str | None = None)", source)
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
        (box / "moex-portfolio-history-sources-2026-08-06.html").write_text("x" * 441097)
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
        self.assertEqual(hand["name"], "deliverables/moex-portfolio-history-sources-2026-08-06.html")
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
            "# Доставка пользователю\n- 2026-08-06, письмо `19fd8d2ef212b626`\n")
        hand = state.handoff(task)
        self.assertTrue(hand["delivered"])
        self.assertIn("product-owner-delivery.md", hand["delivered_src"])

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
        document = task / "deliverables" / "moex-portfolio-history-sources-2026-08-06.html"
        observed = {(document.name, document.stat().st_size): [
            {"channel": "telegram", "message_id": 18491}
        ]}
        hand = state.handoff(task, observed)
        self.assertTrue(hand["delivered"])
        self.assertIn("telegram", hand["delivered_src"])

    def test_every_user_document_must_be_observed_before_the_task_closes(self):
        task = self.like_783()
        first = task / "deliverables" / "moex-portfolio-history-sources-2026-08-06.html"
        (task / "deliverables" / "second-report.md").write_text("second")
        observed = {(first.name, first.stat().st_size): [
            {"channel": "email", "message_id": "gmail-1"}
        ]}
        hand = state.handoff(task, observed)
        self.assertFalse(hand["delivered"])
        self.assertIn("1 из 2", hand["delivered_src"])

    def test_digest_decides_when_the_observation_has_one(self):
        task = self.like_783()
        document = task / "deliverables" / "moex-portfolio-history-sources-2026-08-06.html"
        key = (document.name, document.stat().st_size)
        wrong = {key: [{"channel": "email", "message_id": "wrong",
                        "sha256": "0" * 64, "at": None}]}
        self.assertFalse(state.handoff(task, wrong)["delivered"])
        right = {key: [{"channel": "email", "message_id": "right",
                        "sha256": state._file_sha256(document), "at": None}]}
        self.assertTrue(state.handoff(task, right)["delivered"])

    def test_an_older_same_name_revision_is_named_but_does_not_close(self):
        task = self.like_783()
        document = task / "deliverables" / "moex-portfolio-history-sources-2026-08-06.html"
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
                "dialog": "Calypso", "from_me": False,
            }) + "\n")
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
        task = a_task(id=783, dir="783-moex", status="completed",
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
        # The dark palette survives only as the chrome of the isometric map, and
        # only under the class the screen switch sets.
        self.assertIn("body.mapmode {", text)
        self.assertIn('classList.toggle("mapmode", !on)', text)

    def test_the_page_scrolls_instead_of_squeezing_everything_into_the_window(self):
        text = self.page()
        self.assertNotIn("html, body { margin: 0; height: 100%; overflow: hidden; }", text)
        self.assertIn("body.mapmode { height: 100%; overflow: hidden; }", text)
        # The board is in the flow of the document, not pinned to the viewport.
        self.assertNotIn("#board { position: fixed", text)

    def test_no_plate_area_or_column_scrolls_inside_itself(self):
        css = self.board_css()
        for gone in ("overflow-y: auto", "overflow: auto", "max-height: 24vh", "max-height: 17vh"):
            self.assertNotIn(gone, css, f"вложенный скролл вернулся: {gone}")

    def test_no_text_on_a_plate_is_clamped_or_elided(self):
        # Every `-webkit-line-clamp` in this file existed to protect a fixed
        # screen height that no longer exists, and each of them cut a sentence
        # the board had already promised to answer.
        self.assertNotIn("-webkit-line-clamp", self.page())

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
               waiting=(), worktrees=("/opt/projects/companion-agent",)):
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
        }

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

    def test_yielding_to_another_owner_leaves_the_list_on_disk(self):
        """Yielding is right; yielding silently is how the list reaches nobody."""
        report = self.report(ready=[831])
        report["owners_awake"] = [an_owner(worktrees=["/opt/projects/companion-agent"])]
        left = tick.yielded(report)
        self.assertEqual([item["id"] for item in left["ready_to_start"]], [831])
        self.assertEqual(left["to"][0]["pid"], 1)
        self.assertEqual(left["to"][0]["shared_worktrees"], ["/opt/projects/companion-agent"])
        self.assertTrue(left["src"].strip())

    def test_the_tick_does_not_count_itself_as_the_other_owner(self):
        # The second owner of 2026-08-06 was awake on the *same* direction, so
        # the exclusion has to be this process and not this thread.
        report = self.report(ready=[831])
        report["owners_awake"] = [an_owner(pid=os.getpid(),
                                           worktrees=["/opt/projects/companion-agent"])]
        self.assertIsNone(tick.yielded(report))

    def test_an_owner_that_cannot_take_the_same_tree_is_not_yielded_to(self):
        """The whole of the 2026-08-07 mutism, in one assertion.

        Four timers fired at 16:06:56 and again at 16:47:00, and yielding meant
        «any other tick». `companion` stood down for `process`, `deep-research`
        for `companion` and `process`, `moex` for all three, and `process` for
        nobody — so on every synchronous wake-up exactly one direction could act
        and it was always the same one. The four own four disjoint sets of
        repositories: not one of those collisions could have happened.
        """
        report = self.report(ready=[831], worktrees=["/opt/projects/companion-agent"])
        report["owners_awake"] = [an_owner(thread="moex",
                                           worktrees=["/opt/projects/moex-strategy-lab"])]
        self.assertIsNone(tick.yielded(report))

    def test_the_console_owner_is_yielded_to_when_it_stands_in_the_same_tree(self):
        # And only then. An owner is a person or a process that could put a
        # second child into one working tree, never merely another window.
        report = self.report(ready=[831], worktrees=["/opt/projects/companion-agent"])
        report["owners_awake"] = [an_owner(kind="session", thread=None,
                                           worktrees=["/opt/projects/companion-agent"])]
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
                                                [], [], tick.outbound.no_chat()))

    def test_a_woken_owner_that_says_nothing_does_not_end_the_tick_in_silence(self):
        """«Ни письма не было с вопросами/проблемами, ни информации на доске.»

        The verdict channel is the only one the user reads, and `SILENT` is a
        legitimate answer for a wake-up with nothing to report — but not for one
        woken *because* the direction is standing still. Then the observed
        reasons go out on the same two channels the verdict does.

        Since 861 the mail half of that goes through `outbound.decide` like every
        other letter, so what is asserted here is that the branch still speaks on
        both channels — the push unconditionally, the letter through the gate as
        its own kind. How often such a letter may repeat belongs to
        `test_outbound.StandingIdle`, not here.
        """
        source = Path(tick.__file__).read_text()
        branch = source[source.index('elif idle and not (after or {}).get("live")'):]
        self.assertIn("notify(told)", branch)
        self.assertIn('deliver(', branch)
        self.assertIn('"idle"', branch)
        self.assertIn("for item in reasons", branch)

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

    def test_the_map_tooltip_is_bound_to_the_map_screen(self):
        page = a_page()
        # One writer of the fact, asked by the pointer handler and by the frame
        # loop. Guarding by canvas geometry would not have worked: a hidden
        # canvas keeps its last rectangle, and an event dispatched anywhere
        # bubbles to the window all the same.
        self.assertIn("let mapOpen = false;", page)
        self.assertIn("mapOpen = !on;", page)
        self.assertIn("if (mapOpen && hover) tipFor(hover);", page)
        self.assertIn("if (!mapOpen) { if (hover) hideTip(); return; }", page)
        # Leaving the map puts both facts away together: a stale `hover` with a
        # hidden box comes back the moment a frame is drawn.
        self.assertIn("function hideTip()", page)
        self.assertIn("if (on) hideTip();", page)

    def test_the_tooltip_states_its_own_colours_and_inherits_none(self):
        page = a_page()
        tip = page[page.index("#tip { position: fixed"):page.index("#tip .st") + 80]
        self.assertNotIn("var(--edge)", tip)
        self.assertNotIn("var(--dim)", tip)
        self.assertNotIn("rgba(9,14,24,.96)", tip)
        self.assertIn("background: #0e1524", tip)
        self.assertIn("color: #f2f6fd", tip)
        self.assertIn("color: #b9c6dd", tip)

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
        task = a_task(detail={"summary": "смотри /opt/projects/companion-agent/tasks/864"})
        cleaned = schema.scrub(a_snapshot([task]))
        self.assertNotIn("/opt/projects", cleaned["threads"][0]["tasks"][0]["detail"]["summary"])

    def test_the_description_travels_to_the_plate_the_card_is_built_from(self):
        detail = render.plate(a_task(detail={"summary": "О чём задача"}))["detail"]
        self.assertEqual(detail["summary"], "О чём задача")


if __name__ == "__main__":
    unittest.main()
