#!/usr/bin/env python3
"""Snapshot of the whole process contour for the top-down map.

One JSON document describing every thread, its tasks, its repositories, the
questions waiting for a person and the results already delivered. It is the only
input the map renderer gets: whatever is not here cannot appear on the map, and
whatever is private cannot leak through a renderer that never saw it.

Observed state only, on purpose. Everything comes from task frontmatter,
`status.json`, `.runner/runner.json`, `progress.json` mtime, gate lines of
`verification.md` and git. Child transcripts are never read, so a snapshot costs
the same regardless of how much work happened — the same rule `thread_state.py`
follows.

Two questions this module deliberately does not answer itself:

* whether a recorded process is still this task's run — that belongs to the
  runner that recorded it, and is asked of `task_runner.process_is_live`;
* how a document is anonymised — that belongs to `process_map_schema`, which
  reads no files and can therefore be shared with the renderer.

Both used to live here in a second copy, and both copies drifted from the
original: a PID number counted as a live run, and a caller could put raw values
back after cleaning.

Exit code 0 always; the caller decides what to do with the report.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import product_goal
import product_memory
from process_map_schema import (SCHEMA_VERSION, STATIONS, ContractError, scrub,
                                validate_check, validate_snapshot)

HOME = Path(__file__).resolve().parents[1]
PROC = Path("/proc")
CONFIG = product_memory.CONFIG
# Live product content moved out of git so that product work stops changing the
# evidence base of a code review. The board reads the durable store through its
# owning module, never the archived monoliths under `products/`.
PRODUCTS = product_memory.products_dir()
# The task system this product owner observes, as this installation declares it.
# A path of one particular installation written here would be the one thing that
# keeps this core from being anybody else's.
REPO = product_memory.tasks_repo()
TASKS_INDEX = REPO / "skills" / "task-creator" / "scripts" / "tasks_index.py"
RUNNER_SCRIPTS = REPO / "skills" / "task-runner" / "scripts"
# The task system answers about itself, with its own dependencies. Its own
# interpreter is the right one when it has one; a task system installed against
# the system interpreter is asked with the interpreter we are already running.
PYTHON = REPO / ".venv" / "bin" / "python"
if not PYTHON.is_file():
    PYTHON = Path(sys.executable)
MAIL_ROOT = REPO / ".state" / "gmail" / "product-owner"
MAIL_INBOX = MAIL_ROOT / "inbox"
MAIL_SENT = MAIL_ROOT / "sent"
TELEGRAM_SENT = REPO / ".state" / "telegram" / "sent-documents.jsonl"
# Where the previous sighting of each awake product owner is kept, so «этот
# продакт решает» can be told from «это открытое окно» by a difference rather
# than by a guess. Runtime state, rebuilt by the next wake-up.
OWNER_STATE = HOME / "state" / "process-inventory" / "owners.json"

# Statuses that say the work is over. A record someone wrote, and a live process
# outranks it: see `board_area`.
TERMINAL = {"completed", "cancelled", "superseded"}


def tunable(name: str, default: int) -> int:
    """A whole number from the environment, with the in-code default beside it.

    No bare literal for a duration or a threshold: the value is overridable on
    the stand, and the default is readable where it is used. `thread_tick.py`
    imports this one rather than keeping a second copy.
    """
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


# How much processor time a product owner has to burn between two sightings to
# count as one that is actually deciding something. A session talking to a model
# burns far more than a second; a window left open burns none.
OWNER_CPU_TICK_SECONDS = tunable("PRODUCT_OWNER_OWNER_CPU_TICK_SECONDS", 1)
# And how long a sighting has to span before «не двигалось» is a statement about
# the world rather than about the length of the measurement. Shorter than the
# wake-up interval, so one tick apart is already enough to judge.
OWNER_IDLE_SECONDS = tunable("PRODUCT_OWNER_OWNER_IDLE_SECONDS", 900)

# dev-pipeline event kinds, mapped to the station they say the work is at. The
# contour declares no role state machine, so this is a reading of observed
# events, not a lifecycle the map invents.
EVENT_STATIONS = {
    "attempt_started": "analysis",
    "run_started": "analysis",
    "process_started": "analysis",
    "native_session_discovered": "analysis",
    "increment_ready_for_review": "review",
    "review_started": "review",
    "review_completed": "review",
    "checkpoint_completed": "report",
    "run_completed": "report",
    "attempt_completed": "report",
    "run_failed": "report",
    "attempt_failed": "report",
}


def tail_lines(path: Path, cursor: dict | None) -> tuple[list[str], dict]:
    """Lines appended past a stored byte offset, plus the cursor to store next.

    Reading a whole `events.jsonl` on every look makes the cost of watching grow
    with the amount of work already done, which is the one thing the contour
    forbids. The cursor carries the byte offset with the size and inode it was
    taken at: a file that shrank was truncated and one whose inode changed was
    rotated, and both start from zero rather than silently skipping the head.
    """
    try:
        stat = path.stat()
    except OSError:
        return [], (cursor or {})
    offset = 0
    if cursor:
        same_file = cursor.get("inode") == stat.st_ino
        not_truncated = stat.st_size >= cursor.get("size", 0)
        if same_file and not_truncated:
            offset = min(cursor.get("offset", 0), stat.st_size)
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read()
            end = handle.tell()
    except OSError:
        return [], (cursor or {})
    text = chunk.decode("utf-8", "replace")
    if text and not text.endswith("\n"):
        # A record still being written is not news yet; leave it for next look.
        keep, _, partial = text.rpartition("\n")
        end -= len(partial.encode())
        text = keep
    lines = [line for line in text.splitlines() if line.strip()]
    return lines, {"offset": end, "size": stat.st_size, "inode": stat.st_ino}


def last_json_line(path: Path) -> dict:
    """The last well-formed JSON object of a journal, read from its tail."""
    try:
        size = path.stat().st_size
    except OSError:
        return {}
    window = min(size, 65536)
    try:
        with path.open("rb") as handle:
            handle.seek(size - window)
            chunk = handle.read()
    except OSError:
        return {}
    for line in reversed(chunk.decode("utf-8", "replace").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def load_config() -> dict:
    return json.loads(CONFIG.read_text())


def liveness_owner():
    """The runner module that owns «is this recorded process still this run».

    The question already has an implementation and it is not here:
    `task_runner.process_is_live` compares the recorded PID *and* the kernel
    start tick, and `runner_pid_namespace_state` decides when a negative
    lookup is evidence at all. Both values are already in the
    `.runner/runner.json` this collector reads. Writing a second answer here
    would be a second implementation of one concept, so the owner is imported
    and asked instead (finding MEDIUM-2 of review 786). It is stdlib-only and
    costs about a tenth of a second to import, once.
    """
    if str(RUNNER_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(RUNNER_SCRIPTS))
    try:
        import task_runner
    except ImportError:
        return None
    return task_runner


RUNNER = liveness_owner()


def live_run_owner():
    """This installation's own module listing every registered live run process.

    Which module that is, is a setting and not a constant of this core: a task
    system may ship such an adapter under any name, and an installation may have
    none at all. Unnamed and unimportable are the same answer here — no inventory
    — and the collector says so out loud instead of guessing (see
    `ProcessInventoryUnavailable`).
    """
    module_name = product_memory.run_registry_module()
    if not module_name:
        return None
    if str(RUNNER_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(RUNNER_SCRIPTS))
    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None


RUN_REGISTRY = live_run_owner()


class ProcessInventoryUnavailable(RuntimeError):
    """The observer cannot safely separate registered runs from detached work."""


def run_alive(runner: dict) -> tuple[bool, str | None]:
    """Whether the process this task recorded is still running, and what said so.

    Closed by default. `os.kill(pid, 0)` — what this used to be — answers «some
    process holds this number», and PIDs are reused, so after a wrap an
    unrelated process was shown as a live run of the task, in green, in the «в
    работе сейчас» area. Three answers are now distinguished:

    * live, because the recorded identity still matches the running process;
    * not live, because nothing matches or nothing was recorded to match against;
    * unobservable, because the PID belongs to a namespace this observer cannot
      see — which is not the same as dead, and says so on the plate.

    Which of the last two a foreign namespace is, is not decided here either.
    `runner_pid_namespace_state` (task 938) separates a namespace that is merely
    invisible from one that provably no longer exists: a run recorded in a
    vanished namespace is dead, and only a namespace that still holds processes,
    or one whose absence cannot be proved from here, keeps the run untouched
    behind «ненаблюдаема». Reproducing that judgement locally would put the
    second implementation back.
    """
    if RUNNER is None:
        return False, None
    namespace_state = RUNNER.runner_pid_namespace_state(runner)
    if namespace_state == "recorded_namespace_absent":
        # The namespace itself is gone, so nothing can still be running in it.
        return False, None
    if namespace_state != "local":
        return False, "личность процесса ненаблюдаема: другое пространство имён PID"
    identity = runner.get("process_identity")
    if not isinstance(identity, str) or not identity:
        return False, None
    if RUNNER.process_is_live(runner.get("pid"), identity):
        return True, "pid и стартовый тик ядра совпали с .runner/runner.json"
    return False, None


def read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def query_tasks(args: list[str], limit: int | None = 60) -> list[dict]:
    # An unobservable task system is not an empty one, and the board may not
    # show «задач нет» when the truth is «спросить некого»: the same rule the
    # durable content root already follows.
    if not TASKS_INDEX.is_file():
        raise ContractError(
            f"система задач {REPO} не наблюдается: нет {TASKS_INDEX}. "
            "Назовите её в threads.json ключом tasks_repo или в переменной "
            "PRODUCT_OWNER_TASKS_REPO")
    command = [str(PYTHON), str(TASKS_INDEX), "query", *args, "--format", "json"]
    if limit is not None:
        command.extend(("--limit", str(limit)))
    out = subprocess.run(
        command,
        capture_output=True, text=True, cwd=REPO,
    )
    if out.returncode != 0:
        return []
    try:
        payload = json.loads(out.stdout)
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else payload.get("tasks", [])


def task_catalogue() -> list[dict]:
    """Every task the index knows: id, title and slug, and nothing read from disk.

    The pool a product line is matched against. It is one query against the same
    index the board already reads, so the answer to «есть ли за этой строкой
    задача» is reproducible by hand with `tasks_index.py query`.
    """
    return query_tasks(["--status", "all"], limit=None)


def task_index(catalogue: list[dict], entries: dict[str, dict] | None = None) -> list[dict]:
    """Index metadata, optionally carrying cards built from task directories.

    The list a person searches, so the fixtures our own runs leave behind are not
    in it: three of the 1059 rows were `TEST [1140] …`, and a lookup is worth
    exactly as much as the reader's trust that every row is real work.
    """
    entries = entries or {}
    rows = []
    for task in catalogue:
        name = Path(task["path"]).name
        if re.match(r"^\s*TEST\b", str(task.get("title") or "")):
            continue
        row = {"id": task["id"], "task": name,
               "title": task.get("title") or name, "status": task.get("status")}
        if name in entries:
            row["entry"] = entries[name]
            row["updated_at"] = entries[name]["board"]["since"]
            row["updated_src"] = entries[name]["board"]["since_src"]
        else:
            row["updated_at"], _age, row["updated_src"] = state_age(REPO / task["path"])
        rows.append(row)
    return rows


def owned_elsewhere(thread: dict) -> set[str]:
    """Проекты, которые в `threads.json` объявлены за другими направлениями.

    Имя записи здесь то же, чем её называет конфигурация и чем её разрешает
    `tasks_index.py --project`: каталог записи, а не путь до файла внутри него.
    """
    mine = set(thread.get("projects", []))
    return {project for other in load_config()["threads"].values()
            for project in other.get("projects", [])} - mine


def thread_tasks(thread: dict, limit: int = 60) -> list[dict]:
    """Задачи направления: по его проектам, а поиском — где связи с проектом нет.

    `task_search` — страховка для задачи, которую никто не связал с проектом
    (урок 2026-08-06), и она не должна перевешивать связь, которая есть.
    Наблюдение ревью круга 6: `--search bot` совпадает со слагом #1172
    `…-robot-takes-at-once` — это часть другого слова, а не имя продукта, —
    и колонка «Клиент» называла своей текущей работой живой прогон
    «Платформы». Спорит здесь не подстрока с подстрокой, а догадка с
    объявленной связью, поэтому решает объявленный проект: он однозначен, и
    владельца ему назначает тот же `threads.json`.
    """
    seen: dict[str, dict] = {}
    for project in thread.get("projects", []):
        for task in query_tasks(["--project", project, "--status", "all"], limit=limit):
            seen[task["path"]] = task
    others = owned_elsewhere(thread)
    for term in thread.get("task_search", []):
        for task in query_tasks(["--search", term, "--status", "all"], limit=limit):
            if task["path"] in seen or any(Path(link).parent.name in others
                                           for link in task.get("projects") or []):
                continue
            seen[task["path"]] = task
    return sorted(seen.values(), key=lambda t: t.get("id", 0), reverse=True)


def gates(task_dir: Path) -> list[dict]:
    """Gate names and verdicts from verification.md, without reading the prose."""
    path = task_dir / "verification.md"
    if not path.is_file():
        return []
    try:
        text = path.read_text()
    except OSError:
        return []
    found: list[dict] = []
    name = None
    for line in text.splitlines():
        if line.startswith("## "):
            name = line[3:].strip()
            continue
        match = re.search(r"Result:\s*\**\s*(OK|GAP|BLOCKED|FAIL)", line, re.IGNORECASE)
        if match and name:
            found.append({"gate": name, "result": match.group(1).upper()})
            name = None
    return found


def run_state(task_dir: Path) -> dict:
    status = read_json(task_dir / "status.json")
    runner = read_json(task_dir / ".runner" / "runner.json")
    pid = runner.get("pid") or status.get("pid")
    alive, alive_src = run_alive(runner)

    progress_path = task_dir / "progress.json"
    progress = None
    if progress_path.is_file():
        payload = read_json(progress_path)
        # Freshness is the file's mtime, never the child's own timestamp:
        # children routinely write local time into a UTC field.
        progress = {
            "activity": payload.get("activity"),
            "done": payload.get("completed"),
            "total": payload.get("total"),
            "minutes_ago": round((time.time() - progress_path.stat().st_mtime) / 60),
        }
    refusal = status.get("completion_refusal") or {}
    return {
        "state": status.get("state"),
        # What the run itself says it is doing. `thread_state` printed this line
        # from its own copy of this reader; there is one copy now.
        "current_step": status.get("current_step"),
        "runner": status.get("runner") or runner.get("runner"),
        "workflow": status.get("workflow"),
        "sandbox": runner.get("sandbox_mode") or (runner.get("access_grant") or {}).get("sandbox_mode"),
        "stop_reason": runner.get("watcher_stop_reason") or None,
        "exit_code": runner.get("exit_code"),
        "pid": pid if isinstance(pid, int) else None,
        "alive": alive,
        "alive_src": alive_src,
        "progress": progress,
        # The contour already knows when an owner stopped before its closing
        # step and says so in writing; nothing used to read it. See `refusal`.
        "refusal": refusal.get("kind") if isinstance(refusal, dict) else None,
        "refusal_summary": (refusal.get("summary") or refusal.get("reason")) if isinstance(refusal, dict) else None,
        "repo": subject_repo(runner),
    }


def subject_repo(runner: dict) -> str | None:
    """The repository this task's run was pointed at, as its own record has it.

    `--repo` of the recorded command is what the runner was actually told, and
    `access_grant.granted_directories` is what it was actually allowed to write.
    Both are observations of the same launch, so the command comes first and the
    grant is the fallback; nothing is derived from the title or the project.
    """
    command = runner.get("command") or []
    if "--repo" in command:
        index = command.index("--repo") + 1
        if index < len(command):
            return command[index]
    granted = (runner.get("access_grant") or {}).get("granted_directories") or []
    return granted[0] if granted else None


# Files whose movement says nothing about the work: the runner's own log grows
# with every line a child prints, and a transcript is the one thing the contour
# forbids reading. Excluding both is what keeps a look at a task directory the
# same price whatever happened inside it.
NOT_ARTIFACTS = ("runner.log",)
TRANSCRIPT = re.compile(r"transcript", re.IGNORECASE)

# Files that are the run's own bookkeeping rather than its work, and the task's
# own metadata. These are precisely the files the observer already watches — see
# `AREA_INPUTS` and `run_state` — so counting their movement as «что-то
# происходит в каталоге» would make the new observation a restatement of the old
# one. It also makes it wrong in the common direction: accepting a finished task
# rewrites `task.md`, and eight of the first eleven hits of this rule were that
# acceptance, not work carrying on outside a dead owner.
BOOKKEEPING = {"status.json", "progress.json", "task.md", "task_contract.json"}

# Subtrees that are machinery rather than artifacts. A review that cloned its
# subject into a scratch directory left 16 250 entries under one task, almost
# all of them git objects and pytest caches, and walking them would make a look
# at that task fifty times dearer than a look at any other — the one property
# the contour requires a look not to have. Pruned by name, so the bound holds
# whatever a future child drops there.
NOT_ARTIFACT_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv",
                     "node_modules", ".mypy_cache", ".ruff_cache"}


def artifact_movement(task_dir: Path) -> tuple[str | None, int | None, str | None]:
    """When anything in the task directory last moved, and what moved.

    The hole this closes was observed twice, on 712 and on 757: the owner died,
    the work carried on in another process, and `status.json`/`progress.json` —
    the only two files anyone watched — never moved again because there was
    nobody left to move them. Task 757 sat finished for three and a half hours.

    So the observation is the newest mtime of the *work* in the directory: the
    run's own bookkeeping is excluded, because those are exactly the files the
    old observer already watched and restating them would observe nothing new.
    Names and stat calls only: no file is opened, so the cost is the number of
    entries, not the volume of work, and no transcript is read here or anywhere
    else.
    """
    newest = (0.0, None)
    for root, dirs, files in os.walk(task_dir):
        dirs[:] = [d for d in dirs
                   if d not in NOT_ARTIFACT_DIRS and d != ".runner" and not TRANSCRIPT.search(d)]
        for name in files:
            if name in NOT_ARTIFACTS or name in BOOKKEEPING or TRANSCRIPT.search(name):
                continue
            path = Path(root) / name
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > newest[0]:
                newest = (mtime, str(path.relative_to(task_dir)))
    if not newest[1]:
        return None, None, None
    mtime, name = newest
    return (datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
            max(int(time.time() - mtime), 0),
            f"самый свежий mtime в каталоге задачи: {name}")


def observed_actor(task_dir: Path, run: dict) -> tuple[str | None, str | None]:
    """Who is doing the work, and what said so. Nothing said — nobody named.

    Existence of a file says a file exists, not who wrote it (finding MEDIUM-3
    of review 780). Only these three sources name an executor, so a plate whose
    task has none of them carries a dash instead of a plausible guess.
    """
    if run.get("runner"):
        source = "поле runner в status.json" if read_json(task_dir / "status.json").get("runner") \
            else "поле runner в .runner/runner.json"
        return run["runner"], source
    event = last_json_line(task_dir / "dev-pipeline" / "core" / "events.jsonl")
    runtime = (event.get("payload") or {}).get("runtime")
    if runtime:
        return runtime, "поле runtime события dev-pipeline"
    return None, None


def observed_role(task_dir: Path) -> tuple[str | None, str | None]:
    """The station the last observed dev-pipeline event puts the work at."""
    event = last_json_line(task_dir / "dev-pipeline" / "core" / "events.jsonl")
    station = EVENT_STATIONS.get(event.get("kind"))
    if station in STATIONS:
        return station, f"последнее событие dev-pipeline: {event.get('kind')}"
    return None, None


# Every file the board area of a task is derived from. `task.md` carries the
# status and the questions, `status.json` the run label, `verification.md` the
# gate verdicts, `runner.json` the stop reason. The area depends on all four, so
# no single one of them can stand for «when the area last changed».
AREA_INPUTS = ("task.md", "status.json", "verification.md", ".runner/runner.json")


def state_age(task_dir: Path) -> tuple[str | None, int | None, str | None]:
    """How long nothing the area is built from has changed, and which file said so.

    A jam is not a status, it is time spent in one — so the plate needs an age.
    The previous version picked one carrier, `status.json` if it held any state
    and `task.md` otherwise, and called the result «в этом состоянии». That is
    two claims too many: the area also depends on the questions, the gate
    verdicts and the stop reason, so a task whose `task.md` had just been
    rewritten kept showing the older mtime of its `status.json` (finding
    MEDIUM-1 of review 786).

    Reading it from the *newest* of the area's inputs is an honest upper bound —
    the area cannot have changed later than its latest input did — and needs no
    memory of the previous state, which a collector that deliberately keeps none
    could not provide. What is measured is named in `since_src`, and the caption
    says «без изменений», not «в этом состоянии»: it is the age of the newest
    observation, not of a transition nobody watched.

    Never a timestamp a child wrote itself: children routinely put local time
    into a UTC field, and the contour has been burned by that already.
    """
    seen = []
    for name in AREA_INPUTS:
        try:
            seen.append((task_dir.joinpath(*name.split("/")).stat().st_mtime, name))
        except OSError:
            continue
    if not seen:
        return None, None, None
    mtime, name = max(seen)
    stamp = datetime.fromtimestamp(mtime, timezone.utc).isoformat()
    return stamp, max(int(time.time() - mtime), 0), f"mtime {name} — самый свежий вход области"


def attempt_count(task_dir: Path) -> int:
    """How many dev-pipeline runs left state on disk. One task, several tries."""
    root = task_dir / "dev-pipeline"
    if not root.is_dir():
        return 0
    return sum(1 for child in root.iterdir() if child.is_dir())


VERDICT = re.compile(r"^\s*(?:[-*]\s*)?(?:\**\s*)?Verdict\s*:\s*\**\s*([A-Za-z_-]+)",
                     re.IGNORECASE | re.MULTILINE)
SEVERITY = re.compile(r"^#{1,4}\s*(HIGH|MEDIUM|LOW)-\d+", re.MULTILINE)


def review_verdict(task_dir: Path) -> dict | None:
    """The verdict of the review that was written into this task, and its count.

    `findings.md` is where a cross-review lands, and it opens with `Verdict:`
    and holds one `## HIGH-1`-shaped heading per finding. Both are read from the
    file the reviewer wrote; nothing is inferred from a task title or a status.
    """
    path = task_dir / "findings.md"
    try:
        text = path.read_text()
    except OSError:
        return None
    match = VERDICT.search(text)
    severities = SEVERITY.findall(text)
    if not match and not severities:
        return None
    counts: dict[str, int] = {}
    for level in severities:
        counts[level] = counts.get(level, 0) + 1
    return {
        "verdict": match.group(1).lower() if match else None,
        "findings": len(severities),
        "by_severity": counts,
        "src": "строка Verdict и заголовки находок в findings.md",
    }


def delivery(task_dir: Path) -> dict | None:
    """What actually left for a person, from the receipts the sender wrote.

    A report left on disk counts as undelivered, so the card has to say whether
    anything went out and when the last thing did.

    And whether anything the run tried to say never arrived. That was the one
    thing this journal knew and nobody read: for nine days from 2026-08-13 the
    transport could not import its own module, 41 tasks recorded the refusal,
    and it stayed prose inside each task's `trace.md` while the board went on
    counting messages as if they had gone out (task 1255). A refusal counts as
    outstanding while nothing succeeded after it, so the observation closes
    itself as soon as the next message does get through.

    «Counting messages as if they had gone out» was also literally true of this
    function: it counted journal lines. A receipt is written when the sender
    claims a message, when it goes and when it does not, so lines and messages
    are different numbers — six and five on task 1255's own journal, and 1739
    against 1565 across the repository. The card says «Сообщений», so the number
    is messages: a receipt carrying an identifier the person's client can show.
    Refusals keep their own number next to it instead of hiding inside it, and
    `last_*` describes the last message rather than the last line, because
    «Последнее» under a refusal read as «сообщение ушло тогда-то».
    """
    path = task_dir / "dev-pipeline" / "notification-receipts.jsonl"
    rows = _json_lines(path)
    if not rows:
        return None
    sent = [row for row in rows if row.get("message_id")]
    last = sent[-1] if sent else {}
    observation = {"sent": len(sent), "last_at": last.get("recorded_at"),
                   "last_kind": last.get("kind"),
                   "src": "dev-pipeline/notification-receipts.jsonl: квитанции с "
                          "идентификатором сообщения"}
    refused = sum(1 for row in rows if row.get("kind") in UNDELIVERED_RECEIPTS)
    if refused:
        observation["refused"] = refused
    unresolved = None
    for row in rows:
        if row.get("kind") == DELIVERY_UNRESOLVED_KIND:
            unresolved = row
        elif row.get("message_id"):
            unresolved = None
    if unresolved:
        observation["unresolved"] = {
            "notification": unresolved.get("notification_kind"),
            "at": unresolved.get("recorded_at"),
            "reason": unresolved.get("reason"),
            "current": _recent(unresolved.get("recorded_at"),
                               DELIVERY_UNRESOLVED_SECONDS),
            "src": "dev-pipeline/notification-receipts.jsonl: последняя квитанция "
                   f"{DELIVERY_UNRESOLVED_KIND}, после которой ничего не ушло",
        }
    return observation


def _recent(at: str | None, seconds: int) -> bool:
    """Whether a recorded moment is inside a window ending now."""
    if not at:
        return False
    try:
        moment = datetime.fromisoformat(at)
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds() <= seconds


# ---------------------------------------------------------------------------
# «Сделано, но человеку не показано»
# ---------------------------------------------------------------------------
#
# Task 783 finished at 16:14 with a 441 KB report for the user in its
# `deliverables/`, and the report sat on the server for about an hour. Nothing
# was broken: the run had sent its receipts, and both of them were about the
# life of the run — «работа началась» and «работа кончилась». The result itself
# was never sent, and nobody noticed until the user said «по выполненным задачам
# я не видел документов в почте или в телеграме».
#
# That is exactly the gap this area watches, and it is observable without
# reading a single document: a finished task, a file made for a person, and no
# receipt that the person got it.

# What the run's own notifications say, so «квитанция есть» never means
# «документ доставлен». A kind outside this set is about something other than
# the life of the run and is taken as delivery evidence, because refusing to
# believe an unknown receipt would be inventing an alarm.
#
# That bias is only safe while the set actually holds every lifecycle kind the
# sender writes, and it stopped doing so. This list was measured once and never
# again; counted over the journals on 2026-08-23 it was missing seven kinds, two
# of them the largest in the whole repository — 522 `standard_run_started` and
# 168 `notification_delivery_unresolved`. A missing kind does not merely go
# unnamed: `handoff` read a start-of-run push carrying a message identifier as
# proof that a person has this task's document, so 783's hole reopened silently
# under the very receipts that say nothing about a document (task 1255).
#
# The counterpart set is closed and owned elsewhere: `pipeline_delivery.py`
# writes `document_delivery_started`, `document_delivered`,
# `document_delivery_refused` and `document_delivery_unresolved`, and those four
# are the only receipts that are about a document at all.
LIFECYCLE_RECEIPTS = frozenset({
    "attempt_started", "attempt_completed", "attempt_completed_rejected",
    "attempt_failed", "run_started", "run_completed", "run_failed",
    "process_started", "native_session_discovered", "checkpoint_completed",
    "increment_completed", "increment_ready_for_review", "review_started",
    "review_completed", "blocked_on_user_decision",
    # An ordinary run's own two edges, and the launcher's refusal to start or
    # to close one. Added by companion task 1142 and never seen here until now.
    "standard_run_started", "standard_run_completed", "pipeline_stopped",
    # The review phases of a task number, from the same sender.
    "review_rework_required", "review_waiting", "review_refused",
    # A notification the transport could not deliver. It is a fact about the
    # run's own voice, not about a document — see `DELIVERY_UNRESOLVED_KIND`.
    "notification_delivery_unresolved",
})

# The receipt the sender writes when a notification about the run itself never
# reached the person. It carries `reason`, which since task 1255 names the cause
# rather than only the fact.
DELIVERY_UNRESOLVED_KIND = "notification_delivery_unresolved"

# Every receipt that records something the person did not get: the refused
# notification above and the two the document sender writes. They are counted,
# never added to the messages, and a kind missing here only goes uncounted —
# nothing is called delivered because of it, because delivery is decided by the
# message identifier and not by this set.
UNDELIVERED_RECEIPTS = frozenset({
    DELIVERY_UNRESOLVED_KIND,
    "document_delivery_refused", "document_delivery_unresolved",
})

# How long such a refusal stays something to look at. The product owner watches
# current state: a refusal from nine days ago on a closed task is history, and
# putting all 44 of them on the attention list would bury the live work under
# it. Wider than a night, so a refusal at 02:00 is still there at the morning
# wake-up, and it clears itself the moment the transport starts working again.
DELIVERY_UNRESOLVED_SECONDS = tunable(
    "PRODUCT_OWNER_DELIVERY_UNRESOLVED_SECONDS", 36 * 3600)

# The manifest lists the deliverables; it is not itself a document for a person.
NOT_A_DOCUMENT = {"manifest.json"}

# These names belong to the execution/review conversation. They are useful to
# the product owner and to the next agent, but they are not a document the user
# is waiting to receive. Task 835 established this distinction by checking the
# real correspondence item by item; treating every file in `deliverables/` as a
# user document put the same conclusions back on the board at every wake-up.
INTERNAL_DOCUMENT_NAMES = frozenset({
    "conclusion-ru.md", "product-owner-review.md", "not-delivered-still-useful.md",
})
INTERNAL_DOCUMENT_PATTERNS = (
    re.compile(r"^(?:cross[-_]?review|review)(?:[-_].*)?\.(?:md|html)$", re.I),
    re.compile(r"(?:^|[-_])verdict(?:[-_].*)?\.(?:md|html)$", re.I),
    re.compile(r"^handoff[-_].*\.(?:md|html)$", re.I),
    re.compile(r"^tail[-_]audit(?:[-_].*)?\.(?:md|html)$", re.I),
)


def review_task(task_dir: Path) -> bool:
    """Whether the task title itself names a review, not merely quoted prose."""
    try:
        text = (task_dir / "task.md").read_text(errors="replace")
    except OSError:
        return False
    title = next((line[2:] for line in text.splitlines() if line.startswith("# ")), "")
    return bool(re.search(r"\b(?:review|ревью)\b", title, re.I))


def internal_document(task_dir: Path, path: Path) -> bool:
    """A conventional run conclusion or review hand-off, by its durable name."""
    name = path.name.casefold()
    if name in INTERNAL_DOCUMENT_NAMES or any(
        pattern.search(name) for pattern in INTERNAL_DOCUMENT_PATTERNS
    ):
        return True
    # Older review tasks named the file by subject first and role last. The task
    # title is the second half of that convention and prevents an ordinary
    # user-facing `market-review.md` from disappearing merely because of a token.
    return review_task(task_dir) and bool(re.search(
        r"(?:^|[-_])review(?:[-_].*)?\.(?:md|html)$", name, re.I))


def registered_human_documents(task_dir: Path) -> list[Path] | None:
    """The single reader document declared by an ordered task manifest.

    A manifest registers every output, including instructions, programs and
    material preserved for analysis.  It does not make every output a reader
    document.  The installation's user contract supplies the missing narrow
    fact: a delivered document is HTML and there is exactly one.  Stable
    manifest order decides between several HTML outputs without guessing from
    words in their names.  Older structured entries may declare text/html
    explicitly even when their stored suffix is not .html.

    ``None`` means that this older task has no usable manifest and needs the
    legacy fallback.  An existing valid manifest with no HTML returns an empty
    list: its other registered outputs do not create document debt.
    """
    manifest_path = task_dir / "deliverables" / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if isinstance(manifest, dict):
        entries = manifest.get("deliverables")
    elif isinstance(manifest, list):
        entries = manifest
    else:
        entries = None
    if not isinstance(entries, list):
        return None
    box = manifest_path.parent
    for entry in entries:
        media_type = None
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict):
            name = entry.get("path")
            media_type = entry.get("media_type")
        else:
            continue
        if not isinstance(name, str) or Path(name).name != name:
            continue
        is_html = (media_type == "text/html" if isinstance(media_type, str)
                   else Path(name).suffix.casefold() == ".html")
        path = box / name
        if is_html and path.is_file():
            return [path]
    return []


def human_documents(task_dir: Path) -> list[Path]:
    """Files this task made for the user, with a manifest-owned single HTML."""
    registered = registered_human_documents(task_dir)
    if registered is not None:
        return registered
    found: list[Path] = []
    box = task_dir / "deliverables"
    if box.is_dir():
        try:
            found += [
                path for path in sorted(box.iterdir())
                if path.is_file()
                and path.name not in NOT_A_DOCUMENT
                and not internal_document(task_dir, path)
            ]
        except OSError:
            pass
    try:
        found += [
            path for path in sorted(task_dir.glob("*.html"))
            if path.is_file() and not internal_document(task_dir, path)
        ]
    except OSError:
        pass
    return found


def human_document(task_dir: Path) -> dict | None:
    """A user-facing file this task made, by name and size and nothing else.

    A current manifest owns one ordered HTML choice. Older tasks without a
    usable manifest retain the file fallback so historical board state does not
    disappear merely because it predates this contract.
    """
    found = human_documents(task_dir)
    if not found:
        return None
    biggest = max(found, key=lambda p: p.stat().st_size if p.exists() else 0)
    try:
        size = biggest.stat().st_size
    except OSError:
        size = None
    return {"name": str(biggest.relative_to(task_dir)), "bytes": size,
            "count": len(found),
            "src": "первый зарегистрированный HTML или старый файловый fallback"}


def _json_lines(path: Path) -> list[dict]:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def attachment_observations(
    mail_sent: Path = MAIL_SENT,
    tasks_root: Path = REPO / "tasks",
    telegram_sent: Path = TELEGRAM_SENT,
) -> dict[tuple[str, int], list[dict]]:
    """Persisted evidence that a file with this name and size reached the user.

    The live mail mirror is the primary source. Historical email and Telegram
    exports are also durable observations: task 835 made them once from the real
    correspondence and verified ten samples byte-for-byte. Future direct bot
    sends may append the same compact shape to `telegram_sent`; task-local
    runner deliveries remain covered by their own receipt journal.
    """
    found: dict[tuple[str, int], list[dict]] = {}

    def add(attachment: dict, channel: str, message_id: object,
            at: object = None) -> None:
        name = attachment.get("filename") or attachment.get("file_name")
        size = attachment.get("size")
        if not isinstance(name, str) or not name or not isinstance(size, int):
            return
        observation = {"channel": channel, "message_id": message_id,
                       "sha256": attachment.get("sha256"), "at": at}
        bucket = found.setdefault((name, size), [])
        if observation not in bucket:
            bucket.append(observation)

    try:
        metadata_paths = sorted(mail_sent.glob("*/metadata.json"))
    except OSError:
        metadata_paths = []
    for path in metadata_paths:
        try:
            row = json.loads(path.read_text(errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        for attachment in row.get("attachments", []) or []:
            if isinstance(attachment, dict):
                add(attachment, "email", row.get("message_id") or path.parent.name,
                    row.get("date"))

    try:
        email_exports = sorted(tasks_root.glob("*/evidence/gmail-sent.jsonl"))
        telegram_exports = sorted(tasks_root.glob("*/evidence/telegram-documents.jsonl"))
    except OSError:
        email_exports, telegram_exports = [], []
    for path in email_exports:
        for row in _json_lines(path):
            for attachment in row.get("attachments", []) or []:
                if isinstance(attachment, dict):
                    add(attachment, "email", row.get("id") or row.get("message_id"),
                        row.get("date"))
    for path in telegram_exports:
        for row in _json_lines(path):
            # The historical export spans every private dialog. Only files the
            # user sent themselves or received from the product's delivery bot
            # establish this product's hand-off; a coincidental third-party file
            # does not. Which dialog that bot is, is a name of the installation
            # and stands in `threads.json`; an installation that names none keeps
            # the narrower half of the rule instead of guessing.
            dialog = product_memory.delivery_dialog()
            if (row.get("from_me") is True
                    or (dialog and str(row.get("dialog", "")).casefold() == dialog)):
                add(row, "telegram", row.get("message_id"), row.get("date"))
    for row in _json_lines(telegram_sent):
        # This compact runtime journal is written only after our own bot send.
        add(row, "telegram", row.get("message_id"), row.get("date"))
    return found


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _file_sha256(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def matching_observations(path: Path,
                          observed: dict[tuple[str, int], list[dict]]) -> list[dict]:
    """Matches for these bytes; digest decides wherever a source carries one."""
    size = _file_size(path)
    if size is None:
        return []
    candidates = observed.get((path.name, size), [])
    legacy = [item for item in candidates if not item.get("sha256")]
    hashed = [item for item in candidates if item.get("sha256")]
    if not hashed:
        return candidates
    digest = _file_sha256(path)
    return legacy + [item for item in hashed if item.get("sha256") == digest]


def delivery_receipts(task_dir: Path) -> list[dict]:
    """Receipts about a document rather than about the life of the run.

    Every one of them, delivered or not, because «квитанций о документах нет
    вовсе» and «есть, и ни одна не говорит о доставке» are two different things
    to tell a reader. Which of them arrived is `message_id`: a document that did
    *not* go — `document_delivery_refused`, a claim whose outcome was lost, a
    claim still open — carries a null one by contract, and the id of the notice
    *about* the failure is a different fact under a different name.
    """
    path = task_dir / "dev-pipeline" / "notification-receipts.jsonl"
    try:
        text = path.read_text()
    except OSError:
        return []
    found = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = payload.get("kind")
        if kind and kind not in LIFECYCLE_RECEIPTS:
            found.append({"kind": kind, "at": payload.get("recorded_at"),
                          "document": payload.get("document"),
                          "sha256": payload.get("sha256"),
                          "message_id": payload.get("message_id")})
    return found


def delivery_receipt(task_dir: Path) -> dict | None:
    """The first receipt that says a person has a document, if there is one."""
    found = [item for item in delivery_receipts(task_dir) if item.get("message_id")]
    return found[0] if found else None


def receipt_for(name: str, path: Path, receipts: list[dict]) -> dict | None:
    """The receipt that says a person has *these* bytes under this name.

    The sender keys its own journal by content digest and records the name
    beside it, so this reads the pair the same way round: a digest decides
    wherever the receipt carries one, and the name is what is left to go on
    when it does not. A file rewritten after its receipt therefore stops
    counting — the person has the old one — which is the rule
    `matching_observations` already applies to persisted attachments.
    """
    digest, hashed = None, False
    for receipt in receipts:
        want = receipt.get("sha256")
        if want:
            if not hashed:
                digest, hashed = _file_sha256(path), True
            if want == digest:
                return receipt
            continue
        if receipt.get("document") == name:
            return receipt
    return None


# The names the contour actually writes its delivery disposition under.
# `delivery.md` is
# the one the audit of 835 wrote 71 times; `product-owner-delivery.md` is the one
# the product owner writes by hand when they send a document themselves, and on
# 2026-08-06 they wrote six of them — including all three documents of the very
# decision this observation has to check. Both carry the same three things, which
# is what makes them one convention rather than two: channel, message identifier,
# sha256 of what was attached. Knowing only the first name meant the observer
# called three deliveries of that evening undelivered while the letters were in
# the user's mailbox. `product-owner-decision.md` records the other terminal
# disposition: the document deliberately does not go in a separate message.
DELIVERY_NOTES = ("delivery.md", "product-owner-delivery.md", "product-owner-decision.md")


def delivery_note(task_dir: Path) -> Path | None:
    """The delivery disposition under any of the names the contour uses."""
    for name in DELIVERY_NOTES:
        path = task_dir / name
        if path.is_file():
            return path
    return None


def handoff(task_dir: Path,
            observed: dict[tuple[str, int], list[dict]] | None = None) -> dict | None:
    """Whether what this task made ever reached a person, and what said so.

    Nothing made for a person means nothing to hand over, and the task is not in
    this area at all. Where there are documents, three observations can close
    them: the delivery note the contour writes itself, receipts that are not the
    run's own lifecycle events, and persisted attachment observations.

    The question is asked of the whole set. A receipt names the document it is
    about, so «доставлено» means every document of this task carries evidence —
    not that at least one does. Cross-review 843 built the case: `sent.txt`
    delivered, the larger `failed.txt` refused, and this function called the
    task delivered while the card named the refused file as its document. A
    partly handed-over task belongs in «сделано, но не доставлено» with the
    missing names said out loud, which is exactly the area that exists for a
    result nobody received.
    """
    documents = human_documents(task_dir)
    document = human_document(task_dir)
    if not document:
        return None
    note = delivery_note(task_dir)
    if note:
        return {**document, "delivered": True, "missing": [],
                "delivered_src": f"файл {note.name} в каталоге задачи"}
    # Only a receipt carrying a message identifier says a person has something.
    # The rest are kept, because a journal full of refusals is a different story
    # to tell than a journal with no document receipts at all.
    about_documents = delivery_receipts(task_dir)
    receipts = [receipt for receipt in about_documents if receipt.get("message_id")]
    # A delivery receipt from before receipts said which document they were
    # about, or from a sender of our own that is not the pipeline: it names
    # neither a document nor a digest, so it is about this task as a whole and
    # cannot be correlated. It still closes the task — refusing to believe an
    # uncorrelatable receipt would be inventing an alarm.
    nameless = [receipt for receipt in receipts
                if not receipt.get("document") and not receipt.get("sha256")]
    if nameless:
        return {**document, "delivered": True, "missing": [],
                "delivered_src": f"квитанция {nameless[0]['kind']} с идентификатором сообщения "
                                 f"в dev-pipeline/notification-receipts.jsonl"}
    # The full snapshot passes one index shared by every task. A direct helper
    # call stays local and deterministic unless its caller supplies observations.
    observations = observed or {}
    names = {path: str(path.relative_to(task_dir)) for path in documents}
    receipted = {names[path]: receipt_for(names[path], path, receipts)
                 for path in documents}
    matched = {names[path]: matching_observations(path, observations)
               for path in documents}
    missing = [name for name in names.values()
               if not receipted[name] and not matched[name]]
    receipted_count = sum(bool(item) for item in receipted.values())
    observed_count = sum(bool(rows) for rows in matched.values())
    if names and not missing:
        said = []
        if receipted_count:
            said.append(f"квитанции с идентификаторами сообщений на {receipted_count} из "
                        f"{len(names)} документов задачи "
                        "в dev-pipeline/notification-receipts.jsonl")
        if observed_count:
            channels = sorted({item["channel"] for rows in matched.values() for item in rows})
            said.append("сохранённые наблюдения вложений: " + ", ".join(channels))
        return {**document, "delivered": True, "missing": [],
                "delivered_src": "; ".join(said)}
    near = []
    for path in documents:
        versions = [(size, rows) for (name, size), rows in observations.items()
                    if name == path.name and size != _file_size(path) and rows]
        if versions:
            sizes = ", ".join(str(size) for size, _rows in sorted(versions))
            channels = sorted({row["channel"] for _size, rows in versions for row in rows})
            dates = sorted({str(row["at"])[:10] for _size, rows in versions for row in rows
                            if row.get("at")})
            where = ", ".join([*channels, *dates])
            near.append(f"{path.name}: прежние размеры {sizes} байт ({where})")
    near_text = ("; найдены одноимённые прежние версии — " + "; ".join(near)) if near else ""
    # Which of the two sentences about receipts is true here. A task whose
    # journal names documents is not a task whose journal is all lifecycle
    # events, and saying so would hide exactly the partial hand-over the set
    # rule exists to show.
    if receipts:
        went = [name for name, item in receipted.items() if item]
        receipt_clause = (f"квитанции с идентификатором сообщения есть на {receipted_count} из "
                          f"{len(names)}: нет на "
                          f"{', '.join(name for name in names.values() if name not in went)}"
                          " — частичная выдача не считается доставкой")
    elif about_documents:
        kinds = ", ".join(sorted({receipt["kind"] for receipt in about_documents}))
        receipt_clause = (f"квитанции о документах в задаче есть ({kinds}), но ни одна не несёт "
                          "идентификатора сообщения — доставки среди них нет")
    else:
        receipt_clause = ("квитанции задачи несут только события жизненного цикла прогона — "
                          "доставки документа среди них нет")
    return {**document, "delivered": False, "missing": missing,
            "delivered_src": "записки о доставке в каталоге нет ни под одним из имён "
                             f"({', '.join(DELIVERY_NOTES)}), а {receipt_clause}; "
                             f"в сохранённых вложениях найдено {observed_count} из {len(documents)} файлов"
                             f"{near_text}"}


# Entries of the task directory the card lists. Names, sizes and mtimes only:
# opening any of them would be reading the work rather than observing it, and a
# transcript must not be listed at all.
FILES_ON_CARD = 40


def task_files(task_dir: Path) -> list[dict]:
    """Top-level entries of the task directory, newest first. Names, never content."""
    entries = []
    try:
        children = list(task_dir.iterdir())
    except OSError:
        return []
    for path in children:
        if TRANSCRIPT.search(path.name):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append({
            "name": path.name + ("/" if path.is_dir() else ""),
            "bytes": None if path.is_dir() else stat.st_size,
            "at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        })
    entries.sort(key=lambda e: e["at"], reverse=True)
    return entries[:FILES_ON_CARD]


def board_area(status: str | None, flags: list[str], asked_user: bool,
               blocked_by: str | None = None, ours: bool = False,
               undelivered: bool = False, ready: bool = False,
               decision_unmet: bool = False, plan_role: str | None = None) -> str:
    """Which area of the board a task stands in. One rule, one owner.

    `asked_user` means a question was put to the user in writing and no answer
    has been observed since — see `question_entry`, which owns that judgement. It
    used to mean «any open question at all», and `blocked` was accepted as a
    second, silent synonym for waiting on a person; between them they put
    technical blockages, an already-migrated repair, our own product decisions
    and questions the user had answered in writing under «ждёт решения человека»
    (finding HIGH-1 of review 786, and the user's own count of 2026-08-06: of
    sixteen entries, three were his).

    A blocked task is what it is: work that stands and cannot move — a jam, with
    its reason next to it. Our own open question is what it is too: a decision
    the product owner owes, standing in «Решает продакт» where the person who
    owes it will see it.

    A finished task whose document never reached anyone is not «Сделано». It is
    the case of 783 — a 441 KB report that lay on the server for an hour behind
    two receipts about the life of the run — and it gets its own area.

    `plan_role` is what the current revision of the portfolio plan says about
    this task, and it owns two areas outright. Observation cannot own them: it
    has no way to know an order nobody wrote down, and `planned` is a status
    rather than a place in a queue — the plan says exactly that itself. So a task
    the plan queued stands «в очереди» whatever the disk is doing, and a task a
    holding line of the plan names stands «в бэклоге» instead of being offered as
    work to pick up. A task the revision never names is neither: it is offered
    exactly as before, because silence is not a hold.

    A decision taken out loud and not carried out is the same hole one step
    earlier. On 2026-08-06 the product owner wrote «из девяти живых документов
    человеку идут три» and three hours later none of the three had been sent: the
    decision lived in a sentence, so no observation and no wake-up knew it was
    outstanding. `decision_unmet` is that decision as a state — recorded in the
    task's own field, checked against the delivery evidence the contour already
    writes, and therefore able to close itself.

    A live run outranks a terminal status: a status is a record somebody wrote, a
    running process is an observation. The client column showed «в работе 0»
    while round 5 of 1151 ran under a stale `completed`.
    """
    if status in TERMINAL and "live" not in flags:
        # A decision the product owner took and nobody carried out outranks the
        # passive «не доставлено»: one is work that was merely never looked at,
        # the other is work somebody already decided must go out.
        if decision_unmet:
            return "decision_unmet"
        # «Сделано, но не доставлено» promises a finished task, and a cancelled
        # one is not finished — nobody is owed its document. Task 669 was
        # cancelled as superseded by 722, kept files in `deliverables/`, and so
        # stood among 46 genuinely completed tasks as the 47th (finding MEDIUM-1
        # of review 826). Every terminal status still counts as «Сделано» here,
        # exactly as before; only the undelivered claim narrows.
        return "undelivered" if undelivered and status == "completed" else "done"
    # An unanswered question is the one work state that outranks both a live
    # run and a held file: the user has been explicitly asked to decide before
    # the work can move. Preserve that established board contract.
    if asked_user:
        return "waiting_human"
    if "live" in flags:
        return "running"
    # A ready file that the person does not have is a delivery debt regardless
    # of the task's working status. Task 1316 held its registered answer for
    # five days under `blocked`, where the old terminal-only rule hid it as a
    # generic jam. A live child still outranks the file: bytes may be changing.
    if undelivered:
        return "undelivered"
    if {"stale_label", "killed", "gap", "blocked", "work_outside_owner"} & set(flags):
        return "stuck"
    # An unexecuted decision on a task that is not finished stands here: below
    # the facts about the work, above every list of what one *might* do, because
    # somebody already decided this one.
    if decision_unmet:
        return "decision_unmet"
    # Our own question stands below the live run and the jam — those are facts
    # about the work, and this is a fact about a decision — but above «можно
    # подхватить»: a task nobody has decided about is not free to start.
    if ours:
        return "product_owner"
    # Бэклог — то, что само не поедет, и это ровно одно наблюдение: план держит
    # эту задачу своей строкой. Раньше такая работа стояла под «можно
    # подхватить», то есть доска предлагала подхватить остановленное словом
    # пользователя.
    #
    # Стоит выше очереди и выше наблюдаемого держателя, потому что отвечает на
    # другой вопрос. «За чем стоит» — про порядок работ, который однажды дойдёт
    # до этой задачи сам; «сам не запустится» — про то, что не дойдёт, пока
    # человек не скажет слова. У 1054 и 1067 есть и то и другое — незакрытая
    # предшественница и полка по слову пользователя, — и ниже держателя область
    # «В бэклоге» на живом состоянии оказывалась пустой на всех четырёх
    # направлениях, то есть четвёртый вопрос пользователя оставался без ответа.
    # Держатель при этом никуда не девается: он стоит на самой плашке.
    #
    # Молчание плана сюда не входит и не входило по праву. Круг 1 независимого
    # ревью показал на четырёх задачах (1091, 1093, 1130, 1135), что «редакция её
    # не называет» и «по ней нет разбора и решения» — разные утверждения: все
    # четыре разобраны в собственных task.md. Такая задача автоматом как раз
    # запускается, поэтому область «сам не запустится» о ней солгала бы дважды —
    # и в картине, и в автоматике запуска, из которой она её убирала.
    #
    # Держит только строка, предметом которой эта задача стоит. Круг 2 попробовал
    # держать и всё, что держащая строка называет рядом, и круг 3 показал на
    # живом снимке, чего это стоит: строка «исследование 1150 и 1151 разрешено»
    # объявляла остановленной прямо разрешённую пользователем работу и убирала её
    # из запуска. Страховка от неудачной формулировки принадлежит плану: работу,
    # которую держит чьё-то слово, он называет отдельной строкой со своим номером
    # в начале (редакция 34).
    if plan_role == "paused":
        return "backlog"
    # Everything below used to be one area called «в очереди», which answered
    # neither of the two questions a person actually opens the board with.
    # «Что можно подхватить прямо сейчас» is the first question of every
    # wake-up, and «за чем стоит остальное» is the second; they are the same
    # split, taken on one observation — whether anything on disk is holding
    # this task. Nothing holding it means it can be started now.
    if blocked_by:
        return "queued"
    # Очередь принадлежит плану, а не наблюдению: задача, которую действующая
    # редакция поставила в `next`, стоит в очереди, даже если на диске её ничто не
    # держит. Именно этого расхождения и стоила прежняя доска: 13 августа 1121 и
    # 1138 лежали под «можно подхватить», хотя редакция назначила им места.
    if plan_role == "queue":
        return "queued"
    # «Готово к запуску» is narrower than «можно подхватить» and that is the
    # whole of its value. Both say nothing is holding the task; only this one
    # says somebody wrote a condition down and the condition has since been met.
    # 831 belonged here for forty minutes and there was nowhere to put it.
    if ready:
        return "ready_to_start"
    return "pickup"


def jam_reason(status_detail: str | None, run: dict, verdicts: list[dict],
               flags: list[str], condition: dict | None = None) -> tuple[str | None, str | None]:
    """Why this task stands where it stands, and what observed it.

    `condition` says the detail is a recognised start condition rather than
    prose. Then the reason is not the field's text — a reader is owed «за чем
    именно стоит», not a grammar — and it is left to `assign_areas`, which is the
    only place that has seen every task and can say whether the condition is
    still holding and by what.

    The board named the jam and could not say why, although the reason was
    sitting in the same payload: task 686 showed `happening: null` while its
    frontmatter carried `queued_behind_active_worktree_writers_669_689` (finding
    HIGH-2 of review 786). `happening` comes from a live child's `progress.json`
    and is therefore empty for exactly the tasks that are stuck, which is the one
    case the caption was needed for.

    Four observable sources, most specific first. Nothing is derived, guessed or
    softened: where the disk says nothing, this returns nothing and the plate
    stays silent rather than filling the space.
    """
    if status_detail and condition is None:
        return status_detail, "поле status_detail во frontmatter task.md"
    if run.get("stop_reason"):
        return run["stop_reason"], "watcher_stop_reason в .runner/runner.json"
    failed = [v["gate"] for v in verdicts if v["result"] != "OK"]
    if failed:
        # Имя гейта пишут машине: `f_003_closure_records`,
        # `documentation_consumers_agree`. Читателю оно не говорит ничего, а на
        # первом экране стояло ответом на «почему стоит» — разбирать его глазами
        # приходилось ему (ревью круга 3, HIGH-1). Ответ ему — сколько гейтов не
        # прошло; какие именно, стоит рядом за переключателем источников, где и
        # живёт остальной аппарат происхождения.
        return ("не пройдено гейтов: " + str(len(failed)),
                "строки Result в verification.md: " + ", ".join(failed[:3]))
    if run.get("refusal"):
        return (run.get("refusal_summary") or run["refusal"],
                "completion_refusal в status.json")
    if "stale_label" in flags:
        return ("ярлык говорит running, а записанного процесса нет",
                "status.json против личности процесса из .runner/runner.json")
    if run.get("alive_src") and not run.get("alive"):
        # Unobservable is not dead, and the plate has to say which one it is.
        return run["alive_src"], "сверка пространства имён PID с .runner/runner.json"
    return None, None


# ---------------------------------------------------------------------------
# Условие запуска, которое машина видит
# ---------------------------------------------------------------------------
#
# The condition on which a planned task may start used to live in a sentence.
# 831 carried «Запускать только после завершения прогона 830: он идёт в том же
# рабочем дереве /opt/projects/example-engine» in the last line of its
# `## Summary`, and that sentence is invisible to everything: the observer could
# not tell that the condition had been met, so «препятствие исчезло» could be
# neither a state nor a transition, and the task stood forty minutes after 830
# closed until the user asked how the queue is tracked at all.
#
# The condition therefore has to be a field. It is `status_detail` — the field
# that already carries «почему эта задача стоит», is already written by the one
# interface allowed to write frontmatter (`tasks_index.py set-status --detail`),
# is already read here by `jam_reason` and `queue_reason`, and already carried
# this exact meaning in the contour's own shorthand
# (`queued_behind_active_worktree_writers_669_689` on 686). A second field or a
# second file would be a second mechanism for one concept.
#
# What is new is not the field but that the observer now *evaluates* it. Until
# now any `status_detail` was an opaque hold that could never clear: prose does
# not stop being prose when the thing it describes has finished. A recognised
# condition is checked against observed state — the named tasks' statuses and
# whether the named working tree has a live run — so it clears itself.
#
# Grammar, deliberately small and ASCII so it survives YAML escaping and reads
# to a person as well as to the parser:
#
#     starts_after=830 worktree=/opt/projects/example-engine
#     decision=deliver
#
# Anything the grammar does not recognise behaves exactly as before: an opaque
# hold, shown to the reader in the words its author wrote. The change of
# behaviour is confined to text that opted into it.
STARTS_AFTER = re.compile(r"\bstarts_after\s*=\s*([0-9]+(?:\s*,\s*[0-9]+)*)", re.IGNORECASE)
WORKTREE = re.compile(r"\bworktree\s*=\s*([^\s;,]+)", re.IGNORECASE)
DECISION = re.compile(r"\bdecision\s*=\s*([a-z_]+)", re.IGNORECASE)

# Decisions this observer can check the execution of. A decision it cannot check
# is not recorded as one: an area that cannot say «исполнено» would be a second
# list of prose, which is the defect, not the repair.
DECISIONS = {"deliver"}


def start_condition(status_detail: str | None) -> dict | None:
    """The machine-readable start condition of a task, or `None`.

    `None` covers both «поля нет» and «в поле проза»: neither is a condition this
    module may act on, and treating unrecognised prose as a condition would let
    the board invent a queue nobody wrote down.
    """
    if not status_detail:
        return None
    after: list[int] = []
    for match in STARTS_AFTER.finditer(status_detail):
        after += [int(part) for part in match.group(1).replace(" ", "").split(",")]
    worktrees = [match.group(1) for match in WORKTREE.finditer(status_detail)]
    decision = None
    match = DECISION.search(status_detail)
    if match and match.group(1).lower() in DECISIONS:
        decision = match.group(1).lower()
    if not after and not worktrees and not decision:
        return None
    return {
        "after": sorted(set(after)),
        "worktrees": sorted(set(worktrees)),
        "decision": decision,
        "src": "поле status_detail во frontmatter task.md",
    }


def condition_state(condition: dict, statuses: dict, busy_repos: dict,
                    task_id: int | None) -> dict:
    """Whether a recorded start condition is still holding the task, and what said so.

    Two observations and nothing else, each named where it is reported:

    * the status of every task the condition names, taken from the same index the
      board is built from — a task not yet terminal is still holding;
    * whether the named working tree has a live run, taken from the same
      `busy_repos` map the queue reason already uses — two children in one tree
      is the collision the condition exists to prevent.

    A task the index does not know is *not* silently treated as finished. An
    unobservable predecessor is reported as unobservable and keeps holding, so a
    typo in a number can never promote work to «готово к запуску».
    """
    holding: list[str] = []
    for other in condition["after"]:
        status = statuses.get(other)
        if status is None:
            holding.append(f"задачи {other} нет в индексе, состояние не наблюдается")
        elif status not in TERMINAL:
            holding.append(f"задача {other} ещё не закрыта ({status})")
    for tree in condition["worktrees"]:
        holder = busy_repos.get(tree)
        if holder and holder.get("id") != task_id:
            holding.append(f"рабочее дерево {Path(tree).name} занято живым прогоном "
                           f"задачи {holder['id']}")
    met = [f"задача {other} закрыта ({statuses[other]})" for other in condition["after"]
           if statuses.get(other) in TERMINAL]
    met += [f"рабочее дерево {Path(tree).name} свободно" for tree in condition["worktrees"]
            if not busy_repos.get(tree)]
    return {
        "after": condition["after"],
        "worktrees": condition["worktrees"],
        "decision": condition["decision"],
        "holding": holding,
        "met": met,
        # «Готово к запуску» is the whole point: a condition was written down and
        # nothing it names is holding any more.
        "satisfied": not holding,
        "src": "статусы названных задач в индексе задач и живость прогонов "
               "в тех же рабочих деревьях",
    }


def queue_reason(task: dict, run: dict, busy_repos: dict,
                 condition: dict | None = None) -> tuple[str | None, str | None]:
    """What is holding this task, when something observably is.

    «В очереди» on its own answers nothing: the user asked for «за чем именно
    стоит» — busy repository, waiting review, waiting a person, waiting host
    memory — and asked for the reason to be observed rather than guessed. So
    each branch below names the file it read, and a task nothing on disk holds
    returns nothing and lands under «можно подхватить», which is the point.

    `busy_repos` maps a repository path to the task that has a live run in it,
    and is built once per snapshot: one direction owns its repositories, and a
    second write-run in a repository somebody is already writing to is the one
    queue this contour genuinely has.

    `condition` is the evaluated start condition when `status_detail` carried
    one. It is the one branch that can *stop* holding: a recognised condition
    whose named tasks have closed and whose named tree is free returns nothing
    here, which is how a queued task becomes startable without anybody editing
    the field. Prose keeps its old behaviour — an opaque hold in its author's own
    words — because nothing observable can tell when a sentence stopped being
    true.
    """
    if condition is not None:
        if condition["holding"]:
            return "; ".join(condition["holding"]), condition["src"]
        return None, None
    detail = task.get("status_detail")
    if detail:
        return detail, "поле status_detail во frontmatter task.md"
    repo = run.get("repo")
    holder = busy_repos.get(repo) if repo else None
    if holder and holder.get("id") != task.get("id"):
        return (f"репозиторий {Path(repo).name} занят живым прогоном задачи {holder['id']}",
                "поле --repo в .runner/runner.json обеих задач и живость прогона держателя")
    return None, None


def busy_repository_map(entries: list[dict]) -> dict:
    """Repositories that currently have a live run, and whose run it is.

    Built from the same task entries the board is built from, so «занят» means
    exactly «одна из показанных задач держит его живым прогоном» and cannot
    drift from what the columns show.
    """
    busy: dict[str, dict] = {}
    for entry in entries:
        repo = (entry.get("run") or {}).get("repo")
        if repo and "live" in entry.get("flags", []):
            busy.setdefault(repo, {"id": entry.get("id"), "title": entry.get("title")})
    return busy


OPEN_QUESTIONS = re.compile(r"^##\s+Open Questions\s*$(.*?)(?=^##\s|\Z)",
                            re.MULTILINE | re.DOTALL)

# Что задача такое, словами человека, а не состоянием. Единственное место на
# диске, где это написано, — раздел `## Summary` в `task.md`; карточка задачи
# показывала всё остальное и не показывала этого (пункт 5 задачи 864).
SUMMARY = re.compile(r"^##\s+Summary\s*$(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)

# Сколько знаков описания попадает в снимок. Карточка ничего не обрезает, но
# снимок ездит в каждой живой выдаче целиком, и описание на сто задач — это
# полезная нагрузка, которую никто не читает. Отсечка щедрая: она длиннее любого
# наблюдавшегося `## Summary` и стоит здесь как предел, а не как формат.
SUMMARY_CHARS = 1200


def summary(task_dir: Path) -> str | None:
    """Текст раздела `## Summary` из `task.md`, одной строкой абзацев."""
    try:
        text = (task_dir / "task.md").read_text()
    except OSError:
        return None
    match = SUMMARY.search(text)
    if not match:
        return None
    body = "\n".join(line.rstrip() for line in match.group(1).strip().splitlines()).strip()
    if not body:
        return None
    if len(body) > SUMMARY_CHARS:
        # Обрезка называет себя: молчаливая выглядела бы как конец описания.
        body = body[:SUMMARY_CHARS].rstrip() + "… (описание длиннее, чем показано)"
    return body

# A bullet that answers itself in the same breath. The contour writes exactly
# this shape — «Продуктовый ответ: да, потому что…» under the question it settles
# (task 723) — and reading it as an open question is how the board reported a
# decision that had already been taken.
ANSWERED_HERE = re.compile(r"\b(ответ|решение|answer|decision|decided)\b\s*:", re.IGNORECASE)

# The product's own convention for a closed question, already honoured by
# `products()`: a struck-through line is settled.
STRUCK = "~~"


def open_questions(task_dir: Path) -> list[str]:
    """Bullets of `## Open Questions` that are not the literal `none`."""
    path = task_dir / "task.md"
    try:
        text = path.read_text()
    except OSError:
        return []
    match = OPEN_QUESTIONS.search(text)
    if not match:
        return []
    items: list[str] = []
    for line in match.group(1).splitlines():
        if line.startswith("- "):
            items.append(line[2:].strip())
        elif items and line.startswith("  ") and line.strip():
            # A question wrapped over several lines is one question. Taking only
            # its first line cut real sentences in half on the plate.
            items[-1] += " " + line.strip()
    return [item for item in items if item.lower() not in {"none", "нет"}]


def unsettled_questions(items: list[str]) -> list[str]:
    """Bullets that ask something and are not settled in their own sentence.

    A heading is a place, not a claim. `## Open Questions` holds whatever its
    author put there, and in this contour that includes notes with no question in
    them at all («выбора, ожидающего его ответа, в ней нет»), instructions for
    reading a future result, and questions settled in their own bullet. Counting
    the heading counted all three as people-blocking work (finding HIGH-1 of
    review 786).

    This answers «is this an open question at all», and that is all it answers.
    Whose question it is — the user's or ours — is a second question with a second
    owner, `question_entry`, because the two used to be one and the board read
    every open question of ours as work the user was blocking.
    """
    return [item for item in items
            if "?" in item and not ANSWERED_HERE.search(item) and STRUCK not in item]


# ---------------------------------------------------------------------------
# «Ждёт решения человека» обязано значить «ждёт пользователя»
# ---------------------------------------------------------------------------
#
# The area used to hold every open question the contour had written down
# anywhere, and on the live state of 2026-08-06 it held sixteen entries of which
# three belonged to the user. The other thirteen were our own product decisions,
# questions to an executor about their environment, a repair already made, and
# questions the user had already answered in writing. A board that tells a person
# they are blocking thirteen things they never saw is worse than a board with no
# such area: it was contradicted by the same contour's own letters, which told
# the user nothing was required of them.
#
# So the area is bounded by two observations and nothing else, both taken from
# text and files rather than from a heading:
#
#   1. the question carries a written mark that it went to the user, with the
#      date and the channel it went through, and the identifier of the message;
#   2. no answer has been observed since — neither a letter from the user in that
#      same thread, nor a decision recorded in the product record itself.
#
# Everything else is ours. It is not hidden: it stands in its own area, «Решает
# продакт», next to the person who actually owes the decision.

ASKED = re.compile(
    r"спрошено\s+у\s+пользовател\w*\s+(\d{4}-\d{2}-\d{2})\s*,\s*"
    r"(письм\w*|почт\w*|telegram|телеграм\w*)\s*[«\"'`]*\s*([0-9A-Za-z_-]{2,})",
    re.IGNORECASE)

# The user's own answer, written into the line by whoever read it. The product
# records already close a question this way — «**Закрыт 2026-08-06 письмом
# пользователя**» — so the convention is honoured rather than replaced.
ANSWERED_BY_USER = re.compile(
    r"(?:закрыт\w*|отвеч\w*|уточн\w*)[^.\n]{0,80}?пользовател", re.IGNORECASE)

EMAIL_WORDS = ("письм", "почт")


def asked_of_user(text: str) -> dict | None:
    """The written mark that this question was put to the user, and what it says.

    Date, channel and message identifier or nothing. A question without the mark
    is not «probably asked»: it is ours, and the board says so.
    """
    match = ASKED.search(text)
    if not match:
        return None
    at, channel, ref = match.group(1), match.group(2).lower(), match.group(3)
    kind = "email" if channel.startswith(EMAIL_WORDS) else "telegram"
    return {"at": at, "channel": kind, "ref": ref,
            "src": f"пометка «спрошено у пользователя {at}, "
                   f"{'письмо' if kind == 'email' else 'Telegram'} {ref}» в самой строке"}


def thread_key(subject: str) -> str:
    """One name for a mail thread, whatever prefix a client put in front of it."""
    text = subject or ""
    while True:
        stripped = re.sub(r"^\s*(re|fwd|fw|пересылка|отв)\s*(\[\d+\])?\s*:\s*", "",
                          text, flags=re.IGNORECASE)
        if stripped == text:
            break
        text = stripped
    return normal(text)


def mail_message(path: Path) -> dict | None:
    """One stored message: its identifier, its thread and when it arrived."""
    payload = read_json(path / "metadata.json")
    if not payload.get("message_id"):
        return None
    try:
        at = parsedate_to_datetime(payload.get("date") or "")
    except (TypeError, ValueError):
        at = None
    if at is not None and at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return {"id": payload["message_id"], "thread": thread_key(payload.get("subject") or ""),
            "at": at, "subject": payload.get("subject") or ""}


def mail_moment(at: datetime) -> str:
    """A mail instant as the board says it: one zone for both sides of a pair.

    Two letters in the plate's own sentence are compared by the reader, so they
    have to be stated in the same zone; UTC is the one both sides of the seam
    already store.
    """
    return at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def mailbox() -> dict:
    """The product owner's mail as three facts: which thread a message is in,
    when the question itself went out, and when the user last wrote into each
    thread.

    Read once per snapshot from `metadata.json` files that are already on disk —
    no network, no message body, and the same cost whatever was discussed. `sent`
    is what makes an outgoing question resolvable to a thread at all; `inbox` is
    where the answer lands.

    `sent_at` is the instant, not the day. The written mark on a question line
    carries a date and nothing finer, so a board that compares dates cannot tell
    a letter that arrived two hours *before* the question from one that answered
    it. The instant is already in the stored metadata; keeping it here is what
    lets `answer_observed` require an answer that came afterwards (finding HIGH-1
    of review 826: the questions of `msg0000000000003` went out at 16:28:48 UTC
    and `msg0000000000002`, sent 2 h 28 min earlier about a forgotten document,
    was read as their answer).
    """
    threads: dict[str, str] = {}
    replies: dict[str, datetime] = {}
    sent_at: dict[str, datetime] = {}
    for box, incoming in ((MAIL_SENT, False), (MAIL_INBOX, True)):
        if not box.is_dir():
            continue
        for entry in box.iterdir():
            if not entry.is_dir():
                continue
            message = mail_message(entry)
            if not message:
                continue
            threads[message["id"]] = message["thread"]
            if not incoming and message["at"]:
                sent_at[message["id"]] = message["at"]
            if incoming and message["at"]:
                current = replies.get(message["thread"])
                if current is None or message["at"] > current:
                    replies[message["thread"]] = message["at"]
    return {"threads": threads, "replies": replies, "sent_at": sent_at,
            "sent_known": MAIL_SENT.is_dir()}


def answer_observed(asked: dict, mail: dict) -> dict:
    """Whether the user has answered since the question went out, and what said so.

    Three outcomes, and the difference between them is printed on the plate,
    because the honest failure here is claiming silence that was never listened
    for. A question whose thread cannot be resolved is not «unanswered»: it is a
    question whose answer this observer cannot see, and it stays in the area only
    because the product record still carries it as open.
    """
    if asked["channel"] != "email":
        return {"answered": False, "src": None,
                "note": "ответ в Telegram с диска не наблюдается: хранилища сообщений "
                        "пользователя нет — вопрос уходит из области по продуктовой записи"}
    thread = mail["threads"].get(asked["ref"])
    if not thread:
        return {"answered": False, "src": None,
                "note": f"письмо {asked['ref']} не найдено в почтовом хранилище продакта: "
                        "тред не восстановлен, ответ отслеживается только по продуктовой записи"}
    reply = mail["replies"].get(thread)
    if reply is None:
        return {"answered": False, "src": None,
                "note": f"в треде письма {asked['ref']} писем от пользователя нет"}
    sent_at = mail.get("sent_at", {}).get(asked["ref"])
    if sent_at is not None:
        # The instant, when the outgoing letter is on disk. A letter that arrived
        # before the question was sent cannot be its answer, even by minutes and
        # even on the same day — that is exactly the case the board hid.
        if reply <= sent_at:
            return {"answered": False, "src": None,
                    "note": f"последнее письмо пользователя в треде — "
                            f"{mail_moment(reply)}, не позже самого вопроса, "
                            f"отправленного {mail_moment(sent_at)}"}
        return {"answered": True,
                "src": f"письмо пользователя в том же треде от {mail_moment(reply)}, "
                       f"позже вопроса, отправленного {mail_moment(sent_at)}",
                "note": None}
    # No stored outgoing letter: the written mark carries a date and nothing
    # finer, so the day is all this observer honestly has.
    try:
        marked = datetime.fromisoformat(asked["at"]).replace(tzinfo=timezone.utc)
    except ValueError:
        return {"answered": False, "src": None,
                "note": f"дата вопроса {asked['at']!r} не читается как дата"}
    if reply.date() < marked.date():
        return {"answered": False, "src": None,
                "note": f"последнее письмо пользователя в треде — {reply.date().isoformat()}, "
                        f"раньше вопроса от {asked['at']}"}
    return {"answered": True,
            "src": f"письмо пользователя в том же треде от {reply.date().isoformat()}, "
                   f"не раньше вопроса от {asked['at']}",
            "note": None}


def question_entry(text: str, mail: dict) -> dict:
    """One open question with its owner, and with what decided the ownership.

    `user` — asked, in writing, through a named channel, and no answer observed
    since. `product` — everything else, including a question the user has already
    answered while nobody wrote the decision down: that one now waits on us, not
    on them, which is exactly the distinction the board was missing.
    """
    asked = asked_of_user(text)
    if not asked:
        return {"text": text, "owner": "product", "asked_at": None, "channel": None,
                "ref": None, "asked_src": None, "answer_src": None,
                "note": "не помечено как спрошенное у пользователя — это наш вопрос"}
    answer = answer_observed(asked, mail)
    if answer["answered"] or ANSWERED_BY_USER.search(text):
        return {"text": text, "owner": "product", "asked_at": asked["at"],
                "channel": asked["channel"], "ref": asked["ref"],
                "asked_src": asked["src"],
                "answer_src": answer["src"] or "ответ пользователя записан в самой строке",
                "note": "пользователь ответил: решение осталось незаписанным"}
    return {"text": text, "owner": "user", "asked_at": asked["at"],
            "channel": asked["channel"], "ref": asked["ref"],
            "asked_src": asked["src"], "answer_src": None, "note": answer["note"]}


def questions_of(items: list[str], mail: dict) -> list[dict]:
    """Every open question of a place, each with its observed owner."""
    return [question_entry(item, mail) for item in items]


def owed_by(entries: list[dict], owner: str) -> list[dict]:
    return [entry for entry in entries if entry["owner"] == owner]


def task_entry(task: dict, mail: dict,
               observed: dict[tuple[str, int], list[dict]] | None = None) -> dict:
    task_dir = REPO / task["path"]
    run = run_state(task_dir)
    verdicts = gates(task_dir)
    status = task.get("status")

    flags = []
    # Живость наблюдается независимо от статуса: под устаревшим `completed`
    # признак не выставлялся вовсе, и доска писала «в работе 0» при живой работе.
    if run["alive"]:
        flags.append("live")
    if run["state"] == "running" and not run["alive"]:
        # A run claiming to work while its process is gone is the case the map
        # must surface, not hide.
        flags.append("stale_label")
    if status == "blocked":
        flags.append("blocked")
    if any(v["result"] in {"GAP", "BLOCKED", "FAIL"} for v in verdicts):
        flags.append("gap")
    if run["stop_reason"]:
        flags.append("killed")
    if status in TERMINAL and all(v["result"] == "OK" for v in verdicts) and verdicts:
        flags.append("delivered")
    if status not in TERMINAL and not run["alive"] and status:
        flags.append("idle")

    # Work that carried on after its owner died. The contour writes the refusal
    # down itself — `completion_refusal.kind` with «Work it started may have
    # continued outside its process» — and until now nobody read it, so 757 sat
    # finished for three and a half hours and 712 before it. Two observations
    # have to hold together: the owner refused its closing step, and something
    # in the directory moved after the run record last did.
    #
    # A terminal task is excluded: somebody already looked and accepted it, so
    # «сходите посмотрите артефакты» is an instruction to redo a decision that
    # was taken. The signal is for work nobody has been back to.
    moved, moved_age, moved_src = artifact_movement(task_dir)
    outside = False
    if run["refusal"] and not run["alive"] and moved and status not in TERMINAL:
        try:
            outside = moved > datetime.fromtimestamp(
                (task_dir / "status.json").stat().st_mtime, timezone.utc).isoformat()
        except OSError:
            outside = False
    if outside:
        flags.append("work_outside_owner")

    # Whose question it is decides which area the task stands in. A task's
    # `## Open Questions` is our own working list — that is what the section is
    # for — so a bullet reaches «ждёт решения человека» only when it carries the
    # written mark that it went to the user and no answer has been observed since.
    questions = questions_of(unsettled_questions(open_questions(task_dir)), mail)
    asked_user = owed_by(questions, "user")
    ours = owed_by(questions, "product")
    hand = handoff(task_dir, observed)
    actor, actor_src = observed_actor(task_dir, run)
    role, role_src = observed_role(task_dir)
    since, age, since_src = state_age(task_dir)
    status_detail = task.get("status_detail")
    condition = start_condition(status_detail)
    reason_run = run if status not in TERMINAL else {
        **run, "refusal": None, "refusal_summary": None}
    why, why_src = jam_reason(status_detail, reason_run, verdicts, flags, condition)
    progress = run.get("progress") or {}

    return {
        "id": task.get("id"),
        "title": task.get("title"),
        "status": status,
        "status_detail": status_detail,
        "dir": Path(task["path"]).name,
        "run": run,
        "gates": verdicts,
        "flags": flags,
        "questions": questions,
        # The split, kept as data rather than recomputed by every reader: the
        # counter above the columns, the areas and the tests must not be able to
        # disagree about whose question it is.
        "asked_user": asked_user,
        "our_questions": ours,
        # Everything the card shows when a plate is opened. Collected here
        # because the renderer may not reach a disk, so a drill-down that
        # fetched its own detail would be a second door into the contour past
        # the boundary this split exists to hold.
        "detail": {
            # Что задача такое, словами человека. Всё остальное в карточке —
            # состояние; это единственное, что объясняет, зачем она есть.
            "summary": summary(task_dir),
            "review": review_verdict(task_dir),
            "delivery": delivery(task_dir),
            "files": task_files(task_dir),
            "moved": moved,
            "moved_age_seconds": moved_age,
            "moved_src": moved_src,
            # The document this task made for a person, and whether anything
            # observed it reaching them. `None` means the task made no document.
            "handoff": hand,
        },
        "board": {
            # Filled by `assign_areas` once every task is known: whether a task
            # can be picked up depends on what the other tasks are holding.
            "area": None,
            "blocked_by": None,
            "blocked_by_src": None,
            # Что о задаче говорит действующая редакция плана. Тоже заполняется
            # в `assign_areas`: место в очереди — утверждение об одной задаче
            # среди других, и в одиночку его не прочитать.
            "plan_place": None,
            # The start condition, filled by `assign_areas` for the same reason
            # the area is: whether a condition still holds depends on the other
            # tasks. `None` means the task named no condition, which is not the
            # same as a condition that has been met, and the two must never
            # collapse into one answer.
            "start_condition": None,
            # The decision recorded on this task and whether anything observed it
            # carried out. Also filled by `assign_areas`, from the same delivery
            # evidence the contour already writes.
            "decision": None,
            "actor": actor,
            "actor_src": actor_src,
            "role": role,
            "role_src": role_src,
            # One line of what is going on, from the child's own progress line —
            # the only place that says it in words. Absent is absent.
            "happening": progress.get("activity"),
            # Why it stands there, when the disk says why. This is the answer to
            # the acceptance question the board previously could not give.
            "why": why,
            "why_src": why_src,
            "since": since,
            "since_src": since_src,
            "age_seconds": age,
            "attempt": attempt_count(task_dir),
        },
    }


def assign_areas(entries: list[dict], tasks: list[dict],
                 statuses: dict | None = None, plan: dict | None = None) -> None:
    """The second pass: which area each task stands in, once all of them are known.

    «Можно подхватить» is not a property of one task read alone — it is the
    absence of anything holding it, and one of the things that can hold it is
    another task's live run in the same repository. So the areas are assigned
    after every task has been observed, never during.

    `plan` is the projection of the current plan revision — the owner of the
    order and of the pauses. Callers that pass none get the previous behaviour
    exactly: no plan, no queue and no backlog, because neither can be derived
    from what is on disk.

    `statuses` maps a task number to its status across the whole index, not just
    this direction. A start condition may name a task of another direction — and
    the one this repair was built on did: the wake-up asks about one thread and
    would otherwise have to call an unknown predecessor unobservable. Callers
    that have no catalogue get the directions's own tasks, which is what the
    tests and any single-thread reader already have.
    """
    busy = busy_repository_map(entries)
    known = dict(statuses or {})
    for entry in entries:
        known.setdefault(entry.get("id"), entry.get("status"))
    for entry, task in zip(entries, tasks):
        condition = start_condition(entry.get("status_detail"))
        evaluated = None
        if condition:
            evaluated = condition_state(condition, known, busy, entry.get("id"))
            entry["board"]["start_condition"] = evaluated
        why, why_src = queue_reason(task, entry["run"], busy, evaluated)
        # Место задачи в плане стоит своим полем, а не подменяет собой «за чем
        # стоит». Это разные утверждения: держатель — наблюдение (живой прогон в
        # том же дереве, незакрытая предшественница), а место в очереди — решение
        # владельца порядка. Складывать их в одно поле значило бы называть
        # держателем работу другого направления, которую тот же план разрешил
        # вести параллельно.
        place = plan_place_of(plan or {}, entry.get("id"))
        entry["board"]["plan_place"] = place
        entry["board"]["blocked_by"] = why
        entry["board"]["blocked_by_src"] = why_src
        hand = entry["detail"].get("handoff") or {}
        # A decision is unexecuted only while nothing observed it carried out.
        # The observation is the one the contour already writes for delivery —
        # `delivery.md` or a receipt carrying a message identifier — so the area
        # closes itself the moment the document actually goes out, and no second
        # notion of «исполнено» is invented here.
        decision = None
        if evaluated and evaluated["decision"] == "deliver":
            done = bool(hand) and bool(hand.get("delivered"))
            decision = {
                "kind": "deliver",
                "done": done,
                "src": (hand.get("delivered_src") if hand else
                        "документа для человека в каталоге задачи не наблюдается"),
            }
        entry["board"]["decision"] = decision
        entry["board"]["area"] = board_area(
            entry["status"], entry["flags"], bool(entry["asked_user"]), why,
            ours=bool(entry["our_questions"]),
            undelivered=bool(hand) and not hand.get("delivered"),
            # «Готово к запуску» requires a start condition, not merely a field:
            # a task carrying only a decision has named no obstacle, so it stands
            # where it always stood.
            ready=bool(evaluated) and evaluated["satisfied"]
            and bool(evaluated["after"] or evaluated["worktrees"]),
            decision_unmet=bool(decision) and not decision["done"],
            plan_role=place["role"] if place else None)
        # A queued task's reason for standing is the thing holding it, and the
        # plate has one place for «почему». `jam_reason` already filled it from
        # `status_detail` when there was one; a repository held by a named run
        # is the case it could not see.
        if not entry["board"]["why"] and why:
            entry["board"]["why"] = why
            entry["board"]["why_src"] = why_src


def board_head() -> str | None:
    """Короткий коммит рабочего дерева доски, или `None`, если git молчит."""
    out = subprocess.run(["git", "-C", str(HOME), "log", "-1", "--format=%h"],
                         capture_output=True, text=True)
    return out.stdout.strip() or None if out.returncode == 0 else None


# Ревизия, на которой стартовал этот процесс, прочитанная один раз при импорте:
# это единственный момент, отвечающий на «на чём работает служба».
# `product-owner-board.service` держит свой Python в памяти с запуска, а шаблон
# страницы читает с диска на каждый запрос, поэтому 14 августа полчаса
# показывалась страница нового шаблона на старом сборщике, и понять это можно
# было только расследованием в systemd (находка 8 задачи 1163).
RUNNING_ON = board_head()


def revision() -> dict:
    """На чём работает процесс и что лежит в дереве, каждое со своим источником."""
    return {"running": RUNNING_ON, "disk": board_head(),
            "src": "git log -1: первое — при старте процесса, собравшего снимок, "
                   "второе — в рабочем дереве в момент сборки"}


def repo_state(path: str) -> dict:
    repo = Path(path)
    if not (repo / ".git").exists():
        return {"name": repo.name, "present": False, "path": path}

    def git(*args: str) -> str:
        out = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True)
        return out.stdout.strip() if out.returncode == 0 else ""

    dirty = [line for line in git("status", "--porcelain").splitlines() if not line.startswith("??")]
    unpushed = git("rev-list", "--count", "@{u}..HEAD")
    return {
        "name": repo.name,
        "present": True,
        # The path the thread config names, kept so the one observer can answer
        # `thread_state`'s callers in their own terms. Structural anonymisation
        # replaces it like any other path on the way to a shown document.
        "path": path,
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "head": git("log", "-1", "--format=%h"),
        "head_subject": git("log", "-1", "--format=%s"),
        "head_at": git("log", "-1", "--format=%cI"),
        # Work that exists only as a working copy or only locally is invisible in
        # prose and is exactly what the map is meant to show.
        "tracked_dirty": len(dirty),
        "unpushed": int(unpushed) if unpushed.isdigit() else None,
    }


def count_lines(path: Path) -> int:
    """Records in an append-only journal, counted without holding it in memory."""
    total = 0
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                total += chunk.count(b"\n")
    except OSError:
        return 0
    return total


def thread_channels(tasks: list[dict], is_owner: bool) -> list[dict]:
    """Communication of one direction, counted from what is observable.

    Outgoing from the executors is Telegram — one notification receipt per
    message actually sent. The product owner's mail is incoming and outgoing on
    the contour as a whole, not on one direction, so it hangs on the direction
    that owns the contour rather than being spread over four panels by a
    connection nothing on disk supports.
    """
    # A receipt is written whether or not the message went, so counting lines
    # counted the 168 refusals of task 1255 as outgoing traffic. What the
    # sentence above promises is messages actually sent, and that is the receipt
    # carrying a message identifier.
    telegram = sum(
        1
        for task in tasks
        for row in _json_lines(REPO / "tasks" / task["dir"] / "dev-pipeline" /
                               "notification-receipts.jsonl")
        if row.get("message_id"))
    channels = [{"channel": "telegram", "direction": "out", "count": telegram}]
    if is_owner:
        for direction, box in (("in", "inbox"), ("out", "sent")):
            folder = MAIL_ROOT / box
            count = sum(1 for entry in folder.iterdir()
                        if (entry / "metadata.json").is_file()) if folder.is_dir() else 0
            channels.append({"channel": "email", "direction": direction, "count": count})
    return channels


def markdown_section(text: str, heading: str) -> list[str]:
    """Bullet lines of one `## heading` section, one entry per bullet."""
    items: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().lower() == heading.lower()
            continue
        if not inside:
            continue
        if line.startswith("- "):
            items.append(line[2:].strip())
        elif items and line.startswith("  ") and line.strip():
            items[-1] += " " + line.strip()
    return items


def products(catalogue: list[dict] | None = None, mail: dict | None = None) -> list[dict]:
    """Products, with their open questions split by who actually owes the answer.

    `## Открытые вопросы` of a product is a curated list: only questions are
    written into it, and it has its own closure convention — the product owner
    strikes a settled one through and writes when and by what it was closed. That
    convention is honoured, so a struck line is gone from both lists.

    What is *not* assumed any more is that an open product question is a question
    to the user. Most of them are ours: which of the findings of 706 become work,
    whether the `repo-health` gate is narrowed, whether a stopped migration is
    acceptable. They are decisions of the product owner, and putting them in
    front of the user is how the area came to hold sixteen entries of which three
    were his. `question_entry` owns the split for both places, so a task question
    and a product question are judged by one rule.
    """
    # An unobservable store is not a store without products. Printing an empty
    # products area here would tell the person «вопросов нет, обещаний нет» from
    # a reading that never happened — the same failure the live-run registry was
    # taught to refuse rather than answer with an empty list.
    if not product_memory.available():
        raise ContractError(
            f"долговечный корень продуктов {product_memory.root()} недоступен: "
            "продуктовая область не наблюдается и не может быть показана пустой")
    mail = mailbox() if mail is None else mail
    # The pool a promise is matched against is the whole catalogue, not the
    # tasks of one direction: a promise written in a product record may have
    # become a task under any project, and matching against a narrower pool
    # would report «связь не установлена» for a link that exists.
    catalogue = task_catalogue() if catalogue is None else catalogue
    entries = []
    for path in sorted(PRODUCTS.glob("*/snapshot.md")):
        try:
            text = path.read_text()
        except OSError:
            continue
        asked = questions_of(
            [q for q in markdown_section(text, "Открытые вопросы") if not q.startswith(STRUCK)],
            mail)
        entries.append({
            "slug": path.parent.name,
            "questions": owed_by(asked, "user"),
            # Ours, and shown as ours. Hiding them would trade one wrong answer
            # for another: they are real open decisions, and the person who owes
            # them is the product owner.
            "own_questions": owed_by(asked, "product"),
            "effect": markdown_section(text, "Журнал эффекта")[:8],
            "promises": unplanned(markdown_section(text, "В работе"), catalogue),
        })
    return entries


# ---------------------------------------------------------------------------
# «Надо запланировать»: which lines of a product record have a task behind them
# ---------------------------------------------------------------------------
#
# The area used to answer this with «в строке нет отдельно стоящего трёхзначного
# числа», and that признак cannot establish what the board printed above it
# (finding HIGH-1 of review 814). It was wrong in both directions at once. A
# product line is prose written by the product owner, and most three-digit
# numbers in it are quantities or fragments of something else — «630 строк»,
# «266 тестов», «411 фрагментов», the tail of a commit hash `2a8e061`, `SHA-256`,
# `127.0.0.1` — so a real promise «в наборе 394 теста» was suppressed by a число
# that never named a task. And a line naming an existing task in words rather
# than by number — «ревью кода клиента силами Claude», which is задача 713
# under almost that exact title — was printed as unplanned work, sending the
# person to plan work already planned.
#
# What replaces it is a link to a task that can be checked by hand, from two
# observations and nothing else: a число standing in a *reference position* that
# resolves to a task the index knows, and the *name* of a task standing in the
# line verbatim. When neither holds, the line is shown saying «связь с задачей не
# установлена» — never «задачи нет», which is a claim about the world that
# nothing here observed. `--plan-links` prints the whole judgement, line by line,
# with the evidence for each.

# Три цифры стояли здесь не как длина номера, а как признак: они заодно не
# пускали в ссылки годы и прочие четырёхзначные числа прозы. Номера задач дошли
# до 840 и придут к тысяче за считанные дни, и тогда «заведена задачей 1002»
# перестанет совпадать вовсе — строка исчезнет с табло молча, без единой ошибки.
#
# Расширить шаблон до «три-четыре» и оставить признак прежним нельзя: проверка
# на настоящих продуктовых записях (3323 строки) показала, что тогда ссылками
# становятся `$0,2758`, «убытке 7920», «(390 и 1440)», «(1056/1056, 1ч33м)» и
# «годы (2026)» — то есть ровно та ловушка, о которой предупреждала постановка,
# и не только с годами. Хуже, что 1056 и 1440 сами станут номерами задач через
# недели, и тогда счётчик прогонов молча превратится в ссылку на чужую задачу.
#
# Поэтому четырёхзначное число признаётся ссылкой не везде, где признавалось
# трёхзначное, а только там, где строка называет его задачей:
#   «заведена задачей 1002»          — слово-задача прямо перед ним;
#   «2026-08-07 — **1002 принята**»  — голова датированного утверждения;
#   «(1002 → 1005)»                  — стрелка к задаче, которая его заменила.
# Скобка с закрывающей пунктуацией — «(736)», «(805, 806, идут)» — остаётся
# признаком только для трёхзначного: в этой прозе четырёхзначное в скобках почти
# всегда количество, цена или год. Настоящее упоминание четырёхзначной задачи в
# такой позиции подхватывает не она, а сверка по словам названия ниже
# (`corroborated`), и подхватывает с напечатанным свидетельством.
NUMBER = re.compile(r"(?<![\w])(\d{3,4})(?![\w])")
LEGACY_WIDTH = 3

# Год внутри даты — не число строки, а часть отметки времени; `2026-08-07`
# начинает ровно ту же позицию, в которой пишется номер задачи. Дробная часть
# десятичного числа — тоже: `$0,2758` стоит в скобках с запятой после, то есть
# в позиции, которую разбор считает ссылочной. Обе проверки узкие нарочно.
# Год ловится только по полной ISO-дате, потому что широкая проверка «цифры,
# разделитель, цифры» съедала настоящую ссылку в «112/15/0» и в перечислении
# «242, 245, 249». Дробь — только когда целая часть в одну-две цифры, иначе
# «(805,806)» перестало бы быть перечислением номеров.
YEAR_TAIL = re.compile(r"^-\d{2}-\d{2}(?![\w])")
YEAR_HEAD = re.compile(r"\d{2}\.\d{2}\.$")
FRACTION_HEAD = re.compile(r"(?<!\d)\d{1,2}[.,]$")


def numbers(text: str) -> list[re.Match]:
    """Числа строки, кроме годов внутри дат и дробных частей десятичных чисел."""
    found = []
    for match in NUMBER.finditer(text):
        before, after = text[:match.start()], text[match.end():]
        if FRACTION_HEAD.search(before):
            continue
        if len(match.group(1)) == 4 and (YEAR_TAIL.match(after) or YEAR_HEAD.search(before)):
            continue
        found.append(match)
    return found


TASK_WORD = re.compile(r"(?:задач\w*|таск\w*|task|№)\s*$", re.IGNORECASE)
# Голова утверждения. Датированная принимает четырёхзначные номера, включая
# второй и третий в перечислении: после даты в этой позиции ничего, кроме
# номеров, не пишется. Недатированная остаётся трёхзначной с обеих сторон —
# иначе «1440 и 390 без переполнений» отдаёт и 1440, и существующую задачу 390.
CLAIM_HEAD = re.compile(r"^\s*(?:\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2})?\s*[—–-]*\s*"
                        r"[*`_«»\s]*(?:\d{3,4}[\s,и–—-]*)*"
                        r"|\s*[—–-]*\s*[*`_«»\s]*(?:\d{3}[\s,и–—-]*)*)$")
DATED_HEAD = re.compile(r"^\s*\d{4}-\d{2}-\d{2}")
# What may stand right after a reference: punctuation that closes or separates
# it, an arrow to the task that replaced it, or the end of the line. A letter or
# a digit there means the number is counting something.
AFTER_REFERENCE = re.compile(r"^\s*(?:[)\]},;.!?]|→|$)")
# Стрелка — «(805 → 808)» — называет задачу, заменившую другую, и потому годится
# для номера любой длины с обеих сторон.
ARROW_AFTER = re.compile(r"^\s*→")
ARROW_BEFORE = re.compile(r"→\s*$")
OPEN_BRACKET = re.compile(r"[(\[]")
CLOSE_BRACKET = re.compile(r"[)\]]")

WORDS = re.compile(r"[^0-9a-zа-я]+")


def normal(text: str) -> str:
    """One spelling of a phrase, so a comparison is about words and not markup."""
    return WORDS.sub(" ", text.lower().replace("ё", "е")).strip()


def inside_brackets(text: str, position: int) -> bool:
    """Whether an offset stands inside a bracket that opened before it."""
    opened = OPEN_BRACKET.search(text[:position][::-1])
    closed = CLOSE_BRACKET.search(text[:position][::-1])
    if not opened:
        return False
    return not closed or opened.start() < closed.start()


def task_references(item: str) -> list[int]:
    """Numbers of the line that are written as references to a task."""
    found: list[int] = []
    for match in numbers(item):
        before, after = item[:match.start()], item[match.end():]
        # Трёхзначное число этот контур пишет как номер задачи в любой из трёх
        # позиций. Четырёхзначное — только там, где строка называет его задачей:
        # в прозе, где считают вызовы, тесты, пиксели и рубли, четырёхзначных
        # количеств столько, что позиция сама по себе перестаёт быть признаком.
        narrow = len(match.group(1)) <= LEGACY_WIDTH
        if TASK_WORD.search(before):
            reference = True
        elif CLAIM_HEAD.match(before):
            reference = narrow or bool(DATED_HEAD.match(before))
        elif inside_brackets(item, match.start()) and AFTER_REFERENCE.match(after):
            reference = narrow or bool(ARROW_AFTER.match(after)
                                       or ARROW_BEFORE.search(before))
        else:
            reference = False
        if reference:
            found.append(int(match.group(1)))
    return found


def stems(text: str) -> set[str]:
    """Significant words of a phrase, cut to a stem so a case ending is not a wall.

    «закрывает» and «закрыть», «канон» and «каноне» are the same word for this
    purpose, and the contour writes its records in Russian. Five letters is the
    cut: short enough to survive declension, long enough that the match is still
    about a word and not about a syllable.
    """
    return {word[:5] for word in normal(text).split() if len(word) >= 5}


# How much of a task's own name has to stand in the line before a число in it is
# read as naming that task. Half, and never fewer than two words: one shared word
# is a coincidence between a contour that names its own delivery bot in every second line and
# a catalogue of 710 tasks, and a coincidence is exactly what must not close this
# question.
CORROBORATION = 0.5


def corroborated(item: str, task: dict) -> list[str]:
    """The words of a task's name that also stand in the line, when enough do."""
    named = stems(task.get("title") or "")
    if len(named) < 2:
        return []
    shared = sorted(named & stems(item))
    if len(shared) < 2 or len(shared) / len(named) < CORROBORATION:
        return []
    return shared


