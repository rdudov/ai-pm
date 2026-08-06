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
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from process_map_schema import SCHEMA_VERSION, STATIONS, scrub, validate_snapshot

HOME = Path(__file__).resolve().parents[1]
PROC = Path("/proc")
CONFIG = HOME / "threads.json"
PRODUCTS = HOME / "products"
REPO = Path("/opt/projects/companion-agent")
TASKS_INDEX = REPO / "skills" / "task-creator" / "scripts" / "tasks_index.py"
RUNNER_SCRIPTS = REPO / "skills" / "task-runner" / "scripts"
PYTHON = REPO / ".venv" / "bin" / "python"
MAIL_ROOT = REPO / ".state" / "gmail" / "product-owner"
MAIL_INBOX = MAIL_ROOT / "inbox"
MAIL_SENT = MAIL_ROOT / "sent"

# Terminal statuses never carry a live figure on the map, however loud the label.
TERMINAL = {"completed", "cancelled", "superseded"}

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
    start tick, and `runner_pid_namespace_visible` decides when a negative
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
    """
    if RUNNER is None:
        return False, None
    if not RUNNER.runner_pid_namespace_visible(runner):
        return False, "личность процесса ненаблюдаема: другое пространство имён PID"
    if RUNNER.process_is_live(runner.get("pid"), runner.get("process_identity")):
        return True, "pid и стартовый тик ядра совпали с .runner/runner.json"
    return False, None


def read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def query_tasks(args: list[str], limit: int = 60) -> list[dict]:
    out = subprocess.run(
        [str(PYTHON), str(TASKS_INDEX), "query", *args, "--format", "json", "--limit", str(limit)],
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
    return query_tasks(["--status", "all"], limit=5000)


def thread_tasks(thread: dict) -> list[dict]:
    seen: dict[str, dict] = {}
    for project in thread.get("projects", []):
        for task in query_tasks(["--project", project, "--status", "all"]):
            seen[task["path"]] = task
    for term in thread.get("task_search", []):
        for task in query_tasks(["--search", term, "--status", "all"]):
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
    anything went out and when the last thing did. One receipt per message
    actually sent — the tail of the journal, not its whole length in memory.
    """
    path = task_dir / "dev-pipeline" / "notification-receipts.jsonl"
    count = count_lines(path)
    if not count:
        return None
    last = last_json_line(path)
    return {"count": count, "last_at": last.get("recorded_at"),
            "last_kind": last.get("kind"),
            "src": "dev-pipeline/notification-receipts.jsonl"}


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

# What the run's own notifications say. Every receipt in the whole repository is
# one of these — 117 `attempt_started`, 73 `attempt_completed`, 41
# `attempt_completed_rejected`, 2 `attempt_failed` — so «квитанция есть» has
# never once meant «документ доставлен». A kind outside this set is about
# something other than the life of the run and is taken as delivery evidence,
# because refusing to believe an unknown receipt would be inventing an alarm.
LIFECYCLE_RECEIPTS = frozenset({
    "attempt_started", "attempt_completed", "attempt_completed_rejected",
    "attempt_failed", "run_started", "run_completed", "run_failed",
    "process_started", "native_session_discovered", "checkpoint_completed",
    "increment_completed", "increment_ready_for_review", "review_started",
    "review_completed", "blocked_on_user_decision",
})

# The manifest lists the deliverables; it is not itself a document for a person.
NOT_A_DOCUMENT = {"manifest.json"}


