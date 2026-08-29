#!/usr/bin/env python3
"""Долговечная цель проблемной работы: память, которой у пробуждения нет.

A background wake-up is a new process every twenty minutes, and the router may
hand it to Claude one time and to Codex the next. Whatever the previous owner
understood about a piece of work — what the user actually wanted, where the path
breaks, which task is the main one, which repair it is waiting on, why it was
paused — lived in that process and died with it. A native session goal is a
mirror of one session and cannot be the memory of a mode that outlives sessions
by construction.

So the goal lives on disk, in the durable product store beside the snapshots and
the portfolio plan, and every wake-up reads it before deciding anything.

What this module is *not* is a second technical state machine. Tasks, runs,
reviews and the lifecycle stay where they are, in the development contour, and
nothing here duplicates them: a goal holds the product-side question — какой
пользовательский результат обещан, где он сейчас не сходится, чем это чинится и
что закрывает цель — and points at task numbers for everything technical.

Normal work needs none of this. A goal switches to reinforced control only when a
deviation is observed, and the six deviations are the user's own list: третий
содержательный круг ревью, повтор прежнего замечания, повторный отказ штатного
запуска, ручной обход, необходимость корректирующей задачи, расхождение принятой
и установленной версии. Two review rounds are the threshold of *control*, never a
limit on the number of repairs.

Three rules are enforced in code rather than asked of a prompt, because a prompt
is what a new session may or may not follow:

* a corrective task is created only from an explicit pause, and only with the
  user effect it is expected to produce and the observable criterion for coming
  back to the main task;
* accepting a corrective task never closes the goal — it returns the main task to
  work, which is exactly the step the first live chain would otherwise stop at;
* a repair that did not deliver itself is taken off the list by its own word —
  `retire`, with the reason and what observed it — because acceptance claims a
  delivery and must keep requiring an observed `completed`;
* a goal closes only on a live check of the actually installed product, named
  together with what observed it. A commit, a test, a green review and a
  `completed` status are refused as closing evidence.

Usage:
    product_goal.py list [--thread <тред>] [--all] [--json]
    product_goal.py show <id> [--json]
    product_goal.py open --thread <тред> --outcome <...> --observable <...>
                         --main-task <N> [--product <slug>] [--gap <...>]
    product_goal.py set <id> [--gap <...>] [--next <...>]
    product_goal.py signal <id> --code <код> --text <...> --src <чем наблюдено>
    product_goal.py observe [<id>] [--json]
    product_goal.py pause <id> --reason <...> --src <чем наблюдено>
    product_goal.py corrective <id> --task <N> --effect <...> --return-criterion <...>
    product_goal.py accept <id> --task <N> --src <чем наблюдено>
    product_goal.py retire <id> --task <N> --reason <...> --src <чем наблюдено>
    product_goal.py resume <id> [--gap <...>] [--next <...>]
    product_goal.py close <id> --live-check <...> --src <чем наблюдено>
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import product_memory  # noqa: E402


# Tasks live in the development contour; the product owner only reads them.
# Where that contour is installed is a setting of the installation and has one
# owner, `product_memory.tasks_repo`, which the board asks the same question.
TASKS_REPO = product_memory.tasks_repo()

GOALS = "goals"
JOURNAL = "journal.jsonl"
LOCK = ".goals.lock"

ACTIVE, PAUSED, CLOSED = "active", "paused", "closed"
NORMAL, REINFORCED = "normal", "reinforced"

# Two honest ways a repair stops holding the main task, and they are not the
# same statement: `accepted` says the repair was delivered and the task is
# observed `completed`, `retired` says it was taken off the list without
# delivering itself — its effect came with another accepted task, or it is
# cancelled in the task system.
ACCEPTED, RETIRED = "accepted", "retired"

# The user's own list of deviations, and nothing wider. Each one is a reason to
# turn on control, never a reason to stop repairing: «два круга — порог контроля,
# не предел исправлений».
SIGNALS = {
    "third_review_round": "третий содержательный круг ревью",
    "repeat_finding": "повтор прежнего замечания",
    "launch_refused_again": "повторный отказ штатного запуска, продолжения или перезапуска",
    "manual_bypass": "ручной обход штатного пути",
    "corrective_task": "понадобилась корректирующая задача",
    "version_divergence": "расхождение принятой и установленной версии",
}

# Above this many review rounds the work is no longer on the normal path. Two
# rounds are still normal; the third is the threshold the user named.
REVIEW_ROUND_THRESHOLD = 3
# One refusal of a normal `start`/`resume`/`retry` is an accident; the second is
# the deviation — the work is being held by the machinery rather than by itself.
REFUSAL_THRESHOLD = 2

ROUND_LINE = re.compile(r"Review round (\d+)")
REFUSAL_LINE = re.compile(r"Refus\w*\s+(?:this\s+)?(?:review\s+)?launch", re.IGNORECASE)


class GoalError(RuntimeError):
    """Отказ, который лучше молчаливого продолжения: цель — состояние, не проза."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def root() -> Path:
    """The durable store: outside git, inside the backup, beside the snapshots.

    A goal is a record of a decision and of a promise to the user, so it does not
    belong in `state/`, which is rebuilt from observation on every tick and says
    so about itself.
    """
    return Path(os.environ.get("PRODUCT_OWNER_GOALS") or (product_memory.root() / GOALS))


