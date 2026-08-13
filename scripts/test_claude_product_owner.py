import unittest
from unittest import mock
import urllib.error

from claude_product_owner import (
    CODEX_MODEL,
    STARTUP_PROMPT,
    Route,
    claude_command,
    codex_command,
    inspect_live,
    inspect_observation,
    model_limits,
    model_used_percentages,
    opus_used_percentages,
    select_model,
    select_route,
    shared_limits,
)


class ProductOwnerModelRouterTests(unittest.TestCase):
    def test_interactive_owner_receives_no_code_first_order(self):
        self.assertIn("ничего не делать", STARTUP_PROMPT)
        self.assertIn("убрать или отключить", STARTUP_PROMPT)
        self.assertIn("настроить или переиспользовать", STARTUP_PROMPT)
        self.assertIn("минимально необходимый код", STARTUP_PROMPT)

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

    def test_model_limit_keeps_reset_and_explicit_absence(self):
        usage = {"limits": [{
            "kind": "weekly_scoped",
            "percent": 35,
            "resets_at": "2033-05-18T03:33:20Z",
            "scope": {"model": {"display_name": "Fable"}},
        }]}
        self.assertFalse(model_limits(usage, "opus")["published"])
        fable = model_limits(usage, "fable")
        self.assertTrue(fable["published"])
        self.assertEqual(fable["limits"][0]["remaining_percent"], 65.0)
        self.assertEqual(fable["limits"][0]["resets_at"], "2033-05-18T03:33:20Z")

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
        self.assertEqual(select_route({"five_hour": {"utilization": 130}}).engine, "claude")

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

    def test_401_uses_claude_owned_zero_turn_refresh_then_retries(self):
        unauthorized = urllib.error.HTTPError(
            "https://example.invalid/usage", 401, "Unauthorized", {}, None
        )
        payload = {"five_hour": {"utilization": 4, "resets_at": "later"}, "limits": []}
        with (
            mock.patch("claude_product_owner.fetch_usage", side_effect=[unauthorized, unauthorized, payload]),
            mock.patch("claude_product_owner.refresh_authorization_with_claude") as refresh,
        ):
            observation = inspect_observation()
        refresh.assert_called_once_with()
        self.assertEqual(observation.authorization_recovery, "claude_cli_zero_turn_usage")
        self.assertEqual(observation.route.engine, "claude")
        self.assertIsNone(observation.error)

    def test_authorization_failure_has_no_fabricated_quota(self):
        unauthorized = urllib.error.HTTPError(
            "https://example.invalid/usage", 401, "Unauthorized", {}, None
        )
        with (
            mock.patch("claude_product_owner.fetch_usage", side_effect=unauthorized),
            mock.patch(
                "claude_product_owner.refresh_authorization_with_claude",
                side_effect=RuntimeError("refresh failed"),
            ),
        ):
            observation = inspect_observation()
        self.assertIsNone(observation.usage)
        self.assertEqual(observation.route.reason, "usage_unavailable")
        self.assertEqual(observation.error["kind"], "authorization_or_runtime")

    def test_unknown_live_schema_is_visible_not_exhaustion(self):
        with mock.patch(
            "claude_product_owner.fetch_usage",
            side_effect=ValueError("usage response has no recognized quota fields"),
        ):
            observation = inspect_observation()
        self.assertEqual(observation.route, Route("claude", "opus", "usage_unavailable"))
        self.assertEqual(observation.error["kind"], "schema")

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
