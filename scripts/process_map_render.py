#!/usr/bin/env python3
"""Builds the map: one self-contained HTML file with the recording inside it.

The renderer never reads a repository, a task directory or the disk. It gets a
snapshot document and a timeline, both already checked against
`process_map_schema`, and nothing else — so whatever is private cannot leak
through a picture that never saw it.

Two deliveries out of the same template:

    --out map.html            a recording: data embedded, opens by double click
                              with the network off, is the file you hand over
    --serve 8765              live mode: the same renderer, refetching a fresh
                              snapshot instead of carrying a frozen one

`file://` cannot fetch, which is why the recording embeds its data rather than
loading it — a build requirement, not a detail.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import process_map_state as state
from process_map_schema import validate_record, validate_snapshot

TEMPLATE = Path(__file__).resolve().parent / "process_map_template.html"
DEFAULT_TIMELINE = state.HOME / "state" / "timeline.jsonl"

# How many task objects one area may carry before the picture stops being
# readable. Everything dropped is counted out loud in the area caption: a silent
# cap would read as «показано всё», and it is not.
PER_AREA = 12

# Order of interest when the cap bites. A blocked object and a lying label are
# the reason someone opens the map at all; an idle object is the least of it.
INTEREST = ["live", "blocked", "killed", "stale_label", "gap", "delivered", "idle"]


def load_timeline(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    records = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        records.append(validate_record(record))
    # Sorting by the string would misplace anything written with a local offset
    # — git stamps commits as `+03:00`, the rest of the contour as `+00:00`.
    records.sort(key=instant)
    return records


def instant(record: dict) -> datetime:
    try:
        stamp = datetime.fromisoformat(str(record["at"]))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def leading_flag(task: dict) -> str:
    flags = task.get("flags") or []
    for flag in INTEREST:
        if flag in flags:
            return flag
    return "idle"


def rank(task: dict) -> tuple:
    return (INTEREST.index(leading_flag(task)), -(task.get("id") or 0))


def mix(tasks: list[dict], limit: int) -> list[dict]:
    """Take a mix of states, not the top of one.

    Sixty of ninety tasks carry a `gap`, so a plain ranking fills an area with
    sixty identical orange notches and the picture stops saying anything. Taking
    round-robin across the states keeps a rare `blocked` visible next to a common
    `delivered`.
    """
    buckets: dict[str, list[dict]] = {}
    for task in sorted(tasks, key=rank):
        buckets.setdefault(leading_flag(task), []).append(task)
    chosen: list[dict] = []
    while len(chosen) < limit and any(buckets.values()):
        for flag in INTEREST:
            queue = buckets.get(flag)
            if queue and len(chosen) < limit:
                chosen.append(queue.pop(0))
    return chosen


def build_world(snapshot: dict, timeline: list[dict]) -> dict:
    seen_in_timeline = {r["task"] for r in timeline if r.get("task")}

    areas = []
    span = 0
    for index, thread in enumerate(snapshot["threads"]):
        # A task that moves during the recording is on the map whatever its rank:
        # the user has to find their own task among the rest.
        pinned = sorted([t for t in thread["tasks"] if t["dir"] in seen_in_timeline], key=rank)
        rest = [t for t in thread["tasks"] if t["dir"] not in seen_in_timeline]
        chosen = pinned[:PER_AREA] + mix(rest, max(PER_AREA - len(pinned), 0))
        dropped = len(thread["tasks"]) - len(chosen)
        areas.append({
            "key": thread["key"],
            "title": thread["title"],
            "note": (f"{thread['task_count']} задач, показано {len(chosen)}"
                     + (f", скрыто {dropped}" if dropped else "")),
            "tasks": [{
                "task": t["dir"],
                "id": t["id"],
                "title": t["title"] or t["dir"],
                "status": t["status"],
                "flags": t["flags"],
                "gates": len(t.get("gates") or []),
                "pin": t["dir"] in seen_in_timeline,
            } for t in chosen],
        })

    # Two by two, so four directions read as four plots of one town. The gap
    # between plots is wide enough that a title of one never lands on another.
    side = max((int(len(a["tasks"]) ** 0.5) + 2) for a in areas) if areas else 4
    for index, area in enumerate(areas):
        area["ox"] = (index % 2) * (side + 4)
        area["oy"] = (index // 2) * (side + 4)
        span = max(span, area["ox"] + side, area["oy"] + side)

    # The open questions belong to products, and a thread owns its products, so
    # a question stands as a marker on the ground of the thread that owns it and
    # as a line in the panel — the same fact in both places.
    by_product = {}
    for index, thread in enumerate(snapshot["threads"]):
        for slug in thread.get("products", []):
            by_product[slug] = index
    for product in snapshot["products"]:
        index = by_product.get(product["slug"])
        if index is None:
            continue
        areas[index].setdefault("questions", []).extend(product["questions"])
    for area in areas:
        area.setdefault("questions", [])

    waiting = [{"text": question, "src": product["slug"]}
               for product in snapshot["products"] for question in product["questions"]]
    done = [{"text": entry, "src": product["slug"]}
            for product in snapshot["products"] for entry in product["effect"]]

    if timeline:
        first = instant(timeline[0]).astimezone(timezone.utc).strftime("%d.%m %H:%M")
        last = instant(timeline[-1]).astimezone(timezone.utc).strftime("%d.%m %H:%M")
        subtitle = f"запись {first} — {last} · {len(timeline)} наблюдений"
    else:
        subtitle = "лента пуста: писец ещё ничего не наблюдал"

    return {"areas": areas, "span": span + 1, "waiting": waiting[:14],
            "done": done[:14], "subtitle": subtitle}


def payload(anonymize: bool, timeline_path: Path, snapshot_path: Path | None,
            live_url: str | None = None) -> dict:
    if snapshot_path:
        snapshot = validate_snapshot(json.loads(snapshot_path.read_text()))
    else:
        snapshot = state.build(anonymize)
    timeline = load_timeline(timeline_path)
    return {
        "snapshot": snapshot,
        "timeline": timeline,
        "world": build_world(snapshot, timeline),
        "built_at": datetime.now(timezone.utc).isoformat(),
        "live_url": live_url,
    }


def render(data: dict) -> str:
    template = TEMPLATE.read_text()
    # `</script>` inside the JSON would end the tag early and break the page.
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return template.replace("__DATA__", blob)


class LiveHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, anonymize: bool, timeline: Path, **kwargs):
        self.anonymize, self.timeline = anonymize, timeline
        super().__init__(*args, **kwargs)

    def _send(self, body: bytes, kind: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path.startswith("/data.json"):
            data = payload(self.anonymize, self.timeline, None, live_url="/data.json")
            self._send(json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if self.path in ("/", "/index.html"):
            data = payload(self.anonymize, self.timeline, None, live_url="/data.json")
            self._send(render(data).encode(), "text/html; charset=utf-8")
            return
        self.send_error(404)

    def log_message(self, *args) -> None:  # keep the console for our own output
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="write the self-contained recording here")
    parser.add_argument("--timeline", type=Path, default=DEFAULT_TIMELINE)
    parser.add_argument("--snapshot", type=Path, help="use this snapshot instead of collecting one")
    parser.add_argument("--anonymize", action="store_true",
                        help="strip absolute paths, mail addresses and numeric identifiers")
    parser.add_argument("--serve", type=int, metavar="PORT",
                        help="live mode: same renderer, fresh snapshot on every load")
    args = parser.parse_args()

    if args.serve:
        handler = partial(LiveHandler, anonymize=args.anonymize, timeline=args.timeline)
        server = HTTPServer(("127.0.0.1", args.serve), handler)
        print(f"живой режим: http://127.0.0.1:{args.serve}/ (Ctrl+C — стоп)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0

    if not args.out:
        parser.error("нужен --out или --serve")
    data = payload(args.anonymize, args.timeline, args.snapshot)
    html = render(data)
    args.out.write_text(html)
    world = data["world"]
    shown = sum(len(a["tasks"]) for a in world["areas"])
    hidden = sum(a["note"].count("скрыто") for a in world["areas"])
    print(f"{args.out} — {len(html)} байт, режим {data['snapshot']['mode']}, "
          f"лента {len(data['timeline'])} записей, объектов {shown}, "
          f"областей со скрытыми задачами {hidden}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
