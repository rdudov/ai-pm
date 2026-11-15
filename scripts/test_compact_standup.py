"""Contract boundaries, journal-to-news and continuity of the concise edition."""
import copy
import json
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import daily_standup as daily
import regular_jobs as jobs
from test_daily_standup import SAMPLE

NOW = datetime(2026, 11, 15, 17, tzinfo=timezone.utc)
CONFIG = dict(id='archive', title='Архив', unit='example.service', source='journal:example', journal_argv=['journal-read'])


def journal(at, message):
    return {'__REALTIME_TIMESTAMP': str(int(at.timestamp() * 1_000_000)), 'MESSAGE': message}


def episode(hours, recovered=False):
    start = NOW - timedelta(hours=hours)
    rows = [journal(start - timedelta(hours=1), 'example.service: Deactivated successfully.'),
            journal(start, "example.service: Failed with result 'exit-code'.")]
    if recovered:
        rows.append(journal(NOW - timedelta(minutes=1), 'example.service: Deactivated successfully.'))
    return jobs.summarize(rows, CONFIG, NOW)


class CompactContract(unittest.TestCase):
    def test_separate_row_news_and_cell_boundaries(self):
        for count, accepted in [(8, True), (9, False)]:
            value = copy.deepcopy(SAMPLE)
            value['plans'] *= count
            value['rows'] = [dict(work='Работа', outcome_ids=[], plan_indexes=[i]) for i in range(count)]
            if accepted:
                daily.validate_composition(value, [])
            else:
                with self.assertRaises(ValueError): daily.validate_composition(value, [])
        for count in (3, 4):
            value = copy.deepcopy(SAMPLE); value['extras'] = ['Событие'] * count
            if count == 3: daily.validate_composition(value, [])
            else:
                with self.assertRaises(ValueError): daily.validate_composition(value, [])
        for count in (90, 91):
            value = copy.deepcopy(SAMPLE); value['rows'][0]['work'] = 'я' * count
            if count == 90: daily.validate_composition(value, [])
            else:
                with self.assertRaises(ValueError): daily.validate_composition(value, [])

    def test_one_rebuild_then_refusal_and_engine_failure_not_retried(self):
        bad = copy.deepcopy(SAMPLE); bad['rows'][0]['work'] = 'x' * 91
        for answers, accepted in [([bad, SAMPLE], True), ([bad, bad], False)]:
            with mock.patch.object(daily.subprocess, 'run', side_effect=[
                subprocess.CompletedProcess([], 0, json.dumps(x), '') for x in answers]) as run:
                if accepted: daily.compose({}, '2026-11-16')
                else:
                    with self.assertRaises(ValueError): daily.compose({}, '2026-11-16')
                self.assertEqual(run.call_count, 2)
        with mock.patch.object(daily.subprocess, 'run', return_value=subprocess.CompletedProcess([], 2, '', 'fail')) as run:
            with self.assertRaises(RuntimeError): daily.compose({}, '2026-11-16')
            self.assertEqual(run.call_count, 1)

    def test_eight_previous_steps_and_new_step_all_visible(self):
        previous = [dict(id=f'old-{i}', product='Работа', action=f'Шаг {i}', result='Итог') for i in range(8)]
        value = copy.deepcopy(SAMPLE)
        value['plans'] = [dict(product='Новая', action='Сделать', result='Готово', deadline='16.11')]
        value['outcomes'] = [dict(id=p['id'], status='получилось', detail=f'Итог {i}') for i, p in enumerate(previous)]
        value['rows'] = [dict(work=f'Работа {i}', outcome_ids=[f'old-{i}'], plan_indexes=[]) for i in range(7)]
        value['rows'][0]['outcome_ids'].append('old-7')
        value['rows'].append(dict(work='Новая', outcome_ids=[], plan_indexes=[0]))
        daily.validate_composition(value, [dict(plans=previous)])
        plain = daily.render_plain(value)
        for i in range(8): self.assertEqual(plain.count(f'Итог {i}'), 1)
        self.assertIn('в работе 1', plain)
        for ids in ([], ['old-0', 'old-0'], ['foreign']):
            bad = copy.deepcopy(value); bad['rows'][0]['outcome_ids'] = ids
            with self.assertRaises(ValueError): daily.validate_composition(bad, [dict(plans=previous)])

    def test_quiet_keeps_structured_plan_and_transfer_and_refuses_events(self):
        value = copy.deepcopy(SAMPLE)
        value.update(quiet=True, questions=[], extras=[], waiting='', outcomes=[dict(id='old', status='перенесено',
                     detail='Ждём ответа', deadline='16.11', approach='Другой источник')])
        value['rows'][0]['outcome_ids'] = ['old']
        daily.validate_composition(value, [dict(plans=[dict(id='old', **value['plans'][0])])])
        self.assertEqual(daily.render_plain(value), 'Без изменений. В работе 1, вопросов нет.')
        value['extras'] = ['Новость']
        with self.assertRaises(ValueError): daily.validate_composition(value, [])

    def test_no_long_intro_question_or_waiting_escape(self):
        for field, limit in [('intro', 150), ('waiting', 400)]:
            value = copy.deepcopy(SAMPLE); value[field] = 'x' * (limit+1)
            with self.assertRaises(ValueError): daily.validate_composition(value, [])
        value = copy.deepcopy(SAMPLE); value['questions'][0]['tradeoff'] = 'x'*181
        with self.assertRaises(ValueError): daily.validate_composition(value, [])


