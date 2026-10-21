"""Current slices stay bounded without losing unread material or historical facts."""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

import daily_standup
import product_goal as goals
import product_memory as memory
import startup_context as startup
import goal_session
import thread_tick


def files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and p.name not in {memory.LOCK, goals.LOCK}}


def test_publish_preserves_unread_across_selective_reads_and_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "ROOT", tmp_path)
    monkeypatch.setattr(memory, "installation", lambda: {})
    first = memory.publish_plan({"now": ["first"], "grounds": ["original decision"]})
    plan = json.loads(first.read_text())
    # A record arrived between reading the old prefix and publishing another plan.
    boundary = "2026-01-01T00:00:00+00:00"
    plan["read_through"] = boundary
    for slug in ("alpha", "paused"):
        record = tmp_path / "products" / slug / "history" / "new.md"
        record.parent.mkdir(parents=True)
        record.write_text(slug)
    before = first.read_bytes()
    second = memory.publish_plan(plan, expect_revision=1)
    reloaded = json.loads(second.read_text())
    assert reloaded["grounds"] == []
    assert reloaded["read_through"] == boundary
    assert first.read_bytes() == before
    selected = startup.post_cursor(reloaded, products={"alpha"})
    assert len(selected) == 1 and "alpha" in selected[0]["source"]
    # Reading a source/manifest is not an acknowledgement, including after restart.
    Path(selected[0]["source"]).read_text()
    assert startup.post_cursor(json.loads(second.read_text())) == startup.post_cursor(reloaded)
    assert len(startup.post_cursor(reloaded)) == 2


def test_all_current_writers_refuse_overflow_before_history_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "ROOT", tmp_path)
    monkeypatch.setenv("PRODUCT_OWNER_GOALS", str(tmp_path / "goals"))
    memory.start_product("alpha")
    memory.publish_plan({"now": ["keep"]})
    goal = goals.open_goal("alpha", "keep result", ["real result"], 100)
    before = files(tmp_path)
    operations = [
        lambda: memory.append_work_line("alpha", "я" * 4_001),
        lambda: memory.replace_snapshot("alpha", "я" * 4_001, "irrelevant"),
        lambda: memory.publish_plan({"now": ["я" * 6_001]}),
        lambda: goals.set_fields(goal["id"], gap="я" * 3_001),
    ]
    for operation in operations:
        with pytest.raises((memory.ContentError, goals.GoalError), match="символов"):
            operation()
        assert files(tmp_path) == before


def test_removing_a_plan_result_preserves_its_unread_task_scope(tmp_path, monkeypatch):
    content = tmp_path / "content"
    repo = tmp_path / "repo"
    monkeypatch.setattr(memory, "ROOT", content)
    monkeypatch.setattr(memory, "installation", lambda: {"tasks_repo": str(repo)})
    memory.publish_plan({"now": ["an old result"], "outcome_links": [
        {"now": 1, "next": [], "tasks": [101]}]})
    artifact = repo / "tasks" / "101-example" / "status.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"state":"blocked"}')
    memory.publish_plan({"now": ["a different result"], "outcome_links": [],
                         "read_through": "2026-01-01T00:00:00+00:00"}, expect_revision=1)
    current = memory.current_plan()
    assert current["unread_tasks"] == [101]
    assert str(artifact) in {row["source"] for row in startup.post_cursor(current)}
    # Only an explicit acknowledgement of the whole prefix releases that scope.
    memory.publish_plan({"read_through": datetime.now(timezone.utc).isoformat()}, expect_revision=2)
    assert memory.current_plan()["unread_tasks"] == []