class _Lock:
    """Held around every read-modify-write: a second product owner writes here too."""

    def __init__(self) -> None:
        self.path = root() / LOCK
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+")
        fcntl.flock(self.handle, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None
        return False


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)


def _journal(entry: dict) -> None:
    """Append-only record of every change, because the goal file is rewritten.

    The same reason the outbound ledger appends: a state file rewritten whole on
    every change can only ever answer «как сейчас», and «кто когда перевёл цель в
    паузу и по какому наблюдению» is a question about the past.
    """
    path = root() / JOURNAL
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _path(goal_id: str) -> Path:
    return root() / f"{goal_id}.json"


def load(goal_id: str) -> dict:
    path = _path(goal_id)
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise GoalError(f"цель {goal_id} не читается: {error}") from error


def load_all() -> list[dict]:
    directory = root()
    if not directory.is_dir():
        return []
    goals = []
    for entry in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(entry.read_text())
        except (OSError, json.JSONDecodeError):
            # A goal file that cannot be read is not an absent goal. It is said
            # out loud rather than skipped, so a corrupted record cannot look
            # like «целей нет».
            goals.append({"id": entry.stem, "unreadable": True,
                          "outcome": f"файл цели {entry.name} не разбирается",
                          "state": ACTIVE, "control": REINFORCED,
                          "thread": None, "signals": [], "correctives": []})
            continue
        if isinstance(payload, dict):
            goals.append(payload)
    return sorted(goals, key=lambda goal: str(goal.get("id")))


def active(thread: str | None = None) -> list[dict]:
    """Goals still owed to the user, optionally of one direction.

    A goal whose file does not parse cannot say which direction it belongs to,
    so it is answered to every direction rather than to none. Being loud on four
    panels costs a line; being invisible costs the promise.
    """
    return [goal for goal in load_all()
            if goal.get("state") != CLOSED
            and (thread is None or goal.get("thread") == thread
                 or goal.get("unreadable"))]


def _next_id() -> str:
    used = {goal.get("id") for goal in load_all()}
    number = 1
    while f"{number:04d}" in used:
        number += 1
    return f"{number:04d}"


def _save(goal: dict, event: str, detail: dict | None = None) -> dict:
    goal["updated_at"] = now()
    _atomic_write(_path(goal["id"]), goal)
    _journal({"at": goal["updated_at"], "goal": goal["id"], "event": event,
              "state": goal["state"], "control": goal["control"],
              **(detail or {})})
    return goal


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def open_goal(thread: str, outcome: str, observable: list[str], main_task: int,
              product: str | None = None, gap: str | None = None,
              next_transition: str | None = None) -> dict:
    """Open one durable goal. Everything required here is required for a reason.

    Без наблюдаемого условия достижения цель нельзя ни закрыть, ни проверить, и
    она превращается в намерение — то самое, что уже терялось между сеансами.
    """
    if not outcome.strip():
        raise GoalError("у цели нет пользовательского результата")
    observable = [line.strip() for line in observable if line.strip()]
    if not observable:
        raise GoalError("у цели нет наблюдаемых условий достижения: "
                        "цель без них нельзя ни проверить, ни закрыть")
    with _Lock():
        goal = {
            "schema_version": 1,
            "id": _next_id(),
            "thread": thread,
            "product": product,
            "outcome": outcome.strip(),
            "observable": observable,
            "main_task": int(main_task),
            "state": ACTIVE,
            "control": NORMAL,
            "gap": (gap or "").strip() or None,
            "next_transition": (next_transition or "").strip() or None,
            "pause": None,
            "correctives": [],
            "signals": [],
            "created_at": now(),
            "updated_at": now(),
            "closed": None,
        }
        return _save(goal, "open", {"main_task": goal["main_task"]})


