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

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Snapshot: one document describing every thread at one instant.
# ---------------------------------------------------------------------------

SNAPSHOT_FIELDS = ("schema_version", "mode", "threads", "products", "owners_awake")
THREAD_FIELDS = ("key", "title", "products", "task_count", "tasks", "repos", "channels",
                 # The direction's own last wake-up: when it looked and what came
                 # of it. `None` until a tick has written one.
                 "check",
                 # When it looks next, observed as the snapshot is built rather
                 # than carried out of that record — a separate field because it
                 # is a fact about the present, not about the last check.
                 "next_check")

# What a next-check observation carries. `at` may be `None` — a timer systemd is
# holding unarmed is an honest gap — but then `src` has to say what was seen
# instead, because a board that shows «следующая проверка: неизвестно» owes the
# reader the reason just as much as one that shows a time.
NEXT_CHECK_FIELDS = ("at", "src")

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
               "waiting_on", "src")
GOAL_STATES = ("active", "paused", "closed")
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
    "plan": "Надо запланировать",
    "done": "Сделано и доставлено",
}

# `pickup` stands above `queued` on purpose, and both were one area called «в
# очереди» before. That single area answered neither of the two questions the
# user actually opens the board with — «что можно подхватить прямо сейчас»,
# which is the first question of every wake-up, and «за чем стоит остальное».
# The split is one observation: whether anything on disk is holding the task.
#
# `plan` holds no tasks at all. It carries lines written in a product record for
# which no task could be observed — the place where «посмотреть код companion»
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
        _require(corrective, ("task", "effect", "return_criterion", "accepted"),
                 f"{where}: корректирующая задача")
        # Both halves or neither: «заведём ремонт» without the observable
        # criterion for coming back is how a chain stops at the closed repair.
        if not str(corrective["effect"]).strip() or not str(corrective["return_criterion"]).strip():
            raise ContractError(
                f"{where}: корректирующая задача без пользовательского эффекта "
                "или без критерия возврата к основной задаче")
    return goal


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

    for thread in snapshot["threads"]:
        where_thread = f"направление {thread.get('key')!r}"
        _require(thread, THREAD_FIELDS, where_thread)
        validate_check(thread["check"], f"{where_thread}: проверка")
        # Absent means «этот снимок собран до появления целей», which a state
        # file written by an older tick genuinely is. An empty list means the
        # store was read and holds none — two different claims, kept apart.
        for goal in thread.get("goals") or []:
            validate_goal(goal, f"{where_thread}: цель {goal.get('id')!r}")
        validate_next_check(thread["next_check"], f"{where_thread}: следующая проверка")
        for task in thread["tasks"]:
            where = f"задача {task.get('id')!r}"
            _require(task, TASK_FIELDS, where)
            _require(task["run"], RUN_FIELDS, f"{where}: run")
            _require(task["board"], BOARD_FIELDS, f"{where}: board")
            _require(task["detail"], DETAIL_FIELDS, f"{where}: detail")
            if task["board"]["area"] not in BOARD_AREAS:
                raise ContractError(f"{where}: область {task['board']['area']!r}")
            role = task["board"]["role"]
            if role is not None and role not in STATIONS:
                raise ContractError(f"{where}: роль {role!r}")
            # A named executor, role or reason without a named observation is
            # exactly the caption the board must not carry: it would read as fact
            # and be a guess. The reason joins the rule rather than getting an
            # exemption — «почему затор» is the caption most tempting to invent.
            for value, source, what in ((task["board"]["actor"], task["board"]["actor_src"], "исполнитель"),
                                        (role, task["board"]["role_src"], "роль"),
                                        (task["board"]["why"], task["board"]["why_src"], "причина"),
                                        (task["board"]["since"], task["board"]["since_src"], "давность"),
                                        # «За чем стоит» is the caption a queue
                                        # invents most readily, so it joins the
                                        # rule rather than getting an exemption.
                                        (task["board"]["blocked_by"], task["board"]["blocked_by_src"], "чем задержана"),
                                        (task["detail"].get("moved"), task["detail"].get("moved_src"), "движение артефактов"),
                                        (task["run"]["alive"], task["run"]["alive_src"], "живость прогона")):
                if value and not str(source or "").strip():
                    raise ContractError(f"{where}: {what} названа, но не сказано, чем наблюдена")
            unknown = [flag for flag in task["flags"] if flag not in TASK_FLAGS]
            if unknown:
                raise ContractError(f"{where}: неизвестные флаги {unknown}")
            for question in task["questions"]:
                validate_question(question, f"{where}: вопрос")
            # The split is data, so it has to agree with the questions it was
            # split from. Two lists that can drift are two answers to «чей это
            # вопрос», and the area exists precisely because that answer was
            # wrong for thirteen of sixteen entries.
            for owner, field in (("user", "asked_user"), ("product", "our_questions")):
                mine = [q for q in task["questions"] if q.get("owner") == owner]
                if task[field] != mine:
                    raise ContractError(
                        f"{where}: {field} расходится с разбором questions по владельцу")
            hand = task["detail"].get("handoff")
            if hand is not None:
                _require(hand, HANDOFF_FIELDS, f"{where}: документ человеку")
                if not str(hand["delivered_src"]).strip():
                    raise ContractError(
                        f"{where}: доставка названа, но не сказано, чем наблюдена")
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
    # the real leak `telegram_user_433978200` walked straight through it. Digit
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
