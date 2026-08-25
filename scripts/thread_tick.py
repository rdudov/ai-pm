#!/usr/bin/env python3
"""One wake-up of a product thread.

Cheap by construction: it compares the current observable state with the last
snapshot and wakes the product owner only when there is something to wake them
for. That run starts from disk state, never from a transcript.

Transitions that wake the product owner:
  - a live run finished since the last tick
  - a run claims `running` while its process is gone
  - a task entered `blocked`
  - a repository the thread owns moved to a new commit
  - a task's recorded start condition became met, so it is startable now
  - a decision recorded on a task is still not carried out

Those two last ones exist because the first four could not answer «что теперь
можно запускать». On 2026-08-06 task 831 named its condition in a sentence —
«после завершения прогона 830, то же рабочее дерево» — and the tick saw only
«прогон 830 завершился». Nothing said what that made possible, so 831 stood
forty minutes and moved when the user asked. A condition that is a field becomes
a transition like any other.

A durable goal is read before any of that and adds two more of each. Turning on
reinforced control, and a goal changing state, are transitions; a goal standing
with nothing live doing its work is a state. The goal itself lives in the product
store on disk precisely because this process does not survive its own wake-up:
the timer raises a new one every interval and the router may hand it to the other
family, so «что обещано пользователю и где это стоит» cannot live in a session.

And three *states* wake it too, because a transition is not the only kind of news:

  - идёт простой: no live run of this direction while its queue is not empty
  - «сделано, но не доставлено» стоит дольше разумного порога
  - долговечная цель стоит: ничего живого не занимается ни основной задачей,
    ни её корректирующими

The first two are the same defect seen from two sides, and both were invisible
here by construction. A direction with ten startable tasks and no live run produces no
edge at all, so `if not events: return 0` made it mute forever: on 2026-08-07 all
four timers fired at 16:06:56, sixteen tasks stood startable across the contour,
nothing was running, and neither a letter nor a line on the board appeared.
The user asked why nothing was being done while the board showed nothing in
progress. Standing idle with work available *is* the event.

Chatter is answered with a rate, not with silence: the same reminder is not
repeated inside `PRODUCT_OWNER_IDLE_REMIND_SECONDS`, and a queue that changed is
news again immediately.

Whatever the tick decides, it records what it saw and what came of it in the
direction's own state file. That record — not the prose of a woken agent — is
what the board reads to answer «когда продакт проверял в прошлый раз и чем та
проверка кончилась». When the *next* check falls is not written here: this
process is the service paired with the timer, so it would always be recording
the one instant systemd holds that timer unarmed. The board asks systemd itself,
as it is built (`process_map_state.next_check`).

Usage: thread_tick.py <thread> [--dry-run] [--force]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_budget  # noqa: E402
import daily_standup  # noqa: E402
import goal_session  # noqa: E402  (обращения только в рантайме: импорт взаимный)
import outbound  # noqa: E402
import plain_russian  # noqa: E402
import product_goal  # noqa: E402
import product_memory  # noqa: E402
import startup_context  # noqa: E402
import runner_contract  # noqa: E402
from process_map_schema import run_entrypoint  # noqa: E402
from process_map_state import RUNNER_SCRIPTS, tunable  # noqa: E402
from process_map_state import THREAD_STATE as STATE_DIR  # noqa: E402
from thread_state import HOME, REPO, build  # noqa: E402

# The push channel belongs to the installation, not to the core: it is the
# personal assistant's own bot transport, and an installation that has none is a
# working installation whose news arrives by mail and on the board. Only a
# channel that exists is then required to work — see `require_notification_profile`.
TELEGRAM_SCRIPTS = REPO / "skills" / "telegram-client" / "scripts"
if str(TELEGRAM_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(TELEGRAM_SCRIPTS))
try:
    from bot_transport import resolve_bot_target, send_bot_message  # type: ignore  # noqa: E402
except ImportError:  # no push channel installed beside this product owner
    resolve_bot_target = send_bot_message = None  # type: ignore[assignment]

CLAUDE_PRODUCT_OWNER = HOME / "scripts" / "claude_product_owner.py"
# The letter leaves through the task system's own mail client, and goes to the
# address this installation declares. All three are conditions of the same door,
# named here rather than found inside the sender, so a test can state them and
# an installation missing one can be told which. No address, no client, no
# interpreter — no mail door, which the ledger already handles: a letter that
# could not be sent stays held rather than being written down as said.
MAIL_TO = product_memory.mail_to()
MAIL_SCRIPT = REPO / "skills" / "gmail-client" / "scripts" / "send_email.py"
MAIL_PYTHON = REPO / ".venv" / "bin" / "python"


def route_diagnostics(stderr: str) -> list[str]:
    """Keep the router's selected route without replaying model stderr."""
    return [line for line in stderr.splitlines()
            if line.startswith("product-owner: route selected;")]


# How often the same standing reminder may be repeated. The tick itself runs
# every twenty minutes, so this is the frequency of the *reminder*, not of the
# observation: a queue that has not moved is said once an hour, and a queue that
# moved is news at the next tick.
IDLE_REMIND_SECONDS = tunable("PRODUCT_OWNER_IDLE_REMIND_SECONDS", 3600)
# How often a durable goal standing with nothing live doing its work may be said
# again. Shorter than the idle reminder on purpose: a goal under reinforced
# control is problematic work the user asked to be *driven*, so one tick with
# nothing happening on it is already news. One wake-up interval, so it is said
# every tick the goal stands and never twice for the same tick.
GOAL_REMIND_SECONDS = tunable("PRODUCT_OWNER_GOAL_REMIND_SECONDS", 1200)
# How long a finished task may hold a document nobody was shown before that
# becomes an event. The user saw one stand over forty minutes, and then several
# at once; half an hour is inside the span they were already unhappy about.
UNDELIVERED_SECONDS = tunable("PRODUCT_OWNER_UNDELIVERED_SECONDS", 1800)
# How long the woken owner may take, and how long the two side channels may.
WAKE_TIMEOUT = tunable("PRODUCT_OWNER_WAKE_TIMEOUT_SECONDS", 1800)
MAIL_TIMEOUT = tunable("PRODUCT_OWNER_MAIL_TIMEOUT_SECONDS", 180)
# Above this share of the weekly Codex window, heavy work does not start — the
# same threshold `codex_budget.py` prints its verdict against.
CODEX_HEAVY_PERCENT = tunable("PRODUCT_OWNER_CODEX_HEAVY_PERCENT", 80)
# Письма, которые при неудачной отправке не уходят в общую очередь проактивных
# новостей, и что о каждом из них записано. Ответ принадлежит своей побудке,
# оперативка — своему утру, а письмо о принятой инструкции пересобирается
# следующим тиком: цифра в нём считается с байтов файла в минуту отправки, и
# полежавший в очереди текст назвал бы редакцию, которой на диске может уже не
# быть.
FAILED_SEND = {
    "reply": "отправка ответа не удалась; повтор не выполнен",
    "daily": "отправка оперативки не удалась; следующий утренний тик повторит",
    "instruction": ("письмо о принятой инструкции не ушло; следующий тик соберёт "
                    "его заново с текущим sha256"),
}


def process_observation(report: dict) -> dict:
    """Normalized availability of the detached-process observation.

    Reports written before task 839 had no marker and did contain an actual
    observation, so absence remains the compatible `available` case.
    """
    observation = report.get("long_lived_processes_observation") or {}
    return {
        "available": observation.get("available", True) is True,
        "reason": observation.get("reason"),
    }


