#!/usr/bin/env python3
"""Durable receipts for messages whose owner already selected Gmail.

This module never infers a channel, question, subject, or duplicate from Russian
text. A product composer owns those decisions for product messages. The direct
instruction door owns one narrower decision from task state: a registered
instruction with no returned external result is sent once.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

HOME = Path(__file__).resolve().parent.parent
LEDGER = HOME / "state" / "outbound.json"
KEEP_LETTERS = 50
KEEP_INSTRUCTIONS = 50
IDLE_LETTER_SECONDS = 6 * 60 * 60

EXTERNAL_INSTRUCTION = re.compile(r"(?:^|[-_])instruction\.md$", re.IGNORECASE)
HANDOFF_HEADING = "Актуальная инструкция внешнему исполнителю:"
HANDOFF_NOTE = ("Точное действие: передайте внешнему исполнителю перечисленные "
                "пути и sha256. Файл лежит по этому пути на этой машине; у "
                "внешнего исполнителя есть к ней доступ и он забирает документ "
                "сам. sha256 посчитан с "
                "байтов файла в минуту отправки этого письма — если ревью "
                "перепишет инструкцию, придёт письмо о новой редакции.")


def event_delivered(entry: dict, event_id: str) -> bool:
    """Whether this exact composer-declared event has a successful receipt."""
    return (event_id in entry.get("delivered_events", {})
            or any(item.get("event_id") == event_id for item in entry.get("letters", [])))


def event_delivered_anywhere(data: dict, event_id: str) -> bool:
    """Whether any direction has a successful receipt for this exact event."""
    return any(event_delivered(entry, event_id)
               for entry in data.get("threads", {}).values())


def remember_delivery(entry: dict, *, event_id: str, subject: str, body: str,
                      kind: str, now: datetime, message_id: str | None) -> None:
    """Record a successful send; failed attempts never make an event delivered."""
    entry.setdefault("letters", []).append({
        "at": now.isoformat(), "event_id": event_id, "subject": subject,
        "kind": kind, "excerpt": body.strip()[:400], "message_id": message_id,
    })
    entry["letters"] = entry["letters"][-KEEP_LETTERS:]
    entry.setdefault("delivered_events", {})[event_id] = {
        "at": now.isoformat(), "kind": kind, "message_id": message_id,
    }


def last_of_kind(entry: dict, kind: str) -> datetime | None:
    stamps = []
    for letter in entry.get("letters", []):
        if letter.get("kind") != kind:
            continue
        try:
            stamps.append(datetime.fromisoformat(letter["at"]))
        except (KeyError, TypeError, ValueError):
            continue
    return max(stamps) if stamps else None


def already_said(entry: dict, limit: int = 6) -> list[dict]:
    """Recent successful messages supplied to a composer before it writes."""
    return [{"at": item.get("at"), "event_id": item.get("event_id"),
             "subject": item.get("subject", ""), "excerpt": item.get("excerpt", "")}
            for item in entry.get("letters", [])[-limit:]]


def kind_due(entry: dict, kind: str, now: datetime, seconds: int) -> bool:
    """Whether a mechanical event kind is outside its existing time bound."""
    previous = last_of_kind(entry, kind)
    return previous is None or (now - previous).total_seconds() >= seconds


def instruction_ready(task_dir: Path, instruction: Path) -> bool:
    """Whether the exact current instruction predates its completed approval."""
    try:
        status = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
        rows = [json.loads(line) for line in
                (task_dir / "reviews" / "rounds.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()]
        latest = rows[-1]
        approved_at = datetime.fromisoformat(latest["recorded_at"])
        modified_at = instruction.stat().st_mtime
    except (IndexError, KeyError, OSError, json.JSONDecodeError, TypeError, ValueError):
        return False
    return (isinstance(status, dict) and status.get("state") != "running"
            and isinstance(latest, dict) and latest.get("decision") == "approved"
            and approved_at.tzinfo is not None
            and modified_at <= approved_at.timestamp())


def external_instructions(thread: str) -> list[dict]:
    """Newest approved, idle instruction that still awaits an external result."""
    from process_map_state import REPO, _file_sha256, thread_tasks  # noqa: PLC0415
    from thread_state import load_thread  # noqa: PLC0415

    try:
        config = load_thread(thread)
    except (SystemExit, OSError, ValueError):
        return []
    for task in thread_tasks(config):
        task_dir = REPO / str(task.get("path") or "")
        returned = task_dir / "from-external-agent"
        try:
            has_result = any(path.is_file() and path.name != "README.md"
                             for path in returned.rglob("*"))
        except OSError:
            has_result = False
        if has_result:
            continue
        box = task_dir / "deliverables"
        try:
            data = json.loads((box / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        names = data.get("deliverables") if isinstance(data, dict) else None
        if not isinstance(names, list):
            continue
        found = []
        for name in names:
            if (not isinstance(name, str) or Path(name).name != name
                    or not EXTERNAL_INSTRUCTION.search(name)):
                continue
            path = box / name
            if not path.is_file():
                continue
            if not instruction_ready(task_dir, path):
                continue
            digest = _file_sha256(path)
            if not instruction_ready(task_dir, path):
                continue
            found.append({"task": task.get("id"),
                          "goal": task.get("title") or f"задача {task.get('id')}",
                          "path": str(path.resolve()),
                          "sha256": digest})
        if found:
            return found
    return []


def instruction_event_id(thread: str, item: dict) -> str:
    return f"instruction:{thread}:{item.get('task')}:{item.get('sha256')}"


def instructions_said(entry: dict) -> set[str]:
    """Digests recorded by either the previous or the current receipt schema."""
    digests = {str(item.get("sha256")) for item in entry.get("instructions", [])}
    for item in entry.get("letters", []):
        event_id = str(item.get("event_id") or "")
        if event_id.startswith("instruction:"):
            digests.add(event_id.rsplit(":", 1)[-1])
    return digests


def unnamed_instructions(entry: dict, items: list[dict]) -> list[dict]:
    said = instructions_said(entry)
    return [item for item in items if str(item.get("sha256")) not in said]


def remember_instructions(entry: dict, items: list[dict], now: datetime) -> None:
    said = list(entry.get("instructions", []))
    known = {str(item.get("sha256")) for item in said}
    for item in items:
        digest = str(item.get("sha256"))
        if digest in known:
            continue
        known.add(digest)
        said.append({"at": now.isoformat(), "task": item.get("task"),
                     "path": item.get("path"), "sha256": digest})
    entry["instructions"] = said[-KEEP_INSTRUCTIONS:]


def instruction_letter(thread: str, entry: dict | None = None) -> dict | None:
    """Build one self-contained message only for an unsent registered file."""
    try:
        found = external_instructions(thread)
    except Exception as error:  # noqa: BLE001
        print(f"продакт: инструкция направления «{thread}» не наблюдается, "
              f"письма о ней в этот тик нет: {error}", file=sys.stderr)
        return None
    if entry is not None:
        found = unnamed_instructions(entry, found)
    if not found:
        return None
    unreadable = [item["path"] for item in found if not item.get("sha256")]
    if unreadable:
        print(f"продакт: зарегистрированная инструкция направления «{thread}» "
              f"видна и не читается, письмо без её sha256 не собирается: "
              f"{', '.join(unreadable)}", file=sys.stderr)
        return None
    lines = [f"Цель: {item['goal']}\n"
             "Причина отправки: инструкция зарегистрирована, а результат "
             "внешнего выполнения ещё не возвращён.\n"
             f"- {item['task']} — {item['path']}\n  sha256: {item['sha256']}"
             for item in found]
    return {
        "body": f"{HANDOFF_HEADING}\n\n" + "\n".join(lines) + f"\n\n{HANDOFF_NOTE}",
        "names": found,
        "event_id": instruction_event_id(thread, found[0]),
    }


class Ledger:
    """Locked, recoverable receipts shared by all direction timers."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or LEDGER)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.backup_path = self.path.with_name(self.path.stem + ".backup.json")
        self.journal_path = self.path.with_name(self.path.stem + "-journal.jsonl")
        self._handle = None
        self.data: dict = {}

    def _load(self, path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("version") == 1 else None

    def __enter__(self) -> "Ledger":
        self._handle = self.lock_path.open("a+", encoding="utf-8")
        fcntl.flock(self._handle, fcntl.LOCK_EX)
        data = self._load(self.path)
        if data is None:
            data = self._load(self.backup_path)
            if data is not None:
                data["recovered_at"] = datetime.now(timezone.utc).isoformat()
        self.data = data if data is not None else {"version": 1, "threads": {}}
        self.data.setdefault("threads", {})
        return self

    def _commit(self, path: Path, payload: str) -> None:
        temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def record(self, entry: dict) -> None:
        try:
            line = json.dumps(entry, ensure_ascii=False, default=str)
            with self.journal_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except (OSError, TypeError, ValueError):
            pass

    def __exit__(self, *exc) -> None:
        if self._handle is None:
            return
        try:
            payload = json.dumps(self.data, ensure_ascii=False, indent=2) + "\n"
            self._commit(self.path, payload)
            self._commit(self.backup_path, payload)
        finally:
            fcntl.flock(self._handle, fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None

    def thread(self, name: str) -> dict:
        entry = self.data["threads"].setdefault(
            name, {"letters": [], "delivered_events": {}, "instructions": []})
        entry.setdefault("letters", [])
        entry.setdefault("delivered_events", {})
        entry.setdefault("instructions", [])
        return entry
