#!/usr/bin/env python3
"""Одна продолжающаяся фоновая продуктовая сессия под усиленной целью.

Пока цель под усиленным контролем не разрешена, направление ведёт не новый
двадцатиминутный сеанс, а один продолжающийся разговор.

Почему это не то же самое, что тик. Тик — процесс на двадцать минут: он собирает
портфельный план, снимки продуктов, состояние направления и блок целей заново,
отдаёт всё это модели, получает ответ и умирает. Для обычной работы это
правильно и дёшево. Для проблемной работы это дважды плохо. Во-первых, причинный
контекст — что уже пробовали, почему это не сработало, чего ждём именно сейчас —
каждый раз собирается заново из артефактов, и часть его не восстанавливается
вообще. Во-вторых, реакция на исход технического прогона отложена на случайную
часть двадцати минут: прогон кончается в 00:41, продакт узнаёт об этом в 01:00.
Пользователь назвал это так: «фоновый продакт перестанет работать по тикам, а
будет сохранять сессию, пока проблема не разрулится… каждая новая сессия — это
набор контекста, мимо кеша».

Что здесь продолжается, а что нет — и это стоит сказать прямо, потому что
свидетельство должно совпадать с механизмом. Продолжается *разговор*: у сессии
один идентификатор, и каждый следующий ход входит в неё через `--resume`, так
что модель видит всю предысторию и читает её из кеша, а не собирает заново.
Процесс модели при этом поднимается на ход и завершается — так ход переживает
собственный сбой, а не уносит с собой всю сессию. Непрерывен здесь наблюдающий
цикл: он живёт всё время, пока цель не разрешена, держит наблюдение между
ходами и тратит модель только на значимое изменение.

Отсюда три свойства, каждое из которых проверяется машинно, а не обещанием:

* **идентичность** — `session_id` один и тот же на всех ходах, кроме явно
  записанной ротации;
* **отсутствие пересборки** — тяжёлый стартовый контекст (план, снимки, полное
  состояние направления) уходит только на открывающем ходе; в записи хода это
  поле `context_rebuilt`, и на всех остальных ходах оно `false`, а
  `usage.cache_read_input_tokens` показывает, что предыстория пришла из кеша;
* **скорость реакции** — между наблюдением значимого изменения и началом хода
  проходит `reaction_seconds`, и это секунды-минуты, а не остаток двадцати минут.

Двадцатиминутный тик при этом остаётся, но меняет роль: он watchdog. Пока сессия
жива, он не поднимает второго продакта по этому направлению; когда сессия
умерла, потеряла авторизацию, упёрлась в окно или была вынуждена ротироваться —
он поднимает её заново из долговечной цели и контрольного снимка, без участия
пользователя.

Уступает он при этом только наблюдаемо живой сессии. Принятый запрос на запуск
ею не является: единица может подняться и тут же выйти — например, когда
маршрутизатор увёл продакта к Codex, которым продолжение разговора не
выражается. Поэтому маршрут спрашивается до подавления владельца тика, а запуск
подтверждается перечитанным контрольным снимком; во всех остальных случаях
направление ведёт обычный фоновый продакт тика, и он же проходит пост-контроль
стоячей цели. Цель без продакта — это то, чего в этом режиме быть не должно.

Тут же живёт машинный пост-контроль стоячей цели. Пробуждение по цели, которая
стоит, обязано кончиться одним из двух: живым прогоном по её задаче или
названным блокером. `SILENT` третьим исходом не является — и это проверяется
после хода наблюдением, а не дисциплиной промпта.

Usage:
    goal_session.py run <тред> [--once]
    goal_session.py status <тред> [--json]
    goal_session.py stop <тред>
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
# Запуск скриптом даёт этому модулю имя `__main__`, а `thread_tick` импортирует
# его по имени. Без этой строки в процессе жили бы две копии одного модуля.
sys.modules.setdefault("goal_session", sys.modules[__name__])

import claude_product_owner  # noqa: E402
import outbound  # noqa: E402
import product_goal  # noqa: E402
import thread_tick  # noqa: E402  (обращения только в рантайме: импорт взаимный)
from process_map_state import PROC, tunable  # noqa: E402
from process_map_state import HOME  # noqa: E402
from thread_state import build  # noqa: E402


SESSIONS = HOME / "state" / "goal-sessions"
SELF = Path(__file__).resolve()

# Как часто цикл смотрит на состояние направления. Это наблюдение, а не ход
# модели: цена одного взгляда — обход артефактов, тот же, что делает тик.
POLL_SECONDS = tunable("PRODUCT_OWNER_GOAL_SESSION_POLL_SECONDS", 30)
# Минимальный промежуток между ходами модели. Значимые изменения приходят
# пачками (прогон кончился, статус задачи сменился, коммит появился), и три хода
# подряд про одно и то же — это тот же холостой перезапуск, только чаще.
MIN_TURN_GAP_SECONDS = tunable("PRODUCT_OWNER_GOAL_SESSION_MIN_TURN_GAP_SECONDS", 60)
# Сколько ждать ход модели.
TURN_TIMEOUT = tunable("PRODUCT_OWNER_GOAL_SESSION_TURN_TIMEOUT_SECONDS", 1800)
# После скольких ходов разговор ротируется принудительно. Это страховочный
# потолок, а наблюдаемый рост контекста ниже имеет собственный прямой порог.
MAX_TURNS = tunable("PRODUCT_OWNER_GOAL_SESSION_MAX_TURNS", 60)
# Наблюдаемый дорогой ход прочитал из кеша 4 044 193 токена. После такого хода
# разговор ротируется до следующего обращения модели; watchdog поднимает новую
# сессию из той же долговечной цели и заново собирает стартовый контекст.
MAX_CACHE_READ_INPUT_TOKENS = tunable(
    "PRODUCT_OWNER_GOAL_SESSION_MAX_CACHE_READ_INPUT_TOKENS", 4_044_193)
# Предельная жизнь одной сессии. Тот же смысл: ротация, а не остановка работы.
MAX_LIFETIME_SECONDS = tunable("PRODUCT_OWNER_GOAL_SESSION_MAX_LIFETIME_SECONDS", 21600)
# Сколько последних ходов остаётся в контрольном снимке.
KEEP_TURNS = tunable("PRODUCT_OWNER_GOAL_SESSION_KEEP_TURNS", 40)
# Насколько давно должен быть удар сердца, чтобы сессия считалась подвисшей даже
# при живом процессе. Больше таймаута хода: ход имеет право быть долгим.
HEARTBEAT_STALE_SECONDS = tunable(
    "PRODUCT_OWNER_GOAL_SESSION_HEARTBEAT_STALE_SECONDS", TURN_TIMEOUT + 600)
# Сколько watchdog ждёт от поднятой сессии подтверждения, что она действительно
# ведёт направление. Дороже этого ожидания только его отсутствие: тик, уступивший
# принятому запросу на запуск, оставляет цель без продакта до следующего тика.
LAUNCH_HANDSHAKE_SECONDS = tunable(
    "PRODUCT_OWNER_GOAL_SESSION_HANDSHAKE_SECONDS", 90)
HANDSHAKE_POLL_SECONDS = tunable(
    "PRODUCT_OWNER_GOAL_SESSION_HANDSHAKE_POLL_SECONDS", 1)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _seconds_since(stamp: str | None) -> float | None:
    try:
        return (datetime.now(timezone.utc)
                - datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Контрольный снимок сессии
# ---------------------------------------------------------------------------


def record_path(thread: str) -> Path:
    return SESSIONS / f"{thread}.json"


def read(thread: str) -> dict:
    """Контрольный снимок сессии. Нечитаемый снимок — не «сессии нет».

    Пустой словарь здесь означал бы «живой сессии не наблюдается», и watchdog
    поднял бы вторую поверх работающей первой. Поэтому нечитаемый файл говорит
    о себе вслух и остаётся видимым в снимке направления.
    """
    path = record_path(thread)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        return {"thread": thread, "unreadable": str(error)}
    return payload if isinstance(payload, dict) else {"thread": thread,
                                                      "unreadable": "не объект"}


def write(thread: str, payload: dict) -> dict:
    SESSIONS.mkdir(parents=True, exist_ok=True)
    path = record_path(thread)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)
    return payload


def start_tick(pid: int) -> int | None:
    """Стартовый тик процесса из ядра: pid без него переиспользуется."""
    try:
        return int((PROC / str(pid) / "stat").read_text().rsplit(") ", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def liveness(record: dict) -> dict:
    """Жива ли сессия — по ядру, а не по тому, что она о себе записала.

    Тождество процесса — это пара «pid и стартовый тик»: один pid без второй
    половины через сутки указывает на чужой процесс. Удар сердца проверяется
    отдельно: процесс может быть жив и при этом не двигаться.
    """
    if record.get("unreadable"):
        return {"live": False, "reason": f"снимок сессии не читается: {record['unreadable']}",
                "src": f"{record_path(record.get('thread') or '?')}"}
    if record.get("stopped"):
        return {"live": False,
                "reason": f"сессия остановлена: {record['stopped'].get('reason')}",
                "src": "поле stopped контрольного снимка сессии"}
    pid = record.get("pid")
    if not pid:
        return {"live": False, "reason": "сессии не было", "src": "снимок сессии отсутствует"}
    seen = start_tick(int(pid))
    if seen is None:
        return {"live": False, "reason": f"процесс {pid} не наблюдается в /proc",
                "src": "/proc"}
    if record.get("since") is not None and seen != record.get("since"):
        return {"live": False,
                "reason": f"pid {pid} переиспользован: стартовый тик {seen} вместо "
                          f"{record.get('since')}",
                "src": "поле 22 /proc/<pid>/stat"}
    if record.get("in_turn"):
        # Ход идёт: молчание удара сердца здесь — это работа, а не остановка.
        # Его собственный таймаут ограничивает эту тишину сверху.
        started = _seconds_since(record["in_turn"].get("since"))
        if started is not None and started <= TURN_TIMEOUT + 120:
            return {"live": True, "pid": int(pid),
                    "reason": f"идёт ход {record['in_turn'].get('n')} сессии, "
                              f"{int(started)} с",
                    "src": "pid и стартовый тик /proc, сверенные с отметкой идущего хода"}
    age = _seconds_since(record.get("heartbeat"))
    if age is not None and age > HEARTBEAT_STALE_SECONDS:
        return {"live": False,
                "reason": f"процесс {pid} жив, но не подавал признаков "
                          f"{int(age)} с — это больше порога {HEARTBEAT_STALE_SECONDS} с",
                "src": "поле heartbeat контрольного снимка против часов"}
    return {"live": True, "pid": int(pid), "reason": "процесс сессии наблюдается живым",
            "src": "pid и стартовый тик /proc, сверенные с ударом сердца снимка"}


# ---------------------------------------------------------------------------
# Что делает режим включённым, и что делает изменение значимым
# ---------------------------------------------------------------------------


def reinforced(thread: str) -> list[dict]:
    """Активные цели направления под усиленным контролем.

    Обычная работа сюда не попадает по построению: без наблюдённого признака
    отклонения контроль остаётся `normal`, и непрерывная сессия не поднимается.
    Нечитаемая цель считается усиленной — молчать о ней дороже.
    """
    try:
        goals = product_goal.active(thread)
    except (product_goal.GoalError, OSError, ValueError):
        return []
    return [goal for goal in goals
            if goal.get("control") == product_goal.REINFORCED or goal.get("unreadable")]


def session_route() -> dict:
    """Выражается ли продолжающийся разговор тем движком, который выбрал маршрут.

    Непрерывность здесь — штатное средство Claude CLI: разговор лежит у него на
    диске и продолжается по идентификатору. Когда маршрутизатор уводит продакта
    к Codex, продолжать нечем — ни этого идентификатора, ни этой предыстории у
    того движка нет, и сессия обязана уступить обычному фоновому продакту.

    Ответ нужен в двух местах, и потому решение ровно одно и живёт здесь: сессии,
    которая иначе открыла бы разговор, который не сможет вести, и watchdog'у,
    который иначе подавил бы владельца тика ради сессии, немедленно выходящей.
    Ревью (F-003) назвало вторую половину: тик уступал успешному запросу
    `systemd-run`, дочерняя единица видела Codex и выходила, и при активной цели
    направление оставалось без продакта вообще.
    """
    route, _usage, error = claude_product_owner.inspect_live()
    return {"ok": route.engine == "claude", "engine": route.engine,
            "model": route.model, "reason": route.reason, "error": error,
            "src": "маршрут продакта, спрошенный claude_product_owner.inspect_live()"}


def stand_down(thread: str, route: dict) -> dict:
    """Записать, что непрерывной сессии не будет, и почему её ведёт тик.

    Это не тихий обход: запись остаётся в контрольном снимке, поднимается на
    панель через `stopped` и объясняет наблюдателю, почему направление снова
    ведёт двадцатиминутный продакт.
    """
    return write(thread, {
        **read(thread), "schema_version": 1, "thread": thread,
        "stopped": {"at": now(),
                    "reason": f"маршрут увёл к {route['engine']}: "
                              "продолжение сессии этим движком не выражается",
                    "route": route.get("reason"),
                    "handover": "направление ведёт обычный фоновый продакт тика"},
        "pid": None, "since": None, "heartbeat": now()})


def observation(thread: str) -> dict:
    """Один взгляд на направление: то же наблюдение, что собирает тик."""
    report = build(thread)
    panel = product_goal.panel(thread)
    return {
        "at": now(),
        "report": report,
        "goals": panel,
        "live": sorted(item["id"] for item in report["live_runs"]),
        "blocked": sorted(item["id"] for item in report["needs_attention"]
                          if item["status"] == "blocked"),
        "completed_ready": sorted(item["id"] for item in report["ready_to_start"]),
        "pickup": sorted(item["id"] for item in report["can_pick_up"]),
        "heads": {repo["repo"]: repo.get("head", "")
                  for repo in report["repos"] if repo["present"]},
        "goal_state": {str(goal["id"]): {
            "state": goal.get("state"), "control": goal.get("control"),
            "gap": goal.get("gap"), "waiting_on": goal.get("waiting_on", []),
            "correctives": {str(item["task"]): (item["settled"] or {}).get("kind", "")
                            for item in goal.get("correctives", [])},
        } for goal in panel},
    }


def _settled_kind(value) -> str:
    """Чем закрыт ремонт в снимке — с поправкой на снимок, снятый до обновления.

    Тогда здесь лежало булево «принята», и сравнение его с видом закрытия
    объявило бы приёмку заново на первом же наблюдении после обновления: ход
    модели, потраченный на новость, которой не было.
    """
    if value is True:
        return "accepted"
    return value or ""


def changes(previous: dict | None, current: dict) -> list[str]:
    """Значимые изменения между двумя взглядами — то, ради чего стоит ход.

    Сознательно уже, чем события тика: сессия уже знает предысторию, и ход
    нужен на том, что меняет решение. Изменение цели, конец или появление
    прогона, новый blocked, новый коммит в дереве направления — меняет; уточнение
    прогресса ребёнка внутри прогона — нет.
    """
    if previous is None:
        return ["первый взгляд сессии"]
    said: list[str] = []
    for task_id in sorted(set(previous["live"]) - set(current["live"])):
        said.append(f"прогон задачи {task_id} завершился")
    for task_id in sorted(set(current["live"]) - set(previous["live"])):
        said.append(f"по задаче {task_id} пошёл живой прогон")
    for task_id in sorted(set(current["blocked"]) - set(previous["blocked"])):
        said.append(f"задача {task_id} перешла в blocked")
    for task_id in sorted(set(current["pickup"]) - set(previous["pickup"])):
        said.append(f"задачу {task_id} стало можно подхватить")
    for repo, head in current["heads"].items():
        if previous["heads"].get(repo, head) != head:
            said.append(f"{Path(repo).name}: новый коммит {head}")
    before, after = previous["goal_state"], current["goal_state"]
    for goal_id, state in after.items():
        was = before.get(goal_id)
        if was is None:
            said.append(f"появилась цель {goal_id}")
            continue
        if was.get("state") != state.get("state"):
            said.append(f"цель {goal_id}: {was.get('state')} → {state.get('state')}")
        if was.get("control") != state.get("control"):
            said.append(f"цель {goal_id}: контроль {state.get('control')}")
        if was.get("gap") != state.get("gap"):
            said.append(f"цель {goal_id}: ближайший разрыв изменился")
        for task, settled in state.get("correctives", {}).items():
            settled = _settled_kind(settled)
            if _settled_kind(was.get("correctives", {}).get(task)) != settled and settled:
                # Снятие меняет решение ровно так же, как приёмка: ремонт больше
                # не держит основную работу, и её можно возвращать.
                said.append(f"цель {goal_id}: корректирующая задача {task} "
                            + ("принята" if settled == "accepted" else "снята"))
    for goal_id in sorted(set(before) - set(after)):
        said.append(f"цель {goal_id} закрыта или ушла из направления")
    return said


def standing(current: dict) -> list[dict]:
    """Цели, по которым сейчас ничего живого не идёт."""
    return [goal for goal in current["goals"]
            if goal.get("waiting_on")
            and not set(goal["waiting_on"]) & set(current["live"])]


# ---------------------------------------------------------------------------
# Машинный пост-контроль стоячей цели
# ---------------------------------------------------------------------------


def post_check(thread: str, standing_goals: list[dict], after_live: list[int],
               reply: str, moment: datetime) -> dict | None:
    """Исход пробуждения по стоячей цели, проверенный наблюдением.

    Ревью 1127 (F-002) назвало ровно эту дыру: цель стоит, продакт разбужен,
    рядом идёт посторонний живой прогон — и ответ `SILENT` заканчивал проверку
    успехом. Обязательный исход из приёмки оставался на дисциплине промпта,
    ровно там, где сам режим объясняет, почему нужен машинный отказ.

    Два допустимых исхода, и оба наблюдаемы после хода: по задаче цели пошёл
    живой прогон — или в ответе назван конкретный блокер обычными словами.
    Молчание третьим исходом не является: оно записывается как отказ в снимке
    направления и делает ход неуспешным. Внутренние номера целей в Telegram из
    этого пути не проецируются.
    """
    if not standing_goals:
        return None
    named = bool(reply.strip()) and reply.strip() != "SILENT"
    unresolved = [goal for goal in standing_goals
                  if not set(goal.get("waiting_on") or []) & set(after_live)]
    if not unresolved or named:
        return {
            "at": moment.isoformat(),
            "resolved": True,
            "how": "по задаче цели пошёл живой прогон" if not unresolved
                   else "в ответе назван блокер",
            "goals": [goal["id"] for goal in standing_goals],
            "src": "живые прогоны направления, наблюдённые после хода, и текст ответа",
        }
    told = (f"[{thread}] цель "
            + ", ".join(str(goal["id"]) for goal in unresolved)
            + " стоит, продакт разбужен и не сделал ни того, ни другого: "
              "ни живого прогона по её задаче, ни названного блокера.\n\n"
            + "\n".join(
                f"- цель {goal['id']}: {goal.get('outcome', '')[:160]}; ждёт задачи "
                + ", ".join(str(number) for number in goal.get("waiting_on") or [])
                + f"; ближайший разрыв: {goal.get('gap') or 'не назван'}"
                for goal in unresolved)
            + "\n\nЭто отказ механизма, а не решение продакта: обязательный исход "
              "пробуждения по стоячей цели не наблюдается.")
    # stderr is already collected by both installed systemd units. Keep the
    # failure observable there without turning it back into a user delivery.
    print(told, file=sys.stderr)
    return {
        "at": moment.isoformat(),
        "resolved": False,
        "how": "ни живого прогона по задаче цели, ни названного блокера",
        "goals": [goal["id"] for goal in unresolved],
        "told": told,
        "src": "живые прогоны направления, наблюдённые после хода, и текст ответа",
    }


# ---------------------------------------------------------------------------
# Ход модели
# ---------------------------------------------------------------------------


def opening_prompt(thread: str, current: dict, said: list[dict], chat: dict) -> str:
    """Тяжёлый стартовый контекст. Собирается один раз за сессию, и только тут."""
    report = current["report"]
    return f"""Ты продакт-владелец направления «{report['title']}» и ведёшь одну