def human_document(task_dir: Path) -> dict | None:
    """A file this task made for a person, by name and size and nothing else.

    Two places, both named by the task: anything in `deliverables/`, and an
    `*.html` at the top of the task directory — the shape a report has when a
    run wrote it straight into its own directory. No file is opened.
    """
    found: list[Path] = []
    box = task_dir / "deliverables"
    if box.is_dir():
        try:
            found += [p for p in sorted(box.iterdir())
                      if p.is_file() and p.name not in NOT_A_DOCUMENT]
        except OSError:
            pass
    try:
        found += sorted(task_dir.glob("*.html"))
    except OSError:
        pass
    if not found:
        return None
    biggest = max(found, key=lambda p: p.stat().st_size if p.exists() else 0)
    try:
        size = biggest.stat().st_size
    except OSError:
        size = None
    return {"name": str(biggest.relative_to(task_dir)), "bytes": size,
            "count": len(found),
            "src": "файл в deliverables/ или *.html в каталоге задачи"}


def delivery_receipt(task_dir: Path) -> dict | None:
    """A receipt about a document rather than about the life of the run."""
    path = task_dir / "dev-pipeline" / "notification-receipts.jsonl"
    try:
        text = path.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = payload.get("kind")
        if kind and kind not in LIFECYCLE_RECEIPTS and payload.get("message_id"):
            return {"kind": kind, "at": payload.get("recorded_at")}
    return None


# The names the contour actually writes its delivery note under. `delivery.md` is
# the one the audit of 835 wrote 71 times; `product-owner-delivery.md` is the one
# the product owner writes by hand when they send a document themselves, and on
# 2026-08-06 they wrote six of them — including all three documents of the very
# decision this observation has to check. Both carry the same three things, which
# is what makes them one convention rather than two: channel, message identifier,
# sha256 of what was attached. Knowing only the first name meant the observer
# called three deliveries of that evening undelivered while the letters were in
# the user's mailbox.
DELIVERY_NOTES = ("delivery.md", "product-owner-delivery.md")


def delivery_note(task_dir: Path) -> Path | None:
    """The delivery note of this task under any of the names the contour uses."""
    for name in DELIVERY_NOTES:
        path = task_dir / name
        if path.is_file():
            return path
    return None


