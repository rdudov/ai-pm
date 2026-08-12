#!/usr/bin/env python3
"""Что долговечная цель обязана держать, когда её читает новый процесс.

The mode this covers exists because a background wake-up has no memory: a new
process every interval, possibly on the other model family. So the tests are
about what survives that and what is refused rather than argued about —
accepting a repair must not close the promise, and a goal must not close on
anything cheaper than a live check of the installed product.

    python3 -m unittest discover -s scripts -p 'test_*.py'
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))

import process_map_schema as schema
import process_map_state as state
import product_goal as goals
import thread_tick as tick


def task_tree(root: Path, task_id: int, status: str, trace: str = "",
              rounds: list[dict] | None = None) -> Path:
    directory = root / "tasks" / f"{task_id}-demo"
    directory.mkdir(parents=True)
    (directory / "task.md").write_text(
        f'---\nid: {task_id}\nslug: "{task_id}-demo"\nstatus: "{status}"\n---\n# demo\n')
    if trace:
        (directory / "trace.md").write_text(trace)
    if rounds is not None:
        (directory / "reviews").mkdir()
        (directory / "reviews" / "rounds.jsonl").write_text(
            "\n".join(json.dumps(record) for record in rounds) + "\n")
    return directory


class GoalStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.previous = {key: os.environ.get(key)
                         for key in ("PRODUCT_OWNER_GOALS", "PRODUCT_OWNER_TASKS_REPO")}
        os.environ["PRODUCT_OWNER_GOALS"] = str(self.root / "goals")
        os.environ["PRODUCT_OWNER_TASKS_REPO"] = str(self.root / "repo")
        goals.TASKS_REPO = self.root / "repo"
        self.addCleanup(self.restore)

    def restore(self) -> None:
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        goals.TASKS_REPO = Path(os.environ.get("PRODUCT_OWNER_TASKS_REPO")
                                or "/opt/projects/companion-agent")
        self.tmp.cleanup()

    def opened(self, task_id: int = 100) -> dict:
        return goals.open_goal(
            "process", "пользователь получает результат без терминала",
            ["исходный сценарий прошёл через установленный продукт"], task_id)

    # -- what a goal must carry ------------------------------------------------

    def test_a_goal_without_an_observable_condition_is_refused(self):
        with self.assertRaises(goals.GoalError):
            goals.open_goal("process", "результат", [" "], 100)

    def test_a_goal_survives_a_new_process(self):
        """Ровно то, чего нет у сеанса: цель читается тем, кто её не заводил."""
        opened = self.opened()
        again = goals.load(opened["id"])
        self.assertEqual(again["outcome"], opened["outcome"])
        self.assertEqual([goal["id"] for goal in goals.active("process")], [opened["id"]])
        self.assertEqual(goals.active("moex"), [])

    # -- the corrective task path ---------------------------------------------

    def test_a_corrective_task_needs_an_explicit_pause(self):
        goal = self.opened()
        with self.assertRaises(goals.GoalError):
            goals.add_corrective(goal["id"], 101, "эффект", "критерий")

    def test_a_corrective_task_needs_effect_and_return_criterion(self):
        goal = self.opened()
        goals.pause(goal["id"], "оснастка не пускает автора", "отказ в trace.md")
        with self.assertRaises(goals.GoalError):
            goals.add_corrective(goal["id"], 101, "эффект", "  ")

    def test_a_corrective_task_turns_on_control_by_itself(self):
        goal = self.opened()
        goals.pause(goal["id"], "оснастка не пускает автора", "отказ в trace.md")
        after = goals.add_corrective(goal["id"], 101, "автор входит в свою задачу",
                                     "живой запуск автора проходит без ручных флагов")
        self.assertEqual(after["control"], goals.REINFORCED)
        self.assertIn("corrective_task", {item["code"] for item in after["signals"]})

    def test_an_unfinished_corrective_task_is_not_accepted(self):
        task_tree(self.root / "repo", 101, "in_progress")
        goal = self.opened()
        goals.pause(goal["id"], "причина", "источник")
        goals.add_corrective(goal["id"], 101, "эффект", "критерий")
        with self.assertRaises(goals.GoalError):
            goals.accept_corrective(goal["id"], 101, "живой прогон")

    def test_accepting_a_repair_returns_the_main_task_and_does_not_close_the_goal(self):
        """Именно тот шаг, на котором цепочка раньше останавливалась."""
        task_tree(self.root / "repo", 100, "blocked")
        task_tree(self.root / "repo", 101, "completed")
        goal = self.opened()
        goals.pause(goal["id"], "причина", "источник")
        goals.add_corrective(goal["id"], 101, "эффект", "критерий")
        accepted = goals.accept_corrective(goal["id"], 101, "живой прогон 101")
        self.assertNotEqual(accepted["state"], goals.CLOSED)
        with self.assertRaises(goals.GoalError):
            goals.close(goal["id"], "живая проверка", "наблюдение")
        resumed = goals.resume(goal["id"], gap="ревью не принято")
        self.assertEqual(resumed["state"], goals.ACTIVE)
        self.assertIsNone(resumed["pause"])
        self.assertEqual(goals.live_tasks(resumed), [100])

    def test_the_main_task_does_not_come_back_while_a_repair_is_open(self):
        goal = self.opened()
        goals.pause(goal["id"], "причина", "источник")
        goals.add_corrective(goal["id"], 101, "эффект", "критерий")
        with self.assertRaises(goals.GoalError):
            goals.resume(goal["id"])

    # -- closing ---------------------------------------------------------------

    def test_a_goal_does_not_close_on_a_completed_status_alone(self):
        task_tree(self.root / "repo", 100, "completed")
        goal = self.opened()
        with self.assertRaises(goals.GoalError):
            goals.close(goal["id"], "  ", "наблюдение")
        with self.assertRaises(goals.GoalError):
            goals.close(goal["id"], "живая проверка", "  ")

    def test_a_goal_does_not_close_while_its_main_task_is_not_finished(self):
        task_tree(self.root / "repo", 100, "blocked")
        goal = self.opened()
        with self.assertRaises(goals.GoalError):
            goals.close(goal["id"], "живая проверка", "наблюдение")

    def test_a_goal_closes_on_a_live_check_of_the_installed_product(self):
        task_tree(self.root / "repo", 100, "completed")
        goal = self.opened()
        closed = goals.close(goal["id"], "сценарий прошёл через установленный продукт",
                             "лог живого прогона")
        self.assertEqual(closed["state"], goals.CLOSED)
        self.assertEqual(goals.active("process"), [])
        self.assertTrue((goals.root() / goals.JOURNAL).is_file())

    # -- observation -----------------------------------------------------------

    def test_normal_work_gets_no_reinforced_control(self):
        task_tree(self.root / "repo", 100, "in_progress",
                  trace="- обычная работа\n",
                  rounds=[{"round": 1, "finding_ids": [], "repeated_finding_ids": []},
                          {"round": 2, "finding_ids": ["F-001"], "repeated_finding_ids": []}])
        goal = self.opened()
        self.assertEqual(goals.observe(goal), [])
        self.assertEqual(goals.apply_observed(goal["id"])["control"], goals.NORMAL)

    def test_a_third_round_a_repeat_and_a_second_refusal_are_observed(self):
        task_tree(
            self.root / "repo", 100, "blocked",
            trace=("- Refused this review launch (unresolved_subject)\n"
                   "- Refused this review launch (unresolved_subject)\n"),
            rounds=[{"round": 1, "finding_ids": ["F-001"], "repeated_finding_ids": []},
                    {"round": 2, "finding_ids": ["F-001"], "repeated_finding_ids": ["F-001"]},
                    {"round": 3, "finding_ids": ["F-002"], "repeated_finding_ids": []}])
        goal = self.opened()
        codes = {signal["code"] for signal in goals.observe(goal)}
        self.assertEqual(codes, {"third_review_round", "repeat_finding",
                                 "launch_refused_again"})
        after = goals.apply_observed(goal["id"])
        self.assertEqual(after["control"], goals.REINFORCED)
        self.assertTrue(all(signal["src"] for signal in after["signals"]))
        # Idempotent: the same artifacts observed again add nothing.
        self.assertEqual(len(goals.apply_observed(goal["id"])["signals"]),
                         len(after["signals"]))

    def test_an_invented_signal_is_refused_and_a_sourceless_one_too(self):
        goal = self.opened()
        with self.assertRaises(goals.GoalError):
            goals.add_signal(goal["id"], "что-то своё", "текст", "источник")
        with self.assertRaises(goals.GoalError):
            goals.add_signal(goal["id"], "manual_bypass", "текст", "  ")

    # -- what the wake-up and the board are shown ------------------------------

    def test_a_goal_with_nothing_live_is_news_and_one_with_a_live_run_is_not(self):
        goal = self.opened()
        self.assertTrue(goals.standing("process", [999]))
        self.assertFalse(goals.standing("process", [100]))
        goals.pause(goal["id"], "причина", "источник")
        goals.add_corrective(goal["id"], 101, "эффект", "критерий")
        # Paused: the goal is waiting on its repair, not on the main task.
        self.assertFalse(goals.standing("process", [101]))
        self.assertTrue(goals.standing("process", [100]))

    def test_the_projection_matches_the_contract_the_board_is_promised(self):
        goal = self.opened()
        goals.pause(goal["id"], "причина", "источник")
        goals.add_corrective(goal["id"], 101, "эффект", "критерий")
        projected = goals.panel("process")[0]
        schema.validate_goal(projected, "цель")
        self.assertEqual(projected["waiting_on"], [101])

    def test_the_board_refuses_a_goal_shown_without_what_observed_it(self):
        goal = goals.projection(self.opened())
        with self.assertRaises(schema.ContractError):
            schema.validate_goal({**goal, "src": " "}, "цель")
        with self.assertRaises(schema.ContractError):
            schema.validate_goal({**goal, "control": "строгий"}, "цель")

    def test_the_wake_up_block_names_the_promise_and_the_step_after_the_repair(self):
        goal = self.opened()
        goals.pause(goal["id"], "оснастка не пускает автора", "отказ в trace.md")
        goals.add_corrective(goal["id"], 101, "автор входит в свою задачу",
                             "живой запуск автора проходит")
        block = goals.block("process")
        self.assertIn("основная задача: 100", block)
        self.assertIn("корректирующая задача 101", block)
        self.assertIn("проверкой исходного сценария", block)
        self.assertEqual(goals.block("moex"), "")

    def test_an_unreadable_goal_is_not_an_absent_goal(self):
        self.opened()
        (goals.root() / "broken.json").write_text("{не json")
        listed = goals.load_all()
        self.assertEqual(len(listed), 2)
        self.assertTrue(any(item.get("unreadable") for item in listed))


class TickReadsGoals(unittest.TestCase):
    """Что тик делает с целями до того, как решит, кого будить."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.previous = os.environ.get("PRODUCT_OWNER_GOALS")
        os.environ["PRODUCT_OWNER_GOALS"] = str(self.root / "goals")
        self.addCleanup(self.restore)
        self.goal = goals.open_goal("process", "результат", ["условие"], 100)

    def restore(self) -> None:
        if self.previous is None:
            os.environ.pop("PRODUCT_OWNER_GOALS", None)
        else:
            os.environ["PRODUCT_OWNER_GOALS"] = self.previous
        self.tmp.cleanup()

    def test_a_standing_goal_wakes_the_owner_and_is_written_to_the_state_file(self):
        moment = datetime.now(timezone.utc)
        watch = tick.goal_watch("process", {"live_runs": []}, {}, moment)
        self.assertTrue(watch["standing"])
        self.assertEqual([item["id"] for item in watch["panel"]], [self.goal["id"]])

    def test_the_same_standing_goal_is_not_repeated_inside_the_interval(self):
        moment = datetime.now(timezone.utc)
        first = tick.goal_watch("process", {"live_runs": []}, {}, moment)
        stored = {"goal_reminder": first["reminder"],
                  "goals": first["panel"]}
        again = tick.goal_watch("process", {"live_runs": []}, stored,
                                moment + timedelta(seconds=30))
        self.assertFalse(again["standing"])
        later = tick.goal_watch("process", {"live_runs": []}, stored,
                                moment + timedelta(seconds=tick.GOAL_REMIND_SECONDS + 1))
        self.assertTrue(later["standing"])

    def test_turning_on_control_is_a_transition(self):
        moment = datetime.now(timezone.utc)
        before = tick.goal_watch("process", {"live_runs": []}, {}, moment)
        goals.add_signal(self.goal["id"], "manual_bypass", "обошли руками",
                         "запись продакта")
        after = tick.goal_watch("process", {"live_runs": []},
                                {"goals": before["panel"]}, moment)
        self.assertTrue(any("усиленный контроль" in line for line in after["transitions"]))

    def test_an_unreadable_store_is_said_out_loud(self):
        (self.root / "goals").mkdir(parents=True, exist_ok=True)
        os.environ["PRODUCT_OWNER_GOALS"] = str(self.root / "goals" / "not-a-directory")
        (self.root / "goals" / "not-a-directory").write_text("не каталог")
        watch = tick.goal_watch("process", {"live_runs": []}, {}, datetime.now(timezone.utc))
        self.assertEqual(watch["panel"], [])