def test_read_prefix_keeps_newer_records_in_former_tasks(tmp_path, monkeypatch):
    content, repo = tmp_path / "content", tmp_path / "repo"
    monkeypatch.setattr(memory, "ROOT", content)
    monkeypatch.setattr(memory, "installation", lambda: {"tasks_repo": str(repo)})
    boundary = "2026-01-01T00:00:00+00:00"
    prefix = "2026-01-02T00:00:00+00:00"
    memory.publish_plan({"read_through": boundary, "now": ["old result"],
                         "outcome_links": [{"now": 1, "next": [], "tasks": [101, 102]}]})
    paths = {}
    for number, filename, stamp in ((101, "task.md", prefix),
                                     (101, "status.json", "2026-01-03T00:00:00+00:00"),
                                     (102, "task.md", prefix)):
        path = repo / "tasks" / f"{number}-example" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(filename)
        timestamp = datetime.fromisoformat(stamp).timestamp()
        os.utime(path, (timestamp, timestamp))
        paths[number, filename] = path
    memory.publish_plan({"now": ["different result"]}, expect_revision=1)
    old = memory.current_plan()
    assert old["unread_tasks"] == [101, 102]
    old_bytes = (content / "plan/revisions/000002.json").read_bytes()
    memory.publish_plan({"read_through": prefix}, expect_revision=2)
    current = json.loads((content / "plan/revisions/000003.json").read_text())
    assert current["unread_tasks"] == [101]
    visible = {row["source"] for row in startup.post_cursor(current)}
    assert visible == {str(paths[101, "status.json"])}
    memory.publish_plan({"now": ["selective event read"]}, expect_revision=3)
    assert startup.post_cursor(memory.current_plan()) == startup.post_cursor(current)
    assert (content / "plan/revisions/000002.json").read_bytes() == old_bytes
    memory.publish_plan({"read_through": "2026-01-04T00:00:00+00:00"}, expect_revision=4)
    assert memory.current_plan()["unread_tasks"] == []


def test_snapshot_archive_is_exact_and_duplicate_work_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "ROOT", tmp_path)
    path = memory.start_product("alpha")
    old = path.read_bytes()
    memory.append_work_line("alpha", "an open promise")
    archive = list((path.parent / "history" / "current-state").glob("*.md"))
    assert len(archive) == 1 and archive[0].read_bytes() == old
    before = files(tmp_path)
    memory.append_work_line("alpha", "an open promise")
    assert files(tmp_path) == before
    with pytest.raises(memory.ContentError, match="изменился"):
        memory.replace_snapshot("alpha", memory.snapshot_template("alpha"),
                                hashlib.sha256(old).hexdigest())
    assert files(tmp_path) == before


def test_retired_detail_and_signal_dedup_survive_compaction(tmp_path, monkeypatch):
    monkeypatch.setenv("PRODUCT_OWNER_GOALS", str(tmp_path / "goals"))
    goal = goals.open_goal("alpha", "result", ["live result"], 100)
    goals.pause(goal["id"], "repair", "actual evidence")
    goals.add_corrective(goal["id"], 101, "effect", "return test")
    with mock.patch.object(goals, "task_status", return_value="cancelled"):
        goals.retire_corrective(goal["id"], 101, "replaced by another repair", "original decision")
    current = goals.load(goal["id"])
    archived = goals.corrective_history(goal["id"], 101, 1)
    assert json.loads(archived.read_text())["retired"]["reason"] == "replaced by another repair"
    assert goals.projection(current)["correctives"][0]["settled"]["kind"] == "retired"
    with pytest.raises(goals.GoalError, match="уже записана"):
        goals.add_corrective(goal["id"], 101, "effect", "test")
    before = files(tmp_path)
    goals.add_signal(goal["id"], "corrective_task", "same event", f"запись цели {goal['id']} в {goals.root()}")
    assert files(tmp_path) == before


def test_rogue_heading_cannot_hide_a_current_promise_and_zero_ideas_are_valid():
    import process_map_state
    text = "## В работе\n- one\n## <id>\n- two\n## Открытые вопросы\n- question\n"
    assert "- two" in startup.exact_section(text, "В работе")
    assert memory.section(text, "В работе") == ["one", "two"]
    assert process_map_state.markdown_section(text, "в работе") == ["one", "two"]
    value = {"intro": "Morning", "plans": [], "questions": [], "initiatives": []}
    assert daily_standup.parse_composition(json.dumps(value)) == value
    assert "пустой список" in daily_standup.prompt({}, "2026-01-01")


def test_event_base_counts_actual_loaded_rules_and_language_as_characters(tmp_path, monkeypatch):
    rules = "я" * 1_000
    (tmp_path / "AGENTS.md").write_text(rules)
    monkeypatch.setattr(startup.claude_product_owner, "HOME", tmp_path)
    base = "common instruction"
    size = len(rules + startup.claude_product_owner.plain_russian.as_paragraph() + "\n\n" + base)
    monkeypatch.setattr(startup, "MAX_EVENT_BASE_CHARS", size)
    assert startup.checked_prompt("event payload", event_base=base) == "event payload"
    monkeypatch.setattr(startup, "MAX_EVENT_BASE_CHARS", size - 1)
    with pytest.raises(startup.InputLimitError, match="loaded AGENTS"):
        startup.checked_prompt("event payload", event_base=base)