def title_lead(title: str) -> str:
    """The part of a task title a person repeats when naming the task in prose.

    Titles in this contour carry a qualifier after a colon or a dash — задача 713
    is «Ревью кода клиента силами Claude: старый код и кандидаты на
    рефакторинг» — and the product record names the task by the part in front of
    it. Matching the whole title would find nothing; matching any fragment would
    find everything, so the lead has to be long enough to be a name.
    """
    lead = normal(re.split(r"[:(—]", title)[0])
    return lead if len(lead) >= 15 and len(lead.split()) >= 3 else ""


def promise_link(item: str, catalogue: list[dict]) -> dict | None:
    """The task behind a product line, when one can be observed. Never guessed.

    Two observations, in the order a person would make them: the line references
    a task number that the index resolves, or the line carries a task's name (or
    its directory slug) verbatim. Both are checkable with one command against the
    same index the board reads.
    """
    known = {task.get("id"): task for task in catalogue if task.get("id")}
    for number in task_references(item):
        task = known.get(number)
        if task:
            return {"task": number, "title": task.get("title"),
                    "how": f"номер {number} стоит в строке как ссылка на задачу "
                           f"и найден в каталоге задач"}
    line = normal(item)
    for task in catalogue:
        lead = title_lead(task.get("title") or "")
        if lead and lead in line:
            return {"task": task.get("id"), "title": task.get("title"),
                    "how": f"название задачи {task.get('id')} дословно стоит в строке: «{lead}»"}
        slug = normal(re.sub(r"^\d+-", "", task.get("slug") or "")).replace(" ", "-")
        if len(slug) >= 12 and slug.count("-") >= 1 and slug in item.lower():
            return {"task": task.get("id"), "title": task.get("title"),
                    "how": f"слаг задачи {task.get('id')} «{slug}» стоит в строке"}
    # A число that stands where the contour writes a quantity — «(811 идёт, 812
    # ждёт её)» reads exactly like «(266 тестов)» — still names a task when the
    # line also carries that task's own words. The число alone never decides
    # anything here: the words are the observation and they are printed with it.
    for number in {int(match.group(1)) for match in numbers(item)}:
        task = known.get(number)
        shared = corroborated(item, task) if task else []
        if shared:
            return {"task": number, "title": task.get("title"),
                    "how": f"число {number} стоит в строке вместе со словами названия "
                           f"задачи {number}: {', '.join(shared)}"}
    return None


