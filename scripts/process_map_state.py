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


def query_tasks(args: list[str]) -> list[dict]:
    out = subprocess.run(
        [str(PYTHON), str(TASKS_INDEX), "query", *args, "--format", "json", "--limit", "60"],
        capture_output=True, text=True, cwd=REPO,
    )
    if out.returncode != 0:
        return []
    try:
        payload = json.loads(out.stdout)
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else payload.get("tasks", [])


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


def board_area(status: str | None, flags: list[str], has_questions: bool,
               blocked_by: str | None = None) -> str:
    """Which area of the board a task stands in. One rule, one owner.

    `has_questions` means a question is open *and still unanswered* — see
    `pending_questions`, which owns that judgement. It used to mean «the
    Open Questions heading has any bullet under it», and `blocked` was accepted
    as a second, silent synonym for waiting on a person, which put technical
    blockages, an already-migrated repair, a bullet saying there is no choice to
    make and a question answered in its own sentence under «ждёт решения
    человека» (finding HIGH-1 of review 786).

    A blocked task is now what it is: work that stands and cannot move — a jam,
    with its reason next to it. Someone may well have to unblock it, but that is
    not the same statement as «a person owes an answer», and the board answers
    the second question, not the first.
    """
    if status in TERMINAL:
        return "done"
    # A person owing an answer comes first: it is the whole of one acceptance
    # question, and an answer nobody gives blocks everything behind it.
    if has_questions:
        return "waiting_human"
    if "live" in flags:
        return "running"
    if {"stale_label", "killed", "gap", "blocked", "work_outside_owner"} & set(flags):
        return "stuck"
    # Everything below used to be one area called «в очереди», which answered
    # neither of the two questions a person actually opens the board with.
    # «Что можно подхватить прямо сейчас» is the first question of every
    # wake-up, and «за чем стоит остальное» is the second; they are the same
    # split, taken on one observation — whether anything on disk is holding
    # this task. Nothing holding it means it can be started now.
    return "queued" if blocked_by else "pickup"


def jam_reason(status_detail: str | None, run: dict, verdicts: list[dict],
               flags: list[str]) -> tuple[str | None, str | None]:
    """Why this task stands where it stands, and what observed it.

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
    if status_detail:
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


def queue_reason(task: dict, run: dict, busy_repos: dict) -> tuple[str | None, str | None]:
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
    """
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


def pending_questions(items: list[str]) -> list[str]:
    """The one owner of «a person still owes an answer here».

    A heading is a place, not a claim. `## Open Questions` holds whatever its
    author put there, and in this contour that includes notes with no question in
    them at all («выбора, ожидающего его ответа, в ней нет»), instructions for
    reading a future result, and questions settled in their own bullet. Counting
    the heading counted all three as people-blocking work (finding HIGH-1 of
    review 786).

    What is observable in the text itself is exactly two things: whether it asks
    anything, and whether the answer is already written next to it. Both are used
    here and nowhere else — the collector, the board, the counter above the
    columns and the tests all come through this function, so «ждёт решения
    человека» has one meaning on the whole screen.
    """
    return [item for item in items
            if "?" in item and not ANSWERED_HERE.search(item) and STRUCK not in item]