def handoff(task_dir: Path) -> dict | None:
    """Whether the document this task made ever reached a person, and what said so.

    Nothing made for a person means nothing to hand over, and the task is not in
    this area at all. Where there is a document, exactly two observations can
    close it: the delivery note the contour writes itself, and a receipt that is
    not one of the run's own lifecycle events.
    """
    document = human_document(task_dir)
    if not document:
        return None
    note = delivery_note(task_dir)
    if note:
        return {**document, "delivered": True,
                "delivered_src": f"файл {note.name} в каталоге задачи"}
    receipt = delivery_receipt(task_dir)
    if receipt:
        return {**document, "delivered": True,
                "delivered_src": f"квитанция {receipt['kind']} с идентификатором сообщения "
                                 f"в dev-pipeline/notification-receipts.jsonl"}
    return {**document, "delivered": False,
            "delivered_src": "записки о доставке в каталоге нет ни под одним из имён "
                             f"({', '.join(DELIVERY_NOTES)}), а квитанции задачи несут только "
                             "события жизненного цикла прогона — доставки документа среди них нет"}


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
               decision_unmet: bool = False) -> str:
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

    A decision taken out loud and not carried out is the same hole one step
    earlier. On 2026-08-06 the product owner wrote «из девяти живых документов
    человеку идут три» and three hours later none of the three had been sent: the
    decision lived in a sentence, so no observation and no wake-up knew it was
    outstanding. `decision_unmet` is that decision as a state — recorded in the
    task's own field, checked against the delivery evidence the contour already
    writes, and therefore able to close itself.
    """
    if status in TERMINAL:
        # A decision the product owner took and nobody carried out outranks the
        # passive «не доставлено»: one is work that was merely never looked at,
        # the other is work somebody already decided must go out.
        if decision_unmet:
            return "decision_unmet"
        return "undelivered" if undelivered else "done"
    # The user owing an answer comes first: it is the whole of one acceptance
    # question, and an answer nobody gives blocks everything behind it.
    if asked_user:
        return "waiting_human"
    if "live" in flags:
        return "running"
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
    # Everything below used to be one area called «в очереди», which answered
    # neither of the two questions a person actually opens the board with.
    # «Что можно подхватить прямо сейчас» is the first question of every
    # wake-up, and «за чем стоит остальное» is the second; they are the same
    # split, taken on one observation — whether anything on disk is holding
    # this task. Nothing holding it means it can be started now.
    if blocked_by:
        return "queued"
    # «Готово к запуску» is narrower than «можно подхватить» and that is the
    # whole of its value. Both say nothing is holding the task; only this one
    # says somebody wrote a condition down and the condition has since been met.
    # 831 belonged here for forty minutes and there was nowhere to put it.
    return "ready_to_start" if ready else "pickup"


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
        return ("гейты не пройдены: " + ", ".join(failed[:3]),
                "строки Result в verification.md")
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
# рабочем дереве /opt/projects/moex-trading-engine» in the last line of its
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
#     starts_after=830 worktree=/opt/projects/moex-trading-engine
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


def mailbox() -> dict:
    """The product owner's mail as two facts: which thread a message is in, and
    when the user last wrote into each thread.

    Read once per snapshot from `metadata.json` files that are already on disk —
    no network, no message body, and the same cost whatever was discussed. `sent`
    is what makes an outgoing question resolvable to a thread at all; `inbox` is
    where the answer lands.
    """
    threads: dict[str, str] = {}
    replies: dict[str, datetime] = {}
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
            if incoming and message["at"]:
                current = replies.get(message["thread"])
                if current is None or message["at"] > current:
                    replies[message["thread"]] = message["at"]
    return {"threads": threads, "replies": replies,
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
    try:
        sent_at = datetime.fromisoformat(asked["at"]).replace(tzinfo=timezone.utc)
    except ValueError:
        return {"answered": False, "src": None,
                "note": f"дата вопроса {asked['at']!r} не читается как дата"}
    if reply.date() < sent_at.date():
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


def task_entry(task: dict, mail: dict) -> dict:
    task_dir = REPO / task["path"]
    run = run_state(task_dir)
    verdicts = gates(task_dir)
    status = task.get("status")

    flags = []
    if run["alive"] and status not in TERMINAL:
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
    hand = handoff(task_dir)
    actor, actor_src = observed_actor(task_dir, run)
    role, role_src = observed_role(task_dir)
    since, age, since_src = state_age(task_dir)
    status_detail = task.get("status_detail")
    condition = start_condition(status_detail)
    why, why_src = jam_reason(status_detail, run, verdicts, flags, condition)
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
                 statuses: dict | None = None) -> None:
    """The second pass: which area each task stands in, once all of them are known.

    «Можно подхватить» is not a property of one task read alone — it is the
    absence of anything holding it, and one of the things that can hold it is
    another task's live run in the same repository. So the areas are assigned
    after every task has been observed, never during.

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
            decision_unmet=bool(decision) and not decision["done"])
        # A queued task's reason for standing is the thing holding it, and the
        # plate has one place for «почему». `jam_reason` already filled it from
        # `status_detail` when there was one; a repository held by a named run
        # is the case it could not see.
        if not entry["board"]["why"] and why:
            entry["board"]["why"] = why
            entry["board"]["why_src"] = why_src


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
    telegram = sum(count_lines(REPO / "tasks" / task["dir"] / "dev-pipeline" /
                               "notification-receipts.jsonl") for task in tasks)
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
    mail = mailbox() if mail is None else mail
    # The pool a promise is matched against is the whole catalogue, not the
    # tasks of one direction: a promise written in a product record may have
    # become a task under any project, and matching against a narrower pool
    # would report «связь не установлена» for a link that exists.
    catalogue = task_catalogue() if catalogue is None else catalogue
    entries = []
    for path in sorted(PRODUCTS.glob("*/product.md")):
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
# than by number — «ревью кода companion силами Claude», which is задача 713
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

NUMBER = re.compile(r"(?<![\w])(\d{3})(?![\w])")

