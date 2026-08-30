"""What the durable product store must hold under two owners writing at once."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import product_memory as memory


SNAPSHOT = """# Продукт

## Концепция
Одна фраза.

## Пользовательские пути
- путь работает

## Текущая ставка
- ставка

## Не делаем
- ничего

## В работе
- первая строка

## Журнал эффекта
- 2026-08-12 — что-то поставлено

## Открытые вопросы
- вопрос
"""


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    base = memory.ensure_root(tmp_path / "content")
    (base / "products" / "demo").mkdir(parents=True)
    (base / "products" / "demo" / memory.SNAPSHOT).write_text(SNAPSHOT, encoding="utf-8")
    return base


def test_missing_root_is_not_an_empty_store(tmp_path: Path):
    absent = tmp_path / "nothing"
    assert not memory.available(absent)
    assert memory.slugs(absent) == []
    problems = memory.check(absent)
    assert problems and "недоступен" in problems[0]


def test_sections_read_the_same_way_the_board_reads_them(store: Path):
    text = memory.read_snapshot("demo", store)
    assert memory.section(text, "В работе") == ["первая строка"]
    assert memory.section(text, "Открытые вопросы") == ["вопрос"]
    assert memory.section_text(text, "Пользовательские пути").strip() == "- путь работает"


def test_work_line_is_appended_inside_its_own_section(store: Path):
    memory.append_work_line("demo", "вторая  строка", store)
    text = memory.read_snapshot("demo", store)
    assert memory.section(text, "В работе") == ["первая строка", "вторая строка"]
    # The neighbouring section keeps its own items: the append must not spill.
    assert memory.section(text, "Журнал эффекта") == ["2026-08-12 — что-то поставлено"]


def test_work_section_prompt_keeps_events_in_history():
    prompt = memory.SECTION_PROMPTS["В работе"]
    assert "Только текущие обещания" in prompt
    assert "события и прежние состояния пишутся в history/" in prompt


def test_append_refuses_when_the_section_is_absent(store: Path):
    path = memory.snapshot_path("demo", store)
    path.write_text("# Продукт\n\n## Концепция\nбез раздела\n", encoding="utf-8")
    with pytest.raises(memory.ContentError):
        memory.append_work_line("demo", "строка", store)


def test_two_records_in_the_same_second_both_survive(store: Path):
    first = memory.write_record("Решение пользователя", "остановить Продукт",
                                product="demo", base=store)
    second = memory.write_record("Решение пользователя", "продолжить платформа",
                                 product="demo", base=store)
    assert first != second
    assert len(memory.records("demo", store)) == 2
    assert "остановить Продукт" in first.read_text(encoding="utf-8")
    assert "продолжить платформа" in second.read_text(encoding="utf-8")


def test_identical_body_twice_does_not_overwrite_the_first(store: Path):
    first = memory.write_record("Одно и то же", "тело", product="demo", base=store)
    second = memory.write_record("Одно и то же", "тело", product="demo", base=store)
    assert first != second
    assert len(memory.records("demo", store)) == 2


def test_attachment_keeps_bytes_and_publishes_its_digest(store: Path):
    data = bytes(range(256)) * 8
    path, digest = memory.attach("payload.bin", data, product="demo", base=store)
    assert path.read_bytes() == data
    assert digest == hashlib.sha256(data).hexdigest()
    sidecar = path.parent / "payload.bin.sha256"
    assert sidecar.read_text(encoding="utf-8").split()[0] == digest
    assert memory.check(store) == []


def test_attachment_never_silently_replaces_stored_bytes(store: Path):
    memory.attach("payload.bin", b"first", product="demo", base=store)
    with pytest.raises(memory.ContentError):
        memory.attach("payload.bin", b"second", product="demo", base=store)


def test_corrupted_attachment_is_reported_not_ignored(store: Path):
    path, _ = memory.attach("payload.bin", b"first", product="demo", base=store)
    path.write_bytes(b"tampered")
    problems = memory.check(store)
    assert any("SHA-256 не совпал" in problem for problem in problems)


def test_no_plan_is_stated_as_no_plan(store: Path):
    assert memory.current_plan(store) is None
    assert "Портфельного плана нет" in memory.plan_text(memory.current_plan(store))


def test_plan_revisions_are_immutable_and_numbered(store: Path):
    memory.publish_plan({"headline": "первый", "now": ["839"]}, base=store,
                        expect_revision=0)
    memory.publish_plan({"headline": "второй", "now": ["1095"]}, base=store,
                        expect_revision=1)
    plan = memory.current_plan(store)
    assert plan["revision"] == 2
    assert plan["replaces"] == 1
    assert plan["headline"] == "второй"
    first = json.loads((memory.revisions_dir(store) / "000001.json").read_text())
    assert first["headline"] == "первый"


def test_second_owner_merging_from_a_stale_base_is_refused(store: Path):
    memory.publish_plan({"headline": "первый"}, base=store, expect_revision=0)
    memory.publish_plan({"headline": "второй"}, base=store, expect_revision=1)
    with pytest.raises(memory.PlanConflict):
        memory.publish_plan({"headline": "третий"}, base=store, expect_revision=1)
    # The refusal loses nothing: the loser re-reads and publishes on top.
    memory.publish_plan({"headline": "третий"}, base=store, expect_revision=2)
    assert memory.current_plan(store)["headline"] == "третий"


def test_plan_text_names_the_edition_it_replaces(store: Path):
    memory.publish_plan({"headline": "цель", "now": ["1095"], "paused": ["Продукт"],
                         "grounds": ["CLI 2026-08-12"]}, base=store)
    text = memory.plan_text(memory.current_plan(store))
    assert "Редакция: 1" in text
    assert "На паузе:\n  - Продукт" in text
    assert "*" not in text and "=" not in text


def test_plan_publishes_and_prints_explicit_outcome_links(store: Path):
    memory.publish_plan({"headline": "цель", "now": ["результат"],
                         "next": ["переход"],
                         "outcome_links": [{"now": 1, "next": [1], "tasks": [1095],
                                            "goals": ["0002"]}]},
                        base=store)
    plan = memory.current_plan(store)
    assert plan["outcome_links"][0]["goals"] == ["0002"]
    assert "now 1 → next [1]; tasks [1095]; goals ['0002']" in memory.plan_text(plan)


@pytest.mark.parametrize("links", [
    [{"now": 2, "next": []}],
    [{"now": 1, "next": [2]}],
    [{"now": 1, "next": []}, {"now": 1, "next": []}],
])
def test_plan_refuses_broken_outcome_links_before_publishing(store: Path, links: list):
    with pytest.raises(memory.ContentError):
        memory.publish_plan({"headline": "цель", "now": ["результат"],
                             "next": ["переход"], "outcome_links": links},
                            base=store)
    assert memory.current_plan(store) is None


def test_checksums_cover_every_stored_file(store: Path):
    memory.write_record("Решение", "тело", product="demo", base=store)
    memory.attach("payload.bin", b"bytes", product="demo", base=store)
    sums = memory.checksums(store)
    assert "products/demo/snapshot.md" in sums
    assert any(name.startswith("products/demo/history/") for name in sums)
    assert "products/demo/attachments/payload.bin" in sums
    assert memory.LOCK not in sums


def test_snapshot_missing_a_required_section_is_a_defect(store: Path):
    path = memory.snapshot_path("demo", store)
    path.write_text(SNAPSHOT.replace("## Открытые вопросы", "## Прочее"),
                    encoding="utf-8")
    problems = memory.check(store)
    assert any("Открытые вопросы" in problem for problem in problems)


def test_an_edited_retired_monolith_is_reported_not_synced_back(store: Path,
                                                                tmp_path: Path,
                                                                monkeypatch):
    """The rollback copy may not quietly become a second editable truth."""
    monkeypatch.setattr(memory, "HOME", tmp_path)
    legacy = memory.HOME / "products" / "demo" / "product.md"
    manifest = {"products": {"demo": {
        "source": "products/demo/product.md",
        "source_sha256": hashlib.sha256(b"as migrated").hexdigest()}}}
    (store / "MIGRATION.json").write_text(json.dumps(manifest), encoding="utf-8")

    # No file on disk: nothing to diverge from, and no invented complaint.
    assert memory.legacy_divergence(store) == []

    legacy.parent.mkdir(parents=True, exist_ok=True)
    try:
        legacy.write_text("as migrated", encoding="utf-8")
        assert memory.legacy_divergence(store) == []
        legacy.write_text("edited by habit", encoding="utf-8")
        problems = memory.legacy_divergence(store)
        assert problems and "изменена после переноса" in problems[0]
    finally:
        legacy.unlink(missing_ok=True)
        legacy.parent.rmdir()


def test_concurrent_appends_from_separate_processes_keep_both_lines(store: Path):
    """Two owners, two real processes, one snapshot section."""
    script = (
        "import sys;"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parent)!r});"
        "import product_memory as m;"
        f"m.append_work_line('demo', sys.argv[1], m.Path({str(store)!r}))"
    )
    import subprocess
    processes = [subprocess.Popen([sys.executable, "-c", script, f"строка {index}"])
                 for index in range(6)]
    for process in processes:
        assert process.wait() == 0
    items = memory.section(memory.read_snapshot("demo", store), "В работе")
    assert len(items) == 7
    for index in range(6):
        assert f"строка {index}" in items


# --- migration verification -------------------------------------------------
#
# `--verify` must stay usable after the day it first passes. A line written into
# the store afterwards is growth, not a transfer defect; a promise the board
# printed and the snapshot lost is a defect.

import migrate_product_content as migration  # noqa: E402


def _legacy(tmp_path: Path, text: str) -> Path:
    source = tmp_path / "products" / "demo"
    source.mkdir(parents=True)
    (source / "product.md").write_text(text, encoding="utf-8")
    return tmp_path / "products"


def test_a_line_written_after_the_migration_is_growth_and_not_a_defect(
        tmp_path: Path, store: Path):
    source_root = _legacy(tmp_path, SNAPSHOT)
    memory.append_work_line("demo", "строка, записанная уже в хранилище", store)

    problems, growth = migration.verify(store, source_root)

    assert problems == []
    assert len(growth) == 1
    assert "после переноса" in growth[0]


def test_a_promise_the_board_printed_and_the_snapshot_lost_is_a_defect(
        tmp_path: Path, store: Path, monkeypatch: pytest.MonkeyPatch):
    # An unobservable task catalogue means «treat every entry as unplanned»,
    # which is the conservative direction the migration itself relies on.
    monkeypatch.setattr(migration, "_CATALOGUE", [])
    source_root = _legacy(tmp_path, SNAPSHOT)
    path = memory.snapshot_path("demo", store)
    path.write_text(path.read_text(encoding="utf-8").replace("- первая строка", "- нет записей"),
                    encoding="utf-8")

    problems, _ = migration.verify(store, source_root)

    assert any("исчезли из снимка" in problem for problem in problems)


def test_a_clean_fresh_migration_exits_zero_through_the_real_entrypoint(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    """The migration itself, not `--verify`, is what a first run actually calls."""
    source_root = _legacy(tmp_path, SNAPSHOT)
    # The migration names its sources relative to the checkout it runs in.
    monkeypatch.setattr(migration, "HOME", tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "migrate_product_content.py",
        "--root", str(tmp_path / "content"),
        "--source", str(source_root),
    ])

    status = migration.main()

    assert status == 0
    assert "перенос полон" in capsys.readouterr().out
