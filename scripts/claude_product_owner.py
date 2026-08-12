#!/usr/bin/env python3
"""Route every product-owner entry through one observed Claude/Codex policy."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
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
AUTH_REFRESH_LOCK = Path("/run/lock/product-owner-claude-quota.lock")
OPUS_MODEL = "opus"
FABLE_MODEL = "fable"
CODEX_MODEL = "gpt-5.6-sol"
OPUS_REMAINING_THRESHOLD = 5.0
STARTUP_PROMPT = (
    "Ты работаешь как самостоятельный продакт-владелец. Полностью прочитай AGENTS.md "
    "этого каталога и следуй ему. На старте прочитай текущую редакцию портфельного "
    "плана командой python3 scripts/product_memory.py --plan — она, а не статус "
    "задачи и не старый план в чьём-то тексте, задаёт порядок работ; затем прочитай "
    "снимки продуктов content/products/*/snapshot.md, затем для "
    "каждого ключа из threads.json выполни python3 scripts/thread_state.py <ключ> "
    "--format text и проверь бюджет командой python3 scripts/codex_budget.py. Дай короткую "
    "продуктовую сводку: что изменилось или требует внимания, твой вердикт и что ты "
    "собираешься делать; после этого оставайся в интерактивном диалоге. Порядок "
    "называй по редакции плана и назови её номер. Если решение пользователя меняет "
    "приоритет, паузу или порядок — сначала сохрани его запись в content/, выпусти "
    "новую редакцию плана и только потом отвечай «записано». Не превращай "
    "сводку в технический лог и не проси подтверждения для безопасных read-only проверок "
    "в доступных каталогах."
)


@dataclass(frozen=True)
class Route:
    engine: str
    model: str
    reason: str


@dataclass(frozen=True)
class UsageObservation:
    route: Route
    usage: dict[str, Any] | None
    attempted_at: str
    observed_at: str | None
    authorization_recovery: str
    error: dict[str, Any] | None


class UsageSchemaError(ValueError):
    """The provider answered, but not with a quota shape we recognize."""


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if 0.0 <= number <= 100.0 else None


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


def model_limits(usage: dict[str, Any], model_name: str) -> dict[str, Any]:
    """Expose only limits the provider explicitly scoped to ``model_name``."""
    records: list[dict[str, Any]] = []
    legacy_key = f"seven_day_{model_name.casefold()}"
    legacy = usage.get(legacy_key)
    if isinstance(legacy, dict):
        value = _number(legacy.get("utilization"))
        if value is not None:
            records.append({
                "kind": legacy_key,
                "used_percent": value,
                "remaining_percent": 100.0 - value,
                "resets_at": legacy.get("resets_at"),
                "source_field": legacy_key,
            })

    limits = usage.get("limits")
    if isinstance(limits, list):
        for index, limit in enumerate(limits):
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
            if value is None:
                continue
            records.append({
                "kind": limit.get("kind") if isinstance(limit.get("kind"), str) else "model_scoped",
                "used_percent": value,
                "remaining_percent": 100.0 - value,
                "resets_at": limit.get("resets_at"),
                "source_field": f"limits[{index}]",
            })
    return {"published": bool(records), "limits": records}


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
            "source_field": key,
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
        raise UsageSchemaError("usage response is not an object")
    recognized = {"five_hour", "seven_day", "limits"}.intersection(payload)
    if not recognized:
        raise UsageSchemaError("usage response has no recognized quota fields")
    for key in ("five_hour", "seven_day"):
        if key in payload and payload[key] is not None and not isinstance(payload[key], dict):
            raise UsageSchemaError(f"usage field {key!r} is not an object or null")
        value = payload.get(key)
        if isinstance(value, dict):
            if _number(value.get("utilization")) is None:
                raise UsageSchemaError(f"usage field {key!r} has invalid utilization")
            if value.get("resets_at") is not None and not isinstance(value.get("resets_at"), str):
                raise UsageSchemaError(f"usage field {key!r} has invalid resets_at")
    if "limits" in payload and payload["limits"] is not None and not isinstance(payload["limits"], list):
        raise UsageSchemaError("usage field 'limits' is not a list or null")
    if isinstance(payload.get("limits"), list):
        for index, limit in enumerate(payload["limits"]):
            if not isinstance(limit, dict):
                raise UsageSchemaError(f"usage field limits[{index}] is not an object")
            if _number(limit.get("percent")) is None:
                raise UsageSchemaError(f"usage field limits[{index}] has invalid percent")
            if limit.get("resets_at") is not None and not isinstance(limit.get("resets_at"), str):
                raise UsageSchemaError(f"usage field limits[{index}] has invalid resets_at")
            scope = limit.get("scope")
            if scope is not None and not isinstance(scope, dict):
                raise UsageSchemaError(f"usage field limits[{index}] has invalid scope")
            if isinstance(scope, dict) and scope.get("model") is not None and not isinstance(scope.get("model"), dict):
                raise UsageSchemaError(f"usage field limits[{index}] has invalid model scope")
    return payload


def refresh_authorization_with_claude() -> None:
    """Let Claude Code, the credential owner, refresh auth without a model turn."""
    command = [
        CLAUDE_BIN,
        "--safe-mode",
        "--print",
        "--no-session-persistence",
        "--tools", "",
        "--output-format", "json",
        "/usage",
    ]
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        cwd=HOME,
        env=os.environ,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Claude authorization refresh exited {completed.returncode}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Claude authorization refresh returned non-JSON output") from exc
    usage = result.get("usage") if isinstance(result, dict) else None
    token_total = sum(
        value for key, value in (usage or {}).items()
        if key.endswith("_tokens") and isinstance(value, (int, float))
    )
    if (
        not isinstance(result, dict)
        or result.get("is_error") is not False
        or result.get("num_turns") != 0
        or result.get("total_cost_usd") not in (0, 0.0)
        or token_total != 0
    ):
        raise RuntimeError("Claude authorization refresh was not a zero-turn /usage command")


def _usage_after_authorization_recovery() -> tuple[dict[str, Any], str]:
    AUTH_REFRESH_LOCK.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(AUTH_REFRESH_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(lock_fd, "r+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            # A peer may have refreshed the shared credential while we waited.
            try:
                return fetch_usage(), "refreshed_by_peer"
            except urllib.error.HTTPError as exc:
                if exc.code != 401:
                    raise
            refresh_authorization_with_claude()
            return fetch_usage(), "claude_cli_zero_turn_usage"
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _error_record(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, urllib.error.HTTPError):
        kind = "authorization" if exc.code in (401, 403) else "http"
        return {
            "kind": kind,
            "exception_type": type(exc).__name__,
            "http_status": exc.code,
            "message": str(exc),
        }
    if isinstance(exc, urllib.error.URLError):
        return {"kind": "network", "exception_type": type(exc).__name__, "message": str(exc)}
    if isinstance(exc, (UsageSchemaError, ValueError, json.JSONDecodeError)):
        return {"kind": "schema", "exception_type": type(exc).__name__, "message": str(exc)}
    if isinstance(exc, KeyError):
        return {
            "kind": "authorization_configuration",
            "exception_type": type(exc).__name__,
            "message": f"missing credential field {exc}",
        }
    if isinstance(exc, OSError):
        return {"kind": "network", "exception_type": type(exc).__name__, "message": str(exc)}
    if isinstance(exc, (subprocess.SubprocessError, RuntimeError)):
        return {
            "kind": "authorization_or_runtime",
            "exception_type": type(exc).__name__,
            "message": str(exc),
        }
    return {
        "kind": "unknown",
        "exception_type": type(exc).__name__,
        "message": str(exc),
    }


def inspect_observation() -> UsageObservation:
    attempted_at = datetime.now(timezone.utc).isoformat()
    recovery = "not_needed"
    try:
        try:
            usage = fetch_usage()
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise
            usage, recovery = _usage_after_authorization_recovery()
        return UsageObservation(
            route=select_route(usage),
            usage=usage,
            attempted_at=attempted_at,
            observed_at=datetime.now(timezone.utc).isoformat(),
            authorization_recovery=recovery,
            error=None,
        )
    except (
        OSError,
        KeyError,
        ValueError,
        json.JSONDecodeError,
        urllib.error.URLError,
        subprocess.SubprocessError,
        RuntimeError,
    ) as exc:
        return UsageObservation(
            route=Route("claude", OPUS_MODEL, "usage_unavailable"),
            usage=None,
            attempted_at=attempted_at,
            observed_at=None,
            authorization_recovery=recovery,
            error=_error_record(exc),
        )


def inspect_live() -> tuple[Route, dict[str, Any] | None, str | None]:
    """Backward-compatible tuple view for existing callers."""
    observation = inspect_observation()
    error = observation.error
    rendered_error = None if error is None else f"{error['exception_type']}: {error['message']}"
    return observation.route, observation.usage, rendered_error


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

    observation = inspect_observation()
    route, usage = observation.route, observation.usage
    error = observation.error
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
            "model_limits": {
                "opus": model_limits(usage or {}, "opus"),
                "fable": model_limits(usage or {}, "fable"),
            },
            "quota_observation": {
                "status": "observed" if usage is not None else "unavailable",
                "source": "anthropic_oauth_usage_endpoint",
                "freshness": "live" if usage is not None else "unavailable",
                "attempted_at": observation.attempted_at,
                "observed_at": observation.observed_at,
                "authorization_recovery": observation.authorization_recovery,
                "error": error,
            },
            "usage_error": None if error is None else f"{error['kind']}: {error['message']}",
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
        print(
            "product-owner: quota check unavailable; keeping Opus "
            f"({error['kind']}: {error['message']})",
            file=sys.stderr,
        )
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
