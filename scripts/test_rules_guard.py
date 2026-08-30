"""«В AGENTS.md нет временного плана» — проверкой, а не обещанием."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rules_guard


RULES = """# Продакт-агент

## Роль
Единственная точка контакта пользователя по всем продуктам.

## Уроки
```bash
python3 scripts/lesson.py add --observation ... --owner ...
```
"""
PREDECESSOR = (rules_guard.HOME / "content" / "archive" / "2026-08-12"
               / "AGENTS.md")


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "AGENTS.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_rules_file_of_rules_passes(tmp_path: Path):
    assert rules_guard.guard(write(tmp_path, RULES)) == []


@pytest.mark.parametrize("line, expected", [
    ("Канонический порядок: 836 → 839 → 754.", "активный порядок задач"),
    ("Порядок работ — 836 -> 839 -> 754.", "активный порядок задач"),
    ("ПАУЗА, объявленная пользователем 2026-08-09 — действует до отмены.",
     "объявленная пауза"),
    ("Пауза объявлена 2026-08-09 и снята пользователем.",
     "объявленная пауза"),
    ("Очередь на сегодня: 1330, затем 1331.", "текущая очередь"),
    ("Сейчас в работе задача 1330; до её завершения новых не берём.",
     "активная задача"),
    ("Приостановлено до 2026-09-15.", "датированная пауза"),
    ("Вернуть таймеры: systemctl start product-thread@process.timer.",
     "команда управления службами"),
    ("Запуск: .venv/bin/python skills/task-runner/scripts/task_runner.py start",
     "вызов запускателя задач"),
    ("Задача ведётся с --sandbox-mode danger-full-access.",
     "ключ запускателя задач"),
    ("Повторяется не чаще, чем PRODUCT_OWNER_IDLE_REMIND_SECONDS.",
     "настройка наблюдателя"),
])
def test_what_belongs_to_another_owner_is_refused(tmp_path: Path, line: str,
                                                  expected: str):
    problems = rules_guard.guard(write(tmp_path, RULES + "\n" + line + "\n"))
    assert problems, f"не пойман: {line}"
    assert expected in problems[0]
    # The complaint names the owner, so the reader knows where it goes instead.
    assert "это владение" in problems[0]


def test_a_command_inside_a_fenced_block_is_not_a_plan(tmp_path: Path):
    """The start protocol may show its own commands; a queue may not hide in one."""
    text = RULES + "\n```bash\npython3 scripts/product_memory.py --plan\n```\n"
    assert rules_guard.guard(write(tmp_path, text)) == []


def test_historical_evidence_is_allowed_in_a_durable_rule(tmp_path: Path):
    text = RULES + "\n" + "\n".join([
        "Повод: задача 1223 нашла разрыв 2026-08-20.",
        "Наблюдённый случай: Канонический порядок: 836 → 839 → 754.",
        "- Evidence: Приостановлено до 2026-09-15.",
    ]) + "\n"
    assert rules_guard.guard(write(tmp_path, text)) == []


def test_wrapped_historical_evidence_is_allowed_in_a_durable_rule(tmp_path: Path):
    text = RULES + "\n" + "\n".join([
        "- Evidence: письмо 2026-08-07 —",
        "  «Порядок работ — 836 -> 839 -> 754.»",
    ]) + "\n"
    assert rules_guard.guard(write(tmp_path, text)) == []


def test_the_guard_rejects_the_archived_incident_it_exists_for():
    problems = rules_guard.guard(PREDECESSOR)
    assert any("активный порядок задач" in problem for problem in problems)
    assert any("объявленная пауза" in problem for problem in problems)


def test_the_live_rules_file_is_clean():
    """The one that actually loads on every start."""
    assert rules_guard.guard() == []


def test_repeat_work_rule_requires_full_fallback_and_primary_records():
    text = rules_guard.RULES.read_text(encoding="utf-8")
    assert "нулевой ответ по точной фразе не" in text
    assert "полный поиск по текстам задач" in text
    for name in ("task.md", "findings.md", "verification.md", "sources.md"):
        assert f"`{name}`" in text
    assert "до запуска исполнителя" in text
