#!/usr/bin/env python3
"""The contract between the state collector, the recorder and the map.

The map renderer never reads a repository, a task directory or the disk: it gets
a snapshot document and a timeline of records, and nothing else. That promise is
only worth something if the shape of those two documents is written down and
checked, so this module is the single place where the shape lives — the collector
produces it, the recorder appends to it, the renderer consumes it, the tests
assert on it.

Anonymisation lives here for the same reason. It is a property of a document,
not of whoever produced it, and all three sides need it: the collector cleans a
snapshot, the scribe cleans a record, the renderer cleans a timeline it was
handed. Putting it in the collector meant the renderer could not clean anything
without importing the collector and losing the physical boundary (finding HIGH-2
of review 780), so `--anonymize` silently became a property of how a file had
once been written rather than of how it is being shown (finding HIGH-3 of review
786). This module reads no files, so everyone can import it.

Nothing here touches the disk. It is a description, two validators and one
cleaner.
"""
from __future__ import annotations

import re
import sys

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Snapshot: one document describing every thread at one instant.
# ---------------------------------------------------------------------------

SNAPSHOT_FIELDS = ("schema_version", "mode", "threads", "products", "owners_awake",
                   # The existing SQLite task index as a lookup surface. Its list
                   # fields come from the index; an initial/self-contained snapshot
                   # may attach a card collected from the authoritative task directory.
                   "task_index",
                   # Порядок работ и паузы, прочитанные у их владельца — текущей
                   # редакции портфельного плана. Наблюдение не выводит очередь
                   # само: `planned` очередью не является, и доска, которая
                   # строила её из статусов, показывала не тот порядок, который
                   # установлен планом (задача 1156).
                   "plan",
                   # На какой ревизии работает то, что человек сейчас смотрит, и
                   # что лежит в дереве. Служба держит свой Python в памяти с
                   # запуска, а шаблон читает с диска на каждый запрос: 14 августа
                   # пользователь полчаса читал смесь двух версий, и понять это
                   # можно было только расследованием (находка 8 задачи 1163).
                   "revision")
THREAD_FIELDS = ("key", "title", "products", "task_count", "tasks", "repos", "channels",
                 # The direction's own last wake-up: when it looked and what came
                 # of it. `None` until a tick has written one.
                 "check",
                 # When it looks next, observed as the snapshot is built rather
                 # than carried out of that record — a separate field because it
                 # is a fact about the present, not about the last check.
                 "next_check",
                 # Whether a check of this direction is running right now, and
                 # which unit would run one. Observed of systemd at the same
                 # instant as `next_check` and for the same reason: «продакт
                 # сейчас проверяет» is a fact about the present moment, and the
                 # board offers «продолжить сейчас» against it.
                 "wake")

TASK_INDEX_FIELDS = ("id", "task", "title", "status", "updated_at", "updated_src")

# What a next-check observation carries. `at` may be `None` — a timer systemd is
# holding unarmed is an honest gap — but then `src` has to say what was seen
# instead, because a board that shows «следующая проверка: неизвестно» owes the
# reader the reason just as much as one that shows a time.
NEXT_CHECK_FIELDS = ("at", "src")

# What the wake observation carries. `unit` is the systemd unit that performs one
# wake-up of the direction — the same one the twenty-minute timer starts, which
# is why a click and a tick cannot produce two product owners. `running` is
# `None` when this host had no answer to give: «проверка не идёт» and «спросить
# было не у кого» are different claims, and only the first one may grey the
# button out.
WAKE_FIELDS = ("unit", "running", "src")

# What the wake-up record has to carry. `outcome` is the sentence the board
# prints and `outcome_src` says what produced it, on the same rule every other
# caption on this board lives under: the outcome is derived from what the tick
# observed before and after waking the owner, never from what the woken owner
# wrote about itself.
CHECK_FIELDS = ("at", "outcome", "outcome_src",
                # What the check put in motion, or `None` while the owner is
                # still deciding. «Ещё не запустил» and «не стал запускать» are
                # two different states, and only the second one owes a reason.
                "woke_owner", "started", "events", "reasons", "queue", "src")

# One named reason the owner started nothing. The user listed the kinds outright:
# «занят другой продакт, исчерпан бюджет, занято рабочее дерево, ждём ответа
# пользователя, нет проверяющего». A reason with no observation behind it is the
# invented caption the whole board refuses, so `src` is required.
REASON_FIELDS = ("code", "text", "src")

# The kinds of product owner instance that can be awake. `tick` is the one
# `product-thread@<тред>.timer` starts and the only one that was ever observed;
# `session` is the console owner, which is the *other* half of the pair that
# created 790/792 and 791/793 in one hour and which no observation matched.
# `woken` is retained in the version-1 input contract for snapshots written by
# the previous collector. The current collector folds that child into its
# observable tick parent, so one timer wake-up is one instance. `mail` is a
# detached owner whose `/proc` ancestry leads through the Gmail poller.
OWNER_KINDS = ("tick", "session", "woken", "mail")

# What a durable goal has to carry to be shown at all. It is product memory
# rather than an observation of a process, so what the board prints is the
# promise («какой пользовательский результат обещан»), where it stands («где
# ближайший разрыв»), what is being waited on and under which control — and, on
# the same rule as every caption here, what observed all of it.
GOAL_FIELDS = ("id", "state", "control", "outcome", "observable", "main_task",
               "correctives", "gap", "next_transition", "pause", "signals",
               "waiting_on", "updated_at", "src")
