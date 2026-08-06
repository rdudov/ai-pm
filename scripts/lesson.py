#!/usr/bin/env python3
"""Capture and triage lessons so they end up in an owner, not in a dump.

A lesson is captured with three mandatory parts — what was observed, what it
cost, and what rule follows — and is triaged into exactly one owner. A lesson
that cannot name an owner is an observation, and observations are discarded.

  lesson.py add --observation ... --cost ... --rule ...
  lesson.py list
  lesson.py close <id> --owner <path-or-name> --note "как именно применено"
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
INBOX = HOME / "lessons" / "inbox.json"
ARCHIVE = HOME / "lessons" / "applied.md"


def load() -> list[dict]:
    if not INBOX.is_file():
        return []
    try:
        return json.loads(INBOX.read_text())
    except json.JSONDecodeError:
        return []


def save(items: list[dict]) -> None:
    INBOX.parent.mkdir(parents=True, exist_ok=True)
    INBOX.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n")


def add(args: argparse.Namespace) -> int:
    items = load()
    identifier = max((item["id"] for item in items), default=0) + 1
    items.append({
        "id": identifier,
        "date": date.today().isoformat(),
        "observation": args.observation,
        "cost": args.cost,
        "rule": args.rule,
        "owner_candidate": args.owner,
    })
    save(items)
    print(f"урок {identifier} записан; он останется открытым, пока не будет применён в владельце")
    return 0


def show(args: argparse.Namespace) -> int:
    items = load()
    if not items:
        print("входящих уроков нет")
        return 0
    for item in items:
        print(f"[{item['id']}] {item['date']}")
        print(f"    наблюдение: {item['observation']}")
        print(f"    цена:       {item['cost']}")
        print(f"    правило:    {item['rule']}")
        if item.get("owner_candidate"):
            print(f"    кандидат:   {item['owner_candidate']}")
    stale = [item for item in items if (date.today() - date.fromisoformat(item["date"])).days > 7]
    if stale:
        print(f"\nстарше недели: {[item['id'] for item in stale]} — либо применить, либо выбросить")
    return 0


def close(args: argparse.Namespace) -> int:
    items = load()
    match = [item for item in items if item["id"] == args.id]
    if not match:
        print(f"урока {args.id} во входящих нет")
        return 1
    lesson = match[0]
    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    with ARCHIVE.open("a", encoding="utf-8") as handle:
        handle.write(
            f"\n## {lesson['date']} — {lesson['rule']}\n\n"
            f"- Наблюдение: {lesson['observation']}\n"
            f"- Цена: {lesson['cost']}\n"
            f"- Владелец: {args.owner}\n"
            f"- Как применено: {args.note}\n"
        )
    save([item for item in items if item["id"] != args.id])
    print(f"урок {args.id} применён в {args.owner} и убран из входящих")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    capture = sub.add_parser("add", help="записать урок в момент, когда он случился")
    capture.add_argument("--observation", required=True, help="что именно наблюдалось")
    capture.add_argument("--cost", required=True, help="чего это стоило: время, авария, доверие")
    capture.add_argument("--rule", required=True, help="какое правило из этого следует")
    capture.add_argument("--owner", help="предполагаемый владелец: скилл, док, правило, память")
    capture.set_defaults(func=add)

    listing = sub.add_parser("list", help="показать входящие уроки")
    listing.set_defaults(func=show)

    applied = sub.add_parser("close", help="закрыть урок применением в владельце")
    applied.add_argument("id", type=int)
    applied.add_argument("--owner", required=True, help="куда именно внесено изменение")
    applied.add_argument("--note", required=True, help="что конкретно изменено")
    applied.set_defaults(func=close)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
