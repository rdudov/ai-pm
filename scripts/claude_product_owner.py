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
import shutil
import subprocess
import sys
from typing import Any
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))

import codex_budget  # noqa: E402
import plain_russian  # noqa: E402
import product_memory  # noqa: E402


# Where this product owner is installed, taken from where this file stands: the
# entry point has to work from any checkout, not from one server's layout.
HOME = Path(__file__).resolve().parents[1]
# Found on PATH rather than pinned to one server's layout, and kept as a bare
# name when nothing is installed, so the failure is «claude не установлен» from
# the exec rather than a path that is wrong on every other machine.
CLAUDE_BIN = shutil.which("claude") or "claude"
CODEX_BIN = shutil.which("codex") or "codex"
CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
# Beside this installation's own runtime state, not in a system lock directory:
# the processes this serialises all live in this checkout, and `state/` is the
# one place every installation already has and keeps out of git.
AUTH_REFRESH_LOCK = HOME / "state" / "claude-quota-refresh.lock"
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
    "новую редакцию плана и только потом отвечай «записано». Продуктовый запрос "
    "не превращай сразу в код: перед разработкой последовательно проверь, можно "
    "ли ничего не делать, убрать или отключить лишнее, настроить или переиспользовать "
    "существующее, упростить, и только затем заказывать минимально необходимый код. "
    "Не превращай сводку в технический лог и не проси подтверждения для безопасных read-only проверок "
    "в доступных каталогах. "
    # Пользователь 23 августа 2026 просил исправить язык «отчётов и ответов», а
    # не одних писем. Интерактивная консоль системного приказа не получает —
    # `claude_command` добавляет его только фоновому `--entry print`, — поэтому
    # правила стоят прямо здесь.
    + plain_russian.as_paragraph()
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
    codex_budget: dict[str, Any] | None
    codex_error: dict[str, Any] | None
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


def claude_weekly_remaining(usage: dict[str, Any]) -> float | None:
    """Return only the observed shared seven-day remainder."""
    weekly = next((item for item in shared_limits(usage)
                   if item["kind"] == "seven_day"), None)
    return weekly["remaining_percent"] if weekly else None


def codex_weekly_remaining(codex: dict[str, Any] | None) -> float | None:
    """Return a current remainder only from an explicitly seven-day snapshot."""
    if not isinstance(codex, dict):
        return None
    if codex.get("window_minutes") != codex_budget.WEEKLY_WINDOW_MINUTES:
        return None
    remaining = _number(codex.get("remaining_percent"))
    resets_at = codex.get("resets_at_epoch")
    observed_at = codex.get("observed_at")
    if (remaining is None or isinstance(resets_at, bool)
            or not isinstance(resets_at, (int, float))
            or not isinstance(observed_at, str)):
        return None
    try:
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            return None
    except ValueError:
        return None
    now = datetime.now(timezone.utc)
    age = (now - observed.astimezone(timezone.utc)).total_seconds()
    if (age < -300 or age > codex_budget.WEEKLY_WINDOW_MINUTES * 60
            or float(resets_at) <= now.timestamp()):
        return None
    return remaining


def select_route(
    usage: dict[str, Any], codex: dict[str, Any] | None,
    threshold: float = OPUS_REMAINING_THRESHOLD,
) -> Route:
    """Choose the family with the larger observed weekly remainder."""
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

    claude_model = select_model(usage, threshold)[0]
    claude_remaining = claude_weekly_remaining(usage)
    codex_remaining = codex_weekly_remaining(codex)
    if claude_remaining is None:
        return Route("claude", claude_model, "claude_weekly_remaining_unavailable")
    if codex_remaining is None:
        return Route(
            "claude", claude_model,
            f"codex_weekly_remaining_unavailable:claude_remaining={claude_remaining:g}%",
        )
    comparison = (f"weekly_remaining:claude={claude_remaining:g}%,"
                  f"codex={codex_remaining:g}%")
    if codex_remaining > claude_remaining:
        return Route("codex", CODEX_MODEL, comparison)

    return Route("claude", claude_model, comparison)