GOAL_STATES = ("active", "paused", "closed")
# How a corrective task stopped holding the main task, and the two are different
# statements: `accepted` is a delivered repair observed `completed`, `retired` is
# one taken off the list without delivering itself. The board prints them
# differently because writing «принята» over a retired repair is the untruth the
# retirement exists to avoid.
GOAL_SETTLEMENTS = ("accepted", "retired")
# Two kinds of attention, and the second one is turned on by an observed
# deviation rather than chosen. Normal work never gets it.
GOAL_CONTROLS = ("normal", "reinforced")
TASK_FIELDS = ("id", "title", "status", "dir", "run", "gates", "flags", "board", "detail",
               # Whose question it is, kept apart in the document rather than
               # rederived by every reader: the areas, the counter above the
               # columns and the tests must not be able to disagree about it.
               "questions", "asked_user", "our_questions")
# `alive_src` says what answered the liveness question, on the same rule as
# `actor_src`: a run called live has to name what said so, and the answer now
# comes from the runner that recorded the process rather than from the existence
# of a PID number (finding MEDIUM-2 of review 786).
RUN_FIELDS = ("state", "runner", "workflow", "alive", "alive_src", "progress",
              # `repo` is what the run was pointed at and is what makes «занят
              # репозиторий» observable rather than assumed; `refusal` is the
              # contour's own written verdict that an owner stopped before its
              # closing step, which nothing used to read.
              "repo", "refusal")
REPO_FIELDS = ("name", "present")
CHANNEL_FIELDS = ("channel", "direction", "count")

MODES = ("demo", "real")

# ---------------------------------------------------------------------------
# Board: the plates the user asked for, and the areas they stand in.
# ---------------------------------------------------------------------------

# Areas of one direction panel, top to bottom. The order is urgency, not the
# alphabet, and it is part of the contract: «ждёт решения человека» being first
# and always visible is the whole answer to one of the three acceptance
# questions, so a renderer may not reorder it silently.
BOARD_AREAS = (
    "waiting_human",  # the *user* was asked in writing and has not answered
    "running",        # a process is alive right now
    "stuck",          # a dead run under a living label, a kill, a failed gate
    "decision_unmet", # a decision was recorded and nothing observed it carried out
    "undelivered",    # finished, a document exists, nothing observed it delivered
    "product_owner",  # our own open question: the product owner owes the answer
    "ready_to_start", # a condition was written down, and it has since been met
    "pickup",         # nothing running, nothing observably holding it: start it
    "queued",         # held by something named, and the name is next to it
    "backlog",        # the current plan revision holds this task back in words
    "plan",           # promised in a product record and never made a task
    "done",           # terminal, and the result reached a person
)

BOARD_AREA_RU = {
    "waiting_human": "Ждёт решения пользователя",
    "running": "В работе сейчас",
    "stuck": "Затор",
    "decision_unmet": "Решено, но не исполнено",
    "undelivered": "Сделано, но не доставлено",
    "product_owner": "Решает продакт",
    "ready_to_start": "Готово к запуску",
    "pickup": "Можно подхватить",
    "queued": "В очереди, за чем стоит",
    "backlog": "В бэклоге: сам не запустится",
    "plan": "Надо запланировать",
    "done": "Сделано и доставлено",
}

# `backlog` is the fourth question the user asked by name: «что просто в бэклоге
# и автоматом не запустится (неразобранное и разобранное, но на паузе)». Only one
# of his two kinds is observable, and the area carries only that one: «на паузе»
# is a task the current plan revision holds back in its own words, and the words
# stand beside it. «Неразобрано» has no observable signal here — review round 1
# showed on four tasks that «the revision does not name it» is a different claim
# from «nobody analysed it» — and an area that cannot be observed is left out
# rather than filled with an inference. That is not a property of the task on
# disk either — `planned` means both and neither — which is why the owner of the
# answer is the plan and the board only reads it.
BACKLOG_KINDS = ("paused",)

# `pickup` stands above `queued` on purpose, and both were one area called «в
# очереди» before. That single area answered neither of the two questions the
# user actually opens the board with — «что можно подхватить прямо сейчас»,
# which is the first question of every wake-up, and «за чем стоит остальное».
# The split is one observation: whether anything on disk is holding the task.
#
# `plan` holds no tasks at all. It carries lines written in a product record for
# which no task could be observed — the place where «посмотреть код client»
# stood for two days because the flow had nowhere to put «надо запланировать».
AREAS_WITHOUT_TASKS = ("plan",)

# `waiting_human` and `product_owner` are one split, not two independent areas,
# and the split is the whole of Part 1 of task 817: on the live state of
# 2026-08-06 the first area held sixteen entries and the user owned three of
# them. The other thirteen were our own product decisions, questions to an
# executor, a repair already shipped, and questions the user had answered in
# writing. Nothing is hidden by the split — everything that left the first area
# stands in the second, in front of the person who actually owes the decision.
#
# Which side a question falls on is decided in `process_map_state.question_entry`
# and nowhere else. Two observations: a written mark that the question went to
# the user, with the date, the channel and the identifier of the message; and no
# answer observed since, neither a letter of the user in the same thread nor a
# decision written into the line.
#
# `undelivered` is Part 3, and it exists because a task can be finished and its
# result still never seen: 783 left a 441 KB report in `deliverables/` and sent
# two receipts, both about the life of the run.
OWED_BY = ("user", "product")

# One open question, wherever it was written down. `asked_src` is required of a
# question owed by the user, on the same rule as `actor_src`: an area that tells
# a person they are blocking work has to be able to say what put the question in
# front of them and when.
QUESTION_FIELDS = ("text", "owner", "asked_at", "channel", "ref",
                   "asked_src", "answer_src", "note")