def snapshot(report: dict, previous: dict | None = None) -> dict:
    observation = process_observation(report)
    processes = sorted(({
        "pid": item["pid"], "since": item["since"], "task": item["task"],
        "command": item["command"], "repo": item["repo"],
        "duplicate_count": item["duplicate_count"],
    } for item in report.get("long_lived_processes", [])),
                       key=lambda item: (item["task"] or 0, item["command"],
                                         item["pid"]))
    if not observation["available"] and previous is not None:
        # Unobservable is not empty. Retain the last externally observed
        # identities until the owning registry can distinguish registered runs
        # from detached work again.
        processes = list(previous.get("long_lived", []))
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
        # Not a transition and never was: what can be picked up simply *stands*,
        # and it standing next to zero live runs is the whole of the idle event.
        # It is in the snapshot so the reminder can tell «та же очередь» from «в
        # очереди что-то изменилось» without a second file to keep in step.
        "pickup": sorted(item["id"] for item in report["can_pick_up"]),
        # Очередь плана — тоже стоящее состояние, и она движется чаще прочих:
        # без неё «та же очередь» осталось бы правдой после того, как владелец
        # порядка переставил работы местами.
        "plan_queue": [item["id"] for item in (report.get("queued_by_plan") or [])],
        "undelivered": sorted(item["id"] for item in report["undelivered"]),
        # A process can outlive the run record and even its completed task. Its
        # identity belongs in the tick's state so appearance, cleanup and a new
        # duplicate are edges rather than facts seen only by a manual CLI call.
        "long_lived": processes,
        "long_lived_observation": observation,
    }


def persisted_process_inventory(report: dict, stored: dict) -> list[dict]:
    """The full inventory to persist without converting unknown into empty."""
    if process_observation(report)["available"]:
        return report.get("long_lived_processes", [])
    return stored.get("long_lived_processes", [])


def transitions(previous: dict, current: dict) -> list[str]:
    events = []
    for task_id in sorted(set(previous.get("live", [])) - set(current["live"])):
        events.append(f"прогон задачи {task_id} завершился")
    for task_id in current["stale"]:
        events.append(f"задача {task_id} числится running, но процесс мёртв")
    for task_id in sorted(set(current["blocked"]) - set(previous.get("blocked", []))):
        events.append(f"задача {task_id} перешла в blocked")
    observation = current.get(
        "long_lived_observation", {"available": True, "reason": None})
    previous_observation = previous.get(
        "long_lived_observation", {"available": True, "reason": None})
    if not observation["available"] and previous_observation.get("available", True):
        events.append(
            "опись долгоживущих процессов недоступна: "
            f"{observation.get('reason') or 'причина не указана'}")
    old_processes = {(item.get("pid"), item.get("since")): item
                     for item in previous.get("long_lived", [])}
    new_processes = {(item.get("pid"), item.get("since")): item
                     for item in current.get("long_lived", [])}
    for identity in sorted(set(new_processes) - set(old_processes)):
        item = new_processes[identity]
        events.append(
            f"у завершённой задачи {item['task']} живёт процесс {item['command']} "
            f"(pid {item['pid']})")
    for identity in sorted(set(old_processes) - set(new_processes)):
        item = old_processes[identity]
        events.append(
            f"долгоживущий процесс задачи {item['task']} {item['command']} "
            f"(pid {item['pid']}) больше не жив")
    old_duplicates = {(item["task"], item["repo"], item["command"]):
                      item.get("duplicate_count", 1)
                      for item in old_processes.values()}
    new_duplicates = {(item["task"], item["repo"], item["command"]):
                      item.get("duplicate_count", 1)
                      for item in new_processes.values()}
    for signature, count in sorted(new_duplicates.items()):
        if count > 1 and count > old_duplicates.get(signature, 1):
            events.append(
                f"ДУБЛЬ: процесс задачи {signature[0]} {signature[2]} поднят {count} раза")
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


def queue(report: dict) -> dict:
    """How much work of each kind is standing, and none of it is a transition.

    One place, so the event, the reminder's «та же очередь» test and the line the
    board prints cannot disagree about what «непустая очередь» meant.
    """
    return {
        "live": len(report["live_runs"]),
        "pickup": len(report["can_pick_up"]),
        "ready": len(report["ready_to_start"]),
        "decided": len(report["decided_not_done"]),
        "undelivered": len(report["undelivered"]),
        "waiting_user": len(report["waiting_user"]),
    }


def startable(report: dict) -> int:
    """Work this direction could put a child on right now.

    «Можно подхватить», «готово к запуску» and «решено, но не исполнено» are the
    three areas that say nothing observable is holding the task.

    Очередь плана считается здесь же, и это не расширение понятия, а его
    восстановление: раньше эти задачи стояли в «можно подхватить» и считались
    вот тут — они просто не были названы очередью, потому что очередь выводилась
    из наблюдения. Работа с назначенным местом в очереди — не простой, но и не
    затор: живых прогонов нет, а работа есть, и разбудить владельца надо. Что
    план держит своим словом или не называет вовсе, здесь не считается: это
    работа, которую пользователь остановил или про которую решения нет.
    """
    return (len(report["can_pick_up"]) + len(report["ready_to_start"])
            + len(report["decided_not_done"]) + len(report.get("queued_by_plan") or []))


def overdue_undelivered(report: dict) -> list[dict]:
    """Finished tasks whose document has waited longer than the threshold."""
    return [item for item in report["undelivered"]
            if (item["age_seconds"] or 0) >= UNDELIVERED_SECONDS]


def repeatable(previous: dict | None, signature: str, now: datetime,
               interval: int = IDLE_REMIND_SECONDS) -> bool:
    """Whether a standing reminder may be said again.

    Two guards, and the second one is why this is a frequency rather than a mute
    button. The same queue is not repeated inside `IDLE_REMIND_SECONDS`; a queue
    that changed is news at the very next tick. A missing or unreadable previous
    record means «say it» — the failure mode this whole file is repairing is
    silence, so the doubtful case is loud.
    """
    if not previous or previous.get("signature") != signature:
        return True
    try:
        last = datetime.fromisoformat(previous["at"])
    except (KeyError, TypeError, ValueError):
        return True
    return (now - last).total_seconds() >= interval


def standing_events(report: dict, current: dict, stored: dict,
                    moment: datetime) -> tuple[list[str], dict | None, dict | None]:
    """The two states that wake the owner without being transitions.

    Idleness with work available is the first, and it is the whole of the defect
    the user named: `transitions()` can only speak on an edge, and a direction
    with ten startable tasks and no live run has no edge to offer. It stood mute
    forever, which on 2026-08-07 read as «панель показывает, что в работе ничего
    нет… тогда почему ничего не делаешь?».

    A document nobody was shown, standing longer than the threshold, is the
    second. That area has been on the board since 783 and could wake nobody, so
    the user watched one entry pass forty minutes and then several appear at once.

    Both are rate-limited rather than silenced, and the signature is the queue
    itself: the same queue is not repeated inside `IDLE_REMIND_SECONDS`, and a
    queue that moved is news at the very next tick.
    """
    now = moment.isoformat()
    events = []

    idle_signature = json.dumps(current["pickup"] + current["ready"] + current["decided"]
                                + current.get("plan_queue", []))
    idle_reminder = stored.get("idle_reminder")
    if not report["live_runs"] and startable(report) and repeatable(
            idle_reminder, idle_signature, moment):
        events.append(
            f"идёт простой: живых прогонов нет, а к запуску {startable(report)} "
            f"(в очереди плана {len(report.get('queued_by_plan') or [])}, "
            f"можно подхватить {len(report['can_pick_up'])}, "
            f"готово к запуску {len(report['ready_to_start'])}, "
            f"решено и не исполнено {len(report['decided_not_done'])})")
        idle_reminder = {"at": now, "signature": idle_signature}

    overdue = overdue_undelivered(report)
    held_signature = json.dumps(sorted(item["id"] for item in overdue))
    held_reminder = stored.get("undelivered_reminder")
    if overdue and repeatable(held_reminder, held_signature, moment):
        events.append(
            f"«сделано, но не доставлено» стоит дольше {UNDELIVERED_SECONDS // 60} мин: задачи "
            + ", ".join(str(item["id"]) for item in overdue))
        held_reminder = {"at": now, "signature": held_signature}
    return events, idle_reminder, held_reminder