def unplanned(items: list[str], catalogue: list[dict]) -> list[dict]:
    """Lines of «В работе» with no observable task behind them.

    This is the fourth question of the board, and it has a price already paid:
    «ревью кода клиента силами Claude… Запрошено пользователем» stood in this
    section for two days and never became a task, because the flow had no place
    for «надо запланировать».

    What the area may claim is bounded by what was compared. «Связь с задачей не
    установлена» is the honest reading of a failed comparison; «задачи нет» is
    not, and a board that invents work costs the person more than an empty area
    does. So every line carries what was checked against it, and the numbers that
    were in it but named no task are carried with it — that is where a wrong
    entry is caught by reading, and it is the only place where it can be.
    """
    shown = []
    for item in items:
        if item.startswith(STRUCK):
            continue
        if promise_link(item, catalogue):
            continue
        numbers = [str(n) for n in task_references(item)]
        checked = (f"сверено с каталогом задач ({len(catalogue)} задач) "
                   "по номеру-ссылке, по названию и по слагу")
        if numbers:
            checked += f"; номера-ссылки {', '.join(numbers)} в каталоге не найдены"
        shown.append({"text": item, "link": "unknown", "checked": checked})
    return shown


# ---------------------------------------------------------------------------
# «Что в очереди» и «что в бэклоге»: и то и другое читается у владельца — плана
# ---------------------------------------------------------------------------
#
# Наблюдение не знает очереди и знать не может. `planned` — это статус, а не
# место в очереди, и план говорит это сам последней своей строкой: «не является
# очередью: все прочие задачи со статусом planned». Пока доска выводила очередь
# из наблюдения, она показывала свой порядок вместо установленного: 13 августа у
# Продукт под «в очереди» стояли 853 и 1136, тогда как действующая редакция 31
# ставила 1121 → 1136 → 1138, а 1121 и 1138 лежали под «можно подхватить». И
# целого куска картины — работы, которая заведена, но сама не поедет, — на доске
# не было вовсе.
#
# Поэтому и порядок, и паузы читаются у их владельца: `product_memory`
# .current_plan(). Второго источника правды не заводится — ни своего файла, ни
# нового поля задачи, ни новой стадии жизненного цикла; доска остаётся читателем.