# ---------------------------------------------------------------------------
# The plan: who owns the order of work, and what stands outside it
# ---------------------------------------------------------------------------
#
# The board used to derive the queue from observation, and observation cannot
# derive it: `planned` is a status, not a place in a queue, and the plan says so
# itself in the line it prints last — «не является очередью: все прочие задачи со
# статусом planned». On 2026-08-13 that showed the user two tasks under «в
# очереди» while the current revision of the portfolio plan ordered three others,
# and showed nothing at all for the work standing on his own word.
#
# So the order and the pauses are read from their owner, one revision file, and
# the board stays a reader: it never writes a plan, never renumbers one, and
# never invents an entry the revision does not carry.
PLAN_FIELDS = ("revision", "accepted_at", "src", "outcomes", "queue", "backlog")

# One result named by the current plan's ``now`` section. It carries the line
# unchanged, the tasks the line names, and the next line of the same result.
# Current state and time are deliberately not stored here: the renderer joins
# this plan-owned projection with the already collected task observations.
PLAN_OUTCOME_FIELDS = ("title", "text", "tasks", "goals", "next", "checked")

# What one line of the plan carries onto the board. `text` is the line as the
# product owner wrote it — never a paraphrase — and `checked` says what it was
# compared against, on the same rule as a product promise: an entry may report a
# comparison that failed, never the absence of a task.
# `also` — номера, названные строкой, но не стоящие её предметом. Отдельным полем
# от `tasks`, а не вперемешку: предметом решается порядок очереди, а упоминанием
# ничего не решается, и различить их обязана схема, а не чтение глазами.
PLAN_ENTRY_FIELDS = ("field", "text", "tasks", "also", "checked")

# Which field of the revision the entry stands in, and — for a backlog entry —
# on what observed ground it is held.
PLAN_ENTRY_KINDS = BACKLOG_KINDS

# What the plan says about one task. `unnamed` is a claim about the revision and
# not about the task: the revision was read and does not name this number. It
# says nothing about whether the task was analysed and decides nothing about
# whether it may start — both were read out of it once, and both were wrong.
#
# Держит работу только `paused`, и только там, где задача стоит предметом
# строки. `mentioned` не держит ничего и области не меняет; он существует, чтобы
# доска не говорила «план о ней не говорит» про задачу, которую план называет.
# Роли `held` — «названа строкой, которая держит работу» — здесь больше нет: на
# живом снимке она объявляла остановленной работу, разрешённую пользователем той
# же строкой (круг 3 независимого ревью 1156).
PLAN_ROLES = ("queue", "paused", "named", "mentioned", "unnamed")
PLAN_PLACE_FIELDS = ("role", "position", "line", "field", "ahead", "conflict", "src")

# One line of that area. `checked` says what the line was compared against, and
# it is required for the same reason `actor_src` is: the area may report a failed
# comparison, never the absence of a task, and the difference is only visible if
# the comparison is shown. `link` is `unknown` for everything the area prints —
# a line whose task was observed is that task and does not stand here.
PROMISE_FIELDS = ("text", "link", "checked")
PROMISE_LINKS = ("unknown",)

# Fields of one plate. `actor`, `role` and `why` are nullable on purpose: an
# empty cell is the honest answer when nothing on disk names the executor or the
# reason, and it is better than a confident invention (finding MEDIUM-3 of review
# 780). `why`/`why_src` carry the observed reason a task stands where it stands —
# without it the board named the jam and could not answer «why», which is one of
# the three acceptance questions (finding HIGH-2 of review 786). `since_src` says
# which file's mtime the age is actually measured from, so the caption stops
# implying a state transition it never observed (finding MEDIUM-1).
BOARD_FIELDS = ("area", "actor", "actor_src", "role", "role_src",
                "happening", "why", "why_src", "since", "since_src",
                "age_seconds", "attempt",
                # What is holding a queued task, and what observed it. «В
                # очереди» without this is a label, not an answer: the user
                # asked for «за чем именно стоит».
                "blocked_by", "blocked_by_src",
                # Что о задаче говорит действующая редакция плана: место в
                # очереди, пауза, упоминание или молчание. `None` — плана нет
                # вовсе, и тогда доска про очередь не говорит ничего, а не
                # выводит её из статусов.
                "plan_place",
                # The start condition as a field rather than a sentence, and the
                # decision recorded on the task. Both are `None` when the task
                # named neither, and `None` never means «условие выполнено»: a
                # condition nobody wrote down and a condition that has cleared
                # are two different states and 831 stood for forty minutes in the
                # gap between them.
                "start_condition", "decision")

# What a plate shows when it is opened. Collected by the observer rather than
# fetched by the page: the renderer may not reach a disk, so a drill-down that
# went looking for its own detail would be a second door past the boundary the
# split exists to hold. Everything here is a name, a count or a verdict already
# written down — no child transcript is read to build it.
DETAIL_FIELDS = ("review", "delivery", "files", "moved", "moved_age_seconds", "moved_src",
                 # What the task is, in the words of the person who asked for
                 # it: `## Summary` of `task.md`. Everything else on the card is
                 # state, and the card used to carry only state.
                 "summary",
                 # The document made for a person and whether anything observed
                 # it reaching them. `None` means the task made no document.
                 "handoff")

# What a handoff observation has to carry. `delivered_src` is required whichever
# way it came out: «доставлено» and «не доставлено» are both claims about the
# world, and the area that prints them has to say what it read.
HANDOFF_FIELDS = ("name", "bytes", "count", "src", "delivered", "delivered_src")

# Task flags promised to the renderer. Each one has to be visible as a shape on
# the map, so adding a flag here is a change to the picture, not only to data.
TASK_FLAGS = (
    "live",         # a figure is working on the object right now
    "stale_label",  # the label claims more than the observation supports
    "blocked",      # the object is in chains, the reason next to it
    "gap",          # a gate said GAP/BLOCKED/FAIL
    "killed",       # supervision stopped it: neither working nor done
    "delivered",    # the delivered crate
    "idle",         # nobody is working on it
    # The owner stopped before its closing step and the directory kept moving
    # afterwards: work that may have finished outside the dead process. Task 757
    # sat finished for three and a half hours behind exactly this, and 712
    # before it, because nothing read the refusal the contour writes itself.
    "work_outside_owner",
)

