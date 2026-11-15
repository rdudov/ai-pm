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
                or packet.get("wake_health") or packet.get("active_goals") or any(
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
    for row in data["plans"]:
        if any((row["action"].strip().casefold(), row["result"].strip().casefold()) ==
               (old["action"].strip().casefold(), old.get("result", row["result"]).strip().casefold()) for old in previous):
            raise ValueError("Повтор плана требует другого подхода или вопроса владельцу")
    text = json.dumps(data, ensure_ascii=False).casefold()
    if any(phrase in text for phrase in EMPTY_NEGATIVES):
        raise ValueError("Препятствие нужно превратить в действие или вопрос")


def prompt(packet: dict, local_date: str) -> str:
    return f"""Составь утреннюю продуктовую оперативку на {local_date} по источнику ниже.
Верни только JSON без markdown и комментариев в форме:
{{"intro":"одно человеческое предложение с датой предыдущего выпуска", "outcomes":[{{"id":"id прежнего пункта","status":"получилось|частично|перенесено|снято","detail":"конкретный полученный результат или причина снятия","deadline":"при переносе — когда","approach":"при переносе — другой путь"}}], "extras":["что сделано сверх плана"], "plans":[{{"product":"...","action":"действие","result":"ожидаемый результат","deadline":"срок с часовым поясом"}}], "removed":["что снято и почему"], "waiting":"одна короткая строка всех внешних ожиданий", "questions":[{{"question":"...","recommendation":"...","tradeoff":"цена вариантов"}}]}}

Правила:
- previous_editions содержит полные успешные выпуски, последний — предшественник.
  Дай ровно один итог каждому id его plans. Ничего не пропускай и не закрывай дважды.
  При частичном результате назови полученную часть. При переносе назови новый срок и другой путь.
  При correction предшественник может быть сегодняшним: явно назови его дату в intro.
- Пиши шесть разделов через указанные поля; пустой список допустим в каждом разделе.
- План — работа ради результата, а не ежедневное чтение журналов.
  Сверь прежнее действие и его итог. Второй день без продвижения требует смены подхода
  или вопроса владельцу; изменение одного срока не считается сменой подхода.
  Идентичные action/result будут отвергнуты. Снятие объясни в removed.
- Все постоянные ожидания собери в одну короткую waiting. При событии верни действие в план.
- Вопросы — только новый выбор владельца с рекомендацией и ценой вариантов.
  Не повторяй прежние вопросы без события. Новую полезную инициативу включи в план.
- Избегай «не сделано», «не проверено», «ещё не», «пока не».
  Препятствие преврати в действие или вопрос. Простое инженерное русское предложение:
  одно утверждение, примерно до 20 слов, до 4 предложений на абзац.
  Расшифровывай внутренние слова; отделяй слова от чисел, сохраняя точные идентификаторы.
- Не выдумывай факты. Манифест не равен чтению: открой полный текущий план
  и относящиеся оригиналы до решения. Не пересказывай неизменные recent_letters.
- wake_health содержит причины отказов направлений. Используй их для смены подхода
  или нового вопроса, когда это нужно. Дверь добавит общую строку ожидания восстановления;
  не дублируй её и не называй постоянный отказ сделанной дополнительной работой.

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
    model_input = startup_context.checked_prompt(prompt(packet, local_date))
    result = subprocess.run(
        [str(ROUTER), "--entry", "print"],
        input=model_input, text=True, capture_output=True,
        cwd=HOME, env={**os.environ, "IS_SANDBOX": "1"}, timeout=MODEL_TIMEOUT,
        check=False)
    if result.returncode != 0:
        import thread_tick
        raise RuntimeError(thread_tick.engine_failure(result, "daily"))
    return parse_composition(result.stdout or "", packet.get("previous_editions", []))


# Preserve sentence punctuation supplied by the composer.
SENTENCE_END = (".", "!", "?", "…", ":")


def ends_sentence(text: str) -> str:
    """Фраза с одним конечным знаком: своим, если он есть, иначе точкой."""
    text = text.strip()
    return text if not text or text.endswith(SENTENCE_END) else text + "."


SECTIONS = ("Итоги вчерашнего плана", "Сделано сверх плана", "План на сегодня",
            "Больше не актуально", "Ждём событий", "Нужно от вас")


def section_rows(data: dict) -> list[list[str]]:
    return [
        [f"{row.get('label', row['id'])}: {row['status']}. {ends_sentence(row['detail'])}" +
         (f" Срок: {row['deadline']}. Другой путь: {ends_sentence(row['approach'])}"
          if row['status'] == 'перенесено' else '') for row in data['outcomes']],
        data['extras'],
        [f"{row['product']}: {ends_sentence(row['action'])} Результат: {ends_sentence(row['result'])} "
         f"Срок: {ends_sentence(row['deadline'])}" for row in data['plans']],
        data['removed'], [data['waiting']] if data['waiting'].strip() else [],
        [f"{ends_sentence(row['question'])} Рекомендация: {ends_sentence(row['recommendation'])} "
         f"Цена вариантов: {ends_sentence(row['tradeoff'])}" for row in data['questions']]]


def render_plain(data: dict) -> str:
    parts = [data['intro']]
    for title, rows in zip(SECTIONS, section_rows(data)):
        parts += ['', title] + ([f"- {row}" for row in rows] or ['Нет.'])
    return '\n'.join(parts)


def render_html(data: dict) -> str:
    sections = ''.join(f"<h2>{title}</h2>" +
                       ('<ul>' + ''.join(f'<li>{escape(row)}</li>' for row in rows) + '</ul>'
                        if rows else '<p class=muted>Нет.</p>')
                       for title, rows in zip(SECTIONS, section_rows(data)))
    return f"""<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<style>
body{{margin:0;background:#f5f6f8;color:#202124;font:16px/1.45 Arial,sans-serif}}
.card{{max-width:680px;margin:0 auto;background:#fff;padding:24px;overflow-wrap:anywhere}}
h1{{font-size:24px;line-height:1.2;margin:0 0 12px}}h2{{font-size:18px;margin:28px 0 10px}}
p{{margin:6px 0}}ul{{padding-left:22px}}li{{margin-bottom:14px}}.muted{{color:#5f6368}}
@media(max-width:520px){{.card{{padding:18px 14px}}h1{{font-size:22px}}}}
</style></head><body><main class=card><h1>Продуктовая оперативка</h1>
<p>{escape(data['intro'])}</p>{sections}</main></body></html>"""


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
        labels = {x["id"]: f"{x['product']}: {x['action']}" for x in
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
        data = {"intro": "Восстановить сборку оперативки. Оперативка не собрана: " + reason,
                "plans": [], "outcomes": [], "extras": [],
                "removed": [], "waiting": "", "questions": []}
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
    subject = f"Продуктовая оперативка — {local:%d.%m}"
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