# Строка плана, которая сама говорит, что очередью не является. План пишет это
# своими словами и в тех же полях («1054 и 1067 — на полке по слову пользователя,
# очередью не являются», «1152 и переделка памяти остаются бэклогом»), поэтому
# признак — слова строки, и он печатается рядом с ней: читатель видит, чем
# запись отнесена в бэклог, и может не поверить.
NOT_A_QUEUE = re.compile(r"очеред\w*\s+не\s+явля\w+|не\s+явля\w+\s+очеред\w+|бэклог\w*",
                         re.IGNORECASE)


# Предмет строки плана: номера, которыми строка начинается, до тире. Признак
# «любое число строки, которое знает каталог» был снят кругом 1 независимого
# ревью, и снят, а не сужен: число-ссылка и предмет утверждения им не
# различались, и строка «Клиент — 1152 … порядок 1150 → 1151 → 1152»
# записывала в паузу 1150 и 1151, а строка пауз «исследование 1150 и 1151
# разрешено» ставила разрешённую работу под заголовок паузы и убирала её из
# запуска. Ложная классификация области — это ровно то, чего доска делать не
# имеет права, и никакая печать строки рядом её не отменяет.
#
# Круг 2 попробовал вернуть широкое чтение одной стороной — «держащая строка
# держит любой названный ею номер», — и круг 3 показал на живом снимке, чего это
# стоит: разрешающая строка «исследование 1150 и 1151 разрешено» объявляла
# остановленной прямо разрешённую пользователем работу. Это тот же дефект по
# другой ветви, поэтому правило снова единое для всех областей: роль получает
# только предмет строки. Страховка от неудачной формулировки принадлежит плану,
# и он её получил редакцией 34 — одна строка плана называет одну задачу.
#
# Позиция читается однозначно и только она: пункт начинается с номера (или
# перечисления номеров через «и», запятую или косую), дальше до тире может стоять
# уточнение, но не второе число. Всё остальное в строке — просто текст, и строка
# показывается без машинной привязки задач вовсе. «1121 — первый живой запуск…»
# и «1125 и 1134 — довести…» читаются, «1082 engine-круг — …» читается, а «754 —
# окончание прогона…; 1090 больше не держит» отдаёт 754 и не отдаёт 1090.
PLAN_SUBJECT = re.compile(r"^\**\s*(\d{3,4}(?:\s*(?:и|,|/)\s*\d{3,4})*)[^—–\d]*[—–]")