# Two more shapes on the map are not properties of a task and are not stored as
# task flags, because nothing on disk attributes them to one task:
# «сделано, но не опубликовано» is a property of a repository (a dirty tree or
# unpushed commits), and «ждёт человека» is a product's open question. The
# renderer derives them from `repos` and from `products` respectively.
REPO_FIELDS_DIRTY = ("tracked_dirty", "unpushed")

# ---------------------------------------------------------------------------
# Timeline: append-only records, one per observed change.
# ---------------------------------------------------------------------------

TIMELINE_FIELDS = ("schema_version", "at", "kind", "label", "observed_by")

TIMELINE_KINDS = (
    "task_appeared",
    "task_status",
    "pipeline_event",
    "artifact",
    "activity",
    "commit",
    "notification",
    "mail",
)

# Stations of the development team inside a task object. A station only exists
# on the map when something was actually observed for it; there is no declared
# role state machine in the contour, so the map must not invent one.
STATIONS = ("analysis", "development", "tests", "review", "commit", "report")

# Channels are first-class objects: the user named them directly.
CHANNELS = ("email", "telegram", "git")

ISO8601 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


class ContractError(ValueError):
    """Raised when a document does not match the contract the renderer relies on."""


def run_entrypoint(main) -> int:
    """Run an entry point, turning a refusal to observe into one sentence.

    Most of these refusals are an installation that has not been set up yet —
    no durable content root, no task system named — and a traceback is the least
    useful way to tell somebody that. The refusal keeps its own words and gets a
    non-zero exit code, so a timer still fails and a person still reads why.
    """
    try:
        return main() or 0
    except ContractError as exc:
        print(f"продакт отказывается наблюдать: {exc}", file=sys.stderr)
        return 2


def _require(payload: dict, fields, where: str) -> None:
    missing = [field for field in fields if field not in payload]
    if missing:
        raise ContractError(f"{where}: отсутствуют поля {missing}")


def validate_question(question: dict, where: str) -> dict:
    """Check one open question; return it unchanged or raise ContractError.

    A question owed by the user carries the mark that put it in front of them.
    Without that the area is back to what it was: a list of everything the
    contour had written down anywhere, presented to a person as their debt.
    """
    if not isinstance(question, dict):
        raise ContractError(f"{where}: ожидался объект с владельцем, а не строка")
    _require(question, QUESTION_FIELDS, where)
    if question["owner"] not in OWED_BY:
        raise ContractError(f"{where}: владелец {question['owner']!r}")
    if not str(question["text"]).strip():
        raise ContractError(f"{where}: пустой текст")
    if question["owner"] == "user" and not str(question["asked_src"] or "").strip():
        raise ContractError(f"{where}: вопрос отнесён пользователю, "
                            "но не сказано, чем наблюдено, что его спросили")
    return question


def validate_check(check, where: str):
    """Check one direction's wake-up record; return it unchanged or raise.

    `None` is allowed and means no tick has ever written for this direction. It
    is not the same claim as «проверял и не нашёл, что запустить», and the board
    may not print the second when it observed the first.
    """
    if check is None:
        return None
    if not isinstance(check, dict):
        raise ContractError(f"{where}: ожидался объект")
    _require(check, CHECK_FIELDS, where)
    if not ISO8601.match(str(check["at"])):
        raise ContractError(f"{where}: время {check['at']!r} не ISO 8601")
    if not str(check["outcome"]).strip():
        raise ContractError(f"{where}: чем кончилась проверка — пусто")
    if check["outcome"] and not str(check["outcome_src"] or "").strip():
        raise ContractError(f"{where}: итог проверки назван, но не сказано, чем наблюдён")
    for reason in check["reasons"]:
        _require(reason, REASON_FIELDS, f"{where}: причина")
        if not str(reason["text"]).strip() or not str(reason["src"]).strip():
            raise ContractError(f"{where}: причина названа, но не сказано, чем наблюдена")
    return check


def validate_next_check(value, where: str) -> dict:
    """Check one direction's next-check observation; return it or raise.

    Unlike the wake-up record this is never `None`: the question «когда продакт
    проверит статус в следующий раз» is asked of systemd whenever the board is
    built, so there is always an answer — either a time, or what was seen
    instead of one. A direction no tick has ever run for still has a timer.
    """
    if not isinstance(value, dict):
        raise ContractError(f"{where}: ожидался объект")
    _require(value, NEXT_CHECK_FIELDS, where)
    if value["at"] is not None and not ISO8601.match(str(value["at"])):
        raise ContractError(f"{where}: время {value['at']!r} не ISO 8601")
    if not str(value["src"] or "").strip():
        raise ContractError(f"{where}: не сказано, чем наблюдена")
    return value


def validate_wake(value, where: str) -> dict:
    """Check one direction's wake observation; return it or raise ContractError."""
    if not isinstance(value, dict):
        raise ContractError(f"{where}: ожидался объект")
    _require(value, WAKE_FIELDS, where)
    if not str(value["unit"] or "").strip():
        raise ContractError(f"{where}: не названа единица пробуждения")
    if value["running"] not in (True, False, None):
        raise ContractError(f"{where}: идёт ли проверка — {value['running']!r}")
    if not str(value["src"] or "").strip():
        raise ContractError(f"{where}: не сказано, чем наблюдено")
    return value


# Areas that say nothing observable is holding the task back, so a direction
# standing in them has work it could start right now. One list, because the
# wake-up (`thread_state.py`, and `thread_tick.startable` over its lists) and the
# board's «продолжить сейчас» have to mean the same thing by «есть что начать»;
# two copies of it would be two answers to one question.
STARTABLE_AREAS = ("pickup", "ready_to_start", "decision_unmet")


