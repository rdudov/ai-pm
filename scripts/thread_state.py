#!/usr/bin/env python3
"""Observable state of one product thread, projected from the one observer.

This file used to *be* an observer. It had its own `pid_alive` built on
`os.kill`, its own reader of `status.json` and `progress.json`, its own
`repo_state` and its own idea of what «требует внимания» means — all of it a
second implementation of what `process_map_state.py` already does for the
board. Two observers of one disk is exactly why there was no trustworthy source
of the flow of work: the wake-up and the board counted liveness, freshness and
status each in their own way and could disagree, and neither could be checked
against the other because neither was the original.

So there is one observer now. `process_map_state.build(anonymize=False,
only=<thread>)` collects the direction, and everything below is projection —
renaming and grouping, no second judgement. In particular:

* liveness is `task_runner.process_is_live`, which compares the recorded PID
  *and* the kernel start tick. The `os.kill(pid, 0)` this file used answers
  «some process holds this number», and PIDs are reused: after a wrap an
  unrelated process counted as a live run of the task, so the tick could stay
  silent about a run that had ended and shout about one that had not started;
* freshness is still the mtime of an observed file and never a timestamp a child
  wrote itself;
* «требует внимания» now includes work that carried on outside a dead owner —
  the case that cost three and a half hours on 757 and was invisible to both
  observers before — and a task whose run tried to tell the person something and
  could not, which is how nine days of refused pushes stayed invisible.

The output shape is the one `thread_tick.py` has always consumed, field for
field, because the tick is a caller of this module and not its subject.

It never reads child transcripts, so a tick costs the same regardless of how
much work happened.

Exit code 0 always; the caller decides what to do with the report.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import process_map_state as observer  # noqa: E402
from process_map_schema import run_entrypoint, startable  # noqa: E402

HOME = observer.HOME
CONFIG = observer.CONFIG
# Tasks and their runtime state stay in the development environment; the product
# owner only reads them.
REPO = observer.REPO
# States a run record may hold once nobody is expected to come back to it.
# Anything else, with the process gone, means work was left outstanding.
TERMINAL_RUN_STATES = {"completed", "complete", "superseded"}
# Statuses in which the task itself is closed on purpose. A leftover run record
# under one of these is history, not an abandoned run.
CLOSED_TASK_STATUSES = {"completed", "cancelled"}
# How many entries the attention list carries. Unchanged from when this file
# observed for itself; what the cap drops is counted out loud by the caller.
ATTENTION = 12
PROCESS_STATE = HOME / "state" / "process-inventory"


def load_thread(name: str) -> dict:
    config = json.loads(CONFIG.read_text())
    try:
        return config["threads"][name]
    except KeyError:
        raise SystemExit(f"unknown thread: {name}; known: {sorted(config['threads'])}")


def process_inventory(name: str, config: dict) -> list[dict]:
    """Current `/proc` observation plus growth since this entrypoint last ran.

    The history is written by the observer, not by the child being watched. It
    therefore supplies the second size needed for «растёт ли файл» without
    turning a daemon's self-registration into evidence about itself.
    """
    path = PROCESS_STATE / f"{name}.json"
    previous = observer.read_json(path)
    processes = observer.process_growth(
        observer.long_lived_processes(config), previous)
    record = {
        "schema_version": 1,
        "thread": name,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "processes": processes,
    }
    PROCESS_STATE.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2))
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return processes


def run_projection(task: dict) -> dict | None:
    """One task's run, in the words this module's callers already use.

    Every value here is carried across from the observer's `run` block. The two
    derived lines are the two the tick acts on, and both are stated in terms of
    what the observer saw rather than recomputed from a PID.
    """
    run = task["run"]
    if run["state"] is None and run["pid"] is None and run["progress"] is None:
        # No `status.json` at all: the task was never started, which is not the
        # same as a run in an unknown state.
        return None
    progress = run["progress"] or {}
    return {
        "progress": {
            "activity": progress.get("activity"),
            "done": progress.get("done"),
            "total": progress.get("total"),
            "written_minutes_ago": progress.get("minutes_ago"),
        } if progress else None,
        "state": run["state"],
        "current_step": run["current_step"],
        "runner": run["runner"],
        "workflow": run["workflow"],
        "pid": run["pid"],
        "process_alive": run["alive"],
        # A run that claims to be running while its process is gone is the case
        # the supervisor must surface, not hide.
        "stale_running": "stale_label" in task["flags"],
        # The same hole one step wider: the owner died leaving a non-terminal
        # run record behind. Its frontmatter may still read "planned", which
        # looks exactly like work that was never started — so nothing wakes up
        # and the task sits still. Judge by the run's own record, not the label.
        "abandoned_run": bool(run["state"]) and not run["alive"]
        and run["state"] not in TERMINAL_RUN_STATES,
        # Work that carried on after its owner died. The owner refused its
        # closing step in writing and the task directory kept moving afterwards,
        # so the artifacts are worth a look right now. This is the observation
        # neither observer had, and 757 sat finished for three and a half hours
        # behind its absence.
        "work_outside_owner": "work_outside_owner" in task["flags"],
        "moved_at": task["detail"]["moved"],
        "moved_src": task["detail"]["moved_src"],
    }


def repo_projection(repo: dict) -> dict:
    """A repository, keyed by path the way this module's callers expect."""
    if not repo.get("present"):
        return {"repo": repo.get("path") or repo["name"], "present": False}
    return {
        "repo": repo["path"],
        "present": True,
        "branch": repo["branch"],
        "head": f"{repo['head']} {repo['head_subject']}".strip(),
        "head_at": repo["head_at"],
        "tracked_dirty": repo["tracked_dirty"],
    }