def plan_line_numbers(item: str, known: dict) -> list[int]:
    """Все номера задач, названные строкой плана, — предмет и ссылки вместе."""
    # Номер задачи в прозе не пишут с ведущим нулём, а идентификатор цели пишут:
    # «первое условие цели 0002» дало бы ссылку на задачу 2.
    found = dict.fromkeys(int(match.group(1)) for match in numbers(item)
                          if not match.group(1).startswith("0"))
    return [number for number in found if number in known]


def plan_line_tasks(item: str, known: dict) -> list[int]:
    """Задачи, о которых строка плана говорит как о своём предмете.

    Пусто — обычный и честный ответ: строка плана про статью, про слова
    пользователя или про направление целиком предмета-задачи не имеет, и такая
    строка стоит на доске своим текстом, без задач за ней.
    """
    head = PLAN_SUBJECT.match(item)
    if not head:
        return []
    return plan_line_numbers(head.group(1), known)


def plan_line_mentions(item: str, known: dict) -> list[int]:
    """Задачи, названные строкой, но не стоящие её предметом.

    Упоминание ничего не решает и решать не может: ни области, ни порядка, ни
    автоматики запуска. Оно существует ровно затем, чтобы доска не сказала «план
    о ней не говорит» про задачу, которую план называет, — и печатается рядом со
    строкой, в которой названо. Круг 3 независимого ревью показал цену обратного:
    когда упоминание в держащей строке снимало работу с запуска, разрешающая
    строка «исследование 1150 и 1151 разрешено» объявляла эту работу
    остановленной.
    """
    subject = set(plan_line_tasks(item, known))
    return [number for number in plan_line_numbers(item, known)
            if number not in subject]


