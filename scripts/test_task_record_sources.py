"""Fixture workspaces are not required reports; genuine recursive evidence is."""

from datetime import datetime, timezone
import copy
import os
from pathlib import Path

import pytest

import product_memory as memory
import startup_context as startup


BOUNDARY = "2026-10-26T12:04:54.435431+00:00"
STAMP = datetime.fromisoformat(BOUNDARY).timestamp()


@pytest.fixture
def sources(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    content = tmp_path / "content"
    content.mkdir()
    monkeypatch.setattr(memory, "ROOT", content)
    monkeypatch.setattr(memory, "installation", lambda: {"tasks_repo": str(repo)})
    return repo / "tasks"


def record(tasks, relative, body=b"genuine evidence", stamp=STAMP + 1):
    path = tasks / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    os.utime(path, (stamp, stamp))
    return path


def test_task_records_keep_real_nested_and_unknown_directories(sources):
    expected = set()
    for name in memory.TASK_RECORD_NAMES:
        for parent in ("", "evidence/real", "evidence/unclassified",
                       "test-runtime-notes", "evidence/test-runtime.txt"):
            expected.add(record(sources, f"101-real/{parent}/{name}"))
        for parent in ("test-runtime/case", "evidence/test-runtime/case", ".runner"):
            record(sources, f"101-real/{parent}/{name}", body=b"\xfffixture")
    record(sources, "102-other/findings.md")
    record(sources, "101-real/evidence/unknown.bin")
    assert set(memory.task_records(101)) == expected


@pytest.mark.parametrize("fixture_parent", ["test-runtime", "evidence/test-runtime"])
@pytest.mark.parametrize("task_scope", ["outcome_links", "unread_tasks"])
def test_post_cursor_excludes_non_utf8_fixtures_and_preserves_source_identity(
        sources, fixture_parent, task_scope):
    for name in memory.TASK_RECORD_NAMES:
        record(sources, f"101-real/{fixture_parent}/case/{name}", b"\xfffixture")
    expected = [record(sources, f"101-real/{name}", "текст отчёта".encode())
                for name in ("task.md", "evidence/nested/findings.md",
                             "deliverables/manifest.json", "evidence/unclassified/status.json")]
    record(sources, "101-real/old/verification.md", stamp=STAMP)
    record(sources, "101-real/future/progress.json", stamp=STAMP + 3)
    plan = {"accepted_at": BOUNDARY, "read_through": BOUNDARY, task_scope:
            [101] if task_scope == "unread_tasks" else [{"tasks": [101]}]}
    before = copy.deepcopy(plan)
    ceiling = datetime.fromtimestamp(STAMP + 2, timezone.utc)
    records = startup.post_cursor(plan, frozen_at=ceiling)
    assert plan == before
    assert {r["source"] for r in records} == {str(p) for p in expected}
    for row in records:
        raw = Path(row["source"]).read_bytes()
        assert row["sha256"] == startup.digest_text(raw.decode())
        assert row["bytes"] == len(raw)
        assert row["chars"] == len(raw.decode())
        assert row["manifested"] is True
    assert startup.MAX_PACKET_CHARS == 480_000
    assert startup.MAX_PROMPT_CHARS == 600_000


@pytest.mark.parametrize("relative", ["findings.md", "evidence/nested/findings.md",
                                     "evidence/unclassified/manifest.json"])
def test_post_cursor_still_refuses_non_utf8_required_report(sources, relative):
    required = record(sources, f"101-real/{relative}", b"\xfffixture")
    record(sources, "101-real/test-runtime/case/findings.md", b"\xfffixture")
    plan = {"accepted_at": BOUNDARY, "read_through": BOUNDARY, "unread_tasks": [101]}
    with pytest.raises(startup.InputLimitError) as error:
        startup.post_cursor(plan)
    assert str(required) in str(error.value)
    assert "не прочитан обязательный источник" in str(error.value)
    assert plan["read_through"] == BOUNDARY


def test_plan_retention_uses_only_real_unread_records(sources):
    record(sources, "101-real/findings.md", stamp=STAMP - 1)
    record(sources, "101-real/evidence/test-runtime/case/findings.md", b"\xfffixture")
    fresh = record(sources, "102-real/evidence/nested/findings.md")
    record(sources, "102-real/test-runtime/case/findings.md", b"\xfffixture")
    memory.publish_plan({"read_through": "2026-10-25T00:00:00+00:00",
                         "now": ["previous result"], "outcome_links": [
                             {"now": 1, "next": [], "tasks": [101, 102]}]})
    memory.publish_plan({"read_through": BOUNDARY}, expect_revision=1)
    plan = memory.current_plan()
    assert plan["unread_tasks"] == [102]
    assert plan["read_through"] == BOUNDARY
    assert [row["source"] for row in startup.post_cursor(plan)] == [str(fresh)]
