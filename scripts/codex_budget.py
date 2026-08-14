#!/usr/bin/env python3
"""Remaining Codex subscription budget, from the CLI's own session records.

Codex writes a `rate_limits` snapshot into every session rollout file. The most
recent one is the truth about the weekly window; nothing else on this host knows
it. Exit code is 0 when there is room, 1 when the window is close enough that
heavy work should not start.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone

SESSIONS = os.path.expanduser("~/.codex/sessions")
WEEKLY_WINDOW_MINUTES = 7 * 24 * 60


def weekly_snapshot(line: str, path: str) -> dict | None:
    """Read the explicitly seven-day limit from one Codex event."""
    try:
        event = json.loads(line)
        payload = event["payload"]
        info = payload.get("info") if isinstance(payload, dict) else None
        rate_limits = payload.get("rate_limits") if isinstance(payload, dict) else None
        if not isinstance(rate_limits, dict) and isinstance(info, dict):
            rate_limits = info.get("rate_limits")
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    if not isinstance(rate_limits, dict):
        return None
    weekly = next((limit for limit in (
        rate_limits.get("primary"), rate_limits.get("secondary"),
        rate_limits.get("individual_limit"),
    ) if isinstance(limit, dict)
        and limit.get("window_minutes") == WEEKLY_WINDOW_MINUTES), None)
    if weekly is None:
        return None
    used = weekly.get("used_percent")
    resets = weekly.get("resets_at")
    observed_at = event.get("timestamp")
    if (isinstance(used, bool) or not isinstance(used, (int, float))
            or not 0 <= float(used) <= 100
            or isinstance(resets, bool) or not isinstance(resets, (int, float))
            or not isinstance(observed_at, str)):
        return None
    return {
        "used_percent": float(used),
        "remaining_percent": round(100 - float(used), 1),
        "window_minutes": WEEKLY_WINDOW_MINUTES,
        "window_days": 7.0,
        "resets_at_epoch": float(resets),
        "resets_at": datetime.fromtimestamp(
            resets, timezone.utc).isoformat(timespec="minutes"),
        "resets_in_days": round(
            (float(resets) - datetime.now(timezone.utc).timestamp()) / 86400, 1),
        "observed_at": observed_at,
        "observed_from": os.path.basename(path),
    }


def latest() -> dict | None:
    files = sorted(glob.glob(os.path.join(SESSIONS, "*", "*", "*", "*.jsonl")), reverse=True)
    for path in files[:20]:
        found = None
        try:
            with open(path, encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    if '"rate_limits"' in line:
                        snapshot = weekly_snapshot(line, path)
                        if snapshot:
                            found = snapshot
        except OSError:
            continue
        if found:
            return found
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--heavy-threshold", type=float, default=80.0,
                        help="процент окна, выше которого тяжёлые задачи не запускаем")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    args = parser.parse_args()

    state = latest()
    if not state:
        print("бюджет Codex неизвестен: снимков rate_limits в сессиях нет")
        return 0
    state["heavy_work_allowed"] = state["used_percent"] < args.heavy_threshold
    if args.format == "json":
        print(json.dumps(state, ensure_ascii=False, indent=2))
    else:
        verdict = "можно" if state["heavy_work_allowed"] else "НЕ запускать тяжёлое на Codex"
        print(f"Codex: израсходовано {state['used_percent']}% недельного окна, "
              f"осталось {state['remaining_percent']}%, сброс {state['resets_at']} "
              f"(через {state['resets_in_days']} дн) — {verdict}")
    return 0 if state["heavy_work_allowed"] else 1


if __name__ == "__main__":
    sys.exit(main())
