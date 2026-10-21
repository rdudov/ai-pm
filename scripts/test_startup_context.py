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
            with self.assertRaisesRegex(startup.InputLimitError, "Портфельный план не читается"):
                startup.packet()



class HardInputBounds(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plan = {"revision": 1, "accepted_at": "2026-01-01T00:00:00+00:00",
                     "now": ["first user decision", "second user decision"],
                     "paused": ["third stays paused"]}
        revisions = self.root / "plan" / "revisions"
        revisions.mkdir(parents=True)
        (revisions / "000001.json").write_text(json.dumps(self.plan))
        self.goal_path = self.root / "goals" / "1.json"
        self.goal_path.parent.mkdir()
        self.goal_path.write_text('{"id":"1","outcome":"current user words"}')
        for slug in ("demo", "other"):
            path = self.root / "products" / slug / "snapshot.md"
            path.parent.mkdir(parents=True)
            path.write_text("## Концепция\n" + slug + " exact user words  \n")
        for patch in (
            mock.patch.object(startup.product_memory, "root", return_value=self.root),
            mock.patch.object(startup.product_memory, "revisions_dir", return_value=revisions),
            mock.patch.object(startup.product_memory, "installation", return_value={
                "threads": {"demo": {"products": ["demo"]}}}),
            mock.patch.object(startup.product_goal, "root", return_value=self.root / "goals"),
        ):
            patch.start(); self.addCleanup(patch.stop)
        snapshots = {}
        for slug in ("demo", "other"):
            text = (self.root / "products" / slug / "snapshot.md").read_text()
            with mock.patch.object(startup.product_memory, "read_snapshot", return_value=text):
                snapshots[slug] = startup.snapshot_view(slug)
        self.value = {
            "contract": {}, "portfolio_plan": startup.product_memory.plan_text(self.plan),
            "product_snapshots": snapshots,
            "thread_states": {"demo": {"thread": "demo", "title": "Demo",
                                       "decision": "only-current-thread-report"}},
            "model_budgets": {"codex": None, "route": {}},
            "active_goals": [{"id": "1", "outcome": "current user words"}],
            "post_cursor_records": [],
        }

    def bounded(self):
        return startup.bounded_packet(self.value, "demo", self.plan)

    def test_omit_unrelated_inline_but_keep_all_sources_and_exact_relevant_words(self):
        result = self.bounded()
        self.assertEqual(result["portfolio_plan"], self.value["portfolio_plan"])
        self.assertEqual(result["active_goals"], self.value["active_goals"])
        views = result["product_snapshots"]
        self.assertEqual(views["demo"]["sections"]["Концепция"], "demo exact user words  ")
        self.assertTrue(views["other"]["manifested"])
        path = Path(views["other"]["source"])
        self.assertEqual(startup.digest_text(path.read_text()), views["other"]["source_sha256"])
        self.assertEqual(set(views), {"demo", "other"})
        sources = result["contract"]["sources"]
        self.assertEqual(sources["active_goals"], [str(self.goal_path)])
        self.assertIn("demo", sources["thread_states"])
        self.assertEqual(set(sources["model_budgets"]), {"codex", "route"})

    def test_growing_text_becomes_whole_openable_originals_without_mutation(self):
        huge = "полные слова пользователя 😺\n" * 60_000
        self.plan["grounds"] = [huge]
        (self.root / "plan/revisions/000001.json").write_text(json.dumps(self.plan))
        self.value["portfolio_plan"] = startup.product_memory.plan_text(self.plan)
        self.value["product_snapshots"]["demo"]["sections"]["В работе"] = huge
        self.value["active_goals"][0]["outcome"] = huge
        self.goal_path.write_text(json.dumps(self.value["active_goals"][0], ensure_ascii=False))
        before = startup.render(self.value)
        result = self.bounded()
        self.assertEqual(before, startup.render(self.value))
        self.assertLessEqual(len(startup.render(result)), startup.MAX_PACKET_CHARS)
        plan = result["portfolio_plan"]
        self.assertTrue(plan["read_before_decision"])
        self.assertEqual(plan["revision"], 1)
        self.assertEqual(json.loads(Path(plan["source"]).read_text())["now"], self.plan["now"])
        goal = result["active_goals"][0]
        raw = Path(goal["source"]).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), goal["source_sha256"])
        self.assertEqual(json.loads(raw)["outcome"], huge)
        self.assertNotIn(huge[:200], startup.render(result))

    def test_many_metadata_records_fit_or_refuse_whole_never_silently_drop(self):
        records = [{"source": f"record-{index}.md", "sha256": "a" * 64,
                    "bytes": 1_000_000, "manifested": True} for index in range(408)]
        self.value["post_cursor_records"] = records
        self.assertEqual(self.bounded()["post_cursor_records"], records)
        self.value["post_cursor_records"] = records * 100
        with self.assertRaisesRegex(startup.InputLimitError, "post_cursor_records"):
            self.bounded()
        self.value["post_cursor_records"] = []
        self.value["active_goals"] *= 10_000
        with self.assertRaisesRegex(startup.InputLimitError, "active_goals"):
            self.bounded()

    def test_char_limits_are_inclusive_and_distinct_from_utf8_bytes(self):
        text = "😺" * 100
        self.assertEqual(startup.require_size(text, 100, "test"), text)
        self.assertGreater(len(text.encode()), 100)
        with self.assertRaises(startup.InputLimitError):
            startup.require_size(text, 99, "test")
        language = startup.claude_product_owner.plain_russian.as_paragraph() + "\n\n"
        with mock.patch.object(startup, "MAX_PROMPT_CHARS", len(language) + 100):
            self.assertEqual(startup.checked_prompt(text), text)
            with self.assertRaises(startup.InputLimitError):
                startup.checked_prompt(text + "a")

    def test_final_prompt_bounds_events_reasons_letters_and_runtime_wrapper(self):
        import thread_tick
        report = self.value["thread_states"]["demo"]
        huge = "не резать эти слова" * 100_000
        text = thread_tick.prompt(report, [huge], [{"text": huge, "src": "original"}],
                                  [{"at": "2026-01-01", "event_id": huge,
                                    "subject": huge, "excerpt": huge}],
                                  startup.render(self.bounded()))
        self.assertLessEqual(len(text) + len(thread_tick.plain_russian.as_paragraph()) + 2,
                             startup.MAX_PROMPT_CHARS)
        self.assertIn("state/threads/demo.json#/check/events", text)
        self.assertIn("state/threads/demo.json#/check/reasons", text)
        self.assertIn(str(thread_tick.outbound.LEDGER), text)
        self.assertEqual(text.count("only-current-thread-report"), 1)
        with mock.patch.object(startup, "MAX_PROMPT_CHARS", 50):
            with self.assertRaises(startup.InputLimitError):
                thread_tick.prompt(report, [], [], [], startup.render(self.bounded()))

    def test_packet_limit_catches_even_contract_or_structural_growth(self):
        self.value["contract"] = {"extra": "x" * startup.MAX_PACKET_CHARS}
        with self.assertRaisesRegex(startup.InputLimitError, "startup packet"):
            self.bounded()

    def test_many_snapshot_and_thread_addresses_are_bounded_too(self):
        self.value["product_snapshots"] = {
            f"snapshot-{index}": self.value["product_snapshots"]["demo"]
            for index in range(10_000)}
        with self.assertRaisesRegex(startup.InputLimitError, "product_snapshots"):
            self.bounded()
        self.value["product_snapshots"] = {}
        self.value["thread_states"] = {f"thread-{index}": {} for index in range(10_000)}
        with self.assertRaisesRegex(startup.InputLimitError, "thread_states"):
            self.bounded()

    def test_multibyte_plan_fits_by_characters_not_utf8_bytes(self):
        text = "я" * (startup.SECTION_CHARS["portfolio_plan"] - 2)
        self.value["portfolio_plan"] = text
        self.assertEqual(self.bounded()["portfolio_plan"], text)
        self.assertGreater(len(text.encode()), startup.SECTION_CHARS["portfolio_plan"])

    def test_unreadable_required_record_is_a_refusal_not_an_empty_tail(self):
        record = self.root / "new.md"
        record.write_bytes(b"\xff")
        with self.assertRaisesRegex(startup.InputLimitError, "new.md"):
            startup.post_cursor({**self.plan, "outcome_links": []})


if __name__ == "__main__":
    unittest.main()
