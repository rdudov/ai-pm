#!/usr/bin/env python3
"""One wake-up of a product thread.

Cheap by construction: it compares the current observable state with the last
snapshot and exits silently when nothing changed. Only a real transition costs
an agent run, and that run starts from disk state, never from a transcript.

Transitions that wake the product owner:
  - a live run finished since the last tick
  - a run claims `running` while its process is gone
  - a task entered `blocked`
  - a repository the thread owns moved to a new commit

Usage: thread_tick.py <thread> [--dry-run] [--force]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from thread_state import HOME, REPO, build  # noqa: E402

STATE_DIR = HOME / "state" / "threads"
CLAUDE_BIN = "/usr/local/bin/claude"
COMPANION = Path("/opt/projects/companion-agent")
MAIL_TO = "rdudov@gmail.com"


def snapshot(report: dict) -> dict:
    return {
        "live": sorted(item["id"] for item in report["live_runs"]),
        "blocked": sorted(item["id"] for item in report["needs_attention"] if item["status"] == "blocked"),
        "stale": sorted(item["id"] for item in report["live_runs"] if item["run"]["stale_running"]),
        "heads": {repo["repo"]: repo.get("head", "") for repo in report["repos"] if repo["present"]},
    }


def transitions(previous: dict, current: dict) -> list[str]:
    events = []
    for task_id in sorted(set(previous.get("live", [])) - set(current["live"])):
        events.append(f"прогон задачи {task_id} завершился")
    for task_id in current["stale"]:
        events.append(f"задача {task_id} числится running, но процесс мёртв")
    for task_id in sorted(set(current["blocked"]) - set(previous.get("blocked", []))):
        events.append(f"задача {task_id} перешла в blocked")
    for repo, head in current["heads"].items():
        if previous.get("heads", {}).get(repo, head) != head:
            events.append(f"{Path(repo).name}: новый коммит {head}")
    return events


def send_mail(subject: str, body: str) -> None:
    """Deliver the verdict to the mailbox the user actually reads.

    The telegram path below needs a bot token in the environment, and the
    systemd unit that runs this tick has none: on 2026-08-04 the wake-up for
    the finished deep research task produced a full verdict at 01:17 and then
    dropped it on the floor, so the user learned nothing until they asked.
    Mail is the channel that is provably wired in both directions, so the
    verdict goes there first and telegram stays a bonus.
    """
    script = COMPANION / "skills" / "gmail-client" / "scripts" / "send_email.py"
    python = COMPANION / ".venv" / "bin" / "python"
    if not script.is_file() or not python.is_file():
        return
    try:
        subprocess.run(
            [str(python), str(script), "--to", MAIL_TO, "--subject", subject, "--body", body],
            cwd=str(COMPANION), capture_output=True, text=True, timeout=180, check=False,
        )
    except Exception:
        return


def notify(text: str) -> None:
    token = os.environ.get("COMPANION_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("COMPANION_BOT_CHAT_ID") or os.environ.get("TELEGRAM_USER_ID")
    if not token or not chat_id:
        return
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=payload, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
    except Exception:
        return


def prompt(report: dict, events: list[str]) -> str:
    return f"""Ты продакт-агент на фоновом пробуждении треда «{report['title']}».

Произошло с прошлого пробуждения:
{chr(10).join('- ' + event for event in events)}

Наблюдаемое состояние треда (собрано механически, не со слов исполнителя):
{json.dumps(report, ensure_ascii=False, indent=2)}

Сделай ровно четыре шага и ничего сверх них:
1. Прочитай артефакты только тех задач, которых касаются события выше.
2. Реши: принять результат, вернуть на доработку, запустить следующий шаг или
   спросить пользователя. Правило кросс-ревью: работу Codex ревьюит Claude и
   наоборот; на замечания сначала анализ и план, потом правки, потом повторное
   ревью.
3. Допиши одну строку в раздел «В работе» записи продукта
   `/opt/projects/product-owner/products/<продукт>/product.md` и обнови состояние пользовательских путей, если оно
   изменилось по артефакту, а не по прозе исполнителя.
4. Верни короткий текст для пользователя в формате вердикта продакта: что теперь
   может пользователь, цена, что осталось, и строка «Риск/долг», если в
   verification есть GAP. Если сказать нечего — верни ровно слово SILENT.

Не запускай новых детей в write-режиме без явного разрешения пользователя.
Не читай транскрипты детей: только артефакты задач и наблюдаемое состояние.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("thread")
    parser.add_argument("--dry-run", action="store_true", help="показать события и выйти")
    parser.add_argument("--force", action="store_true", help="разбудить агента даже без событий")
    args = parser.parse_args()

    report = build(args.thread)
    current = snapshot(report)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path = STATE_DIR / f"{args.thread}.json"
    previous = {}
    if state_path.is_file():
        try:
            previous = json.loads(state_path.read_text()).get("snapshot", {})
        except (json.JSONDecodeError, OSError):
            previous = {}

    events = transitions(previous, current) if previous else ["первый запуск треда"]
    now = datetime.now(timezone.utc).isoformat()

    if args.dry_run:
        print(json.dumps({"events": events, "snapshot": current}, ensure_ascii=False, indent=2))
        return 0

    state_path.write_text(json.dumps(
        {"thread": args.thread, "updated_at": now, "snapshot": current, "last_events": events},
        ensure_ascii=False, indent=2,
    ))

    if not events and not args.force:
        return 0

    # Same trust level as the user's own `claude-full` session: sibling projects
    # under /opt/projects must be reachable or the tick is blind to every thread
    # but this one. IS_SANDBOX=1 is what lets Claude Code skip permissions as
    # root. The prompt goes through stdin because `--add-dir` is variadic and
    # would otherwise swallow a trailing positional prompt.
    environment = {**os.environ, "IS_SANDBOX": "1"}
    result = subprocess.run(
        [CLAUDE_BIN, "--print", "--add-dir", "/opt/projects",
         "--dangerously-skip-permissions"],
        input=prompt(report, events), env=environment,
        capture_output=True, text=True, cwd=HOME, timeout=1800,
    )
    message = (result.stdout or "").strip()
    if result.returncode != 0:
        failure = f"[{args.thread}] пробуждение треда не отработало: {(result.stderr or '')[:300]}"
        notify(failure)
        send_mail(f"Продакт: пробуждение треда «{report['title']}» не отработало", failure)
        return 1
    if message and message != "SILENT":
        notify(f"[{report['title']}]\n{message}")
        send_mail(f"Продакт: {report['title']}", message)
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
