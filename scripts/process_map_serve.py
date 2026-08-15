#!/usr/bin/env python3
"""The adapter: collects a snapshot and hands the renderer a finished document.

This is the one component that sees both sides. `process_map_state.py` reads the
contour, `process_map_render.py` draws whatever JSON it is given, and neither
imports the other — the promise that the picture cannot show what it never saw is
a property of the renderer's input, not of the discipline of its author. Anything
that has to collect and draw in one breath belongs here and nowhere else.

    --out board.html          build a self-contained recording, network off
    --serve 8765              live mode: fresh snapshot on every request

Live mode exists because a board has to move without the timeline moving: a
status change, a run that died, a repository that went dirty append nothing to
the timeline and would otherwise never reach the screen. The page compares the
digest of the whole shown document, so any of those changes is noticed.

The scribe is called from here too, and after the answer has gone out rather than
before it: the feed above the board is «недавние наблюдаемые изменения», and a
file nobody appends to answers that question with the state of a week ago. It is
one look at most per `PRODUCT_OWNER_TIMELINE_TICK_SECONDS`, so a ten-second poll
does not pay for it, and the look after a pause fills the gap with the instants
the changes actually happened at — a file's mtime, a commit's date — not with the
moment they were noticed.

`POST /wake` asks the direction's owner to look now instead of at the next
twenty-minute firing. It starts `product-thread@<тред>.service`, the very unit
the timer starts, so the button needs no queue, no lock and no supervisor of its
own: systemd runs one instance of a unit, and a second click or a tick arriving
in the same minute join the check already running.

Exit code 0 on a clean stop.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

import process_map_recorder as recorder
import process_map_render as render
import process_map_state as state
from process_map_schema import run_entrypoint

DEFAULT_TIMELINE = state.HOME / "state" / "timeline.jsonl"

# How often the scribe is allowed to look while the board is being watched. One
# look cost 2,0 s on the live contour against a board build of 7 s, so it is
# rate-limited rather than run per request — and taken after the response, so no
# poll waits for it.
TIMELINE_TICK_SECONDS = state.tunable("PRODUCT_OWNER_TIMELINE_TICK_SECONDS", 60)
# How long the wake request waits for systemd to accept the job. `--no-block`
# returns as soon as the job is queued; the check itself runs for as long as it
# runs, under its own unit and its own timeout.
WAKE_TIMEOUT = state.tunable("PRODUCT_OWNER_WAKE_REQUEST_TIMEOUT_SECONDS", 15)
# The board is bound to the loopback address and has no other guard. A request
# arriving from a page the user merely happened to open would be a wider door
# than the one that exists, so a cross-origin caller is refused: a browser sends
# `Origin` on every cross-origin fetch and cannot omit it, and a form post — the
# one shape that carries no `Origin` — cannot set this content type without a
# preflight this server never answers.
WAKE_CONTENT_TYPE = "application/json"


def record_timeline(timeline: Path) -> int:
    """One look of the scribe, at most one per interval, and never in the way.

    The cursor's own mtime is the clock: it is written by every look, so two
    processes watching the same file rate-limit each other without a lock and
    without a second piece of state to keep. A first run with no cursor seeds it
    silently — that is the scribe's own rule, and it is what keeps a fresh
    install from opening with a burst of history that never happened.
    """
    cursor = timeline.with_suffix(".cursor.json")
    try:
        looked = cursor.stat().st_mtime
    except OSError:
        looked = 0.0
    if time.time() - looked < TIMELINE_TICK_SECONDS:
        return 0
    try:
        return recorder.Scribe(timeline, cursor, anonymize=False).tick()
    except Exception as error:  # noqa: BLE001 - the board outlives a bad look
        print(f"писец: наблюдение не отработало — {error}", flush=True)
        return 0


def collect(anonymize: bool, include_task_cards: bool = False) -> Path:
    """Write a validated snapshot to a file, because that file is the boundary.

    The renderer takes a path, not an object, on purpose: passing a dict in
    process would rebuild exactly the shortcut this split was made to remove.
    """
    snapshot = state.build(anonymize, include_task_cards=include_task_cards)
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    with handle:
        json.dump(snapshot, handle, ensure_ascii=False)
    return Path(handle.name)


def build_payload(anonymize: bool, timeline: Path, live_url: str | None,
                  include_task_cards: bool = False,
                  live_page_url: str | None = None) -> dict:
    """One anonymisation decision, applied to everything that reaches the page.

    The snapshot was collected anonymously and the timeline was read from disk as
    it happened to be written, so `--anonymize` was only as good as the flags the
    scribe had been started with hours earlier (finding HIGH-3 of review 786).
    The flag now travels with the request: whatever is being shown is cleaned
    before it is shown.
    """
    path = collect(anonymize, include_task_cards)
    try:
        return render.payload(timeline, path, live_url=live_url, anonymize=anonymize,
                              live_page_url=live_page_url)
    finally:
        path.unlink(missing_ok=True)


def wake(thread: str) -> tuple[int, dict]:
    """Ask this direction's owner to look now. Returns an HTTP code and an answer.

    Nothing here decides what the owner will do, and nothing here starts a task:
    it starts the same one-wake-up unit the timer starts, and the owner reads the
    plan and decides as it always does. Two clicks, or a click and a firing of
    the timer, are one check — that is systemd's property of a unit, not a
    promise this function makes.
    """
    known = state.load_config()["threads"]
    if thread not in known:
        return 400, {"accepted": False, "thread": thread,
                     "detail": f"направления {thread!r} нет в threads.json; "
                               f"известны: {', '.join(sorted(known))}"}
    unit = state.wake_unit(thread)
    at = datetime.now(timezone.utc).isoformat()
    try:
        done = subprocess.run(["systemctl", "start", "--no-block", unit],
                              capture_output=True, text=True, timeout=WAKE_TIMEOUT,
                              check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return 502, {"accepted": False, "thread": thread, "unit": unit, "at": at,
                     "detail": f"systemd не принял запрос: {error}",
                     "wake": state.wake_state(thread)}
    if done.returncode != 0:
        return 502, {"accepted": False, "thread": thread, "unit": unit, "at": at,
                     "detail": (done.stderr or done.stdout or "").strip()[:300]
                               or f"systemctl вернул код {done.returncode}",
                     "wake": state.wake_state(thread)}
    # Observed after the request rather than assumed from its exit code: the job
    # was accepted, and what the unit is doing about it is a separate fact the
    # board keeps showing on its own afterwards.
    return 200, {"accepted": True, "thread": thread, "unit": unit, "at": at,
                 "detail": "запрос принят: продакт проверит состояние и продолжит план",
                 "wake": state.wake_state(thread)}


class LiveHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, anonymize: bool, timeline: Path, **kwargs):
        self.anonymize, self.timeline = anonymize, timeline
        super().__init__(*args, **kwargs)

    def _send(self, body: bytes, kind: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict, code: int = 200) -> None:
        self._send(json.dumps(payload, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8", code)

    def _live_page_url(self) -> str:
        """Point a saved copy to this loopback service, never to a Host header."""
        port = self.server.server_address[1]
        return f"http://127.0.0.1:{port}/"

    def _same_origin(self) -> bool:
        """Whether this request came from the board itself.

        The door stays exactly as wide as it was: a page served from here may
        ask, anything else may not. A browser sets `Origin` on every cross-origin
        fetch and cannot be told not to, and `Sec-Fetch-Site` says the same thing
        from the other side; a request with neither is not coming from a page.
        """
        site = self.headers.get("Sec-Fetch-Site")
        if site and site != "same-origin":
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host, port = self.server.server_address[:2]
        parsed = urlparse(origin)
        return (parsed.scheme == "http" and parsed.port == port
                and parsed.hostname in (host, "localhost", "127.0.0.1"))

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path.startswith("/data.json"):
            data = build_payload(self.anonymize, self.timeline, "/data.json",
                                 live_page_url=self._live_page_url())
            self._send(json.dumps(data, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")
            # After the answer, never before it: the poll must not wait for a
            # look at the disk, and the records this look writes are on the
            # screen one poll later.
            record_timeline(self.timeline)
            return
        if self.path in ("/", "/index.html"):
            data = build_payload(self.anonymize, self.timeline, "/data.json",
                                 include_task_cards=True,
                                 live_page_url=self._live_page_url())
            self._send(render.render(data).encode(), "text/html; charset=utf-8")
            record_timeline(self.timeline)
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if urlparse(self.path).path != "/wake":
            self.send_error(404)
            return
        if not self._same_origin():
            self._send_json({"accepted": False,
                             "detail": "запрос не с этой доски"}, 403)
            return
        if not (self.headers.get("Content-Type") or "").startswith(WAKE_CONTENT_TYPE):
            self._send_json({"accepted": False,
                             "detail": f"ожидался {WAKE_CONTENT_TYPE}"}, 415)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            thread = str(body["thread"])
        except (ValueError, KeyError, TypeError):
            self._send_json({"accepted": False,
                             "detail": "в запросе нет направления"}, 400)
            return
        code, answer = wake(thread)
        self._send_json(answer, code)

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
    parser.add_argument("--live-page-url",
                        help="address of the live board to show in a saved --out snapshot")
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

    # Before collecting, unlike live mode: a recording is made once and has no
    # next poll to carry a look taken after it.
    record_timeline(args.timeline)
    snapshot_path = collect(args.anonymize, include_task_cards=True)
    try:
        if args.snapshot_out:
            args.snapshot_out.write_text(snapshot_path.read_text())
        data = render.payload(args.timeline, snapshot_path, anonymize=args.anonymize,
                              live_page_url=args.live_page_url)
    finally:
        snapshot_path.unlink(missing_ok=True)

    html = render.render(data)
    args.out.write_text(html)
    board = data["board"]
    plates = sum(len(a["plates"]) for p in board["panels"] for a in p["areas"])
    hidden = sum(a["hidden"] for p in board["panels"] for a in p["areas"])
    print(f"{args.out} — {len(html)} байт, режим {data['snapshot']['mode']}, "
          f"лента {data['timeline_total']} записей, из них на экране {len(data['timeline'])}, "
          f"панелей {len(board['panels'])}, "
          f"плашек {plates}, скрыто {hidden}")
    return 0


if __name__ == "__main__":
    sys.exit(run_entrypoint(main))
