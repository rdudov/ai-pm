#!/usr/bin/env python3
"""Keep `AGENTS.md` a rules file, and prove the migration out of it lost nothing.

Two independent checks, because the failure had two halves.

`--guard` refuses a temporary plan living in the rules. On 2026-08-12 a newer
plan was saved in another CLI thread, a product record and the tasks, and the
older one in `AGENTS.md` still looked canonical — so it was the one that ran.
Nothing here judges whether a plan is current; the file simply may not carry
active task numbers, dated pauses, queues or operational launch commands. Those
have their own owners, and an owner that is read at the right moment beats a
rule that outlives its own truth.

`--map` refuses «ничего важного не осталось» as a migration check. Every `##`
section of the frozen predecessor is named with exactly one new owner, and the
owner must actually contain it: verbatim for the sections that moved, and by
their leading rule sentence for the ones that were rewritten shorter.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import product_memory


HOME = Path(__file__).resolve().parents[1]
RULES = HOME / "AGENTS.md"

# What may not stand in a rules file, with the owner that should hold it.
FORBIDDEN = (
    (re.compile(r"\b20\d\d-\d\d-\d\d\b"),
     "датированная запись", "портфельный план или запись решения"),
    (re.compile(r"(?<![\w/.-])\d{3,4}(?![\w/.-])"),
     "номер задачи или счётчик", "портфельный план"),
    (re.compile(r"\bsystemctl\b"),
     "команда управления службами", "документация наблюдателя"),
    (re.compile(r"task_runner\.py|tasks_index\.py|task-agent-tasks-index"),
     "вызов запускателя задач", "docs/handing-work-to-development.md"),
    (re.compile(r"--(?:runner|sandbox-mode|workflow|destination|state-dir|project)\b"),
     "ключ запускателя задач", "docs/handing-work-to-development.md"),
    (re.compile(r"\bPRODUCT_OWNER_IDLE_REMIND_SECONDS\b|\bOnUnitActiveSec\b"
                r"|\bNextElapseUSecRealtime\b"),
     "настройка наблюдателя", "docs/observer.md"),
)

# One line per `##` section of the predecessor: where it went, and how that is
# checked. `verbatim` — the owner contains the section body as it stood.
# `rule` — the owner is the rewritten rules file, checked by these phrases.
INVENTORY: dict[str, dict] = {
    "ПАУЗА, объявленная пользователем 2026-08-09 — СНЯТА ДЛЯ ПРОЦЕССНОГО ПЛАНА 2026-08-10": {
        "owner": "content/decisions/ + content/plan/", "how": "verbatim",
        "note": "временная рамка и очередь: их владелец теперь портфельный план"},
    "Роль": {"owner": "AGENTS.md", "how": "rule",
             "phrases": ["Единственная точка контакта пользователя",
                         "полный владелец приоритета"]},
    "Режим Goal для продуктовой работы": {
        "owner": "AGENTS.md + PRODUCT_OWNER_ROUTING.md", "how": "rule",
        "phrases": ["ведётся как Goal", "штатный маршрутизатор"]},
    "Жёсткий порядок продуктовой работы": {
        "owner": "AGENTS.md", "how": "rule",
        "phrases": ["Зафиксировать потребность пользователя его словами",
                    "сценарий не работает", "Простой при доступной работе"]},
    "Как разговаривать с пользователем": {
        "owner": "content/decisions/ + AGENTS.md", "how": "verbatim",
        "note": "правила остались, разобранные случаи ушли в записи решений"},
    "Записи продуктов": {
        "owner": "content/decisions/ + AGENTS.md", "how": "verbatim",
        "note": "схема записи переписана под снимок, план и историю"},
    "Треды": {"owner": "docs/observer.md", "how": "verbatim"},
    "Правила работы, заданные пользователем": {
        "owner": "AGENTS.md", "how": "rule",
        "phrases": ["Пара «автор — проверяющий» жёсткая",
                    "Cursor настоящую работу не исполняет",
                    "остаток провайдера важнее чередования",
                    "Публичный перенос"]},
    "Как отдавать работу в разработку": {
        "owner": "docs/handing-work-to-development.md", "how": "verbatim"},
    "Уроки": {"owner": "AGENTS.md + content/decisions/", "how": "verbatim"},
    "Границы": {"owner": "AGENTS.md", "how": "rule",
                "phrases": ["не правит код продуктов своими руками",
                            "не добавляет гейтов",
                            "не подтверждает технический факт прозой"]},
}


def sections(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    title, body = None, []
    for line in text.splitlines():
        if line.startswith("## "):
            if title is not None:
                result[title] = "\n".join(body).strip("\n")
            title, body = line[3:].strip(), []
            continue
        if title is not None:
            body.append(line)
    if title is not None:
        result[title] = "\n".join(body).strip("\n")
    return result


def guard(path: Path = RULES) -> list[str]:
    """Everything in the rules file that belongs to another owner."""
    problems: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        return [f"{path} не читается: {error}"]
    inside_code = False
    for number, line in enumerate(text.splitlines(), start=1):
        if line.strip().startswith("```"):
            inside_code = not inside_code
            continue
        if inside_code:
            continue
        for pattern, what, owner in FORBIDDEN:
            found = pattern.search(line)
            if found:
                problems.append(
                    f"{path.name}:{number}: {what} «{found.group(0)}» — "
                    f"это владение «{owner}», а не правило: {line.strip()[:70]}")
    return problems


def archived_rules(base: Path | None = None) -> Path | None:
    base = base or product_memory.root()
    candidates = sorted((base / "archive").glob("*/AGENTS.md")) \
        if (base / "archive").is_dir() else []
    return candidates[-1] if candidates else None


def inventory(base: Path | None = None) -> tuple[list[dict], list[str]]:
    """Every section of the predecessor with its owner, and what failed."""
    base = base or product_memory.root()
    previous = archived_rules(base)
    if previous is None:
        return [], ["замороженного предшественника AGENTS.md нет: "
                    "перенос нечем проверить"]
    before = sections(previous.read_text(encoding="utf-8"))
    rules = RULES.read_text(encoding="utf-8")
    history = "\n".join(path.read_text(encoding="utf-8")
                        for path in product_memory.records(None, base))
    docs = "\n".join(path.read_text(encoding="utf-8")
                     for path in sorted((HOME / "docs").glob("*.md"))) \
        if (HOME / "docs").is_dir() else ""
    carriers = rules + "\n" + history + "\n" + docs

    rows, problems = [], []
    for title, body in before.items():
        plan = INVENTORY.get(title)
        if plan is None:
            problems.append(f"раздел «{title}» не назван ни одному владельцу")
            continue
        row = {"section": title, "chars": len(body), "owner": plan["owner"],
               "how": plan["how"], "note": plan.get("note", "")}
        if plan["how"] == "verbatim":
            flat_carriers = " ".join(carriers.split())
            missing = [line for line in body.splitlines()
                       if line.strip() and " ".join(line.split()) not in flat_carriers]
            row["carried"] = not missing
            if missing:
                problems.append(
                    f"«{title}»: {len(missing)} строк нет ни у одного владельца; "
                    f"первая — {missing[0].strip()[:60]!r}")
        else:
            # Whitespace-insensitive: these files are hard-wrapped prose, and a
            # rule that merely wrapped across two lines is still present.
            flat = " ".join(rules.split())
            absent = [phrase for phrase in plan["phrases"]
                      if " ".join(phrase.split()) not in flat]
            row["carried"] = not absent
            if absent:
                problems.append(f"«{title}»: правило не дошло до AGENTS.md: {absent}")
        rows.append(row)

    for title in INVENTORY:
        if title not in before:
            problems.append(f"опись называет раздел «{title}», которого не было")
    return rows, problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guard", action="store_true",
                        help="в AGENTS.md нет планов, очередей и команд запуска")
    parser.add_argument("--map", action="store_true",
                        help="карта переноса: у каждого фрагмента один владелец")
    parser.add_argument("--json", type=Path, help="записать карту сюда")
    args = parser.parse_args()

    problems: list[str] = []
    if args.map or not args.guard:
        rows, found = inventory()
        problems.extend(found)
        for row in rows:
            mark = "перенесено" if row["carried"] else "НЕ ПЕРЕНЕСЕНО"
            print(f"[{mark}] {row['section']} ({row['chars']} симв.) → "
                  f"{row['owner']} ({row['how']})")
        if args.json:
            args.json.write_text(
                json.dumps({"sections": rows, "problems": found},
                           ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.guard or not args.map:
        found = guard()
        problems.extend(found)
        for problem in found:
            print(problem)
        print(f"AGENTS.md: {len(RULES.read_text(encoding='utf-8'))} символов")

    print("чисто" if not problems else f"нарушений: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