class JournalNews(unittest.TestCase):
    def test_long_short_recovered_and_delivered_cutoff(self):
        self.assertIn('продолжается', jobs.required_news([episode(49)], None)[0])
        self.assertEqual(jobs.required_news([episode(23)], None), [])
        self.assertEqual(jobs.required_news([episode(24)], None), [])
        recovered = episode(97, True)
        self.assertEqual(recovered['state'], 'ok')
        self.assertIn('теперь работает', jobs.required_news([recovered], (NOW-timedelta(days=1)).isoformat())[0])
        self.assertEqual(jobs.required_news([recovered], NOW.isoformat()), [])
        self.assertTrue(jobs.required_news([episode(49)], NOW.isoformat()))

    def test_recovered_episode_does_not_claim_current_health_after_new_failure(self):
        rows = [journal(NOW-timedelta(days=4), "example.service: Failed with result 'timeout'."),
                journal(NOW-timedelta(hours=2), 'example.service: Deactivated successfully.'),
                journal(NOW-timedelta(hours=1), "example.service: Failed with result 'exit-code'.")]
        state = jobs.summarize(rows, CONFIG, NOW)
        self.assertEqual(state['state'], 'failed')
        news = jobs.required_news([state], None)
        self.assertIn('новый сбой', news[0])
        self.assertNotIn('теперь работает', news[0])

    def test_source_unavailable_and_empty_are_unknown(self):
        with mock.patch.object(jobs.product_memory, 'installation', return_value={'regular_jobs':[CONFIG]}), \
             mock.patch.object(jobs.subprocess, 'run', side_effect=OSError('offline')):
            state = jobs.collect(NOW)
        self.assertEqual(state[0]['state'], 'unknown')
        self.assertIn('неизвестно', jobs.required_news(state, None)[0])
        self.assertEqual(jobs.summarize([], CONFIG, NOW)['state'], 'unknown')

    def test_multiple_jobs_bounded_and_mandatory_even_after_success(self):
        states = [dict(episode(97, True), id=str(i), title=f'Работа {i}') for i in range(5)]
        facts = jobs.required_news(states, None)
        self.assertLessEqual(len(facts), 3)
        data = copy.deepcopy(SAMPLE); data['extras'] = []
        daily.apply_job_news(data, {'regular_jobs': states})
        plain = daily.render_plain(data)
        for i in range(5): self.assertIn(f'Работа {i}', plain.split('Главное')[1].split('В работе')[0])
        self.assertFalse(data['quiet'])

    def test_real_reader_to_packet_to_composer_to_news_with_controlled_transport(self):
        rows = [journal(NOW-timedelta(days=4), "example.service: Failed with result 'exit-code'."),
                journal(NOW-timedelta(hours=1), 'example.service: Deactivated successfully.')]
        def read(argv, **kw):
            kw['stdout'].write(('\n'.join(json.dumps(x) for x in rows)).encode())
            return subprocess.CompletedProcess(argv, 0)
        with mock.patch.object(jobs.product_memory, 'installation', return_value={'regular_jobs':[CONFIG]}), \
             mock.patch.object(jobs.subprocess, 'run', side_effect=read):
            states = jobs.collect(NOW)
        answer = copy.deepcopy(SAMPLE); answer['extras']=[]
        with mock.patch.object(daily.subprocess, 'run', return_value=subprocess.CompletedProcess([],0,json.dumps(answer),'')) as model:
            data = daily.compose({'regular_jobs':states}, '2026-11-16')
        self.assertIn('last_success', model.call_args.kwargs['input'])
        self.assertIn('journal:example', model.call_args.kwargs['input'])
        self.assertIn('теперь работает', daily.render_plain(data))
        self.assertIn('теперь работает', daily.render_html(data))


if __name__ == '__main__': unittest.main()