def plan_entry(field: str, text: str, known: dict, kind: str | None = None) -> dict:
    """Одна строка плана в том виде, в котором её показывает доска."""
    ids = plan_line_tasks(text, known)
    also = plan_line_mentions(text, known)
    checked = ("предметом строки считаются только номера в её начале, до тире, и "
               f"сверены они с каталогом задач ({len(known)} задач); "
               + (f"каталог знает как задачи: {', '.join(str(i) for i in ids)}"
                  if ids else "строка начинается не с номера задачи, "
                              "поэтому задачи за ней не названы")
               + (f"; названы в строке, но не её предметом: "
                  f"{', '.join(str(i) for i in also)}" if also else ""))

    def plate(number: int) -> dict:
        return {"id": number, "title": known[number].get("title"),
                "status": known[number].get("status")}

    entry = {
        "field": field,
        "text": text,
        "tasks": [plate(number) for number in ids],
        # Номера, названные строкой не в качестве предмета. Показываются отдельно
        # и названы отдельно: «1156 — идёт прямо сейчас; после неё 1054, затем
        # 1067» говорит о 1054 и 1067, но места в очереди прозой не назначает, и
        # доска не имеет права ни сказать «план о них не говорит», ни вывести из
        # этой фразы порядок.
        "also": [plate(number) for number in also],
        "checked": checked,
    }
    if kind:
        entry["kind"] = kind
    return entry


PLAN_HEADING = re.compile(r"^\s*\*\*(.+?)\*\*")


def plan_outcomes(plan: dict, known: dict) -> list[dict]:
    """Results named by ``now`` and only explicitly related ``next`` lines.

    ``outcome_links`` is part of the same immutable current-plan revision.  It
    contains one-based ``now`` and ``next`` line indexes, so the board never
    guesses a result relation from similar prose or a coincidental task number.
    """
    next_lines = list(plan.get("next") or [])
    try:
        links = product_memory.validate_outcome_links(plan)
    except product_memory.ContentError as error:
        raise ContractError(str(error)) from error
    for relation in links.values():
        if any(value not in known for value in relation["tasks"]):
            raise ContractError("план: outcome_links содержит неизвестные tasks")

    outcomes = []
    for index, text in enumerate(plan.get("now") or [], 1):
        relation = links.get(index, {"next": [], "tasks": [], "goals": []})
        ids = relation["tasks"]
        heading = PLAN_HEADING.search(text)
        title = heading.group(1).strip() if heading else " ".join(text.split())[:120]
        positions = relation["next"]
        transitions = [next_lines[position - 1] for position in positions
                       if isinstance(position, int) and 1 <= position <= len(next_lines)]
        outcomes.append({
            "title": title,
            "text": text,
            "tasks": [{"id": number, "title": known[number].get("title"),
                       "status": known[number].get("status")} for number in ids],
            "goals": relation["goals"],
            "next": transitions,
            "checked": ((f"строка now {index} и явно связанные outcome_links "
                         f"строки next {positions} действующей редакции; "
                         if index in links else
                         f"строка now {index}; явной связи outcome_links в редакции нет; ")
                        + f"номера сверены с каталогом задач ({len(known)} задач)"),
        })
    return outcomes


def plan_projection(catalogue: list[dict] | None = None) -> dict:
    """Очередь, бэклог и место каждой задачи — по действующей редакции плана.

    Три поля редакции читаются как три разных утверждения владельца порядка:
    `next` — очередь, `paused` — то, что стоит и само не поедет, всё остальное —
    упоминание без места в очереди. Задача, которую редакция не называет вовсе,
    получает `unnamed`, и это утверждение ровно о прочитанной редакции: план
    прочитан, и этой задачи в нём нет. Вывода о самой задаче отсюда не делается —
    ни что она не разобрана, ни что её нельзя запускать.

    Чтение предмета несимметрично, и это решение владельца плана по кругу 2
    независимого ревью. Строку каждой редакции пишет человек свободной прозой, и
    цена ошибки в две стороны разная. Ложная связь в очереди переставляет работы,
    поэтому там предмет читается строго — только из явной позиции в начале
    пункта. Ложное срабатывание в паузе не стоит ничего: задача не показывается
    доступной к подхвату, а места в очереди не теряет, потому что очередь ведёт
    план. Поэтому любой номер, названный строкой, которая держит работу, с
    «можно подхватить» снимается — иначе следующая формулировка снова отправит в
    автозапуск работу, которую пользователь остановил (это и случилось с 1152:
    номер стоял после слова «Клиент»).

    Противоречие внутри самой редакции не решается молча. Если один номер назван
    и очередью, и держащей строкой, побеждает очередь — она конкретнее, — а на
    месте задачи остаётся запись `conflict`, и доска говорит рядом, что об этой
    работе план сказал двояко. Молча выбранная строка однажды уже показала
    снятую пользователем работу как бэклог.
    """
    if not product_memory.available():
        # Недоступное хранилище — не пустой план. Показать пустую очередь значило
        # бы сказать «порядок не установлен» по чтению, которого не было.
        raise ContractError(
            f"долговечный корень продуктов {product_memory.root()} недоступен: "
            "порядок работ не наблюдается и не может быть показан пустым")
    catalogue = task_catalogue() if catalogue is None else catalogue
    known = {task["id"]: task for task in catalogue if task.get("id")}
    plan = product_memory.current_plan()
    if plan is None:
        # «Плана нет» — честный ответ и единственный доступный: очередь не
        # выводится из статусов, и молчание владельца порядка не заменяется
        # догадкой. Места задач остаются пустыми, и доска про очередь молчит.
        return {"revision": None, "accepted_at": None, "outcomes": [],
                "queue": [], "backlog": [],
                "places": {}, "conflicts": [],
                "src": f"редакций плана нет в {product_memory.revisions_dir()}"}
    revisions = product_memory.plan_revisions()
    source = str(revisions[-1]) if revisions else str(product_memory.revisions_dir())
    revision = plan.get("revision")
    src = f"действующая редакция {revision} портфельного плана, файл {source}"

    outcomes = plan_outcomes(plan, known)
    queue: list[dict] = []
    backlog: list[dict] = []
    for text in plan.get("next") or []:
        if NOT_A_QUEUE.search(text):
            backlog.append(plan_entry("next", text, known, kind="paused"))
        else:
            queue.append(plan_entry("next", text, known))
    for text in plan.get("paused") or []:
        backlog.append(plan_entry("paused", text, known, kind="paused"))

    # Место каждой задачи, по одному разу и по самому конкретному утверждению
    # плана о ней: стоять в очереди конкретнее, чем стоять на паузе, а быть
    # предметом строки — конкретнее, чем быть в ней упомянутой. Место назначает
    # только предмет строки, в любой области и в любом поле; упоминание места не
    # назначает нигде.
    places: dict[int, dict] = {}
    conflicts: list[dict] = []

    def place(number: int, role: str, line: str | None, field: str | None,
              detail: str, position: int | None = None) -> None:
        seen = places.get(number)
        if seen:
            # Побеждает более конкретное утверждение — то, что записано первым.
            # Но если план в одной редакции и ставит работу в очередь, и держит
            # её, читатель обязан увидеть обе строки: молчаливый выбор одной из
            # двух несовместимых строк однажды показал 1054 и 1067 как бэклог,
            # хотя пользователь только что снял их с полки. Противоречие — это
            # две строки, каждая из которых говорит об этой задаче как о своём
            # предмете; упоминание ни с чем не спорит.
            if seen["role"] == "queue" and role == "paused":
                seen["conflict"].append({"role": role, "line": line, "field": field,
                                         "src": f"{detail}; {src}"})
                conflicts.append({"id": number, "kept": seen["role"], "also": role,
                                  "line": line, "field": field})
            return
        places[number] = {"role": role, "position": position, "line": line,
                          "field": field, "ahead": [], "conflict": [],
                          "src": f"{detail}; {src}"}

    for position, entry in enumerate(queue, start=1):
        for task in entry["tasks"]:
            place(task["id"], "queue", entry["text"], entry["field"],
                  f"строка {position} очереди (поле next)", position)
    for entry in backlog:
        for task in entry["tasks"]:
            place(task["id"], "paused", entry["text"], entry["field"],
                  f"строка поля {entry['field']}, которая держит работу")

    def elsewhere():
        """Строки редакции, которые ни очередью, ни бэклогом не являются."""
        for field in ("headline", "now", "parallel", "grounds", "contradictions"):
            values = plan.get(field) or []
            for text in ([values] if isinstance(values, str) else values):
                yield field, text

    # Два прохода, а не один: предмет строки конкретнее упоминания в другой
    # строке, и одним проходом задача получала бы то из двух, что встретилось
    # раньше по полям.
    for field, text in elsewhere():
        for number in plan_line_tasks(text, known):
            place(number, "named", text, field,
                  f"задача названа в поле {field}, но в очередь не поставлена")
    # Просто упоминание: план эту задачу называет, а места ей не даёт. Читается
    # одинаково во всех полях и во всех областях, потому что ничего не решает —
    # ни очереди, ни бэклога, ни автоматики запуска. Существует затем, чтобы
    # доска не сказала «план о ней не говорит» про задачу, которую план называет:
    # держащая строка «Клиент — … исследование 1150 и 1151 разрешено» называет
    # обе, и правдой о ней будет «названа», а не «остановлена» и не «не названа».
    for entry in queue + backlog:
        for task in entry["also"]:
            place(task["id"], "mentioned", entry["text"], entry["field"],
                  f"задача названа в строке поля {entry['field']}, но предметом "
                  "строки не стоит: места ей эта строка не назначает")
    for field, text in elsewhere():
        for number in plan_line_mentions(text, known):
            place(number, "mentioned", text, field,
                  f"задача названа в строке поля {field}, но предметом строки "
                  "не стоит: места ей эта строка не назначает")

    # За чем стоит задача очереди — это незакрытые задачи, которые план поставил
    # строго перед ней. Считается по местам, а не по строкам: одна строка
    # называет несколько номеров, один номер встречается в нескольких строках, и
    # по строкам задача попадала в собственный список стоящих перед ней. Закрытая
    # предшественница никого не держит: показывать её значило бы показывать
    # очередь, которой уже нет.
    living = sorted((number for number, place in places.items()
                     if place["role"] == "queue"
                     and known[number].get("status") not in TERMINAL),
                    key=lambda number: places[number]["position"])
    for number in living:
        places[number]["ahead"] = [other for other in living
                                   if places[other]["position"] < places[number]["position"]]
    return {"revision": revision, "accepted_at": plan.get("accepted_at"),
            "outcomes": outcomes, "queue": queue, "backlog": backlog, "places": places,
            "conflicts": conflicts, "src": src}


def plan_place_of(projection: dict, number: int | None) -> dict | None:
    """Что действующая редакция плана говорит об одной задаче.

    `None` — редакции нет вовсе. Это не то же самое, что `unnamed`: первое —
    молчание владельца порядка, второе — прочитанная редакция, которая эту задачу
    не называет. Ни то ни другое ничего не говорит о том, разобрана задача или
    нет, и ни то ни другое не решает, можно ли её запускать.
    """
    if projection.get("revision") is None:
        return None
    place = (projection.get("places") or {}).get(number)
    if place:
        return place
    return {"role": "unnamed", "position": None, "line": None, "field": None,
            "ahead": [], "conflict": [],
            "src": "редакция не называет эту задачу ни в одном поле; "
                   + projection["src"]}


def ticked_thread(cmdline: list[str]) -> str | None:
    """The direction a wake-up process is working on, from its own argv.

    The tick's own arguments, not a command line that merely mentions it. A
    `bash -c` wrapper carries the whole command in one argument, so searching
    every argument for the substring matched the shell instead and put
    «/bin/bash» on the strip as the name of a direction — found by driving the
    page against the real process table, not by reading this code.

    The script has to be an argument of its own, and the thread is the argument
    after it, which is exactly what `product-thread@<тред>.service` passes.
    """
    for index, part in enumerate(cmdline):
        if part.endswith("thread_tick.py"):
            return next((later for later in cmdline[index + 1:]
                         if later and not later.startswith("-")), None)
    return None


