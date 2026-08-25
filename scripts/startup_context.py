#!/usr/bin/env python3
"""Build one bounded, source-complete context for a background owner wake.

The packet is ephemeral. It neither caches nor copies product state: every
invocation reads the current plan, every snapshot, every thread, both routing
budgets, every active goal and every textual record after the plan cursor.
Historical effect entries and repeated goal-signal explanations remain at their
durable addresses and are represented by a count and digest; current decision
fields stay verbatim.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import claude_product_owner
import codex_budget
import product_goal
import product_memory
import thread_state


HISTORICAL_SNAPSHOT_SECTION = "Журнал эффекта"
TASK_RECORD_NAMES = {
    "task.md", "plan.md", "findings.md", "verification.md", "status.json",
    "progress.json", "manifest.json",
}


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def exact_section(text: str, title: str) -> str:
    """Section body without whitespace rewriting."""
    lines = text.splitlines(keepends=True)
    start = None
    for index, line in enumerate(lines):
        if line.rstrip("\r\n") == f"## {title}":
            start = index + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return "".join(lines[start:end]).strip("\r\n")


def snapshot_view(slug: str) -> dict:
    """Current snapshot decisions verbatim; dated effect history by address."""
    text = product_memory.read_snapshot(slug)
    sections = {}
    for title in product_memory.SNAPSHOT_SECTIONS:
        body = exact_section(text, title)
        if title == HISTORICAL_SNAPSHOT_SECTION:
            sections[title] = {
                "entries": len(product_memory.section(text, title)),
                "sha256": digest_text(body),
                "history": f"content/products/{slug}/history/",
            }
        else:
            sections[title] = body
    return {
        "source": f"content/products/{slug}/snapshot.md",
        "source_sha256": digest_text(text),
        "sections": sections,
    }


def goal_view(goal: dict) -> dict:
    """Keep the active decision; collapse only repeated historical evidence."""
    projected = product_goal.projection(goal)
    signals = projected.pop("signals", [])
    projected["signal_summary"] = {
        "count": len(signals),
        "codes": sorted({item["code"] for item in signals}),
        "sha256": digest_text(json.dumps(signals, ensure_ascii=False, sort_keys=True)),
        "source": projected["src"],
    }
    return projected


def post_cursor(plan: dict, frozen_at: datetime | None = None) -> list[dict]:
    """Text records newer than the existing accepted-at cursor, as in task 1226."""
    if not plan:
        return []
    cursor = datetime.fromisoformat(plan["accepted_at"]).timestamp()
    ceiling = frozen_at.timestamp() if frozen_at else datetime.now().timestamp()
    paths: set[Path] = set()
    root = product_memory.root()
    for path in root.rglob("*"):
        if (not path.is_file() or path.name == "snapshot.md"
                or path.suffix.lower() not in {".md", ".json", ".jsonl", ".txt"}):
            continue
        stamp = path.stat().st_mtime
        if cursor < stamp <= ceiling:
            paths.add(path)
    config = product_memory.installation()
    task_root = Path(config.get("tasks_repo") or "") / "tasks"
    task_ids = {task for link in plan.get("outcome_links", [])
                for task in link.get("tasks", [])}
    if task_root.is_dir():
        for task_id in task_ids:
            for directory in task_root.glob(f"{int(task_id)}-*"):
                for path in directory.rglob("*"):
                    if (path.is_file() and ".runner" not in path.parts
                            and path.name in TASK_RECORD_NAMES
                            and cursor < path.stat().st_mtime <= ceiling):
                        paths.add(path)
    records = []
    for path in sorted(paths, key=str):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        records.append({"source": str(path), "text": text})
    return records


def packet(current_thread: tuple[str, dict] | None = None) -> dict:
    plan = product_memory.current_plan()
    reports = {}
    for name in product_memory.installation().get("threads", {}):
        reports[name] = (current_thread[1] if current_thread and current_thread[0] == name
                         else thread_state.build(name))
    observation = claude_product_owner.inspect_observation()
    return {
        "contract": {
            "kind": "bounded-background-startup-v1",
            "rules": "AGENTS.md is already loaded by the model runtime; do not read it twice",
            "historical_detail": (
                "Counts, hashes and source addresses below manifest mandatory sources; "
                "open historical detail only when the observed event requires it"
            ),
        },
        "portfolio_plan": product_memory.plan_text(plan),
        "product_snapshots": {slug: snapshot_view(slug) for slug in product_memory.slugs()},
        "thread_states": reports,
        "model_budgets": {
            "codex": codex_budget.latest(),
            "route": {
                "engine": observation.route.engine,
                "model": observation.route.model,
                "reason": observation.route.reason,
                "claude_usage": observation.usage,
                "codex_budget": observation.codex_budget,
            },
        },
        "active_goals": [goal_view(goal) for goal in product_goal.active()],
        "post_cursor_records": post_cursor(plan),
    }


def render(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def main() -> int:
    print(render(packet()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
