#!/usr/bin/env python3
"""The adapter: collects a snapshot and hands the renderer a finished document.

This is the one component that sees both sides. `process_map_state.py` reads the
contour, `process_map_render.py` draws whatever JSON it is given, and neither
imports the other — the promise that the picture cannot show what it never saw is
a property of the renderer's input, not of the discipline of its author. Anything
that has to collect and draw in one breath belongs here and nowhere else.

    --out map.html            build a self-contained recording, network off
    --serve 8765              live mode: fresh snapshot on every request

Live mode exists because a board has to move without the timeline moving: a
status change, a run that died, a repository that went dirty append nothing to
the timeline and would otherwise never reach the screen. The page compares the
digest of the whole shown document, so any of those changes is noticed.

Exit code 0 on a clean stop.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import process_map_render as render
import process_map_state as state

DEFAULT_TIMELINE = state.HOME / "state" / "timeline.jsonl"


def collect(anonymize: bool) -> Path:
    """Write a validated snapshot to a file, because that file is the boundary.

    The renderer takes a path, not an object, on purpose: passing a dict in
    process would rebuild exactly the shortcut this split was made to remove.
    """
    snapshot = state.build(anonymize)
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    with handle:
        json.dump(snapshot, handle, ensure_ascii=False)
    return Path(handle.name)


def build_payload(anonymize: bool, timeline: Path, live_url: str | None) -> dict:
    path = collect(anonymize)
    try:
        return render.payload(timeline, path, live_url=live_url)
    finally:
        path.unlink(missing_ok=True)


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
            data = build_payload(self.anonymize, self.timeline, "/data.json")
            self._send(json.dumps(data, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")
            return
        if self.path in ("/", "/index.html"):
            data = build_payload(self.anonymize, self.timeline, "/data.json")
            self._send(render.render(data).encode(), "text/html; charset=utf-8")
            return
        self.send_error(404)

    def log_message(self, *args) -> None:  # keep the console for our own output
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="write the self-contained recording here")
    parser.add_argument("--snapshot-out", type=Path,
                        help="also keep the collected snapshot at this path")
    parser.add_argument("--timeline", type=Path, default=DEFAULT_TIMELINE)
    parser.add_argument("--anonymize", action="store_true",
                        help="strip absolute paths, mail addresses and numeric identifiers")
    parser.add_argument("--serve", type=int, metavar="PORT",
                        help="live mode: same renderer, fresh snapshot on every load")
    args = parser.parse_args()

    if args.serve:
        handler = partial(LiveHandler, anonymize=args.anonymize, timeline=args.timeline)
        server = HTTPServer(("127.0.0.1", args.serve), handler)
        print(f"живой режим: порт {args.serve} на 127.0.0.1 (Ctrl+C — стоп)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0

    if not args.out:
        parser.error("нужен --out или --serve")

    snapshot_path = collect(args.anonymize)
    try:
        if args.snapshot_out:
            args.snapshot_out.write_text(snapshot_path.read_text())
        data = render.payload(args.timeline, snapshot_path)
    finally:
        snapshot_path.unlink(missing_ok=True)

    html = render.render(data)
    args.out.write_text(html)
    board = data["board"]
    plates = sum(len(a["plates"]) for p in board["panels"] for a in p["areas"])
    hidden = sum(a["hidden"] for p in board["panels"] for a in p["areas"])
    print(f"{args.out} — {len(html)} байт, режим {data['snapshot']['mode']}, "
          f"лента {len(data['timeline'])} записей, панелей {len(board['panels'])}, "
          f"плашек {plates}, скрыто {hidden}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