def task_entry(task: dict) -> dict:
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

    # Only the still-unanswered ones reach the plate and the counter: a settled
    # question on a board of open ones is the same lie as a wrong number.
    questions = pending_questions(open_questions(task_dir))
    actor, actor_src = observed_actor(task_dir, run)
    role, role_src = observed_role(task_dir)
    since, age, since_src = state_age(task_dir)
    status_detail = task.get("status_detail")
    why, why_src = jam_reason(status_detail, run, verdicts, flags)
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
        },
        "board": {
            # Filled by `assign_areas` once every task is known: whether a task
            # can be picked up depends on what the other tasks are holding.
            "area": None,
            "blocked_by": None,
            "blocked_by_src": None,
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


def assign_areas(entries: list[dict], tasks: list[dict]) -> None:
    """The second pass: which area each task stands in, once all of them are known.

    «Можно подхватить» is not a property of one task read alone — it is the
    absence of anything holding it, and one of the things that can hold it is
    another task's live run in the same repository. So the areas are assigned
    after every task has been observed, never during.
    """
    busy = busy_repository_map(entries)
    for entry, task in zip(entries, tasks):
        why, why_src = queue_reason(task, entry["run"], busy)
        entry["board"]["blocked_by"] = why
        entry["board"]["blocked_by_src"] = why_src
        entry["board"]["area"] = board_area(
            entry["status"], entry["flags"], bool(entry["questions"]), why)
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


def products() -> list[dict]:
    """Products, with the canonical list of questions the user owes an answer to.

    Deliberately *not* filtered through `pending_questions`, and the difference
    is the point. `## Открытые вопросы` of a product is a curated list: only
    questions are written into it, and it has its own closure convention — the
    product owner strikes a settled one through and writes when and by what it
    was closed. Honouring that convention is the whole test here.

    A task's `## Open Questions` has neither property, which is why it needs the
    stricter reading. Applying the strict reading here instead would drop four
    real product questions that happen to be phrased as statements («Какие из
    находок 706 превращаем в работу»), and dropping a question the user is
    actually waiting on is the more expensive error of the two.
    """
    entries = []
    for path in sorted(PRODUCTS.glob("*/product.md")):
        try:
            text = path.read_text()
        except OSError:
            continue
        entries.append({
            "slug": path.parent.name,
            "questions": [q for q in markdown_section(text, "Открытые вопросы") if not q.startswith(STRUCK)],
            "effect": markdown_section(text, "Журнал эффекта")[:8],
            "promises": unplanned(markdown_section(text, "В работе")),
        })
    return entries


# A reference to a task, as the product record writes one: a bare number in
# brackets at the end of a claim, or a number named in the sentence.
TASK_REFERENCE = re.compile(r"(?<!\d)\d{3}(?!\d)")


def unplanned(items: list[str]) -> list[str]:
    """Lines of «В работе» that name no task — work promised and never started.

    This is the fourth question of the board, and it has a price already paid:
    «ревью кода companion силами Claude… Запрошено пользователем» stood in this
    section for two days and never became a task, because the flow had no place
    for «надо запланировать». The section is where the product owner writes what
    is being done, so a line in it with no task number behind it is a promise
    nobody is executing.

    The test is deliberately the weak one — whether a task is referenced at all,
    not whether that task is alive. A number in the line is an observation; that
    the numbered task actually covers the promise is a reading, and reading the
    line is not something this collector is allowed to do.
    """
    return [item for item in items
            if not item.startswith(STRUCK) and not TASK_REFERENCE.search(item)]


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
        if not any("thread_tick.py" in part for part in cmdline):
            continue
        thread = next((part for part in reversed(cmdline)
                       if part and not part.startswith("-") and "thread_tick.py" not in part
                       and "python" not in part), None)
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
    threads = []
    for key, thread in config["threads"].items():
        if only and key != only:
            continue
        source = thread_tasks(thread)
        tasks = [task_entry(task) for task in source]
        assign_areas(tasks, source)
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
        "products": products(),
        # Who else is deciding right now. Not a thread and not a task: it is the
        # contour watching itself, and it belongs above the columns.
        "owners_awake": owner_wakeups(),
    }
    if anonymize:
        snapshot = scrub(snapshot)
    # The renderer sees this document and nothing else, so the shape it is
    # promised is checked here rather than discovered in the browser.
    return validate_snapshot(snapshot)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anonymize", action="store_true",
                        help="strip absolute paths, mail addresses and numeric identifiers")
    parser.add_argument("--out", type=Path, help="write here instead of stdout")
    parser.add_argument("--summary", action="store_true", help="print counts instead of the document")
    parser.add_argument("--thread", help="observe one direction instead of all four")
    args = parser.parse_args()

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
