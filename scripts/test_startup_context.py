from __future__ import annotations

from datetime import datetime, timezone
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import startup_context as startup


class StartupContextTests(unittest.TestCase):
    def test_snapshot_keeps_current_sections_and_manifests_effect_history(self):
        text = """# demo
## Концепция
current concept
## Пользовательские пути
current path
## Текущая ставка
current bet
## Не делаем
- no
## В работе
- task 1
## Журнал эффекта
- 2026 old delivered effect
## Открытые вопросы
- question
"""
        with mock.patch.object(startup.product_memory, "read_snapshot", return_value=text):
            view = startup.snapshot_view("demo")
        self.assertEqual(view["sections"]["Текущая ставка"], "current bet")
        self.assertNotIn("old delivered effect", json.dumps(view, ensure_ascii=False))
        effect = view["sections"]["Журнал эффекта"]
        self.assertEqual(effect["entries"], 1)
        self.assertEqual(effect["sha256"], hashlib.sha256(
            "- 2026 old delivered effect".encode()).hexdigest())

    def test_goal_keeps_decision_and_manifests_repeated_signal_prose(self):
        projected = {"id": "1", "outcome": "result", "signals": [
            {"code": "repeat", "text": "long history", "src": "evidence"}],
            "src": "content/goals/1.json"}
        with mock.patch.object(startup.product_goal, "projection", return_value=projected):
            view = startup.goal_view({})
        self.assertEqual(view["outcome"], "result")
        self.assertEqual(view["signal_summary"]["codes"], ["repeat"])
        self.assertNotIn("long history", json.dumps(view, ensure_ascii=False))

    def test_current_snapshot_formatting_is_verbatim(self):
        text = "# demo\n\n## Концепция\nfirst  \n\nsecond\n## Пользовательские пути\n"
        self.assertEqual(startup.exact_section(text, "Концепция"), "first  \n\nsecond")

    def test_post_cursor_reads_only_new_named_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old.md"; old.write_text("old")
            new = root / "new.md"; new.write_text("new")
            snap = root / "snapshot.md"; snap.write_text("snapshot")
            cursor = datetime.now(timezone.utc).timestamp() - 1
            old_stamp = cursor - 10
            __import__("os").utime(old, (old_stamp, old_stamp))
            plan = {"accepted_at": datetime.fromtimestamp(cursor, timezone.utc).isoformat(),
                    "outcome_links": []}
            with mock.patch.object(startup.product_memory, "root", return_value=root), \
                    mock.patch.object(startup.product_memory, "installation", return_value={}):
                records = startup.post_cursor(plan)
            self.assertEqual([item["source"] for item in records], [str(new)])
            self.assertTrue(records[0]["manifested"])
            self.assertEqual(records[0]["sha256"], startup.digest_text("new"))
            self.assertEqual(records[0]["bytes"], 3)
            self.assertNotIn("text", records[0])

    def test_manifest_keeps_all_unread_sources_without_moving_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            texts = {"a.md": "новое слово", "b.json": '{"decision":"keep"}',
                     "c.txt": "x" * (startup.MAX_RECORD_BYTES + 1)}
            for name, text in texts.items():
                (root / name).write_text(text, encoding="utf-8")
            cursor = datetime.now(timezone.utc).timestamp() - 1
            plan = {"accepted_at": datetime.fromtimestamp(cursor, timezone.utc).isoformat(),
                    "outcome_links": []}
            original = json.dumps(plan, sort_keys=True)
            with mock.patch.object(startup.product_memory, "root", return_value=root), \
                    mock.patch.object(startup.product_memory, "installation", return_value={}):
                records = startup.post_cursor(plan)
                repeated = startup.post_cursor(plan)
            self.assertEqual(original, json.dumps(plan, sort_keys=True))
            self.assertEqual(records, repeated)
            self.assertEqual([Path(item["source"]).name for item in records], sorted(texts))
            for item in records:
                text = texts[Path(item["source"]).name]
                self.assertEqual(item["sha256"], startup.digest_text(text))
                self.assertEqual(item["bytes"], len(text.encode("utf-8")))
                self.assertTrue(item["manifested"])
                self.assertNotIn("text", item)

    def test_tick_uses_same_current_report_once_and_preserves_legacy_prompt(self):
        import thread_tick
        report = {"thread": "demo", "title": "Demo", "decision": "unique-current-decision"}
        packet = {"thread_states": {"demo": report}}
        prompt = thread_tick.prompt(report, ["an event"], [], [], startup.render(packet))
        self.assertEqual(prompt.count("unique-current-decision"), 1)
        self.assertIn("thread_states", prompt)
        self.assertIn("манифест", prompt.lower())
        with mock.patch.object(thread_tick, "plan_block", return_value="plan"):
            legacy = thread_tick.prompt(report, [], [], [], "legacy startup")
        self.assertIn(json.dumps(report, ensure_ascii=False, indent=2), legacy)

    def test_full_tick_input_reaches_codex_with_language_rules_and_sources_intact(self):
        import thread_tick
        router = startup.claude_product_owner
        report = {"thread": "demo", "title": "Demo", "decision": "current decision"}
        value = {"portfolio_plan": "plan with a pause", "product_snapshots": {"demo": "snapshot"},
                 "thread_states": {"demo": report}, "model_budgets": {"codex": None},
                 "active_goals": [{"waiting": {"reason": "external prerequisite"}}],
                 "post_cursor_records": [{"source": "unread.md", "manifested": True,
                                          "sha256": startup.digest_text("unread"), "bytes": 6}]}
        prompt = thread_tick.prompt(report, ["changed"], [], [], startup.render(value))
        observation = router.UsageObservation(
            route=router.Route("codex", router.CODEX_MODEL, "test"), usage=None,
            codex_budget=None, codex_error=None, attempted_at="", observed_at=None,
            authorization_recovery="not_attempted", error=None)
        with mock.patch.object(router, "inspect_observation", return_value=observation), \
                mock.patch.object(router.sys, "stdin", StringIO(prompt)), \
                mock.patch.object(router.subprocess, "run", return_value=mock.Mock(
                    returncode=1, stdout="", stderr="fixture refusal")) as run, \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(router.main(["--entry", "print"]), 1)
        self.assertEqual(run.call_args.kwargs["input"], router.plain_russian.as_paragraph() + "\n\n" + prompt)
        self.assertIn(startup.render(value), run.call_args.kwargs["input"])
        self.assertEqual(run.call_args.args[0][-1], "-")

    def test_one_large_post_cursor_record_is_manifested_not_inlined(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            large = root / "large.md"; large.write_text("x" * (startup.MAX_RECORD_BYTES + 1))
            cursor = datetime.now(timezone.utc).timestamp() - 1
            plan = {"accepted_at": datetime.fromtimestamp(cursor, timezone.utc).isoformat(),
                    "outcome_links": []}
            with mock.patch.object(startup.product_memory, "root", return_value=root), \
                    mock.patch.object(startup.product_memory, "installation", return_value={}):
                records = startup.post_cursor(plan)
            self.assertTrue(records[0]["manifested"])
            self.assertEqual(records[0]["bytes"], startup.MAX_RECORD_BYTES + 1)
            self.assertNotIn("text", records[0])

    def test_unreadable_plan_becomes_explicit_safe_refusal(self):
        observation = mock.Mock(route=mock.Mock(engine="claude", model="opus", reason="test"),
                                usage={}, codex_budget={})
        with mock.patch.object(startup.product_memory, "current_plan",
                               side_effect=startup.product_memory.ContentError("broken")), \
                mock.patch.object(startup.product_memory, "installation", return_value={}), \
                mock.patch.object(startup.product_memory, "slugs", return_value=[]), \
                mock.patch.object(startup.claude_product_owner, "inspect_observation",
                                  return_value=observation), \
                mock.patch.object(startup.codex_budget, "latest", return_value={}), \
                mock.patch.object(startup.product_goal, "active", return_value=[]):
            value = startup.packet()
        self.assertIn("не запускай работу", value["portfolio_plan"])
        self.assertEqual(value["post_cursor_records"], [])


if __name__ == "__main__":
    unittest.main()
