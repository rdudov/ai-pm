import unittest
from unittest import mock

from claude_product_owner import (
    CODEX_MODEL,
    Route,
    claude_command,
    codex_command,
    inspect_live,
    model_used_percentages,
    opus_used_percentages,
    select_model,
    select_route,
    shared_limits,
)


class ProductOwnerModelRouterTests(unittest.TestCase):
    def test_switches_only_below_five_percent_remaining(self):
        self.assertEqual(select_model({"seven_day_opus": {"utilization": 95}})[0], "opus")
        self.assertEqual(select_model({"seven_day_opus": {"utilization": 95.1}})[0], "fable")

    def test_reads_new_scoped_opus_limit(self):
        usage = {"limits": [{
            "kind": "weekly_scoped",
            "percent": 98,
            "scope": {"model": {"display_name": "Opus", "id": None}},
        }]}
        self.assertEqual(opus_used_percentages(usage), [98.0])
        self.assertEqual(select_model(usage)[0], "fable")

    def test_shared_limits_do_not_masquerade_as_opus_limit(self):
        usage = {
            "five_hour": {"utilization": 100, "resets_at": "soon"},
            "seven_day": {"utilization": 99},
            "limits": [{
                "kind": "weekly_scoped",
                "percent": 96,
                "scope": {"model": {"display_name": "Fable"}},
            }],
        }
        self.assertEqual(select_model(usage), ("opus", "no_opus_specific_limit"))
        self.assertEqual(shared_limits(usage)[0]["remaining_percent"], 0.0)

    def test_malformed_values_are_ignored(self):
        self.assertEqual(select_model({"seven_day_opus": {"utilization": "96"}})[0], "opus")

    def test_available_opus_and_fable_stay_on_claude(self):
        self.assertEqual(select_route({}).engine, "claude")
        route = select_route({"seven_day_opus": {"utilization": 96}})
        self.assertEqual((route.engine, route.model), ("claude", "fable"))

    def test_observed_shared_exhaustion_selects_codex(self):
        route = select_route({
            "five_hour": {"utilization": 100, "resets_at": "2033-05-18T03:33:20Z"},
        })
        self.assertEqual(route, Route(
            "codex", CODEX_MODEL, "observed_shared_limit_exhausted:five_hour"
        ))

    def test_only_observed_exhaustion_of_both_scoped_models_selects_codex(self):
        usage = {"limits": [
            {"percent": 100, "scope": {"model": {"display_name": "Opus"}}},
            {"percent": 100, "scope": {"model": {"display_name": "Fable"}}},
        ]}
        self.assertEqual(model_used_percentages(usage, "fable"), [100.0])
        self.assertEqual(select_route(usage).engine, "codex")

    def test_fable_exhaustion_does_not_discard_remaining_opus(self):
        usage = {"limits": [
            {"percent": 96, "scope": {"model": {"display_name": "Opus"}}},
            {"percent": 100, "scope": {"model": {"display_name": "Fable"}}},
        ]}
        route = select_route(usage)
        self.assertEqual((route.engine, route.model), ("claude", "opus"))

    def test_unavailable_or_unknown_usage_keeps_fail_visible_opus(self):
        with mock.patch("claude_product_owner.fetch_usage", side_effect=OSError("offline")):
            route, usage, error = inspect_live()
        self.assertEqual((route.engine, route.model), ("claude", "opus"))
        self.assertIsNone(usage)
        self.assertIn("OSError", error)

    def test_background_engines_have_the_same_owner_cwd_and_access(self):
        claude = claude_command("opus", "print", [])
        codex = codex_command("print", [])
        self.assertIn("--add-dir", claude)
        self.assertIn("/opt/projects", claude)
        self.assertIn("--add-dir", codex)
        self.assertIn("/opt/projects", codex)
        self.assertEqual(codex[codex.index("-C") + 1], "/opt/projects/product-owner")
        self.assertEqual(codex[codex.index("--model") + 1], "gpt-5.6-sol")


if __name__ == "__main__":
    unittest.main()