def startable(task: dict) -> bool:
    """Whether this task could be put on a child right now, by its area alone.

    The plan's queue counts: a task the current revision gave a place to, with
    no observed holder, is work with a назначенное место rather than work that
    is held — and the wake-up has always treated it as startable. What the plan
    holds by its own word (`backlog`) and what stands behind an observed holder
    (`queued` with `blocked_by`) do not.
    """
    board = task.get("board") or {}
    if board.get("area") in STARTABLE_AREAS:
        return True
    place = board.get("plan_place") or {}
    return (board.get("area") == "queued" and place.get("role") == "queue"
            and not board.get("blocked_by"))


GOAL_SESSION_FIELDS = ("live", "reason", "id", "engine", "model", "turns",
                       "opened_at", "heartbeat", "last_turn_at",
                       "last_turn_reaction_seconds", "post_check", "recovered",
                       "stopped", "src")


def validate_goal_session(session: dict, where: str) -> dict:
    """Check the continuous session projection; return it or raise ContractError.

    «Сессия жива» — самая дорогая надпись в этом режиме: пока она стоит, тик
    сознательно не поднимает второго продакта. Поэтому она обязана назвать, чем
    наблюдена, и обязана назвать причину, когда сессия не жива, — иначе доска
    показывала бы «ведёт сессия» там, где не ведёт никто.
    """
    if not isinstance(session, dict):
        raise ContractError(f"{where}: ожидался объект")
    _require(session, GOAL_SESSION_FIELDS, where)
    if not isinstance(session["live"], bool):
        raise ContractError(f"{where}: живость должна быть булевой")
    if not str(session["src"] or "").strip():
        raise ContractError(f"{where}: сессия показана, но не сказано, чем наблюдена")
    if not str(session["reason"] or "").strip():
        raise ContractError(f"{where}: не сказано, почему сессия жива или не жива")
    if session["live"] and not str(session["id"] or "").strip():
        raise ContractError(f"{where}: живая сессия без идентификатора разговора")
    post_check = session["post_check"]
    if post_check is not None and (
            not isinstance(post_check, dict)
            or not isinstance(post_check.get("resolved"), bool)
            or not str(post_check.get("how") or "").strip()
            or not str(post_check.get("src") or "").strip()
            or (not post_check["resolved"]
                and not str(post_check.get("told") or "").strip())):
        raise ContractError(f"{where}: пост-контроль цели не называет исход и наблюдение")
    return session


def validate_goal(goal: dict, where: str) -> dict:
    """Check one durable goal; return it unchanged or raise ContractError.

    The same rule the plates live under: what is shown as a fact says what
    observed it. A goal also has to carry the user result and the main task —
    without them the board would print a mode without a promise, which is the
    caption this whole area exists to avoid.
    """
    if not isinstance(goal, dict):
        raise ContractError(f"{where}: ожидался объект")
    _require(goal, GOAL_FIELDS, where)
    if goal["state"] not in GOAL_STATES:
        raise ContractError(f"{where}: состояние {goal['state']!r}")
    if goal["control"] not in GOAL_CONTROLS:
        raise ContractError(f"{where}: контроль {goal['control']!r}")
    if not str(goal["outcome"]).strip():
        raise ContractError(f"{where}: пустой пользовательский результат")
    if not str(goal["src"] or "").strip():
        raise ContractError(f"{where}: цель показана, но не сказано, чем наблюдена")
    for signal in goal["signals"]:
        _require(signal, ("code", "text", "src"), f"{where}: признак отклонения")
        if not str(signal["src"] or "").strip():
            raise ContractError(f"{where}: признак назван, но не сказано, чем наблюдён")
    for corrective in goal["correctives"]:
        _require(corrective, ("task", "effect", "return_criterion", "settled"),
                 f"{where}: корректирующая задача")
        # Both halves or neither: «заведём ремонт» without the observable
        # criterion for coming back is how a chain stops at the closed repair.
        if not str(corrective["effect"]).strip() or not str(corrective["return_criterion"]).strip():
            raise ContractError(
                f"{where}: корректирующая задача без пользовательского эффекта "
                "или без критерия возврата к основной задаче")
        _validate_settlement(corrective["settled"], f"{where}: корректирующая задача")
    return goal


def _validate_settlement(settled, where: str) -> None:
    """Ремонт, снятый с основной работы, называет чем — и по чему это видно.

    Same rule as every caption on this board: shown and not saying what observed
    it is refused. A retirement carries one thing more than an acceptance — the
    reason it was taken off the list — because nothing on disk states that by
    itself, and a reason nobody wrote is how «снята» becomes «замолчана».
    """
    if settled is None:
        return
    if not isinstance(settled, dict):
        raise ContractError(f"{where}: ожидался объект с тем, чем ремонт закрыт")
    _require(settled, ("kind", "at", "src"), where)
    if settled["kind"] not in GOAL_SETTLEMENTS:
        raise ContractError(f"{where}: ремонт закрыт способом {settled['kind']!r}")
    if not str(settled["src"] or "").strip():
        raise ContractError(f"{where}: ремонт закрыт, но не сказано, чем это наблюдено")
    if settled["kind"] == "retired" and not str(settled.get("reason") or "").strip():
        raise ContractError(f"{where}: ремонт снят, но не названа причина")