продолжающуюся фоновую сессию: она не кончается вместе с этим ходом. Пока цель
под усиленным контролем не разрешена, ты остаёшься тем же разговором, и я буду
присылать тебе только то, что изменилось. Поэтому держи в голове, что уже
пробовал и почему это не сработало: пересказывать тебе стартовый контекст я
больше не буду.
{thread_tick.plan_block()}
{product_goal.block(thread)}
{thread_tick.heard_block(said, chat)}

Наблюдаемое состояние направления (собрано механически, не со слов исполнителя):
{json.dumps(report, ensure_ascii=False, indent=2)}

{_rules_block()}

Сейчас, на открывающем ходе: прочитай артефакты задач цели, прими решение и
сделай следующий безопасный шаг сам. Ответ сформируй по правилам выше.
"""


def delta_prompt(thread: str, current: dict, reasons: list[str]) -> str:
    """Ход по изменению. Здесь нет ни плана, ни снимков, ни полного состояния."""
    report = current["report"]
    compact = {
        "живые прогоны": [{"задача": item["id"], "прогресс": (item.get("run") or {}).get("activity")}
                          for item in report["live_runs"]],
        "blocked": current["blocked"],
        "можно подхватить": current["pickup"],
        "готово к запуску": current["completed_ready"],
        "цели": current["goals"],
    }
    return f"""Изменилось с прошлого твоего хода:
{chr(10).join('- ' + line for line in reasons)}