# A число is a reference to a task when it is written the way this contour
# writes one, not merely when it is three digits long. Three positions, all
# taken from the product records as they are actually written:
#   «Работа заведена задачей 813»  — a task word right in front of it;
#   «2026-08-06 — **806 принята**» — the head of the claim, after the date;
#   «(736)», «(805 → 808)», «(805, 806, идут)» — inside brackets, with the
#                                                 number not counting a noun.
# The last one is why the bracket alone is not enough: «(266 тестов)» and «(630
# строк)» are brackets too, and a number immediately followed by a word is
# counting that word.
TASK_WORD = re.compile(r"(?:задач\w*|таск\w*|task|№)\s*$", re.IGNORECASE)
CLAIM_HEAD = re.compile(r"^\s*(?:\d{4}-\d{2}-\d{2})?(?:\s+\d{2}:\d{2})?\s*[—–-]*\s*"
                        r"[*`_«»\s]*(?:\d{3}[\s,и–—-]*)*$")
# What may stand right after a reference: punctuation that closes or separates
# it, an arrow to the task that replaced it, or the end of the line. A letter or
# a digit there means the number is counting something.
AFTER_REFERENCE = re.compile(r"^\s*(?:[)\]},;.!?]|→|$)")
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
    for match in NUMBER.finditer(item):
        before, after = item[:match.start()], item[match.end():]
        reference = bool(TASK_WORD.search(before)) or bool(CLAIM_HEAD.match(before))
        if not reference and inside_brackets(item, match.start()):
            reference = bool(AFTER_REFERENCE.match(after))
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
# is a coincidence between a contour that says «Calypso» in every second line and
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
    is «Ревью кода companion силами Claude: старый код и кандидаты на
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
    for number in {n for n in map(int, NUMBER.findall(item))}:
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
    «ревью кода companion силами Claude… Запрошено пользователем» stood in this
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


def owner_wakeups() -> list[dict]:
    """Other instances of the product owner that are awake right now.

    Named by the user as the sixth question after two pairs of duplicate tasks
    (790/792 and 791/793) were created in one hour: the product owner in the
    chat and the product owner woken by `product-thread@<тред>.timer` each had
    their own queue and neither could see the other's. A live tick is the
    observation that another instance is deciding something.

    Read from `/proc` command lines only — no transcript, no session file, and
    nothing the other instance says about itself.
    """
    awake = []
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().decode("utf-8", "replace").split("\0")
            started = entry.stat().st_mtime
        except OSError:
            continue
        thread = ticked_thread(cmdline)
        if thread is None:
            continue
        awake.append({
            "pid": int(entry.name),
            "thread": thread,
            "since": datetime.fromtimestamp(started, timezone.utc).isoformat(),
            "age_seconds": max(int(time.time() - started), 0),
            "src": "командная строка процесса в /proc",
        })
    return sorted(awake, key=lambda w: w["since"])


def build(anonymize: bool, only: str | None = None) -> dict:
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
    statuses = {task["id"]: task.get("status") for task in catalogue if task.get("id")}
    threads = []
    for key, thread in config["threads"].items():
        if only and key != only:
            continue
        source = thread_tasks(thread)
        tasks = [task_entry(task, mail) for task in source]
        # A start condition may name a task of another direction, and the index
        # is the same one the board is already built from, so the answer to «эта
        # задача закрыта?» cannot depend on which thread is being collected.
        assign_areas(tasks, source, statuses)
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
        })
    if only and not threads:
        raise SystemExit(f"unknown thread: {only}; known: {sorted(config['threads'])}")
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "mode": "demo" if anonymize else "real",
        "threads": threads,
        "products": products(catalogue, mail),
        # Who else is deciding right now. Not a thread and not a task: it is the
        # contour watching itself, and it belongs above the columns.
        "owners_awake": owner_wakeups(),
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
    for path in sorted(PRODUCTS.glob("*/product.md")):
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
    for path in sorted(PRODUCTS.glob("*/product.md")):
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
