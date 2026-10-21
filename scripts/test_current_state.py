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
    source = tmp_path / "observation.md"
    source.write_text("The required external input is absent.")
    goals.wait(goal["id"], "The required external input is absent.",
               "Check the input on arrival; keep the main work paused.", str(source))
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
        assert watch["objects"] == [] and watch["standing"] == []
        checked = goal_session.post_check("alpha", [projected], [], "SILENT", observed_at)
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


def test_event_carries_current_wait_and_changed_return_without_history_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "ROOT", tmp_path / "content")
    monkeypatch.setenv("PRODUCT_OWNER_GOALS", str(memory.root() / "goals"))
    monkeypatch.setattr(memory, "installation", lambda: {
        "threads": {"alpha": {"products": ["alpha"]}, "other": {"products": ["other"]}}})
    for slug in ("alpha", "other"):
        memory.start_product(slug)
    memory.publish_plan({"now": ["priority"], "next": ["afterwards"],
                         "parallel": ["separate tree"], "paused": ["owner pause"],
                         "contradictions": ["unresolved owner question"]})
    goal = goals.open_goal("alpha", "A promised result", ["Real acceptance"], 100)
    goals.pause(goal["id"], "Owner pause stays in force", "owner.md")
    goals.add_corrective(goal["id"], 101, "Still owed", "Installed result")
    evidence, returned = tmp_path / "evidence.md", tmp_path / "return.md"
    evidence.write_text("Observed external wait")
    goals.wait(goal["id"], "The external answer has not arrived", "Read the answer on arrival",
               str(evidence), watch_paths=[str(returned)])
    unrelated = goals.open_goal("other", "Another product", ["Other acceptance"], 200)
    report = {"thread": "alpha", "title": "Alpha", "live_runs": [],
              "worktrees": ["occupied"],
              "owners_awake": [{"worktrees": ["occupied"], "src": "live observer"},
                               {"worktrees": ["unrelated"], "src": "other tree"},
                               {"pid": os.getpid(), "worktrees": ["occupied"]}]}
    monkeypatch.setattr(startup.claude_product_owner, "inspect_observation", lambda *_: mock.Mock(
        route=mock.Mock(engine="codex", model="test", reason="fixture")))
    monkeypatch.setattr(startup.codex_budget, "latest", lambda: None)
    monkeypatch.setattr(startup, "post_cursor", lambda *_a, **_k: pytest.fail("Unrequested history scan"))
    before = files(memory.root())
    for _ in range(3):
        packet = startup.packet(("alpha", report))
        assert packet["thread_states"] == {"alpha": {
            **report, "owners_awake": report["owners_awake"][:1]}}
        assert len(report["owners_awake"]) == 3  # the actual observation stays intact
        assert set(packet["product_snapshots"]) == {"alpha"}
        assert packet["product_snapshots"]["alpha"]["sections"] == startup.snapshot_view("alpha")["sections"]
        assert packet["portfolio_plan"]["current"] == memory.plan_text(memory.current_plan())
        assert [item["id"] for item in packet["active_goals"]] == [goal["id"]]
        current = packet["active_goals"][0]
        original = goals.load(goal["id"])
        assert current["state"] == goals.PAUSED and current["pause"] == original["pause"]
        assert current["pending"] == [101] and current["main_task"] == 100
        assert current["next_transition"] == original["next_transition"]
        assert current["waiting"]["reason"] == original["waiting"]["reason"]
        assert current["waiting"]["watch_paths"] == [str(returned)]
        assert current["waiting"]["source_sha256"] == current["waiting"]["current_source_sha256"]
        source = packet["contract"]["other_sources"]["goals"].replace("<id>", current["id"])
        assert json.loads(Path(source).read_text()) == original
        assert unrelated["id"] not in [row["id"] for row in packet["active_goals"]]
    assert files(memory.root()) == before
    returned.write_text("A new actual return")
    changed = startup.packet(("alpha", report))["active_goals"][0]["waiting"]
    assert changed["current_source_sha256"] != changed["source_sha256"]
    assert files(memory.root()) == before


def test_event_without_explicit_wait_keeps_gap_and_unsettled_promises(tmp_path, monkeypatch):
    monkeypatch.setenv("PRODUCT_OWNER_GOALS", str(tmp_path / "goals"))
    goal = goals.open_goal("alpha", "Promised result", ["Real acceptance"], 100)
    goals.pause(goal["id"], "Explicit owner pause", "owner.md")
    goals.add_corrective(goal["id"], 101, "Unsettled repair", "Real return")
    goal = goals.set_fields(goal["id"], gap="No result yet", next_transition="Choose a safe step")
    current = startup.event_goal_view(goal)
    assert current["waiting"] is None
    assert current["gap"] == goal["gap"] and current["next_transition"] == goal["next_transition"]
    assert current["pending"] == [101]
    event = goals.standing("alpha", [])[0]
    assert goal["id"] in event and goal["outcome"] in event
    assert goal["gap"] not in event


