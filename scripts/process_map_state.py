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

from process_map_schema import SCHEMA_VERSION, STATIONS, validate_snapshot

HOME = Path(__file__).resolve().parents[1]
CONFIG = HOME / "threads.json"
PRODUCTS = HOME / "products"
REPO = Path("/opt/projects/companion-agent")
TASKS_INDEX = REPO / "skills" / "task-creator" / "scripts" / "tasks_index.py"
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


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    return True


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
    alive = pid_alive(int(pid)) if isinstance(pid, int) else False

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


def state_age(task_dir: Path, run: dict) -> tuple[str | None, int | None]:
    """When the current state was last observed to change, and how long ago.

    A jam is not a status, it is time spent in one — so the plate needs an age.
    It is taken from the mtime of the file that carries the state, never from a
    timestamp the child wrote itself: children routinely put local time into a
    UTC field, and the contour has been burned by that already.
    """
    carrier = task_dir / ("status.json" if run.get("state") else "task.md")
    try:
        mtime = carrier.stat().st_mtime
    except OSError:
        return None, None
    stamp = datetime.fromtimestamp(mtime, timezone.utc).isoformat()
    return stamp, max(int(time.time() - mtime), 0)


def attempt_count(task_dir: Path) -> int:
    """How many dev-pipeline runs left state on disk. One task, several tries."""
    root = task_dir / "dev-pipeline"
    if not root.is_dir():
        return 0
    return sum(1 for child in root.iterdir() if child.is_dir())


def board_area(status: str | None, flags: list[str], has_questions: bool) -> str:
    if status in TERMINAL:
        return "done"
    # A person deciding comes first, so the area is checked first: a blocked task
    # in our contour is one a human has to restart.
    if has_questions or status == "blocked":
        return "waiting_human"
    if "live" in flags:
        return "running"
    if {"stale_label", "killed", "gap"} & set(flags):
        return "stuck"
    return "queued"


OPEN_QUESTIONS = re.compile(r"^##\s+Open Questions\s*$(.*?)(?=^##\s|\Z)",
                            re.MULTILINE | re.DOTALL)


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

    questions = open_questions(task_dir)
    actor, actor_src = observed_actor(task_dir, run)
    role, role_src = observed_role(task_dir)
    since, age = state_age(task_dir, run)
    progress = run.get("progress") or {}

    return {
        "id": task.get("id"),
        "title": task.get("title"),
        "status": status,
        "status_detail": task.get("status_detail"),
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
            "since": since,
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
    entries = []
    for path in sorted(PRODUCTS.glob("*/product.md")):
        try:
            text = path.read_text()
        except OSError:
            continue
        entries.append({
            "slug": path.parent.name,
            "questions": [q for q in markdown_section(text, "Открытые вопросы") if not q.startswith("~~")],
            "effect": markdown_section(text, "Журнал эффекта")[:8],
        })
    return entries


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
    titles from the cleaning altogether — which is what this used to do — let a
    real chat identifier through inside a real title. Content privacy of titles
    stays a human step before showing, and is declared as a limit.
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