# The CLI binaries an interactive product owner is driven by. Matched on the
# executable argument alone, exactly like `ticked_thread` matches the script: a
# `bash -c` wrapper carries the whole command in one argument and would otherwise
# put «/bin/bash» on the strip as the name of an owner.
OWNER_CLIS = ("claude", "codex", "cursor-agent")
OWNER_PROMPT_MARKER = "Ты работаешь как самостоятельный продакт-владелец"


def runs_the_tick(cmdline: list[str], executable: Path | None) -> bool:
    """Whether this process *is* a tick, rather than one that mentions it.

    Two observations, because argv alone is not enough. `ticked_thread` requires
    the script to be an argument of its own, which already rules out a `bash -c`
    wrapper carrying the whole command in one string — but not `timeout 600
    python3 scripts/thread_tick.py process`, where the script *is* an argument of
    its own and the process is `timeout`. That wrapper was observed being counted
    as a second owner on 2026-08-07, so the executable has to be a Python
    interpreter as well. The unit runs `/usr/bin/python3 …/thread_tick.py %i`, so
    the real tick passes both.
    """
    if ticked_thread(cmdline) is None:
        return False
    return bool(executable) and executable.name.startswith("python")


def owner_cli(cmdline: list[str]) -> str | None:
    """The owner CLI named by argv, tolerating a Node launcher."""
    for part in cmdline[:2]:
        if part and Path(part).name in OWNER_CLIS:
            return Path(part).name
    return None


def ancestor_cmdlines(entry: Path, limit: int = 32) -> list[list[str]]:
    """Observable command lines from this process's parent chain."""
    ancestors = []
    seen = {entry.name}
    current = entry
    for _ in range(limit):
        try:
            # Field two may contain spaces inside parentheses. Everything after
            # the final `) ` starts with state and then PPID.
            fields = (current / "stat").read_text().rsplit(") ", 1)[1].split()
            parent = fields[1]
        except (OSError, IndexError):
            break
        if parent == "0" or parent in seen:
            break
        seen.add(parent)
        current = PROC / parent
        try:
            command = (current / "cmdline").read_bytes().decode(
                "utf-8", "replace").split("\0")
        except OSError:
            break
        ancestors.append(command)
    return ancestors


def process_started(entry: Path) -> float:
    """Kernel process start tick converted with the observed boot time.

    `/proc/<pid>` mtime is not a process start clock: on the live 692 probe it
    was four days newer than `ps`'s start time. Field 22 of stat is the identity
    tick the runner also trusts, and `/proc/stat` supplies the boot epoch.
    """
    try:
        fields = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
        start_ticks = int(fields[19])
        boot = next(int(line.split()[1]) for line in (PROC / "stat").read_text().splitlines()
                    if line.startswith("btime "))
        return boot + start_ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, IndexError, StopIteration, ValueError):
        return entry.stat().st_mtime


def command_has(cmdline: list[str], name: str) -> bool:
    """A script is an argv item of its own, never a shell-command substring."""
    return any(part and Path(part).name == name for part in cmdline)


def owner_tree(cmdline: list[str], cwd: Path | None) -> Path | None:
    """The product-owner tree observed as cwd or as the CLI's cwd option."""
    if cwd == HOME:
        return HOME
    for index, part in enumerate(cmdline[:-1]):
        if part in ("-C", "--cd") and Path(cmdline[index + 1]) == HOME:
            return HOME
    return None


def session_owner(cmdline: list[str], cwd: Path | None,
                  ancestors: list[list[str]] | None = None) -> str | None:
    """Which kind of non-tick product owner this process is, if any.

    The console session is the *other* instance of the two that created 790/792
    and 791/793 in one hour, and it was never observed: `ticked_thread` matches
    the tick script and an interactive `claude` never runs it. That is the whole
    of why «Другой я» has been empty on every board the user has opened — the
    only thing it could ever match lives for a second or two per twenty minutes,
    between two timer firings nobody is looking at.

    Two observations, both from `/proc` and neither from anything the process
    says about itself: the executable is one of the owner's CLIs, and its working
    directory is the product owner's own tree. A child run started *by* a task
    sits in its task's repository and is a run, not an owner; it is already on
    the board as a live run and must not be counted twice.

    `--print` splits the two that remain. A tick runs its owner agent
    non-interactively from the same directory, so calling that «продакт в
    консоли» would be a caption that names the wrong thing — and this board is
    built on not doing that.
    """
    if owner_tree(cmdline, cwd) is None:
        return None
    cli = owner_cli(cmdline)
    if cli is None:
        return None
    ancestors = ancestors or []
    if any(command_has(command, "mail_product_owner.py") for command in ancestors):
        return "mail"
    if any(command_has(command, "thread_tick.py") for command in ancestors):
        return "woken"
    # A repository-local Codex `exec` can be a development child whose cwd is
    # HOME. Unlike Claude's explicit --name, that is not enough to call it a
    # product owner. The installed product-owner launcher puts this stable role
    # marker in an interactive Codex argv; observing it avoids counting task 839
    # itself as another product owner.
    named = any(part in ("product-owner", "product-owner-background")
                for part in cmdline)
    prompted = any(OWNER_PROMPT_MARKER in part for part in cmdline)
    if not (named or prompted):
        return None
    return "session"


def thread_worktrees(config: dict) -> dict[str, list[str]]:
    """Which repositories each direction owns, from the one configuration file."""
    return {key: list(thread.get("repos", []))
            for key, thread in config.get("threads", {}).items()}


def owner_cpu_seconds(entry: Path) -> float | None:
    """CPU this process has actually burned, from the kernel's own counters.

    Fields 14 and 15 of `stat` are user and system time in clock ticks. They are
    the one observation that tells a product owner *deciding something* from a
    terminal window somebody left open: a session talking to a model burns CPU
    every few seconds, and a sleeping one burns none at all.
    """
    try:
        fields = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    except (OSError, IndexError, ValueError):
        return None


def owner_activity(owner: dict, previous: dict) -> dict:
    """Whether this awake owner is deciding, or is a window left open.

    Yielding to another owner is correct while that owner may put a child into
    the same working tree. It is not correct forever: a terminal the user walked
    away from would otherwise hold a background goal hostage for as long as the
    process exists, which is precisely the thing the user asked to stop needing
    to watch — «мне приходилось следить за доступностью терминала».

    Two observations of the same process, one wake-up apart, and the difference
    between them. Absent a previous observation the honest answer is «активность
    ещё не измерена», and that counts as active: the first sighting of a real
    owner must not be waved through.
    """
    cpu = owner_cpu_seconds(PROC / str(owner["pid"]))
    seen = previous.get(f"{owner['pid']}:{owner['since']}")
    if cpu is None:
        return {"cpu_seconds": None, "cpu_delta": None, "measured_over": None,
                "active": True,
                "src": "счётчики процессорного времени процесса в /proc недоступны — "
                       "бодрствующий продакт считается работающим, пока не наблюдено обратное"}
    if not seen or seen.get("cpu_seconds") is None:
        return {"cpu_seconds": cpu, "cpu_delta": None, "measured_over": None,
                "active": True,
                "src": "процессорное время процесса наблюдено впервые: "
                       "разницы ещё нет, поэтому продакт считается работающим"}
    try:
        span = int((datetime.now(timezone.utc)
                    - datetime.fromisoformat(seen["observed_at"])).total_seconds())
    except (KeyError, TypeError, ValueError):
        span = None
    delta = round(cpu - float(seen["cpu_seconds"]), 3)
    working = delta >= OWNER_CPU_TICK_SECONDS
    # A window shorter than the judging threshold says nothing about the world:
    # «не двигалось за минуту» is a statement about the minute. Such a sighting
    # keeps the right of way until it can answer.
    too_short = span is None or span < OWNER_IDLE_SECONDS
    if working:
        source = (f"процессорное время процесса в /proc выросло на {delta} с за "
                  f"{span} с наблюдения")
    elif too_short:
        source = (f"процессорное время процесса в /proc выросло на {delta} с, но "
                  f"наблюдение идёт всего {span} с — короче порога суждения, "
                  "поэтому продакт считается работающим")
    else:
        source = (f"процессорное время процесса в /proc не двигалось {span} с "
                  f"(рост {delta} с): решение в нём не принимается")
    return {
        "cpu_seconds": cpu, "cpu_delta": delta, "measured_over": span,
        "active": working or too_short,
        "src": source,
    }


def owner_observations(path: Path | None = None) -> dict:
    """The previous sighting of every awake owner, or nothing."""
    record = read_json(path or OWNER_STATE)
    seen = record.get("owners")
    return seen if isinstance(seen, dict) else {}


def write_owner_observations(owners: list[dict], path: Path | None = None) -> None:
    """Persist this sighting, and only from the process that is the wake-up.

    The board is rebuilt every few seconds in live mode, so if it wrote here the
    difference two observations apart would be seconds of wall clock and every
    owner would read as idle. The tick is the observer paired with the timer, so
    the span between two of its records is one wake-up interval — long enough for
    «этот процесс ничего не делает» to be a statement about the world.
    """
    path = path or OWNER_STATE
    now = datetime.now(timezone.utc)
    moment = now.isoformat()
    previous = owner_observations(path)
    seen = {}
    for owner in owners:
        key = f"{owner['pid']}:{owner['since']}"
        old = previous.get(key)
        # A baseline younger than the judging window is kept rather than
        # refreshed. Four directions tick five minutes apart and share this
        # file, so overwriting on every one of them would leave every span
        # shorter than the window and «ничего не делает» could never be said at
        # all. The baseline ages until it can answer, then starts again.
        if old and old.get("cpu_seconds") is not None:
            try:
                age = (now - datetime.fromisoformat(old["observed_at"])).total_seconds()
            except (KeyError, TypeError, ValueError):
                age = None
            if age is not None and age < OWNER_IDLE_SECONDS:
                seen[key] = old
                continue
        seen[key] = {"cpu_seconds": (owner.get("activity") or {}).get("cpu_seconds"),
                     "observed_at": moment}
    record = {"schema_version": 1, "observed_at": moment, "owners": seen}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2))
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def owner_wakeups(config: dict | None = None) -> list[dict]:
    """Other instances of the product owner that are awake right now.

    Named by the user as the sixth question after two pairs of duplicate tasks
    (790/792 and 791/793) were created in one hour: the product owner in the
    chat and the product owner woken by `product-thread@<тред>.timer` each had
    their own queue and neither could see the other's. A live tick is one such
    instance; an interactive session in the owner's own tree is the other, and
    it used not to be observed at all.

    Every entry carries the working trees it could occupy, because that is the
    only thing yielding is actually about. Four timers fired in the same second
    on 2026-08-07 and three of the four directions stood down for a neighbour
    that could not have collided with them: `client` yielded to `process`,
    `platform` to `client` and `process`, `product` to all three, and the
    four directions own four disjoint sets of repositories.

    Read from `/proc` command lines, process ancestry and working trees only — no
    transcript, no session file, and nothing the other instance says about
    itself.
    """
    owned = thread_worktrees(config if config is not None else load_config())
    previous = owner_observations()
    awake = []
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().decode("utf-8", "replace").split("\0")
        except OSError:
            continue
        try:
            cwd = Path(os.readlink(entry / "cwd"))
        except OSError:
            cwd = None
        try:
            executable = Path(os.readlink(entry / "exe"))
        except OSError:
            executable = None
        thread = ticked_thread(cmdline)
        if runs_the_tick(cmdline, executable):
            kind, worktrees = "tick", owned.get(thread, [])
            src = "командная строка и исполняемый файл процесса в /proc"
        elif owner_cli(cmdline) and owner_tree(cmdline, cwd):
            ancestors = ancestor_cmdlines(entry)
            kind = session_owner(cmdline, cwd, ancestors)
            if kind is None:
                continue
            # A Codex launcher and its native child are one owner instance. Keep
            # the highest observable CLI process and do not turn one window into
            # a duplicate warning about itself.
            if any(owner_cli(parent) for parent in ancestors):
                continue
            # The tick process is already the observable timer-wakeup instance.
            # Its CLI child is implementation detail, not a fourth owner.
            if kind == "woken":
                continue
            # Neither a console owner nor a woken one declares a direction, so
            # what it could occupy is what it is standing in and nothing wider.
            # Guessing «все деревья» here would put every tick back to yielding
            # to every chat window.
            thread, worktrees = None, [str(owner_tree(cmdline, cwd))]
            src = "командная строка, цепочка родителей и рабочее дерево процесса в /proc"
        else:
            continue
        try:
            started = process_started(entry)
        except OSError:
            continue
        owner = {
            "pid": int(entry.name),
            "kind": kind,
            "thread": thread,
            "worktrees": worktrees,
            "since": datetime.fromtimestamp(started, timezone.utc).isoformat(),
            "age_seconds": max(int(time.time() - started), 0),
            "src": src,
        }
        # Being awake and deciding something are two different states, and only
        # the second one is a reason for a background goal to stand still.
        owner["activity"] = owner_activity(owner, previous)
        awake.append(owner)
    return sorted(awake, key=lambda w: w["since"])


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _observed_path(value: str) -> Path | None:
    if not value or not value.startswith("/"):
        return None
    # Linux appends this suffix to fd links after unlinking the target. The
    # location is still observed, but statting it is no longer possible.
    return Path(value.removesuffix(" (deleted)"))


def _writable_fd_paths(entry: Path) -> list[Path]:
    paths = []
    try:
        descriptors = list((entry / "fd").iterdir())
    except OSError:
        return paths
    for descriptor in descriptors:
        try:
            info = (entry / "fdinfo" / descriptor.name).read_text()
            flags_line = next(line for line in info.splitlines() if line.startswith("flags:"))
            flags = int(flags_line.split()[1], 8)
            if (flags & os.O_ACCMODE) not in (os.O_WRONLY, os.O_RDWR):
                continue
            target = _observed_path(os.readlink(descriptor))
        except (OSError, StopIteration, ValueError):
            continue
        if target is not None and target.is_file():
            paths.append(target)
    return paths


def _task_for_paths(paths: list[Path], tasks_by_dir: dict[str, dict]) -> tuple[dict, Path] | None:
    tasks_root = REPO / "tasks"
    for path in paths:
        try:
            relative = path.relative_to(tasks_root)
        except ValueError:
            continue
        if not relative.parts:
            continue
        task = tasks_by_dir.get(relative.parts[0])
        if task:
            return task, tasks_root / relative.parts[0]
    return None


def _process_command(cmdline: list[str]) -> str:
    for part in cmdline[1:]:
        if part and not part.startswith("-") and Path(part).suffix in (".py", ".sh"):
            return Path(part).name
    return Path(cmdline[0]).name if cmdline and cmdline[0] else "unknown"


def _ancestor_pids(entry: Path) -> list[int]:
    """Observed parent chain for one process, stopping at an unreadable edge."""
    ancestors = []
    current = entry
    seen = {entry.name}
    while True:
        try:
            parent_line = next(line for line in (current / "status").read_text().splitlines()
                               if line.startswith("PPid:"))
            parent = int(parent_line.split()[1])
        except (OSError, StopIteration, IndexError, ValueError):
            break
        if parent <= 0 or str(parent) in seen:
            break
        ancestors.append(parent)
        seen.add(str(parent))
        current = PROC / str(parent)
    return ancestors


def _registered_live_pids(catalogue: list[dict]) -> set[int]:
    """Identity-checked child and watcher roots owned by the existing runner."""
    if RUN_REGISTRY is None:
        raise ProcessInventoryUnavailable(
            "реестр живых прогонов этой установки недоступен; "
            "опись долгоживущих процессов подавлена")
    live = set()
    for task in catalogue:
        directory = task.get("dir") or task.get("slug")
        if not directory:
            continue
        try:
            processes = RUN_REGISTRY.live_run_processes(REPO / "tasks" / directory)
        except OSError:
            continue
        for process in processes:
            pid = process.get("pid")
            if isinstance(pid, int):
                live.add(pid)
    return live


def _output_entry(path: Path, observed_by: str, now: float,
                  direct: bool) -> dict | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    return {
        "path": str(path),
        "size": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "modified_age_seconds": max(int(now - stat.st_mtime), 0),
        "observed_by": observed_by,
        "direct": direct,
        "growing": None,
        "growth_bytes": None,
        "growth_src": "нужны два наблюдения размера файла",
    }


def _write_chars(entry: Path) -> int | None:
    try:
        fields = dict(line.split(":", 1) for line in (entry / "io").read_text().splitlines())
        return int(fields["wchar"].strip())
    except (OSError, KeyError, ValueError):
        return None