@pytest.mark.parametrize("anchor", ["", "#цель-0002", "#цель-0030", "#цель-0044", "#цель-0047"])
@pytest.mark.parametrize("relative", [False, True])
def test_wait_change_restores_standing_and_requires_a_fresh_owner_decision(tmp_path, monkeypatch, anchor, relative):
    monkeypatch.setenv("PRODUCT_OWNER_GOALS", str(tmp_path / "goals"))
    monkeypatch.setattr(memory, "ROOT", tmp_path / "content")
    monkeypatch.setattr(memory, "HOME", tmp_path / "unrelated-home")
    source = memory.root() / "products/alpha/history/decision.md"
    source.parent.mkdir(parents=True)
    source.write_text("old")
    goal = goals.open_goal("alpha", "Installed result", ["Real acceptance"], 100)
    goals.pause(goal["id"], "Owner pause", "owner.md")
    goals.add_corrective(goal["id"], 101, "Unsettled promise", "Installed result")
    src = ("content/" + str(source.relative_to(memory.root())) if relative else str(source)) + anchor
    goals.wait(goal["id"], "External return is pending", "Verify new bytes", src)
    report = {"live_runs": []}
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = thread_tick.goal_watch("alpha", report, {}, moment)
    assert first["standing"] == first["objects"] == []
    before = files(tmp_path)
    for minutes in (20, 40, 61):
        repeated = thread_tick.goal_watch("alpha", report, {"goals": first["panel"]},
                                          moment + timedelta(minutes=minutes))
        assert repeated["standing"] == repeated["objects"] == repeated["transitions"] == []
        current = goals.load(goal["id"])
        goals.wait(goal["id"], current["waiting"]["reason"], current["next_transition"], src)
    assert files(tmp_path) == before
    stored = {"goals": first["panel"]}
    source.write_text("new")
    changed = thread_tick.goal_watch("alpha", report, stored, moment)
    assert changed["standing"] and changed["objects"]
    assert any("изменился источник" in event for event in changed["transitions"])
    assert not goal_session.post_check("alpha", changed["objects"], [], "SILENT", moment)["resolved"]
    current = goals.load(goal["id"])
    assert current["state"] == goals.PAUSED and current["pause"]["reason"] == "Owner pause"
    assert goals.pending(current) == [101]
    goals.wait(goal["id"], current["waiting"]["reason"], current["next_transition"], src)
    assert goal_session.post_check("alpha", changed["objects"], [], "SILENT", moment)["resolved"]


@pytest.mark.parametrize("unavailable", ["missing", "unreadable", "unreadable_watch", "legacy"])
def test_unknown_wait_keeps_owner_decision_and_requires_outcome(tmp_path, monkeypatch, unavailable):
    monkeypatch.setenv("PRODUCT_OWNER_GOALS", str(tmp_path / "goals"))
    source, returned = tmp_path / "decision.md", tmp_path / "return.txt"
    source.write_text("Owner observation")
    returned.write_text("Return")
    src = str(source) + "#цель-0002"
    goal = goals.open_goal("alpha", "Installed result", ["Acceptance"], 100)
    goals.pause(goal["id"], "Keep owner pause", "owner.md")
    goals.add_corrective(goal["id"], 101, "Keep promise", "Acceptance")
    goals.wait(goal["id"], "External obstacle", "Read actual return", src,
               watch_paths=[str(returned)] if unavailable == "unreadable_watch" else [])
    previous = goals.panel("alpha")
    if unavailable == "legacy":
        path = goals.root() / f"{goal['id']}.json"
        value = json.loads(path.read_text())
        value["waiting"].pop("source_sha256")
        path.write_text(json.dumps(value))
    elif unavailable == "missing":
        source.unlink()
    else:
        original = Path.open
        denied = returned if unavailable == "unreadable_watch" else source
        def deny(path, *args, **kwargs):
            if path == denied:
                raise PermissionError("fixture unreadable source")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "open", deny)
    before = files(goals.root())
    current = goals.load(goal["id"])
    decision = goals.waiting_decision(current)
    assert decision and decision["src"] == src
    assert decision["source_sha256" if unavailable == "legacy" else "current_source_sha256"] is None
    assert not goals.waiting_unchanged(decision)
    assert not goals.waiting_unchanged({"source_sha256": None, "current_source_sha256": None})
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
    watch = thread_tick.goal_watch("alpha", {"live_runs": []}, {"goals": previous}, moment)
    assert watch["standing"] and watch["objects"]
    if unavailable != "legacy":
        assert any("изменился источник" in event for event in watch["transitions"])
    assert not goal_session.post_check("alpha", watch["objects"], [], "SILENT", moment)["resolved"]
    monkeypatch.setattr(memory, "current_plan", lambda: {
        "outcome_links": [{"tasks": [100, 101], "goals": [goal["id"]]}]})
    monkeypatch.setattr(thread_tick, "codex_window", lambda: None)
    report = {"live_runs": [], "waiting_user": [], "can_pick_up": [{"id": 101}],
              "queued_by_plan": [], "ready_to_start": [], "decided_not_done": [],
              "needs_attention": [], "repos": [], "undelivered": []}
    assert thread_tick.idle_reasons(report, {}, watch["panel"])[0]["code"] == "none_observed"
    assert current["state"] == goals.PAUSED and goals.pending(current) == [101]
    assert current["waiting"]["reason"] == "External obstacle"
    assert current["next_transition"] == "Read actual return"
    assert files(goals.root()) == before