Короткое состояние (полное состояние и план у тебя уже есть выше по разговору,
я их не повторяю):
{json.dumps(compact, ensure_ascii=False, indent=2)}

{_rules_block()}

Действуй сам. Ответ сформируй по правилам выше.
"""


def recovery_prompt(thread: str, current: dict, recovered: dict) -> str:
    """Первый ход после восстановления: тот же разговор, объяснённый разрыв.

    Стартовый контекст здесь тоже не пересобирается — он в этом разговоре уже
    есть. Пересказывается только то, чего продакт не мог видеть: что его подняли
    заново, почему предыдущий процесс перестал жить и что за это время
    изменилось.
    """
    return f"""Тебя подняли заново: процесс предыдущей сессии перестал жить
({recovered.get('reason')}), а разговор продолжен тот же — всё, что было выше,
твоё. Ходов до перерыва: {recovered.get('turns_before')}.

Долговечная цель этого направления не зависела от того процесса и цела:
{product_goal.block(thread)}

Состояние направления сейчас:
{json.dumps({"живые прогоны": current["live"], "blocked": current["blocked"],
             "можно подхватить": current["pickup"], "цели": current["goals"]},
            ensure_ascii=False, indent=2)}

{_rules_block()}

Продолжай с того места, где остановился. Ответ сформируй по правилам выше.
"""


def _rules_block() -> str:
    """Правила, одинаковые на всех ходах: одно место, чтобы они не разъехались."""
    return f"""Правила этого режима:
