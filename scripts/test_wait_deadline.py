"""Calendar waits use real goal storage/goal_watch/tick; external actors are fakes."""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

import goal_session
import outbound
import product_goal as goals
import product_memory as memory
import startup_context
import thread_tick as tick


AT = datetime(2030, 1, 2, 9, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PRODUCT_OWNER_GOALS", str(tmp_path / "goals"))
    monkeypatch.setattr(goals, "TASKS_REPO", tmp_path / "tasks-repo")
    monkeypatch.setattr(memory, "ROOT", tmp_path / "content")
    monkeypatch.setattr(memory, "current_plan", lambda: None)
    source = tmp_path / "observation.md"
    source.write_text("External dependency still pending")
    returned = tmp_path / "return.md"
    goal = goals.open_goal("alpha", "Installed user result", ["Actual acceptance"], 100)
    goals.wait(goal["id"], "External dependency", "Review the dependency", str(source),
               watch_paths=[str(returned)])
    # Directly persisted input also runs on old code for the causal counterexample.
    path = goals.root() / f"{goal['id']}.json"
    value = json.loads(path.read_text())
    value["waiting"]["review_at"] = AT.isoformat()
    path.write_text(json.dumps(value))
    return goal["id"], source, returned


def tree(path):
    return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()}


def cli(*args):
    return subprocess.run([sys.executable, str(Path(goals.__file__)), *args],
                          text=True, capture_output=True)


def test_cli_normalizes_displays_and_preserves_deadline_without_repeat_writes(store):
    gid, source, returned = store
    command = ["wait", gid, "--reason", "External dependency", "--next", "Review the dependency",
               "--src", str(source), "--review-at", "2030-01-02T10:00:00+01:00"]
    result = cli(*command)
    assert result.returncode == 0, result.stderr
    current = goals.load(gid)
    assert current["waiting"]["review_at"] == AT.isoformat()
    assert current["waiting"]["watch_paths"] == [str(returned)]
    assert AT.isoformat() in result.stdout
    assert AT.isoformat() in cli("show", gid).stdout
    assert AT.isoformat() in goals.block("alpha")
    assert goals.projection(current)["waiting"]["review_at"] == AT.isoformat()
    assert startup_context.goal_view(current)["waiting"]["review_at"] == AT.isoformat()
    before = tree(goals.root())
    assert cli(*command).returncode == 0
    goals.wait(gid, "External dependency", "Review the dependency", str(source))
    assert tree(goals.root()) == before
    goals.wait(gid, "External dependency", "Review the dependency", str(source),
               review_at=(AT + timedelta(days=1)).isoformat())
    assert goals.load(gid)["waiting"]["review_at"] != AT.isoformat()


@pytest.mark.parametrize("value", ["", "tomorrow", "2030-01-02", "2030-01-02T09:00:00",
                                    "2030-13-02T09:00:00Z", "2030-01-02T09:00:00+25:00"])
def test_invalid_cli_deadline_refuses_before_any_record_write(store, value):
    gid, source, _ = store
    before = tree(goals.root())
    result = cli("wait", gid, "--reason", "External dependency", "--next", "Review the dependency",
                 "--src", str(source), "--review-at", value)
    assert result.returncode != 0
    assert "ISO8601" in result.stderr
    assert tree(goals.root()) == before


def test_due_boundary_shared_clock_and_source_changes(store):
    gid, source, returned = store
    decision = goals.waiting_decision(goals.load(gid))
    assert goals.waiting_unchanged(decision, AT - timedelta(microseconds=1))
    assert not goals.waiting_unchanged(decision, AT)
    offset = AT.astimezone(timezone(timedelta(hours=-5)))
    assert goals.waiting_due(decision, offset)
    assert goals.standing("alpha", [], [], AT)
    assert not goals.standing("alpha", [100], [100], AT)
    returned.write_text("New return before deadline")
    assert not goals.waiting_unchanged(goals.waiting_decision(goals.load(gid)),
                                       AT - timedelta(days=1))
    source.unlink()
    assert not goals.waiting_unchanged(goals.waiting_decision(goals.load(gid)),
                                       AT - timedelta(days=1))


def test_legacy_without_date_has_no_inferred_deadline(store):
    gid, source, _ = store
    goals.resume(gid)
    goals.wait(gid, "Wait until 2000-01-01T09:00:00Z", "Review later", str(source))
    decision = goals.waiting_decision(goals.load(gid))
    assert decision.get("review_at") is None
    assert goals.waiting_unchanged(decision, AT)
    assert not goals.standing("alpha", [], [], AT)


