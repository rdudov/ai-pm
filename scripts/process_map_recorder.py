#!/usr/bin/env python3
"""The scribe: turns observed state into an append-only timeline.

Every few seconds it looks at what is observable on disk and appends to
`timeline.jsonl` only what changed since the previous look: a task that appeared,
a dev-pipeline event, an artifact of a role that showed up or was rewritten, a
new `activity` line, a commit, a notification that went out, a letter that came
in. Each record says when, what it was observed by, which participant it belongs
to, which artifact and which channel.

The cost of a tick does not depend on how much work happened: child transcripts
are never read, and everything else is either a `stat` or a tail of an
append-only file past a stored cursor. That is the rule `thread_state.py` set and
the state collector kept.

Exit code 0 on a clean stop.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import process_map_state as state
from process_map_schema import SCHEMA_VERSION, validate_record

TASKS = state.REPO / "tasks"
MAIL_INBOX = state.REPO / ".state" / "gmail" / "product-owner" / "inbox"

# Artifacts of a role, and the station each one is evidence of. A station the
# contour never produced simply never appears on the map: no role state machine
# is declared anywhere, so the scribe reports files, not a lifecycle.
ARTIFACT_STATIONS = {
    "analysis.md": "analysis",
    "plan.md": "analysis",
    "findings.md": "review",
    "verification.md": "report",
    "trace.md": "report",
    "task.md": "report",
}
REVIEW_FILE = re.compile(r"(review|findings)", re.IGNORECASE)
TEST_FILE = re.compile(r"^(test_.*\.py|.*_test\.py)$")
CODE_FILE = re.compile(r"\.(py|js|ts|sh|html|css)$")

# dev-pipeline event kinds, mapped to the station they move the figure to.
EVENT_STATIONS = {
    "attempt_started": "analysis",
    "run_started": "analysis",
    "process_started": "analysis",
    "increment_ready_for_review": "review",
    "review_completed": "review",
    "checkpoint_completed": "report",
    "run_completed": "report",
    "attempt_completed": "report",
    "run_failed": "report",
    "attempt_failed": "report",
}

FRONTMATTER_STATUS = re.compile(r'^status:\s*"?([a-z_]+)"?', re.MULTILINE)
FRONTMATTER_TITLE = re.compile(r'^title:\s*"?(.+?)"?\s*$', re.MULTILINE)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def read_frontmatter(task_dir: Path) -> dict:
    """Task id, title and status from the first lines of task.md — never the body."""
    path = task_dir / "task.md"
    try:
        with path.open() as handle:
            head = "".join(next(handle, "") for _ in range(20))
    except OSError:
        return {}
    status = FRONTMATTER_STATUS.search(head)
    title = FRONTMATTER_TITLE.search(head)
    return {
        "status": status.group(1) if status else None,
        "title": title.group(1) if title else task_dir.name,
    }


class Scribe:
    def __init__(self, out: Path, cursor_path: Path, anonymize: bool,
                 replay_since: str | None = None):
        self.out = out
        self.cursor_path = cursor_path
        self.anonymize = anonymize
        self.replay_since = replay_since
        # A first look at 671 task directories is not news: without this the
        # timeline would open with a burst of history that never happened
        # during the recording. The first tick fills the cursor silently, and
        # `--replay-since` lets exactly one window of real, really-timestamped
        # history through — the run being demonstrated and the letter that
        # started it.
        self.seeding = not cursor_path.is_file()
        self.cursor = state.read_json(cursor_path) if cursor_path.is_file() else {}
        self.cursor.setdefault("events", {})     # task dir -> last sequence seen
        self.cursor.setdefault("artifacts", {})  # "task/file" -> mtime
        self.cursor.setdefault("activity", {})   # task dir -> last activity line
        self.cursor.setdefault("status", {})     # task dir -> last frontmatter status
        self.cursor.setdefault("commits", {})    # repo -> last HEAD
        self.cursor.setdefault("receipts", {})   # task dir -> receipts seen
        self.cursor.setdefault("mail", [])       # message ids seen
        self.threads = self._thread_of_task()
        self.written = 0

    # -- attribution ------------------------------------------------------
    def _thread_of_task(self) -> dict:
        """Task directory -> thread key. Costs one collector-style lookup, once."""
        mapping: dict[str, str] = {}
        config = state.load_config()
        for key, thread in config["threads"].items():
            for task in state.thread_tasks(thread):
                mapping[Path(task["path"]).name] = key
        return mapping

    def _repos(self) -> list[tuple[str, Path]]:
        config = state.load_config()
        pairs = []
        for key, thread in config["threads"].items():
            for repo in thread.get("repos", []):
                pairs.append((key, Path(repo)))
        return pairs

    # -- writing ----------------------------------------------------------
    def emit(self, record: dict) -> None:
        record = {"schema_version": SCHEMA_VERSION, **record}
        validate_record(record)
        if self.seeding and not (self.replay_since and str(record["at"]) >= self.replay_since):
            return
        if self.anonymize:
            record = scrub_record(record)
        with self.out.open("a") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.written += 1

    def save(self) -> None:
        self.cursor_path.write_text(json.dumps(self.cursor, ensure_ascii=False))

    # -- observations -----------------------------------------------------
    def tick(self) -> int:
        before = self.written
        for task_dir in sorted(TASKS.glob("[0-9]*-*")):
            if not task_dir.is_dir():
                continue
            self.observe_task(task_dir)
        self.observe_commits()
        self.observe_mail()
        self.save()
        self.seeding = False
        return self.written - before

    def observe_task(self, task_dir: Path) -> None:
        name = task_dir.name
        front = read_frontmatter(task_dir)
        if not front:
            return
        title = front["title"]
        thread = self.threads.get(name)
        base = {"thread": thread, "task": name, "task_title": title}

        if name not in self.cursor["status"]:
            started = state.read_json(task_dir / ".runner" / "runner.json").get("started_at")
            self.emit({**base, "at": started or mtime_iso(task_dir / "task.md"),
                       "kind": "task_appeared",
                       "label": title, "observed_by": "каталог задачи на диске",
                       "actor": "продакт"})
        elif self.cursor["status"][name] != front["status"]:
            self.emit({**base, "at": now(), "kind": "task_status",
                       "label": f"{self.cursor['status'][name]} → {front['status']}",
                       "observed_by": "frontmatter task.md", "actor": "продакт",
                       "status": front["status"]})
        self.cursor["status"][name] = front["status"]

        self.observe_events(task_dir, base)
        self.observe_artifacts(task_dir, base)
        self.observe_activity(task_dir, base)
        self.observe_receipts(task_dir, base)

    def observe_events(self, task_dir: Path, base: dict) -> None:
        path = task_dir / "dev-pipeline" / "core" / "events.jsonl"
        if not path.is_file():
            return
        seen = self.cursor["events"].get(base["task"], 0)
        highest = seen
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            sequence = event.get("sequence", 0)
            if sequence <= seen:
                continue
            highest = max(highest, sequence)
            payload = event.get("payload") or {}
            self.emit({
                **base,
                "at": event.get("timestamp") or now(),
                "kind": "pipeline_event",
                "label": event.get("kind"),
                "observed_by": "событие dev-pipeline в events.jsonl",
                "station": EVENT_STATIONS.get(event.get("kind")),
                "actor": payload.get("runtime") or "исполнитель",
                "detail": payload.get("reason") or payload.get("outcome"),
            })
        self.cursor["events"][base["task"]] = highest

    def observe_artifacts(self, task_dir: Path, base: dict) -> None:
        for path in sorted(task_dir.iterdir()):
            if not path.is_file():
                continue
            station = self.station_of(path.name)
            if station is None:
                continue
            key = f"{base['task']}/{path.name}"
            try:
                stamp = mtime_iso(path)
            except OSError:
                continue
            if self.cursor["artifacts"].get(key) == stamp:
                continue
            fresh = key not in self.cursor["artifacts"]
            self.cursor["artifacts"][key] = stamp
            self.emit({
                **base,
                "at": stamp,
                "kind": "artifact",
                "label": path.name,
                "observed_by": "появление файла" if fresh else "обновление файла",
                "station": station,
                "artifact": path.name,
                "actor": "исполнитель",
            })

    @staticmethod
    def station_of(filename: str) -> str | None:
        if filename in ARTIFACT_STATIONS:
            return ARTIFACT_STATIONS[filename]
        if TEST_FILE.match(filename):
            return "tests"
        if REVIEW_FILE.search(filename) and filename.endswith(".md"):
            return "review"
        if CODE_FILE.search(filename):
            return "development"
        return None

    def observe_activity(self, task_dir: Path, base: dict) -> None:
        path = task_dir / "progress.json"
        if not path.is_file():
            return
        payload = state.read_json(path)
        activity = payload.get("activity")
        if not activity or self.cursor["activity"].get(base["task"]) == activity:
            return
        self.cursor["activity"][base["task"]] = activity
        try:
            stamp = mtime_iso(path)
        except OSError:
            stamp = now()
        self.emit({
            **base,
            "at": stamp,
            "kind": "activity",
            "label": activity,
            # Freshness is the file's mtime, never the child's own timestamp:
            # children routinely write local time into a UTC field.
            "observed_by": "строка activity в progress.json (свежесть по mtime)",
            "actor": "исполнитель",
        })

    def observe_receipts(self, task_dir: Path, base: dict) -> None:
        path = task_dir / "dev-pipeline" / "notification-receipts.jsonl"
        if not path.is_file():
            return
        seen = set(self.cursor["receipts"].get(base["task"], []))
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return
        for line in lines:
            try:
                receipt = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_id = receipt.get("event_id")
            if not event_id or event_id in seen:
                continue
            seen.add(event_id)
            self.emit({
                **base,
                "at": receipt.get("recorded_at") or now(),
                "kind": "notification",
                "label": f"уведомление: {receipt.get('kind')}",
                "observed_by": "notification-receipts.jsonl",
                "channel": "telegram",
                "actor": "исполнитель",
            })
        self.cursor["receipts"][base["task"]] = sorted(seen)

    def observe_commits(self) -> None:
        for thread, repo in self._repos():
            if not (repo / ".git").exists():
                continue
            out = subprocess.run(
                ["git", "-C", str(repo), "log", "-15", "--format=%H%x1f%cI%x1f%s"],
                capture_output=True, text=True,
            )
            if out.returncode != 0:
                continue
            commits = [line.split("\x1f") for line in out.stdout.strip().splitlines() if line]
            last = self.cursor["commits"].get(str(repo))
            fresh = []
            for sha, stamp, subject in commits:
                if sha == last:
                    break
                fresh.append((sha, stamp, subject))
            if commits:
                self.cursor["commits"][str(repo)] = commits[0][0]
            for sha, stamp, subject in reversed(fresh):
                self.emit({
                    "thread": thread,
                    "at": stamp,
                    "kind": "commit",
                    "label": subject,
                    "observed_by": f"git log в {repo.name}",
                    "station": "commit",
                    "channel": "git",
                    "actor": "исполнитель",
                    "repo": repo.name,
                })

    def observe_mail(self) -> None:
        if not MAIL_INBOX.is_dir():
            return
        seen = set(self.cursor["mail"])
        for entry in sorted(MAIL_INBOX.iterdir()):
            metadata = entry / "metadata.json"
            if not metadata.is_file() or entry.name in seen:
                continue
            seen.add(entry.name)
            payload = state.read_json(metadata)
            self.emit({
                "at": mtime_iso(metadata),
                "kind": "mail",
                "label": payload.get("subject") or "письмо",
                "observed_by": "каталог входящей почты продакта",
                "channel": "email",
                "actor": "пользователь",
            })
        self.cursor["mail"] = sorted(seen)


def scrub_record(record: dict) -> dict:
    """Structural anonymisation, with task titles deliberately preserved.

    The user asked to recognise a specific task among the rest by its real name,
    so titles survive. Content-level privacy of those titles stays a human step
    before any showing — declared as a limit, not as a closed box.
    """
    kept = {key: record[key] for key in ("task_title", "label") if key in record}
    scrubbed = state.scrub(record)
    if record.get("kind") in {"task_appeared", "task_status", "artifact", "pipeline_event"}:
        scrubbed.update(kept)
    else:
        scrubbed.update({key: value for key, value in kept.items() if key == "task_title"})
    return scrubbed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=state.HOME / "state" / "timeline.jsonl",
                        help="where the timeline is appended")
    parser.add_argument("--cursor", type=Path, default=None,
                        help="cursor file, so a restart does not repeat records")
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between looks")
    parser.add_argument("--ticks", type=int, default=0, help="stop after N looks (0 = forever)")
    parser.add_argument("--anonymize", action="store_true",
                        help="strip absolute paths, mail addresses and numeric identifiers")
    parser.add_argument("--replay-since", default=None,
                        help="on a fresh cursor, also write history from this ISO instant on")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    cursor = args.cursor or args.out.with_suffix(".cursor.json")
    scribe = Scribe(args.out, cursor, args.anonymize, args.replay_since)

    tick = 0
    try:
        while True:
            tick += 1
            started = time.time()
            new = scribe.tick()
            if not args.quiet and new:
                print(f"тик {tick}: {new} записей, {time.time() - started:.2f} с", flush=True)
            if args.ticks and tick >= args.ticks:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    if not args.quiet:
        print(f"итого записей за прогон: {scribe.written}; лента {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