def goal_watch(thread: str, report: dict, stored: dict, moment: datetime) -> dict:
    """The durable goals of this direction: read, escalated and made loud.

    Read *before* anything is decided, because that is the whole difference
    between a mode and an intention. A timer raises a new process every twenty
    minutes and the router may hand it to the other family, so nothing a previous
    wake-up understood survives except what is on disk.

    Three things happen here and none of them needs a human. The deviations
    observable in the main task's own artifacts are collected and recorded, so
    reinforced control switches on by observation rather than by somebody
    noticing. A goal that turned on control since the last tick is a transition
    like any other. And a goal with nothing live doing its work is a standing
    state exactly like идёт простой — rate-limited rather than silenced, because
    a goal standing still *is* the news.

    A store that cannot be read is said out loud rather than treated as «целей
    нет»: an unreadable goal is the case where silence costs the most.
    """
    previous = {str(goal.get("id")): goal for goal in stored.get("goals") or []}
    try:
        for goal in product_goal.active(thread):
            product_goal.apply_observed(goal["id"])
        panel = product_goal.panel(thread)
        waiting = product_goal.standing(
            thread, [item["id"] for item in report["live_runs"]])
    except (product_goal.GoalError, OSError, ValueError) as error:
        return {"transitions": [f"долговечные цели направления не читаются: {error}"],
                "standing": [], "reminder": stored.get("goal_reminder"), "panel": [],
                "objects": []}

    transitions_seen = []
    for goal in panel:
        was = previous.get(str(goal["id"]))
        if goal["control"] == "reinforced" and (
                was is None or was.get("control") != "reinforced"):
            codes = ", ".join(sorted({item["code"] for item in goal["signals"]}))
            transitions_seen.append(
                f"цель {goal['id']} переведена в усиленный контроль: {codes}")
        if was is not None and was.get("state") != goal["state"]:
            transitions_seen.append(
                f"цель {goal['id']} перешла из {was.get('state')} в {goal['state']}")

    reminder = stored.get("goal_reminder")
    standing_now = []
    signature = json.dumps(waiting, ensure_ascii=False)
    if waiting and repeatable(reminder, signature, moment, GOAL_REMIND_SECONDS):
        standing_now = waiting
        reminder = {"at": moment.isoformat(), "signature": signature}
    # Сами стоячие цели, а не только фразы о них. Частота повторения решает, что
    # сказать пользователю; обязательный исход пробуждения решается по факту
    # стояния, иначе замолчавший повтор снимал бы и проверку.
    live_ids = {item["id"] for item in report["live_runs"]}
    objects = [goal for goal in panel
               if goal.get("waiting_on") and not set(goal["waiting_on"]) & live_ids]
    return {"transitions": transitions_seen, "standing": standing_now,
            "reminder": reminder, "panel": panel, "objects": objects}


def session_leads(session: dict, standing_goals: list[dict]) -> tuple[bool, list[str]]:
    """Уступает ли тик направление сессии — и что записать, когда не уступает.

    Уступка — самое дорогое решение этого пробуждения: пока она стоит, тик
    сознательно не поднимает своего продакта. Поэтому она берётся из одного поля
    watchdog'а, который единственный наблюдал сессию, а не собирается здесь из
    признаков. Раньше собиралась: успешно принятый запрос `systemd-run` считался
    за ведущую сессию, дочерняя единица видела маршрут на Codex и выходила, и при
    активной усиленной цели направление оставалось без продакта вовсе.

    Вторая половина того же — обратная. Если вести цель сессии некем, вести её
    обязан продакт тика, и обязан на этом же пробуждении, а не когда совпадёт
    частота напоминания: иначе стоячая цель ждала бы двадцать минут ради того же
    решения, а обязательный пост-контроль судил бы пробуждение, которого не было.
    """
    if session.get("mode") != "session" or session.get("holds"):
        return bool(session.get("holds")), []
    if not standing_goals:
        return False, []
    return False, ["цель " + ", ".join(str(goal["id"]) for goal in standing_goals)
                   + " осталась без непрерывной сессии: " + str(session.get("detail"))
                   + " — ведёт продакт тика"]


def codex_window() -> dict | None:
    """The weekly Codex window, from the CLI's own session records."""
    try:
        return codex_budget.latest()
    except Exception:
        return None


def idle_reasons(report: dict, standing: dict) -> list[dict]:
    """Why this direction may be standing still, in the words the user named.

    «Занят другой продакт, исчерпан бюджет, занято рабочее дерево, ждём ответа
    пользователя, нет проверяющего» — each one observed, or not stated. And when
    nothing observable explains it, that is said out loud too: «везде нули» with
    no reason is the defect, so the absence of a reason is itself the finding and
    not an empty list somebody has to notice.
    """
    reasons = []
    others = (standing.get("yielded_to_awake_owner") or {}).get("to") or []
    if others:
        who = ", ".join(f"{o['kind']} «{o['thread'] or 'консоль'}»" for o in others)
        reasons.append({
            "code": "awake_owner",
            "text": f"занят другой продакт: {who} — он может занять то же рабочее дерево",
            "src": "командные строки и рабочие каталоги процессов в /proc",
        })
    busy = [item for item in report["live_runs"]
            if (item["run"] or {}).get("process_alive")]
    if busy:
        reasons.append({
            "code": "worktree_busy",
            "text": "занято рабочее дерево: живёт прогон "
                    + ", ".join(str(item["id"]) for item in busy),
            "src": "pid и стартовый тик ядра из .runner/runner.json",
        })
    window = codex_window()
    if window and window["used_percent"] >= CODEX_HEAVY_PERCENT:
        reasons.append({
            "code": "codex_budget",
            "text": f"исчерпан бюджет: недельное окно Codex израсходовано на "
                    f"{window['used_percent']}%, сброс {window['resets_at']}. "
                    "Пара «автор — проверяющий» жёсткая, поэтому по работе Claude "
                    "проверяющего сейчас нет",
            "src": f"снимок rate_limits в сессии Codex {window['observed_from']}",
        })
    # The only reason on this list tied to particular tasks, and therefore the
    # only one that can be said about the wrong ones. «Занят другой продакт»,
    # «занято рабочее дерево» and «исчерпан бюджет» hold the whole direction, so
    # they explain standing still whatever is in the queue. A question standing
    # on task 827 holds task 827 and nothing else: with nine free tasks next to
    # it, printing it as the reason for idleness explains the nine by the tenth.
    # It also cost the honest answer — `none_observed` below stands under `if not
    # reasons`, so a single open question silenced «значит запускать надо» — and
    # went into the wake-up prompt, handing the woken owner a ready excuse made
    # of somebody else's task.
    if report["waiting_user"] and not startable(report):
        waiting = ", ".join(str(item["id"]) for item in report["waiting_user"])
        reasons.append({
            "code": "waiting_user",
            "text": f"свободной работы нет, а что стоит — стоит на ответе пользователя: "
                    f"задачи {waiting}",
            "src": "область «ждёт решения пользователя» наблюдаемого состояния треда, "
                   "сверенная с пустыми «можно подхватить», «готово к запуску» и "
                   "«решено, но не исполнено»",
        })
    # And the case the user actually wrote in about. It is only a finding when
    # the direction really is standing still with work available: on a direction
    # that has nothing to start, «значит запускать надо» would be a false claim,
    # and an empty list there is the honest answer.
    if not reasons and not report["live_runs"] and startable(report):
        reasons.append({
            "code": "none_observed",
            "text": "причина простоя не наблюдается: ни другого продакта, ни занятого "
                    "дерева, ни исчерпанного бюджета, ни вопроса к пользователю — "
                    "значит запускать надо",
            "src": "тот же снимок треда, в котором ни одна из известных причин не сработала",
        })
    return reasons


def started_runs(before: dict, after: dict | None) -> list[int]:
    """Runs that were not live before the wake-up and are live after it."""
    return sorted(set((after or {}).get("live", [])) - set(before["live"]))


