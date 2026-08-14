#!/usr/bin/env python3
"""Что обязана держать одна продолжающаяся фоновая сессия под усиленной целью.

Режим стоит на трёх утверждениях, и каждое из них здесь проверяется тем, что
наблюдаемо, а не тем, что написано в промпте: разговор один и тот же на всех
ходах; тяжёлый стартовый контекст собирается ровно один раз; пробуждение по
стоячей цели не может кончиться молчанием.

Плюс роль тика после этой работы: пока сессия жива, он не поднимает второго
продакта, а когда она умерла — поднимает её заново, и обычная работа без
усиленной цели не получает ничего лишнего.

    python3 -m unittest discover -s scripts -p 'test_*.py'
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import goal_session as session
import process_map_schema as schema
import product_goal as goals
import thread_tick as tick


def goal(state: str = "active", control: str = "reinforced", waiting: list[int] | None = None,
         goal_id: str = "0001") -> dict:
    return {"id": goal_id, "thread": "process", "state": state, "control": control,
            "outcome": "пользователь получает результат без терминала",
            "observable": ["исходный сценарий прошёл через установленный продукт"],
            "main_task": 1094, "correctives": [], "gap": "ремонт не установлен",
            "next_transition": "вернуть основную задачу", "pause": None, "signals": [],
            "waiting_on": [1094] if waiting is None else waiting,
            "src": "долговечная запись цели 0001"}


def look(live: list[int] | None = None, blocked: list[int] | None = None,
         pickup: list[int] | None = None, heads: dict | None = None,
         panel: list[dict] | None = None) -> dict:
    panel = [goal()] if panel is None else panel
    return {
        "at": "2026-08-13T00:00:00+00:00",
        "report": {"title": "Процессный контур", "live_runs": [], "needs_attention": [],
                   "ready_to_start": [], "can_pick_up": [], "repos": []},
        "goals": panel,
        "live": live or [], "blocked": blocked or [], "completed_ready": [],
        "pickup": pickup or [], "heads": heads or {},
        "goal_state": {str(item["id"]): {
            "state": item["state"], "control": item["control"], "gap": item["gap"],
            "waiting_on": item["waiting_on"],
            "correctives": {str(c["task"]): bool(c["accepted"])
                            for c in item["correctives"]}} for item in panel},
    }


class Liveness(unittest.TestCase):
    """«Сессия жива» — самая дорогая надпись режима: при ней тик молчит."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.previous = session.SESSIONS
        session.SESSIONS = Path(self.tmp.name)
        self.addCleanup(lambda: setattr(session, "SESSIONS", self.previous))

    def test_no_record_at_all_is_not_a_live_session(self):
        self.assertFalse(session.liveness({})["live"])

    def test_an_unreadable_record_is_not_read_as_absence(self):
        """Пустой ответ поднял бы вторую сессию поверх работающей первой."""
        (Path(self.tmp.name) / "process.json").write_text("{не json")
        record = session.read("process")
        self.assertIn("unreadable", record)
        self.assertFalse(session.liveness(record)["live"])
        self.assertIn("не читается", session.liveness(record)["reason"])

    def test_a_live_process_with_a_fresh_heartbeat_is_live(self):
        record = {"pid": os.getpid(), "since": session.start_tick(os.getpid()),
                  "heartbeat": session.now()}
        self.assertTrue(session.liveness(record)["live"])

    def test_a_reused_pid_is_not_the_same_session(self):
        """pid без стартового тика через сутки указывает на чужой процесс."""
        record = {"pid": os.getpid(), "since": (session.start_tick(os.getpid()) or 0) + 7,
                  "heartbeat": session.now()}
        alive = session.liveness(record)
        self.assertFalse(alive["live"])
        self.assertIn("переиспользован", alive["reason"])

    def test_a_process_that_stopped_moving_is_not_live(self):
        stale = (datetime.now(timezone.utc)
                 - timedelta(seconds=session.HEARTBEAT_STALE_SECONDS + 60)).isoformat()
        record = {"pid": os.getpid(), "since": session.start_tick(os.getpid()),
                  "heartbeat": stale}
        alive = session.liveness(record)
        self.assertFalse(alive["live"])
        self.assertIn("не подавал признаков", alive["reason"])

    def test_a_stopped_session_is_not_live_even_while_its_process_exits(self):
        record = {"pid": os.getpid(), "since": session.start_tick(os.getpid()),
                  "heartbeat": session.now(),
                  "stopped": {"at": session.now(), "reason": "принудительная ротация"}}
        self.assertFalse(session.liveness(record)["live"])


