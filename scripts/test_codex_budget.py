import json
import unittest
from datetime import datetime, timezone

import codex_budget
import claude_product_owner


class CodexWeeklyBudgetTests(unittest.TestCase):
    def event(self, primary: dict, secondary: dict | None = None) -> str:
        return json.dumps({
            "timestamp": "2026-08-14T22:21:25.451Z",
            "payload": {"info": {}, "rate_limits": {
                "primary": primary,
                "secondary": secondary,
                "individual_limit": None,
            }},
        })

    def test_reads_only_the_explicit_seven_day_window(self):
        line = self.event(
            {"used_percent": 12, "window_minutes": 300, "resets_at": 1_787_196_742},
            {"used_percent": 19, "window_minutes": 10_080,
             "resets_at": 1_787_196_742},
        )
        state = codex_budget.weekly_snapshot(line, "/tmp/rollout.jsonl")
        self.assertEqual(state["remaining_percent"], 81.0)
        self.assertEqual(state["window_minutes"], 10_080)
        self.assertEqual(state["observed_at"], "2026-08-14T22:21:25.451Z")

    def test_a_five_hour_window_is_not_a_weekly_observation(self):
        line = self.event({
            "used_percent": 12, "window_minutes": 300, "resets_at": 1_787_196_742,
        })
        self.assertIsNone(codex_budget.weekly_snapshot(line, "/tmp/rollout.jsonl"))

    def test_real_rollout_shape_reaches_the_router_as_a_current_weekly_value(self):
        now = datetime.now(timezone.utc)
        line = json.dumps({
            "timestamp": now.isoformat().replace("+00:00", "Z"),
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 599_068}},
                "rate_limits": {
                    "limit_id": "codex",
                    "primary": {"used_percent": 19.0, "window_minutes": 10_080,
                                "resets_at": int(now.timestamp()) + 5 * 86_400},
                    "secondary": None,
                    "individual_limit": None,
                },
            },
        })
        snapshot = codex_budget.weekly_snapshot(
            line, "/tmp/rollout-2026-08-15.jsonl")
        self.assertEqual(claude_product_owner.codex_weekly_remaining(snapshot), 81.0)


if __name__ == "__main__":
    unittest.main()