def set_fields(goal_id: str, gap: str | None = None,
               next_transition: str | None = None) -> dict:
    with _Lock():
        goal = load(goal_id)
        if gap is not None:
            goal["gap"] = gap.strip() or None
        if next_transition is not None:
            goal["next_transition"] = next_transition.strip() or None
        return _save(goal, "set", {"gap": goal["gap"], "next": goal["next_transition"]})


def add_signal(goal_id: str, code: str, text: str, src: str,
               goal: dict | None = None) -> dict:
    """Record one observed deviation and turn on control, visibly.

    Turning control on is a consequence of an observation, never of a mood, so a
    signal without a named observation is refused: «наблюдено» is the whole
    difference between a mode that can be checked and a mode that can be felt.
    """
    if code not in SIGNALS:
        raise GoalError(f"неизвестный признак отклонения {code!r}; известны: "
                        + ", ".join(sorted(SIGNALS)))
    if not str(src).strip():
        raise GoalError("признак отклонения назван, но не сказано, чем наблюдён")
    entry = {"at": now(), "code": code, "text": text.strip() or SIGNALS[code],
             "src": src.strip()}

    def apply(goal: dict) -> dict:
        known = {(item["code"], item["src"]) for item in goal["signals"]}
        if (entry["code"], entry["src"]) in known:
            return goal
        goal["signals"].append(entry)
        goal["control"] = REINFORCED
        return goal

    if goal is not None:                      # already inside a lock
        return apply(goal)
    with _Lock():
        return _save(apply(load(goal_id)), "signal", {"code": code, "src": entry["src"]})


def pause(goal_id: str, reason: str, src: str) -> dict:
    """Explicitly hold the main task. A pause nobody can see is not a pause."""
    if not reason.strip() or not str(src).strip():
        raise GoalError("пауза называется причиной и тем, чем эта причина наблюдена")
    with _Lock():
        goal = load(goal_id)
        if goal["state"] == CLOSED:
            raise GoalError("цель закрыта: её нельзя поставить на паузу")
        goal["state"] = PAUSED
        goal["pause"] = {"at": now(), "reason": reason.strip(), "src": src.strip()}
        return _save(goal, "pause", {"reason": reason.strip()})


def add_corrective(goal_id: str, task: int, effect: str,
                   return_criterion: str) -> dict:
    """Register a repair task the product owner decided on.

    Only from a pause, and only with both halves the user asked for: the user
    effect the repair is expected to produce, and the observable criterion for
    going back to the main task. Without the second one «после ремонта вернёмся»
    is a hope, and the chain stops at the closed repair — which is exactly the
    failure this whole mode exists to prevent.
    """
    if not effect.strip() or not return_criterion.strip():
        raise GoalError("корректирующая задача заводится с ожидаемым пользовательским "
                        "эффектом и наблюдаемым критерием возврата к основной задаче")
    with _Lock():
        goal = load(goal_id)
        if goal["state"] != PAUSED:
            raise GoalError(
                "корректирующая задача заводится только из явной паузы основной "
                f"работы: сначала `product_goal.py pause {goal_id} --reason ... --src ...`")
        if any(item["task"] == int(task) for item in goal["correctives"]):
            raise GoalError(f"задача {task} уже записана корректирующей у цели {goal_id}")
        goal["correctives"].append({
            "task": int(task), "effect": effect.strip(),
            "return_criterion": return_criterion.strip(),
            "added_at": now(), "accepted": None,
        })
        add_signal(goal_id, "corrective_task",
                   f"по цели заведена корректирующая задача {task}",
                   f"запись цели {goal_id} в {root()}", goal=goal)
        return _save(goal, "corrective", {"task": int(task)})


def settlement(item: dict) -> dict | None:
    """Чем эта корректирующая запись закрыта — приёмкой, снятием или ничем.

    One place knows both ways, because «держит ли этот ремонт основную работу» is
    one question and a reader who has to check two keys to answer it is how a
    retired repair ends up shown as accepted.
    """
    for kind in (ACCEPTED, RETIRED):
        record = item.get(kind)
        if record:
            return {"kind": kind, **record}
    return None


