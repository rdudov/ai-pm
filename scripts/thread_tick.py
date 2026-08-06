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
  - a task's recorded start condition became met, so it is startable now
  - a decision recorded on a task is still not carried out

The last two exist because the first four could not answer «что теперь можно
запускать». On 2026-08-06 task 831 named its condition in a sentence — «после
завершения прогона 830, то же рабочее дерево» — and the tick saw only «прогон
830 завершился». Nothing said what that made possible, so 831 stood forty
minutes and moved when the user asked. A condition that is a field becomes a
transition like any other.

And when the tick yields to another instance of the product owner, it leaves the
list of what it did not start on disk beside the snapshot. Yielding is right —
two children in one working tree is the collision the condition exists to
prevent — but yielding silently is how the list ends up in nobody's hands.

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
        # Startability is a state now, so it can be compared between two ticks
        # like every other state here. Without it «условие снялось» could only be
        # inferred by a reader, and readers of a background tick are the thing
        # this file does not have.
        "ready": sorted(item["id"] for item in report["ready_to_start"]),
        "decided": sorted(item["id"] for item in report["decided_not_done"]),
    }


def transitions(previous: dict, current: dict) -> list[str]:
    events = []
    for task_id in sorted(set(previous.get("live", [])) - set(current["live"])):
        events.append(f"прогон задачи {task_id} завершился")
    for task_id in current["stale"]:
        events.append(f"задача {task_id} числится running, но процесс мёртв")
    for task_id in sorted(set(current["blocked"]) - set(previous.get("blocked", []))):
        events.append(f"задача {task_id} перешла в blocked")
    # A condition that has just cleared is the transition the queue was missing.
    # It is reported on the edge, exactly like a finished run: standing in
    # «готово к запуску» is a state, becoming startable is the event.
    for task_id in sorted(set(current["ready"]) - set(previous.get("ready", []))):
        events.append(f"условие запуска задачи {task_id} выполнено — её можно запускать")
    # An unexecuted decision is reported on its edge too, so a decision taken and
    # then forgotten does not have to wait for the next unrelated event to be
    # mentioned.
    for task_id in sorted(set(current["decided"]) - set(previous.get("decided", []))):
        events.append(f"решение по задаче {task_id} записано и не исполнено")
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


def yielded(report: dict) -> dict | None:
    """What this tick did not start because another product owner is awake.

    Yielding is correct: two children in one working tree is the collision the
    start condition exists to prevent, and the tick has no way to know what the
    other instance is about to do. What was wrong is that yielding was silent —
    on 2026-08-06 the background owner stood down and the list of work it did not
    start existed only inside its own text, so nothing and nobody held it.

    So the list is written down instead. It is an observation, not a claim: who
    else was seen awake, and which tasks were startable at that moment. The
    interactive owner reads the same file the tick writes, which is the whole
    point — a timer wakes a process, never a conversation.
    """
    # Every awake owner except this very process. Excluding by thread instead
    # would have thrown away exactly the case that cost the forty minutes: the
    # second owner of 2026-08-06 was awake on the *same* direction, and it is a
    # same-direction second owner that must not put a second child into one
    # working tree.
    others = [owner for owner in report["owners_awake"] if owner["pid"] != os.getpid()]
    if not others:
        return None
    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "to": others,
        "ready_to_start": report["ready_to_start"],
        "decided_not_done": report["decided_not_done"],
        "src": "командные строки процессов в /proc и области наблюдаемого состояния треда",
    }


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

Разделы «готово к запуску» и «решено, но не исполнено» в состоянии выше — это
работа, у которой условие уже снято или решение уже принято: по ней нужен либо
запуск, либо названная причина, почему нет. Если бодрствует ещё один продакт,
уступи ему дорогу — двух детей в одном рабочем дереве быть не должно; список
того, что ты не стал запускать, уже лежит на диске в файле состояния треда, так
что пересказывать его в тексте не нужно.

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
    # Written whether or not an agent is woken, and before it is: the list of
    # work standing ready has to survive a tick that decides to say nothing, and
    # it must not depend on what the woken agent chose to write down.
    standing = {
        "ready_to_start": report["ready_to_start"],
        "decided_not_done": report["decided_not_done"],
        "yielded_to_awake_owner": yielded(report),
    }

    if args.dry_run:
        print(json.dumps({"events": events, "snapshot": current, **standing},
                         ensure_ascii=False, indent=2))
        return 0

    state_path.write_text(json.dumps(
        {"thread": args.thread, "updated_at": now, "snapshot": current,
         "last_events": events, **standing},
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
