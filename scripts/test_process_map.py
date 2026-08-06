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


def a_task(**over) -> dict:
    board = over.pop("board", {})
    run = over.pop("run", None)
    task = {"id": 1, "title": "Задача", "status": "planned", "status_detail": None,
            "dir": "001-task", "gates": [], "flags": [], "questions": [],
            "run": {"state": None, "runner": None, "workflow": None, "sandbox": None,
                    "stop_reason": None, "exit_code": None, "pid": None,
                    "alive": False, "alive_src": None, "progress": None},
            "board": {"area": "queued", "actor": None, "actor_src": None,
                      "role": None, "role_src": None, "happening": None,
                      "why": None, "why_src": None, "since": None, "since_src": None,
                      "age_seconds": None, "attempt": 0}}
    task.update(over)
    task["board"].update(board)
    if run:
        task["run"].update(run)
    return task


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
        "products": [{"slug": "task-agent", "questions": ["Публиковать ли перенос?"],
                      "effect": ["2026-08-06 — карта показывает ленту"]}],
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
        plate = board["panels"][0]["areas"][3]["plates"][0]
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
        self.assertEqual(order, ["waiting_human", "running", "stuck", "queued", "done"])

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
        first = render.build_board(a_snapshot(near))["panels"][0]["areas"][3]
        # The same tasks, collected again with the ages rounded differently.
        for task in near:
            task["board"]["age_seconds"] += 1
        second = render.build_board(a_snapshot(near))["panels"][0]["areas"][3]
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

    def test_nothing_happening_and_nothing_wrong_is_a_queue_not_work(self):
        self.assertEqual(state.board_area("planned", ["idle"], False), "queued")


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
                self.assertEqual(state.pending_questions(bullets), [])

    def test_a_task_blocked_without_a_question_is_not_waiting_for_a_person(self):
        # 760, 727 and 747 carry `- none` under the heading and reached the area
        # purely through their status.
        self.assertEqual(state.pending_questions([]), [])
        self.assertEqual(state.board_area("blocked", ["blocked"], False), "stuck")

    def test_a_real_open_question_still_gets_there(self):
        # The other half of the acceptance criterion: nothing genuinely waiting
        # on a person may disappear while the false positives go.
        self.assertEqual(state.pending_questions(self.STILL_WAITING), self.STILL_WAITING)
        self.assertEqual(state.board_area("planned", [], True), "waiting_human")

    def test_a_struck_through_question_is_settled(self):
        self.assertEqual(state.pending_questions(["~~Перезапускать ли 035?~~ Закрыт 2026-08-04."]), [])

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
        waiting = a_task(id=9, dir="009-t", questions=["Брать все девятнадцать?"],
                         board={"area": "waiting_human"})
        board = render.build_board(a_snapshot([waiting]))
        self.assertEqual(board["waiting"], board["waiting_tasks"] + board["waiting_questions"])
        self.assertEqual(board["waiting"], 2)

    def test_a_question_of_a_product_no_direction_owns_is_not_invented_onto_a_panel(self):
        snapshot = a_snapshot()
        snapshot["products"].append({"slug": "unowned", "questions": ["Ничей вопрос?"],
                                     "effect": []})
        board = render.build_board(snapshot)
        shown = [q["text"] for a in board["panels"][0]["areas"] for q in a["questions"]]
        self.assertNotIn("Ничей вопрос?", shown)


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
        self.assertIn('why.textContent = "почему: " + p.why', html)
        self.assertIn('w.textContent = " — " + why', html)


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


if __name__ == "__main__":
    unittest.main()
