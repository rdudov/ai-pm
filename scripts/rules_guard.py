#!/usr/bin/env python3
"""Keep temporary plans, operational commands and settings out of `AGENTS.md`.

On 2026-08-12 a newer plan was saved in another CLI thread, a product record
and the tasks, while an older operational instruction in `AGENTS.md` still
looked canonical and ran. Active queues and pauses therefore stay with their
current-state owners, while commands and settings stay with their runtime
owners, instead of becoming durable rules.

Dates, task numbers and measured counts are allowed. Durable rules cite the
observations that caused them; a number alone cannot distinguish that evidence
from a temporary plan. The completed one-time migration from the frozen
predecessor is likewise no longer a live invariant of an evolving rules file.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys


HOME = Path(__file__).resolve().parents[1]
RULES = HOME / "AGENTS.md"

# What may not stand in a rules file, with the owner that should hold it.
TEMPORARY_DIRECTIVES = (
    (re.compile(r"\b\d{3,4}(?:\s*(?:→|->)\s*\d{3,4})+", re.IGNORECASE),
     "активный порядок задач", "портфельный план"),
    (re.compile(r"^\s*(?:#{1,6}\s*)?(?:[-*]\s*)?(?:\*\*)?пауза\b.*"
                r"(?:объявлен|действует\s+до|снята)", re.IGNORECASE),
     "объявленная пауза", "портфельный план или запись решения"),
    (re.compile(r"\bочередь\s+на\s+(?:сегодня|завтра)\s*:", re.IGNORECASE),
     "текущая очередь", "портфельный план"),
    (re.compile(r"\bсейчас\s+в\s+работе\s+задача\s+\d{3,4}\b",
                re.IGNORECASE),
     "активная задача", "портфельный план"),
    (re.compile(r"\bприостановлено\s+до\s+20\d\d-\d\d-\d\d\b",
                re.IGNORECASE),
     "датированная пауза", "портфельный план или запись решения"),
)

# Evidence may quote the incident that caused a durable rule. Such a citation
# describes what happened; it does not make that old queue current again.
EVIDENCE_PREFIX = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?"
    r"(?:повод|наблюдённый\s+(?:повод|случай)|evidence)\s*:",
    re.IGNORECASE,
)

FORBIDDEN = (
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


def guard(path: Path = RULES) -> list[str]:
    """Return operational mechanisms found outside their runtime owner."""
    problems: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        return [f"{path} не читается: {error}"]
    inside_code = False
    evidence_indent: int | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        if line.strip().startswith("```"):
            inside_code = not inside_code
            evidence_indent = None
            continue
        if inside_code:
            continue
        if not line.strip():
            evidence_indent = None
            continue

        evidence_prefix = EVIDENCE_PREFIX.search(line)
        if evidence_prefix:
            prefix = re.match(r"^\s*(?:[-*]\s*)?", line)
            evidence_indent = len(prefix.group(0)) if prefix else 1
            is_evidence = True
        else:
            indentation = len(line) - len(line.lstrip())
            is_evidence = (evidence_indent is not None
                           and indentation >= max(1, evidence_indent))
            if not is_evidence:
                evidence_indent = None
        patterns = FORBIDDEN
        if not is_evidence:
            patterns = TEMPORARY_DIRECTIVES + patterns
        for pattern, what, owner in patterns:
            found = pattern.search(line)
            if found:
                problems.append(
                    f"{path.name}:{number}: {what} «{found.group(0)}» — "
                    f"это владение «{owner}», а не правило: {line.strip()[:70]}")
    return problems


def main() -> int:
    problems = guard()
    for problem in problems:
        print(problem)
    print(f"AGENTS.md: {len(RULES.read_text(encoding='utf-8'))} символов")
    print("чисто" if not problems else f"нарушений: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
