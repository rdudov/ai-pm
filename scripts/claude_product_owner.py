#!/usr/bin/env python3
"""Route every product-owner entry through one observed Claude/Codex policy."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
import urllib.error
import urllib.request


HOME = Path("/opt/projects/product-owner")
CLAUDE_BIN = "/usr/local/bin/claude"
CODEX_BIN = "/usr/local/bin/codex"
CREDENTIALS_PATH = Path("/root/.claude/.credentials.json")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
OPUS_MODEL = "opus"
FABLE_MODEL = "fable"
CODEX_MODEL = "gpt-5.6-sol"
OPUS_REMAINING_THRESHOLD = 5.0
STARTUP_PROMPT = (
    "Ты работаешь как самостоятельный продакт-владелец. Полностью прочитай AGENTS.md "
    "этого каталога и следуй ему. На старте прочитай products/*/product.md, затем для "
    "каждого ключа из threads.json выполни python3 scripts/thread_state.py <ключ> "
    "--format text и проверь бюджет командой python3 scripts/codex_budget.py. Дай короткую "
    "продуктовую сводку: что изменилось или требует внимания, твой вердикт и что ты "
    "собираешься делать; после этого оставайся в интерактивном диалоге. Не превращай "
    "сводку в технический лог и не проси подтверждения для безопасных read-only проверок "
    "в доступных каталогах."
)


@dataclass(frozen=True)
class Route:
    engine: str
    model: str
    reason: str


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return min(100.0, max(0.0, float(value)))


def model_used_percentages(usage: dict[str, Any], model_name: str) -> list[float]:
    """Return only utilization explicitly scoped to one named Claude model."""
    percentages: list[float] = []
    legacy = usage.get(f"seven_day_{model_name.casefold()}")
    if isinstance(legacy, dict):
        value = _number(legacy.get("utilization"))
        if value is not None:
            percentages.append(value)
    limits = usage.get("limits")
    if not isinstance(limits, list):
        return percentages
    for limit in limits:
        if not isinstance(limit, dict):
            continue
        scope = limit.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        if not isinstance(model, dict):
            continue
        identity = " ".join(str(model.get(key) or "") for key in ("display_name", "id"))
        if model_name.casefold() not in identity.casefold():
            continue
        value = _number(limit.get("percent"))
        if value is not None:
            percentages.append(value)
    return percentages


def opus_used_percentages(usage: dict[str, Any]) -> list[float]:
    return model_used_percentages(usage, "opus")


def shared_limits(usage: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for key in ("five_hour", "seven_day"):
        value = usage.get(key)
        if not isinstance(value, dict):
            continue
        used = _number(value.get("utilization"))
        if used is None:
            continue
        result.append({
            "kind": key,
            "used_percent": used,
            "remaining_percent": 100.0 - used,
            "resets_at": value.get("resets_at"),
        })
    return result


def select_route(
    usage: dict[str, Any], threshold: float = OPUS_REMAINING_THRESHOLD
) -> Route:
    """Use Codex only when the usage payload proves no Claude route is usable."""
    exhausted_shared = [item for item in shared_limits(usage) if item["used_percent"] >= 100.0]
    if exhausted_shared:
        kinds = ",".join(item["kind"] for item in exhausted_shared)
        return Route("codex", CODEX_MODEL, f"observed_shared_limit_exhausted:{kinds}")

    opus = opus_used_percentages(usage)
    fable = model_used_percentages(usage, "fable")
    opus_used = max(opus) if opus else None
    fable_used = max(fable) if fable else None
    opus_exhausted = opus_used is not None and opus_used >= 100.0
    fable_exhausted = fable_used is not None and fable_used >= 100.0
    if opus_exhausted and fable_exhausted:
        return Route("codex", CODEX_MODEL, "observed_opus_and_fable_limits_exhausted")

    if opus_used is None:
        return Route("claude", OPUS_MODEL, "no_opus_specific_limit")
    remaining = 100.0 - opus_used
    if remaining < threshold and not fable_exhausted:
        return Route("claude", FABLE_MODEL, f"opus_remaining={remaining:g}%")
    if remaining < threshold and fable_exhausted:
        return Route(
            "claude", OPUS_MODEL,
            f"fable_exhausted_but_opus_remaining={remaining:g}%",
        )
    return Route("claude", OPUS_MODEL, f"opus_remaining={remaining:g}%")


def select_model(
    usage: dict[str, Any], threshold: float = OPUS_REMAINING_THRESHOLD
) -> tuple[str, str]:
    """Backward-compatible Claude model view for existing callers and tests."""
    percentages = opus_used_percentages(usage)
    if not percentages:
        return OPUS_MODEL, "no_opus_specific_limit"
    remaining = 100.0 - max(percentages)
    if remaining < threshold:
        return FABLE_MODEL, f"opus_remaining={remaining:g}%"
    return OPUS_MODEL, f"opus_remaining={remaining:g}%"


def fetch_usage(
    credentials_path: Path = CREDENTIALS_PATH,
    usage_url: str = USAGE_URL,
) -> dict[str, Any]:
    credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
    token = credentials["claudeAiOauth"]["accessToken"]
    request = urllib.request.Request(
        usage_url,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_BETA,
            "User-Agent": "product-owner-engine-router/2",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("usage response is not an object")
    return payload


def inspect_live() -> tuple[Route, dict[str, Any] | None, str | None]:
    try:
        usage = fetch_usage()
        return select_route(usage), usage, None
    except (OSError, KeyError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        return Route("claude", OPUS_MODEL, "usage_unavailable"), None, f"{type(exc).__name__}: {exc}"


def claude_command(model: str, entry: str | None, extra: list[str]) -> list[str]:
    if entry == "interactive":
        return [
            CLAUDE_BIN, "--model", model, "--name", "product-owner",
            "--add-dir", "/opt/projects", "--dangerously-skip-permissions",
            "--setting-sources", "project", *extra,
        ]
    if entry == "print":
        return [
            CLAUDE_BIN, "--model", model, "--print", "--name", "product-owner-background",
            "--add-dir", "/opt/projects", "--dangerously-skip-permissions",
            "--setting-sources", "project", *extra,
        ]
    return [CLAUDE_BIN, "--model", model, *extra]


def codex_command(entry: str, extra: list[str]) -> list[str]:
    common = [
        CODEX_BIN, "--ask-for-approval", "never", "--sandbox", "danger-full-access",
        "--model", CODEX_MODEL, "-C", str(HOME), "--add-dir", "/opt/projects",
    ]
    if entry == "print":
        return [*common, "exec", "--skip-git-repo-check", "-"]
    return [*common, *extra]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--select", action="store_true", help="print the selected model")
    mode.add_argument("--status", action="store_true", help="print redacted routing status as JSON")
    mode.add_argument("--show-command", action="store_true", help="print the selected argv without executing it")
    parser.add_argument("--entry", choices=("interactive", "print"))
    parser.add_argument("--force-codex", action="store_true")
    args, extra = parser.parse_known_args(argv)
    if extra and extra[0] == "--":
        extra = extra[1:]

    route, usage, error = inspect_live()
    if args.force_codex:
        route = Route("codex", CODEX_MODEL, "explicit_codex_pm_command")
    if args.select:
        print(route.model)
        return 0
    if args.status:
        print(json.dumps({
            **asdict(route),
            "opus_used_percentages": opus_used_percentages(usage or {}),
            "fable_used_percentages": model_used_percentages(usage or {}, "fable"),
            "shared_limits": shared_limits(usage or {}),
            "usage_error": error,
        }, ensure_ascii=False, indent=2))
        return 0

    entry = args.entry or ("print" if "--print" in extra else "interactive")
    if args.entry == "interactive":
        user_request = " ".join(extra).strip()
        extra = [
            STARTUP_PROMPT + (
                f"\n\nПервый запрос пользователя: {user_request}" if user_request else ""
            )
        ]
    command = (
        claude_command(route.model, args.entry, extra)
        if route.engine == "claude"
        else codex_command(entry, extra)
    )
    if args.show_command:
        print(json.dumps(command, ensure_ascii=False))
        return 0
    if error:
        print(f"product-owner: quota check unavailable; keeping Opus ({error})", file=sys.stderr)
    if route.engine == "claude":
        os.execvpe(CLAUDE_BIN, command, {**os.environ, "IS_SANDBOX": "1"})
        return 127

    notice = (
        "Продакт: наблюдаемый лимит не оставил пригодного Claude-маршрута; "
        "продолжаю через резервный Codex GPT-5.6 Sol."
        if not args.force_codex else
        "Продакт запущен явной командой codex-pm через Codex GPT-5.6 Sol."
    )
    if entry == "interactive":
        print(notice, file=sys.stderr)
        os.execvpe(CODEX_BIN, command, os.environ)
        return 127
    completed = subprocess.run(
        command, input=sys.stdin.read(), text=True, capture_output=True,
        cwd=HOME, env=os.environ, check=False,
    )
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")
    print(notice)
    if completed.stdout:
        print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