class WhenTheModeIsOn(unittest.TestCase):
    """Обычная работа не получает второй машины состояний — и не получает сессии."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.previous = os.environ.get("PRODUCT_OWNER_GOALS")
        os.environ["PRODUCT_OWNER_GOALS"] = str(self.root / "goals")
        self.addCleanup(self.restore)
        self.sessions = session.SESSIONS
        session.SESSIONS = self.root / "sessions"
        self.addCleanup(lambda: setattr(session, "SESSIONS", self.sessions))

    def restore(self) -> None:
        if self.previous is None:
            os.environ.pop("PRODUCT_OWNER_GOALS", None)
        else:
            os.environ["PRODUCT_OWNER_GOALS"] = self.previous

    def test_a_normal_goal_does_not_raise_a_continuous_session(self):
        goals.open_goal("process", "результат", ["условие"], 100)
        self.assertEqual(session.reinforced("process"), [])
        state = session.watchdog("process", datetime.now(timezone.utc), act=False)
        self.assertEqual(state["mode"], "none")

    def test_an_observed_deviation_turns_the_session_on(self):
        opened = goals.open_goal("process", "результат", ["условие"], 100)
        goals.add_signal(opened["id"], "manual_bypass", "ручной обход",
                         "строка обхода в trace.md задачи 100")
        self.assertEqual([item["id"] for item in session.reinforced("process")],
                         [opened["id"]])

    def test_an_unreadable_goal_keeps_the_mode_on(self):
        (self.root / "goals").mkdir(parents=True)
        (self.root / "goals" / "0009.json").write_text("{сломано")
        self.assertTrue(session.reinforced("process"))

    def test_the_watchdog_does_not_raise_a_second_owner_over_a_live_session(self):
        opened = goals.open_goal("process", "результат", ["условие"], 100)
        goals.add_signal(opened["id"], "manual_bypass", "обход", "trace.md задачи 100")
        session.write("process", {"pid": os.getpid(),
                                  "since": session.start_tick(os.getpid()),
                                  "heartbeat": session.now(),
                                  "session": {"id": "s-1", "turns": 3}})
        launched = []
        original, session.launch = session.launch, lambda thread: launched.append(thread)
        try:
            state = session.watchdog("process", datetime.now(timezone.utc))
        finally:
            session.launch = original
        self.assertTrue(state["live"])
        self.assertEqual(launched, [])

    def test_the_watchdog_restores_a_dead_session_by_itself(self):
        opened = goals.open_goal("process", "результат", ["условие"], 100)
        goals.add_signal(opened["id"], "manual_bypass", "обход", "trace.md задачи 100")
        session.write("process", {"pid": 999_999_999, "since": 1,
                                  "heartbeat": session.now(),
                                  "session": {"id": "s-1", "turns": 3}})

        def started_and_leading(thread: str) -> dict:
            """Единица поднялась и записала себя ведущей — так это и выглядит."""
            session.write(thread, {"pid": os.getpid(),
                                   "since": session.start_tick(os.getpid()),
                                   "heartbeat": session.now(),
                                   "session": {"id": "s-1", "turns": 3}})
            return {"started": True, "boundary": "systemd_transient_unit"}

        original, route = session.launch, session.session_route
        session.launch = started_and_leading
        session.session_route = lambda: {"ok": True, "engine": "claude", "model": "opus",
                                         "reason": "opus_remaining=80%", "error": None,
                                         "src": "проба маршрута"}
        try:
            state = session.watchdog("process", datetime.now(timezone.utc))
        finally:
            session.launch, session.session_route = original, route
        self.assertTrue(state["recovered"])
        self.assertTrue(state["holds"])
        self.assertEqual(state["previous_session"], "s-1")
        self.assertIn("не наблюдается", state["recovery_reason"])


class TheGoalIsNeverLeftWithoutAnOwner(unittest.TestCase):
    """F-003: уступка стоит на наблюдённой сессии, а не на принятом запросе запуска.

    Порядок, в котором терялась цель, был ровно такой: watchdog отправлял
    `systemd-run`, получал ноль, тик по этому нулю не будил своего продакта, а
    поднятая единица видела маршрут на Codex и выходила. Каждое звено при этом
    отчитывалось об успехе, и только вместе они оставляли активную цель без
    продакта до следующего тика — где всё повторялось. Здесь проверяется весь
    порядок целиком, а не каждое звено по отдельности.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.previous = os.environ.get("PRODUCT_OWNER_GOALS")
        os.environ["PRODUCT_OWNER_GOALS"] = str(self.root / "goals")
        self.addCleanup(self.restore_env)
        self.sessions, session.SESSIONS = session.SESSIONS, self.root / "sessions"
        self.addCleanup(lambda: setattr(session, "SESSIONS", self.sessions))
        self.route, self.launch = session.session_route, session.launch
        self.addCleanup(self.restore_stubs)
        self.opened = goals.open_goal("process", "результат", ["условие"], 1094)
        goals.add_signal(self.opened["id"], "manual_bypass", "обход", "trace.md 1094")
        session.write("process", {"pid": 999_999_999, "since": 1,
                                  "heartbeat": session.now(),
                                  "session": {"id": "s-old", "turns": 4}})
        self.launched: list[str] = []

    def restore_env(self) -> None:
        if self.previous is None:
            os.environ.pop("PRODUCT_OWNER_GOALS", None)
        else:
            os.environ["PRODUCT_OWNER_GOALS"] = self.previous

    def restore_stubs(self) -> None:
        session.session_route, session.launch = self.route, self.launch

    def to_codex(self) -> None:
        session.session_route = lambda: {
            "ok": False, "engine": "codex", "model": "gpt", "error": None,
            "reason": "observed_opus_and_fable_limits_exhausted", "src": "проба маршрута"}

    def to_claude(self) -> None:
        session.session_route = lambda: {
            "ok": True, "engine": "claude", "model": "opus", "error": None,
            "reason": "opus_remaining=80%", "src": "проба маршрута"}

    def test_a_route_that_left_claude_is_decided_before_the_owner_is_suppressed(self):
        self.to_codex()
        session.launch = lambda thread: self.launched.append(thread)
        state = session.watchdog("process", datetime.now(timezone.utc))
        self.assertEqual(self.launched, [], "единицу, которая сразу выйдет, не поднимают")
        self.assertFalse(state["holds"])
        self.assertIn("codex", state["detail"])
        self.assertIn("продакт тика", state["handover"])
        self.assertIn("маршрут увёл", session.read("process")["stopped"]["reason"])

    def test_the_exact_order_that_used_to_lose_the_goal(self):
        """watchdog → решение тика → дочерняя единица на маршруте Codex."""
        self.to_codex()
        session.launch = lambda thread: {"started": True,
                                         "boundary": "systemd_transient_unit"}
        state = session.watchdog("process", datetime.now(timezone.utc))
        holds, handover = tick.session_leads(state, [goal()])
        code = session.loop("process", once=True)
        self.assertFalse(holds, "тик уступил бы сессии, которой нет")
        self.assertEqual(code, 0)
        self.assertEqual(len(handover), 1)
        self.assertIn("0001", handover[0])
        self.assertIn("ведёт продакт тика", handover[0])

    def test_a_unit_that_started_and_refused_does_not_suppress_the_tick(self):
        """Отказ дочерней единицы виден в её же снимке — и сразу, а не через срок."""
        self.to_claude()

        def refuse(thread: str) -> dict:
            session.write(thread, {**session.read(thread), "pid": None, "since": None,
                                   "stopped": {"at": session.now(),
                                               "reason": "маршрут увёл к codex"}})
            return {"started": True, "boundary": "systemd_transient_unit"}

        session.launch = refuse
        state = session.watchdog("process", datetime.now(timezone.utc))
        self.assertFalse(state["holds"])
        self.assertFalse(state["recovered"])
        self.assertIn("сразу остановилась", state["handshake"]["reason"])
        self.assertIn("продакт тика", state["handover"])

    def test_a_launch_that_never_comes_alive_does_not_suppress_the_tick(self):
        """Принятый запрос — ещё не ведущая сессия; ожидание ограничено сроком."""
        self.to_claude()
        session.launch = lambda thread: {"started": True,
                                         "boundary": "systemd_transient_unit"}
        window, poll = session.LAUNCH_HANDSHAKE_SECONDS, session.HANDSHAKE_POLL_SECONDS
        session.LAUNCH_HANDSHAKE_SECONDS, session.HANDSHAKE_POLL_SECONDS = 0.3, 0.05
        try:
            state = session.watchdog("process", datetime.now(timezone.utc))
        finally:
            session.LAUNCH_HANDSHAKE_SECONDS = window
            session.HANDSHAKE_POLL_SECONDS = poll
        self.assertFalse(state["holds"])
        self.assertIn("не начала вести направление", state["handshake"]["reason"])

    def test_the_tick_leads_a_standing_goal_the_session_cannot(self):
        dead = {"mode": "session", "holds": False, "detail": "маршрут увёл к codex"}
        holds, handover = tick.session_leads(dead, [goal()])
        self.assertFalse(holds)
        self.assertIn("0001", handover[0])
        live = {"mode": "session", "holds": True, "detail": "сессия жива"}
        self.assertEqual(tick.session_leads(live, [goal()]), (True, []))

    def test_normal_work_gets_nothing_extra_from_this(self):
        """Без усиленной цели тик ведёт направление как раньше и молча."""
        self.assertEqual(tick.session_leads({"mode": "none", "holds": False}, []),
                         (False, []))


