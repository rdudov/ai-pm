#!/usr/bin/env python3
"""Compose and deliver one human-readable morning product standup."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import outbound  # noqa: E402
import plain_russian  # noqa: E402
import product_memory  # noqa: E402
import startup_context  # noqa: E402
import regular_jobs  # noqa: E402

HOME = Path(__file__).resolve().parents[1]
ROUTER = HOME / "scripts" / "claude_product_owner.py"
LOCAL_ZONE = ZoneInfo(os.environ.get("PRODUCT_OWNER_TIMEZONE", "Europe/Amsterdam"))
SEND_HOUR = 8
MODEL_TIMEOUT = 1800
FAILURE_RETRY_SECONDS = 3600
EMPTY_NEGATIVES = ("не сделано", "не проверено", "ещё не", "еще не", "пока не")


def source_packet(moment: datetime) -> dict:
    value = startup_context.packet()
    with outbound.Ledger() as ledger:
        recent = [letter for entry in ledger.data.get("threads", {}).values()
                  for letter in outbound.already_said(entry)]
    recent = sorted(recent, key=lambda item: item["at"])[-12:]
    plan = value.pop("portfolio_plan")
    if not value["contract"].get("plan_available", True):
        plan = ""
    result = {"plan": plan,
              "snapshots": value.pop("product_snapshots"),
              "threads": value.pop("thread_states"), **value,
              "wake_health": wake_health(),
              "regular_jobs": regular_jobs.collect(moment),
              "recent_letters": startup_context.fit(
                  recent, startup_context.reference(recent, str(outbound.LEDGER)),
                  20_000, "daily recent letters")}
    startup_context.require_size(startup_context.render(result),
                                 startup_context.MAX_PACKET_CHARS, "daily packet")
    return result


def wake_health() -> list[dict]:
    """Read the same state a tick writes; no second registry or notifications."""
    import thread_tick
    result = []
    for name, config in product_memory.installation().get("threads", {}).items():
        path = thread_tick.STATE_DIR / f"{name}.json"
        try:
            failure = json.loads(path.read_text()).get("wake_failure")
        except FileNotFoundError:
            continue
        if failure:
            result.append({"product": config.get("title", name),
                           "today": "Восстановить фоновую проверку",
                           "state": "Пробуждение не состоялось",
                           "blocker": failure["reason"], "source": str(path)})
    startup_context.require_size(startup_context.render(result), 20_000, "wake health")
    return result


def mechanically_empty(packet: dict) -> bool:
    plan = packet["plan"]
    return not ((plan.strip() if isinstance(plan, str) else plan)
                or any(value.strip() if isinstance(value, str) else value
                       for value in packet["snapshots"].values())
                or packet.get("regular_jobs") or packet.get("wake_health") or packet.get("active_goals") or any(
        any(report.get(key) for key in report if key not in {"thread", "title"})
        for report in packet["threads"].values()))


def previous_editions(legacy_path: Path | None = None) -> list[dict]:
    """Read bounded successful editions; migration is explicit, never prose parsing."""
    with outbound.Ledger() as ledger:
        entry = ledger.thread("portfolio")
        editions = entry.get("daily_editions", [])
        letters = [x for x in entry.get("letters", []) if x.get("kind") == "daily"]
    if not letters:
        if entry.get("delivered_events") and any(x.get("kind") == "daily" for x in entry["delivered_events"].values()):
            raise ValueError("Последнее письмо отсутствует в ограниченном журнале квитанций")
        return []
    latest = letters[-1]
    if editions and editions[-1]["event_id"] == latest["event_id"]:
        return editions
    if legacy_path is None:
        raise ValueError("Последний выпуск старого формата требует --previous-plan с полным письмом и разбором плана")
    extraction = json.loads(legacy_path.read_text(encoding="utf-8"))
    if extraction.get("source_message_id") != latest.get("message_id"):
        raise ValueError("Разбор старого плана относится к другому выпуску")
    body = Path(extraction["source_path"]).read_text(encoding="utf-8")
    plans = extraction.get("items")
    if not body.strip() or not isinstance(plans, list) or not all(
            isinstance(row, dict) and all(isinstance(row.get(k), str) and row[k].strip()
                                         for k in ("id", "product", "action", "source_text"))
            and row["source_text"] in body for row in plans):
        raise ValueError("Нужен сохранённый разбор с точными строками полного письма")
    if len({row["id"] for row in plans}) != len(plans):
        raise ValueError("Повторный id прежнего плана")
    if not body.strip().startswith(latest.get("excerpt", "")):
        raise ValueError("Исходное письмо отличается от квитанции")
    return [{**latest, "body": body, "plans": plans}]



def validate_plans(plans) -> None:
    keys = {"product", "action", "result", "deadline"}
    if not isinstance(plans, list) or not all(
            isinstance(row, dict) and all(isinstance(row.get(k), str) and row[k].strip()
                                         for k in keys) for row in plans):
        raise ValueError("Каждый шаг требует действие, результат и срок")


def validate_composition(data: dict, editions: list[dict]) -> None:
    validate_plans(data["plans"])
    if len(data["waiting"].splitlines()) > 1:
        raise ValueError("Ожидания должны занимать одну строку")
    previous = editions[-1].get("plans", []) if editions else []
    ids = [row["id"] for row in previous]
    outcomes = data["outcomes"]
    if sorted(row["id"] for row in outcomes) != sorted(ids):
        raise ValueError("Итоги должны покрывать каждый прежний пункт ровно один раз")
    for row in outcomes:
        if row["status"] not in {"получилось", "частично", "перенесено", "снято"} or not row["detail"].strip():
            raise ValueError("Нужен конкретный итог прежнего пункта")
        if row["status"] == "перенесено" and not all(
                isinstance(row.get(k), str) and row[k].strip() for k in ("deadline", "approach")):
            raise ValueError("Перенос требует срок и другой путь")
    if data.get("quiet"):
        keys = ("product", "action", "result", "deadline")
        if [tuple(p.get(k) for k in keys) for p in data["plans"]] != [tuple(p.get(k) for k in keys) for p in previous]:
            raise ValueError("Тихий день должен сохранять прежний план без новых шагов")
    for row in data["plans"]:
        if not data.get("quiet") and any((row["action"].strip().casefold(), row["result"].strip().casefold()) ==
               (old["action"].strip().casefold(), old.get("result", row["result"]).strip().casefold()) for old in previous):
            raise ValueError("Повтор плана требует другого подхода или вопроса владельцу")
    validate_compact(data)
    text = json.dumps(data, ensure_ascii=False).casefold()
    if any(phrase in text for phrase in EMPTY_NEGATIVES):
        raise ValueError("Препятствие нужно превратить в действие или вопрос")


def prompt(packet: dict, local_date: str) -> str:
    return f"""Составь короткую оперативку на {local_date}. Верни только JSON:
{{"intro":"коротко о предшественнике", "headline":"главное событие для темы",
"outcomes":[{{"id":"прежний id","status":"получилось|частично|перенесено|снято",
"detail":"краткий итог","deadline":"при переносе — когда","approach":"другой путь"}}],
"plans":[{{"product":"работа (номер задачи)","action":"действие","result":"результат","deadline":"срок"}}],
"rows":[{{"work":"работа (номер задачи)","outcome_ids":["прежний id"],"plan_indexes":[0]}}],
"extras":["важная новость"],"removed":[],"waiting":"одна короткая строка",
"waiting_count":0,"quiet":false,
"questions":[{{"question":"выбор","recommendation":"совет","tradeoff":"цена"}}]}}
Инициативы: пустой список допустим; инициатив и новостей может быть ноль.
Порядок: загрузка; вопросы; главное (до трёх); таблица до восьми строк;
ожидания одной строкой; снятое при наличии. Каждая ячейка до 90 знаков.
Каждый прежний id получает ровно один outcomes. Каждый итог кроме снятого
покажи ровно в одной rows.outcome_ids, каждый новый шаг — в rows.plan_indexes.
Можно объединять связанные работы, сохранив все итоги видимыми.
Вчера: ✓ получилось, ◐ частично, → перенесено; снятое с ✗ отдельно.
План называет действие, результат и срок; второй день без продвижения требует
другого подхода или вопроса. Один изменённый срок другим подходом не считается.
Снятие объясни; ожидания не превращай в ежедневное действие. waiting_count —
число внешних ожиданий; число в работе вычисляется по реальным plans.
Вопросы — реальные решения владельца с рекомендацией и ценой; прежний ожидающий
ответа вопрос допустим. Не придумывай вопросы. Числа образца не константы.
Тихий день: quiet=true, короткое «Без изменений. В работе N, вопросов нет»;
в этом случае план и итоги всё равно сохраняются, но никаких новых событий,
вопросов или снятий. Не используй quiet для сокрытия обязательной новости.
regular_jobs — первичный журнал регулярных работ. Обязательные факты дверь
добавляет в Главное сама: учитывай их в пределе трёх, не дублируй.
Не прячь простыню в intro, вопросы или ожидания; никаких хешей, попыток и
проверяющих. Прежний полный выпуск и promises находятся в previous_editions.
Не выдумывай факты; прочитай полный текущий план и относящиеся оригиналы.
wake_health дверь добавит в ожидания; это не сделанная работа.
{plain_russian.as_bullet()}
Источник:
{json.dumps(packet, ensure_ascii=False)}
"""


def parse_composition(text: str, editions: list[dict] | None = None) -> dict:
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        if not all(isinstance(value.get(k), str) for k in ("intro", "waiting")):
            continue
        if not all(isinstance(value.get(k), list) for k in
                   ("plans", "outcomes", "extras", "removed", "questions")):
            continue
        if not all(isinstance(x, str) and x.strip() for k in ("extras", "removed") for x in value[k]):
            continue
        if not all(isinstance(x, dict) and all(isinstance(x.get(k), str) and x[k].strip()
                   for k in keys) for name, keys in
                   (("outcomes", ("id", "status", "detail")),
                    ("questions", ("question", "recommendation", "tradeoff"))) for x in value[name]):
            continue
        validate_composition(value, editions or [])
        return value
    raise ValueError("product owner did not return the daily standup JSON contract")


def compose(packet: dict, local_date: str) -> dict:
    request = prompt(packet, local_date)
    for attempt in range(2):
        model_input = startup_context.checked_prompt(request)
        result = subprocess.run(
            [str(ROUTER), "--entry", "print"], input=model_input, text=True,
            capture_output=True, cwd=HOME, env={**os.environ, "IS_SANDBOX": "1"},
            timeout=MODEL_TIMEOUT, check=False)
        if result.returncode != 0:
            import thread_tick
            raise RuntimeError(thread_tick.engine_failure(result, "daily"))
        try:
            data = parse_composition(result.stdout or "", packet.get("previous_editions", []))
            apply_job_news(data, packet)
            validate_composition(data, packet.get("previous_editions", []))
            return data
        except (ValueError, KeyError, TypeError) as error:
            if attempt:
                raise
            request = prompt(packet, local_date) + "\nПересобери один раз: " + str(error)
    raise AssertionError("unreachable")


def apply_job_news(data: dict, packet: dict) -> None:
    editions = packet.get("previous_editions", [])
    previous_at = editions[-1].get("at") if editions else None
    facts = regular_jobs.required_news(packet.get("regular_jobs", []), previous_at)
    if facts:
        data["extras"] = facts + data["extras"]
        data["quiet"] = False
        data["headline"] = facts[0] if len(facts[0]) <= 150 else "Длительный сбой регулярных заданий"
    validate_compact(data)


# Preserve sentence punctuation supplied by the composer.
SENTENCE_END = (".", "!", "?", "…", ":")


def ends_sentence(text: str) -> str:
    """Фраза с одним конечным знаком: своим, если он есть, иначе точкой."""
    text = text.strip()
    return text if not text or text.endswith(SENTENCE_END) else text + "."


STATUS_MARKS = {"получилось": "✓", "частично": "◐", "перенесено": "→"}


def table_rows(data: dict) -> list[list[str]]:
    outcomes = {row["id"]: row for row in data["outcomes"]}
    rows = []
    for row in data["rows"]:
        yesterday = "; ".join(
            f"{STATUS_MARKS[outcomes[key]['status']]} {outcomes[key]['detail']}" +
            (f"; {outcomes[key]['deadline']}; {outcomes[key]['approach']}"
             if outcomes[key]['status'] == 'перенесено' else '')
            for key in row["outcome_ids"]) or "новое"
        plans = [data["plans"][index] for index in row["plan_indexes"]]
        rows.append([row["work"], yesterday,
                     "; ".join(f"{p['action']} → {p['result']}" for p in plans) or "—",
                     "; ".join(p["deadline"] for p in plans) or "—"])
    return rows


def removed_rows(data: dict) -> list[str]:
    return [f"✗ {row.get('label', row['id'])}: {row['detail']}"
            for row in data["outcomes"] if row["status"] == "снято"] + data["removed"]


def validate_compact(data: dict) -> None:
    def bounded(text, limit):
        if not isinstance(text, str) or len(text) > limit or len(text.splitlines()) > 1:
            raise ValueError(f"Короткое поле превышает {limit} знаков или одну строку")
    if not isinstance(data.get("rows"), list) or len(data["rows"]) > 8:
        raise ValueError("Таблица: максимум восемь строк")
    if len(data["extras"]) > 3:
        raise ValueError("Главное: максимум три новости")
    if type(data.get("waiting_count")) is not int or data["waiting_count"] < 0:
        raise ValueError("Нужно число ожиданий")
    if type(data.get("quiet")) is not bool:
        raise ValueError("Нужен признак тихого дня")
    for key, limit in (("intro", 150), ("headline", 150), ("waiting", 400)):
        bounded(data.get(key), limit)
    for text in data["extras"] + data["removed"]:
        bounded(text, 300)
    for question in data["questions"]:
        for key in ("question", "recommendation", "tradeoff"):
            bounded(question[key], 180)
    expected = [row["id"] for row in data["outcomes"] if row["status"] != "снято"]
    actual, indexes = [], []
    for row in data["rows"]:
        if not isinstance(row, dict) or not isinstance(row.get("outcome_ids"), list) or not isinstance(row.get("plan_indexes"), list):
            raise ValueError("Строка требует связи с итогами и планом")
        if not row["outcome_ids"] and not row["plan_indexes"]:
            raise ValueError("Пустая строка работы")
        actual += row["outcome_ids"]
        indexes += row["plan_indexes"]
    if sorted(actual) != sorted(expected) or any(type(x) is not int for x in indexes) or sorted(indexes) != list(range(len(data["plans"]))):
        raise ValueError("Каждый итог и новый шаг должны быть видимы ровно один раз")
    for row in table_rows(data):
        for cell in row:
            bounded(cell, 90)
    if data["quiet"] and (data["extras"] or data["questions"] or removed_rows(data)
                          or any(x["status"] != "перенесено" for x in data["outcomes"])):
        raise ValueError("Тихий день содержит события")


def load_line(data: dict) -> str:
    return f"Загрузка: в работе {len(data['plans'])} · ждут событий {data['waiting_count']}"


def quiet_line(data: dict) -> str:
    return f"Без изменений. В работе {len(data['plans'])}, вопросов нет."


def render_plain(data: dict) -> str:
    if data.get("quiet"):
        return quiet_line(data) + ("\n\nЖдём событий: " + data['waiting'] if data['waiting'] else "")
    parts = [load_line(data)]
    if data.get("refusal"):
        parts += [data["intro"]]
    if data['questions']:
        parts += ["", "Нужно от вас"] + [
            f"{i}. {q['question']} Рекомендую: {q['recommendation']} Цена: {q['tradeoff']}"
            for i, q in enumerate(data['questions'], 1)]
    if data['extras']:
        parts += ["", "Главное"] + [f"- {x}" for x in data['extras']]
    if data['rows']:
        parts += ["", "В работе", "Работа | Вчера | Сегодня → результат | Срок"]
        parts += [' | '.join(row) for row in table_rows(data)]
    if data['waiting']:
        parts += ["", "Ждём событий: " + data['waiting']]
    if removed_rows(data):
        parts += ["", "Снято: " + "; ".join(removed_rows(data))]
    return '\n'.join(parts)


def render_html(data: dict) -> str:
    if data.get("quiet"):
        content = f"<p>{escape(quiet_line(data))}</p>"
        if data['waiting']:
            content += f"<p>Ждём событий: {escape(data['waiting'])}</p>"
    else:
        content = f"<p>{escape(load_line(data))}</p>"
        if data.get("refusal"):
            content += f"<p>{escape(data['intro'])}</p>"
        if data['questions']:
            content += '<h2>Нужно от вас</h2><ol>' + ''.join(
                f"<li>{escape(q['question'])} Рекомендую: {escape(q['recommendation'])} Цена: {escape(q['tradeoff'])}</li>"
                for q in data['questions']) + '</ol>'
        if data['extras']:
            content += '<h2>Главное</h2><ul>' + ''.join(f'<li>{escape(x)}</li>' for x in data['extras']) + '</ul>'
        if data['rows']:
            content += '<h2>В работе</h2><table><thead><tr>' + ''.join(
                f'<th>{x}</th>' for x in ('Работа', 'Вчера', 'Сегодня → результат', 'Срок')) + '</tr></thead><tbody>'
            content += ''.join('<tr>' + ''.join(f'<td>{escape(cell)}</td>' for cell in row) + '</tr>' for row in table_rows(data))
            content += '</tbody></table>'
        if data['waiting']:
            content += f"<p>Ждём событий: {escape(data['waiting'])}</p>"
        if removed_rows(data):
            content += '<p>Снято: ' + escape('; '.join(removed_rows(data))) + '</p>'
    return f"""<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<style>body{{margin:0;color:#202124;font:16px/1.45 Arial,sans-serif}}
.card{{max-width:1000px;margin:auto;padding:20px;overflow-wrap:anywhere}}
h1{{font-size:24px}}h2{{font-size:18px;margin:20px 0 8px}}li{{margin:6px 0}}
table{{border-collapse:collapse;width:100%;table-layout:fixed;font-size:14px}}
th,td{{text-align:left;vertical-align:top;border-bottom:1px solid #ddd;padding:8px 5px}}
th:last-child,td:last-child{{width:14%}}
@media(max-width:520px){{.card{{padding:10px}}table{{font-size:13px}}th,td{{padding:6px 3px}}}}
</style></head><body><main class=card><h1>Оперативка</h1>{content}</main></body></html>"""


def raw_message(to: str, subject: str, plain: str, html: str) -> bytes:
    message = EmailMessage(policy=policy.default.clone(refold_source="none"))
    message["To"] = to
    message["Subject"] = subject
    message.set_content(plain)
    message.add_alternative(html, subtype="html")
    return message.as_bytes()


def sent_today(moment: datetime) -> bool:
    with outbound.Ledger() as ledger:
        last = outbound.last_of_kind(ledger.thread("portfolio"), "daily")
    return bool(last and last.astimezone(LOCAL_ZONE).date() == moment.astimezone(LOCAL_ZONE).date())


def maybe_send(moment: datetime | None = None, force: bool = False,
               previous: dict | None = None, legacy_path: Path | None = None) -> dict:
    moment = moment or datetime.now(timezone.utc)
    local = moment.astimezone(LOCAL_ZONE)
    # The CLI and process timer share the same current outcome. A standalone
    # forced attempt has no direction snapshot, but must still wake process.
    with outbound.Ledger() as ledger:
        latest = ledger.thread("portfolio").get("daily_standup")
    previous = latest if latest is not None else previous
    pending = previous if previous and previous.get("action") == "fail" else None
    if not force and local.hour < SEND_HOUR:
        if pending:
            return {**pending, "deferred": True}
        return {"action": "skip", "reason": "before the 08:00 local timer firing"}
    event_id = f"daily:{local.date().isoformat()}"
    if sent_today(moment):
        if not force:
            if pending:
                return {**pending, "deferred": True}
            return {"action": "skip", "reason": "daily standup already sent today",
                    "daily_delivered": True}
        # Explicit correction of a legacy refusal receipt (or an intentionally
        # corrected edition). One fixed identity per date, never a random retry.
        event_id += ":correction"
        with outbound.Ledger() as ledger:
            if outbound.event_delivered_anywhere(ledger.data, event_id):
                return {"action": "skip", "reason": "daily correction already sent today",
                        "daily_delivered": True}
    if not force and previous and previous.get("action") == "fail":
        try:
            failed_at = datetime.fromisoformat(str(previous["at"]))
        except (KeyError, TypeError, ValueError):
            failed_at = None
        if failed_at and (moment - failed_at).total_seconds() < FAILURE_RETRY_SECONDS:
            return {**previous, "deferred": True}
    import thread_tick
    if not (thread_tick.MAIL_TO and thread_tick.MAIL_SCRIPT.is_file()
            and thread_tick.MAIL_PYTHON.is_file()):
        return record_failure(moment, "delivery",
                              "почтовая дверь недоступна — оперативка не собиралась")
    failure = None
    stage = "sources"
    try:
        packet = source_packet(moment)
        packet["previous_editions"] = previous_editions(legacy_path)
        if mechanically_empty(packet) and not any(x.get("plans") for x in packet["previous_editions"][-1:]):
            if pending:
                return {**pending, "deferred": True}
            return {"action": "skip", "reason": "all mechanical sources are empty"}
        startup_context.require_size(startup_context.render(packet), startup_context.MAX_PACKET_CHARS, "daily packet")
        stage = "composition"
        data = compose(packet, local.date().isoformat())
        validate_composition(data, packet["previous_editions"])
        labels = {x["id"]: x["product"] for x in
                  (packet["previous_editions"][-1]["plans"] if packet["previous_editions"] else [])}
        for row in data["outcomes"]:
            row["label"] = labels[row["id"]]
        for index, row in enumerate(data["plans"], 1):
            row["id"] = f"{event_id}:{index}"
    except (startup_context.InputLimitError, product_memory.ContentError,
            RuntimeError, ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired) as error:
        print(f"[daily {stage}] {type(error).__name__}: {error}", file=sys.stderr)
        reason = str(error) if isinstance(error, (startup_context.InputLimitError, RuntimeError)) else (
            "Не удалось прочитать источники; подробности — в журнале службы."
            if stage == "sources" else
            "Модель не вернула готовую оперативку; подробности — в журнале службы.")
        failure = record_failure(moment, stage, reason, code=type(error).__name__)
        # A failed composer cannot explain its own absence. Use the same daily
        # channel, but never receipt a refusal as a delivered daily edition.
        data = {"refusal": True, "intro": "Восстановить сборку оперативки. Оперативка не собрана: " + reason,
                "plans": [], "outcomes": [], "extras": [],
                "removed": [], "waiting": "", "questions": [], "rows": [],
                "headline": "Восстановить сборку оперативки", "waiting_count": 0, "quiet": False}
        try:
            packet = {"wake_health": wake_health()}
        except (startup_context.InputLimitError, OSError, ValueError, KeyError, TypeError):
            packet = {}
    if packet.get("wake_health"):
        # Persistent failures are waits, never completed work. Raw diagnostics
        # belong only in the refusal; successful prose stays with the composer.
        health_text = "Ждём восстановления фоновых проверок."
        if failure:
            details = []
            for row in packet["wake_health"]:
                detail = f"{row['product']}: восстановить фоновую проверку. Причина: {row['blocker']}"
                if any(phrase in detail.casefold() for phrase in EMPTY_NEGATIVES):
                    print(f"[daily wake health] {detail}", file=sys.stderr)
                    detail = "Восстановить фоновую проверку. Причина и направление — в журнале службы."
                details.append(ends_sentence(detail))
            health_text = " ".join(details)
        data["waiting"] = " ".join(filter(None, (ends_sentence(data["waiting"]), health_text)))
    plain, html = render_plain(data), render_html(data)
    subject = f"Оперативка {local:%d.%m} — {len(data['questions'])} вопросов к вам; {data['headline']}"
    delivery = thread_tick.deliver(
        "portfolio", "daily_refusal" if failure else "daily", subject, plain, moment,
        raw_message=raw_message(thread_tick.MAIL_TO, subject, plain, html),
        event_id=f"daily:{local.date().isoformat()}:refusal" if failure else event_id,
        daily_edition=None if failure else {"composition": data, "plans": data["plans"],
                                           "body": plain, "html": html,
                                           "previous_event_id": (packet["previous_editions"][-1]["event_id"]
                                                                 if packet["previous_editions"] else None)})
    if failure:
        return {**failure, "notice": delivery}
    if delivery.get("action") == "fail":
        return record_failure(moment, "delivery", delivery["reason"])
    with outbound.Ledger() as ledger:
        ledger.thread("portfolio")["daily_standup"] = delivery
    return delivery


def record_failure(moment: datetime, stage: str, reason: str, *, code: str | None = None) -> dict:
    """Reuse the delivery journal for history and process state for current health."""
    failure = {"action": "fail", "at": moment.isoformat(), "stage": stage,
               "reason": reason, "code": code or stage,
               "thread": "portfolio", "kind": "daily"}
    with outbound.Ledger() as ledger:
        ledger.thread("portfolio")["daily_standup"] = failure
        ledger.record(failure)
    return failure


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--previous-plan", type=Path, help="Saved product-owned extraction of latest legacy daily")
    args = parser.parse_args()
    result = maybe_send(force=args.force, legacy_path=args.previous_plan)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("action") in {"send", "skip"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