def select_model(
    usage: dict[str, Any], threshold: float = OPUS_REMAINING_THRESHOLD
) -> tuple[str, str]:
    """Choose a usable Claude model from observed model-scoped limits."""
    percentages = opus_used_percentages(usage)
    if not percentages:
        return OPUS_MODEL, "no_opus_specific_limit"
    remaining = 100.0 - max(percentages)
    fable = model_used_percentages(usage, "fable")
    fable_exhausted = bool(fable) and max(fable) >= 100.0
    if remaining < threshold and not fable_exhausted:
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
        # Nothing is asked of this child, and the channel it would otherwise
        # inherit is the caller's own: a background wake passes its text to us
        # through stdin, and a `--print` child reads that pipe to the end before
        # answering `/usage`. Leaving it open cost a whole wake-up — the real
        # run then met an empty stdin and died with «Input must be provided».
        stdin=subprocess.DEVNULL,
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
    codex_error = None
    try:
        codex = codex_budget.latest()
    except Exception as exc:
        codex = None
        codex_error = {
            "kind": "codex_observation",
            "exception_type": type(exc).__name__,
            "message": str(exc),
        }
    if codex_error is None and codex_weekly_remaining(codex) is None:
        codex_error = {
            "kind": "unavailable_or_stale",
            "exception_type": None,
            "message": "no current explicitly seven-day Codex observation",
        }
    try:
        try:
            usage = fetch_usage()
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise
            usage, recovery = _usage_after_authorization_recovery()
        return UsageObservation(
            route=select_route(usage, codex),
            usage=usage,
            codex_budget=codex,
            codex_error=codex_error,
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
            codex_budget=codex,
            codex_error=codex_error,
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


def workspace_access() -> list[str]:
    """`--add-dir` for every directory this installation says the owner works in.

    The list is a setting (`product_memory.workspace_dirs`), because which shelf
    of repositories a product owner watches is a fact about one installation. An
    installation that names none gets no flag at all: the owner then works in its
    own checkout, which is exactly right for a fresh clone.
    """
    return [flag for directory in product_memory.workspace_dirs()
            for flag in ("--add-dir", directory)]


def claude_command(model: str, entry: str | None, extra: list[str]) -> list[str]:
    if entry == "interactive":
        return [
            CLAUDE_BIN, "--model", model, "--name", "product-owner",
            *workspace_access(), "--dangerously-skip-permissions",
            "--setting-sources", "project", *extra,
        ]
    if entry == "print":
        return [
            CLAUDE_BIN, "--model", model, "--print", "--name", "product-owner-background",
            # Единственное место, через которое проходит каждый фоновый ход
            # продакта: письмо треда, утренняя оперативка, непрерывная сессия по
            # цели, ответ на входящее письмо и ответ на просьбу из разговора. Два
            # последних промпта собирает почтовая дверь соседнего репозитория, и
            # правил языка в них нет; здесь они есть у всех сразу.
            "--append-system-prompt", plain_russian.as_paragraph(),
            *workspace_access(), "--dangerously-skip-permissions",
            "--setting-sources", "project", *extra,
        ]
    return [CLAUDE_BIN, "--model", model, *extra]


def codex_command(entry: str, extra: list[str]) -> list[str]:
    common = [
        CODEX_BIN, "--ask-for-approval", "never", "--sandbox", "danger-full-access",
        "--model", CODEX_MODEL, "-C", str(HOME), *workspace_access(),
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
    parser.add_argument("--force-claude", action="store_true")
    args, extra = parser.parse_known_args(argv)
    if extra and extra[0] == "--":
        extra = extra[1:]

    observation = inspect_observation()
    route, usage = observation.route, observation.usage
    error = observation.error
    if args.force_codex:
        route = Route("codex", CODEX_MODEL, "explicit_codex_pm_command")
    elif args.force_claude:
        route = Route(
            "claude", select_model(usage or {})[0], "explicit_claude_pm_command"
        )
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
            "codex_budget": observation.codex_budget,
            "codex_quota_observation": {
                "status": "observed" if observation.codex_error is None else "unavailable",
                "source": "codex_session_rate_limits",
                "freshness": "current_window" if observation.codex_error is None else "unavailable",
                "error": observation.codex_error,
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
        print(
            "product-owner: route selected; Claude "
            f"({route.reason})",
            file=sys.stderr,
        )
    if route.engine == "claude":
        os.chdir(HOME)
        os.execvpe(CLAUDE_BIN, command, {**os.environ, "IS_SANDBOX": "1"})
        return 127

    if args.force_codex:
        notice = "Продакт запущен явной командой codex-pm через Codex GPT-5.6 Sol."
    elif route.reason.startswith("weekly_remaining:"):
        notice = ("Продакт: у Codex больше наблюдаемый остаток недельного окна; "
                  "продолжаю через Codex GPT-5.6 Sol.")
    else:
        notice = ("Продакт: наблюдаемый лимит не оставил пригодного Claude-маршрута; "
                  "продолжаю через Codex GPT-5.6 Sol.")
    if entry == "interactive":
        print(notice, file=sys.stderr)
        os.execvpe(CODEX_BIN, command, os.environ)
        return 127
    # Те же правила языка, что Claude получает флагом `--append-system-prompt`.
    # У `codex exec` такого флага нет, а промпт он читает со stdin, поэтому
    # правила встают перед ним. Перед, а не после: контракт письма требует
    # `ПОВОД` первой строкой, и приказ, стоящий последним, ставит перед ним
    # свой абзац.
    completed = subprocess.run(
        command, input=f"{plain_russian.as_paragraph()}\n\n{sys.stdin.read()}",
        text=True, capture_output=True, cwd=HOME, env=os.environ, check=False,
    )
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")
    # Which engine answered is a diagnostic, and on this path stdout is not a
    # console — it is the letter. Until 2026-08-23 this line was printed there,
    # so every Codex-routed letter opened with «Продакт: у Codex больше
    # наблюдаемый остаток недельного окна…», and a wake-up that answered exactly
    # `SILENT` became a two-line letter that was no longer `SILENT`: the user was
    # mailed the word itself at 11:35 UTC that day (Gmail `message-id`).
    # The interactive branch above already treats this line as a diagnostic; here
    # it is given the shape `thread_tick.route_diagnostics` keeps, so the route
    # stays visible in the unit's own journal instead of in the mail.
    print(f"product-owner: route selected; Codex ({route.reason}) — {notice}",
          file=sys.stderr)
    if completed.stdout:
        print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
