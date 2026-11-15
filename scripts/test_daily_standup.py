from __future__ import annotations

from email import policy
from email.parser import BytesParser
from datetime import datetime, timezone
import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import daily_standup as daily
import outbound
import thread_tick


SAMPLE = {
    "intro": "Доброе утро. Продолжаю прежний план.", "outcomes": [],
    "plans": [{"product": "Companion", "action": "Закончить выравнивание.",
               "result": "Один понятный отчёт.", "deadline": "Сегодня к 12:00 МСК"}],
    "extras": ["Получено дополнительное заключение."], "removed": [],
    "waiting": "Ждём внешнее событие.",
    "questions": [{"question": "Удалять архив?", "recommendation": "Сохранить копию.",
                   "tradeoff": "Место против обратимости."}],
}


class DailyStandupRendering(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.enterContext(mock.patch.object(outbound, "LEDGER", Path(temporary.name) / "outbound.json"))

    def test_background_composition_selects_print_entry_explicitly(self):
        completed = mock.Mock(returncode=0, stdout=__import__("json").dumps(SAMPLE), stderr="")
        with mock.patch.object(daily.subprocess, "run", return_value=completed) as run:
            daily.compose({"plan": "plan"}, "2026-08-15")
        self.assertEqual(run.call_args.args[0][1:3], ["--entry", "print"])

    def test_thread_dry_run_cannot_call_the_daily_sender(self):
        source = Path(thread_tick.__file__).read_text(encoding="utf-8")
        daily_block = source[source.index("daily_result = None"):
                             source.index("# Before anything is observed")]
        self.assertIn('if args.thread == "process" and not args.dry_run:', daily_block)
        self.assertIn('[str(CLAUDE_PRODUCT_OWNER), "--entry", "print"]', source)

    def test_one_source_makes_plain_and_real_html_table(self):
        plain = daily.render_plain(SAMPLE)
        html = daily.render_html(SAMPLE)
        self.assertIn("Companion", plain)
        self.assertEqual(html.count("<h2>"), 6)
        self.assertIn("Companion", html)
        self.assertNotIn("ПОВОД:", plain + html)
        self.assertNotIn("ВОПРОС:", plain + html)
        self.assertNotIn("мешает: нет", plain)
        self.assertNotIn('data-label="Что мешает">нет', html)

    def test_a_finished_phrase_does_not_get_a_second_punctuation_mark(self):
        """Ровно те поля, из которых 24 августа собралась оперативка пользователя.

        Модель пишет их законченными предложениями и ставит знак сама, а рендер
        добавлял свой поверх: пользователь получил «проверки API.; мешает» и
        «Первый шаг: … ..». Наблюдение — `live-evidence/round8-standup`
        задачи 1260.
        """
        plain = daily.render_plain(SAMPLE)
        for doubled in ("..", ".;", ",;", ".,", "?.", "!."):
            self.assertNotIn(doubled, plain)

    def test_mobile_scale_is_explicit_and_headings_are_bounded(self):
        html = daily.render_html(SAMPLE)
        self.assertIn('name=viewport content="width=device-width, initial-scale=1"', html)
        self.assertIn("h1{font-size:24px", html)
        self.assertIn("@media(max-width:520px)", html)
        self.assertIn("overflow-wrap:anywhere", html)

    def test_raw_message_contains_both_alternatives(self):
        raw = daily.raw_message("user@example.test", "Оперативка", "plain", "<b>html</b>")
        parsed = BytesParser(policy=policy.default).parsebytes(raw)
        parts = {part.get_content_type(): part.get_content() for part in parsed.walk()
                 if part.get_content_type() in {"text/plain", "text/html"}}
        self.assertEqual(parts["text/plain"].strip(), "plain")
        self.assertIn("<b>html</b>", parts["text/html"])

    def test_parser_ignores_router_notice_before_json(self):
        parsed = daily.parse_composition("route notice\n" + __import__("json").dumps(SAMPLE))
        self.assertEqual(parsed["intro"], SAMPLE["intro"])

    def test_empty_sources_cost_no_model_call(self):
        packet = {"plan": "", "snapshots": {"empty": ""},
                  "threads": {"process": {"thread": "process", "title": "p"}}}
        self.assertTrue(daily.mechanically_empty(packet))

    def test_first_tick_after_eight_catches_up(self):
        at = datetime(2026, 8, 15, 10, 20, tzinfo=timezone.utc)
        packet = {"plan": "plan", "snapshots": {}, "threads": {}}
        with mock.patch.object(daily, "sent_today", return_value=False), \
                mock.patch.object(daily, "source_packet", return_value=packet), \
                mock.patch.object(daily, "compose", return_value=SAMPLE), \
                mock.patch.object(thread_tick, "MAIL_TO", "user@example.test"), \
                mock.patch.object(thread_tick, "MAIL_SCRIPT", Path(__file__)), \
                mock.patch.object(thread_tick, "MAIL_PYTHON", Path(__file__)), \
                mock.patch.object(thread_tick, "deliver", return_value={"action": "send"}) as deliver:
            result = daily.maybe_send(at)
        self.assertEqual(result["action"], "send")
        deliver.assert_called_once()

    def test_missing_mail_door_costs_no_model_call(self):
        at = datetime(2026, 8, 15, 6, 0, tzinfo=timezone.utc)
        with mock.patch.object(daily, "sent_today", return_value=False), \
                mock.patch.object(thread_tick, "MAIL_TO", ""), \
                mock.patch.object(daily, "compose") as compose:
            result = daily.maybe_send(at)
        self.assertEqual(result["action"], "fail")
        compose.assert_not_called()

    def test_failed_delivery_is_retried_no_more_than_hourly(self):
        at = datetime(2026, 8, 15, 6, 20, tzinfo=timezone.utc)
        previous = {"action": "fail",
                    "at": datetime(2026, 8, 15, 6, 0,
                                   tzinfo=timezone.utc).isoformat(),
                    "reason": "gmail unavailable"}
        with mock.patch.object(daily, "sent_today", return_value=False), \
                mock.patch.object(daily, "compose") as compose:
            result = daily.maybe_send(at, previous=previous)
        self.assertEqual(result["action"], "fail")
        self.assertTrue(result["deferred"])
        compose.assert_not_called()

    def test_daily_kind_bypasses_the_proactive_threshold_and_carries_raw_mime(self):
        at = datetime(2026, 8, 15, 6, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.object(outbound, "LEDGER", Path(home) / "outbound.json"), \
                mock.patch.object(thread_tick, "send_mail", return_value="gmail-daily") as send:
            record = thread_tick.deliver(
                "portfolio", "daily", "Оперативка", "Доброе утро", at,
                raw_message=b"To: user@example.test\n\nbody")
        self.assertEqual(record["action"], "send")
        self.assertEqual(record["message_id"], "gmail-daily")
        self.assertEqual(send.call_args.kwargs["raw_message"], b"To: user@example.test\n\nbody")

    def test_real_state_failure_survives_composer_silence_into_daily_mail(self):
        import copy
        at = datetime(2026, 8, 15, 10, 20, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as home:
            directory = Path(home)
            state_path = directory / "demo.json"
            reason = "Вход для модели превысил допустимый размер. Подробности — в журнале службы."
            thread_tick.persist_wake_failure(state_path, {
                "updated_at": at.isoformat(), "check": {}}, reason, {})
            value = {"portfolio_plan": "first then second", "product_snapshots": {},
                     "thread_states": {}, "contract": {}}
            with mock.patch.object(thread_tick, "STATE_DIR", directory), \
                    mock.patch.object(outbound, "LEDGER", directory / "outbound.json"), \
                    mock.patch.object(daily.product_memory, "installation", return_value={
                        "threads": {"demo": {"title": "Demo"}}}), \
                    mock.patch.object(daily.startup_context, "packet", return_value=value), \
                    mock.patch.object(daily, "sent_today", return_value=False), \
                    mock.patch.object(daily, "compose", return_value=copy.deepcopy(SAMPLE)) as compose, \
                    mock.patch.object(thread_tick, "MAIL_TO", "user@example.test"), \
                    mock.patch.object(thread_tick, "MAIL_SCRIPT", Path(__file__)), \
                    mock.patch.object(thread_tick, "MAIL_PYTHON", Path(__file__)), \
                    mock.patch.object(thread_tick, "deliver", return_value={"action": "send"}) as deliver:
                self.assertEqual(daily.maybe_send(at)["action"], "send")
            plain = deliver.call_args.args[3]
            self.assertIn(reason, str(compose.call_args.args[0]["wake_health"]))
            self.assertNotIn(reason, plain)
            self.assertIn("Ждём восстановления фоновых проверок", plain)
            message = BytesParser(policy=policy.default).parsebytes(deliver.call_args.kwargs["raw_message"])
            html = message.get_body(preferencelist=("html",)).get_content()
            self.assertNotIn(reason, html)
            self.assertIn("Ждём восстановления фоновых проверок", html)
            self.assertEqual(deliver.call_args.args[1], "daily")

    def test_refusals_preserve_bounded_direction_health_and_delivery_identity(self):
        import json
        at = datetime(2026, 11, 15, 10, tzinfo=timezone.utc)
        reason = "Вход для модели превысил допустимый размер."
        for stage in ("sources", "engine", "parse"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as home:
                directory = Path(home)
                (directory / "demo.json").write_text(json.dumps({"wake_failure": {"reason": reason}}))
                with contextlib.ExitStack() as stack:
                    for patch in (
                        mock.patch.object(outbound, "LEDGER", directory / "outbound.json"),
                        mock.patch.object(thread_tick, "STATE_DIR", directory),
                        mock.patch.object(daily.product_memory, "installation", return_value={
                            "threads": {"demo": {"title": "Demo"}}}),
                        mock.patch.object(daily, "source_packet", side_effect=
                            daily.startup_context.InputLimitError("source limit") if stage == "sources"
                            else lambda *args: {"plan": "work", "snapshots": {}, "threads": {}}),
                        mock.patch.object(daily.subprocess, "run", return_value=
                            subprocess.CompletedProcess([], 2 if stage == "engine" else 0,
                                                        "invalid JSON", "")),
                        mock.patch.object(thread_tick, "MAIL_TO", "user@example.test"),
                        mock.patch.object(thread_tick, "MAIL_SCRIPT", Path(__file__)),
                        mock.patch.object(thread_tick, "MAIL_PYTHON", Path(__file__)),
                    ):
                        stack.enter_context(patch)
                    send = stack.enter_context(mock.patch.object(thread_tick, "send_mail", return_value="refusal-id"))
                    journal = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                    self.assertEqual(daily.maybe_send(at, force=True)["action"], "fail")
                    self.assertEqual(daily.maybe_send(at, force=True)["action"], "fail")
                    send.assert_called_once()
                    message = BytesParser(policy=policy.default).parsebytes(send.call_args.kwargs["raw_message"])
                    for kind in ("plain", "html"):
                        body = message.get_body(preferencelist=(kind,)).get_content()
                        self.assertIn(reason, body)
                        self.assertIn("Demo: восстановить фоновую проверку", body)
                    with outbound.Ledger() as ledger:
                        entry = ledger.thread("portfolio")
                        self.assertFalse(entry.get("daily_editions"))
                        self.assertTrue(outbound.event_delivered_anywhere(
                            ledger.data, "daily:2026-11-15:refusal"))
                        self.assertEqual(entry["daily_standup"]["action"], "fail")

    def test_unreadable_or_oversized_health_does_not_stop_refusal(self):
        for error in (OSError("unreadable"), daily.startup_context.InputLimitError("health bound")):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(daily, "source_packet", side_effect=ValueError("source")), \
                    mock.patch.object(daily, "wake_health", side_effect=error), \
                    mock.patch.object(thread_tick, "MAIL_TO", "user@example.test"), \
                    mock.patch.object(thread_tick, "MAIL_SCRIPT", Path(__file__)), \
                    mock.patch.object(thread_tick, "MAIL_PYTHON", Path(__file__)), \
                    mock.patch.object(thread_tick, "deliver", return_value={"action": "send"}) as deliver:
                self.assertEqual(daily.maybe_send(force=True)["action"], "fail")
                deliver.assert_called_once()

    def test_persistent_unsafe_health_is_waiting_never_completed_work(self):
        import copy
        health = [{"product": "Demo", "blocker": "Пока не проверено соединение."}]
        for refusal in (False, True):
            with self.subTest(refusal=refusal), \
                    mock.patch.object(daily, "source_packet", return_value={
                        "plan": "work", "snapshots": {}, "threads": {}, "wake_health": health}), \
                    mock.patch.object(daily, "wake_health", return_value=health), \
                    mock.patch.object(daily, "compose", side_effect=ValueError("parse") if refusal else
                                      lambda *args: copy.deepcopy(SAMPLE)), \
                    mock.patch.object(thread_tick, "MAIL_TO", "user@example.test"), \
                    mock.patch.object(thread_tick, "MAIL_SCRIPT", Path(__file__)), \
                    mock.patch.object(thread_tick, "MAIL_PYTHON", Path(__file__)), \
                    mock.patch.object(thread_tick, "deliver", return_value={"action": "send"}) as deliver:
                for day in (15, 16):
                    daily.maybe_send(datetime(2026, 11, day, 10, tzinfo=timezone.utc), force=True)
                    plain = deliver.call_args.args[3]
                    for phrase in daily.EMPTY_NEGATIVES:
                        self.assertNotIn(phrase, plain.casefold())
                    extras = plain.split("Сделано сверх плана")[1].split("План на сегодня")[0]
                    self.assertNotIn("фонов", extras)
                    self.assertIn("фонов", plain.split("Ждём событий")[1])

    def test_failed_daily_engine_keeps_streams_in_journal_only(self):
        banner = "runtime header /private/runtime/path\n" * 200
        for out, err, known_limit in [
            ("partial answer", banner + "input_too_large", True),
            (banner + "Input exceeds the maximum length of 1048576 characters", "", True),
            (banner + "partial answer", "", False),
        ]:
            with self.subTest(known_limit=known_limit, stdout_only=not err):
                journal = io.StringIO()
                with contextlib.redirect_stderr(journal), \
                        mock.patch.object(daily, "sent_today", return_value=False), \
                        mock.patch.object(daily, "source_packet", return_value={"plan": "plan"}), \
                        mock.patch.object(daily, "wake_health", return_value=[]), \
                        mock.patch.object(daily.subprocess, "run", return_value=
                            subprocess.CompletedProcess([], 2, out, err)) as engine, \
                        mock.patch.object(thread_tick, "MAIL_TO", "user@example.test"), \
                        mock.patch.object(thread_tick, "MAIL_SCRIPT", Path(__file__)), \
                        mock.patch.object(thread_tick, "MAIL_PYTHON", Path(__file__)), \
                        mock.patch.object(thread_tick, "deliver", return_value={"action": "send"}) as deliver:
                    self.assertEqual(daily.maybe_send(force=True)["action"], "fail")
                engine.assert_called_once()
                self.assertIn("stderr:\n" + err, journal.getvalue())
                self.assertIn("stdout:\n" + out, journal.getvalue())
                message = BytesParser(policy=policy.default).parsebytes(deliver.call_args.kwargs["raw_message"])
                for kind in ("plain", "html"):
                    body = message.get_body(preferencelist=(kind,)).get_content()
                    self.assertIn("превысил допустимый размер" if known_limit
                                  else "причина не установлена", body)
                    self.assertIn("в журнале службы", body)
                    for fragment in ("runtime header", "/private/runtime/path", "partial answer",
                                     "input_too_large", "Input exceeds"):
                        self.assertNotIn(fragment, body)

    def test_daily_bound_refuses_model_but_sends_one_readable_daily_failure(self):
        at = datetime(2026, 8, 15, 10, 20, tzinfo=timezone.utc)
        with mock.patch.object(daily, "sent_today", return_value=False), \
                mock.patch.object(daily, "source_packet", return_value={
                    "plan": "x" * 1_100_000, "snapshots": {}, "threads": {}}), \
                mock.patch.object(daily, "wake_health", return_value=[]), \
                mock.patch.object(daily.subprocess, "run") as run, \
                mock.patch.object(thread_tick, "MAIL_TO", "user@example.test"), \
                mock.patch.object(thread_tick, "MAIL_SCRIPT", Path(__file__)), \
                mock.patch.object(thread_tick, "MAIL_PYTHON", Path(__file__)), \
                mock.patch.object(thread_tick, "deliver", return_value={"action": "send"}) as deliver:
            self.assertEqual(daily.maybe_send(at)["action"], "fail")
        run.assert_not_called()
        deliver.assert_called_once()
        self.assertIn("Оперативка не собрана", deliver.call_args.args[3])
        self.assertIn("модель не вызвана", deliver.call_args.args[3])


class PlanContinuity(unittest.TestCase):
    def setUp(self):
        import copy
        self.copy = copy.deepcopy
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.at = datetime(2026, 11, 15, 10, tzinfo=timezone.utc)
        for patch in [mock.patch.object(outbound, 'LEDGER', self.home / 'outbound.json'),
                      mock.patch.object(thread_tick, 'MAIL_TO', 'user@example.test'),
                      mock.patch.object(thread_tick, 'MAIL_SCRIPT', Path(__file__)),
                      mock.patch.object(thread_tick, 'MAIL_PYTHON', Path(__file__)),
                      mock.patch.object(daily, 'source_packet', side_effect=lambda *args:
                          {'plan': 'work', 'snapshots': {}, 'threads': {}})]:
            self.enterContext(patch)
        self.mail = self.enterContext(mock.patch.object(thread_tick, 'send_mail', return_value='gmail-test'))

    def edition(self):
        with outbound.Ledger() as ledger:
            return self.copy(ledger.thread('portfolio').get('daily_editions', []))

    def compose(self, packet, date):
        value = self.copy(SAMPLE)
        old = packet.get('previous_editions', [])
        value['plans'][0]['action'] = 'Подготовить итог ' + date + str(len(old))
        value['outcomes'] = [{'id': x['id'], 'status': 'частично', 'detail': 'Получена опись файлов.'}
                             for x in (old[-1]['plans'] if old else [])]
        return value

    def send(self, at=None, **kwargs):
        with mock.patch.object(daily, 'compose', side_effect=self.compose):
            return daily.maybe_send(at or self.at, **kwargs)

    def test_day_correction_next_day_keep_full_chain_and_drop_changes_nothing(self):
        from datetime import timedelta
        self.assertEqual(self.send()['action'], 'send')
        first = self.edition()[-1]
        self.assertIn('Цена вариантов', first['body'])
        self.assertIn('<html', first['html'])
        self.assertEqual(self.send(force=True)['action'], 'send')
        second = self.edition()[-1]
        self.assertEqual(second['previous_event_id'], first['event_id'])
        self.assertEqual(second['composition']['outcomes'][0]['id'], first['plans'][0]['id'])
        before = self.edition()
        self.assertEqual(self.send(force=True)['action'], 'skip')
        self.assertEqual(self.edition(), before)
        self.assertEqual(self.send(self.at + timedelta(days=1))['action'], 'send')
        third = self.edition()[-1]
        self.assertEqual(third['previous_event_id'], second['event_id'])
        self.assertEqual(len(self.edition()), 2)
        self.assertIn('daily_edition', (self.home / 'outbound-journal.jsonl').read_text())

    def test_missing_duplicate_unknown_and_repeated_plan_refuse(self):
        self.send()
        old = self.edition()
        valid = self.compose({'previous_editions': old}, 'tomorrow')
        for outcomes in ([], valid['outcomes'] * 2, [{**valid['outcomes'][0], 'id': 'unknown'}]):
            value = {**valid, 'outcomes': outcomes}
            with self.subTest(outcomes=outcomes), self.assertRaises(ValueError):
                daily.parse_composition(__import__('json').dumps(value), old)
        repeated = {**valid, 'plans': self.copy(old[-1]['plans'])}
        repeated['plans'][0]['deadline'] = 'Следующая неделя'
        with self.assertRaisesRegex(ValueError, 'Повтор плана'):
            daily.validate_composition(repeated, old)

    def test_all_statuses_transfer_fields_and_removed_reason(self):
        self.send()
        old = self.edition()
        value = self.compose({'previous_editions': old}, 'tomorrow')
        for status in ('получилось', 'частично', 'перенесено', 'снято'):
            row = value['outcomes'][0]
            row.update(status=status, deadline='16 ноября 12:00 МСК', approach='Получить данные с другого сервера')
            value['removed'] = ['Снята старая проверка: источник закрыт.'] if status == 'снято' else []
            self.assertEqual(daily.parse_composition(__import__('json').dumps(value), old), value)
        value['outcomes'][0].update(status='перенесено', approach='')
        with self.assertRaises(ValueError):
            daily.validate_composition(value, old)

    def test_compose_parse_and_mail_failures_preserve_previous_success(self):
        self.send()
        before = self.edition()
        for failure in (ValueError('invalid parse'), RuntimeError('engine failed')):
            with mock.patch.object(daily, 'compose', side_effect=failure):
                self.assertEqual(daily.maybe_send(self.at, force=True)['action'], 'fail')
            self.assertEqual(before, self.edition())
        with mock.patch.object(thread_tick, 'send_mail', return_value=False):
            self.assertEqual(self.send(force=True)['action'], 'fail')
        self.assertEqual(before, self.edition())
        self.assertEqual(self.send(force=True)['action'], 'send')
        self.assertEqual(len(self.edition()), 2)

    def test_old_format_requires_bound_extraction_and_survives_retry(self):
        import json
        thread_tick.deliver('portfolio', 'daily', 'Old', 'Full original plan', self.at, event_id='legacy')
        self.assertEqual(self.send(force=True)['action'], 'fail')
        self.assertEqual(self.edition(), [])
        body = self.home / 'body.txt'; body.write_text('Full original plan')
        extraction = {'source_message_id': 'gmail-test', 'source_path': str(body),
                      'items': [{'id': 'legacy-1', 'product': 'Demo', 'action': 'Read sources',
                                 'source_text': 'Full original plan'}]}
        path = self.home / 'legacy.json'; path.write_text(json.dumps(extraction))
        with mock.patch.object(thread_tick, 'send_mail', return_value=False):
            self.assertEqual(self.send(force=True, legacy_path=path)['action'], 'fail')
        self.assertEqual(self.edition(), [])
        self.assertEqual(self.send(force=True, legacy_path=path)['action'], 'send')
        self.assertEqual(self.edition()[-1]['composition']['outcomes'][0]['id'], 'legacy-1')
        self.assertEqual(self.send(force=True)['action'], 'skip')

    def test_stale_composition_is_refused_under_delivery_lock(self):
        self.send()
        count = self.mail.call_count
        result = thread_tick.deliver('portfolio', 'daily', 'Stale', 'Stale', self.at,
                 event_id='other', daily_edition={'previous_event_id': None})
        self.assertEqual(result['action'], 'fail')
        self.assertEqual(self.mail.call_count, count)

    def test_empty_sections_waiting_questions_and_escaping(self):
        value = {**self.copy(SAMPLE), 'plans': [], 'outcomes': [], 'extras': [], 'removed': [], 'waiting': ''}
        self.assertEqual(daily.render_html(value).count('<h2>'), 6)
        self.assertEqual(daily.render_plain(value).count('\nНет.'), 5)
        for key in ('recommendation', 'tradeoff'):
            broken = self.copy(value); broken['questions'][0][key] = ''
            with self.assertRaises(ValueError):
                daily.parse_composition(__import__('json').dumps(broken))
        value['waiting'] = '<Все внешние события>'
        self.assertIn('&lt;Все внешние события&gt;', daily.render_html(value))
        value['intro'] = 'Пока не проверено'
        with self.assertRaises(ValueError):
            daily.validate_composition(value, [])


if __name__ == "__main__":
    unittest.main()
