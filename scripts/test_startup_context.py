from __future__ import annotations

from datetime import datetime, timezone
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
            self.assertEqual(records[0]["text"], "new")


if __name__ == "__main__":
    unittest.main()
