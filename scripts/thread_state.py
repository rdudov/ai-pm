#!/usr/bin/env python3
"""Observable state of one product thread.

Collects only what can be observed: live child runs, task statuses, and recent
commits in the repositories the thread owns. It never reads child transcripts,
so a tick costs the same regardless of how much work happened.

Exit code 0 always; the caller decides what to do with the report.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
CONFIG = HOME / "threads.json"
# Tasks and their runtime state stay in the development environment; the product
# owner only reads them.
REPO = Path("/opt/projects/companion-agent")
TASKS_INDEX = REPO / "skills" / "task-creator" / "scripts" / "tasks_index.py"
PYTHON = REPO / ".venv" / "bin" / "python"
# States a run record may hold once nobody is expected to come back to it.
# Anything else, with the process gone, means work was left outstanding.
TERMINAL_RUN_STATES = {"completed", "complete", "superseded"}
# Statuses in which the task itself is closed on purpose. A leftover run record
# under one of these is history, not an abandoned run.
CLOSED_TASK_STATUSES = {"completed", "cancelled"}


def load_thread(name: str) -> dict:
    config = json.loads(CONFIG.read_text())
    try:
        return config["threads"][name]
    except KeyError:
        raise SystemExit(f"unknown thread: {name}; known: {sorted(config['threads'])}")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    return True


def query_tasks(args: list[str]) -> list[dict]:
    out = subprocess.run(
        [str(PYTHON), str(TASKS_INDEX), "query", *args, "--format", "json", "--limit", "40"],
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


def run_state(task_dir: Path) -> dict | None:
    """Live run of a task, from observed process state rather than child claims."""
    status_path = task_dir / "status.json"
    runner_path = task_dir / ".runner" / "runner.json"
    if not status_path.is_file():
        return None
    try:
        status = json.loads(status_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    pid = None
    if runner_path.is_file():
        try:
            pid = json.loads(runner_path.read_text()).get("pid")
        except (json.JSONDecodeError, OSError):
            pid = None
    alive = pid_alive(int(pid)) if isinstance(pid, int) else False
    # Freshness comes from observed file mtime, never from the child's own
    # timestamp: children routinely write local time into a UTC field, and a
    # long-running owner leaves `status.json` on an old label while progress moves.
    progress_path = task_dir / "progress.json"
    progress = {}
    if progress_path.is_file():
        try:
            payload = json.loads(progress_path.read_text())
        except (json.JSONDecodeError, OSError):
            payload = {}
        progress = {
            "activity": payload.get("activity"),
            "done": payload.get("completed"),
            "total": payload.get("total"),
            "written_minutes_ago": round((time.time() - progress_path.stat().st_mtime) / 60),
        }
    return {
        "progress": progress or None,
        "state": status.get("state"),
        "current_step": status.get("current_step"),
        "updated_at": status.get("updated_at"),
        "runner": status.get("runner"),
        "workflow": status.get("workflow"),
        "pid": pid,
        "process_alive": alive,
        # A run that claims to be running while its process is gone is the case
        # the supervisor must surface, not hide.
        "stale_running": status.get("state") == "running" and not alive,
        # The same hole one step wider: the owner died leaving a non-terminal
        # run record behind. Its frontmatter may still read "planned", which
        # looks exactly like work that was never started — so nothing wakes up
        # and the task sits still. Judge by the run's own record, not the label.
        "abandoned_run": bool(status) and not alive
        and status.get("state") not in TERMINAL_RUN_STATES,
    }


def repo_state(path: str) -> dict:
    repo = Path(path)
    if not (repo / ".git").exists():
        return {"repo": path, "present": False}

    def git(*args: str) -> str:
        out = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True)
        return out.stdout.strip() if out.returncode == 0 else ""

    dirty = [line for line in git("status", "--porcelain").splitlines() if not line.startswith("??")]
    return {
        "repo": path,
        "present": True,
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "head": git("log", "-1", "--format=%h %s"),
        "head_at": git("log", "-1", "--format=%cI"),
        "tracked_dirty": len(dirty),
    }


def build(name: str) -> dict:
    thread = load_thread(name)
    tasks = thread_tasks(thread)
    live, attention = [], []
    for task in tasks:
        task_dir = REPO / task["path"]
        state = run_state(task_dir)
        entry = {"id": task.get("id"), "title": task.get("title"), "status": task.get("status"),
                 "path": task["path"], "run": state}
        if state and (state["process_alive"] or state["stale_running"]):
            live.append(entry)
        abandoned = bool(state) and state["abandoned_run"] and task.get("status") not in CLOSED_TASK_STATUSES
        if task.get("status") in {"blocked", "in_progress"} or abandoned or (state and state["stale_running"]):
            attention.append(entry)
    return {
        "thread": name,
        "title": thread.get("title", name),
        "products": thread.get("products", []),
        "live_runs": live,
        "needs_attention": attention[:12],
        "repos": [repo_state(path) for path in thread.get("repos", [])],
        "task_count": len(tasks),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("thread")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    args = parser.parse_args()
    report = build(args.thread)
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(f"# {report['title']} ({report['thread']}) — задач {report['task_count']}")
    print(f"живых прогонов: {len(report['live_runs'])}")
    for item in report["live_runs"]:
        run = item["run"]
        flag = " ПРОЦЕСС УМЕР" if run["stale_running"] else ""
        line = f"  {item['id']} {run['state']}{flag} — {run['current_step']}"
        if run.get("progress"):
            p = run["progress"]
            line += f"\n      шаг {p['done']}/{p['total']}, запись {p['written_minutes_ago']} мин назад: {p['activity']}"
        print(line)
    print(f"требуют внимания: {len(report['needs_attention'])}")
    for item in report["needs_attention"]:
        print(f"  {item['id']} {item['status']} — {item['title'][:70]}")
    for repo in report["repos"]:
        if not repo["present"]:
            print(f"  репозиторий отсутствует: {repo['repo']}")
            continue
        print(f"  {Path(repo['repo']).name}: {repo['branch']} @ {repo['head']} (грязных {repo['tracked_dirty']})")


if __name__ == "__main__":
    sys.exit(main())
