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
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from process_map_schema import SCHEMA_VERSION, STATIONS, scrub, validate_snapshot

HOME = Path(__file__).resolve().parents[1]
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
    return {
        "state": status.get("state"),
        "runner": status.get("runner") or runner.get("runner"),
        "workflow": status.get("workflow"),
        "sandbox": runner.get("sandbox_mode"),
        "stop_reason": runner.get("watcher_stop_reason") or None,
        "exit_code": runner.get("exit_code"),
        "pid": pid if isinstance(pid, int) else None,
        "alive": alive,
        "alive_src": alive_src,
        "progress": progress,
    }


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


def board_area(status: str | None, flags: list[str], has_questions: bool) -> str:
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
    if {"stale_label", "killed", "gap", "blocked"} & set(flags):
        return "stuck"
    return "queued"


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
        "board": {
            "area": board_area(status, flags, bool(questions)),
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


def repo_state(path: str) -> dict:
    repo = Path(path)
    if not (repo / ".git").exists():
        return {"name": repo.name, "present": False}

    def git(*args: str) -> str:
        out = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True)
        return out.stdout.strip() if out.returncode == 0 else ""

    dirty = [line for line in git("status", "--porcelain").splitlines() if not line.startswith("??")]
    unpushed = git("rev-list", "--count", "@{u}..HEAD")
    return {
        "name": repo.name,
        "present": True,
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
        })
    return entries


def build(anonymize: bool) -> dict:
    config = load_config()
    threads = []
    for key, thread in config["threads"].items():
        tasks = [task_entry(task) for task in thread_tasks(thread)]
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
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "mode": "demo" if anonymize else "real",
        "threads": threads,
        "products": products(),
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
    args = parser.parse_args()

    snapshot = build(args.anonymize)
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