def test_before_deadline_observes_exact_files_without_history_traversal(store, tmp_path, monkeypatch):
    gid, source, returned = store
    history = tmp_path / "unrelated-history"
    history.mkdir()
    for i in range(64):
        (history / f"{i}.bin").write_bytes(b"old history" * 16384)
    before = tree(goals.root())
    opened = []
    original = Path.open

    def observe(path, *a, **kw):
        assert not path.is_relative_to(history), "Unrelated historical bytes read"
        opened.append(path)
        return original(path, *a, **kw)

    with monkeypatch.context() as observing:
        observing.setattr(Path, "open", observe)
        observing.setattr(Path, "rglob", lambda *a: pytest.fail("Recursive history traversal"))
        for seconds in (1200, 600, 1):
            watch = tick.goal_watch("alpha", {"live_runs": []}, {}, AT - timedelta(seconds=seconds))
            assert watch["standing"] == watch["objects"] == watch["transitions"] == []
    assert source in opened
    assert all(path == source or path.is_relative_to(goals.root()) for path in opened)
    assert tree(goals.root()) == before


@pytest.mark.parametrize("unknown", ["missing", "unreadable", "legacy_hash"])
def test_future_deadline_does_not_bless_unknown_source(store, monkeypatch, unknown):
    gid, source, _ = store
    previous = goals.panel("alpha")
    if unknown == "missing":
        source.unlink()
    elif unknown == "legacy_hash":
        path = goals.root() / f"{gid}.json"
        current = json.loads(path.read_text())
        current["waiting"].pop("source_sha256")
        path.write_text(json.dumps(current))
    else:
        original = Path.open

        def deny(path, *a, **kw):
            if path == source:
                raise PermissionError("Unavailable observation")
            return original(path, *a, **kw)
        monkeypatch.setattr(Path, "open", deny)
    moment = AT - timedelta(days=1)
    watch = tick.goal_watch("alpha", {"live_runs": []}, {"goals": previous}, moment)
    assert watch["standing"] and watch["objects"]
    assert not goal_session.post_check("alpha", watch["objects"], [], "SILENT", moment)["resolved"]


def test_paused_deadline_requires_decision_but_never_resumes_product(store, monkeypatch):
    gid, source, _ = store
    goals.pause(gid, "Keep product paused", "Owner decision")
    goals.wait(gid, "External dependency", "Review the dependency", str(source),
               review_at=AT.isoformat())
    watch = tick.goal_watch("alpha", {"live_runs": []}, {}, AT)
    assert watch["standing"] and watch["objects"]
    assert not goal_session.post_check("alpha", watch["objects"], [], "SILENT", AT)["resolved"]
    goals.wait(gid, "External dependency", "Review the dependency", str(source),
               review_at=(AT + timedelta(days=1)).isoformat())
    assert goal_session.post_check("alpha", watch["objects"], [], "SILENT", AT)["resolved"]
    assert goals.load(gid)["state"] == "paused"
    assert goals.load(gid)["pause"]["reason"] == "Keep product paused"
    assert goals.load(gid)["correctives"] == []
    # Corrupt pauses and closed goals never acquire permission to wait.
    current = goals.load(gid)
    current["pause"] = {}
    assert goals.waiting_decision(current) is None
    current["state"] = "closed"
    assert goals.waiting_decision(current) is None
    monkeypatch.setattr(goals, "task_status", lambda _: "completed")
    goals.resume(gid)
    goals.close(gid, "Actual installed check", "evidence.md")
    assert not tick.goal_watch("alpha", {"live_runs": []}, {}, AT)["standing"]