- по каждой стоячей цели обязателен один из двух исходов: запущен следующий
  безопасный шаг по её задаче — или в ответе назван конкретный внешний блокер
  обычными словами. Молчание третьим исходом не является и записывается как
  отказ механизма;
- кросс-ревью: работу Codex ревьюит Claude и наоборот; на замечания сначала
  анализ и план, потом правки, потом повторное ревью;
- код продуктов ты руками не правишь: работа уходит обычным путём задач;
- приёмка корректирующей задачи цель не закрывает — после неё основная задача
  возвращается в работу (`product_goal.py accept`, затем `resume`), а цель
  закрывается только живой проверкой исходного сценария через фактически
  установленный продукт;
- ремонт, который сам себя не доставил — эффект пришёл с другой принятой задачей
  или он отменён в системе задач, — снимается `product_goal.py retire --task N
  --reason ... --src ...`, а не приёмкой: приёмка утверждает доставку. Снятый
  ремонт основную работу не держит;
- запуск ребёнка в режиме записи по нашему коду в нашем репозитории разрешения
  не требует; единственный физический ограничитель — занятое рабочее дерево;
- не читай транскрипты детей: только артефакты задач и наблюдаемое состояние;
- значимое изменение снимка продукта пиши через
  `python3 {claude_product_owner.HOME}/scripts/product_memory.py`, а не правкой
  файла руками.

