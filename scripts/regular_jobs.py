"""Bounded daily reading of configured systemd journals; no incident state owner.

Private installation regular_jobs entries provide id, title, unit and journal_argv.
The argv must read JSON journal history including the last successful run. No
commands or host addresses are invented here. Delivery history supplies the
cutoff for recovered incidents; ongoing incidents remain visible every day.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

import product_memory
import startup_context

MAX_BYTES = 4_000_000
MAX_SOURCES = 16


def summarize(rows: list[dict], config: dict, moment: datetime) -> dict:
    unit = config['unit']
    failure_since = None
    last_success = None
    episodes = []
    for row in sorted(rows, key=lambda row: int(row['__REALTIME_TIMESTAMP'])):
        at = datetime.fromtimestamp(int(row['__REALTIME_TIMESTAMP']) / 1_000_000, timezone.utc)
        if at > moment:
            continue
        message = row.get('MESSAGE', '')
        # Native unit terminal records, not a classifier of exception prose.
        if message.startswith(f"{unit}: Failed with result ") or message.startswith(f'Failed to start {unit} '):
            if failure_since is None:
                failure_since = at
        elif message == f'{unit}: Deactivated successfully.' or message.startswith(f'Finished {unit} '):
            if failure_since is not None:
                episodes.append({'since': failure_since.isoformat(), 'until': at.isoformat()})
                failure_since = None
            last_success = at
    if failure_since is not None:
        episodes.append({'since': failure_since.isoformat(), 'until': None})
    return {'id': config['id'], 'title': config['title'], 'source': config['source'],
            'state': 'failed' if failure_since else ('ok' if last_success else 'unknown'),
            'last_success': last_success.isoformat() if last_success else None,
            'episodes': episodes, 'observed_at': moment.isoformat()}


def collect(moment: datetime) -> list[dict]:
    sources = product_memory.installation().get('regular_jobs', [])
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
        raise ValueError('regular_jobs must be a bounded list')
    result = []
    for config in sources:
        # Configuration errors must be fixed by the installer, never look healthy.
        for key in ('id', 'title', 'unit', 'source'):
            if not isinstance(config.get(key), str) or not config[key]:
                raise ValueError(f'regular_jobs requires {key}')
        argv = config.get('journal_argv')
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            raise ValueError('regular_jobs requires journal_argv')
        try:
            with tempfile.TemporaryFile() as output:
                run = subprocess.run(argv, stdout=output, stderr=subprocess.DEVNULL, timeout=30, check=False)
                if run.returncode or output.tell() > MAX_BYTES:
                    raise ValueError('journal unavailable or oversized')
                output.seek(0)
                rows = [json.loads(line) for line in output.read().decode().splitlines() if line.strip()]
            result.append(summarize(rows, config, moment))
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            result.append({'id': config['id'], 'title': config['title'], 'source': config['source'],
                           'state': 'unknown', 'last_success': None, 'episodes': [],
                           'observed_at': moment.isoformat()})
    startup_context.require_size(startup_context.render(result), 20_000, 'regular jobs')
    return result


def required_news(jobs: list[dict], previous_at: str | None) -> list[str]:
    cutoff = datetime.fromisoformat(previous_at) if previous_at else None
    facts = []
    for job in jobs:
        if job['state'] == 'unknown':
            facts.append(f"{job['title']}: состояние неизвестно, журнал недоступен")
            continue
        now = datetime.fromisoformat(job['observed_at'])
        for episode in job['episodes']:
            start = datetime.fromisoformat(episode['since'])
            end = datetime.fromisoformat(episode['until']) if episode['until'] else None
            duration = (end or now) - start
            if duration <= timedelta(days=1) or (end and cutoff and end <= cutoff):
                continue
            days = duration.days
            facts.append(f"{job['title']}: сбой {days} дн.; " + (('теперь работает' if job['state'] == 'ok' else 'восстановлено; новый сбой') if end else 'продолжается'))
    # Multiple jobs share a news row, preserving every mandatory fact. If even
    # three bounded rows cannot hold them, composition refuses instead of hiding.
    groups = []
    for fact in facts:
        if groups and len(groups[-1] + '; ' + fact) <= 300:
            groups[-1] += '; ' + fact
        else:
            groups.append(fact)
    return groups
