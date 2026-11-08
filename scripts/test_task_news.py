"""Real event/input/checkpoint path; model and mail edges are isolated fakes."""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
from unittest import mock

import pytest

import outbound
import product_memory as memory
import startup_context
import task_news
import thread_tick as tick

# The public API must come from the engine version being tested/installed.
import task_engine
import task_runner
import review_admission


@pytest.fixture
def running(tmp_path, monkeypatch):
    assert hasattr(task_engine, "facts"), "test with the matching public task-engine"
    root = tmp_path / "repo"
    home = tmp_path / "home"
    states = home / "state/threads"
    states.mkdir(parents=True)
    tasks = []
    for n in range(101, 116):
        path = root / "tasks" / f"{n}-work"
        (path / ".runner").mkdir(parents=True)
        tasks.append({"id": n, "path": str(path.relative_to(root))})
    report = {"thread": "alpha", "title": "Example", "live_runs": [],
              "needs_attention": [{"id": row["id"], "status": "blocked", "run": {}}
                                  for row in tasks[:12]],
              "repos": [], "ready_to_start": [], "decided_not_done": [], "can_pick_up": [],
              "undelivered": [], "waiting_user": [], "worktrees": [], "owners_awake": [],
              "task_catalog": tasks}
    monkeypatch.setattr(memory, "HOME", home)
    monkeypatch.setattr(memory, "tasks_repo", lambda: root)
    monkeypatch.setattr(memory, "installation", lambda: {"threads": {"alpha": {}}})
    monkeypatch.setattr(tick, "STATE_DIR", states)
    monkeypatch.setattr(outbound, "LEDGER", tmp_path / "outbound.json")
    monkeypatch.setattr(tick, "build", lambda _: report)
    monkeypatch.setattr(tick, "runner_contract_alarm", lambda *a, **kw: ([], None))
    monkeypatch.setattr(tick, "codex_window", lambda: None)
    monkeypatch.setattr(tick, "persisted_process_inventory", lambda *a: {})
    monkeypatch.setattr(tick.goal_session, "watchdog", lambda *a, **kw: {"mode": "none"})
    monkeypatch.setattr(tick, "goal_watch", lambda *a: {
        "transitions": [], "standing": [], "panel": [], "objects": [], "reminder": None})
    monkeypatch.setattr(outbound, "instruction_letter", lambda *a: None)
    monkeypatch.setattr(tick, "deliver_undelivered", lambda *a: [])
    monkeypatch.setattr(startup_context, "packet", lambda *a: {})
    engine = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "SILENT", ""))
    monkeypatch.setattr(tick.subprocess, "run", engine)
    mail = mock.Mock(side_effect=AssertionError("no unsolicited delivery"))
    monkeypatch.setattr(tick, "send_mail", mail)
    state_path = states / "alpha.json"
    state_path.write_text(json.dumps({"snapshot": tick.snapshot(report)}))

    def run():
        with mock.patch.object(sys, "argv", ["thread_tick.py", "alpha"]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = tick.main()
        try:
            state = json.loads(state_path.read_text())
        except json.JSONDecodeError:
            state = None
        return result, state

    return run, engine, report, root, state_path


def launch(root, number=115, name="short", *, finish=True):
    path = root / "tasks" / f"{number}-work"
    task_runner.write_json(path / ".runner/runner.json", {
        "task_dir": str(path), "run_id": name, "runner": "codex", "workflow": "standard",
        "started_at": "2030-01-01T00:00:01Z", "child_started_at": "2030-01-01T00:00:02Z"})
    if finish:
        task_runner.finish_runner_meta(path, {"finished_at": "2030-01-01T00:00:05Z",
                                             "outcome": "completed"})
    return path


def record_outcome(state_path, *, kind="impediment", source=None, first_only=False):
    state = json.loads(state_path.read_text())
    item = state["task_news"]["tasks"]["115"]
    source = source or state_path.parent / "checked-dependency.json"
    source.write_text('{"bound_reviewer":"unavailable"}')
    for event in item["events"][:1] if first_only else item["events"]:
        with mock.patch.object(sys, "argv", ["task_news.py", "alpha", "--event",
                task_news.event_key("115", event), "--kind", kind, "--reason",
                "Bound reviewer cannot start", "--source", str(source), "--next",
                "Retry after reviewer is available"]):
            assert task_news.main() == 0


def test_short_run_reaches_actual_prompt_beyond_attention_cap_and_requires_outcome(running):
    run, engine, report, root, state_path = running
    assert run()[0] == 0
    assert engine.call_count == 0  # legacy direction first enablement is quiet
    launch(root)
    # No process was live in either report, and blocked/stuck never changes.
    code, state = run()
    assert code == 1 and state["check"]["woke_owner"]
    assert any("задачи 115" in event for event in state["check"]["events"])
    real_input = engine.call_args.kwargs["input"]
    assert "terminal-runs.jsonl#bytes=" in real_input
    assert "task:115:runs:short" in real_input
    assert "scripts/task_news.py alpha" in real_input
    assert state["task_news"]["post_check"]["unresolved"] == ["task:115:runs:short"]
    assert report["live_runs"] == [] and len(report["needs_attention"]) == 12
    assert run()[1]["check"]["woke_owner"]  # bare SILENT did not eat the event
    engine.side_effect = lambda *a, **kw: (record_outcome(state_path) or
                                          subprocess.CompletedProcess([], 0, "SILENT", ""))
    code, state = run()
    assert code == 0 and state["task_news"]["handled"][0]["outcome"]["kind"] == "impediment"
    before = engine.call_count
    assert run()[0] == 0
    assert engine.call_count == before
    assert json.loads(state_path.read_text())["task_news"]["tasks"]["115"]["last_outcome"]["outcome"]["kind"] == "impediment"


@pytest.mark.parametrize("decision", ["rework", "approved", "refused"])
def test_each_new_review_of_unchanged_task_is_separate_news(running, decision):
    run, engine, _, root, state_path = running
    run()
    path = root / "tasks/115-work"

    def review(identifier):
        if decision == "refused":
            review_admission.record_infrastructure_obligation(
                path, event_id=identifier, source="dev-pipeline:review_refused",
                reason="Reviewer unavailable", allocate=False)
        else:
            review_admission.record_review_round(path, event_id=identifier,
                decision={"decision": decision, "findings": ["fix"] if decision == "rework" else []})

    engine.side_effect = lambda *a, **kw: (record_outcome(state_path) or
                                          subprocess.CompletedProcess([], 0, "SILENT", ""))
    review("review-1")
    assert run()[0] == 0
    before = engine.call_count
    run()
    assert engine.call_count == before
    review("review-2")
    assert run()[0] == 0 and engine.call_count == before + 1
    assert "review-2" in engine.call_args.kwargs["input"]


@pytest.mark.parametrize("failure", ["owner", "session", "engine", "timeout", "composer"])
def test_held_and_failed_wakes_preserve_news(running, monkeypatch, failure):
    run, engine, _, root, state_path = running
    run()
    launch(root)
    if failure == "owner":
        monkeypatch.setattr(tick, "yielded", lambda _: {"to": [{"kind": "product owner", "thread": "other"}]})
    elif failure == "session":
        monkeypatch.setattr(tick, "session_leads", lambda *a: (True, []))
    elif failure == "engine":
        engine.return_value = subprocess.CompletedProcess([], 1, "", "unavailable")
    elif failure == "timeout":
        engine.side_effect = subprocess.TimeoutExpired("engine", 1)
    else:
        engine.return_value = subprocess.CompletedProcess([], 0, "bad response", "")
    _, state = run()
    assert state["task_news"]["tasks"]["115"]["cursor"]["runs"]["offset"] == 0
    monkeypatch.setattr(tick, "yielded", lambda _: None)
    monkeypatch.setattr(tick, "session_leads", lambda *a: (False, []))
    engine.side_effect = lambda *a, **kw: (record_outcome(state_path) or
                                          subprocess.CompletedProcess([], 0, "SILENT", ""))
    assert run()[0] == 0
    assert "task:115:runs:short" in engine.call_args.kwargs["input"]


def test_new_short_action_is_observed_even_if_it_also_finishes_between_snapshots(running):
    run, engine, _, root, _ = running
    run()
    launch(root)
    engine.side_effect = lambda *a, **kw: (launch(root, name="rework") and
                                          subprocess.CompletedProcess([], 0, "SILENT", ""))
    code, state = run()
    assert code == 0 and state["task_news"]["handled"][0]["outcome"]["kind"] == "run_started"
    # Only the original event was acknowledged. The action's end is fresh news.
    engine.side_effect = None
    code, state = run()
    assert code == 1 and "rework" in engine.call_args.kwargs["input"]


def test_changed_proof_refuses_acknowledgement(running):
    run, engine, _, root, state_path = running
    run()
    launch(root)
    source = state_path.parent / "proof.json"

    def answer(*args, **kwargs):
        record_outcome(state_path, source=source)
        source.write_text("changed after checking")
        return subprocess.CompletedProcess([], 0, "SILENT", "")

    engine.side_effect = answer
    assert run()[0] == 1
    assert json.loads(state_path.read_text())["task_news"]["post_check"]["resolved"] is False


def test_partial_handling_does_not_replay_an_already_handled_fact(running):
    run, engine, _, root, state_path = running
    run()
    path = launch(root)
    review_admission.record_review_round(path, event_id="review", decision={"decision": "approved"})
    engine.side_effect = lambda *a, **kw: (record_outcome(state_path, first_only=True) or
                                          subprocess.CompletedProcess([], 0, "SILENT", ""))
    assert run()[0] == 1
    assert run()[0] == 0
    assert "task:115:runs:short" not in engine.call_args.kwargs["input"]
    assert "task:115:reviews:review" in engine.call_args.kwargs["input"]
    before = engine.call_count
    assert run()[0] == 0 and engine.call_count == before


def test_checked_acceptance_is_recorded_without_requiring_mail(running):
    run, engine, _, root, state_path = running
    run()
    launch(root)
    engine.side_effect = lambda *a, **kw: (record_outcome(state_path, kind="acceptance") or
                                          subprocess.CompletedProcess([], 0, "SILENT", ""))
    code, state = run()
    assert code == 0 and state["task_news"]["handled"][0]["outcome"]["kind"] == "acceptance"


def test_unreadable_checkpoint_refuses_instead_of_rebaselining(running):
    run, engine, _, root, state_path = running
    run()
    launch(root)
    state_path.write_text("{truncated")
    assert run()[0] == 1
    assert engine.call_count == 0
    assert state_path.read_text() == "{truncated"


def test_replaced_finished_run_still_reaches_product_without_live_edge(running):
    run, engine, report, root, _ = running
    run()
    launch(root, name="first")
    launch(root, name="second")
    assert not report["live_runs"]
    assert run()[0] == 1
    value = engine.call_args.kwargs["input"]
    assert "task:115:runs:first" in value and "task:115:runs:second" in value