class SignificantChange(unittest.TestCase):
    """Ход модели тратится на то, что меняет решение, и не тратится ни на что ещё."""

    def test_the_first_look_is_a_reason_to_speak(self):
        self.assertEqual(session.changes(None, look()), ["первый взгляд сессии"])

    def test_nothing_moved_is_not_a_turn(self):
        before = look(live=[1094])
        self.assertEqual(session.changes(before, look(live=[1094])), [])

    def test_a_finished_run_is_the_change_the_tick_used_to_delay(self):
        said = session.changes(look(live=[1094]), look())
        self.assertEqual(said, ["прогон задачи 1094 завершился"])

    def test_a_goal_moving_state_is_a_change(self):
        said = session.changes(look(panel=[goal(state="paused")]), look())
        self.assertIn("цель 0001: paused → active", said)

    def test_an_accepted_repair_is_a_change(self):
        repaired = goal()
        repaired["correctives"] = [{"task": 1126, "effect": "e", "return_criterion": "r",
                                    "accepted": True}]
        pending = goal()
        pending["correctives"] = [{"task": 1126, "effect": "e", "return_criterion": "r",
                                   "accepted": False}]
        said = session.changes(look(panel=[pending]), look(panel=[repaired]))
        self.assertIn("цель 0001: корректирующая задача 1126 принята", said)

    def test_a_new_commit_in_the_directions_tree_is_a_change(self):
        said = session.changes(look(heads={"/path/to/task-agent": "aaa"}),
                               look(heads={"/path/to/task-agent": "bbb"}))
        self.assertEqual(said, ["task-agent: новый коммит bbb"])

    def test_a_goal_with_a_live_run_on_its_task_is_not_standing(self):
        self.assertEqual(session.standing(look(live=[1094])), [])
        self.assertEqual([item["id"] for item in session.standing(look())], ["0001"])


