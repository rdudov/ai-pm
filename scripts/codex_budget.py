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
import re
import sys
from datetime import datetime

SESSIONS = os.path.expanduser("~/.codex/sessions")
PATTERN = re.compile(
    r'"used_percent":([\d.]+).*?"window_minutes":(\d+).*?"resets_at":(\d+)'
)


def latest() -> dict | None:
    files = sorted(glob.glob(os.path.join(SESSIONS, "*", "*", "*", "*.jsonl")), reverse=True)
    for path in files[:20]:
        found = None
        try:
            with open(path, encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    if '"rate_limits"' in line:
                        match = PATTERN.search(line)
                        if match:
                            found = match
        except OSError:
            continue
        if found:
            used, window, resets = float(found.group(1)), int(found.group(2)), int(found.group(3))
            return {
                "used_percent": used,
                "remaining_percent": round(100 - used, 1),
                "window_days": round(window / 1440, 1),
                "resets_at": datetime.fromtimestamp(resets).isoformat(timespec="minutes"),
                "resets_in_days": round((resets - datetime.now().timestamp()) / 86400, 1),
                "observed_from": os.path.basename(path),
            }
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