def outcome(before: dict, after: dict | None, woke: bool, report: dict,
            session: dict | None = None) -> str:
    """What the check came to, in ordinary words, from what was observed.

    Never from the text the woken owner returned. The owner's own account of what
    it did is exactly the prose the board is not allowed to believe: what is
    printed here is the difference between the live runs before the wake-up and
    the live runs after it.
    """
    if not woke:
        # Не будиться, потому что направление уже ведёт непрерывная сессия, — это
        # не то же самое, что не будиться, потому что новостей нет. Смешать их
        # значило бы показать пользователю тишину там, где идёт работа.
        if session and session.get("mode") == "session":
            if session.get("live"):
                # Имя сессии в эту фразу не входит: UUID написан машине и ни на
                # один вопрос доски не отвечает, а искать его есть где — та же
                # запись несёт `goal_session.session.id`, и `check.src` называет
                # файл. Приклеивать его к собственной русской фразе — тот же
                # дефект, что доска уже сняла у причины затора.
                return ("не будился: направление ведёт непрерывная сессия "
                        f"(ходов {session.get('turns')})")
            if session.get("recovered"):
                return ("не будился: watchdog поднял непрерывную сессию заново — "
                        + str(session.get("recovery_reason")))
            if session.get("handover"):
                # Сессии нет, и уступать было некому: направление ведёт этот
                # тик. Будить было не за чем — стоячая цель разбудила бы его
                # выше, — но «сессия не ведёт» и «новостей нет» на доске должны
                # читаться по-разному.
                return ("не будился: новостей нет; непрерывной сессии нет — "
                        + str(session.get("detail")))
        return "не будился: ни событий, ни простоя при доступной работе"
    started = started_runs(before, after)
    if started:
        return f"запустил {len(started)} — задачи " + ", ".join(str(i) for i in started)
    if startable(report):
        return f"не нашёл, что запустить, хотя работы к запуску {startable(report)}"
    if report["waiting_user"]:
        return "жду ответов на вопросы пользователю"
    return "запускать нечего: свободной работы в очереди нет"