@pytest.fixture
def ticking(store, tmp_path, monkeypatch):
    gid, source, returned = store
    states = tmp_path / "threads"
    states.mkdir()
    report = {"thread": "alpha", "title": "Example", "live_runs": [], "needs_attention": [],
              "repos": [], "ready_to_start": [], "decided_not_done": [], "can_pick_up": [],
              "undelivered": [], "waiting_user": [], "worktrees": [], "owners_awake": []}
    monkeypatch.setattr(tick, "STATE_DIR", states)
    monkeypatch.setattr(outbound, "LEDGER", tmp_path / "outbound.json")
    monkeypatch.setattr(tick, "build", lambda _: report)
    monkeypatch.setattr(tick, "runner_contract_alarm", lambda *a, **kw: ([], None))
    monkeypatch.setattr(tick, "codex_window", lambda: None)
    monkeypatch.setattr(tick, "persisted_process_inventory", lambda *a: {})
    monkeypatch.setattr(tick.goal_session, "watchdog", lambda *a, **kw: {"mode": "none"})
    monkeypatch.setattr(outbound, "instruction_letter", lambda *a: None)
    monkeypatch.setattr(tick, "deliver_undelivered", lambda *a: [])
    monkeypatch.setattr(tick, "prompt", lambda *a: "Existing tick decision")
    monkeypatch.setattr(startup_context, "packet", lambda *a: {})
    engine = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "SILENT", ""))
    monkeypatch.setattr(tick.subprocess, "run", engine)
    mail = mock.Mock(side_effect=AssertionError("Deadline alone must not send mail"))
    monkeypatch.setattr(tick, "send_mail", mail)
    clock = mock.Mock(wraps=datetime)
    monkeypatch.setattr(tick, "datetime", clock)
    state_path = states / "alpha.json"
    state_path.write_text(json.dumps({"snapshot": tick.snapshot(report), "goals": goals.panel("alpha")}))

    def run(at):
        clock.now.return_value = at
        with mock.patch.object(sys, "argv", ["thread_tick.py", "alpha"]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = tick.main()
        return code, json.loads(state_path.read_text())
    return run, engine, mail, report


def test_deadline_crossing_reaches_real_goal_watch_and_tick(store, ticking):
    run, engine, mail, _ = ticking
    gid, _, _ = store
    before = tree(goals.root())
    for seconds in (1200, 1):
        _, state = run(AT - timedelta(seconds=seconds))
        assert not state["check"]["woke_owner"]
    assert engine.call_count == 0
    # Exact deadline, unchanged files, no task, file, or mail news.
    _, state = run(AT)
    assert state["check"]["woke_owner"]
    assert engine.call_count == 1
    assert any("срок перепроверки" in event for event in state["last_events"])
    assert state["goals"][0]["waiting"]["review_at"] == AT.isoformat()
    assert state["goal_post_check"]["resolved"] is False
    run(AT + timedelta(seconds=1))
    assert engine.call_count == 1
    run(AT + timedelta(seconds=tick.GOAL_REMIND_SECONDS))
    assert engine.call_count == 2  # existing cadence, no immediate loop
    assert tree(goals.root()) == before
    assert goals.load(gid)["state"] == "active"
    mail.assert_not_called()


def test_due_wait_with_ready_bound_task_does_not_automatically_send_idle_mail(ticking):
    run, engine, mail, report = ticking
    report["ready_to_start"] = [{"id": 100, "title": "Bound task", "run": {}}]
    # Settle the unrelated queue transition before the calendar-only wake.
    run(AT - timedelta(seconds=1))
    engine.reset_mock()
    _, state = run(AT)
    assert state["check"]["woke_owner"] and engine.call_count == 1
    assert any(reason["code"] == "goal_wait" for reason in state["check"]["reasons"])
    assert state["goal_post_check"]["resolved"] is False
    mail.assert_not_called()


@pytest.mark.parametrize("hold", ["owner", "session"])
def test_held_deadline_remains_pending_until_release(ticking, monkeypatch, hold):
    run, engine, mail, report = ticking
    holding = {"to": [{"kind": "product owner", "thread": "other"}]} if hold == "owner" else {}
    monkeypatch.setattr(tick, "yielded", lambda _: holding)
    if hold == "session":
        monkeypatch.setattr(tick, "session_leads", lambda *a: (bool(holding.get("session")), []))
        holding["session"] = True
    for seconds in (0, 1):
        _, state = run(AT + timedelta(seconds=seconds))
        assert not state["check"]["woke_owner"]
        assert state["goal_reminder"] is None
    assert engine.call_count == 0
    holding.clear()
    _, state = run(AT + timedelta(seconds=2))
    assert state["check"]["woke_owner"] and engine.call_count == 1
    assert state["goal_reminder"]
    mail.assert_not_called()


@pytest.mark.parametrize("failure", ["engine", "timeout", "composer"])
def test_failed_wake_does_not_consume_deadline(ticking, failure):
    run, engine, mail, _ = ticking
    if failure == "engine":
        engine.return_value = subprocess.CompletedProcess([], 1, "", "Engine failed")
    elif failure == "timeout":
        engine.side_effect = subprocess.TimeoutExpired("fake engine", 1)
    else:
        engine.return_value = subprocess.CompletedProcess([], 0, "Invalid envelope", "")
    code, state = run(AT)
    assert code == 1 and state["goal_reminder"] is None
    engine.side_effect = None
    engine.return_value = subprocess.CompletedProcess([], 0, "SILENT", "")
    _, state = run(AT + timedelta(seconds=1))
    assert state["check"]["woke_owner"] and engine.call_count == 2
    mail.assert_not_called()