class StandingGoalOutcome(unittest.TestCase):
    """F-002: молчание не является третьим исходом пробуждения по стоячей цели."""

    def setUp(self) -> None:
        self.told: list[str] = []
        self.mail: list[tuple] = []
        self.notify, tick.notify = tick.notify, self.told.append
        self.deliver, tick.deliver = tick.deliver, lambda *args, **kwargs: self.mail.append(args)
        self.addCleanup(self.restore)

    def restore(self) -> None:
        tick.notify, tick.deliver = self.notify, self.deliver

    def moment(self) -> datetime:
        return datetime.now(timezone.utc)

    def test_a_started_run_on_the_goals_task_is_the_outcome(self):
        checked = session.post_check("process", [goal()], [1094], "SILENT", self.moment())
        self.assertTrue(checked["resolved"])
        self.assertEqual(self.told, [])

    def test_a_named_blocker_is_the_other_outcome(self):
        checked = session.post_check("process", [goal()], [],
                                     "ПОВОД: механика\nдерево занято прогоном 1127",
                                     self.moment())
        self.assertTrue(checked["resolved"])
        self.assertEqual(self.told, [])

    def test_silence_beside_an_unrelated_live_run_is_refused_without_mail(self):
        """Ровно та дыра, которую назвало ревью: `idle` не срабатывает, цель стоит."""
        checked = session.post_check("process", [goal()], [1126], "SILENT", self.moment())
        self.assertFalse(checked["resolved"])
        self.assertEqual(checked["goals"], ["0001"])
        self.assertEqual(len(self.told), 1)
        self.assertIn("ни живого прогона", self.told[0])
        self.assertEqual(self.mail, [])

    def test_an_empty_answer_is_refused_like_silence(self):
        checked = session.post_check("process", [goal()], [], "   ", self.moment())
        self.assertFalse(checked["resolved"])

    def test_a_direction_without_standing_goals_is_not_judged(self):
        self.assertIsNone(session.post_check("process", [], [], "SILENT", self.moment()))
        self.assertEqual(self.told, [])

    def test_the_tick_fails_its_unit_on_an_unresolved_standing_goal(self):
        """Код возврата — единственный сигнал, переживающий проверку, которую никто не читал."""
        source = Path(tick.__file__).read_text()
        self.assertIn('return 1 if goal_check and not goal_check["resolved"] else verdict',
                      source)

    def test_the_tick_judges_every_standing_goal_not_only_the_announced_one(self):
        """Частота повтора решает, что сказать пользователю, а не что проверить."""
        source = Path(tick.__file__).read_text()
        block = source[source.index("def goal_watch("):source.index("def codex_window(")]
        self.assertIn('"objects": objects', block)
        self.assertIn('goals["objects"]', source[source.index("def main("):])