def validate_plan_entry(entry: dict, where: str, kind: bool = False) -> dict:
    """Check one line of the plan on its way to the board; return it or raise."""
    if not isinstance(entry, dict):
        raise ContractError(f"{where}: ожидался объект со строкой плана")
    _require(entry, PLAN_ENTRY_FIELDS, where)
    if not str(entry["text"]).strip():
        raise ContractError(f"{where}: пустая строка плана")
    if not str(entry["checked"] or "").strip():
        # The same rule the «Надо запланировать» area lives under: an entry that
        # names a task, or reports that it could name none, has to say what it
        # compared. Without that the queue is believable and not checkable.
        raise ContractError(f"{where}: не сказано, с чем сверена строка")
    if kind and entry.get("kind") not in PLAN_ENTRY_KINDS:
        # Два вида бэклога пользователь назвал сам, и различимы они обязаны быть
        # в данных, а не в чтении заголовка глазами.
        raise ContractError(f"{where}: род записи бэклога {entry.get('kind')!r}")
    for task in entry["tasks"]:
        _require(task, ("id", "title", "status"), f"{where}: задача строки")
    for task in entry["also"]:
        _require(task, ("id", "title", "status"), f"{where}: задача, названная строкой")
    subject = {task["id"] for task in entry["tasks"]}
    if subject & {task["id"] for task in entry["also"]}:
        # Одна задача не может быть и предметом строки, и её упоминанием: тогда
        # различие ничего не значит, а по нему решается, назначает ли строка
        # задаче место — очередь или паузу — или только называет её.
        raise ContractError(f"{where}: задача стоит и предметом строки, и упоминанием")
    return entry


def validate_plan(plan: dict, where: str = "план") -> dict:
    """Check the plan projection; return it unchanged or raise ContractError.

    `revision` may be `None` and then both lists are empty: «плана нет» is a
    real answer and the honest one. What it may never be is a queue built from
    somewhere else — the whole point of reading the plan is that nothing else
    knows the order.
    """
    if not isinstance(plan, dict):
        raise ContractError(f"{where}: ожидался объект")
    _require(plan, PLAN_FIELDS, where)
    if not str(plan["src"] or "").strip():
        raise ContractError(f"{where}: не сказано, чем наблюдён")
    if plan["revision"] is None and (plan["outcomes"] or plan["queue"] or plan["backlog"]):
        raise ContractError(f"{where}: редакции нет, а результаты, очередь или бэклог не пусты")
    for index, outcome in enumerate(plan["outcomes"]):
        outcome_where = f"{where}: результат дня, строка {index + 1}"
        _require(outcome, PLAN_OUTCOME_FIELDS, outcome_where)
        if not str(outcome["title"] or "").strip() or not str(outcome["text"] or "").strip():
            raise ContractError(f"{outcome_where}: пустое название или строка")
        if not str(outcome["checked"] or "").strip():
            raise ContractError(f"{outcome_where}: не сказано, с чем сверена строка")
        if not isinstance(outcome["next"], list) or not all(
                isinstance(line, str) and line.strip() for line in outcome["next"]):
            raise ContractError(f"{outcome_where}: next должен быть списком непустых строк")
        for task in outcome["tasks"]:
            _require(task, ("id", "title", "status"), f"{outcome_where}: задача")
        if not isinstance(outcome["goals"], list) or not all(
                isinstance(goal, str) and goal.strip() for goal in outcome["goals"]):
            raise ContractError(f"{outcome_where}: goals должен быть списком идентификаторов")
    for index, entry in enumerate(plan["queue"]):
        validate_plan_entry(entry, f"{where}: очередь, строка {index + 1}")
    for index, entry in enumerate(plan["backlog"]):
        validate_plan_entry(entry, f"{where}: бэклог, строка {index + 1}", kind=True)
    return plan


def validate_plan_place(place, where: str):
    """Check what the plan says about one task; return it or raise.

    `None` means no plan revision was published at all. It is not the same claim
    as «план эту задачу не называет», and the two must not collapse: the first
    says the owner of the order is silent, the second says it spoke and left this
    task out. Neither one places the task anywhere or holds it back.
    """
    if place is None:
        return None
    if not isinstance(place, dict):
        raise ContractError(f"{where}: ожидался объект")
    _require(place, PLAN_PLACE_FIELDS, where)
    if place["role"] not in PLAN_ROLES:
        raise ContractError(f"{where}: роль {place['role']!r}")
    if not str(place["src"] or "").strip():
        raise ContractError(f"{where}: место в плане названо, но не сказано, чем наблюдено")
    if place["role"] != "unnamed" and not str(place["line"] or "").strip():
        raise ContractError(f"{where}: роль {place['role']!r} без строки плана, "
                            "которой она наблюдена")
    if place["role"] == "queue" and not isinstance(place["position"], int):
        raise ContractError(f"{where}: место в очереди должно быть числом")
    if not isinstance(place["ahead"], list):
        raise ContractError(f"{where}: «перед ней» должно быть списком")
    # План, сказавший об одной работе двояко, обязан показать обе строки: место
    # выбрано более конкретным утверждением, и читатель имеет право это оспорить.
    if not isinstance(place["conflict"], list):
        raise ContractError(f"{where}: «сказано двояко» должно быть списком")
    for other in place["conflict"]:
        _require(other, ("role", "line", "field", "src"), f"{where}: вторая строка плана")
        if not str(other["line"] or "").strip():
            raise ContractError(f"{where}: вторая строка плана названа пустой")
    return place