class AwakeIsNotDeciding(unittest.TestCase):
    """Спящий терминал не держит фоновую цель.

    Именно та половина, из-за которой пользователю приходилось следить за
    доступностью терминала: бодрствующий процесс уступки не заслуживает,
    заслуживает — работающий.
    """

    def owner(self, pid: int = 4242) -> dict:
        return {"pid": pid, "kind": "session", "thread": None,
                "worktrees": ["/opt/projects/companion-agent"],
                "since": "2026-08-12T10:00:00+00:00", "age_seconds": 9000,
                "src": "командная строка процесса в /proc"}

    def test_a_first_sighting_is_treated_as_working(self):
        activity = state.owner_activity(self.owner(os.getpid()), {})
        self.assertTrue(activity["active"])
        self.assertIsNone(activity["cpu_delta"])
        self.assertIn("впервые", activity["src"])

    def test_a_process_that_burned_no_time_is_a_window_left_open(self):
        me = self.owner(os.getpid())
        seen = state.owner_activity(me, {})
        previous = {f"{me['pid']}:{me['since']}": {
            "cpu_seconds": seen["cpu_seconds"],
            "observed_at": (datetime.now(timezone.utc)
                            - timedelta(seconds=state.OWNER_IDLE_SECONDS + 60)).isoformat()}}
        activity = state.owner_activity(me, previous)
        self.assertFalse(activity["active"])
        self.assertIn("не двигалось", activity["src"])

    def test_a_process_that_did_work_still_gets_the_right_of_way(self):
        me = self.owner(os.getpid())
        seen = state.owner_activity(me, {})
        previous = {f"{me['pid']}:{me['since']}": {
            # Eighteen seconds of processor time behind the current reading:
            # a session talking to a model burns that between two wake-ups.
            "cpu_seconds": seen["cpu_seconds"] - 18,
            "observed_at": (datetime.now(timezone.utc)
                            - timedelta(seconds=state.OWNER_IDLE_SECONDS + 60)).isoformat()}}
        self.assertTrue(state.owner_activity(me, previous)["active"])

    def test_the_tick_does_not_yield_to_it_and_writes_that_down(self):
        idle = {**self.owner(), "activity": {
            "cpu_seconds": 12.0, "cpu_delta": 0.0, "measured_over": 1200,
            "active": False, "src": "процессорное время не двигалось 1200 с"}}
        report = {"worktrees": ["/opt/projects/companion-agent"],
                  "owners_awake": [idle], "ready_to_start": [], "decided_not_done": []}
        yielded = tick.yielded(report)
        self.assertEqual(yielded["to"], [])
        self.assertEqual([item["pid"] for item in yielded["not_yielded_to"]], [idle["pid"]])
        # And an idle owner may not become a reason for standing still.
        self.assertEqual(
            [reason["code"] for reason in tick.idle_reasons(
                {"live_runs": [], "waiting_user": [], "can_pick_up": [],
                 "ready_to_start": [], "decided_not_done": []},
                {"yielded_to_awake_owner": yielded})],
            [])

    def test_a_working_owner_is_still_yielded_to(self):
        busy = {**self.owner(), "activity": {
            "cpu_seconds": 30.0, "cpu_delta": 18.0, "measured_over": 1200,
            "active": True, "src": "процессорное время выросло на 18 с"}}
        report = {"worktrees": ["/opt/projects/companion-agent"],
                  "owners_awake": [busy], "ready_to_start": [], "decided_not_done": []}
        yielded = tick.yielded(report)
        self.assertEqual([item["pid"] for item in yielded["to"]], [busy["pid"]])
        self.assertEqual(yielded["not_yielded_to"], [])

    def test_the_sighting_is_written_by_the_wake_up_and_read_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "owners.json"
            state.write_owner_observations(
                [{**self.owner(), "activity": {"cpu_seconds": 7.5}}], path)
            stored = state.owner_observations(path)
            self.assertEqual(stored["4242:2026-08-12T10:00:00+00:00"]["cpu_seconds"], 7.5)

    def test_a_young_baseline_is_not_overwritten_by_the_next_direction(self):
        """Четыре направления тикают со сдвигом и пишут в один файл.

        Обновление базы на каждом из них оставило бы окно измерения короче
        порога навсегда, и «ничего не делает» нельзя было бы сказать никогда.
        """
        key = "4242:2026-08-12T10:00:00+00:00"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "owners.json"
            state.write_owner_observations(
                [{**self.owner(), "activity": {"cpu_seconds": 7.5}}], path)
            first = state.owner_observations(path)[key]["observed_at"]
            state.write_owner_observations(
                [{**self.owner(), "activity": {"cpu_seconds": 7.5}}], path)
            self.assertEqual(state.owner_observations(path)[key]["observed_at"], first)

    def test_a_matured_baseline_starts_again(self):
        key = "4242:2026-08-12T10:00:00+00:00"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "owners.json"
            stale = (datetime.now(timezone.utc)
                     - timedelta(seconds=state.OWNER_IDLE_SECONDS + 60)).isoformat()
            path.write_text(json.dumps({"schema_version": 1, "observed_at": stale,
                                        "owners": {key: {"cpu_seconds": 1.0,
                                                         "observed_at": stale}}}))
            state.write_owner_observations(
                [{**self.owner(), "activity": {"cpu_seconds": 9.0}}], path)
            self.assertEqual(state.owner_observations(path)[key]["cpu_seconds"], 9.0)


if __name__ == "__main__":
    unittest.main()
