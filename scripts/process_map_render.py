#!/usr/bin/env python3
"""Builds the map: one self-contained HTML file with the recording inside it.

The renderer never reads a repository, a task directory or the disk. It gets a
snapshot document and a timeline, both already checked against
`process_map_schema`, and nothing else — so whatever is private cannot leak
through a picture that never saw it.

The snapshot is a required argument and this module imports no collector, so the
boundary is physical rather than a convention about how to call it: there is no
code path from here to a repository or a task directory. Collecting a fresh
snapshot and serving it live is the job of `process_map_serve.py`, which sits on
the other side of that boundary.

    --snapshot state.json --out map.html

produces a recording: data embedded, opens by double click with the network off,
and is the file you hand over. `file://` cannot fetch, which is why the recording
embeds its data rather than loading it — a build requirement, not a detail.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from process_map_schema import (BOARD_AREA_RU, BOARD_AREAS, scrub,
                                validate_record, validate_snapshot)

HOME = Path(__file__).resolve().parents[1]
TEMPLATE = Path(__file__).resolve().parent / "process_map_template.html"
DEFAULT_TIMELINE = HOME / "state" / "timeline.jsonl"

# How many task objects one area may carry before the picture stops being
# readable. Everything dropped is counted out loud in the area caption: a silent
# cap would read as «показано всё», and it is not.
PER_AREA = 12

# Order of interest when the cap bites. A blocked object and a lying label are
# the reason someone opens the map at all; an idle object is the least of it.
INTEREST = ["live", "blocked", "killed", "stale_label", "gap", "delivered", "idle"]


def load_timeline(path: Path, anonymize: bool = False) -> list[dict]:
    """The timeline as the page will see it, cleaned when the showing is anonymous.

    `--anonymize` used to mean «the scribe was told to write this file safely»,
    so live mode handed over whatever was on disk and trusted its provenance
    (finding HIGH-3 of review 786). It now means what it says: the document being
    shown is cleaned on its way to the screen. Cleaning is idempotent, so an
    already-anonymous timeline is unchanged by passing through here again.
    """
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
        if anonymize:
            record = scrub(record)
        records.append(validate_record(without_unsourced_actor(record)))
    # Sorting by the string would misplace anything written with a local offset
    # — git stamps commits as `+03:00`, the rest of the contour as `+00:00`.
    records.sort(key=instant)
    return records


def without_unsourced_actor(record: dict) -> dict:
    """Drop a participant nothing named, keeping the record itself.

    Records written before the scribe stopped inventing an actor say things like
    `actor: исполнитель` with no source behind them. Refusing such a record would
    throw away a real observation because of one wrong caption, so the caption
    goes and the observation stays.
    """
    if record.get("actor") and not str(record.get("actor_src") or "").strip():
        record = {key: value for key, value in record.items() if key != "actor"}
    return record


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
        areas[index].setdefault("questions", []).extend(
            q["text"] for q in product["questions"])
    for area in areas:
        area.setdefault("questions", [])

    waiting = [{"text": question["text"], "src": product["slug"]}
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


# How many plates one area of one panel shows before the column stops being
# readable. What the cap dropped is counted out loud in the area caption.
PER_BOARD_AREA = 25

# How many names the strip above the columns carries. The strip answers two of
# the three acceptance questions by name, so it has to be readable at a glance
# and has to leave the board itself on the screen.
NOW_IN_STRIP = 3
JAMS_IN_STRIP = 4
# «Что подхватить» is a shortlist to choose from, not a catalogue: the whole
# area stands in the columns below and the strip says how many it left there.
PICKUP_IN_STRIP = 3
# «Сделано, но не доставлено» is a shortlist too: the point is that the person
# learns such a thing exists at all, and the area below carries the rest.
UNDELIVERED_IN_STRIP = 3

# Characters of name plus reason one strip group may take. The count is the whole
# mechanism that keeps the strip short: the page shows every reason it prints in
# full — no ellipsis, no line clamp — so the only honest way to bound the strip
# is to print fewer names and say how many were left out. Review 789 found the
# other way round: four jams named, one reason readable, three running past the
# right edge of the window.
#
# Measured in a browser, not guessed: at 390×844 — the narrower of the two sizes
# the board is checked on — a group of this many characters wraps into a block
# that still leaves «Ждёт решения человека» and the top of the columns on the
# first screen. Raising it is a change to be re-measured on a phone size, not a
# constant to tune by eye.
# Re-measured when the strip went from three groups to five: «что подхватить»
# and «другой я» were added, and the budget that had been measured for three
# groups let the strip take 296 of 900 pixels, leaving 286 for seven areas —
# every area crushed to its minimum and the columns answering nothing. This is
# the same lesson the comment above records, paid a second time: the budget is a
# measurement of a layout, so adding a group means measuring again.
#
# At 1440×900 with five groups this leaves the strip at about a sixth of the
# window and every area of every column its own room; at 390×844 the strip is
# the first-screen answer and still scrolls to at most a quarter of it. Raising
# it is a change to be re-measured on both sizes, not a constant to tune by eye.
STRIP_GROUP_CHARS = 190


def strip_group(items: list[dict], cap: int, budget: int = STRIP_GROUP_CHARS) -> tuple[list[dict], int]:
    """The names of one strip group that fit with their reason shown whole.

    Returns what to show and how many were left for the columns below. The first
    item is always shown: «где затор» with nothing named answers nothing, and a
    single long reason is still one honest answer.
    """
    shown: list[dict] = []
    used = 0
    for item in items[:cap]:
        reason = item.get("why") or item.get("happening") or ""
        cost = len(item.get("title") or "") + len(reason)
        if shown and used + cost > budget:
            break
        shown.append(item)
        used += cost
    return shown, len(items) - len(shown)


def plate(task: dict) -> dict:
    """One task as the user described it: number, name, status, who, what, how long."""
    board = task["board"]
    run = task["run"]
    return {
        "id": task["id"],
        "task": task["dir"],
        "title": task["title"] or task["dir"],
        "status": task["status"],
        "status_detail": task.get("status_detail"),
        "actor": board["actor"],
        "actor_src": board["actor_src"],
        "role": board["role"],
        "role_src": board["role_src"],
        "happening": board["happening"],
        # Why it stands where it stands, and what observed that. Without it the
        # board named the jam and left «почему» unanswered while the answer sat
        # in the same payload (finding HIGH-2 of review 786).
        "why": board["why"],
        "why_src": board["why_src"],
        "age_seconds": board["age_seconds"],
        # The instant, so the page can keep the age honest between refreshes
        # instead of freezing it at the moment the snapshot was collected, and
        # what its mtime actually belongs to (finding MEDIUM-1).
        "since": board["since"],
        "since_src": board["since_src"],
        # A live process and a label saying «running» are two different facts,
        # and the contour has already been burned by reading one as the other.
        # They stay two fields, so a dead run under a living label reads as the
        # jam it is instead of as work in progress.
        "alive": run["alive"],
        "stale_label": "stale_label" in task["flags"],
        "attempt": board["attempt"],
        "questions": task.get("questions") or [],
        # Kept apart on the plate for the same reason they are kept apart in the
        # document: «спросили тебя и ты не ответил» and «мы ещё не решили» are
        # two sentences, and merging them is what put thirteen of our own
        # questions in front of the user.
        "asked_user": task.get("asked_user") or [],
        "our_questions": task.get("our_questions") or [],
        "flags": task["flags"],
        # What is holding a queued task. «В очереди» on its own is a label; the
        # user asked for «за чем именно стоит», and the answer is observed.
        "blocked_by": board["blocked_by"],
        "blocked_by_src": board["blocked_by_src"],
        # Everything the card shows when the plate is opened. It travels with
        # the plate rather than being fetched, because the page has no way to
        # reach a disk and must not grow one.
        "detail": {
            **task["detail"],
            "gates": task.get("gates") or [],
            "dir": task["dir"],
            "runner": run.get("runner"),
            "workflow": run.get("workflow"),
            "sandbox": run.get("sandbox"),
            "state": run.get("state"),
            "current_step": run.get("current_step"),
            "refusal": run.get("refusal"),
            "refusal_summary": run.get("refusal_summary"),
            "repo": run.get("repo"),
            "progress": run.get("progress"),
            "stop_reason": run.get("stop_reason"),
            "exit_code": run.get("exit_code"),
        },
    }


def board_age(tasks: list[dict]) -> int | None:
    """How long the oldest plate of an area has stood there."""
    ages = [t["age_seconds"] for t in tasks if t["age_seconds"] is not None]
    return max(ages) if ages else None


def product_questions(snapshot: dict, thread: dict, owner: str = "user") -> list[dict]:
    """The canonical questions of the products this direction owns, by owner.

    `## Открытые вопросы` of a product is the one place written specifically to
    hold questions for the user, and the board used to ignore it completely: the
    number above the columns counted task plates only, so seventeen questions
    sitting in the same payload were absent from the answer to «что ждёт решения
    человека» (finding HIGH-1 of review 786).

    They stand in the same area of the same panel as the task plates and are
    added into the same count. A question is a question wherever it was written
    down; two competing counters of one concept would be the defect again under
    a nicer name.

    Which area, though, depends on who owes the answer, and the collector has
    already decided that — `questions` are the user's, `own_questions` are ours.
    This function only carries the decision to the panel that owns the product.
    """
    mine = set(thread.get("products") or [])
    field = "questions" if owner == "user" else "own_questions"
    return [{**question, "product": product["slug"]}
            for product in snapshot["products"] if product["slug"] in mine
            for question in product[field]]


def product_promises(snapshot: dict, thread: dict) -> list[dict]:
    """Lines of this direction's products for which no task could be observed.

    The fourth question of the board, and the one with a price already paid: a
    request to review the companion code stood in `## В работе` of the product
    record for two days and never became a task, because the flow had nowhere
    to put «надо запланировать».

    What each line carries with it is the comparison that failed, and that is
    the whole difference from what shipped before. The area used to print «в
    строке нет номера задачи» as if it meant «задачи нет», and three of the four
    lines it showed already had tasks (finding HIGH-1 of review 814). A failed
    comparison is «связь не установлена» — the reader is told what was compared,
    so the list can be judged instead of believed.
    """
    mine = set(thread.get("products") or [])
    return [{"text": promise["text"], "product": product["slug"],
             "link": promise["link"], "checked": promise["checked"]}
            for product in snapshot["products"] if product["slug"] in mine
            for promise in product.get("promises") or []]


def build_board(snapshot: dict) -> dict:
    """Four direction panels, each split into areas by urgency.

    The order of the areas is the schema's, not this function's: «ждёт решения
    человека» has to be first and visible even when empty, because that is the
    whole answer to one of the three acceptance questions.
    """
    panels = []
    for thread in snapshot["threads"]:
        plates = [plate(task) for task in thread["tasks"]]
        questions = product_questions(snapshot, thread, "user")
        ours = product_questions(snapshot, thread, "product")
        promises = product_promises(snapshot, thread)
        areas = []
        for key in BOARD_AREAS:
            mine = [p for p, task in zip(plates, thread["tasks"])
                    if task["board"]["area"] == key]
            # Oldest first inside an area: time in a state is the jam. Sorted on
            # the instant rather than on the age derived from it — the age is a
            # rounded number of seconds, so two plates a fraction apart would
            # swap places between two collections and look like a change.
            mine.sort(key=lambda p: (p["since"] or "9999", -(p["id"] or 0)),
                      # «Сделано, но не доставлено» is the one area where time in
                      # the state is not the problem: an old finished task was
                      # either handed over some other way or has stopped
                      # mattering, and the document somebody is waiting for right
                      # now is the freshest one. So this area alone reads newest
                      # first, and the strip below sorts it the same way.
                      reverse=key == "undelivered")
            shown = mine[:PER_BOARD_AREA]
            asked = questions if key == "waiting_human" else (
                ours if key == "product_owner" else [])
            # «Надо запланировать» carries no tasks by construction: a line with
            # an observed task behind it is that task and stands in one of the
            # areas above. The area is the only one whose whole content is text
            # from a product record, so the rule that selected it is shown with
            # it, and each line says what was compared against it.
            owed = promises if key == "plan" else []
            areas.append({
                "key": key,
                "title": BOARD_AREA_RU[key],
                # One count for one concept: a question of a product and a task
                # whose question is still open are the same statement about the
                # same person, so they are counted together or the number lies.
                "count": len(mine) + len(asked) + len(owed),
                "hidden": len(mine) - len(shown),
                "oldest": board_age(mine),
                # «Можно подхватить» is the first question of every wake-up, so
                # it opens with the board rather than folded. `done` and the
                # queue behind a named holder are reference, not the question.
                "collapsed": key in ("queued", "done"),
                "plates": shown,
                "questions": asked,
                "promises": owed,
            })
        panels.append({
            "key": thread["key"],
            "title": thread["title"],
            "task_count": thread["task_count"],
            "areas": areas,
            "channels": thread["channels"],
            # Commits and pushes stay at the level of the product: nothing on
            # disk ties a commit to a task number, and the map does not invent
            # a tie it cannot observe.
            "repos": [r for r in thread["repos"] if r.get("present")],
            # When this direction last looked and what came of it. Carried
            # through untouched: the tick recorded it at the moment of the
            # check, and a renderer that recomputed any of it would be answering
            # from a different instant than the one it labels.
            "check": thread["check"],
            # And when it looks next, as systemd reported it while the snapshot
            # was being collected. Carried through on the same rule.
            "next_check": thread["next_check"],
        })
    # A strip above the columns, so the two questions a person opens the board
    # for — who is working, where the jam is — are answered without scrolling
    # and by name, not by a count. The columns below carry the detail.
    now = []
    jams = []
    pickup = []
    for panel in panels:
        for area in panel["areas"]:
            if area["key"] == "running":
                now += [{**p, "thread": panel["title"]} for p in area["plates"]]
            if area["key"] == "stuck":
                jams += [{**p, "thread": panel["title"]} for p in area["plates"]]
            if area["key"] == "pickup":
                pickup += [{**p, "thread": panel["title"]} for p in area["plates"]]
    waiting_areas = [a for p in panels for a in p["areas"] if a["key"] == "waiting_human"]
    ours_areas = [a for p in panels for a in p["areas"] if a["key"] == "product_owner"]
    undelivered = [{**p, "thread": panel["title"]}
                   for panel in panels for a in panel["areas"]
                   if a["key"] == "undelivered" for p in a["plates"]]
    now_shown, now_hidden = strip_group(now, NOW_IN_STRIP)
    jams_shown, jams_hidden = strip_group(
        sorted(jams, key=lambda p: -(p["age_seconds"] or 0)), JAMS_IN_STRIP)
    # Oldest first: a task that has been startable the longest is the one most
    # likely to have been forgotten, which is the whole reason the area exists.
    pickup_shown, pickup_hidden = strip_group(
        sorted(pickup, key=lambda p: -(p["age_seconds"] or 0)), PICKUP_IN_STRIP)
    # Newest first, unlike every other group: see the sort in the area above.
    undelivered_shown, undelivered_hidden = strip_group(
        sorted(undelivered, key=lambda p: (p["since"] or ""), reverse=True),
        UNDELIVERED_IN_STRIP)
    return {
        "panels": panels,
        "areas": [{"key": k, "title": BOARD_AREA_RU[k]} for k in BOARD_AREAS],
        # A summary that fills the screen is not one: the strip sits above the
        # columns and every line it takes is a line the board loses. What the cap
        # drops is still in the columns below, under «Затор» and «В работе», and
        # the strip says how many that is instead of dropping them silently.
        "now": now_shown,
        "now_hidden": now_hidden,
        "jams": jams_shown,
        "jams_hidden": jams_hidden,
        "pickup": pickup_shown,
        "pickup_hidden": pickup_hidden,
        # Other instances of the product owner deciding right now. Two pairs of
        # duplicate tasks were created in one hour because the owner in the chat
        # and the owner woken by the timer could not see each other's queue, so
        # this stands on the strip beside «кто работает сейчас».
        "owners_awake": snapshot.get("owners_awake") or [],
        # The headline number, and the two things it is made of. A single number
        # nobody can take apart is what let thirteen wrong entries stand as an
        # answer: split by source, a reader can check it against the columns.
        "waiting": sum(a["count"] for a in waiting_areas),
        "waiting_tasks": sum(len(a["plates"]) + a["hidden"] for a in waiting_areas),
        "waiting_questions": sum(len(a["questions"]) for a in waiting_areas),
        # Ours, counted next to theirs on the same line. The split has to be
        # visible as a split: a number that merely got smaller reads as questions
        # having been dropped, and they were not — they moved to the area of the
        # person who owes them.
        "ours": sum(a["count"] for a in ours_areas),
        # Finished work whose document nobody was shown. The strip names it
        # because an hour of a 441 KB report lying on the server is exactly what
        # nobody was looking at (task 783).
        "undelivered": undelivered_shown,
        "undelivered_hidden": undelivered_hidden,
        "undelivered_total": sum(a["count"] for p in panels for a in p["areas"]
                                 if a["key"] == "undelivered"),
    }


def payload(timeline_path: Path, snapshot_path: Path, live_url: str | None = None,
            anonymize: bool = False) -> dict:
    """The whole document the page gets, built from a snapshot and a timeline.

    The renderer takes a validated JSON document and nothing else. It does not
    import the collector and cannot reach a repository, a task directory or the
    disk beyond these two files, so «the picture cannot show what it never saw»
    is a property of the input rather than of how someone chose to call it
    (finding HIGH-2 of review 780).

    `anonymize` cleans both documents on the way in, whoever wrote them. The
    collector already cleans a snapshot it was asked to anonymise; doing it again
    here costs nothing and removes the assumption that it was asked.
    """
    raw = json.loads(snapshot_path.read_text())
    snapshot = validate_snapshot(scrub(raw) if anonymize else raw)
    timeline = load_timeline(timeline_path, anonymize)
    data = {
        "snapshot": snapshot,
        "timeline": timeline,
        "board": build_board(snapshot),
        "world": build_world(snapshot, timeline),
        "built_at": datetime.now(timezone.utc).isoformat(),
        "live_url": live_url,
    }
    # A digest of everything shown, so live mode notices a status or a liveness
    # change that appends nothing to the timeline (finding MEDIUM-1).
    data["digest"] = digest_of(data)
    return data


# Derived from the wall clock rather than from the contour: an age ticks every
# second whether or not anything happened. Leaving them in the digest would make
# live mode reload every ten seconds forever and say nothing — the instant they
# are derived from (`since`) is in the digest, so no real change is lost.
TICKING = {"age_seconds", "oldest", "minutes_ago", "moved_age_seconds"}


def without_ticking(value):
    if isinstance(value, dict):
        return {key: without_ticking(item) for key, item in value.items() if key not in TICKING}
    if isinstance(value, list):
        return [without_ticking(item) for item in value]
    return value


def digest_of(data: dict) -> str:
    """A fingerprint of the state shown, insensitive to time simply passing."""
    shown = without_ticking({key: data[key] for key in ("snapshot", "timeline", "board", "world")})
    return hashlib.sha256(json.dumps(shown, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def render(data: dict) -> str:
    template = TEMPLATE.read_text()
    # `</script>` inside the JSON would end the tag early and break the page.
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return template.replace("__DATA__", blob)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True,
                        help="the collected snapshot; the renderer has no other way in")
    parser.add_argument("--out", type=Path, required=True,
                        help="write the self-contained recording here")
    parser.add_argument("--timeline", type=Path, default=DEFAULT_TIMELINE)
    parser.add_argument("--anonymize", action="store_true",
                        help="clean both documents on the way to the page, "
                             "whoever wrote them")
    args = parser.parse_args()

    data = payload(args.timeline, args.snapshot, anonymize=args.anonymize)
    html = render(data)
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
