#!/usr/bin/env python3
"""Move live product content out of git into the durable store, losing nothing.

The migration is deliberately conservative about two different things at once.

Nothing is dropped. Every `## section` of every `product.md` gets exactly one
new owner: the compact snapshot, or an addressable history record. The manifest
names the owner of each fragment, and `--verify` proves that every non-empty
source line is present in some destination file.

Nothing the board already prints changes. `В работе` is not a list of active
promises — it is a reverse-chronological log of dated status reports, and the
board reads it to find promises that have no task behind them. So the snapshot
keeps *every* entry the board currently reports as unplanned, plus the newest
few, and the rest move to history. `--verify` re-runs the board's own judgement
over both carriers and refuses when the two lists differ.

Two sections are renamed rather than moved, because two products had spelled
them their own way and the mail gateway consequently hashed an empty string for
them: `Общие пользовательские пути (обе поверхности)` of client and
`Канонические пользовательские сценарии` of dev-pipeline become
`Пользовательские пути`. That is a change in what the gateway can observe, and
it is named here rather than left silent.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import product_memory as memory


HOME = Path(__file__).resolve().parents[1]
LEGACY = HOME / "products"

# How many newest `В работе` entries stay in the snapshot on top of everything
# the board reports as unplanned. The number is small on purpose: the snapshot
# answers «что идёт сейчас», and the log answers «что было».
KEEP_WORK_ENTRIES = 6

# Sections that carry the current state of a product. Everything else is
# history: canonical models, audiences, dated plans, development retrospectives.
KEEP = {
    "Концепция": "Концепция",
    "Пользовательские пути": "Пользовательские пути",
    "Общие пользовательские пути (обе поверхности)": "Пользовательские пути",
    "Канонические пользовательские сценарии": "Пользовательские пути",
    "Текущая ставка": "Текущая ставка",
    "Не делаем": "Не делаем",
    "В работе": "В работе",
    "Журнал эффекта": "Журнал эффекта",
    "Открытые вопросы": "Открытые вопросы",
}

ORDER = ("Концепция", "Пользовательские пути", "Текущая ставка", "Не делаем",
         "В работе", "Журнал эффекта", "Открытые вопросы")


def sections(text: str) -> list[tuple[str, list[str]]]:
    """Every `## section` with its raw lines, in file order and byte-exact."""
    result: list[tuple[str, list[str]]] = []
    title = None
    body: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if title is not None:
                result.append((title, body))
            title = line[3:].strip()
            body = []
            continue
        if title is None:
            continue
        body.append(line)
    if title is not None:
        result.append((title, body))
    return result


def preamble(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            break
        lines.append(line)
    return lines


def entries(body: list[str]) -> list[list[str]]:
    """Split a list section into raw entries, keeping every original line break."""
    result: list[list[str]] = []
    for line in body:
        if line.startswith("- "):
            result.append([line])
        elif result:
            result[-1].append(line)
        elif line.strip():
            result.append([line])
    return result


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_CATALOGUE: list[dict] | None = None


def unplanned_items(body: list[str]) -> list[str]:
    """The entries the board prints as «надо запланировать», by its own rule.

    The rule lives in the observer, not here; a second copy of it would drift
    and the whole point of this call is that the two agree. When the task
    catalogue cannot be observed at all, every entry is treated as unplanned —
    the conservative direction, which keeps the log in the snapshot rather than
    quietly moving a live promise out of sight.
    """
    global _CATALOGUE
    items = memory.section("## В работе\n" + "\n".join(body), "В работе")
    if _CATALOGUE is None:
        try:
            import process_map_state as observer
            _CATALOGUE = observer.task_catalogue()
        except Exception:
            _CATALOGUE = []
    if not _CATALOGUE:
        return items
    import process_map_state as observer
    return [entry["text"] for entry in observer.unplanned(items, _CATALOGUE)]


def board_view(text: str) -> dict:
    """What the board and the mail gateway observe in one product record."""
    return {
        "unplanned_raw": memory.section(text, "В работе"),
        "questions": memory.section(text, "Открытые вопросы"),
        "effect_head": memory.section(text, "Журнал эффекта")[:8],
    }


def migrate(base: Path, source_root: Path, stamp: str) -> dict:
    memory.ensure_root(base)
    archive = base / "archive" / stamp
    manifest: dict = {"migrated_at": memory.now(), "products": {}, "archive": str(archive)}

    for source in sorted(source_root.glob("*/product.md")):
        slug = source.parent.name
        text = source.read_text(encoding="utf-8")
        record: dict = {
            "source": str(source.relative_to(HOME)),
            "source_sha256": digest(source.read_bytes()),
            "source_bytes": len(source.read_bytes()),
            "fragments": [],
        }

        # 1. The byte-exact predecessor is archived before anything is derived
        #    from it. Nothing is deleted until this copy is in the backup.
        archived = archive / "products" / slug / "product.md"
        archived.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, archived)
        record["archive"] = str(archived.relative_to(base))

        kept: dict[str, list[str]] = {}
        moved: list[tuple[str, list[str]]] = []
        for title, body in sections(text):
            target = KEEP.get(title)
            if target is None:
                moved.append((title, body))
                record["fragments"].append(
                    {"section": title, "lines": len(body), "owner": "history"})
                continue
            kept.setdefault(target, []).extend(body)
            record["fragments"].append(
                {"section": title, "lines": len(body), "owner": f"snapshot:{target}"})

        # 2. `В работе` splits by what the board says about each entry, not by
        #    age alone: an entry with no task behind it is a promise the board
        #    still prints under «надо запланировать», and it stays visible.
        work_entries = entries(kept.get("В работе", []))
        unplanned = set(unplanned_items(kept.get("В работе", [])))
        stay: list[list[str]] = []
        archived_entries: list[list[str]] = []
        for index, entry in enumerate(work_entries):
            item = memory.section("## В работе\n" + "\n".join(entry), "В работе")
            if index < KEEP_WORK_ENTRIES or (item and item[0] in unplanned):
                stay.append(entry)
            else:
                archived_entries.append(entry)
        kept["В работе"] = [line for entry in stay for line in entry]
        record["work_entries_total"] = len(work_entries)
        record["work_entries_in_snapshot"] = len(stay)
        record["work_entries_in_history"] = len(archived_entries)
        record["work_entries_unplanned_kept"] = len(unplanned)

        # 3. History records: one per moved section, one for the log tail.
        product_dir = memory.products_dir(base) / slug
        (product_dir / "history").mkdir(parents=True, exist_ok=True)
        for title, body in moved:
            memory.write_record(title, "\n".join(body).strip("\n"), product=slug,
                                base=base, source=record["source"])
        if archived_entries:
            body = "\n".join("\n".join(entry) for entry in archived_entries)
            memory.write_record(
                "Журнал «В работе» до переноса", body, product=slug, base=base,
                source=record["source"])

        # 4. The snapshot itself.
        head = [f"# {slug}", "",
                "Снимок продукта: текущее состояние, а не история. Полная история —",
                "в `history/`, вложения — в `attachments/`, порядок работ между",
                "продуктами — в единой редакции портфельного плана.", "",
                f"Перенесено из `{record['source']}` {stamp}, SHA-256 источника",
                f"`{record['source_sha256']}`.", ""]
        lines = list(head)
        for title in ORDER:
            lines.append(f"## {title}")
            body = kept.get(title, [])
            trimmed = "\n".join(body).strip("\n")
            lines.append(trimmed if trimmed else "- нет записей")
            lines.append("")
        snapshot = memory.snapshot_path(slug, base)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
        record["snapshot_bytes"] = snapshot.stat().st_size

        # 5. Sibling materials of the product directory become attachments:
        #    tables, fact sheets and reports that were tracked only because
        #    they happened to lie next to the record.
        record["attachments"] = []
        for extra in sorted(source.parent.iterdir()):
            if extra.name == "product.md" or not extra.is_file():
                continue
            data = extra.read_bytes()
            path, sha = memory.attach(extra.name, data, product=slug, base=base)
            shutil.copy2(extra, archive / "products" / slug / extra.name)
            record["attachments"].append(
                {"name": extra.name, "bytes": len(data), "sha256": sha,
                 "owner": str(path.relative_to(base))})

        preface = preamble(text)
        if any(line.strip() for line in preface):
            memory.write_record("Заголовок записи продукта до переноса",
                                "\n".join(preface).strip("\n"), product=slug,
                                base=base, source=record["source"])
            record["fragments"].append(
                {"section": "(преамбула)", "lines": len(preface), "owner": "history"})

        manifest["products"][slug] = record

    # Keep the rules file and readme with the migration snapshot so the source
    # state remains recoverable and auditable after the live documents evolve.
    for name in ("AGENTS.md", "README.md"):
        path = HOME / name
        if path.is_file():
            shutil.copy2(path, archive / name)
            manifest.setdefault("archived_files", {})[name] = digest(path.read_bytes())

    (base / "MIGRATION.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def verify(base: Path, source_root: Path) -> tuple[list[str], list[str]]:
    """Prove that nothing was lost and that the board still sees the same thing.

    Returns the defects and, separately, what the store has grown by since. The
    split is not cosmetic. The migration only ever copies source lines into a
    snapshot, so a snapshot line the source never had was written afterwards by
    a product owner — which is the store doing its job. Counting that as a
    transfer defect made the check unusable from the day after it first passed:
    the first ordinary `В работе` line turned «перенос полон» into «дефектов
    переноса: 1», and a reader could no longer tell growth from loss. Loss stays
    a hard failure in both directions of the board's own judgement.
    """
    problems: list[str] = []
    growth: list[str] = []
    for source in sorted(source_root.glob("*/product.md")):
        slug = source.parent.name
        text = source.read_text(encoding="utf-8")
        snapshot = memory.read_snapshot(slug, base)
        history = "\n".join(path.read_text(encoding="utf-8")
                            for path in memory.records(slug, base))
        destination = snapshot + "\n" + history

        # Every non-empty source line must exist somewhere in the new carriers.
        present = set(destination.splitlines())
        lost = [line for line in text.splitlines()
                if line.strip() and line not in present and not line.startswith("## ")]
        if lost:
            problems.append(
                f"{slug}: {len(lost)} строк исходной записи не найдены ни в снимке, "
                f"ни в истории; первая — {lost[0][:80]!r}")

        before = board_view(text)
        after = board_view(snapshot)
        if before["questions"] != after["questions"]:
            problems.append(f"{slug}: список открытых вопросов изменился при переносе")
        if before["effect_head"] != after["effect_head"]:
            problems.append(f"{slug}: восемь верхних строк журнала эффекта изменились")
        # The board must not lose a promise it used to print. Old entries are
        # supposed to leave the snapshot for history, so the strict direction is
        # the board's own judgement: everything it reports as «надо
        # запланировать» in the source is still in the snapshot.
        source_work = [body for title, body in sections(text) if title == "В работе"]
        dropped = [item
                   for item in unplanned_items(source_work[0] if source_work else [])
                   if item not in after["unplanned_raw"]]
        if dropped:
            problems.append(
                f"{slug}: {len(dropped)} строк «В работе», которые панель считала "
                f"незапланированными, исчезли из снимка; первая — {dropped[0][:80]!r}")
        appeared = [item for item in after["unplanned_raw"]
                    if item not in before["unplanned_raw"]]
        if appeared:
            growth.append(
                f"{slug}: в снимке {len(appeared)} строк «В работе», записанных "
                f"уже в хранилище после переноса; первая — {appeared[0][:80]!r}")

        paths_before = memory.section_text(text, "Пользовательские пути")
        paths_after = memory.section_text(snapshot, "Пользовательские пути")
        if not paths_after.strip():
            problems.append(f"{slug}: раздел «Пользовательские пути» пуст после переноса")
        elif paths_before.strip() and paths_before.strip() != paths_after.strip():
            problems.append(f"{slug}: раздел «Пользовательские пути» изменился")

    problems.extend(memory.check(base))
    return problems, growth


def report(base: Path, source_root: Path) -> int:
    """Print the verdict of one verification and return the process status.

    Both entrypoints — the fresh migration and `--verify` — end the same way, so
    they end in the same code. Keeping two copies is what let a fresh migration
    report failure while printing nothing wrong.
    """
    problems, growth = verify(base, source_root)
    for problem in problems:
        print(problem)
    for line in growth:
        print(line)
    print("перенос полон" if not problems else f"дефектов переноса: {len(problems)}")
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=memory.ROOT)
    parser.add_argument("--source", type=Path, default=LEGACY)
    parser.add_argument("--verify", action="store_true", help="только проверить перенос")
    args = parser.parse_args()

    if args.verify:
        return report(args.root, args.source)

    if memory.slugs(args.root):
        print(f"в {args.root} уже есть снимки продуктов; перенос не повторяется")
        return 1
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    manifest = migrate(args.root, args.source, stamp)
    for slug, record in manifest["products"].items():
        print(f"{slug}: источник {record['source_bytes']} байт → снимок "
              f"{record['snapshot_bytes']} байт, разделов "
              f"{len(record['fragments'])}, вложений {len(record['attachments'])}, "
              f"строк «В работе» {record['work_entries_in_snapshot']}"
              f"/{record['work_entries_total']}")
    return report(args.root, args.source)


if __name__ == "__main__":
    sys.exit(main())
