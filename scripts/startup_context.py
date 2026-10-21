#!/usr/bin/env python3
"""Build one bounded, source-complete context for a background owner wake.

The packet is ephemeral. An event entry reads the shared current plan and only
its own direction's snapshots, observation and goals. Other current sources
stay addressable; dependencies expand reading rather than launching a full
portfolio audit. A manual/daily overview keeps all directions. Historical detail
is addressed only when an event needs it. Serialized packet and complete caller
input retain the outer hard limits from the existing implementation.

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
MAX_RECORD_BYTES = 65_536
# Keep unread bodies at their existing durable addresses. The current decision
# blocks already fill most of a tick's initial input; copying the unread tail
# as well exceeded the engine's turn/start limit. Its existing manifest keeps
# every source, digest and size without advancing the plan cursor.
MAX_POST_CURSOR_BYTES = 0

# Python/JSON character counts, not UTF-8 bytes or provider tokens. Leave room
# for the runtime's instructions/tools below the observed 1 Mi-character limit.
MAX_PACKET_CHARS = 480_000
MAX_PROMPT_CHARS = 600_000
MAX_EVENT_BASE_CHARS = 16_000
SECTION_CHARS = {
    "portfolio_plan": 80_000, "product_snapshots": 80_000,
    "thread_states": 60_000, "model_budgets": 10_000,
    "active_goals": 60_000, "post_cursor_records": 160_000,
}


class InputLimitError(ValueError):
    """Source completeness cannot be preserved within the input bound."""


def require_size(text: str, limit: int, section: str) -> str:
    if len(text) > limit:
        raise InputLimitError(
            f"{section}: {len(text)} символов > {limit}; "
            "полный источник или его манифест не помещается; модель не вызвана")
    return text


def reference(value, source: str, **identity) -> dict:
    """Name a complete original; hashes of projections are explicitly labelled."""
    text = value if isinstance(value, str) else render(value)
    return {"source": source, "manifested": True,
            "projection_sha256": digest_text(text), "projection_chars": len(text),
            "projection_bytes": len(text.encode("utf-8")),
            "read_before_decision": True, **identity}


def file_reference(value, path: Path) -> dict:
    raw = path.read_bytes()
    if value is None:
        return {"source": str(path), "source_sha256": hashlib.sha256(raw).hexdigest(),
                "source_bytes": len(raw), "source_chars": len(raw.decode("utf-8"))}
    return reference(value, str(path), source_sha256=hashlib.sha256(raw).hexdigest(),
                     source_bytes=len(raw))


def fit(value, original: dict, limit: int, section: str):
    candidate = value if len(render(value)) <= limit else original
    require_size(render(candidate), limit, section)
    return candidate


def fit_items(values: dict, originals: dict, limit: int, section: str) -> dict:
    """Bound every item and the whole manifest, including its keys/count."""
    each = limit // max(1, len(values))
    result = {key: fit(value, originals[key], each, section)
              for key, value in values.items()}
    require_size(render(result), limit, section)
    return result


def bounded_packet(value: dict, current_thread: str | None = None,
                   plan: dict | None = None) -> dict:
    """Reuse whole-source manifests; excessive manifest cardinality refuses."""
    value = dict(value)
    plan_path = (product_memory.revisions_dir() / f"{int(plan['revision']):06d}.json"
                 if plan else None)
    plan_source = str(plan_path) if plan_path else "python3 scripts/product_memory.py --plan"
    plan_ref = (file_reference(value["portfolio_plan"], plan_path) if plan_path else
                reference(value["portfolio_plan"], plan_source))
    if plan_path:
        plan_ref["revision"] = plan["revision"]
        plan_ref["accepted_at"] = plan["accepted_at"]
    sources = {"portfolio_plan": plan_ref}
    value["portfolio_plan"] = fit(value["portfolio_plan"], plan_ref,
                                  SECTION_CHARS["portfolio_plan"], "portfolio_plan")
    snapshots = value["product_snapshots"]
    refs = {slug: reference(
                view, str(product_memory.root() / view["source"].removeprefix("content/")),
                source_sha256=view["source_sha256"])
            for slug, view in snapshots.items()}
    sources["product_snapshots"] = {slug: ref["source"] for slug, ref in refs.items()}
    if current_thread:
        config = product_memory.installation().get("threads", {}).get(current_thread, {})
        relevant = config.get("products", [])
        snapshots = {slug: view if slug in relevant else refs[slug]
                     for slug, view in snapshots.items()}
    value["product_snapshots"] = fit_items(
        snapshots, refs, SECTION_CHARS["product_snapshots"], "product_snapshots")
    reports = value["thread_states"]
    refs = {name: reference(report, f"python3 scripts/thread_state.py {name}")
            for name, report in reports.items()}
    sources["thread_states"] = {name: ref["source"] for name, ref in refs.items()}
    value["thread_states"] = fit_items(reports, refs, SECTION_CHARS["thread_states"], "thread_states")
    commands = {"codex": "python3 scripts/codex_budget.py",
                "route": "python3 scripts/claude_product_owner.py --status"}
    sources["model_budgets"] = commands
    refs = {name: reference(budget, commands[name])
            for name, budget in value["model_budgets"].items()}
    value["model_budgets"] = fit_items(
        value["model_budgets"], refs, SECTION_CHARS["model_budgets"], "model_budgets")
    goals = {str(index): goal for index, goal in enumerate(value["active_goals"])}
    refs = {index: file_reference(goal, product_goal.root() / f"{goal['id']}.json")
            for index, goal in goals.items()}
    sources["active_goals"] = [ref["source"] for ref in refs.values()]
    value["active_goals"] = list(fit_items(
        goals, refs, SECTION_CHARS["active_goals"], "active_goals").values())
    # These records are already whole-source manifests. Never drop their tail.
    require_size(render(value["post_cursor_records"]),
                 SECTION_CHARS["post_cursor_records"], "post_cursor_records")
    # Inline projections still name their originals; records already carry
    # their own addresses. The final cap also counts this source catalog.
    value["contract"] = {**value["contract"], "sources": sources}
    require_size(render(value), MAX_PACKET_CHARS, "startup packet")
    return value


def checked_prompt(text: str, event_base: str | None = None) -> str:
    # The router prepends this exact language paragraph before codex exec.
    if event_base is not None:
        rules = (claude_product_owner.HOME / "AGENTS.md").read_text(encoding="utf-8")
        require_size(rules + claude_product_owner.plain_russian.as_paragraph() + "\n\n" + event_base,
                     MAX_EVENT_BASE_CHARS, "event base including loaded AGENTS and language")
    require_size(claude_product_owner.plain_russian.as_paragraph() + "\n\n" + text,
                 MAX_PROMPT_CHARS, "complete prompt including language wrapper")
    return text


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
        if product_memory.section_heading(lines[index], title) is not None:
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
    # Completed repair detail is historical; only unresolved repairs belong to
    # the event's current decision. Their status remains in the goal's index.
    current = {**goal, "correctives": [item for item in goal.get("correctives", [])
                                      if not product_goal.settlement(item)],
               "settled_correctives": {}}
    projected = product_goal.projection(current)
    projected["history"] = str(product_goal.root() / "history" / str(goal.get("id")))
    signals = projected.pop("signals", [])
    projected["signal_summary"] = {
        "count": len(signals),
        "codes": sorted({item["code"] for item in signals}),
        "sha256": digest_text(json.dumps(signals, ensure_ascii=False, sort_keys=True)),
        "source": projected["src"],
    }
    return projected


def post_cursor(plan: dict, frozen_at: datetime | None = None,
                products: set[str] | None = None) -> list[dict]:
    """Unread prefix boundary survives publication and selective event reading."""
    if not plan:
        return []
    cursor = datetime.fromisoformat(plan.get("read_through", plan["accepted_at"])).timestamp()
    ceiling = frozen_at.timestamp() if frozen_at else datetime.now().timestamp()
    paths: set[Path] = set()
    root = product_memory.root()
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if (not path.is_file() or path.name == "snapshot.md"
                or relative.parts[0] in {"plan", "goals"}
                or "current-state" in relative.parts
                or "attachments" in relative.parts
                or path.suffix.lower() not in {".md", ".json", ".jsonl", ".txt"}):
            continue
        if (products is not None and len(relative.parts) > 2
                and relative.parts[0] == "products"
                and relative.parts[1] not in products):
            continue
        stamp = path.stat().st_mtime
        if cursor < stamp <= ceiling:
            paths.add(path)
    config = product_memory.installation()
    task_root = Path(config.get("tasks_repo") or "") / "tasks"
    task_ids = {task for link in plan.get("outcome_links", [])
                for task in link.get("tasks", [])}
    task_ids.update(plan.get("unread_tasks", []))
    if task_root.is_dir():
        for task_id in task_ids:
            for directory in task_root.glob(f"{int(task_id)}-*"):
                for path in directory.rglob("*"):
                    if (path.is_file() and ".runner" not in path.parts
                            and path.name in TASK_RECORD_NAMES
                            and cursor < path.stat().st_mtime <= ceiling):
                        paths.add(path)
    records = []
    used = 0
    for path in sorted(paths, key=str):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise InputLimitError(f"не прочитан обязательный источник {path}: {error}") from error
        size = len(text.encode("utf-8"))
        if size > MAX_RECORD_BYTES or used + size > MAX_POST_CURSOR_BYTES:
            records.append({
                "source": str(path), "manifested": True, "bytes": size, "chars": len(text),
                "sha256": digest_text(text),
                "reason": "post-cursor input bound; open this durable original addressably",
            })
            continue
        records.append({"source": str(path), "text": text})
        used += size
    return records


def packet(current_thread: tuple[str, dict] | None = None) -> dict:
    try:
        plan = product_memory.current_plan()
    except product_memory.ContentError as error:
        raise InputLimitError(f"Портфельный план не читается: {error}; модель не вызвана") from error
    else:
        plan_text = product_memory.plan_text(plan)
    config = product_memory.installation().get("threads", {})
    selected = ({current_thread[0]} if current_thread else set(config))
    relevant_products = (set(config.get(current_thread[0], {}).get("products", []))
                         if current_thread else set(product_memory.slugs()))
    reports = {}
    for name in selected:
        reports[name] = (current_thread[1] if current_thread and current_thread[0] == name
                         else thread_state.build(name))
    observation = claude_product_owner.inspect_observation("print")
    if current_thread:
        name, report = current_thread
        plan_path = (product_memory.revisions_dir() / f"{int(plan['revision']):06d}.json"
                     if plan else None)
        # The initial instruction carries shared priorities/pauses and addresses.
        # Current event detail is actually short on disk, and read only as needed.
        value = {
            "contract": {
                "kind": "event-current-state-v3", "plan_available": plan is not None,
                "reading": "Event sources; expand by dependency. "
                           "Manifests do not acknowledge reading.",
                "other_sources": {
                    "snapshots": str(product_memory.root() / "products/*/snapshot.md"),
                    "goals": str(product_goal.root() / "<id>.json"),
                    "threads": "python3 scripts/thread_state.py <thread>",
                },
            },
            "portfolio_plan": ({"revision": plan["revision"],
                                "headline": plan.get("headline", ""),
                                "paused": plan.get("paused", []),
                                **file_reference(None, plan_path)}
                               if plan else plan_text),
            "product_snapshots": {slug: file_reference(None, product_memory.snapshot_path(slug))
                                  for slug in sorted(relevant_products)},
            "thread_states": {name: reference(report, f"python3 scripts/thread_state.py {name}")},
            "model_budgets": {"codex": codex_budget.latest(),
                              "route": {"engine": observation.route.engine,
                                        "model": observation.route.model,
                                        "reason": observation.route.reason}},
            "active_goals": [{"id": goal["id"], "state": goal["state"],
                              "main_task": goal.get("main_task")}
                             for goal in product_goal.active(name)],
            "post_cursor_records": {
                "source": f"python3 scripts/startup_context.py --thread {name} --records",
                "read_through": plan.get("read_through", plan["accepted_at"]) if plan else None,
                "reading": "Enumerate event-related unread sources; neither this address "
                           "nor publication advances their boundary.",
            },
        }
        require_size(render(value), MAX_PACKET_CHARS, "startup packet")
        return value
    value = {
        "contract": {
            "kind": "event-current-state-v3" if current_thread else "current-overview-v3",
            "plan_available": plan is not None,
            "rules": "AGENTS.md is already loaded by the model runtime; do not read it twice",
            "historical_detail": (
                "Read event materials and the shared current plan, priorities and pauses. "
                "Expand reading by exact address when a dependency concerns another product, "
                "including a paused product. History is optional unless this event needs it. "
                "A manifest or selective reading never advances read_through. "
                "Inline current decisions are verbatim; open relevant manifested originals."
            ),
            "other_sources": {
                "snapshots": {slug: str(product_memory.snapshot_path(slug))
                              for slug in product_memory.slugs() if slug not in relevant_products},
                "threads": {name: f"python3 scripts/thread_state.py {name}"
                            for name in config if name not in selected},
                "goals": "python3 scripts/product_goal.py list",
            },
        },
        "portfolio_plan": plan_text,
        "product_snapshots": {slug: snapshot_view(slug) for slug in sorted(relevant_products)},
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
        "active_goals": [goal_view(goal) for goal in product_goal.active(
            current_thread[0] if current_thread else None)],
        "post_cursor_records": post_cursor(plan, products=relevant_products if current_thread else None)
                               if plan else [],
    }
    return bounded_packet(value, current_thread[0] if current_thread else None, plan)


def render(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread")
    parser.add_argument("--records", action="store_true")
    args = parser.parse_args()
    if args.records:
        config = product_memory.installation().get("threads", {})
        if args.thread and args.thread not in config:
            parser.error("unknown thread")
        products = set(config[args.thread].get("products", [])) if args.thread else None
        value = post_cursor(product_memory.current_plan(), products=products)
        require_size(render(value), SECTION_CHARS["post_cursor_records"], "post-cursor records")
        print(render(value))
    else:
        print(render(packet((args.thread, thread_state.build(args.thread)) if args.thread else None)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