def validate_task(task: dict, where: str) -> dict:
    """Validate one directory-backed task wherever the snapshot carries it."""
    _require(task, TASK_FIELDS, where)
    _require(task["run"], RUN_FIELDS, f"{where}: run")
    _require(task["board"], BOARD_FIELDS, f"{where}: board")
    _require(task["detail"], DETAIL_FIELDS, f"{where}: detail")
    if task["board"]["area"] not in BOARD_AREAS:
        raise ContractError(f"{where}: область {task['board']['area']!r}")
    validate_plan_place(task["board"]["plan_place"], f"{where}: место в плане")
    role = task["board"]["role"]
    if role is not None and role not in STATIONS:
        raise ContractError(f"{where}: роль {role!r}")
    for value, source, what in (
            (task["board"]["actor"], task["board"]["actor_src"], "исполнитель"),
            (role, task["board"]["role_src"], "роль"),
            (task["board"]["why"], task["board"]["why_src"], "причина"),
            (task["board"]["since"], task["board"]["since_src"], "давность"),
            (task["board"]["blocked_by"], task["board"]["blocked_by_src"], "чем задержана"),
            (task["detail"].get("moved"), task["detail"].get("moved_src"),
             "движение артефактов"),
            (task["run"]["alive"], task["run"]["alive_src"], "живость прогона")):
        if value and not str(source or "").strip():
            raise ContractError(f"{where}: {what} названа, но не сказано, чем наблюдена")
    unknown = [flag for flag in task["flags"] if flag not in TASK_FLAGS]
    if unknown:
        raise ContractError(f"{where}: неизвестные флаги {unknown}")
    for question in task["questions"]:
        validate_question(question, f"{where}: вопрос")
    for owner, field in (("user", "asked_user"), ("product", "our_questions")):
        mine = [q for q in task["questions"] if q.get("owner") == owner]
        if task[field] != mine:
            raise ContractError(f"{where}: {field} расходится с разбором questions по владельцу")
    hand = task["detail"].get("handoff")
    if hand is not None:
        _require(hand, HANDOFF_FIELDS, f"{where}: документ человеку")
        if not str(hand["delivered_src"]).strip():
            raise ContractError(f"{where}: доставка названа, но не сказано, чем наблюдена")
    return task


def validate_snapshot(snapshot: dict) -> dict:
    """Check a collector snapshot; return it unchanged or raise ContractError."""
    if not isinstance(snapshot, dict):
        raise ContractError("снимок: ожидался объект")
    _require(snapshot, SNAPSHOT_FIELDS, "снимок")
    if snapshot["schema_version"] != SCHEMA_VERSION:
        raise ContractError(f"снимок: версия схемы {snapshot['schema_version']!r}")
    if snapshot["mode"] not in MODES:
        raise ContractError(f"снимок: режим {snapshot['mode']!r}")
    if not isinstance(snapshot["threads"], list):
        raise ContractError("снимок: threads должен быть списком")
    if not isinstance(snapshot["task_index"], list):
        raise ContractError("снимок: task_index должен быть списком")
    for item in snapshot["task_index"]:
        _require(item, TASK_INDEX_FIELDS, "строка индекса задач")
        if not isinstance(item["id"], int) or not str(item["task"]).strip():
            raise ContractError("строка индекса задач: нужен номер и каталог")
        if not str(item["title"] or "").strip():
            raise ContractError(f"строка индекса задач {item['id']}: пустое название")
        entry = item.get("entry")
        if entry is not None:
            validate_task(entry, f"карточка индекса задач {item['id']}")
            if entry["id"] != item["id"] or entry["dir"] != item["task"]:
                raise ContractError(f"строка индекса задач {item['id']}: карточка другой задачи")
    validate_plan(snapshot["plan"])
    # Ревизия названа вместе с тем, чем она наблюдена: это надпись, существующая
    # ровно затем, чтобы её сверили с установленным, и без источника её сверить
    # нечем — правило то же, что у любой подписи на этой доске.
    _require(snapshot["revision"], ("running", "disk", "src"), "снимок: ревизия")
    if not str(snapshot["revision"]["src"] or "").strip():
        raise ContractError("снимок: ревизия названа, но не сказано, чем наблюдена")

    for thread in snapshot["threads"]:
        where_thread = f"направление {thread.get('key')!r}"
        _require(thread, THREAD_FIELDS, where_thread)
        validate_check(thread["check"], f"{where_thread}: проверка")
        # Absent means «этот снимок собран до появления целей», which a state
        # file written by an older tick genuinely is. An empty list means the
        # store was read and holds none — two different claims, kept apart.
        for goal in thread.get("goals") or []:
            validate_goal(goal, f"{where_thread}: цель {goal.get('id')!r}")
        # Кто ведёт направление. `None` — усиленных целей нет и вести нечего;
        # объект обязан сказать, чем наблюдена живость, на том же правиле, что и
        # всё остальное на этой доске.
        if thread.get("goal_session") is not None:
            validate_goal_session(thread["goal_session"],
                                  f"{where_thread}: непрерывная сессия")
        validate_next_check(thread["next_check"], f"{where_thread}: следующая проверка")
        validate_wake(thread["wake"], f"{where_thread}: пробуждение")
        for task in thread["tasks"]:
            where = f"задача {task.get('id')!r}"
            validate_task(task, where)
        for repo in thread["repos"]:
            _require(repo, REPO_FIELDS, f"репозиторий {repo.get('name')!r}")
        for channel in thread["channels"]:
            _require(channel, CHANNEL_FIELDS, f"канал направления {thread.get('key')!r}")
            if channel["channel"] not in CHANNELS:
                raise ContractError(f"канал {channel['channel']!r}")
            if channel["direction"] not in ("in", "out"):
                raise ContractError(f"канал: направление {channel['direction']!r}")

    for product in snapshot["products"]:
        _require(product, ("slug", "questions", "own_questions", "effect", "promises"), "продукт")
        for owner, field in (("user", "questions"), ("product", "own_questions")):
            for question in product[field]:
                validate_question(question, f"вопрос продукта {product['slug']!r}")
                if question["owner"] != owner:
                    raise ContractError(
                        f"вопрос продукта {product['slug']!r}: лежит в {field}, "
                        f"а владелец {question['owner']!r}")
        for promise in product["promises"]:
            where = f"строка «В работе» продукта {product['slug']!r}"
            if not isinstance(promise, dict):
                raise ContractError(f"{where}: ожидался объект со сверкой, а не строка")
            _require(promise, PROMISE_FIELDS, where)
            if promise["link"] not in PROMISE_LINKS:
                raise ContractError(f"{where}: связь {promise['link']!r}")
            # The same rule the plates live under, applied to the one area whose
            # content is prose: a line may be shown as unplanned only together
            # with what was compared against it. Without that the area is back to
            # asserting «задачи нет» from a test that never looked for one.
            if not str(promise["checked"]).strip():
                raise ContractError(f"{where}: не сказано, с чем сверена строка")
    if not isinstance(snapshot["owners_awake"], list):
        raise ContractError("снимок: owners_awake должен быть списком")
    for owner in snapshot["owners_awake"]:
        _require(owner, ("kind", "thread", "worktrees", "since", "age_seconds", "src"),
                 "разбуженный продакт")
        if owner["kind"] not in OWNER_KINDS:
            raise ContractError(f"разбуженный продакт: род {owner['kind']!r}")
        if not str(owner["src"]).strip():
            # Same rule as everywhere else: a second instance of the product
            # owner named on the strip has to say what observed it.
            raise ContractError("разбуженный продакт: не сказано, чем наблюдён")
        # Absent means «эта опись собрана до наблюдения активности». Present
        # means a claim is being made about whether that owner is deciding
        # anything, and a claim carries what observed it, like every other one
        # on this board.
        activity = owner.get("activity")
        if activity is not None:
            _require(activity, ("active", "src"), "активность разбуженного продакта")
            if not str(activity["src"]).strip():
                raise ContractError(
                    "разбуженный продакт: активность названа, но не сказано, чем наблюдена")
        if not isinstance(owner["worktrees"], list):
            # Yielding is about a working tree and nothing else, so an instance
            # that cannot say which trees it could occupy cannot be reasoned
            # about — and «уступить всем» is how three of four directions went
            # mute on 2026-08-07.
            raise ContractError("разбуженный продакт: рабочие деревья должны быть списком")
    return snapshot