def pending(goal: dict) -> list[int]:
    """Корректирующие задачи, которые всё ещё держат основную работу."""
    return [item["task"] for item in goal.get("correctives", [])
            if not settlement(item)]


def _corrective(goal: dict, task: int) -> dict:
    for item in goal["correctives"]:
        if item["task"] == int(task):
            return item
    raise GoalError(f"у цели {goal['id']} нет корректирующей задачи {task}")


def accept_corrective(goal_id: str, task: int, src: str) -> dict:
    """Accept a repair — and say plainly that the goal is not closed by it."""
    if not str(src).strip():
        raise GoalError("приёмка называется тем, чем она наблюдена")
    with _Lock():
        goal = load(goal_id)
        item = _corrective(goal, task)
        if item.get(RETIRED):
            raise GoalError(
                f"корректирующая задача {task} снята с цели {goal_id} "
                f"({item[RETIRED]['reason']}): приёмкой её задним числом не заменяют")
        status = task_status(int(task))
        if status != "completed":
            raise GoalError(
                f"корректирующая задача {task} не принята: её статус наблюдается как "
                f"{status or 'неизвестный'}, а не completed")
        item[ACCEPTED] = {"at": now(), "src": str(src).strip()}
        return _save(goal, "accept_corrective", {"task": int(task)})


def retire_corrective(goal_id: str, task: int, reason: str, src: str) -> dict:
    """Снять ремонт, который сам себя не доставил: поглощён другой работой или отменён.

    Приёмка требует наблюдаемого `completed`, и требует правильно: она
    утверждает, что ремонт доставлен. Но ремонт бывает исчерпан иначе — его
    пользовательский эффект пришёл с другой принятой задачей, или он отменён в
    системе задач. Такая запись честно не `completed`, и приёмка ей была бы
    записанной неправдой; а без всякого выхода она запирает возврат основной
    работы навсегда, по бухгалтерии, а не по продукту.

    Поэтому у снятия своё слово и своя запись. Ничего на диске не утверждает
    «этот ремонт исчерпан» само: это суждение продакта — ровно как «ручной
    обход» и «расхождение версий», — поэтому оно называется причиной (чем именно
    доставлен эффект или где записана отмена) и тем, чем эта причина наблюдена,
    и без любой из двух половин не записывается. Снятие не заменяет приёмку:
    наблюдаемо завершённый ремонт снять нельзя, его принимают.
    """
    if not str(reason).strip() or not str(src).strip():
        raise GoalError("корректирующая задача снимается с причиной — чем именно "
                        "доставлен её эффект или где записана отмена — и с тем, "
                        "чем эта причина наблюдена")
    with _Lock():
        goal = load(goal_id)
        item = _corrective(goal, task)
        settled = settlement(item)
        if settled:
            raise GoalError(
                f"корректирующая задача {task} у цели {goal_id} уже "
                + ("принята" if settled["kind"] == ACCEPTED else "снята"))
        status = task_status(int(task))
        if status == "completed":
            raise GoalError(
                f"корректирующая задача {task} наблюдается завершённой: такую "
                f"принимают (`accept`), а не снимают")
        item[RETIRED] = {"at": now(), "reason": str(reason).strip(),
                         "src": str(src).strip(), "task_status": status}
        return _save(goal, "retire_corrective",
                     {"task": int(task), "reason": item[RETIRED]["reason"]})


def resume(goal_id: str, gap: str | None = None,
           next_transition: str | None = None) -> dict:
    """Return the main task to work. The step the chain used to stop before."""
    with _Lock():
        goal = load(goal_id)
        if goal["state"] == CLOSED:
            raise GoalError("цель закрыта: возвращать нечего")
        holding = pending(goal)
        if holding:
            raise GoalError(
                "нельзя вернуть основную работу: не приняты и не сняты "
                "корректирующие задачи "
                + ", ".join(str(number) for number in holding))
        goal["state"] = ACTIVE
        goal["pause"] = None
        if gap is not None:
            goal["gap"] = gap.strip() or None
        if next_transition is not None:
            goal["next_transition"] = next_transition.strip() or None
        return _save(goal, "resume", {"main_task": goal["main_task"]})


