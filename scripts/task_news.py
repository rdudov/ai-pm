"""Durable work news in the existing direction checkpoint, via task_engine.

Observation proposes byte cursors; only observed work or an explicit checked
outcome commits them. This is neither a task queue nor launch authorization.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import uuid

import product_memory


def engine_facts(path: Path, cursors: dict, *, baseline: bool = False) -> dict:
    # process_map_state already exposes this installation's public entrypoints.
    engine = importlib.import_module("task_engine")
    return engine.facts(path, cursors, baseline=baseline)


def observe(report: dict, stored: dict) -> dict:
    first = not stored.get("initialized")
    previous = stored.get("tasks", {})
    members = {str(item["id"]): item["path"] for item in report.get("task_catalog", [])}
    # A membership change must not discard a page already waiting for action.
    members.update({key: value["path"] for key, value in previous.items()
                    if value.get("events") or value.get("error")})
    tasks = {}
    for key, relative in members.items():
        path = product_memory.tasks_repo() / relative
        old = previous.get(key, {})
        cursor = old.get("cursor", {})
        try:
            page = engine_facts(path, cursor, baseline=first and key not in previous)
            receipts = old.get("handled_pending", {})
            pending = [row for row in page["events"] if event_key(key, row) not in receipts]
            tasks[key] = {"path": relative,
                          "cursor": page["cursors"] if not pending else cursor,
                          "proposed": page["cursors"], "events": pending,
                          "run_id": page["run_id"], "run_started": page["run_started"],
                          "run": page["run"],
                          "more": page["more"], "last_outcome": old.get("last_outcome"),
                          "handled_pending": receipts if pending else {}}
        except (ImportError, AttributeError, OSError, ValueError) as error:
            tasks[key] = {**old, "path": relative, "cursor": cursor,
                          "error": f"{path}: {type(error).__name__}: {error}"}
    return {"turn_id": uuid.uuid4().hex,
            "initialized": not any(item.get("error") for item in tasks.values())
            if first else True, "tasks": tasks, "outcomes": {}, "handled": []}


def event_key(task: str, event: dict) -> str:
    return f"task:{task}:{event['stream']}:{event['id']}"


def events(news: dict) -> list[str]:
    result = []
    for task, item in news["tasks"].items():
        if item.get("error"):
            result.append("не удалось прочитать сохранённые исходы: " + item["error"])
        for event in item.get("events", []):
            fact = event["fact"]
            decision = fact.get("published_verdict") or fact.get("decision") or fact.get("outcome") or "refused"
            result.append(f"новый сохранённый исход задачи {task}: {decision}; "
                          f"event_id={event_key(task, event)}; источник {event['source']}; "
                          f"sha256={event['sha256']}")
    return result


def source_digest(path: str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def started_action(outcome: dict, turn: str | None, previous_run: str | None,
                   event: dict, runs: list[dict]) -> bool:
    """An explicit event receipt must match this turn's admitted launch."""
    action = outcome.get("action")
    if (not turn or outcome.get("turn_id") != turn or action not in {"author", "reviewer"}
            or (event.get("decision", event.get("published_verdict")) == "rework" and action != "author")):
        return False
    return any(run.get("id") == outcome.get("run_id")
               and run.get("id") != previous_run and run.get("child_started_at")
               and run.get("owner_turn") == turn and run.get("role") == action
               and ((action == "author" and run.get("grants_write") is True)
                    or (action == "reviewer" and run.get("review_kind") == "technical"))
               for run in runs)


def check(news: dict, after: dict, recorded: dict) -> dict:
    """A mail or SILENT alone never acknowledges action-required news."""
    unresolved = []
    for task, item in news["tasks"].items():
        later = after["tasks"].get(task, {})
        runs = [later.get("run", {})] + [
            {**row["fact"], "id": row["fact"].get("run_id", row["id"])} for row in later.get("events", [])
            if row["stream"] == "runs"]
        pending = []
        for row in item.get("events", []):
            key = event_key(task, row)
            outcome = recorded.get(key)
            valid = False
            if outcome:
                try:
                    if outcome["kind"] == "run_started":
                        valid = started_action(outcome, news.get("turn_id"), item.get("run_id"),
                                               row["fact"], runs)
                    else:
                        valid = (outcome["kind"] in {"impediment", "acceptance"}
                             and bool(outcome["reason"].strip())
                             and (outcome["kind"] == "acceptance" or bool(outcome["next"].strip()))
                             and source_digest(outcome["source"]) == outcome["sha256"])
                except (OSError, ValueError, KeyError):
                    pass
            if valid:
                handled = {"event_id": key, "task": task, "outcome": outcome}
                news["handled"].append(handled)
                item["last_outcome"] = handled
                item.setdefault("handled_pending", {})[key] = handled
            else:
                pending.append(key)
        if item.get("error"):
            pending.append(f"task:{task}:facts_unavailable")
        if not pending:
            # Only this pre-wake page was handled. Facts created by the action
            # remain after its proposed boundary for the following ordinary tick.
            item["cursor"] = item.get("proposed", item["cursor"])
            item["handled_pending"] = {}
        unresolved.extend(pending)
    news["post_check"] = {"resolved": not unresolved, "unresolved": unresolved}
    return news["post_check"]


def save(path: Path, state: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w") as handle:
        handle.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("thread")
    parser.add_argument("--event", required=True)
    parser.add_argument("--kind", choices=("impediment", "acceptance", "run_started"), required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--source", help="Primary checked evidence file.")
    parser.add_argument("--run-id", help="Actual started follow-up run identity.")
    parser.add_argument("--action", choices=("author", "reviewer"))
    parser.add_argument("--next", default="", help="Required return condition for an impediment.")
    args = parser.parse_args()
    if not args.reason.strip() or (args.kind == "impediment" and not args.next.strip()):
        parser.error("name the checked result and the impediment's return condition")
    if args.thread not in product_memory.installation()["threads"]:
        parser.error("unknown thread")
    path = product_memory.HOME / "state" / "threads" / f"{args.thread}.json"
    state = json.loads(path.read_text())
    news = state["task_news"]
    known = {event_key(task, row) for task, item in news["tasks"].items()
             for row in item.get("events", [])}
    if args.event not in known:
        parser.error("event is not in the pending direction checkpoint")
    if args.kind == "run_started":
        if (not args.run_id or not args.action
                or os.environ.get("TASK_AGENT_OWNER_TURN") != news.get("turn_id")):
            parser.error("name run/action from the current owner turn")
        outcome = {"kind": args.kind, "reason": args.reason, "run_id": args.run_id,
                   "action": args.action, "turn_id": news["turn_id"]}
    else:
        if not args.source:
            parser.error("checked outcome requires --source")
        source = str(Path(args.source).resolve(strict=True))
        if not Path(source).is_file() or not Path(source).stat().st_size:
            parser.error("checked evidence must be a nonempty regular file")
        outcome = {"kind": args.kind, "reason": args.reason,
                                   "next": args.next, "source": source,
                                   "sha256": source_digest(source),
                                   "checked_at": datetime.now(timezone.utc).isoformat()}
    news["outcomes"][args.event] = outcome
    save(path, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
