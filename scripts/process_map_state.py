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
from pathlib import Path

from process_map_schema import SCHEMA_VERSION, validate_snapshot

HOME = Path(__file__).resolve().parents[1]
CONFIG = HOME / "threads.json"
PRODUCTS = HOME / "products"
REPO = Path("/opt/projects/companion-agent")
TASKS_INDEX = REPO / "skills" / "task-creator" / "scripts" / "tasks_index.py"
PYTHON = REPO / ".venv" / "bin" / "python"

# Terminal statuses never carry a live figure on the map, however loud the label.
TERMINAL = {"completed", "cancelled", "superseded"}


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

    return {
        "id": task.get("id"),
        "title": task.get("title"),
        "status": status,
        "status_detail": task.get("status_detail"),
        "dir": Path(task["path"]).name,
        "run": run,
        "gates": verdicts,
        "flags": flags,
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
    (re.compile(r"\b\d{7,}\b"), "<id>"),
]


# Titles survive anonymisation on purpose: the user asked to recognise a specific
# task among all the shown work by its real name, not by a demo caption. Content
# privacy of those titles stays a human step before any showing.
KEEP_AS_IS = {"title", "task_title"}


def scrub(value):
    if isinstance(value, str):
        for pattern, replacement in SCRUB:
            value = pattern.sub(replacement, value)
        return value
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: item if key in KEEP_AS_IS else scrub(item)
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