class OneConversation(unittest.TestCase):
    """Идентичность разговора и отсутствие пересборки стартового контекста."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.previous = os.environ.get("PRODUCT_OWNER_GOALS")
        os.environ["PRODUCT_OWNER_GOALS"] = str(self.root / "goals")
        self.addCleanup(self.restore_env)
        self.sessions, session.SESSIONS = session.SESSIONS, self.root / "sessions"
        self.addCleanup(lambda: setattr(session, "SESSIONS", self.sessions))
        opened = goals.open_goal("process", "результат", ["условие"], 1094)
        goals.add_signal(opened["id"], "manual_bypass", "обход", "trace.md задачи 1094")

        self.prompts: list[dict] = []
        self.turn, session.run_turn = session.run_turn, self.fake_turn
        self.observations = [look(live=[1094]), look(live=[1094], blocked=[1094]),
                             look(live=[1094], blocked=[1094])]
        self.observe, session.observation = session.observation, self.fake_observation
        self.notify, tick.notify = tick.notify, lambda text: None
        self.deliver, tick.deliver = tick.deliver, lambda *args, **kwargs: {"action": "hold"}
        self.addCleanup(self.restore_stubs)

    def restore_env(self) -> None:
        if self.previous is None:
            os.environ.pop("PRODUCT_OWNER_GOALS", None)
        else:
            os.environ["PRODUCT_OWNER_GOALS"] = self.previous

    def restore_stubs(self) -> None:
        session.run_turn, session.observation = self.turn, self.observe
        tick.notify, tick.deliver = self.notify, self.deliver

    def fake_observation(self, thread: str) -> dict:
        return self.observations[min(len(self.prompts), len(self.observations) - 1)]

    def fake_turn(self, model: str, session_id: str, prompt: str, opening: bool) -> dict:
        self.prompts.append({"id": session_id, "opening": opening, "prompt": prompt})
        return {"ok": True, "reply": "работаю по задаче 1094", "session_id": session_id,
                "duration_seconds": 1.0, "cost_usd": 0.0, "error": None,
                "usage": {"input_tokens": 1, "cache_creation_input_tokens": 2,
                          "cache_read_input_tokens": 30_000, "output_tokens": 4}}

    def test_the_opening_turn_carries_the_startup_context_and_the_next_one_does_not(self):
        session.MIN_TURN_GAP_SECONDS = 0
        session.loop("process", once=True)
        opening = self.prompts[0]["prompt"]
        self.assertTrue(self.prompts[0]["opening"])
        self.assertIn("Долговечные цели этого направления", opening)
        delta = session.delta_prompt("process", look(), ["прогон задачи 1094 завершился"])
        self.assertNotIn("редакция портфельного плана", delta)
        self.assertNotIn("Долговечные цели этого направления", delta)
        self.assertIn("прогон задачи 1094 завершился", delta)
        # Правила режима стоят на обоих ходах: разговор один, но ход, который
        # забыл правило, ошибается ровно там, где режим и заводился.
        self.assertIn("Молчание третьим исходом не является", delta)
        self.assertIn("Молчание третьим исходом не является", opening)

    def test_the_record_says_which_turn_rebuilt_the_context(self):
        session.MIN_TURN_GAP_SECONDS = 0
        session.loop("process", once=True)
        record = session.read("process")
        self.assertEqual(record["turns"][0]["context_rebuilt"], True)
        self.assertEqual(record["turns"][0]["kind"], "open")
        self.assertEqual(record["session"]["turns"], 1)
        self.assertEqual(record["turns"][0]["usage"]["cache_read_input_tokens"], 30_000)

    def test_a_failed_turn_rotates_instead_of_continuing_blind(self):
        session.MIN_TURN_GAP_SECONDS = 0
        session.run_turn = lambda *args, **kwargs: {
            "ok": False, "reply": "", "usage": None, "duration_seconds": 1.0,
            "error": "окно исчерпано"}
        self.assertEqual(session.loop("process", once=True), 1)
        record = session.read("process")
        self.assertIn("окно исчерпано", record["stopped"]["reason"])
        self.assertEqual(record["stopped"]["rotation"], "требуется новая сессия")

    def test_recovery_continues_the_same_conversation(self):
        """Новый разговор стоил бы ровно ту пересборку, ради отказа от которой режим и заведён."""
        session.MIN_TURN_GAP_SECONDS = 0
        session.write("process", {"pid": 999_999_999, "since": 1,
                                  "heartbeat": session.now(),
                                  "session": {"id": "s-old", "turns": 5}})
        session.loop("process", once=True)
        record = session.read("process")
        self.assertEqual(record["recovered"]["previous_session"], "s-old")
        self.assertEqual(record["recovered"]["turns_before"], 5)
        self.assertEqual(record["recovered"]["resumed_conversation"], True)
        self.assertEqual(record["session"]["id"], "s-old")
        self.assertEqual(record["turns"][0]["kind"], "recovery")
        self.assertFalse(record["turns"][0]["context_rebuilt"])
        self.assertFalse(self.prompts[0]["opening"])
        self.assertIn("Тебя подняли заново", self.prompts[0]["prompt"])

    def test_a_rotation_opens_a_new_conversation_instead(self):
        """Ротация — это «продолжать нечем или нельзя», и она записана явно."""
        session.MIN_TURN_GAP_SECONDS = 0
        session.write("process", {"pid": 999_999_999, "since": 1,
                                  "heartbeat": session.now(),
                                  "session": {"id": "s-old", "turns": 60},
                                  "stopped": {"at": session.now(), "reason": "60 ходов",
                                              "rotation": "требуется новая сессия"}})
        session.loop("process", once=True)
        record = session.read("process")
        self.assertNotEqual(record["session"]["id"], "s-old")
        self.assertEqual(record["recovered"]["resumed_conversation"], False)
        self.assertEqual(record["turns"][0]["kind"], "open")
        self.assertTrue(record["turns"][0]["context_rebuilt"])

    def test_the_conversation_is_resumed_rather_than_started_again(self):
        """`--session-id` открывает разговор, `--resume` продолжает тот же."""
        source = Path(session.__file__).read_text()
        block = source[source.index("def run_turn("):source.index("def loop(")]
        self.assertIn('["--session-id", session_id] if opening else ["--resume", session_id]',
                      block)


class OnTheBoard(unittest.TestCase):
    """Кто ведёт направление, видно без расспросов и с названным наблюдением."""

    def projection(self, **fields) -> dict:
        base = {"live": True, "reason": "процесс сессии наблюдается живым", "id": "s-1",
                "engine": "claude", "model": "opus", "turns": 4, "opened_at": None,
                "heartbeat": None, "last_turn_at": None,
                "last_turn_reaction_seconds": None, "recovered": False, "stopped": None,
                "src": "pid и стартовый тик /proc"}
        return {**base, **fields}

    def test_a_named_session_says_what_observed_it(self):
        schema.validate_goal_session(self.projection(), "направление")

    def test_a_session_shown_without_an_observation_is_refused(self):
        with self.assertRaises(schema.ContractError):
            schema.validate_goal_session(self.projection(src=" "), "направление")

    def test_a_live_session_without_a_conversation_identifier_is_refused(self):
        with self.assertRaises(schema.ContractError):
            schema.validate_goal_session(self.projection(id=None), "направление")

    def test_a_dead_session_must_say_why(self):
        with self.assertRaises(schema.ContractError):
            schema.validate_goal_session(
                self.projection(live=False, reason="", id=None), "направление")

    def test_the_template_prints_it_beside_the_goals(self):
        template = (Path(session.__file__).parent / "process_map_template.html").read_text()
        self.assertIn("function goalSessionNode(session)", template)
        self.assertIn("goalSessionNode(panel.goal_session)", template)


class TheTickBecomesAWatchdog(unittest.TestCase):
    """Двадцать минут остаются, но перестают быть обычным способом управления."""

    def test_the_tick_does_not_wake_its_own_owner_while_a_session_holds(self):
        source = Path(tick.__file__).read_text()
        body = source[source.index("def main("):]
        self.assertIn("session = goal_session.watchdog(", body)
        self.assertIn("session_holds, handover = session_leads(session, goals[\"objects\"])",
                      body)
        self.assertIn("woke = (bool(events) or args.force) and not session_holds", body)

    def test_the_board_is_told_who_is_driving_even_when_nobody_is(self):
        source = Path(tick.__file__).read_text()
        self.assertIn('"goal_session": session,', source)

    def test_the_outcome_line_tells_a_held_direction_from_a_quiet_one(self):
        quiet = tick.outcome({"live": []}, None, False,
                             {"live_runs": [], "can_pick_up": [], "ready_to_start": [],
                              "decided_not_done": [], "waiting_user": []}, None)
        held = tick.outcome({"live": []}, None, False,
                            {"live_runs": [], "can_pick_up": [], "ready_to_start": [],
                             "decided_not_done": [], "waiting_user": []},
                            {"mode": "session", "live": True, "turns": 7,
                             "session": {"id": "s-1"}})
        self.assertIn("ни событий", quiet)
        self.assertIn("непрерывная сессия", held)
        self.assertIn("ходов 7", held)
        # Имя сессии в эту фразу не входит: её читает человек с доски, а UUID
        # написан машине. Найти его есть где — та же запись несёт
        # `goal_session.session.id`.
        self.assertNotIn("s-1", held)

    def test_a_recovered_session_is_not_reported_as_a_quiet_check(self):
        recovered = tick.outcome({"live": []}, None, False,
                                 {"live_runs": [], "can_pick_up": [], "ready_to_start": [],
                                  "decided_not_done": [], "waiting_user": []},
                                 {"mode": "session", "live": False, "recovered": True,
                                  "recovery_reason": "процесс 1 не наблюдается в /proc"})
        self.assertIn("watchdog поднял", recovered)

    def test_the_session_outlives_the_process_that_started_it(self):
        """Тик — oneshot: всё, что осталось в его контрольной группе, systemd уберёт."""
        source = Path(session.__file__).read_text()
        block = source[source.index("def launch("):source.index("def watchdog(")]
        self.assertIn("systemd-run", block)
        self.assertIn("session_detachment_only", block)


if __name__ == "__main__":
    unittest.main()