def long_lived_processes(thread: dict, catalogue: list[dict] | None = None) -> list[dict]:
    """Processes attributable to a finished task and this thread's repositories.

    Attribution is entirely external to the child: paths observed in `/proc`
    connect it to a task directory and to a configured repository. A writable
    fd names an output directly. A short-lived append may close between scans,
    so a regular file beside the process's task cwd is also included when its
    mtime is newer than the process; the evidence kind remains explicit.
    """
    # Thread membership has one existing owner: the task index filtered by the
    # direction's linked projects/search terms. Looking at the whole catalogue
    # made every task appear to belong to `process`, because every task cwd is
    # below that direction's shared task-agent repository.
    catalogue = catalogue if catalogue is not None else thread_tasks(thread, limit=5000)
    tasks_by_dir = {(task.get("dir") or task.get("slug")): task for task in catalogue
                    if task.get("dir") or task.get("slug")}
    registered = _registered_live_pids(catalogue)
    repos = [Path(path) for path in thread.get("repos", [])]
    now = time.time()
    found = []
    try:
        entries = list(PROC.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().decode(
                "utf-8", "replace").split("\0")
            cwd = Path(os.readlink(entry / "cwd"))
        except OSError:
            continue
        if not any(cmdline):
            continue
        # A terminal frontmatter value does not end a still-running registered
        # chain. Its child, watcher and descendants remain the ordinary live
        # run and must never be projected a second time as detached work.
        if int(entry.name) in registered or registered.intersection(_ancestor_pids(entry)):
            continue
        try:
            executable = Path(os.readlink(entry / "exe"))
        except OSError:
            executable = None
        writable = _writable_fd_paths(entry)
        observed_paths = [cwd, *writable]
        if executable is not None:
            observed_paths.append(executable)
        observed_paths.extend(path for part in cmdline
                              if (path := _observed_path(part)) is not None)
        attributed = _task_for_paths(observed_paths, tasks_by_dir)
        if attributed is None:
            continue
        task, task_root = attributed
        if task.get("status") not in TERMINAL:
            continue
        task_storage = REPO / "tasks"
        repository_paths = [path for path in observed_paths
                            if not _under(path, task_storage)]
        matched_repos = [repo for repo in repos
                         if any(_under(path, repo) for path in repository_paths)]
        if not matched_repos:
            continue
        try:
            started = process_started(entry)
        except OSError:
            continue
        outputs: dict[str, dict] = {}
        for path in writable:
            item = _output_entry(path, "writable fd in /proc", now, direct=True)
            if item:
                outputs[item["path"]] = item
        # The concrete 692 probe opens its JSONL only for the append itself, so
        # its fd is normally absent during a scan. Its cwd is the task artifacts
        # directory and only files changed after process start are candidates.
        if _under(cwd, task_root):
            try:
                neighbours = list(cwd.iterdir())
            except OSError:
                neighbours = []
            for path in neighbours:
                try:
                    changed_after_start = path.is_file() and path.stat().st_mtime >= started
                except OSError:
                    continue
                if changed_after_start:
                    item = _output_entry(
                        path, "mtime файла в рабочем каталоге задачи новее процесса",
                        now, direct=False)
                    if item:
                        outputs.setdefault(item["path"], item)
        found.append({
            "pid": int(entry.name),
            "task": task.get("id"),
            "task_title": task.get("title"),
            "task_status": task.get("status"),
            "repo": str(matched_repos[0]),
            "command": _process_command(cmdline),
            "launcher": cmdline[0],
            "since": datetime.fromtimestamp(started, timezone.utc).isoformat(),
            "age_seconds": max(int(now - started), 0),
            "outputs": sorted(outputs.values(), key=lambda item: item["path"]),
            "write_chars": _write_chars(entry),
            "duplicate": False,
            "duplicate_group": None,
            "duplicate_count": 1,
            "src": "cmdline, cwd, exe, fd и mtime в /proc/{}".format(entry.name),
        })
    groups: dict[tuple, list[dict]] = {}
    for process in found:
        signature = (process["task"], process["repo"], process["command"])
        groups.setdefault(signature, []).append(process)
    for signature, group in groups.items():
        if len(group) < 2:
            continue
        group_id = hashlib.sha256(repr(signature).encode()).hexdigest()[:12]
        for process in group:
            process.update(duplicate=True, duplicate_group=group_id,
                           duplicate_count=len(group))
    return sorted(found, key=lambda item: (item["task"] or 0, item["command"], item["pid"]))


def process_growth(current: list[dict], previous: dict | None) -> list[dict]:
    """Compare observer-owned file sizes from two real process-table scans."""
    prior = {}
    for process in (previous or {}).get("processes", []):
        identity = (process.get("pid"), process.get("since"))
        for output in process.get("outputs", []):
            prior[(*identity, output.get("path"))] = output
    prior_processes = {(process.get("pid"), process.get("since")): process
                       for process in (previous or {}).get("processes", [])}
    previous_at = (previous or {}).get("observed_at")
    for process in current:
        for output in process["outputs"]:
            old = prior.get((process["pid"], process["since"], output["path"]))
            if old is None or not isinstance(old.get("size"), int):
                continue
            delta = output["size"] - old["size"]
            old_process = prior_processes.get((process["pid"], process["since"]), {})
            old_chars = old_process.get("write_chars")
            new_chars = process.get("write_chars")
            wrote = (isinstance(old_chars, int) and isinstance(new_chars, int)
                     and new_chars > old_chars)
            # A writable fd directly associates the process and file. An mtime
            # neighbour is only a candidate until both the file and this
            # process's kernel write counter advance in the same interval; this
            # prevents a sleeping neighbour from claiming another probe's file.
            attributed = output.get("direct") or wrote
            output["growing"] = delta > 0 if attributed else None
            output["growth_bytes"] = delta
            if not attributed and delta > 0:
                output["growth_src"] = (
                    f"файл вырос {old['size']} → {output['size']} байт, но счётчик "
                    "записи этого процесса не вырос; файл ему не приписан")
            else:
                output["growth_src"] = (
                    f"размер {old['size']} → {output['size']} байт между наблюдениями"
                    + (f" (предыдущее {previous_at})" if previous_at else ""))
    return current


# Where a tick leaves what it saw and what it did. The board reads it and does
# not recompute it: «когда проверял в прошлый раз и чем та проверка кончилась»
# is an observation taken at the moment of the check, and a board that derived
# it later would be answering a different question from a different instant.
#
# «Когда продакт проверит статус в следующий раз» is the opposite case and is
# not in that file: it is a fact about *now*, and the only moment at which the
# tick could have written it is the one moment it is guaranteed to be missing —
# see `next_check` below.
THREAD_STATE = HOME / "state" / "threads"

# `systemctl show` prints a timestamp as «Fri 2026-08-07 17:00:00 CEST» whatever
# `--timestamp=` is asked of it, so the instant is taken out of it by shape.
SYSTEMD_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
# States in which the paired service is not occupying its timer. Anything else
# means the timer is not armed *because a check is running right now*, which is
# the difference between «неизвестно» and «неизвестно, и вот почему».
SERVICE_AT_REST = {"inactive", "failed"}
SYSTEMCTL_TIMEOUT = tunable("PRODUCT_OWNER_SYSTEMCTL_TIMEOUT_SECONDS", 10)


def systemd(command: list[str]) -> str | None:
    """One systemd query, or `None` if this host has no answer to give.

    A stand without systemd is a supported place to build this snapshot, so
    every question asked of it has to survive not being answered.
    """
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=SYSTEMCTL_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def next_check(thread: str) -> dict:
    """When this direction is due to look again, asked at the instant of asking.

    Asked of systemd, and *only* of what systemd itself reports as armed.
    Computing the next calendar minute instead — from `systemd-analyze calendar`,
    or from the `next_elapse=` systemd prints inside `TimersCalendar`, which is
    the same computation — looks like an observation and is not one: the calendar
    answers «when would this spec fire», systemd answers «when is this timer set
    to fire», and a woken owner that runs past the twenty-minute step makes the
    two disagree. A check started at 18:05 and still running at 18:26 has lost
    the 18:25 firing; systemd arms 18:45 and the calendar still says 18:25.

    Collected here, with the rest of the board, rather than written into the
    direction's state file by the tick. The tick *is* the service paired with the
    timer, so the one moment it could write this field is the one moment
    `NextElapseUSecRealtime` is empty by construction — and review 900 found the
    result: `next_at=null` in all four live files while systemd was holding real
    future times for three of them. The question «когда проверит в следующий
    раз» is about the present, so it is answered when the board is built.
    """
    unit = f"product-thread@{thread}.timer"
    armed = systemd(["systemctl", "show", unit, "--property=NextElapseUSecRealtime", "--value"])
    match = SYSTEMD_STAMP.search(armed or "")
    if match:
        try:
            local = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").astimezone()
            return {"at": local.isoformat(), "src": f"NextElapseUSecRealtime таймера {unit}"}
        except ValueError:
            pass
    # An unknown instant is said as unknown — but why it is unknown is itself
    # observable, and the common case has an ordinary answer: a check running
    # right now is what is holding the timer unarmed.
    service = f"product-thread@{thread}.service"
    state = (systemd(["systemctl", "show", service, "--property=ActiveState", "--value"])
             or "").strip()
    if state and state not in SERVICE_AT_REST:
        return {"at": None,
                "src": f"таймер {unit} не взведён, пока идёт проверка этого направления: "
                       f"{service} в состоянии {state}, а NextElapseUSecRealtime пуст — "
                       "systemd назовёт следующее срабатывание, когда проверка закончится"}
    return {"at": None,
            "src": f"systemd не сообщил следующего срабатывания таймера {unit}: "
                   "NextElapseUSecRealtime пуст, а вычислять время по календарю "
                   "вместо наблюдения эта панель не станет"}


def wake_unit(thread: str) -> str:
    """The unit that performs one wake-up of this direction.

    The same one the twenty-minute timer starts. That is the whole reason the
    board may offer «продолжить сейчас» without inventing a queue, a lock or a
    supervisor of its own: systemd does not run two instances of one unit, so a
    second click, and a background tick arriving in the same minute, are the same
    single check.
    """
    return f"product-thread@{thread}.service"


def wake_state(thread: str) -> dict:
    """Whether a check of this direction is running right now.

    Asked of systemd while the board is built, like `next_check` and for the
    same reason. `running: None` means this host had nothing to answer with — a
    stand without systemd is a supported place to build this snapshot — and the
    board must not read that as «проверка не идёт».
    """
    unit = wake_unit(thread)
    state = systemd(["systemctl", "show", unit, "--property=ActiveState", "--value"])
    if state is None:
        return {"unit": unit, "running": None,
                "src": f"systemd не ответил про {unit} на этом хосте"}
    value = state.strip() or "unknown"
    return {"unit": unit, "running": value not in SERVICE_AT_REST,
            "src": f"ActiveState={value} единицы {unit}"}


# What the board says when a record exists but does not match the contract the
# renderer is promised. A state file is runtime state, rebuilt by the next tick
# within the wake-up interval, so a record written by an older version of the
# tick is a normal transient — and taking the whole board down for twenty
# minutes over one stale field would be a far worse answer than saying so. It is
# still not «проверок не было»: that would be a false claim about the world.
UNREADABLE_CHECK_QUEUE = {"live": 0, "pickup": 0, "ready": 0, "decided": 0,
                          "undelivered": 0, "waiting_user": 0}


def thread_check(key: str) -> dict | None:
    """The last wake-up of one direction, as that wake-up recorded it.

    `None` means no tick has ever written for this direction — which is a real
    answer and not the same as «проверял и ничего не нашёл». The board prints
    the difference, and prints a third answer for a record it cannot read.
    """
    path = THREAD_STATE / f"{key}.json"
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    check = payload.get("check")
    if not isinstance(check, dict):
        return None
    try:
        return validate_check(check, f"проверка направления {key!r}")
    except ContractError as broken:
        return {
            "at": str(payload.get("updated_at") or "1970-01-01T00:00:00+00:00"),
            "outcome": "запись прошлой проверки не читается: она не сходится с "
                       f"нынешним контрактом ({broken})",
            "outcome_src": f"файл {path.name}, разобранный контрактом снимка",
            "woke_owner": False, "started": None, "events": [], "reasons": [],
            "queue": dict(UNREADABLE_CHECK_QUEUE),
            "src": f"state/threads/{key}.json",
        }


def thread_goals(key: str) -> list[dict]:
    """Durable goals of one direction, read from the store that outlives sessions.

    Read here rather than copied out of the tick's state file, so the board shows
    what a person asked for even before the next wake-up runs — and so a goal
    opened in the console appears at once. The store answers «нет целей» only when
    there are none; a file it cannot parse comes back as a goal that says so.
    """
    try:
        return product_goal.panel(key)
    except (OSError, ValueError, product_goal.GoalError):
        return []


def thread_session(key: str) -> dict | None:
    """Кто ведёт направление: непрерывная сессия под целью — или сам тик.

    Читается здесь и сейчас, а не переносится из записи тика, ровно по той же
    причине, что и `next_check`: живость сессии — факт о настоящем моменте, и
    запись двадцатиминутной давности отвечала бы про другой момент. `None`
    значит «целей под усиленным контролем нет», а это не то же самое, что
    «сессия умерла»: обычная работа ведётся тиком и ничего лишнего не получает.
    """
    # Импорт внутри функции: `goal_session` читает отсюда наблюдение процессов, и
    # взаимный импорт на уровне модуля оставил бы одному из двух недособранный.
    import goal_session  # noqa: PLC0415

    try:
        record = goal_session.read(key)
        if not goal_session.reinforced(key) and not record:
            return None
        alive = goal_session.liveness(record)
        session = record.get("session") or {}
        turn = record.get("last_turn") or {}
        return {
            "live": bool(alive["live"]),
            "reason": alive["reason"],
            "id": session.get("id"),
            "engine": session.get("engine"),
            "model": session.get("model"),
            "turns": session.get("turns") or 0,
            "opened_at": session.get("opened_at"),
            "heartbeat": record.get("heartbeat"),
            "last_turn_at": turn.get("at") or None,
            "last_turn_reaction_seconds": turn.get("reaction_seconds"),
            "post_check": turn.get("post_check"),
            "recovered": bool(record.get("recovered")),
            "stopped": (record.get("stopped") or {}).get("reason"),
            "src": alive["src"],
        }
    except (OSError, ValueError):
        return None


def build(anonymize: bool, only: str | None = None,
          include_task_cards: bool = False) -> dict:
    """The one observation of the contour everything else reads.

    `only` narrows it to a single direction. It exists so a scheduled wake-up
    costs what it used to when it had its own observer: the tick asks about one
    thread and pays for one thread. What it must not do is answer differently —
    the same functions produce the same fields either way.
    """
    config = load_config()
    catalogue = task_catalogue()
    # Read once: whose question is still unanswered is asked of every task and
    # every product, and the mailbox may not answer it differently between them.
    mail = mailbox()
    observed = attachment_observations()
    statuses = {task["id"]: task.get("status") for task in catalogue if task.get("id")}
    # The order of work and what stands outside it, read once from their owner.
    # Every direction is judged against the same revision: a queue that differed
    # between panels would be two queues.
    plan = plan_projection(catalogue)
    indexed_entries: dict[str, dict] = {}
    if include_task_cards and only is None:
        all_entries = [task_entry(task, mail, observed) for task in catalogue]
        assign_areas(all_entries, catalogue, statuses, plan)
        indexed_entries = {entry["dir"]: entry for entry in all_entries}
    threads = []
    for key, thread in config["threads"].items():
        if only and key != only:
            continue
        source = thread_tasks(thread)
        tasks = [indexed_entries.get(Path(task["path"]).name)
                 or task_entry(task, mail, observed) for task in source]
        # A start condition may name a task of another direction, and the index
        # is the same one the board is already built from, so the answer to «эта
        # задача закрыта?» cannot depend on which thread is being collected.
        assign_areas(tasks, source, statuses, plan)
        threads.append({
            "key": key,
            "title": thread.get("title", key),
            "products": thread.get("products", []),
            "task_count": len(tasks),
            "tasks": tasks,
            "repos": [repo_state(path) for path in thread.get("repos", [])],
            # The product owner's mailbox belongs to the contour, so it is shown
            # on the direction that owns the contour rather than invented for all
            # four.
            "channels": thread_channels(tasks, is_owner=str(REPO) in thread.get("repos", [])),
            # When the owner of this direction last looked and what came of it.
            # Read from what the tick wrote at the moment of the check, never
            # from the prose of a woken agent.
            "check": thread_check(key),
            # What is owed to the user on this direction and where it stands.
            # A goal is product memory rather than an observation of a process,
            # so it is read from its own durable store.
            "goals": thread_goals(key),
            # И кто их сейчас ведёт: одна продолжающаяся сессия под усиленной
            # целью или двадцатиминутный тик. `None` — усиленных целей нет.
            "goal_session": thread_session(key),
            # When it looks next — asked of systemd here and now, because that
            # is the instant the answer is about. `None` with a named reason
            # when systemd is holding nothing armed.
            "next_check": next_check(key),
            # And whether one is running at this instant, asked of the same
            # systemd. The board offers «продолжить сейчас» against this: a
            # check already under way is the answer to that request rather than
            # a reason for a second one.
            "wake": wake_state(key),
        })
    if only and not threads:
        raise SystemExit(f"unknown thread: {only}; known: {sorted(config['threads'])}")
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "mode": "demo" if anonymize else "real",
        "threads": threads,
        "products": products(catalogue, mail),
        # Очередь и бэклог словами их владельца. `places` и `conflicts` наружу не
        # едут: это рабочая опись по всем задачам каталога, а на экране нужна
        # строка плана рядом с задачей — и она, и вторая строка, если план сказал
        # двояко, уже стоят на плашке.
        "plan": {key: value for key, value in plan.items()
                 if key not in ("places", "conflicts")},
        # Who else is deciding right now. Not a thread and not a task: it is the
        # contour watching itself, and it belongs above the columns.
        "owners_awake": owner_wakeups(config),
        # На чём работает то, что человек сейчас читает. Стоит в подвале страницы,
        # чтобы расхождение установленного и закоммиченного было видно глазами.
        "revision": revision(),
        # Lookup stays lookup: its metadata comes from tasks_index.py. The initial
        # page may also carry directory-backed cards; light live polls do not copy
        # those 3+ MiB every ten seconds, and the page keeps the first document's.
        # A narrowed observer is the wake-up interface, not the board. It does
        # not carry an unrelated all-task list in each direction's state file.
        "task_index": task_index(catalogue, indexed_entries) if only is None else [],
    }
    if anonymize:
        snapshot = scrub(snapshot)
    # The renderer sees this document and nothing else, so the shape it is
    # promised is checked here rather than discovered in the browser.
    return validate_snapshot(snapshot)


def plan_links() -> None:
    """Every line of every «В работе» with its verdict and the evidence for it.

    The area on the board shows one side of this — the lines nothing was found
    for. Judging the area means seeing the other side too: which line was held
    back, by which task, and by which of the two observations. Without this the
    rule is only as checkable as the reader's patience, and the previous rule
    survived a whole review that way.
    """
    catalogue = task_catalogue()
    for path in sorted(PRODUCTS.glob("*/snapshot.md")):
        try:
            text = path.read_text()
        except OSError:
            continue
        items = markdown_section(text, "В работе")
        print(f"\n=== {path.parent.name}: строк в «В работе» — {len(items)}")
        for item in items:
            head = " ".join(item.split())[:110]
            if item.startswith(STRUCK):
                print(f"  [зачёркнуто] {head}")
                continue
            link = promise_link(item, catalogue)
            if link:
                print(f"  [связана {link['task']}] {head}")
                print(f"      чем: {link['how']}")
                print(f"      задача: {link['title']}")
                continue
            numbers = task_references(item)
            print(f"  [НАДО ЗАПЛАНИРОВАТЬ] {head}")
            print("      связь с задачей не установлена; сверено с каталогом "
                  f"({len(catalogue)} задач) по номеру-ссылке, названию и слагу"
                  + (f"; номера-ссылки {numbers} в каталоге не найдены" if numbers else ""))


def questions_report() -> None:
    """Every open question of every product, with its owner and the evidence.

    The board shows one side of this — what the user still owes. Judging the area
    means seeing the other side too: which question was held back as ours, and by
    which observation. The previous rule shipped without such a view and stood
    until a person counted the sixteen entries by hand.
    """
    mail = mailbox()
    print(f"почта продакта: тредов известно {len(mail['threads'])}, "
          f"каталог sent {'есть' if mail['sent_known'] else 'отсутствует'}")
    for path in sorted(PRODUCTS.glob("*/snapshot.md")):
        try:
            text = path.read_text()
        except OSError:
            continue
        items = [q for q in markdown_section(text, "Открытые вопросы")
                 if not q.startswith(STRUCK)]
        print(f"\n=== {path.parent.name}: открытых строк — {len(items)}")
        for entry in questions_of(items, mail):
            head = " ".join(entry["text"].split())[:110]
            print(f"  [{'ЖДЁТ ПОЛЬЗОВАТЕЛЯ' if entry['owner'] == 'user' else 'решает продакт'}] {head}")
            for label, value in (("спрошено", entry["asked_src"]),
                                 ("ответ", entry["answer_src"]),
                                 ("прим.", entry["note"])):
                if value:
                    print(f"      {label}: {value}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anonymize", action="store_true",
                        help="strip absolute paths, mail addresses and numeric identifiers")
    parser.add_argument("--out", type=Path, help="write here instead of stdout")
    parser.add_argument("--summary", action="store_true", help="print counts instead of the document")
    parser.add_argument("--thread", help="observe one direction instead of all four")
    parser.add_argument("--plan-links", action="store_true",
                        help="судьба каждой строки «В работе»: с какой задачей связана и чем")
    parser.add_argument("--questions", action="store_true",
                        help="судьба каждого открытого вопроса: чей он и чем это наблюдено")
    args = parser.parse_args()

    if args.plan_links:
        return plan_links()
    if args.questions:
        return questions_report()

    snapshot = build(args.anonymize, args.thread)
    if args.summary:
        for thread in snapshot["threads"]:
            flags: dict[str, int] = {}
            for task in thread["tasks"]:
                for flag in task["flags"]:
                    flags[flag] = flags.get(flag, 0) + 1
            print(f"{thread['key']}: задач {thread['task_count']}, {flags}")
        print(f"вопросов пользователю: {sum(len(p['questions']) for p in snapshot['products'])}")
        return

    text = json.dumps(snapshot, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(text)
        print(f"{args.out} — {len(text)} байт, режим {snapshot['mode']}")
        return
    print(text)


if __name__ == "__main__":
    sys.exit(main())