{thread_tick.verdict_block()}"""


def run_turn(model: str, session_id: str, prompt: str, opening: bool) -> dict:
    """Один ход в *том же* разговоре. `--resume` — это и есть непрерывность.

    Открывающий ход задаёт идентификатор сам (`--session-id`), чтобы он был
    известен до того, как модель ответит: сессия, чей идентификатор виден только
    из её вывода, не переживает падение этого вывода.
    """
    entry = ["--session-id", session_id] if opening else ["--resume", session_id]
    command = claude_product_owner.claude_command(
        model, "print", [*entry, "--output-format", "json"])
    started = time.time()
    try:
        completed = subprocess.run(
            command, input=prompt, text=True, capture_output=True, cwd=str(HOME),
            env={**os.environ, "IS_SANDBOX": "1"}, timeout=TURN_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reply": "", "usage": None,
                "duration_seconds": round(time.time() - started, 1),
                "error": f"ход не уложился в {TURN_TIMEOUT} с"}
    duration = round(time.time() - started, 1)
    if completed.returncode != 0:
        return {"ok": False, "reply": "", "usage": None, "duration_seconds": duration,
                "error": (completed.stderr or "").strip()[:400] or
                         f"код возврата {completed.returncode}"}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        # Текст вместо JSON — ход состоялся, но его учёт непроверяем; это
        # говорится вслух, а не выдаётся за наблюдённый кеш.
        return {"ok": True, "reply": (completed.stdout or "").strip(), "usage": None,
                "duration_seconds": duration,
                "error": "вывод хода не разбирается как JSON: учёт кеша не наблюдён"}
    usage = payload.get("usage") or {}
    return {
        "ok": not payload.get("is_error"),
        "reply": (payload.get("result") or "").strip(),
        "session_id": payload.get("session_id"),
        "duration_seconds": duration,
        "usage": {key: usage.get(key) for key in
                  ("input_tokens", "cache_creation_input_tokens",
                   "cache_read_input_tokens", "output_tokens")},
        "cost_usd": payload.get("total_cost_usd"),
        "error": payload.get("result") if payload.get("is_error") else None,
    }


# ---------------------------------------------------------------------------
# Цикл
# ---------------------------------------------------------------------------


def loop(thread: str, once: bool = False) -> int:
    """Наблюдай, тратя модель только на значимое, пока цель не разрешена."""
    goals = reinforced(thread)
    if not goals:
        print(f"по направлению {thread} нет целей под усиленным контролем: "
              "непрерывная сессия не нужна", file=sys.stderr)
        return 0

    route = session_route()
    if not route["ok"]:
        # Маршрут увёл к Codex: у того разговора нет продолжения по этому
        # идентификатору. Это записанная вынужденная ротация, а не тихий обход.
        # Уступать тут есть кому: watchdog спрашивает тот же маршрут до того, как
        # подавит владельца тика, так что после этого выхода направление ведёт
        # обычный фоновый продакт, а не никто.
        stand_down(thread, route)
        print(f"маршрут выбрал {route['engine']}: непрерывная сессия не поднимается, "
              "направление ведёт двадцатиминутный тик", file=sys.stderr)
        return 0
    route_error = route.get("error")

    previous_record = read(thread)
    # Восстановление после смерти — тоже продолжение, если продолжать есть что.
    # Разговор лежит на диске у CLI и переживает процесс, который его вёл, так
    # что watchdog возвращает продакта в тот же разговор, а не в новый: цена
    # нового — ровно та пересборка контекста, ради отказа от которой режим и
    # заведён. Исключение — остановка, помеченная ротацией: там продолжать
    # нечего или нельзя, и это записано явно.
    carried = (previous_record.get("session") or {}).get("id")
    rotated = bool((previous_record.get("stopped") or {}).get("rotation"))
    resumed = bool(carried) and not rotated
    session_id = carried if resumed else str(uuid.uuid4())
    started_at = now()
    record = {
        "schema_version": 1,
        "thread": thread,
        "pid": os.getpid(),
        "since": start_tick(os.getpid()),
        "started_at": started_at,
        "heartbeat": started_at,
        "poll_seconds": POLL_SECONDS,
        # Ходы и момент открытия принадлежат разговору, а не процессу: после
        # восстановления счётчик, начатый заново, сказал бы «ходов 0» о сессии,
        # которая их уже сделала.
        "session": {"id": session_id, "engine": route["engine"], "model": route["model"],
                    "route": route["reason"],
                    "opened_at": ((previous_record.get("session") or {}).get("opened_at")
                                  if resumed else None),
                    "turns": ((previous_record.get("session") or {}).get("turns") or 0)
                             if resumed else 0},
        "goals": [goal["id"] for goal in goals],
        "turns": [],
        "stopped": None,
        "src": "запись непрерывной продуктовой сессии этого направления; "
               "живость — pid и стартовый тик в /proc, ходы — вывод claude --print",
    }
    if route_error:
        record["route_note"] = route_error
    if carried:
        record["recovered"] = {
            "at": started_at,
            "previous_session": carried,
            "previous_pid": previous_record.get("pid"),
            "reason": (f"предыдущая сессия остановлена: "
                       f"{previous_record['stopped'].get('reason')}"
                       if previous_record.get("stopped")
                       else liveness(previous_record)["reason"]),
            "turns_before": (previous_record.get("session") or {}).get("turns"),
            # Продолжен ли тот же разговор или начат новый — это ровно разница
            # между «контекст сохранён» и «контекст собран заново», и она не
            # выводится из идентификаторов задним числом.
            "resumed_conversation": resumed,
            "src": "предыдущий контрольный снимок сессии этого направления",
        }
    write(thread, record)

    stop = {"asked": False}

    def _stop(_signum, _frame):
        stop["asked"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    seen: dict | None = None
    last_turn_at = 0.0
    # Восстановленный разговор открывать нечем: он уже открыт, и первый ход в
    # нём — не сборка контекста, а объяснение разрыва.
    opening_done = resumed
    after_recovery = resumed
    while True:
        current = observation(thread)
        record["heartbeat"] = now()
        record["last_observation_at"] = current["at"]
        record["goals"] = [goal["id"] for goal in current["goals"]]
        write(thread, record)

        if not reinforced(thread):
            record["stopped"] = {"at": now(), "reason": "целей под усиленным контролем "
                                                        "в направлении больше нет"}
            write(thread, record)
            return 0

        reasons = changes(seen, current)
        due = reasons and (time.time() - last_turn_at) >= MIN_TURN_GAP_SECONDS
        if due:
            observed_at = current["at"]
            moment = datetime.now(timezone.utc)
            before_live = list(current["live"])
            standing_goals = standing(current)
            if not opening_done:
                with outbound.Ledger() as ledger:
                    said = outbound.already_said(ledger.thread(thread), moment)
                chat = outbound.heard_in_chat(moment)
                prompt, kind = opening_prompt(thread, current, said, chat), "open"
            elif after_recovery:
                prompt = recovery_prompt(thread, current, record.get("recovered") or {})
                kind, after_recovery = "recovery", False
            else:
                prompt, kind = delta_prompt(thread, current, reasons), "delta"
            # Ход может идти долго — он думает и запускает детей, — а удар сердца
            # пишется между проходами. Без этой отметки живая сессия посреди
            # длинного хода выглядела бы замершей и на панели, и в статусе.
            record["in_turn"] = {"since": moment.isoformat(), "reason": reasons,
                                 "n": record["session"]["turns"] + 1}
            record["heartbeat"] = now()
            write(thread, record)
            turn = run_turn(record["session"]["model"], session_id, prompt,
                            opening=not opening_done)
            record["in_turn"] = None
            last_turn_at = time.time()
            after = observation(thread)
            started_runs = sorted(set(after["live"]) - set(before_live))
            checked = post_check(thread, standing_goals, after["live"],
                                 turn.get("reply", ""), datetime.now(timezone.utc))
            entry = {
                "n": record["session"]["turns"] + 1,
                "at": moment.isoformat(),
                "kind": kind,
                # Единственное поле, по которому видно пересборку стартового
                # контекста. `true` бывает ровно один раз за сессию.
                "context_rebuilt": not opening_done,
                "reason": reasons,
                "change_observed_at": observed_at,
                "reaction_seconds": round(
                    (moment - datetime.fromisoformat(observed_at)).total_seconds(), 1),
                "session_id": turn.get("session_id") or session_id,
                "duration_seconds": turn.get("duration_seconds"),
                "usage": turn.get("usage"),
                "cost_usd": turn.get("cost_usd"),
                "ok": turn.get("ok"),
                "error": turn.get("error"),
                "started_runs": started_runs,
                "reply_excerpt": (turn.get("reply") or "")[:400],
                "silent": (turn.get("reply") or "").strip() in ("", "SILENT"),
                "post_check": checked,
            }
            record["turns"] = (record["turns"] + [entry])[-KEEP_TURNS:]
            record["session"]["turns"] += 1
            record["last_turn"] = entry
            if not opening_done and turn.get("ok"):
                record["session"]["opened_at"] = moment.isoformat()
            record["heartbeat"] = now()
            write(thread, record)

            if turn.get("ok"):
                opening_done = True
                seen = after
                reply = (turn.get("reply") or "").strip()
                if reply and reply != "SILENT":
                    thread_tick.announce(
                        thread, current["report"]["title"], reply,
                        current["report"], datetime.now(timezone.utc))
            else:
                # Ход не состоялся. Разговор от этого не теряется — он на диске у
                # CLI, — но продолжать вслепую нельзя: ротация записывается и
                # сессия уступает watchdog'у.
                record["stopped"] = {"at": now(),
                                     "reason": f"ход сессии не отработал: {turn.get('error')}",
                                     "rotation": "требуется новая сессия"}
                write(thread, record)
                return 1
            cache_read = (turn.get("usage") or {}).get("cache_read_input_tokens")
            if (isinstance(cache_read, (int, float))
                    and not isinstance(cache_read, bool)
                    and cache_read >= MAX_CACHE_READ_INPUT_TOKENS):
                record["stopped"] = {
                    "at": now(),
                    "reason": (f"ход прочитал из кеша {cache_read:g} токенов: "
                               f"порог {MAX_CACHE_READ_INPUT_TOKENS:g} достигнут"),
                    "rotation": "требуется новая сессия",
                }
                write(thread, record)
                return 0
            if record["session"]["turns"] >= MAX_TURNS:
                record["stopped"] = {"at": now(),
                                     "reason": f"сессия отработала {MAX_TURNS} ходов: "
                                               "принудительная ротация",
                                     "rotation": "требуется новая сессия"}
                write(thread, record)
                return 0
        elif seen is None:
            seen = current

        if once:
            record["stopped"] = {"at": now(), "reason": "один проход по требованию"}
            write(thread, record)
            return 0
        if stop["asked"]:
            record["stopped"] = {"at": now(), "reason": "остановлена сигналом"}
            write(thread, record)
            return 0
        if (_seconds_since(started_at) or 0) >= MAX_LIFETIME_SECONDS:
            record["stopped"] = {"at": now(),
                                 "reason": f"сессия живёт дольше {MAX_LIFETIME_SECONDS} с: "
                                           "принудительная ротация",
                                 "rotation": "требуется новая сессия"}
            write(thread, record)
            return 0
        time.sleep(POLL_SECONDS)


# ---------------------------------------------------------------------------
# Watchdog: то, чем стал двадцатиминутный тик
# ---------------------------------------------------------------------------


def launch(thread: str) -> dict:
    """Поднять сессию так, чтобы она пережила процесс, который её поднял.

    Тик — `Type=oneshot`, и всё, что он оставил в своей контрольной группе,
    systemd убирает вместе с ним. Поэтому сессия уходит в собственную временную
    единицу. Там, где systemd нет, остаётся отделение сеанса — более слабая
    граница, и это записывается прямо, а не подразумевается.
    """
    command = [sys.executable, str(SELF), "run", thread]
    unit = f"product-goal-session-{thread}"
    runner = shutil.which("systemd-run")
    if runner:
        try:
            completed = subprocess.run(
                [runner, "--collect", "--unit", unit, "--property", "Type=simple",
                 "--working-directory", str(HOME), *command],
                capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            return {"started": False, "boundary": "systemd_transient_unit",
                    "error": str(error)}
        if completed.returncode == 0:
            return {"started": True, "boundary": "systemd_transient_unit", "unit": unit,
                    "src": f"systemd-run --unit {unit}, вне контрольной группы тика"}
        return {"started": False, "boundary": "systemd_transient_unit",
                "error": (completed.stderr or "").strip()[:300]}
    try:
        process = subprocess.Popen(command, cwd=str(HOME), start_new_session=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as error:
        return {"started": False, "boundary": "session_detachment_only", "error": str(error)}
    return {"started": True, "boundary": "session_detachment_only", "pid": process.pid,
            "src": "отделение сеанса без systemd: граница слабее временной единицы"}


def await_live(thread: str, before: dict, seconds: float | None = None) -> dict:
    """Дождаться, пока поднятая сессия действительно начнёт вести направление.

    Принятый запрос на запуск ведущей сессией не является, и разница между ними
    стоила ревью отдельного замечания (F-003): `systemd-run` возвращает ноль,
    как только systemd принял единицу, а дочерний процесс после этого может
    увидеть чужой маршрут, отсутствие целей или собственную ошибку и выйти. Тик,
    уступивший этому нулю, оставляет цель без продакта до следующего тика.

    Поэтому уступка стоит не на коде возврата запуска, а на наблюдении: в
    контрольном снимке появился *другой* процесс, и он жив. Отказ поднятой
    сессии виден там же и сразу — она записывает причину в `stopped` и выходит,
    и ждать её полный срок незачем.
    """
    seconds = LAUNCH_HANDSHAKE_SECONDS if seconds is None else seconds
    was = (before.get("pid"), before.get("since"))
    was_stopped = before.get("stopped")
    started = time.time()
    while True:
        record = read(thread)
        alive = liveness(record)
        waited = round(time.time() - started, 1)
        if alive["live"] and (record.get("pid"), record.get("since")) != was:
            return {"held": True, "waited_seconds": waited, "pid": alive.get("pid"),
                    "session": (record.get("session") or {}).get("id"),
                    "reason": alive["reason"],
                    "src": "контрольный снимок сессии, перечитанный после запуска"}
        stopped = record.get("stopped")
        if stopped and stopped != was_stopped:
            return {"held": False, "waited_seconds": waited,
                    "reason": "поднятая сессия сразу остановилась: "
                              f"{stopped.get('reason')}",
                    "src": "контрольный снимок сессии, перечитанный после запуска"}
        if time.time() - started >= seconds:
            return {"held": False, "waited_seconds": waited,
                    "reason": f"поднятая сессия не начала вести направление за "
                              f"{int(seconds)} с: {alive['reason']}",
                    "src": "контрольный снимок сессии, перечитанный после запуска"}
        time.sleep(HANDSHAKE_POLL_SECONDS)


def watchdog(thread: str, moment: datetime, act: bool = True) -> dict:
    """Что делает тик, когда направление ведёт непрерывная сессия.

    Исходы записываются в снимок направления все до одного, и каждый отвечает на
    единственный вопрос тика — уступать ли ему своего продакта (`holds`).

    Сессия не нужна: усиленных целей нет, обычная работа не получает ничего
    лишнего. Сессия жива: тик не поднимает второго продакта поверх неё. Маршрут
    ушёл к другому движку: непрерывного разговора в этом обходе не будет, и
    направление ведёт обычный фоновый продакт тика — эта ветка спрашивается
    *до* подавления владельца, потому что подавить ради сессии, которая сейчас
    выйдет, значит оставить цель без продакта вообще. Сессия умерла: тик
    поднимает её заново из долговечной цели и контрольного снимка и уступает
    только тогда, когда наблюдал её живой.
    """
    goals = reinforced(thread)
    record = read(thread)
    if not goals:
        return {"at": moment.isoformat(), "mode": "none", "live": False, "holds": False,
                "detail": "целей под усиленным контролем нет: направление ведёт тик",
                "session": record.get("session"),
                "src": "цели направления из продуктового хранилища"}
    alive = liveness(record)
    if alive["live"]:
        return {"at": moment.isoformat(), "mode": "session", "live": True, "holds": True,
                "pid": alive.get("pid"),
                "session": record.get("session"),
                "turns": record.get("session", {}).get("turns"),
                "last_turn_at": (record.get("last_turn") or {}).get("at"),
                "heartbeat": record.get("heartbeat"),
                "detail": "непрерывная сессия жива: тик не поднимает второго продакта",
                "src": alive["src"]}
    route = session_route()
    if not route["ok"]:
        if act:
            stand_down(thread, route)
        return {"at": moment.isoformat(), "mode": "session", "live": False,
                "holds": False, "recovered": False,
                "recovery_reason": alive["reason"],
                "previous_session": record.get("session", {}).get("id"),
                "route": route,
                "handover": "обычный фоновый продакт тика",
                "detail": f"маршрут увёл к {route['engine']}: продолжения разговора "
                          "этим движком нет",
                "goals": [goal["id"] for goal in goals],
                "src": route["src"]}
    started = (launch(thread) if act else
               {"started": False, "reason": "сухой прогон: сессию не поднимали"})
    handshake = (await_live(thread, record) if started.get("started")
                 else {"held": False, "waited_seconds": 0.0,
                       "reason": str(started.get("error") or started.get("reason")),
                       "src": "запуск сессии не состоялся: ждать было нечего"})
    failed = ("сухой прогон: сессию не поднимали, направление вёл бы продакт тика"
              if not act else "сессию поднять не удалось: " + str(handshake["reason"]))
    return {"at": moment.isoformat(), "mode": "session", "live": False,
            # `recovered` означает «направление снова ведёт сессия», а не
            # «запрос на запуск принят»: между этими двумя утверждениями и
            # проходила потерянная цель.
            "recovered": handshake["held"],
            "holds": handshake["held"],
            "handshake": handshake,
            "recovery_reason": alive["reason"],
            "previous_session": record.get("session", {}).get("id"),
            "boundary": started.get("boundary"),
            "error": started.get("error"),
            "handover": None if handshake["held"] else "обычный фоновый продакт тика",
            "detail": ("сессия восстановлена watchdog'ом: " + alive["reason"])
                      if handshake["held"] else failed,
            "goals": [goal["id"] for goal in goals],
            "src": handshake["src"]}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    running = sub.add_parser("run", help="вести направление одной сессией")
    running.add_argument("thread")
    running.add_argument("--once", action="store_true",
                         help="один проход наблюдения и выход")

    status = sub.add_parser("status", help="контрольный снимок сессии")
    status.add_argument("thread")
    status.add_argument("--json", action="store_true")

    stopping = sub.add_parser("stop", help="остановить сессию направления")
    stopping.add_argument("thread")

    args = parser.parse_args()
    if args.command == "run":
        return loop(args.thread, once=args.once)
    if args.command == "status":
        record = read(args.thread)
        alive = liveness(record)
        if args.json:
            print(json.dumps({"liveness": alive, **record}, ensure_ascii=False, indent=2))
            return 0
        if not record:
            print(f"по направлению {args.thread} сессии не было")
            return 0
        session = record.get("session") or {}
        print(f"сессия {session.get('id')} [{'жива' if alive['live'] else 'не жива'}]: "
              f"{alive['reason']}")
        print(f"  движок {session.get('engine')}/{session.get('model')}, "
              f"ходов {session.get('turns')}, открыта {session.get('opened_at')}")
        print(f"  удар сердца {record.get('heartbeat')}, "
              f"наблюдение {record.get('last_observation_at')}")
        for turn in record.get("turns", [])[-5:]:
            usage = turn.get("usage") or {}
            print(f"  ход {turn['n']} [{turn['kind']}] {turn['at']}: "
                  f"реакция {turn.get('reaction_seconds')} с, "
                  f"из кеша {usage.get('cache_read_input_tokens')}, "
                  f"пересборка контекста {turn.get('context_rebuilt')}, "
                  f"запустил {turn.get('started_runs')}")
        if record.get("recovered"):
            print(f"  восстановлена: {record['recovered'].get('reason')}")
        if record.get("stopped"):
            print(f"  остановлена: {record['stopped'].get('reason')}")
        return 0
    if args.command == "stop":
        record = read(args.thread)
        alive = liveness(record)
        if not alive["live"]:
            print(f"сессия направления {args.thread} не жива: {alive['reason']}")
            return 0
        os.kill(int(record["pid"]), signal.SIGTERM)
        print(f"сессии {record['session']['id']} послан SIGTERM")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