def close(goal_id: str, live_check: str, src: str) -> dict:
    """Close on a live pass of the user path through the installed product.

    Every cheaper piece of evidence is refused here rather than argued about in a
    prompt: a commit, a test, an approved review and a `completed` status are all
    true statements about the machinery and none of them is the user getting
    their result. `--src` is what observed the live pass.
    """
    if not live_check.strip() or not str(src).strip():
        raise GoalError("цель закрывается живой проверкой установленного продукта "
                        "и тем, чем эта проверка наблюдена")
    with _Lock():
        goal = load(goal_id)
        if goal["state"] == CLOSED:
            raise GoalError(f"цель {goal_id} уже закрыта")
        if goal["state"] == PAUSED:
            raise GoalError("цель на паузе: сначала верните основную задачу в работу")
        holding = pending(goal)
        if holding:
            raise GoalError("не приняты и не сняты корректирующие задачи "
                            + ", ".join(str(number) for number in holding))
        status = task_status(goal["main_task"])
        if status != "completed":
            raise GoalError(
                f"основная задача {goal['main_task']} наблюдается в статусе "
                f"{status or 'неизвестном'}: цель закрывается после неё, а не вместо неё")
        goal["state"] = CLOSED
        goal["closed"] = {"at": now(), "live_check": live_check.strip(),
                          "src": str(src).strip()}
        return _save(goal, "close", {"live_check": live_check.strip()})


# ---------------------------------------------------------------------------
# Observation: what can be seen in the artifacts of the task itself
# ---------------------------------------------------------------------------


def task_dir(task_id: int) -> Path | None:
    matches = sorted((TASKS_REPO / "tasks").glob(f"{int(task_id)}-*"))
    return matches[0] if matches else None


def task_status(task_id: int) -> str | None:
    """The task's own frontmatter status, read from disk and nothing else."""
    directory = task_dir(task_id)
    if directory is None:
        return None
    try:
        text = (directory / "task.md").read_text()
    except OSError:
        return None
    match = re.search(r'^status:\s*"?([a-z_]+)"?\s*$', text, re.MULTILINE)
    return match.group(1) if match else None


def review_rounds(directory: Path) -> tuple[int, list[str]]:
    """How many substantive review rounds this task has been through.

    `reviews/rounds.jsonl` is the machine record the pipeline writes; the trace
    is the fallback for a task that never had one. Both are the task's own
    artifacts, never the prose of a run.
    """
    path = directory / "reviews" / "rounds.jsonl"
    repeated: list[str] = []
    rounds = 0
    if path.is_file():
        for line in path.read_text().splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            rounds = max(rounds, int(record.get("round") or 0))
            repeated += [str(item) for item in record.get("repeated_finding_ids") or []]
        return rounds, sorted(set(repeated))
    trace = directory / "trace.md"
    if trace.is_file():
        for match in ROUND_LINE.finditer(trace.read_text()):
            rounds = max(rounds, int(match.group(1)))
    return rounds, repeated


def refusals(directory: Path) -> int:
    """How often a normal start/resume/retry of this task was refused."""
    trace = directory / "trace.md"
    if not trace.is_file():
        return 0
    return sum(1 for line in trace.read_text().splitlines() if REFUSAL_LINE.search(line))


def observe(goal: dict) -> list[dict]:
    """Deviations observable in the main task's own artifacts, right now.

    Only the mechanical half of the list lives here. «Ручной обход» and
    «расхождение принятой и установленной версии» are recorded by the product
    owner with what observed them, because nothing on disk states them without a
    judgement — and a guessed signal would turn the mode on for normal work.
    """
    directory = task_dir(goal["main_task"])
    if directory is None:
        return []
    found = []
    rounds, repeated = review_rounds(directory)
    if rounds >= REVIEW_ROUND_THRESHOLD:
        found.append({
            "code": "third_review_round",
            "text": f"ревью задачи {goal['main_task']} идёт {rounds}-й круг",
            "src": f"reviews/rounds.jsonl задачи {goal['main_task']} "
                   "(или строки «Review round N» её trace.md)",
        })
    if repeated:
        found.append({
            "code": "repeat_finding",
            "text": f"ревью задачи {goal['main_task']} повторило замечания "
                    + ", ".join(repeated),
            "src": f"поле repeated_finding_ids в reviews/rounds.jsonl задачи {goal['main_task']}",
        })
    refused = refusals(directory)
    if refused >= REFUSAL_THRESHOLD:
        found.append({
            "code": "launch_refused_again",
            "text": f"штатный запуск задачи {goal['main_task']} отказан {refused} раза",
            "src": f"строки об отказе запуска в trace.md задачи {goal['main_task']}",
        })
    return found