def test_wait_reason_covers_only_explicit_plan_bindings_and_keeps_window_checks(tmp_path, monkeypatch):
    monkeypatch.setenv("PRODUCT_OWNER_GOALS", str(tmp_path / "goals"))
    goal = goals.open_goal("alpha", "Installed result", ["Real acceptance"], 100)
    source = tmp_path / "owner.md"
    source.write_text("Agreed future installation window")
    goals.wait(goal["id"], "Agreed future installation window", "Install in the window", str(source))
    plan = {"outcome_links": [{"tasks": [100, 101], "goals": [goal["id"]]}]}
    monkeypatch.setattr(memory, "current_plan", lambda: plan)
    monkeypatch.setattr(thread_tick, "codex_window", lambda: None)
    report = {"live_runs": [], "waiting_user": [], "can_pick_up": [{"id": 101}],
              "queued_by_plan": [], "ready_to_start": [], "decided_not_done": [],
              "needs_attention": [], "repos": [], "undelivered": []}
    panel = goals.panel("alpha")
    reasons = thread_tick.idle_reasons(report, {}, panel)
    assert [reason["code"] for reason in reasons] == ["goal_wait"]
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events, reminder, _ = thread_tick.standing_events(report, thread_tick.snapshot(report), {}, moment, reasons)
    assert len(events) == 1 and "проверка условий" in events[0]
    # Reuse the existing reminder clock: no new timer, and a future window is
    # still reviewed after the interval even when its source bytes do not change.
    again = thread_tick.standing_events(report, thread_tick.snapshot(report),
                                       {"idle_reminder": reminder}, moment, reasons)[0]
    assert again == []
    later = moment + timedelta(seconds=thread_tick.IDLE_REMIND_SECONDS + 1)
    assert thread_tick.standing_events(report, thread_tick.snapshot(report),
                                      {"idle_reminder": reminder}, later, reasons)[0]
    report["can_pick_up"].append({"id": 102})
    assert thread_tick.idle_reasons(report, {}, panel)[0]["code"] == "none_observed"
    report["can_pick_up"].pop()
    # A group with another unresolved goal cannot be excused by this wait.
    plan["outcome_links"][0]["goals"].append("other")
    assert thread_tick.idle_reasons(report, {}, panel)[0]["code"] == "none_observed"


def test_busy_unrelated_run_does_not_resolve_goal_but_registered_repair_does(tmp_path, monkeypatch):
    monkeypatch.setenv("PRODUCT_OWNER_GOALS", str(tmp_path / "goals"))
    goal = goals.open_goal("alpha", "Installed result", ["Real acceptance"], 100)
    goals.pause(goal["id"], "All conditions still required", "owner.md")
    goals.add_corrective(goal["id"], 101, "Capacity", "Installed capacity")
    report = {"live_runs": [{"id": 102}]}
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
    watch = thread_tick.goal_watch("alpha", report, {}, moment)
    assert watch["objects"]
    assert not goal_session.post_check("alpha", watch["objects"], [102], "SILENT", moment)["resolved"]
    goals.add_corrective(goal["id"], 102, "Repair the same product", "Installed user path")
    watch = thread_tick.goal_watch("alpha", report, {}, moment)
    assert watch["objects"] == [] and watch["standing"] == []
    assert goals.load(goal["id"])["state"] == goals.PAUSED
    assert goals.pending(goals.load(goal["id"])) == [101, 102]