def send_mail(subject: str, body: str, *,
              reply_to_message_id: str | None = None,
              attachments: list[str] | None = None,
              raw_message: bytes | None = None) -> str | bool | None:
    """Put one letter in the mailbox the user actually reads.

    The push path needs a bot token in the environment, and the systemd unit
    that runs this tick has none: one night's wake-up produced a full verdict
    and then dropped it on the floor, so the user learned nothing until they
    asked. Mail is the channel that is provably wired in both directions, so
    the verdict goes there first and the push stays a bonus.

    Whether a letter *should* go is not decided here — `outbound.decide` owns
    that, and `deliver` below is the only caller. What is decided here is
    whether it went, which the ledger needs: a send that failed must stay held
    rather than be written down as said. On success the Gmail message id is
    returned when the sender prints it; `True` preserves the older success
    contract if that receipt is absent.
    """
    if not MAIL_TO or not MAIL_SCRIPT.is_file() or not MAIL_PYTHON.is_file():
        return False
    if raw_message is not None and (reply_to_message_id or attachments):
        raise ValueError("raw_message owns its MIME structure and cannot be combined")
    command = [str(MAIL_PYTHON), str(MAIL_SCRIPT), "--to", MAIL_TO]
    if raw_message is not None:
        command += ["--raw-message", "-"]
    else:
        command += ["--body", body]
    # A finished report is delivered only when the user can open it. An
    # approved research report once sat on disk for fourteen hours because the
    # only door mail leaves through could carry text and nothing else, and the
    # user had to ask where the reports they were told about actually were.
    for path in attachments or []:
        command += ["--attach", str(path)]
    if raw_message is not None:
        pass
    elif reply_to_message_id:
        command += ["--reply-to-message-id", reply_to_message_id]
    else:
        command += ["--subject", subject]
    try:
        result = subprocess.run(
            command, input=raw_message,
            cwd=str(REPO), capture_output=True, text=raw_message is None,
            timeout=MAIL_TIMEOUT, check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    stdout = (result.stdout or b"").decode() if isinstance(result.stdout, bytes) else (result.stdout or "")
    receipt = re.search(r"Message ID:\s*([A-Za-z0-9_-]+)", stdout)
    # A successful upload must never be retried merely because a future sender
    # changed its human-readable receipt. The string is evidence when present;
    # True preserves the older success contract without risking a duplicate.
    return receipt.group(1) if receipt else True


def deliver(thread: str, kind: str, subject: str, body: str,
            report: dict | None, moment: datetime, chat: dict | None = None,
            *, reply_to_message_id: str | None = None,
            attachments: list[str] | None = None,
            raw_message: bytes | None = None,
            names_instructions: list[dict] | None = None) -> dict:
    """The one door mail leaves this contour through.

    The gate is on this side only, and everything it turns away is still on the
    board and in the gateway journal. «Прогон стартовал», «прогон закончился»,
    «репозиторий двинулся» are not letters. Of what does leave through this
    door, Telegram sees only a letter that asks the user something — see
    `announce` and `notify` for the user's words and the measurement behind that
    boundary.

    A failed proactive send is held, not recorded as sent: the ledger's whole
    worth is that it says what the user was told. A failed reply stays an
    explicit failure for its mail-wake owner instead of entering the proactive
    merge queue.

    `names_instructions` — редакции инструкции внешнему исполнителю, которые это
    письмо называет путём и полным sha256. Заявлено вызывающим, а не угадано
    здесь по прозе или по соседству: это единственная связь между готовым файлом
    и конкретным сообщением, и она в подписи. Названными редакции становятся
    только после успешной отправки — см. `outbound.instruction_letter`.
    """
    if (kind == "reply") != bool(reply_to_message_id):
        raise ValueError("kind='reply' and reply_to_message_id must be supplied together")
    if kind != "verdict":
        chat = outbound.no_chat()
    elif chat is None:
        chat = outbound.heard_in_chat(moment)
    with outbound.Ledger() as ledger:
        entry = ledger.thread(thread)
        decision = outbound.decide(thread, kind, subject, body, report or {},
                                   moment, entry, chat)
        delivered = None
        message_id = None
        if names_instructions and not outbound.unnamed_instructions(
                entry, names_instructions):
            # Пока это письмо собиралось, редакцию назвал другой тик того же
            # направления: реестр читается под тем же замком, под которым
            # пишется, и решает он, а не наблюдение минутной давности.
            decision = {**decision, "action": "drop", "flush": [],
                        "reason": "эту редакцию пользователю уже называли"}
        if decision["action"] == "send":
            mail_options = {"reply_to_message_id": reply_to_message_id,
                            "attachments": attachments}
            if raw_message is not None:
                mail_options["raw_message"] = raw_message
            send_result = send_mail(subject, decision["body"], **mail_options)
            delivered = bool(send_result)
            message_id = send_result if isinstance(send_result, str) else None
            if not delivered:
                if kind in FAILED_SEND:
                    # A reply may not ride out later as proactive news. Keep the
                    # failure explicit for the mail-wake owner to handle; putting
                    # it in generic pending would violate the very reply boundary
                    # this kind establishes. The morning letter and the letter
                    # about an accepted instruction are owed to their own moment
                    # in the same way — see `FAILED_SEND`.
                    decision = {**decision, "action": "fail",
                                "reason": FAILED_SEND[kind],
                                "body": decision["raw_body"], "flush": []}
                else:
                    # Held as it was written, not as it was merged: what
                    # accumulated is still in `pending` because nothing was
                    # flushed, and holding the merged text would put every one
                    # of those items in twice.
                    decision = {**decision, "action": "hold",
                                "reason": "отправка не удалась, письмо ждёт следующего",
                                "body": decision["raw_body"], "flush": []}
            if delivered and names_instructions:
                # Названной редакция считается только когда письмо действительно
                # ушло: несостоявшееся письмо соберут заново на следующем тике, с
                # той редакцией, которая будет лежать на диске в ту минуту.
                outbound.remember_instructions(entry, names_instructions, moment)
        # A reply is owed independently of proactive news and therefore must
        # neither advance nor erase the baseline used by the proactive gate. The
        # door's own letter about an accepted instruction is owed the same way:
        # it says nothing about the direction's news, and moving the baseline
        # with it would silence the owner's letter about that very acceptance.
        applied_report = None if kind in {"reply", "instruction"} else report
        outbound.apply(entry, decision, subject, moment, applied_report, kind)
        record = {"at": moment.isoformat(), "thread": thread, "kind": kind,
                  "subject": subject, "action": decision["action"],
                  "reason": decision["reason"],
                  "delivered": None if delivered is None else bool(delivered),
                  "message_id": message_id,
                  "reply_to_message_id": reply_to_message_id,
                  "asks_user": outbound.asks_user(decision["raw_body"]),
                  "chat": chat["src"]}
        # Appended rather than written into the direction's state file, which is
        # rewritten whole on every tick: on 2026-08-09 a review could show only
        # the later of two production ticks having gone through this gate,
        # because the earlier one's evidence had lasted twenty minutes.
        ledger.record(record)
    return {**record,
            "src": "state/outbound.json — реестр сказанного пользователю; "
                   "state/outbound-journal.jsonl — все решения шлюза подряд"}


def announce(thread: str, title: str, message: str, report: dict,
             moment: datetime, chat: dict | None = None) -> dict:
    """Вердикт продакта: письмо через дверь, а пуш — только если письмо спрашивает.

    23 августа 2026 пользователь попросил две вещи сразу: не повторять один и
    тот же вопрос и не слать в Telegram отчёты по задачам. Обе выполняются
    здесь, и порядок важен.

    Пуш стоит после двери, а не до неё: пуш до двери уносил бы в Telegram тот
    самый повторный вопрос, который дверь только что отбросила.

    Пуш идёт только у письма, которое спрашивает пользователя. Отчёт о работе
    уходит письмом и остаётся на табло. Границу «отчёта» здесь не выбирает
    автор: до 23 августа пуш срабатывал на каждый продуктовый текст, и в том
    самом окне, на которое пожаловался пользователь (сообщения Calypso 93571
    22:57 UTC — 93870 13:12 UTC), журнал двери содержит 104 решения вида
    `verdict` и ни одного вида `idle`. Пользователь назвал отчётами вердикты
    тика, а вопрос отчётом не является — и терять его нельзя. На том же окне
    это правило даёт 3 сообщения вместо 112.

    Спрашивает письмо или нет, решает `outbound.asks_user` — та же функция,
    которой дверь решает судьбу вопроса и которую она пишет в свой журнал.
    Считается она по тексту, который и уходит в пуш. Вопрос, который дверь
    придержала и приложила к более позднему письму, пуша не получит: письмо с
    ним пользователь получит почтой, а Telegram повторяет только то, что дверь
    отправила сама.

    Отсюда же вердикт непрерывной сессии по цели: правило одно, и место у него
    тоже одно.
    """
    letter = deliver(thread, "verdict", f"Продакт: {title}", message, report,
                     moment, chat)
    if letter["action"] == "send" and outbound.asks_user(message):
        notify(f"[{title}]\n{message}")
    return letter


def notify(text: str) -> None:
    """Сообщение продакта в личный Telegram владельца установки.

    23 августа 2026 пользователь написал: «Я не просил присылать отчёты по
    задачам в телеграм, только в почту. Отбивки от dev-pipeline пусть приходят,
    но отчёты — нет». С тех пор сюда доходят ровно две вещи.

    Первая — письмо, которое ставит перед пользователем вопрос или выбор, и
    только после того, как дверь его отправила (см. `announce`). Вопрос отчётом
    не является, а увидеть его надо быстро.

    Вторая — сообщение о сломанном контуре: разошедшийся контракт с
    `task_runner`, не отработавшее пробуждение и стоячая цель, у которой
    пробуждение не дало ни живого прогона, ни названного блокера
    (`goal_session.post_check`). Ни у одного из трёх нет почтовой копии, поэтому
    без пуша они не видны нигде; пользователь просил не дублировать письмо, а не
    молчать о сломанном.

    Рассказ о работе сюда не идёт вообще: ни вердикт тика, ни вердикт
    непрерывной сессии, ни отчёт о простое (`idle`). Он уходит письмом и стоит
    на табло. Отбивки dev-pipeline о фоновых прогонах приходят своим
    транспортом из системы задач, эта правка их не касается.
    """
    if send_bot_message is None:
        return
    try:
        send_bot_message(text)
    except (OSError, SystemExit, ValueError):
        return


def require_notification_profile() -> str | None:
    """Fail before product work when its server-owned push route is absent.

    «Absent» here means a channel this installation has and cannot use, which is
    the failure this check was written for: work that nobody would be told about.
    An installation with no push channel at all is a different thing and not a
    failure — it says everything by mail and on the board.
    """
    if resolve_bot_target is None:
        return None
    try:
        _token, destination = resolve_bot_target()
    except (OSError, SystemExit, ValueError) as exc:
        raise SystemExit(
            "product-owner: server-owned notification profile is unavailable: "
            f"{exc}"
        ) from None
    return destination


def runner_contract_alarm(thread: str, stored: dict, moment: datetime,
                          announce: bool) -> tuple[list[dict], dict | None]:
    """Whether this repository can still ask the runner what it asks it.

    The observation of every direction is built on names imported from
    `task-agent`. On 2026-08-08 one of them was renamed there, nothing here
    noticed, and all four directions spent about ten hours answering
    `AttributeError` instead of answering. Nobody was told: the failure lived in
    a systemd journal, and the board simply kept showing the state files of the
    previous night.

    A test guarded the names, and review 954 (HIGH-1) pointed out that no live
    mechanism ran that test — so the guard runs from here, where a mechanism
    genuinely exists: four timers, every twenty minutes each. What it changes is
    not the breakage but the silence around it. A divergence now leaves through
    the two channels the verdicts leave through, is written into the direction's
    own state file where the board reads it, and makes the unit fail.

    Rate-limited like the other standing reminders, and for the same reason: the
    contour wakes twelve times an hour and the same alarm twelve times an hour is
    a mute button by other means. A *changed* set of violations is news at once.
    """
    violations = runner_contract.check()
    reminder = stored.get("runner_contract_reminder")
    print(runner_contract.report(violations, RUNNER_SCRIPTS), file=sys.stderr)
    if not violations:
        # Nothing to remember: the next divergence must be loud even if an
        # identical one was announced an hour ago and then repaired.
        return [], None
    signature = json.dumps(sorted(item["text"] for item in violations))
    if announce and repeatable(reminder, signature, moment):
        told = (f"[{thread}] наблюдение продакта держится на именах из task-agent, "
                f"и они разошлись:\n\n"
                + "\n".join(f"- {item['text']}\n  ({item['src']})" for item in violations)
                + "\n\nПока это не починено, живые прогоны, простой и свежесть работы "
                  "считаются кодом, который может отвечать ошибкой вместо ответа.")
        notify(told)
        deliver(thread, "alarm", "Продакт: контракт с task_runner разошёлся",
                told, None, moment)
        reminder = {"at": moment.isoformat(), "signature": signature}
    return violations, reminder


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

    What changed on 2026-08-07 is *whom* it yields to. Yielding used to mean «any
    other process with thread_tick.py in its command line», and the four timers
    all fired in the same second, so on every synchronous wake-up exactly one
    direction could act and it was always the same one: `client` stood down
    for `process`, `platform` for `client` and `process`, `product` for all
    three, and `process` for nobody. The four directions own four disjoint sets
    of repositories, so not one of those collisions was real. A collision is a
    working tree two children would land in, and nothing else.
    """
    mine = set(report["worktrees"])
    others = []
    handed_off = []
    for owner in report["owners_awake"]:
        if owner["pid"] == os.getpid():
            # Excluding by thread instead would throw away exactly the case that
            # cost the forty minutes: the second owner of 2026-08-06 was awake on
            # the *same* direction, and it is a same-direction second owner that
            # must not put a second child into one working tree.
            continue
        shared = sorted(mine & set(owner.get("worktrees") or []))
        if not shared:
            continue
        # Awake is not the same as deciding. A terminal session the user left
        # open holds no working tree in any sense that matters: it starts no
        # child, writes nothing and burns no processor time. Yielding to it
        # forever is how «фоновый продакт» would quietly turn back into «следи
        # за доступностью терминала», which is the thing being repaired here.
        # The observation is a difference between two sightings one wake-up
        # apart, and it is written down rather than dropped: work not started
        # because of somebody else must always be visible on disk.
        activity = owner.get("activity") or {}
        if activity.get("active") is False:
            handed_off.append({**owner, "shared_worktrees": shared})
            continue
        others.append({**owner, "shared_worktrees": shared})
    if not others and not handed_off:
        return None
    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "to": others,
        # Whom this tick did *not* stand down for, and it is here rather than
        # dropped for the same reason `to` is: a decision about somebody else's
        # window has to be checkable on disk.
        "not_yielded_to": handed_off,
        # What was not started because of a yield. Empty when nothing was
        # yielded to: work this tick was free to start was not held by anyone.
        "ready_to_start": report["ready_to_start"] if others else [],
        "decided_not_done": report["decided_not_done"] if others else [],
        "src": "командные строки и рабочие каталоги процессов в /proc, сверенные с "
               "репозиториями направления из threads.json; работает ли бодрствующий "
               "продакт — по разнице его процессорного времени между наблюдениями тика",
    }


def heard_block(said: list[dict], chat: dict) -> str:
    """What the user has already been told, put in front of the owner.

    The gate in `outbound` can only refuse a repeat after it is written; this is
    the half that keeps it from being written. Both sources were on disk all
    along and neither was read: «меня например раздражают письма примерно про
    одно и то же. Особенно если мы проговорили в чате CLI, а потом приходит
    письмо „А знаешь, мы тут такое сделали за это время! …“».
    """
    if not said and not chat["sessions"]:
        return ""
    letters = "\n".join(
        f"- {item['at'][:16].replace('T', ' ')} UTC «{item['subject']}»: "
        f"{' '.join(item['excerpt'].split())[:220]}" for item in said
    ) or "- писем в этом окне не было"
    spoken = (f"Разговоры в CLI, где говорил человек: {len(chat['sessions'])} сессий, "
              f"{chat['chars']} символов, названы задачи "
              + (", ".join(str(i) for i in chat["tasks"]) or "нет")
              + f" [{chat['src']}]") if chat["sessions"] else (
        "Разговоров в CLI с человеком в этом окне не было.")
    return f"""
Что пользователь уже слышал (письма — из реестра отправленного, разговор — из
стенограмм CLI; и то и другое наблюдаемо, не со слов):
{letters}
{spoken}

Новостью может быть только то, чего в этом списке нет: пересказ уже сказанного
письмом не идёт, он будет отброшен как повтор, и пользователь увидит вместо него
табло. Если новости нет — SILENT. Это не разрешение писать непонятно: шапка «над
чем работаем» и объяснение предмета — не пересказ новости, а то, без чего
новость нельзя понять, и они нужны в каждом письме.

Если вопрос из этого списка ещё без ответа, второй раз его не задают. Такое
письмо имеет смысл только с новым фактом и обязано назвать его строкой `НОВОЕ:`.
"""


def plan_block() -> str:
    """The current portfolio revision, read from disk on every wake-up.

    The tick used to infer order from task statuses, and a `planned` task read
    as permission to start. It is not: the order between products is set by one
    current revision, and a direction the user paused stays paused even when its
    queue is full. Reading it here is also what makes a decision taken in the
    CLI reach the background owner without anyone carrying it by hand.
    """
    try:
        plan = product_memory.current_plan()
    except product_memory.ContentError as error:
        return ("\nПортфельный план не читается: " + str(error)
                + "\nПорядок работ не установлен — не выводи его из статусов задач;\n"
                  "назови это вслух и не запускай работу, меняющую пользу.\n")
    return f"""
Текущая редакция портфельного плана (порядок работ задаёт она, а не статус
`planned` и не старый план в чьём-то тексте):
{product_memory.plan_text(plan)}

Направление на паузе не запускается, даже если очередь непуста; наличие
`planned` разрешением не является. Если решение пользователя меняет порядок,
сначала сохрани его запись и выпусти новую редакцию, потом отвечай.
"""


def verdict_block() -> str:
    """One output contract for both the ordinary tick and a goal session.

    Self-sufficiency is asked of *every* letter, and that is the whole of the
    change of 2026-08-23. Until then this block asked it of a question and told
    an ordinary verdict to stay short — two commands about the same letter, and
    the shorter one won. What came out on 2026-08-23 07:20 UTC (Gmail
    `1a02d831f8eb1e44`) was five sentences with no heading, no task number and
    eight untranslated internal terms in a row, about which the user said: «даже
    если получатель немного в контексте наших задач, ему крайне сложно понять, о
    чём идёт речь». Task 1244 had already fixed exactly this for questions by
    editing exactly this block, and the letters it produced do carry the heading,
    the choice and the price — which is why the fix here is the same edit with
    the fork removed rather than a new checker over the model's output.

    «Коротко» is not deleted, it is put where it belongs: nothing to say is still
    `SILENT`, and a letter still says only what moved. What it may no longer do
    is leave the reader guessing what it is about.

    What stays here is the letter: its heading, its service lines, its question
    and its repeat. Every rule about the language itself — grammar, word order
    and «ни одного внутреннего термина без расшифровки» — moved to
    `plain_russian` on 2026-08-24, because a language rule kept in the contract
    of one letter reaches one letter. The mail reply that a review read on that
    day said `lifecycle`, `drop` and `idle` to a person, and this block was the
    only place that forbade it.
    """
    return f"""Правила ответа пользователю:
- если сказать пользователю нечего, ответь ровно словом `SILENT`. Всё остальное,
  что ты отправляешь, — письмо человеку вне нашего контекста;
- твой ответ целиком — это либо ровно `SILENT`, либо само письмо. Не рассуждай
  вслух до первой строки и не передумывай в ответе: письмо начинается строкой
  `ПОВОД:`, и всё, что стоит выше неё, пользователь читает как начало письма;
- любое письмо самодостаточно: его читают, не открывая задач и не помня
  предыдущей переписки. Начинай шапкой «Над чем работаем» — список работ, у
  каждой номер задачи и одна фраза, что это и в какой стадии. Дальше: что
  изменилось для пользователя, цена, что осталось, и «Риск/долг», если в
  verification есть GAP;
- в конце письма скажи обычными словами, что нужно от пользователя. Нужно
  решение или действие — назови его одной фразой. Не нужно ничего — так и
  напиши: «От вас ничего не требуется». Служебная строка `ВОПРОС: нет` человеку
  этого не говорит: он читает её как часть письма, а не как ответ на свой
  вопрос «что от меня хотят»;
{plain_russian.as_bullet()};
- вопрос пользователю несёт сверх этого: исходная потребность пользователя,
  что именно проверено и ключевые наблюдения,
  рекомендация и реальная альтернатива с её ценой,
  точный выбор и что произойдёт при каждом ответе,
  что пока не изменилось и существенный риск;
- человек должен суметь принять решение из одного этого письма, не зная номеров
  задач и предыдущего разговора. Подробность важнее требования краткости, но не
  повторяй сведения, которые не помогают понять и выбрать. Не выдумывай
  цену, срок или вариант ответа: называй только то, что подтверждают артефакты,
  а неизвестное помечай неизвестным. В шапку и выбор включай только работы и
  варианты из наблюдаемого состояния и относящихся к событию артефактов;
  примеры из общих правил не являются текущим состоянием. Не добавляй «паузу»
  третьим вариантом и не синтезируй сочетание вариантов, если их прямо не
  называет проверенный материал;
- один и тот же вопрос дважды не задают. Если ты уже спрашивал пользователя об
  этом же выборе и ответа ещё нет, новое письмо имеет смысл только тогда, когда
  с прошлого письма стало известно что-то новое. Тогда назови это строкой
  `НОВОЕ: <что стало известно с прошлого письма>` сразу после шапки — и только
  настоящий новый факт, не пересказ прежнего другими словами. Нечего написать в
  этой строке — значит писать нечего: `SILENT`. Дверь письма проверяет это же
  наблюдением и повтор без нового факта не отправляет;
- первой строкой поставь `ПОВОД: вопрос|польза|готово|механика`, второй —
  `ВОПРОС: да|нет`. `вопрос` означает, что нужен выбор пользователя, и такое
  письмо доходит всегда, пока это не повтор уже заданного. `польза` —
  изменилось, что пользователь может; `готово` — закончилась заказанная работа;
  `механика` — прогон или движение репозитория, которые видны на табло.
  Просьба выбрать, даже кончающаяся точкой, требует `ВОПРОС: да`."""


def prompt(report: dict, events: list[str], reasons: list[dict],
           said: list[dict], chat: dict, startup: str = "") -> str:
    # Shown only when there is something observed to show. A heading over an
    # empty list reads as «причин нет», which is a different claim.
    seen = ("\nЧто наблюдение говорит о простое (это не приговор, а то, что видно с диска):\n"
            + "\n".join(f"- {item['text']} [{item['src']}]" for item in reasons) + "\n"
            ) if reasons else ""
    bounded = startup.lstrip().startswith("{")
    startup_block = (f"""
Ограниченный стартовый пакет ниже уже собран из всех обязательных источников.
Не перечитывай AGENTS.md, план, снимки, состояния, бюджеты и цели второй раз.
Историческую подробность по хешу открывай адресно только для события этого тика.
{startup}
""" if bounded else plan_block() + startup)
    return f"""Ты продакт-агент на фоновом пробуждении треда «{report['title']}».
{startup_block}
{heard_block(said, chat)}

Произошло с прошлого пробуждения:
{chr(10).join('- ' + event for event in events)}
{seen}
Наблюдаемое состояние треда (собрано механически, не со слов исполнителя):
{json.dumps(report, ensure_ascii=False, indent=2)}

Сделай ровно четыре шага и ничего сверх них:
1. Прочитай артефакты только тех задач, которых касаются события выше.
2. Реши: принять результат, вернуть на доработку, запустить следующий шаг или
   спросить пользователя. Правило кросс-ревью: работу Codex ревьюит Claude и
   наоборот; на замечания сначала анализ и план, потом правки, потом повторное
   ревью. Перед тем как заказывать код, проверь по порядку: ничего не делать;
   убрать или отключить; настроить или переиспользовать; упростить; только затем
   заказывать минимально необходимый код. Кратко зафиксируй, почему более ранний
   вариант не закрывает потребность.
3. Сохрани событие, разбор, отчёт и вложения отдельной уникальной записью в
   `content/products/<продукт>/history/`. В раздел «В работе» снимка ничего не
   дописывай, если набор текущих обещаний не изменился: там остаются только
   актуальные обещания с наблюдаемой задачей, а не события прогонов и приёмок.
   Новое обещание добавляй через `product_memory.append_work_line`, а не правкой
   файла руками: рядом пишет второй продакт. Обнови состояние пользовательских
   путей в снимке, если оно изменилось по артефакту, а не по прозе исполнителя.
4. Верни текст для пользователя в формате вердикта продакта: что теперь
   может пользователь, цена, что осталось, и строка «Риск/долг», если в
   verification есть GAP.

{verdict_block()}

Разделы «готово к запуску», «можно подхватить» и «решено, но не исполнено» в
состоянии выше — это работа, у которой ничего не держит: по ней нужен либо
запуск, либо названная причина, почему нет. Простой при непустой очереди — сам
по себе повод: если ты ничего не запустил, причина обязана быть в твоём ответе
обычными словами, иначе пользователь снова увидит нули без объяснения.

Уступать дорогу надо только тому продакту, который может занять то же рабочее
дерево и при этом действительно работает: `yielded_to_awake_owner` в файле
состояния треда уже содержит только таких и называет общие деревья, а те, чьё
процессорное время между двумя наблюдениями не двигалось, лежат там же в
`not_yielded_to` — это открытые терминалы, которым работа уже передана, и
останавливаться из-за них нельзя. Продакт на чужих репозиториях тебе тоже не
мешает. Список того, что ты не стал запускать, уже лежит на диске рядом,
пересказывать его в тексте не нужно.

Запуск ребёнка в режиме записи по нашему коду в нашем репозитории разрешения не
требует: это обычная доставка, и на пробуждении она делается молча. Разрешение
спрашивается по существу работы — необратимое, выход наружу, выбор, меняющий
пользу, — а не по режиму песочницы. Единственный ограничитель запуска здесь
физический: занятое рабочее дерево, где уже живёт другой ребёнок.
Не читай транскрипты детей: только артефакты задач и наблюдаемое состояние.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("thread")
    parser.add_argument("--dry-run", action="store_true", help="показать события и выйти")
    parser.add_argument("--force", action="store_true", help="разбудить агента даже без событий")
    args = parser.parse_args()

    require_notification_profile()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path = STATE_DIR / f"{args.thread}.json"
    stored = {}
    if state_path.is_file():
        try:
            stored = json.loads(state_path.read_text())
        except (json.JSONDecodeError, OSError):
            stored = {}
    moment = datetime.now(timezone.utc)
    now = moment.isoformat()

    # The existing process timer fires on :00/:20/:40. Its first firing at or
    # after 08:00 owns the daily letter. A failed mail is retried hourly from
    # the observation already written here, without adding another scheduler.
    daily_result = None
    daily_failure = None
    if args.thread == "process" and not args.dry_run:
        try:
            daily_result = daily_standup.maybe_send(
                moment, previous=stored.get("daily_standup"))
        except Exception as error:
            daily_result = {"action": "fail", "at": moment.isoformat(),
                            "reason": str(error)[:500]}
        if daily_result.get("action") == "fail":
            daily_failure = daily_result["reason"]
            print(f"утренняя оперативка не доставлена: {daily_failure}", file=sys.stderr)

    # Before anything is observed, because a broken contract with the runner is
    # what makes the observation itself worthless — and it comes first so the
    # alarm is already out even if `build` then dies on that very name.
    contract, contract_reminder = runner_contract_alarm(
        args.thread, stored, moment, announce=not args.dry_run)

    report = build(args.thread)
    previous = stored.get("snapshot", {})
    current = snapshot(report, previous)
    long_lived_processes = persisted_process_inventory(report, stored)

    events = transitions(previous, current) if previous else ["первый запуск треда"]

    # Before the idle test and before the wake-up decision: a goal is the memory
    # this process does not have, and «что стоит по цели» is a reason to wake up
    # in its own right, even on a direction whose queue looks busy.
    goals = goal_watch(args.thread, report, stored, moment)
    events += goals["transitions"]

    standing_now, idle_reminder, held_reminder = standing_events(
        report, current, stored, moment)
    events += standing_now + goals["standing"]
    idle = bool(not report["live_runs"] and startable(report))

    # Written whether or not an agent is woken, and before it is: the list of
    # work standing ready has to survive a tick that decides to say nothing, and
    # it must not depend on what the woken agent chose to write down.
    standing = {
        "ready_to_start": report["ready_to_start"],
        "decided_not_done": report["decided_not_done"],
        "yielded_to_awake_owner": yielded(report),
    }
    reasons = idle_reasons(report, standing)

    # Кем ведётся направление прямо сейчас. Пока цель под усиленным контролем не
    # разрешена, обычный режим — одна продолжающаяся сессия, а тик при ней
    # watchdog: он не поднимает второго продакта поверх живой сессии и
    # восстанавливает её, когда она умерла или была вынуждена ротироваться.
    # Обычная работа сюда не попадает: без усиленной цели `mode` остаётся `none`
    # и всё ниже идёт ровно как раньше.
    session = goal_session.watchdog(args.thread, moment, act=not args.dry_run)
    session_holds, handover = session_leads(session, goals["objects"])
    events += handover
    woke = (bool(events) or args.force) and not session_holds

    def record(final: dict | None, done: bool) -> dict:
        """The direction's state file, in the one shape the board reads."""
        return {
            "thread": args.thread, "updated_at": now, "snapshot": current,
            "long_lived_processes": long_lived_processes,
            "long_lived_processes_observation": process_observation(report),
            "last_events": events, **standing,
            "idle_reminder": idle_reminder,
            "undelivered_reminder": held_reminder,
            # The durable goals of this direction, as the store holds them at the
            # moment of the check. Written whether or not an owner is woken, so
            # the board can show «что обещано пользователю и где это стоит»
            # without asking a running agent anything.
            "goals": goals["panel"],
            "goal_reminder": goals["reminder"],
            # Кто ведёт направление и что тик с этим сделал. Написано и когда
            # сессии нет: «усиленных целей нет» — тоже ответ на вопрос «почему
            # тик разбудил продакта сам».
            "goal_session": session,
            # Written on every tick, healthy or not, so the board can tell «эта
            # проверка была и прошла» from «этой проверки никто не делал». The
            # observation the previous outage never produced.
            "runner_contract": {
                "at": now,
                "violations": contract,
                "src": f"скан RUNNER.<имя> в scripts/*.py против {RUNNER_SCRIPTS}",
            },
            "runner_contract_reminder": contract_reminder,
            # A daily failure must stay visible without suppressing the normal
            # thread observation that tells the user what else happened.
            "daily_standup": daily_result,
            # What this check saw and what came of it, at the moment of the
            # check. When the *next* one falls is deliberately not here: this
            # process is the service paired with the timer, so the only instant
            # it could write that field is the one instant systemd is holding
            # the timer unarmed. `process_map_state.next_check` asks systemd
            # when the board is built, which is the moment the answer is about.
            "check": {
                "at": now,
                "outcome": (outcome(current, final, woke, report, session) if done
                            else "проверка идёт: продакт разбужен, решение ещё не принято"),
                "outcome_src": (
                    "события и очередь треда в момент проверки" if not woke else
                    "живые прогоны треда, наблюдённые до и после пробуждения" if done else
                    "тик записал начало проверки до запуска продакта"),
                "woke_owner": woke,
                # What the check actually put in motion. `None` while the owner
                # is still deciding: «ещё не запустил» and «не стал запускать»
                # are two different states, and the board shows the reasons for
                # standing still only for the second one. Without the split the
                # panel printed «запустил 3 — задачи 823, 872, 873» and «причина
                # простоя не наблюдается» on the same column.
                "started": started_runs(current, final) if done and woke else None,
                "events": events,
                "reasons": reasons,
                "queue": queue(report),
                "src": f"state/threads/{args.thread}.json — запись тика в момент проверки",
            },
        }

    # A divergence with the runner makes the unit fail even when this tick still
    # managed to observe something: an exit code is the one signal that survives
    # a wake-up nobody reads, and «наблюдение считает сломанным кодом» is not a
    # success whatever came out of it.
    verdict = 1 if contract or daily_failure else 0

    if args.dry_run:
        print(json.dumps({"events": events, "snapshot": current, **standing,
                          "goals": goals["panel"], "goal_session": session,
                          "check": record(None, not woke)["check"]},
                         ensure_ascii=False, indent=2))
        return verdict

    state_path.write_text(json.dumps(record(None, not woke), ensure_ascii=False, indent=2))

    # Письмо о принятой инструкции внешнему исполнителю. Здесь, а не внутри
    # разбуженного продакта: оно целиком собрано из наблюдения, поэтому не зависит
    # ни от того, что продакт написал, ни от того, будили ли его вообще. Уходит
    # один раз на редакцию — `deliver` под замком реестра проверяет, не назвал ли
    # её уже соседний тик, — и своим письмом, поэтому чужие письма направления
    # остаются ровно такими, какими были.
    mail = []
    letter = outbound.instruction_letter(args.thread)
    if letter is not None:
        mail.append(deliver(
            args.thread, "instruction",
            f"Продакт: {report['title']} — путь к принятой инструкции для внешнего исполнителя",
            letter["body"], report, moment, names_instructions=letter["names"]))

    if not woke:
        if mail:
            final = record(None, True)
            final["mail"] = mail
            state_path.write_text(json.dumps(final, ensure_ascii=False, indent=2))
        return verdict

    # Same trust level as the user's own full session: the directories this
    # installation names in `workspace_dirs` must be reachable or the tick is
    # blind to every thread but this one. IS_SANDBOX=1 is what lets Claude Code
    # skip permissions. The prompt goes through stdin because `--add-dir` is
    # variadic and would otherwise swallow a trailing positional prompt.
    # Read before the owner is woken, not after it has written: a repeat that
    # was never composed costs nothing, and one that was costs a wake-up.
    chat = outbound.heard_in_chat(moment)
    with outbound.Ledger() as ledger:
        said = outbound.already_said(ledger.thread(args.thread), moment)

    environment = {**os.environ, "IS_SANDBOX": "1"}
    result = subprocess.run(
        [str(CLAUDE_PRODUCT_OWNER), "--entry", "print"],
        input=prompt(report, events, reasons, said, chat,
                     startup_context.render(startup_context.packet((args.thread, report)))),
        env=environment,
        capture_output=True, text=True, cwd=HOME, timeout=WAKE_TIMEOUT,
    )
    for diagnostic in route_diagnostics(result.stderr or ""):
        print(diagnostic, file=sys.stderr)
    message = (result.stdout or "").strip()
    if result.returncode != 0:
        failure = f"[{args.thread}] пробуждение треда не отработало: {(result.stderr or '')[:300]}"
        notify(failure)
        deliver(args.thread, "wake_failure",
                f"Продакт: пробуждение треда «{report['title']}» не отработало",
                failure, report, moment)
        return 1

    # What the wake-up came to, observed rather than believed: the same
    # projection taken again, and the difference in live runs is the answer.
    try:
        after = snapshot(build(args.thread), current)
    except Exception:
        after = None
    state_path.write_text(json.dumps(record(after, True), ensure_ascii=False, indent=2))

    # Обязательный исход пробуждения по стоячей цели, проверенный наблюдением, а
    # не обещанный промптом: живой прогон по её задаче или названный блокер.
    # Молчание третьим исходом не является — и до этой проверки оно им было,
    # когда рядом шёл посторонний живой прогон и обычный `idle` не срабатывал.
    goal_check = goal_session.post_check(
        args.thread, goals["objects"], (after or {}).get("live", []), message, moment)

    if message and message != "SILENT":
        mail.append(announce(args.thread, report["title"], message, report,
                             moment, chat))
    elif idle and not (after or {}).get("live"):
        # The owner was woken because the direction is standing still and said
        # nothing. Silence is what the user complained about in as many words —
        # «ни письма не было с вопросами/проблемами, ни информации на доске» — so
        # the observed reason goes out as a letter.
        #
        # И только как письмо: это рассказ о работе, а такой текст 23 августа
        # 2026 пользователь попросил в Telegram не слать. Вердикт выше идёт по
        # тому же правилу — пуш достаётся только письму с вопросом, см.
        # `announce`.
        told = (f"[{report['title']}] простоя не сняли: живых прогонов нет, "
                f"к запуску {startable(report)}.\n\nПочему, по наблюдению:\n"
                + "\n".join(f"- {item['text']}" for item in reasons))
        mail.append(deliver(
            args.thread, "idle",
            f"Продакт: «{report['title']}» ничего не запустил при непустой очереди",
            told, report, moment))
    if mail or goal_check:
        # Written after the fact and into the same file the board reads, because
        # «письмо не ушло» is an observation about this check like every other
        # one here, and the only place it could otherwise be seen is a journal
        # nobody opens.
        final = record(after, True)
        final["mail"] = mail
        final["goal_post_check"] = goal_check
        state_path.write_text(json.dumps(final, ensure_ascii=False, indent=2))
    print(message)
    # A wake-up that left a standing goal without either outcome is a failure of
    # this mechanism, not a quiet result: the exit code is the one signal that
    # survives a check nobody reads.
    return 1 if goal_check and not goal_check["resolved"] else verdict


if __name__ == "__main__":
    sys.exit(run_entrypoint(main))