def apply_observed(goal_id: str) -> dict:
    """Record every newly observed deviation on the goal. Idempotent by source."""
    with _Lock():
        goal = load(goal_id)
        before = len(goal["signals"])
        for signal in observe(goal):
            add_signal(goal_id, signal["code"], signal["text"], signal["src"], goal=goal)
        if len(goal["signals"]) == before:
            # Nothing new was seen. Writing anyway would put one journal line per
            # tick per goal into the record of decisions, which is the one place
            # that must stay readable by a person.
            return goal
        return _save(goal, "observe", {"signals": len(goal["signals"])})


# ---------------------------------------------------------------------------
# Reading: what a wake-up and the board are shown
# ---------------------------------------------------------------------------


def live_tasks(goal: dict) -> list[int]:
    """Task numbers this goal is currently waiting on.

    Tolerant of a record that could not be parsed: such a goal has no task
    number, and the honest answer is an empty list rather than an exception that
    would take the whole board down over one unreadable file.
    """
    if goal.get("state") == PAUSED:
        return pending(goal)
    return [goal["main_task"]] if goal.get("main_task") is not None else []


def projection(goal: dict) -> dict:
    """One goal in the shape the board and the tick both read.

    Short on purpose: a panel column and a wake-up prompt need the same six
    answers — что обещано, где разрыв, чем занята работа сейчас, что дальше,
    почему пауза, включён ли контроль.
    """
    correctives = [{"task": item["task"], "effect": item["effect"],
                    "return_criterion": item["return_criterion"],
                    "settled": settlement(item)}
                   for item in goal.get("correctives", [])]
    return {
        "id": goal.get("id"),
        "thread": goal.get("thread"),
        "state": goal.get("state"),
        "control": goal.get("control"),
        "outcome": goal.get("outcome"),
        "observable": goal.get("observable", []),
        "main_task": goal.get("main_task"),
        "correctives": correctives,
        "gap": goal.get("gap"),
        "next_transition": goal.get("next_transition"),
        "pause": goal.get("pause"),
        "signals": [{"code": item["code"], "text": item["text"], "src": item["src"]}
                    for item in goal.get("signals", [])],
        "waiting_on": live_tasks(goal) if goal.get("state") != CLOSED else [],
        "updated_at": goal.get("updated_at"),
        "src": f"долговечная запись цели {goal.get('id')} в {root()}",
    }


def mark(item: dict) -> str:
    """Как называется состояние одного ремонта, одинаково везде, где его читают.

    Снятая запись не бывает написана словом «принята»: неправда в этой строке —
    ровно то, из-за чего снятие вообще понадобилось.
    """
    settled = settlement(item)
    if settled is None:
        return "в работе"
    if settled["kind"] == ACCEPTED:
        return "принята"
    return f"снята: {settled['reason']}"


def panel(thread: str | None = None) -> list[dict]:
    """Active goals of one direction, for the board and for the state file."""
    return [projection(goal) for goal in active(thread)]


def standing(thread: str, live_run_ids: list[int],
             actionable_task_ids: list[int] | None = None) -> list[str]:
    """Goals that are owed something and have no live run doing it.

    The tick wakes on transitions and on two standing states; a goal with nothing
    running is the third, and it is the whole point of the mode: «каждое фоновое
    пробуждение… при отсутствии живой работы запускает следующий безопасный шаг
    либо записывает конкретный внешний блокер».
    """
    said = []
    live = set(live_run_ids)
    actionable = (set(actionable_task_ids)
                  if actionable_task_ids is not None else None)
    for goal in active(thread):
        waiting = live_tasks(goal)
        if set(waiting) & live:
            continue
        # A stable blocked task or an explicit external wait has no safe move
        # for a model to make.  Its transition was already news once; waking
        # every twenty minutes after that only rereads the same context.  The
        # old two-argument call keeps the historical diagnostic behaviour for
        # readers that do not know which tasks are actionable.
        if actionable is not None:
            if waiting and not set(waiting) & actionable:
                continue
            if not waiting and goal.get("state") == PAUSED and not goal.get("unreadable"):
                continue
        where = ("основная задача" if goal.get("state") != PAUSED
                 else "корректирующие задачи")
        said.append(
            f"цель {goal['id']} стоит без живой работы: {goal['outcome'][:120]}; "
            f"{where} " + ", ".join(str(number) for number in waiting) +
            f"; ближайший разрыв: {goal.get('gap') or 'не назван'}")
    return said


