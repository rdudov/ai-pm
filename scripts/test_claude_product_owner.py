import json
import subprocess
import unittest
from unittest import mock
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO

import claude_product_owner as router
import plain_russian
from claude_product_owner import (
    CODEX_MODEL,
    STARTUP_PROMPT,
    Route,
    claude_command,
    codex_command,
    claude_weekly_remaining,
    codex_weekly_remaining,
    inspect_live,
    inspect_observation,
    model_limits,
    model_used_percentages,
    opus_used_percentages,
    select_model,
    select_route,
    shared_limits,
)


def observed_codex(remaining: float) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "used_percent": 100 - remaining,
        "remaining_percent": remaining,
        "window_minutes": 10_080,
        "window_days": 7.0,
        "observed_at": now.isoformat(),
        "resets_at_epoch": now.timestamp() + 3 * 86_400,
    }


class ProductOwnerModelRouterTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("claude_product_owner.codex_budget.latest",
                             return_value=observed_codex(81))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_interactive_owner_receives_no_code_first_order(self):
        self.assertIn("ничего не делать", STARTUP_PROMPT)
        self.assertIn("убрать или отключить", STARTUP_PROMPT)
        self.assertIn("настроить или переиспользовать", STARTUP_PROMPT)
        self.assertIn("минимально необходимый код", STARTUP_PROMPT)
        self.assertIn("Не превращай сводку в технический лог", STARTUP_PROMPT)

    def test_every_text_for_the_user_carries_the_same_language_rules(self):
        # 2026-08-23: «мне реально сложно читать, что ты пишешь… я трачу больше
        # времени и быстрее устаю, читая твои отчёты и ответы». Пользователь
        # назвал отчёты и ответы, а правила языка до этого стояли только в
        # контракте письма. Текст к пользователю уходит тремя путями, и тест
        # проверяет совпадение одного текста в трёх, а не похожие слова в трёх
        # местах.
        import daily_standup
        import thread_tick

        rules = " ".join(plain_russian.RULES.split())
        packet = {"plan": "", "snapshots": {}, "threads": {}, "recent_letters": []}
        for name, text in (
            ("консольный ответ", STARTUP_PROMPT),
            ("письмо", thread_tick.verdict_block()),
            ("утренняя оперативка", daily_standup.prompt(packet, "2026-08-24")),
        ):
            with self.subTest(path=name):
                self.assertIn(rules, " ".join(text.split()))

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
        self.assertEqual(select_route({"five_hour": {"utilization": 130}}, None).engine,
                         "claude")

    def test_available_opus_and_fable_stay_on_claude(self):
        self.assertEqual(select_route({}, observed_codex(80)).engine, "claude")
        route = select_route({
            "seven_day": {"utilization": 10},
            "seven_day_opus": {"utilization": 96},
        }, observed_codex(80))
        self.assertEqual((route.engine, route.model), ("claude", "fable"))

    def test_larger_observed_codex_remainder_selects_codex(self):
        usage = {"seven_day": {"utilization": 69}}
        route = select_route(usage, observed_codex(81))
        self.assertEqual(route, Route(
            "codex", CODEX_MODEL, "weekly_remaining:claude=31%,codex=81%"
        ))
        self.assertEqual(claude_weekly_remaining(usage), 31.0)

    def test_manual_claude_binding_reuses_model_selection(self):
        def shown(usage: dict, argv: list[str]) -> list[str]:
            output = StringIO()
            diagnostic_argv = list(argv)
            if "--" in diagnostic_argv:
                diagnostic_argv.insert(diagnostic_argv.index("--"), "--show-command")
            else:
                diagnostic_argv.append("--show-command")
            with (mock.patch("claude_product_owner.fetch_usage", return_value=usage),
                  mock.patch("claude_product_owner.codex_budget.latest",
                             return_value=observed_codex(90)),
                  redirect_stdout(output)):
                self.assertEqual(router.main(diagnostic_argv), 0)
            return json.loads(output.getvalue())

        unforced = shown(
            {"seven_day": {"utilization": 90}},
            ["--entry", "interactive"],
        )
        forced_opus = shown(
            {"seven_day": {"utilization": 90}},
            ["--entry", "interactive", "--force-claude", "--", "probe"],
        )
        forced_fable = shown(
            {
                "seven_day": {"utilization": 60},
                "seven_day_opus": {"utilization": 100},
            },
            ["--entry", "interactive", "--force-claude", "--", "probe"],
        )
        forced_opus_when_fable_exhausted = shown(
            {
                "seven_day": {"utilization": 60},
                "seven_day_opus": {"utilization": 97},
                "seven_day_fable": {"utilization": 100},
            },
            ["--entry", "interactive", "--force-claude", "--", "probe"],
        )

        self.assertEqual(unforced[0], router.CODEX_BIN)
        self.assertEqual(forced_opus[0], router.CLAUDE_BIN)
        self.assertEqual(forced_opus[1:3], ["--model", router.OPUS_MODEL])
        self.assertTrue(forced_opus[-1].endswith("Первый запрос пользователя: probe"))
        self.assertEqual(forced_fable[1:3], ["--model", router.FABLE_MODEL])
        self.assertEqual(
            forced_opus_when_fable_exhausted[1:3],
            ["--model", router.OPUS_MODEL],
        )

    def test_larger_observed_claude_remainder_selects_claude(self):
        route = select_route(
            {"seven_day": {"utilization": 18}}, observed_codex(31)
        )
        self.assertEqual(route, Route(
            "claude", "opus", "weekly_remaining:claude=82%,codex=31%"
        ))

    def test_missing_remainder_is_visible_and_not_fabricated(self):
        no_codex = select_route({"seven_day": {"utilization": 69}}, None)
        self.assertEqual(no_codex.engine, "claude")
        self.assertEqual(
            no_codex.reason,
            "codex_weekly_remaining_unavailable:claude_remaining=31%",
        )
        no_claude = select_route({}, observed_codex(81))
        self.assertEqual(no_claude.engine, "claude")
        self.assertEqual(no_claude.reason, "claude_weekly_remaining_unavailable")

    def test_missing_codex_observation_preserves_the_fable_fallback(self):
        route = select_route({
            "seven_day": {"utilization": 69},
            "seven_day_opus": {"utilization": 96},
        }, None)
        self.assertEqual((route.engine, route.model), ("claude", "fable"))
        self.assertIn("codex_weekly_remaining_unavailable", route.reason)

    def test_codex_remainder_requires_a_current_explicit_weekly_window(self):
        not_weekly = {**observed_codex(81), "window_minutes": 300}
        self.assertIsNone(codex_weekly_remaining(not_weekly))
        self.assertEqual(
            select_route({"seven_day": {"utilization": 69}}, not_weekly).reason,
            "codex_weekly_remaining_unavailable:claude_remaining=31%",
        )
        stale = {**observed_codex(81), "resets_at_epoch": 1}
        self.assertIsNone(codex_weekly_remaining(stale))

    def test_codex_observation_failure_is_visible_in_status_record(self):
        with (mock.patch("claude_product_owner.codex_budget.latest",
                         side_effect=OSError("sessions unreadable")),
              mock.patch("claude_product_owner.fetch_usage", return_value={
                  "seven_day": {"utilization": 69},
              })):
            observation = inspect_observation()
        self.assertEqual(observation.route.reason,
                         "codex_weekly_remaining_unavailable:claude_remaining=31%")
        self.assertEqual(observation.codex_error["kind"], "codex_observation")

    def test_codex_observation_failure_is_visible_before_claude_exec(self):
        stderr = StringIO()
        with (mock.patch("claude_product_owner.codex_budget.latest",
                         side_effect=OSError("sessions unreadable")),
              mock.patch("claude_product_owner.fetch_usage", return_value={
                  "seven_day": {"utilization": 69},
              }),
              mock.patch("claude_product_owner.os.chdir"),
              mock.patch("claude_product_owner.os.execvpe",
                         side_effect=RuntimeError("exec boundary")),
              redirect_stderr(stderr),
              self.assertRaisesRegex(RuntimeError, "exec boundary")):
            router.main(["--entry", "print"])
        self.assertIn(
            "product-owner: route selected; Claude "
            "(codex_weekly_remaining_unavailable:claude_remaining=31%)",
            stderr.getvalue(),
        )

    def test_observed_claude_comparison_is_visible_before_exec(self):
        stderr = StringIO()
        with (mock.patch("claude_product_owner.codex_budget.latest",
                         return_value=observed_codex(31)),
              mock.patch("claude_product_owner.fetch_usage", return_value={
                  "seven_day": {"utilization": 18},
              }),
              mock.patch("claude_product_owner.os.chdir"),
              mock.patch("claude_product_owner.os.execvpe",
                         side_effect=RuntimeError("exec boundary")),
              redirect_stderr(stderr),
              self.assertRaisesRegex(RuntimeError, "exec boundary")):
            router.main(["--entry", "print"])
        self.assertIn(
            "product-owner: route selected; Claude "
            "(weekly_remaining:claude=82%,codex=31%)",
            stderr.getvalue(),
        )

    def test_unavailable_claude_observation_is_visible_before_exec(self):
        stderr = StringIO()
        with (mock.patch("claude_product_owner.fetch_usage",
                         side_effect=OSError("offline")),
              mock.patch("claude_product_owner.os.chdir"),
              mock.patch("claude_product_owner.os.execvpe",
                         side_effect=RuntimeError("exec boundary")),
              redirect_stderr(stderr),
              self.assertRaisesRegex(RuntimeError, "exec boundary")):
            router.main(["--entry", "print"])
        self.assertIn(
            "product-owner: route selected; Claude (usage_unavailable)",
            stderr.getvalue(),
        )

    def test_observed_shared_exhaustion_selects_codex(self):
        route = select_route({
            "five_hour": {"utilization": 100, "resets_at": "2033-05-18T03:33:20Z"},
        }, observed_codex(80))
        self.assertEqual(route, Route(
            "codex", CODEX_MODEL, "observed_shared_limit_exhausted:five_hour"
        ))

    def test_only_observed_exhaustion_of_both_scoped_models_selects_codex(self):
        usage = {"limits": [
            {"percent": 100, "scope": {"model": {"display_name": "Opus"}}},
            {"percent": 100, "scope": {"model": {"display_name": "Fable"}}},
        ]}
        self.assertEqual(model_used_percentages(usage, "fable"), [100.0])
        self.assertEqual(select_route(usage, observed_codex(80)).engine, "codex")

    def test_fable_exhaustion_does_not_discard_remaining_opus(self):
        usage = {"limits": [
            {"percent": 96, "scope": {"model": {"display_name": "Opus"}}},
            {"percent": 100, "scope": {"model": {"display_name": "Fable"}}},
        ]}
        route = select_route(usage, observed_codex(3))
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

    def test_authorization_refresh_never_reads_the_callers_stdin(self):
        # A background wake hands its text over through stdin. A `--print`
        # child reads an inherited pipe to the end, and the whole wake is then
        # lost: the real run meets an empty stdin and dies with «Input must be
        # provided». This refresh has nothing to say to stdin at all.
        completed = mock.Mock(returncode=0, stdout=json.dumps(
            {"is_error": False, "num_turns": 0, "total_cost_usd": 0,
             "usage": {"input_tokens": 0, "output_tokens": 0}}))
        with mock.patch("claude_product_owner.subprocess.run",
                        return_value=completed) as run:
            router.refresh_authorization_with_claude()
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

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
        # The directories are the installation's own answer, so the test states
        # one instead of asserting the shelf of whichever machine runs it.
        shelf = ["/srv/example-projects", "/srv/example-lab"]
        with mock.patch.object(router.product_memory, "workspace_dirs",
                               return_value=shelf):
            claude = claude_command("opus", "print", [])
            codex = codex_command("print", [])
        for command in (claude, codex):
            self.assertEqual([command[index + 1] for index, item
                              in enumerate(command) if item == "--add-dir"], shelf)
        # Where this product owner is installed, not where one server keeps it.
        self.assertEqual(codex[codex.index("-C") + 1], str(router.HOME))
        self.assertEqual(codex[codex.index("--model") + 1], "gpt-5.6-sol")

    def test_claude_exec_starts_in_the_owner_checkout(self):
        with (mock.patch("claude_product_owner.fetch_usage", return_value={}),
              mock.patch("claude_product_owner.os.chdir") as chdir,
              mock.patch("claude_product_owner.os.execvpe",
                         side_effect=RuntimeError("exec boundary")),
              self.assertRaisesRegex(RuntimeError, "exec boundary")):
            router.main(["--entry", "interactive"])
        chdir.assert_called_once_with(router.HOME)

    def test_the_codex_route_notice_is_a_diagnostic_and_not_part_of_the_letter(self):
        """«Ровно `SILENT` остаётся молчанием» — 2026-08-23.

        On the print path stdout is the letter. Until this day the router put its
        Russian routing notice there, so every Codex-routed letter opened with
        it, and a wake-up that answered exactly `SILENT` produced a two-line
        letter that `thread_tick` no longer recognized as silence: the word
        itself was mailed to the user at 11:35 UTC as Gmail `message-id`.
        """
        out, err = StringIO(), StringIO()
        answer = subprocess.CompletedProcess([], 0, stdout="SILENT\n", stderr="")
        with (mock.patch("claude_product_owner.codex_budget.latest",
                         return_value=observed_codex(81)),
              mock.patch("claude_product_owner.fetch_usage", return_value={
                  "seven_day": {"utilization": 82},
              }),
              mock.patch("claude_product_owner.sys.stdin") as stdin,
              mock.patch("claude_product_owner.subprocess.run", return_value=answer),
              redirect_stdout(out), redirect_stderr(err)):
            stdin.read.return_value = "проснись"
            router.main(["--entry", "print"])
        self.assertEqual(out.getvalue().strip(), "SILENT")
        self.assertIn("product-owner: route selected; Codex", err.getvalue())
        self.assertIn("продолжаю через Codex", err.getvalue())

    def test_an_installation_that_names_no_directories_gets_no_flag(self):
        # A fresh clone works in its own checkout: an empty `--add-dir` would be
        # a broken command line, and a default shelf would be somebody else's.
        with mock.patch.object(router.product_memory, "workspace_dirs",
                               return_value=[]):
            self.assertNotIn("--add-dir", claude_command("opus", "print", []))
            self.assertNotIn("--add-dir", codex_command("print", []))


if __name__ == "__main__":
    unittest.main()
