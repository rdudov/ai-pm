#!/usr/bin/env python3
"""Durable owner of live product content: outside git, inside the backup.

Git owns the portable core of the product owner — this module, the schema it
writes, the empty templates, the migration and the tests. It does not own the
live content of one installation: the product snapshots, the portfolio plan, the
decisions and the attachments of this particular user.

The reason is observed, not stylistic. Product work and a technical review
candidate shared one carrier — the tracked worktree — so saving a discussion
changed the evidence base of a review that had nothing to do with it. That is
how review of 839 was refused. Moving the content to an ignored root makes
`git status --short` blind to product work, while borg keeps the only copy safe.

The layout is a set of independent files, never one growing monolith:

    content/
      products/<slug>/snapshot.md          compact current state of one product
      products/<slug>/history/<record>.md  addressable decisions and reports
      products/<slug>/attachments/<name>   bytes, with a .sha256 beside them
      plan/revisions/<000042>.json         one immutable file per revision
      decisions/<record>.md                cross-product decisions
      archive/<date>/…                     the frozen predecessors

Two product owners write at once, so nothing here is a read-modify-write of a
shared file except under an explicit lock, and a new plan revision is claimed
with O_EXCL: the loser of the race re-reads and merges instead of overwriting.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Iterator


HOME = Path(__file__).resolve().parents[1]

# The environment override exists for the tests and for a restore check into a
# temporary directory. Production has exactly one root and it is not a symlink
# to the tracked tree.
ROOT = Path(os.environ.get("PRODUCT_OWNER_CONTENT") or (HOME / "content"))

SNAPSHOT = "snapshot.md"
LOCK = ".lock"

# The sections a snapshot is allowed to carry. Everything dated, technical or
# historical belongs to `history/`, which is why the list is short and closed:
# the snapshot is what a starting product owner must read, not what is known.
SNAPSHOT_SECTIONS = (
    "Концепция",
    "Пользовательские пути",
    "Текущая ставка",
    "Не делаем",
    "В работе",
    "Журнал эффекта",
    "Открытые вопросы",
)

WORK_SECTION = "В работе"


class ContentError(RuntimeError):
    """The store could not answer, and must not be mistaken for an empty store."""


class PlanConflict(ContentError):
    """Another owner published a revision from the same base."""


def root() -> Path:
    return ROOT


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def ensure_root(base: Path | None = None) -> Path:
    """Create the skeleton. Never touches content that already exists."""
    base = base or ROOT
    for leaf in ("products", "plan/revisions", "decisions", "archive"):
        (base / leaf).mkdir(parents=True, exist_ok=True)
    return base


def available(base: Path | None = None) -> bool:
    """Whether the durable root is observable at all.

    A missing root is not an empty store. Callers that read product state must
    say «наблюдение недоступно» rather than print zero products, for the same
    reason the process inventory may not report an empty list when the owning
    registry is unavailable.
    """
    base = base or ROOT
    return (base / "products").is_dir()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class _Lock:
    """Whole-root lock, held only around read-modify-write of a shared file."""

    def __init__(self, base: Path):
        self.path = base / LOCK
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+")
        fcntl.flock(self.handle, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None
        return False


# ---------------------------------------------------------------------------
# Product snapshots
# ---------------------------------------------------------------------------


def products_dir(base: Path | None = None) -> Path:
    return (base or ROOT) / "products"


def slugs(base: Path | None = None) -> list[str]:
    directory = products_dir(base)
    if not directory.is_dir():
        return []
    return sorted(entry.name for entry in directory.iterdir()
                  if (entry / SNAPSHOT).is_file())


def snapshot_path(slug: str, base: Path | None = None) -> Path:
    return products_dir(base) / slug / SNAPSHOT


def read_snapshot(slug: str, base: Path | None = None) -> str:
    path = snapshot_path(slug, base)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise ContentError(f"снимок продукта {slug} не читается: {error}") from error


def snapshots(base: Path | None = None) -> Iterator[tuple[str, Path]]:
    """Every readable product snapshot, in the order the board prints them."""
    for slug in slugs(base):
        yield slug, snapshot_path(slug, base)


def section(text: str, title: str) -> list[str]:
    """List items of one `## title` section, in the board's own reading.

    Kept byte-compatible with `process_map_state.markdown_section` on purpose:
    the migration must not change what the board sees, only where it reads it
    from.
    """
    items: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip() == title
            continue
        if not inside:
            continue
        if line.startswith("- "):
            items.append(line[2:].strip())
        elif items and line.startswith("  ") and line.strip():
            items[-1] += " " + line.strip()
    return items


def section_text(text: str, title: str) -> str:
    """The raw body of one section, for digests that must see it verbatim."""
    body: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line.strip() == f"## {title}"
            continue
        if inside:
            body.append(line.strip())
    return "\n".join(body)


def append_work_line(slug: str, line: str, base: Path | None = None) -> Path:
    """Add one line to `## В работе` of a snapshot, without losing a neighbour.

    The background tick and an interactive owner both write here, and they do it
    minutes apart. Read-modify-write of the whole file under a lock is honest
    for a section of prose; the history records that carry the substance are
    lock-free because their names are unique.
    """
    base = base or ROOT
    path = snapshot_path(slug, base)
    entry = "- " + " ".join(line.split())
    with _Lock(base):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ContentError(f"снимок продукта {slug} не читается: {error}") from error
        lines = text.splitlines()
        head = None
        for index, value in enumerate(lines):
            if value.strip() == f"## {WORK_SECTION}":
                head = index
                break
        if head is None:
            raise ContentError(
                f"в снимке {slug} нет раздела «{WORK_SECTION}»; строка не записана")
        tail = head + 1
        while tail < len(lines) and not lines[tail].startswith("## "):
            tail += 1
        stop = tail
        while stop > head + 1 and not lines[stop - 1].strip():
            stop -= 1
        lines.insert(stop, entry)
        _atomic_write(path, ("\n".join(lines) + "\n").encode("utf-8"))
    return path


# ---------------------------------------------------------------------------
# History records and attachments
# ---------------------------------------------------------------------------


# Cyrillic stays in the name. Stripping it left every Russian title as
# `record-<digest>.md`, which turns an addressable history into a directory
# nobody can read; the filesystem here is UTF-8 and the names are opened by
# people far more often than by scripts.
SAFE = re.compile(r"[^\w-]+", re.UNICODE)


def _slugify(title: str) -> str:
    cleaned = SAFE.sub("-", title.lower().strip().replace(" ", "-")).strip("-")
    return cleaned[:60] or "record"


def write_record(title: str, body: str, product: str | None = None,
                 base: Path | None = None, source: str | None = None,
                 replaces: str | None = None) -> Path:
    """Save one independent record. Never overwrites, never merges.

    Concurrency is settled by the name, not by a lock: two owners saving at the
    same second land on different files because the digest of the body and the
    pid are part of it. Losing a record to another owner's save is the failure
    this shape exists to prevent.
    """
    base = base or ROOT
    directory = (products_dir(base) / product / "history") if product \
        else (base / "decisions")
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]
    name = f"{_stamp()}-{_slugify(title)}-{digest}.md"
    path = directory / name
    header = [f"# {title}", "", f"Записано: {now()}"]
    if product:
        header.append(f"Продукт: {product}")
    if source:
        header.append(f"Источник: {source}")
    if replaces:
        header.append(f"Заменяет: {replaces}")
    header.append("")
    document = "\n".join(header) + "\n" + body.rstrip("\n") + "\n"
    # O_EXCL: if the same owner writes the same body twice in one second, the
    # second write is a duplicate and must be visible as an error, not silently
    # merged into the first.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        handle = os.open(path, flags, 0o644)
    except FileExistsError:
        path = directory / f"{path.stem}-{os.getpid()}.md"
        handle = os.open(path, flags, 0o644)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(document)
    return path


def attach(name: str, data: bytes, product: str | None = None,
           base: Path | None = None) -> tuple[Path, str]:
    """Store bytes unchanged, with their SHA-256 beside them.

    The digest is written as a separate small file on purpose: the board, the
    backup check and a restore can compare it without reading — or loading into
    a model's context — a file that may be hundreds of megabytes.
    """
    base = base or ROOT
    directory = (products_dir(base) / product / "attachments") if product \
        else (base / "attachments")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    if path.exists():
        raise ContentError(f"вложение {path} уже существует; байты не перезаписаны")
    _atomic_write(path, data)
    digest = hashlib.sha256(data).hexdigest()
    (directory / f"{name}.sha256").write_text(f"{digest}  {name}\n", encoding="utf-8")
    return path, digest


def records(product: str | None = None, base: Path | None = None) -> list[Path]:
    base = base or ROOT
    directory = (products_dir(base) / product / "history") if product \
        else (base / "decisions")
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.suffix == ".md")


# ---------------------------------------------------------------------------
# The one cross-product plan
# ---------------------------------------------------------------------------


PLAN_FIELDS = ("headline", "now", "next", "parallel", "paused",
               "grounds", "contradictions")


def revisions_dir(base: Path | None = None) -> Path:
    return (base or ROOT) / "plan" / "revisions"


def plan_revisions(base: Path | None = None) -> list[Path]:
    directory = revisions_dir(base)
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.suffix == ".json")


def current_plan(base: Path | None = None) -> dict | None:
    """The single current projection, or None when none was ever published.

    None is «плана нет», and every caller must say that out loud instead of
    inventing an order from task statuses. A `planned` task is not a queue.
    """
    files = plan_revisions(base)
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContentError(f"текущая редакция плана не читается: {error}") from error


def publish_plan(plan: dict, base: Path | None = None,
                 expect_revision: int | None = None) -> Path:
    """Publish the next revision, or refuse because someone else published one.

    `expect_revision` is the revision the caller merged from. When another owner
    published in the meantime, this raises `PlanConflict` and the caller re-reads
    and merges again. It never wins by overwriting: a lost decision is exactly
    the failure the user described.
    """
    base = base or ROOT
    directory = revisions_dir(base)
    directory.mkdir(parents=True, exist_ok=True)
    existing = plan_revisions(base)
    latest = int(existing[-1].stem) if existing else 0
    if expect_revision is not None and expect_revision != latest:
        raise PlanConflict(
            f"план сдвинулся: вы сводили от редакции {expect_revision}, "
            f"текущая — {latest}; перечитайте и сведите заново")
    document = dict(plan)
    document["revision"] = latest + 1
    document["replaces"] = latest or None
    document["accepted_at"] = now()
    for field in PLAN_FIELDS:
        document.setdefault(field, [] if field != "headline" else "")
    path = directory / f"{latest + 1:06d}.json"
    payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        handle = os.open(path, flags, 0o644)
    except FileExistsError as error:
        raise PlanConflict(
            f"редакция {latest + 1} уже занята другим продактом") from error
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(payload)
    return path


def plan_text(plan: dict | None) -> str:
    """The plan as the user and the model both read it: plain lines, no markup."""
    if plan is None:
        return ("Портфельного плана нет. Порядок работ не установлен; статус "
                "`planned` очередью не является.")
    lines = [f"Редакция: {plan.get('revision')}",
             f"Принята: {plan.get('accepted_at')}",
             f"Заменяет: {plan.get('replaces') or 'нет'}",
             "",
             "Главный результат:",
             f"  {plan.get('headline', '')}"]
    for title, field in (("Сейчас", "now"), ("Следом", "next"),
                         ("Параллельно разрешено", "parallel"),
                         ("На паузе", "paused"), ("Основания", "grounds"),
                         ("Неустранённые противоречия", "contradictions")):
        lines.append("")
        lines.append(f"{title}:")
        values = plan.get(field) or []
        lines.extend([f"  - {value}" for value in values] or ["  - нет"])
    lines.append("")
    lines.append("Не является очередью:")
    lines.append("  - все прочие задачи со статусом planned")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------


def checksums(base: Path | None = None) -> dict[str, str]:
    """SHA-256 of every stored file, for the migration and the restore check."""
    base = base or ROOT
    result: dict[str, str] = {}
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.name == LOCK:
            continue
        result[str(path.relative_to(base))] = hashlib.sha256(
            path.read_bytes()).hexdigest()
    return result


def check(base: Path | None = None) -> list[str]:
    """Everything observably wrong with the store, as plain sentences."""
    base = base or ROOT
    problems: list[str] = []
    if not available(base):
        return [f"долговечный корень {base} недоступен: продуктовое содержание не наблюдается"]
    for slug, path in snapshots(base):
        text = path.read_text(encoding="utf-8")
        missing = [title for title in SNAPSHOT_SECTIONS
                   if f"## {title}" not in text]
        if missing:
            problems.append(f"{slug}: в снимке нет разделов " + ", ".join(missing))
    for directory in (base / "products").glob("*/attachments"):
        for path in directory.iterdir():
            if path.suffix == ".sha256" or not path.is_file():
                continue
            sidecar = directory / f"{path.name}.sha256"
            if not sidecar.is_file():
                problems.append(f"вложение {path} без контрольной суммы")
                continue
            stored = sidecar.read_text(encoding="utf-8").split()[0]
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if stored != actual:
                problems.append(f"вложение {path}: SHA-256 не совпал")
    numbers = [int(p.stem) for p in plan_revisions(base)]
    if numbers and numbers != list(range(1, len(numbers) + 1)):
        problems.append(f"редакции плана идут с пропуском: {numbers}")
    problems.extend(legacy_divergence(base))
    return problems


def legacy_divergence(base: Path | None = None) -> list[str]:
    """Whether the retired monoliths became a second editable truth.

    The migration left `products/*/product.md` on disk as a dated archive and
    as the rollback path, and nothing reads them any more. That is only true
    while they do not move: an owner editing one by habit would be writing into
    a file no consumer reads, and would believe the product record was updated.
    Compatibility here is one-directional and refuses on divergence rather than
    syncing back.
    """
    base = base or ROOT
    manifest_path = base / "MIGRATION.json"
    if not manifest_path.is_file():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"опись переноса {manifest_path} не читается: {error}"]
    problems: list[str] = []
    for slug, record in (manifest.get("products") or {}).items():
        source = HOME / record.get("source", "")
        if not source.is_file():
            continue
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if digest != record.get("source_sha256"):
            problems.append(
                f"{slug}: архивная запись {record['source']} изменена после переноса "
                "— её больше никто не читает, правка потеряна; перенесите её в "
                f"content/products/{slug}/snapshot.md")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="проверить другой корень (восстановление)")
    parser.add_argument("--check", action="store_true", help="целостность хранилища")
    parser.add_argument("--inventory", action="store_true", help="что лежит в хранилище")
    parser.add_argument("--plan", action="store_true", help="текущая редакция плана")
    parser.add_argument("--checksums", action="store_true", help="SHA-256 всех файлов")
    args = parser.parse_args()
    base = args.root or ROOT

    if args.plan:
        print(plan_text(current_plan(base)))
        return 0
    if args.checksums:
        for name, digest in checksums(base).items():
            print(f"{digest}  {name}")
        return 0
    if args.inventory or not args.check:
        if not available(base):
            print(f"корень {base} недоступен")
            return 1
        plan = current_plan(base)
        print(f"корень: {base}")
        print(f"план: редакция {plan['revision']} от {plan['accepted_at']}"
              if plan else "план: не опубликован")
        for slug in slugs(base):
            text = read_snapshot(slug, base)
            attachments = list((products_dir(base) / slug / "attachments").glob("*")) \
                if (products_dir(base) / slug / "attachments").is_dir() else []
            print(f"{slug}: снимок {len(text)} симв., записей "
                  f"{len(records(slug, base))}, вложений "
                  f"{len([a for a in attachments if a.suffix != '.sha256'])}")
        print(f"кросс-продуктовых решений: {len(records(None, base))}")
        if not args.check:
            return 0

    problems = check(base)
    for problem in problems:
        print(problem)
    print("хранилище цело" if not problems else f"дефектов: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
