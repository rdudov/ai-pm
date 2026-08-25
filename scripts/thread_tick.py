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
import hashlib
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
# оперативка — своему утру, а письмо о зарегистрированной инструкции пересобирается
# следующим тиком: цифра в нём считается с байтов файла в минуту отправки, и
# полежавший в очереди текст назвал бы редакцию, которой на диске может уже не
# быть.
FAILED_SEND = {
    "reply": "отправка ответа не удалась; повтор не выполнен",
    "daily": "отправка оперативки не удалась; следующий утренний тик повторит",
    "instruction": ("письмо о зарегистрированной инструкции не ушло; следующий тик соберёт "
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

    Whether a letter should exist and which channel owns it was decided by its
    composer before this function. What is decided here is only whether Gmail
    accepted it, which the receipt ledger needs. On success the Gmail message id
    is returned when the sender prints it; `True` preserves the older success
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


COMPOSED_KEYS = ("channel", "kind", "event_id", "subject", "body", "attachments")
COMPOSED_EVENT = re.compile(r"^[a-z0-9][a-z0-9:._-]{5,199}$")
COMPOSED_FENCE = re.compile(
    r"```(?:json)?[ \t]*\r?\n(?P<payload>.*?)\r?\n```", re.DOTALL)


def parse_composed_message(text: str) -> dict | None:
    """Read the composer's route declaration without interpreting its prose."""
    payload = text.strip()
    if re.fullmatch(r"\*\*[ \t]*SILENT[ \t]*\*\*", payload):
        payload = "SILENT"
    if payload in {"", "SILENT"}:
        return None
    fenced = [match.group("payload") for match in COMPOSED_FENCE.finditer(payload)]
    if fenced:
        if len(set(fenced)) != 1:
            raise ValueError("composer returned multiple different fenced blocks")
        payload = fenced[0].strip()
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValueError(f"composer returned neither SILENT nor JSON: {error}") from error
    if not isinstance(value, dict) or set(value) != set(COMPOSED_KEYS):
        raise ValueError(
            "composer JSON keys must be channel, kind, event_id, subject, body, attachments")
    if value["channel"] != "gmail":
        raise ValueError("a background product message may select only gmail")
    if value["kind"] not in {"question", "report"}:
        raise ValueError("composer kind must be question or report")
    if not isinstance(value["event_id"], str) or not COMPOSED_EVENT.fullmatch(value["event_id"]):
        raise ValueError("composer event_id is missing or unstable")
    if not isinstance(value["subject"], str) or not value["subject"].strip():
        raise ValueError("composer subject is empty")
    if not isinstance(value["body"], str) or not value["body"].strip():
        raise ValueError("composer body is empty")
    attachments = value["attachments"]
    if (not isinstance(attachments, list)
            or not all(isinstance(path, str) and Path(path).is_absolute()
                       for path in attachments)):
        raise ValueError("composer attachments must be absolute paths")
    return value


def composer_failure(raw_response: str, error: ValueError, src: str) -> dict:
    """Describe one composed response that did not satisfy its envelope."""
    return {
        "error": str(error),
        "raw_response": raw_response,
        "src": src,
    }


def persist_composer_failure(state_path: Path, state: dict,
                             raw_response: str, error: ValueError,
                             retry_snapshot: dict | None = None) -> None:
    """Keep a malformed response durable without consuming its observation."""
    state["composer_failure"] = composer_failure(
        raw_response, error, f"stdout составителя; {state_path}")
    if retry_snapshot is not None:
        state["snapshot"] = retry_snapshot
    if isinstance(state.get("check"), dict):
        state["check"]["outcome"] = "составитель вернул ответ без допустимого конверта"
        state["check"]["outcome_src"] = f"composer_failure; {state_path}"
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    with outbound.Ledger() as ledger:
        ledger.record({"at": state.get("updated_at"),
                       "thread": state.get("thread"),
                       "kind": "composer_failure", "action": "fail",
                       "reason": str(error), "raw_response": raw_response,
                       "src": f"stdout составителя; {state_path}"})


def deliver(thread: str, kind: str, subject: str, body: str, moment: datetime,
            *, reply_to_message_id: str | None = None,
            attachments: list[str] | None = None,
            raw_message: bytes | None = None,
            names_instructions: list[dict] | None = None,
            event_id: str | None = None,
            selected_by: str = "delivery_door") -> dict:
    """Send one already-routed Gmail message and receipt its exact event."""
    if (kind == "reply") != bool(reply_to_message_id):
        raise ValueError("kind='reply' and reply_to_message_id must be supplied together")
    if event_id is None:
        if reply_to_message_id:
            event_id = f"reply:{reply_to_message_id}"
        elif names_instructions:
            event_id = outbound.instruction_event_id(thread, names_instructions[0])
        else:
            # Mechanical legacy callers have no semantic event field yet. An
            # exact byte digest prevents a retry from becoming a duplicate; it
            # does not interpret the text or decide whether it was worth mail.
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:24]
            event_id = f"{kind}:{thread}:{digest}"
    with outbound.Ledger() as ledger:
        entry = ledger.thread(thread)
        duplicate = outbound.event_delivered(entry, event_id)
        if names_instructions and not outbound.unnamed_instructions(entry, names_instructions):
            duplicate = True
        action = "drop" if duplicate else "send"
        reason = ("это событие уже доставлено" if duplicate else
                  f"канал Gmail выбран до текста: {selected_by}")
        delivered = None
        message_id = None
        if action == "send":
            mail_options = {"reply_to_message_id": reply_to_message_id,
                            "attachments": attachments}
            if raw_message is not None:
                mail_options["raw_message"] = raw_message
            send_result = send_mail(subject, body, **mail_options)
            delivered = bool(send_result)
            message_id = send_result if isinstance(send_result, str) else None
            if not delivered:
                action = "fail"
                reason = FAILED_SEND.get(kind, "отправка не удалась; событие не отмечено доставленным")
            else:
                outbound.remember_delivery(
                    entry, event_id=event_id, subject=subject, body=body,
                    kind=kind, now=moment, message_id=message_id)
            if delivered and names_instructions:
                outbound.remember_instructions(entry, names_instructions, moment)
        record = {"at": moment.isoformat(), "thread": thread, "kind": kind,
                  "event_id": event_id, "channel": "gmail", "subject": subject,
                  "action": action, "reason": reason,
                  "delivered": None if delivered is None else bool(delivered),
                  "message_id": message_id,
                  "reply_to_message_id": reply_to_message_id,
                  "decision_owner": selected_by,
                  "composer_selected": selected_by == "composer"}
        ledger.record(record)
    return {**record, "src": "state/outbound.json — успешные event_id; "
            "state/outbound-journal.jsonl — все попытки доставки подряд"}


def deliver_idle(thread: str, title: str, report: dict, reasons: list[dict],
                 moment: datetime) -> dict:
    """Send the observed idle outcome through its sole product channel."""
    body = (f"[{title}] простоя не сняли: живых прогонов нет, "
            f"к запуску {startable(report)}.\n\nПочему, по наблюдению:\n"
            + "\n".join(f"- {item['text']}" for item in reasons))
    return deliver(
        thread, "idle",
        f"Продакт: «{title}» ничего не запустил при непустой очереди",
        body, moment)


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
    Gmail, is written into the direction's own state file where the board reads
    it, and makes the unit fail.

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
        deliver(thread, "alarm", "Продакт: контракт с task_runner разошёлся",
                told, moment)
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


def heard_block(said: list[dict]) -> str:
    """Successful Gmail events shown before the composer chooses a channel."""
    if not said:
        return ""
    letters = "\n".join(
        f"- {item.get('event_id') or 'старое событие без id'} — "
        f"{str(item['at'])[:16].replace('T', ' ')} UTC «{item['subject']}»: "
        f"{' '.join(item['excerpt'].split())[:220]}" for item in said
    )
    return f"""
Уже доставленные Gmail-события (из реестра успешной отправки):
{letters}

Не создавай второе сообщение с тем же `event_id`. Если нового Gmail-события нет,
верни SILENT. Ответ, который уже дан пользователю в текущем CLI-диалоге,
принадлежит тому диалогу и сам по себе Gmail-событием не становится.
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
    """One output contract for both the ordinary tick and a goal session."""
    return f"""Контракт результата этого фонового составителя:
- сначала реши, есть ли отдельное Gmail-событие. Ответ в текущем CLI-диалоге не
  дублируется в почту. Технические отбивки системы задач остаются в Telegram,
  но продуктовые отчёты и вопросы туда не отправляются;
- если Gmail-события нет, верни ровно `SILENT` и не пиши сообщение;
- если оно есть, сначала выбери канал и устойчивую тождественность события, и
  только после этого пиши текст. Верни только JSON без markdown и комментариев,
  ровно с этими ключами:
  {{"channel":"gmail","kind":"question|report","event_id":"...","subject":"...","body":"...","attachments":[]}};
- `event_id` описывает наблюдаемое событие, а не формулировку текста: например
  `question:task-861:choose-run-order` или `report:task-861:accepted`. Повтор того
  же перехода получает тот же id; новое состояние — новый id;
- любое письмо самодостаточно: его читают, не открывая задач и не помня
  предыдущего разговора. Начинай шапкой «Над чем работаем» — список работ, у
  каждой номер задачи и одна фраза, что это и в какой стадии. Дальше: что
  изменилось для пользователя, цена, что осталось, и «Риск/долг», если в
  verification есть GAP;
- вопрос пользователю несёт сверх этого: исходная потребность пользователя,
  что именно проверено и ключевые наблюдения,
  рекомендация и реальная альтернатива с её ценой,
  точный выбор и что произойдёт при каждом ответе,
  что пока не изменилось и существенный риск;
- подробность важнее требования краткости, но не повторяй сведения, которые не
  помогают понять и выбрать. Не выдумывай
  цену, срок или вариант ответа: называй только то, что подтверждают артефакты,
  а неизвестное помечай неизвестным. В шапку и выбор включай только работы и
  варианты из наблюдаемого состояния и относящихся к событию артефактов;
  примеры из общих правил не являются текущим состоянием. Не добавляй «паузу»
  третьим вариантом и не синтезируй сочетание вариантов, если их прямо не
  называет проверенный материал;
- в конце письма скажи обычными словами, что нужно от пользователя. Нужно
  решение или действие — назови его одной фразой. Не нужно ничего — так и
  напиши: «От вас ничего не требуется». Поле `kind` технического конверта
  человеку этого не говорит;
- `kind=question` означает, что нужен выбор пользователя; `kind=report` — что
  изменилось, что пользователь может, либо закончилась заказанная работа.
  Прогон, коммит и движение репозитория отдельным письмом не становятся;
{plain_russian.as_bullet()}."""


def prompt(report: dict, events: list[str], reasons: list[dict],
           said: list[dict], startup: str = "") -> str:
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
{heard_block(said)}

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
4. Верни `SILENT` или одно типизированное Gmail-сообщение по контракту ниже.
   Текст отчёта отвечает: что теперь может пользователь, цена, что осталось, и
   строка «Риск/долг», если в verification есть GAP.

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

    # Письмо об актуальной зарегистрированной инструкции внешнему исполнителю.
    # Здесь, а не внутри разбуженного продакта: дверь наблюдает и сам файл, и
    # отсутствие возвращённого внешнего результата в той же задаче. Уходит
    # один раз на редакцию — `deliver` под замком реестра проверяет, не назвал ли
    # её уже соседний тик, — и своим письмом, поэтому чужие письма направления
    # остаются ровно такими, какими были.
    mail = []
    with outbound.Ledger() as ledger:
        letter = outbound.instruction_letter(args.thread, ledger.thread(args.thread))
    if letter is not None:
        mail.append(deliver(
            args.thread, "instruction",
            f"Продакт: {report['title']} — путь к зарегистрированной инструкции для внешнего исполнителя",
            letter["body"], moment, names_instructions=letter["names"],
            event_id=letter["event_id"], selected_by="instruction_door"))

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
    with outbound.Ledger() as ledger:
        said = outbound.already_said(ledger.thread(args.thread))

    environment = {**os.environ, "IS_SANDBOX": "1"}
    result = subprocess.run(
        [str(CLAUDE_PRODUCT_OWNER), "--entry", "print"],
        input=prompt(report, events, reasons, said,
                     startup_context.render(startup_context.packet((args.thread, report)))),
        env=environment,
        capture_output=True, text=True, cwd=HOME, timeout=WAKE_TIMEOUT,
    )
    for diagnostic in route_diagnostics(result.stderr or ""):
        print(diagnostic, file=sys.stderr)
    message = (result.stdout or "").strip()
    if result.returncode != 0:
        failure = f"[{args.thread}] пробуждение треда не отработало: {(result.stderr or '')[:300]}"
        deliver(args.thread, "wake_failure",
                f"Продакт: пробуждение треда «{report['title']}» не отработало",
                failure, moment)
        return 1

    try:
        composed = parse_composed_message(message)
    except ValueError as error:
        failure = f"[{args.thread}] составитель не выбрал канал до текста: {error}"
        persist_composer_failure(
            state_path, record(None, True), message, error,
            retry_snapshot=previous)
        print(failure, file=sys.stderr)
        return 1
    message_body = composed["body"] if composed else ""

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
        args.thread, goals["objects"], (after or {}).get("live", []), message_body, moment)

    if composed:
        mail.append(deliver(
            args.thread, composed["kind"], composed["subject"], composed["body"],
            moment, attachments=composed["attachments"],
            event_id=composed["event_id"], selected_by="composer"))
    elif idle and not (after or {}).get("live"):
        # The owner was woken because the direction is standing still and said
        # nothing. Silence is what the user complained about in as many words —
        # «ни письма не было с вопросами/проблемами, ни информации на доске» — so
        # the observed reason goes out on the same channel the verdict does.
        with outbound.Ledger() as ledger:
            idle_due = outbound.kind_due(
                ledger.thread(args.thread), "idle", moment,
                outbound.IDLE_LETTER_SECONDS)
        if idle_due:
            mail.append(deliver_idle(
                args.thread, report["title"], report, reasons, moment))
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
