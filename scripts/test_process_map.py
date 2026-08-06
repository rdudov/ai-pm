#!/usr/bin/env python3
"""Regressions for the map's data contract.

The renderer's only guarantee — that it cannot show what it never saw — rests on
the shape of two documents. These tests hold that shape, the anonymisation that
goes with it and the layout decisions the picture depends on.

    python3 -m unittest discover -s scripts -p 'test_*.py'
"""
from __future__ import annotations

import json
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
    task = {"id": 1, "title": "Задача", "status": "planned", "status_detail": None,
            "dir": "001-task", "gates": [], "flags": [], "questions": [],
            "run": {"state": None, "runner": None, "workflow": None, "sandbox": None,
                    "stop_reason": None, "exit_code": None, "pid": None,
                    "alive": False, "progress": None},
            "board": {"area": "queued", "actor": None, "actor_src": None,
                      "role": None, "role_src": None, "happening": None,
                      "since": None, "age_seconds": None, "attempt": 0}}
    task.update(over)
    task["board"].update(board)
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


class Anonymisation(unittest.TestCase):
    """The negative control: what must not survive, and what must."""

    SECRET = ("/opt/projects/moex-strategy-lab/report.md пишет elfiona.sea.girl@gmail.com "
              "в чат 433978200")

    def test_paths_mail_and_numeric_ids_do_not_survive(self):
        clean = state.scrub({"detail": self.SECRET, "dir": "316-identify-max-user-433978200"})
        self.assertNotIn("/opt/projects", clean["detail"])
        self.assertNotIn("@gmail.com", clean["detail"])
        self.assertNotIn("433978200", clean["detail"])
        self.assertNotIn("433978200", clean["dir"])

    def test_real_task_titles_survive_on_purpose(self):
        # The user asked to recognise a specific task by its real name among all
        # the shown work. Content privacy of titles stays a human step.
        clean = state.scrub({"title": "Карта процессной работы: вид сверху",
                             "task_title": "Пересчитать 18 строк калькулятора"})
        self.assertEqual(clean["title"], "Карта процессной работы: вид сверху")
        self.assertEqual(clean["task_title"], "Пересчитать 18 строк калькулятора")

    def test_recorder_scrubs_a_record_but_keeps_the_title(self):
        clean = recorder.scrub_record(a_record(
            kind="artifact", task_title="Карта процессной работы",
            detail=self.SECRET, label="analysis.md"))
        self.assertEqual(clean["task_title"], "Карта процессной работы")
        self.assertNotIn("433978200", clean["detail"])


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

    def test_a_secret_inside_a_kept_title_does_not_survive(self):
        # The old scrub excluded titles from cleaning altogether, so a real chat
        # identifier went out inside a real task name, in a file stamped
        # «ОБЕЗЛИЧЕНО». The title keeps its meaning; its content gets cleaned.
        clean = state.scrub({"title": "Identify Max telegram_user_433978200",
                             "task_title": "Отчёт в /opt/projects/moex-strategy-lab на elfiona.sea.girl@gmail.com"})
        self.assertNotIn("433978200", clean["title"])
        self.assertIn("Identify Max", clean["title"])
        self.assertNotIn("/opt/projects", clean["task_title"])
        self.assertNotIn("@gmail.com", clean["task_title"])
        self.assertIn("Отчёт", clean["task_title"])

    def test_an_integer_identifier_does_not_survive(self):
        # `scrub` used to return every int untouched, which is how 298 PIDs
        # reached a public artifact: a regex never sees a number that is not text.
        clean = state.scrub({"run": {"pid": 2065651, "alive": True}, "task_count": 73})
        self.assertIsNone(clean["run"]["pid"])
        self.assertTrue(clean["run"]["alive"])
        # A count is a measurement, not an identifier, and has to stay.
        self.assertEqual(clean["task_count"], 73)

    def test_a_built_anonymised_document_carries_no_pid_and_no_long_number(self):
        snapshot = a_snapshot([a_task(title="Задача telegram_user_433978200",
                                      run={"state": "running", "runner": "codex",
                                           "workflow": "dev-pipeline", "sandbox": None,
                                           "stop_reason": None, "exit_code": None,
                                           "pid": 2065651, "alive": True, "progress": None})])
        blob = json.dumps(state.scrub(snapshot), ensure_ascii=False)
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
                            "pid": None, "alive": True, "progress": None})
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
        board = render.build_board(a_snapshot([a_task(board={"area": "done"})]))
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
    def test_a_person_deciding_comes_before_everything_else(self):
        self.assertEqual(state.board_area("planned", ["live"], True), "waiting_human")
        self.assertEqual(state.board_area("blocked", [], False), "waiting_human")

    def test_a_dead_run_under_a_living_label_is_a_jam(self):
        self.assertEqual(state.board_area("planned", ["stale_label"], False), "stuck")
        self.assertEqual(state.board_area("planned", ["killed"], False), "stuck")

    def test_a_finished_task_is_done_whatever_its_flags(self):
        self.assertEqual(state.board_area("completed", ["gap"], False), "done")

    def test_nothing_happening_and_nothing_wrong_is_a_queue_not_work(self):
        self.assertEqual(state.board_area("planned", ["idle"], False), "queued")


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
