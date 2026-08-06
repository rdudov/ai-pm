#!/usr/bin/env python3
"""Regressions for the map's data contract.

The renderer's only guarantee — that it cannot show what it never saw — rests on
the shape of two documents. These tests hold that shape, the anonymisation that
goes with it and the layout decisions the picture depends on.

    python3 -m unittest discover -s scripts -p 'test_*.py'
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import process_map_recorder as recorder
import process_map_render as render
import process_map_schema as schema
import process_map_state as state


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
            "detail": {"review": None, "delivery": None, "files": [],
                       "moved": None, "moved_age_seconds": None, "moved_src": None,
                       "handoff": None},
            "board": {"area": "queued", "actor": None, "actor_src": None,
                      "role": None, "role_src": None, "happening": None,
                      "why": None, "why_src": None, "since": None, "since_src": None,
                      "age_seconds": None, "attempt": 0,
                      "blocked_by": None, "blocked_by_src": None}}
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
        self.assertEqual(order, ["waiting_human", "running", "stuck", "undelivered",
                                 "product_owner", "pickup", "queued", "plan", "done"])

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
        self.assertIn('p.why === p.blocked_by ? "стоит за: " : "почему: ") + p.why', html)
        self.assertIn('w.textContent = " — " + why', html)

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

    def test_a_pid_of_another_namespace_is_unobservable_not_dead(self):
        alive, src = state.run_alive({"pid": 1, "process_identity": "x",
                                      "pid_namespace": "pid:[4026500000]"})
        self.assertFalse(alive)
        self.assertIn("ненаблюдаема", src)

    def test_a_live_run_without_a_stated_observation_is_refused(self):
        with self.assertRaises(schema.ContractError):
            schema.validate_snapshot(a_snapshot([a_task(run={"alive": True})]))
        schema.validate_snapshot(a_snapshot([a_task(
            run={"alive": True, "alive_src": "pid и стартовый тик ядра"})]))


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
        broken["owners_awake"] = [{"thread": "moex", "since": "2026-08-06T12:00:00+00:00",
                                   "age_seconds": 60, "src": "  "}]
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
        snapshot["owners_awake"] = [{"thread": "moex", "since": "2026-08-06T12:00:00+00:00",
                                     "age_seconds": 60, "src": "командная строка процесса в /proc"}]
        board = render.build_board(snapshot)
        self.assertEqual(len(board["owners_awake"]), 1)
        self.assertIn("сверьтесь с его очередью, прежде чем заводить задачу", a_page(snapshot))


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
        page = a_page()
        for asked in ("Вердикт ревью", "Гейты verification.md",
                      "Последняя запись прогресса", "Прогон",
                      "Доставлено человеку", "Движение артефактов",
                      "Файлы каталога задачи"):
            self.assertIn(asked, page, asked)

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

    def test_a_report_written_straight_into_the_task_directory_counts(self):
        (self.task / "report.html").write_text("<html></html>")
        self.assertEqual(state.handoff(self.task)["name"], "report.html")

    def test_the_manifest_alone_is_not_a_document_for_a_person(self):
        box = self.task / "deliverables"
        box.mkdir()
        (box / "manifest.json").write_text("[]")
        self.assertIsNone(state.handoff(self.task))

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


if __name__ == "__main__":
    unittest.main()