def build(name: str) -> dict:
    config = load_thread(name)              # fail loudly on an unknown thread
    snapshot = observer.build(False, only=name)
    thread = snapshot["threads"][0]
    try:
        detached = process_inventory(name, config)
    except observer.ProcessInventoryUnavailable as error:
        # An empty list would mean «observed and found none». If the owning
        # registry cannot be imported, it is unsafe to make that claim: a
        # registered live run could otherwise be projected as detached work.
        detached = []
        detached_observation = {"available": False, "reason": str(error)}
    else:
        detached_observation = {"available": True, "reason": None}

    # This entrypoint is the wake-up's own observation, so it is the one allowed
    # to record how much processor time each awake owner had burned by now. The
    # board must not: it is rebuilt every few seconds in live mode, and a
    # difference measured over seconds would make every owner look idle.
    observer.write_owner_observations(snapshot["owners_awake"])

    live, attention = [], []
    for task in thread["tasks"]:
        state = run_projection(task)
        entry = {"id": task["id"], "title": task["title"], "status": task["status"],
                 "path": f"tasks/{task['dir']}", "run": state}
        stale_open = bool(state) and state["stale_running"] and task["status"] not in CLOSED_TASK_STATUSES
        if state and (state["process_alive"] or stale_open):
            live.append(entry)
        abandoned = bool(state) and state["abandoned_run"] and task["status"] not in CLOSED_TASK_STATUSES
        outside = bool(state) and state["work_outside_owner"]
        # Something the run tried to tell the person and could not, with nothing
        # delivered since. Status is deliberately not part of this: the nine days
        # of 2026-08-13 were invisible precisely because every one of the 41
        # affected tasks finished cleanly, so a rule that only looks at open work
        # would have missed the whole of it (task 1255). The observer decides
        # what still counts as current; this adapter only projects.
        silent = (task["detail"].get("delivery") or {}).get("unresolved")
        if silent and silent.get("current"):
            entry["undelivered_notification"] = silent
        if (task["status"] in {"blocked", "in_progress"} or abandoned or outside
                or stale_open or "undelivered_notification" in entry):
            attention.append(entry)
    return {
        "thread": name,
        "title": thread["title"],
        "products": thread["products"],
        "live_runs": live,
        # Processes a finished task left behind. The process-map collector owns
        # attribution and duplicate detection; this adapter only persists the
        # previous observed sizes so growth can be stated from two measurements.
        "long_lived_processes": detached,
        "long_lived_processes_observation": detached_observation,
        "needs_attention": attention[:ATTENTION],
        "repos": [repo_projection(repo) for repo in thread["repos"]],
        "task_count": thread["task_count"],
        # Work with nothing holding it, so a wake-up can answer «что подхватить»
        # from the same observation the board answers it from, rather than by
        # reading the whole list again.
        "can_pick_up": [{"id": task["id"], "title": task["title"]}
                        for task in thread["tasks"] if task["board"]["area"] == "pickup"],
        # Очередь этого направления, как её установил портфельный план. Своим
        # полем, а не внутри «можно подхватить»: у этой работы есть назначенное
        # место, и порядок принадлежит плану. Пока очередь выводилась из
        # наблюдения, эти задачи стояли в «можно подхватить» вперемешку с теми,
        # о которых план не говорит вовсе, — и вместе с теми, которые
        # пользователь остановил своим словом.
        "queued_by_plan": [
            {"id": task["id"], "title": task["title"],
             "position": task["board"]["plan_place"]["position"],
             "ahead": task["board"]["plan_place"]["ahead"],
             "src": task["board"]["plan_place"]["src"]}
            for task in sorted(
                # Наблюдаемый держатель остаётся держателем: задача, за которой
                # стоит незакрытая предшественница или занятое рабочее дерево, к
                # запуску не готова, какое бы место ей ни назначил план. Это и
                # ещё три области — один предикат `startable` в контракте, тот
                # же самый, которым доска решает, показывать ли «продолжить
                # сейчас»: два ответа на вопрос «есть ли что начать» были бы две
                # очереди.
                (t for t in thread["tasks"]
                 if t["board"]["area"] == "queued" and startable(t)),
                key=lambda t: t["board"]["plan_place"]["position"])],
        # И то, что план держит своим словом. Пробуждению это не работа на
        # выбор, и раньше ровно она предлагалась к запуску. Держит только та
        # строка, предметом которой задача стоит: чтение «держащая строка держит
        # любой названный ею номер» снято, потому что строка «исследование 1150 и
        # 1151 разрешено» убирала из доступной работы прямо разрешённую
        # пользователем (круг 3 независимого ревью). Молчание плана сюда не
        # входит: задача, которой текущая редакция не называет, разобрана в
        # своём task.md ничуть не меньше и запускается как прежде (круг 1
        # независимого ревью, HIGH-2). `src` называет строку, которой она стоит.
        "backlog": [{"id": task["id"], "title": task["title"],
                     "src": (task["board"]["plan_place"] or {}).get("src")}
                    for task in thread["tasks"] if task["board"]["area"] == "backlog"],
        # Work whose start condition was written down and has since been met.
        # Kept apart from «можно подхватить» on purpose: both say nothing is
        # holding the task, but only this one says somebody decided in advance
        # when it may start and that moment has arrived. 831 was exactly this and
        # had nowhere to be seen — it stood forty minutes after 830 closed and
        # moved only when the user asked how the queue is tracked.
        "ready_to_start": [
            {"id": task["id"], "title": task["title"],
             "condition": task["board"]["start_condition"],
             # What the condition asked for and what was observed instead of it,
             # so «готово» can be checked rather than believed.
             "met": (task["board"]["start_condition"] or {}).get("met") or [],
             "met_src": (task["board"]["start_condition"] or {}).get("src")}
            for task in thread["tasks"] if task["board"]["area"] == "ready_to_start"],
        # Decisions recorded on a task that nothing observed carried out. The
        # other half of the same hole: on 2026-08-06 «из девяти живых документов
        # человеку идут три» was written down and three hours later none had gone
        # out, because a decision in a sentence moves nobody.
        "decided_not_done": [
            {"id": task["id"], "title": task["title"],
             "decision": task["board"]["decision"]["kind"],
             "src": task["board"]["decision"]["src"]}
            for task in thread["tasks"] if task["board"]["area"] == "decision_unmet"],
        # Ready work whose document nobody was shown, with how long it has
        # stood that way. The task directory is the observer's existing `dir`
        # field; carry the exact missing set rather than guessing one file from
        # the largest artifact.
        "undelivered": [
            {"id": task["id"], "title": task["title"],
             "path": f"tasks/{task['dir']}",
             "age_seconds": task["board"]["age_seconds"],
             "missing": (task["detail"]["handoff"] or {}).get("missing") or [],
             "src": (task["detail"]["handoff"] or {}).get("delivered_src")}
            for task in thread["tasks"] if task["board"]["area"] == "undelivered"],
        # Work standing on an answer from the user. Not something to start, and
        # exactly one of the named reasons a wake-up may start nothing.
        "waiting_user": [{"id": task["id"], "title": task["title"]}
                         for task in thread["tasks"]
                         if task["board"]["area"] == "waiting_human"],
        # The repositories this direction owns, as declared rather than as found
        # on disk: yielding is about who may occupy which tree, and a tree that
        # is missing today is still this direction's and not a neighbour's.
        "worktrees": list(config.get("repos", [])),
        # Other instances of the product owner deciding right now. A tick that
        # cannot see one creates the task the other one just created.
        "owners_awake": snapshot["owners_awake"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("thread")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    args = parser.parse_args()
    report = build(args.thread)
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(f"# {report['title']} ({report['thread']}) — задач {report['task_count']}")
    print(f"живых прогонов: {len(report['live_runs'])}")
    for item in report["live_runs"]:
        run = item["run"]
        flag = " ПРОЦЕСС УМЕР" if run["stale_running"] else ""
        line = f"  {item['id']} {run['state']}{flag} — {run['current_step']}"
        if run.get("progress"):
            p = run["progress"]
            line += f"\n      шаг {p['done']}/{p['total']}, запись {p['written_minutes_ago']} мин назад: {p['activity']}"
        print(line)
    observation = report["long_lived_processes_observation"]
    if not observation["available"]:
        print(f"опись долгоживущих процессов недоступна: {observation['reason']}")
    else:
        print(f"долгоживущих процессов завершённых задач: {len(report['long_lived_processes'])}")
    for process in report["long_lived_processes"]:
        duplicate = (f" ДУБЛЬ ×{process['duplicate_count']}"
                     if process["duplicate"] else "")
        print(f"  задача {process['task']} {process['command']}{duplicate} — "
              f"pid {process['pid']}, живёт {process['age_seconds'] // 60} мин, "
              f"репозиторий {process['repo']}")
        for output in process["outputs"]:
            growth = (f"растёт: +{output['growth_bytes']} байт"
                      if output["growing"] is True else
                      "рост не наблюдался" if output["growing"] is False else
                      "рост пока не измерен")
            relation = ("пишет" if output["growing"] is True else
                        "открыт на запись" if output.get("direct") else
                        "кандидат рядом с процессом")
            print(f"      {relation} {output['path']} ({output['size']} байт; {growth}; "
                  f"{output['growth_src']})")
    print(f"требуют внимания: {len(report['needs_attention'])}")
    for item in report["needs_attention"]:
        mark = " РАБОТА ШЛА ВНЕ УМЕРШЕГО ВЛАДЕЛЬЦА" if (item["run"] or {}).get("work_outside_owner") else ""
        print(f"  {item['id']} {item['status']}{mark} — {item['title'][:70]}")
    print(f"готово к запуску: {len(report['ready_to_start'])}")
    for item in report["ready_to_start"]:
        print(f"  {item['id']} — {item['title'][:70]}")
        for line in item["met"]:
            print(f"      условие снято: {line}")
    print(f"решено, но не исполнено: {len(report['decided_not_done'])}")
    for item in report["decided_not_done"]:
        print(f"  {item['id']} {item['decision']} — {item['title'][:70]}")
        print(f"      исполнения не наблюдается: {item['src']}")
    print(f"можно подхватить: {len(report['can_pick_up'])}")
    for item in report["can_pick_up"][:8]:
        print(f"  {item['id']} — {item['title'][:70]}")
    print(f"документ готов, но не доставлен: {len(report['undelivered'])}")
    for item in report["undelivered"][:8]:
        stood = "" if item["age_seconds"] is None else f", стоит {item['age_seconds'] // 60} мин"
        print(f"  {item['id']} — {item['title'][:70]}{stood}")
        print(f"      документ: {item['document']}; {item['src']}")
    print(f"ждёт ответа пользователя: {len(report['waiting_user'])}")
    for item in report["waiting_user"]:
        print(f"  {item['id']} — {item['title'][:70]}")
    for owner in report["owners_awake"]:
        who = ("фоновое пробуждение треда «{}»".format(owner["thread"])
               if owner["kind"] == "tick" else
               "почтовое пробуждение продакта" if owner["kind"] == "mail" else
               "продакт, разбуженный тиком" if owner["kind"] == "woken" else
               "продакт в консоли")
        trees = ", ".join(owner["worktrees"]) or "рабочее дерево не названо"
        print(f"  бодрствует {who}: может занять {trees} ({owner['src']})")
    for repo in report["repos"]:
        if not repo["present"]:
            print(f"  репозиторий отсутствует: {repo['repo']}")
            continue
        print(f"  {Path(repo['repo']).name}: {repo['branch']} @ {repo['head']} (грязных {repo['tracked_dirty']})")


if __name__ == "__main__":
    sys.exit(run_entrypoint(main))