def test_paused_wait_preserves_pause_promises_and_postcheck_across_ticks(tmp_path, monkeypatch):
    monkeypatch.setenv("PRODUCT_OWNER_GOALS", str(tmp_path / "goals"))
    goal = goals.open_goal("alpha", "An installed result", ["A real user path"], 100)
    goals.pause(goal["id"], "Owner has paused the main work.", "Owner decision")
    goals.add_corrective(goal["id"], 101, "Repair still owed", "Installed verification")
    monkeypatch.setattr(goals, "task_status", lambda _: "blocked")
    before = goals.load(goal["id"])
    projected = goals.projection(before)
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # A pause or next-transition plan alone is never an implicit wait.
    assert not goal_session.post_check("alpha", [projected], [], "SILENT", moment)["resolved"]
    goals.wait(goal["id"], "The required external input is absent.",
               "Check the input on arrival; keep the main work paused.", "Observed absence")
    current = goals.load(goal["id"])
    for key in before.keys() - {"waiting", "next_transition", "updated_at"}:
        assert current[key] == before[key]
    assert current["state"] == goals.PAUSED
    assert goals.pending(current) == [101]
    saved = files(tmp_path)
    report = {"live_runs": [], "continuation_holds": []}
    stored = {}
    for index in range(3):
        observed_at = moment + timedelta(minutes=20 * index)
        # Ordinary tick's real wait reader and post-check, no model/transport.
        watch = thread_tick.goal_watch("alpha", report, stored, observed_at)
        assert watch["panel"][0]["waiting"]["reason"] == current["waiting"]["reason"]
        checked = goal_session.post_check("alpha", watch["objects"], [], "SILENT", observed_at)
        assert checked["resolved"] and checked["waiting"][0]["id"] == goal["id"]
        goals.wait(goal["id"], current["waiting"]["reason"], current["next_transition"],
                   current["waiting"]["src"])
        assert files(tmp_path) == saved
        stored = {"goals": watch["panel"], "goal_reminder": watch["reminder"]}
    # Changing the current plan withdraws wait but never resumes a pause.
    goals.set_fields(goal["id"], gap="A new input requires a fresh decision.")
    assert goals.load(goal["id"])["pause"] == before["pause"]
    assert not goal_session.post_check("alpha", [projected], [], "SILENT", moment)["resolved"]


def test_paused_wait_overflow_refuses_before_any_history_or_goal_write(tmp_path, monkeypatch):
    monkeypatch.setenv("PRODUCT_OWNER_GOALS", str(tmp_path / "goals"))
    goal = goals.open_goal("alpha", "Keep the promise", ["Installed acceptance"], 100)
    goals.pause(goal["id"], "Owner pause", "Owner decision")
    goals.add_corrective(goal["id"], 101, "Unsettled repair", "A real check")
    before = files(tmp_path)
    with pytest.raises(goals.GoalError, match="3000"):
        goals.wait(goal["id"], "я" * 3000, "Wait without resuming.", "Observation")
    assert files(tmp_path) == before
    assert goals.pending(goals.load(goal["id"])) == [101]


def test_event_prompt_separates_shared_base_from_event_sources(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text("rules")
    monkeypatch.setattr(startup.claude_product_owner, "HOME", tmp_path)
    captured = {}
    original = startup.checked_prompt
    def check(text, event_base=None):
        captured.update(text=text, base=event_base)
        return original(text, event_base)
    monkeypatch.setattr(startup, "checked_prompt", check)
    payload = json.dumps({"source": "event-address", "data": "x" * 20000})
    prompt = thread_tick.prompt({"thread": "alpha", "title": "Alpha"},
                                ["the actual event"], [], [], payload)
    assert "event-address" in prompt and "the actual event" in prompt
    assert "event-address" not in captured["base"]
    assert "the actual event" not in captured["base"]
    assert len(captured["base"]) < 8000
    monkeypatch.setattr(startup, "MAX_PROMPT_CHARS", len(prompt) - 1)
    with pytest.raises(startup.InputLimitError, match="complete prompt"):
        thread_tick.prompt({"thread": "alpha", "title": "Alpha"}, [], [], [], payload)
