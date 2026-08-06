#!/usr/bin/env python3
"""The contract between the state collector, the recorder and the map.

The map renderer never reads a repository, a task directory or the disk: it gets
a snapshot document and a timeline of records, and nothing else. That promise is
only worth something if the shape of those two documents is written down and
checked, so this module is the single place where the shape lives — the collector
produces it, the recorder appends to it, the renderer consumes it, the tests
assert on it.

Nothing here reads files. It is a description plus two validators.
"""
from __future__ import annotations

import re

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Snapshot: one document describing every thread at one instant.
# ---------------------------------------------------------------------------

SNAPSHOT_FIELDS = ("schema_version", "mode", "threads", "products")
THREAD_FIELDS = ("key", "title", "products", "task_count", "tasks", "repos", "channels")
TASK_FIELDS = ("id", "title", "status", "dir", "run", "gates", "flags", "board")
RUN_FIELDS = ("state", "runner", "workflow", "alive", "progress")
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
    "waiting_human",  # a person has to decide; shown even when empty
    "running",        # a process is alive right now
    "stuck",          # a dead run under a living label, a kill, a failed gate
    "queued",         # nothing is happening and nothing is wrong
    "done",           # terminal
)

BOARD_AREA_RU = {
    "waiting_human": "Ждёт решения человека",
    "running": "В работе сейчас",
    "stuck": "Затор",
    "queued": "В очереди",
    "done": "Сделано",
}

# Fields of one plate. `actor` and `role` are nullable on purpose: an empty cell
# is the honest answer when nothing on disk names the executor, and it is better
# than a confident invention (finding MEDIUM-3 of review 780).
BOARD_FIELDS = ("area", "actor", "actor_src", "role", "role_src",
                "happening", "since", "age_seconds", "attempt")

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
        _require(thread, THREAD_FIELDS, f"направление {thread.get('key')!r}")
        for task in thread["tasks"]:
            where = f"задача {task.get('id')!r}"
            _require(task, TASK_FIELDS, where)
            _require(task["run"], RUN_FIELDS, f"{where}: run")
            _require(task["board"], BOARD_FIELDS, f"{where}: board")
            if task["board"]["area"] not in BOARD_AREAS:
                raise ContractError(f"{where}: область {task['board']['area']!r}")
            role = task["board"]["role"]
            if role is not None and role not in STATIONS:
                raise ContractError(f"{where}: роль {role!r}")
            # A named executor without a named observation is exactly the caption
            # the board must not carry: it would read as fact and be a guess.
            for value, source, what in ((task["board"]["actor"], task["board"]["actor_src"], "исполнитель"),
                                        (role, task["board"]["role_src"], "роль")):
                if value and not str(source or "").strip():
                    raise ContractError(f"{where}: {what} названа, но не сказано, чем наблюдена")
            unknown = [flag for flag in task["flags"] if flag not in TASK_FLAGS]
            if unknown:
                raise ContractError(f"{where}: неизвестные флаги {unknown}")
        for repo in thread["repos"]:
            _require(repo, REPO_FIELDS, f"репозиторий {repo.get('name')!r}")
        for channel in thread["channels"]:
            _require(channel, CHANNEL_FIELDS, f"канал направления {thread.get('key')!r}")
            if channel["channel"] not in CHANNELS:
                raise ContractError(f"канал {channel['channel']!r}")
            if channel["direction"] not in ("in", "out"):
                raise ContractError(f"канал: направление {channel['direction']!r}")

    for product in snapshot["products"]:
        _require(product, ("slug", "questions", "effect"), "продукт")
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