def validate_record(record: dict) -> dict:
    """Check one timeline record; return it unchanged or raise ContractError."""
    if not isinstance(record, dict):
        raise ContractError("запись ленты: ожидался объект")
    _require(record, TIMELINE_FIELDS, "запись ленты")
    if record["schema_version"] != SCHEMA_VERSION:
        raise ContractError(f"запись ленты: версия схемы {record['schema_version']!r}")
    if record["kind"] not in TIMELINE_KINDS:
        raise ContractError(f"запись ленты: неизвестный род {record['kind']!r}")
    if not ISO8601.match(str(record["at"])):
        raise ContractError(f"запись ленты: время {record['at']!r} не ISO 8601")
    if not str(record["observed_by"]).strip():
        # A transition without a stated observation is exactly what the task
        # forbids: the caption has to say what the move was observed by.
        raise ContractError("запись ленты: не сказано, чем наблюдено")
    if record.get("actor") and not str(record.get("actor_src") or "").strip():
        # Same rule as `observed_by`, applied to the participant: a record may
        # leave the executor unnamed, but may not name one without saying what
        # named them.
        raise ContractError("запись ленты: исполнитель назван, но не сказано, чем наблюдён")
    station = record.get("station")
    if station is not None and station not in STATIONS:
        raise ContractError(f"запись ленты: станция {station!r}")
    channel = record.get("channel")
    if channel is not None and channel not in CHANNELS:
        raise ContractError(f"запись ленты: канал {channel!r}")
    return record


# ---------------------------------------------------------------------------
# Anonymisation: one cleaner, applied to whichever document is being shown.
# ---------------------------------------------------------------------------

SCRUB = [
    # Structural identifiers only. Content-level review before publication stays
    # a human step, and the map declares it rather than pretending otherwise.
    (re.compile(r"/opt/projects/[A-Za-z0-9_./-]*"), "<repo>"),
    (re.compile(r"/(?:home|root|Users)/[A-Za-z0-9_./-]*"), "<home>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "<email>"),
    # Not `\b\d{7,}\b`: a word boundary never fires between `_` and a digit, so
    # the real leak `telegram_user_100200300` walked straight through it. Digit
    # look-around catches an identifier however it is glued to its name.
    (re.compile(r"(?<!\d)\d{7,}(?!\d)"), "<id>"),
]

# Numeric identifiers hide from the regexes by not being text at all: `scrub`
# used to return every integer untouched, and 298 real PIDs went out in a file
# stamped «ОБЕЗЛИЧЕНО» (finding HIGH-1 of review 780). These keys carry an
# identifier rather than a measurement, so their value is dropped outright —
# a count of tasks or a line number stays, a PID does not.
DROP_NUMERIC_KEYS = {"pid", "inode", "chat_id", "message_id", "user_id"}


def scrub(value):
    """Structural anonymisation of a document, applied to every string in it.

    Task titles are *kept as meaning* and *cleaned as text*: the user asked to
    recognise a specific task by its real name, and that is compatible with
    running the name through the same expressions as everything else. Excluding
    titles from the cleaning — which both this function and the scribe used to
    do, in that order — let a real chat identifier through inside a real title
    (finding HIGH-1 of review 780, and its survivor HIGH-3 of review 786).
    Content privacy of titles stays a human step before showing, and is declared
    as a limit.

    There is no exemption list and no caller may reinstate one: a caller that
    puts a raw value back after cleaning has un-cleaned the document, which is
    exactly the bypass that shipped. Cleaning is idempotent, so cleaning an
    already-clean document on the way to the screen is safe and cheap.
    """
    if isinstance(value, str):
        for pattern, replacement in SCRUB:
            value = pattern.sub(replacement, value)
        return value
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: None if key in DROP_NUMERIC_KEYS and isinstance(item, int) else scrub(item)
                for key, item in value.items()}
    return value
