#!/usr/bin/env python3
"""Regressions for the map's data contract.

The renderer's only guarantee — that it cannot show what it never saw — rests on
the shape of two documents. These tests hold that shape, the anonymisation that
goes with it and the layout decisions the picture depends on.

    python3 -m unittest discover -s scripts -p 'test_*.py'
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import process_map_recorder as recorder
import process_map_render as render
import process_map_schema as schema
import process_map_state as state


def a_task(**over) -> dict:
    task = {"id": 1, "title": "Задача", "status": "planned", "status_detail": None,
            "dir": "001-task", "gates": [], "flags": [],
            "run": {"state": None, "runner": None, "workflow": None, "sandbox": None,
                    "stop_reason": None, "exit_code": None, "pid": None,
                    "alive": False, "progress": None}}
    task.update(over)
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


if __name__ == "__main__":
    unittest.main()