def block(thread: str) -> str:
    """The goals block put in front of every wake-up, before anything is decided."""
    goals = active(thread)
    if not goals:
        return ""
    lines = []
    for goal in goals:
        marks = ", ".join(sorted({item["code"] for item in goal.get("signals", [])}))
        lines.append(
            f"- цель {goal['id']} [{goal['state']}, контроль {goal['control']}"
            + (f", признаки: {marks}" if marks else "") + "]\n"
            f"  результат для пользователя: {goal['outcome']}\n"
            "  условия достижения: " + "; ".join(goal.get("observable", [])) + "\n"
            f"  основная задача: {goal.get('main_task') or 'не названа'}\n"
            + "".join(
                f"  корректирующая задача {item['task']} "
                f"({mark(item)}): {item['effect']}; "
                f"возврат к основной: {item['return_criterion']}\n"
                for item in goal.get("correctives", []))
            + (f"  пауза: {goal['pause']['reason']} [{goal['pause']['src']}]\n"
               if goal.get("pause") else "")
            + f"  ближайший разрыв: {goal.get('gap') or 'не назван'}\n"
            f"  следующий переход: {goal.get('next_transition') or 'не назван'}\n")
    return f"""
Долговечные цели этого направления (хранятся в продуктовом контуре на диске и
переживают смену процесса, сеанса и модели — читай их до решения о запуске):
{chr(10).join(lines)}
По каждой активной цели на этом пробуждении обязателен один из двух исходов:
запущен следующий безопасный шаг по названной задаче — или в ответе назван
конкретный внешний блокер обычными словами. Закрытая корректирующая задача цель
не закрывает: после её приёмки основная задача возвращается в работу
(`product_goal.py accept` и затем `resume`), и цель закрывается только живой
проверкой исходного сценария через фактически установленный продукт
(`product_goal.py close --live-check ... --src ...`). Ремонт, который сам себя не
доставил — его эффект пришёл с другой принятой задачей или он отменён в системе
задач, — снимается словом снятия, а не приёмкой: `product_goal.py retire <id>
--task N --reason "чем именно доставлен эффект или где записана отмена" --src
"чем это наблюдено"`; после этого он основную работу не держит. Код продуктов
продакт руками не правит: работа уходит обычным путём задач.
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print(goal: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(goal, ensure_ascii=False, indent=2))
        return
    print(f"цель {goal['id']} [{goal['state']}, контроль {goal['control']}] "
          f"направление {goal['thread']}")
    print(f"  результат: {goal['outcome']}")
    for line in goal.get("observable", []):
        print(f"  условие: {line}")
    print(f"  основная задача: {goal['main_task']} (статус "
          f"{task_status(goal['main_task']) or 'неизвестен'})")
    for item in goal.get("correctives", []):
        print(f"  корректирующая {item['task']} [{mark(item)}]: {item['effect']}")
        print(f"      возврат к основной: {item['return_criterion']}")
        settled = settlement(item)
        if settled and settled["kind"] == RETIRED:
            print(f"      наблюдено: {settled['src']}")
    if goal.get("pause"):
        print(f"  пауза: {goal['pause']['reason']} ({goal['pause']['src']})")
    print(f"  ближайший разрыв: {goal.get('gap') or 'не назван'}")
    print(f"  следующий переход: {goal.get('next_transition') or 'не назван'}")
    for item in goal.get("signals", []):
        print(f"  признак {item['code']}: {item['text']} ({item['src']})")
    if goal.get("closed"):
        print(f"  закрыта {goal['closed']['at']}: {goal['closed']['live_check']} "
              f"({goal['closed']['src']})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="активные цели")
    listing.add_argument("--thread")
    listing.add_argument("--all", action="store_true", help="включая закрытые")
    listing.add_argument("--json", action="store_true")

    show = sub.add_parser("show")
    show.add_argument("id")
    show.add_argument("--json", action="store_true")

    opening = sub.add_parser("open", help="завести долговечную цель")
    opening.add_argument("--thread", required=True)
    opening.add_argument("--outcome", required=True)
    opening.add_argument("--observable", required=True, action="append",
                         help="наблюдаемое условие достижения; можно несколько раз")
    opening.add_argument("--main-task", required=True, type=int)
    opening.add_argument("--product")
    opening.add_argument("--gap")
    opening.add_argument("--next", dest="next_transition")

    setter = sub.add_parser("set", help="ближайший разрыв и следующий переход")
    setter.add_argument("id")
    setter.add_argument("--gap")
    setter.add_argument("--next", dest="next_transition")

    signal = sub.add_parser("signal", help="записать признак отклонения")
    signal.add_argument("id")
    signal.add_argument("--code", required=True, choices=sorted(SIGNALS))
    signal.add_argument("--text", default="")
    signal.add_argument("--src", required=True)

    observing = sub.add_parser("observe", help="снять признаки с артефактов задачи")
    observing.add_argument("id", nargs="?")
    observing.add_argument("--json", action="store_true")

    pausing = sub.add_parser("pause")
    pausing.add_argument("id")
    pausing.add_argument("--reason", required=True)
    pausing.add_argument("--src", required=True)

    corrective = sub.add_parser("corrective", help="завести корректирующую задачу")
    corrective.add_argument("id")
    corrective.add_argument("--task", required=True, type=int)
    corrective.add_argument("--effect", required=True)
    corrective.add_argument("--return-criterion", required=True)

    accept = sub.add_parser("accept", help="принять корректирующую задачу")
    accept.add_argument("id")
    accept.add_argument("--task", required=True, type=int)
    accept.add_argument("--src", required=True)

    retire = sub.add_parser("retire", help="снять корректирующую задачу, "
                                           "поглощённую другой работой или отменённую")
    retire.add_argument("id")
    retire.add_argument("--task", required=True, type=int)
    retire.add_argument("--reason", required=True,
                        help="чем именно доставлен её эффект или где записана отмена")
    retire.add_argument("--src", required=True)

    resuming = sub.add_parser("resume", help="вернуть основную задачу в работу")
    resuming.add_argument("id")
    resuming.add_argument("--gap")
    resuming.add_argument("--next", dest="next_transition")

    closing = sub.add_parser("close", help="закрыть цель живой проверкой")
    closing.add_argument("id")
    closing.add_argument("--live-check", required=True)
    closing.add_argument("--src", required=True)

    args = parser.parse_args()
    try:
        if args.command == "list":
            goals = load_all() if args.all else active(args.thread)
            if args.all and args.thread:
                goals = [goal for goal in goals if goal.get("thread") == args.thread]
            if args.json:
                print(json.dumps([projection(goal) for goal in goals],
                                 ensure_ascii=False, indent=2))
                return 0
            if not goals:
                print("активных целей нет")
                return 0
            for goal in goals:
                _print(goal, False)
            return 0
        if args.command == "show":
            _print(load(args.id), args.json)
            return 0
        if args.command == "open":
            _print(open_goal(args.thread, args.outcome, args.observable,
                             args.main_task, args.product, args.gap,
                             args.next_transition), False)
            return 0
        if args.command == "set":
            _print(set_fields(args.id, args.gap, args.next_transition), False)
            return 0
        if args.command == "signal":
            _print(add_signal(args.id, args.code, args.text, args.src), False)
            return 0
        if args.command == "observe":
            ids = [args.id] if args.id else [goal["id"] for goal in active()]
            found = []
            for goal_id in ids:
                found += [{"goal": goal_id, **signal} for signal in observe(load(goal_id))]
                apply_observed(goal_id)
            if args.json:
                print(json.dumps(found, ensure_ascii=False, indent=2))
                return 0
            if not found:
                print("отклонений в артефактах не наблюдается")
                return 0
            for item in found:
                print(f"цель {item['goal']}: {item['code']} — {item['text']} ({item['src']})")
            return 0
        if args.command == "pause":
            _print(pause(args.id, args.reason, args.src), False)
            return 0
        if args.command == "corrective":
            _print(add_corrective(args.id, args.task, args.effect,
                                  args.return_criterion), False)
            return 0
        if args.command == "accept":
            goal = accept_corrective(args.id, args.task, args.src)
            _print(goal, False)
            print("корректирующая задача принята; цель этим не закрыта — "
                  "верните основную задачу в работу командой resume")
            return 0
        if args.command == "retire":
            goal = retire_corrective(args.id, args.task, args.reason, args.src)
            _print(goal, False)
            print("корректирующая задача снята и основную работу больше не держит; "
                  "приёмкой это не считается — доставки этой задачей не было")
            return 0
        if args.command == "resume":
            _print(resume(args.id, args.gap, args.next_transition), False)
            return 0
        if args.command == "close":
            _print(close(args.id, args.live_check, args.src), False)
            return 0
    except GoalError as error:
        print(f"отказ: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
