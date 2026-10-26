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


def prompt(packet: dict, local_date: str) -> str:
    return f"""Составь утреннюю продуктовую оперативку на {local_date} по источнику ниже.
Верни только JSON без markdown и комментариев в форме:
{{"intro":"одно человеческое предложение", "plans":[{{"product":"...","today":"...","state":"...","blocker":"..."}}], "questions":[{{"question":"...","recommendation":"...","tradeoff":"цена вариантов"}}], "initiatives":[{{"idea":"...","effect":"пользовательский эффект","first_step":"самый короткий следующий шаг"}}]}}

Правила:
- планы — по каждому направлению, где сегодня есть работа или наблюдаемое ожидание;
- вопросы — только выбор, который действительно должен сделать пользователь; к каждому дай рекомендацию и цену вариантов;
- инициативы — от 0 до 3; только при новой потребности или наблюдаемой проблеме;
  без полезного повода верни пустой список, сохранив планы, ожидания и вопросы;
- правила языка передаёт общий маршрутизатор;
- не рассказывай внутреннюю машинерию, номера гейтов и раннеров;
- не используй строки «ПОВОД» и «ВОПРОС»;
- не выдумывай факты и не повторяй отложенный вопрос из другого предмета.
- планы дня показывай всегда; статусы, вопросы и инициативы из recent_letters
  не пересказывай дословно, если с тех пор ничего не изменилось.
- Манифесты заменяют текст, а не чтение: открой текущий план с порядком работ
  и все относящиеся к решению полные оригиналы до составления оперативки.
- wake_health — наблюдаемые отказы, а не обещание работающих проверок.
  Эти строки дверь добавит сама; не дублируй их и не обещай выполненную проверку.

Источник:
{json.dumps(packet, ensure_ascii=False)}
"""


def parse_composition(text: str) -> dict:
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
        required = {"intro", "plans", "questions", "initiatives"}
        if required <= value.keys() and isinstance(value["intro"], str) and all(
                isinstance(value[key], list) for key in required - {"intro"}):
            shapes = (("plans", {"product", "today", "state", "blocker"}),
                      ("questions", {"question", "recommendation", "tradeoff"}),
                      ("initiatives", {"idea", "effect", "first_step"}))
            valid_rows = all(
                all(isinstance(row, dict) and keys <= row.keys()
                    and all(isinstance(row[key], str) for key in keys)
                    for row in value[name])
                for name, keys in shapes)
            if valid_rows and 0 <= len(value["initiatives"]) <= 3:
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
    return parse_composition(result.stdout or "")


# Модель пишет поля оперативки законченными предложениями и ставит знак сама.
# Рендер добавлял свой знак поверх, и 24 августа 2026 пользователь получил
# «нужны.; мешает» и «Первый шаг: … ..». Оба helper'а ниже читают то, что уже
# написано, а не переписывают текст.
SENTENCE_END = (".", "!", "?", "…", ":")


def ends_sentence(text: str) -> str:
    """Фраза с одним конечным знаком: своим, если он есть, иначе точкой."""
    text = text.strip()
    return text if not text or text.endswith(SENTENCE_END) else text + "."


def opens_clause(text: str) -> str:
    """Та же фраза перед нашей точкой с запятой, без её собственной точки."""
    return text.strip().rstrip(".;,")


def render_plain(data: dict) -> str:
    parts = [data["intro"], "", "Планы на сегодня"]
    for row in data["plans"]:
        blocker = row.get("blocker", "").strip()
        tail = ("" if blocker.casefold() in {"", "нет", "-", "—"}
                else f"; мешает: {ends_sentence(blocker)}")
        state = opens_clause(row["state"]) if tail else ends_sentence(row["state"])
        parts.append(
            f"- {row['product']}: {opens_clause(row['today'])} — {state}{tail}")
    if data["questions"]:
        parts += ["", "Нужен ваш выбор"]
        for item in data["questions"]:
            parts += [f"- {item['question']}", f"  Рекомендация: {item['recommendation']}",
                      f"  Цена вариантов: {item['tradeoff']}"]
    if data["initiatives"]:
        parts += ["", "Что ещё стоит попробовать"]
    for item in data["initiatives"]:
        parts.append(f"- {ends_sentence(item['idea'])} "
                     f"Эффект: {ends_sentence(item['effect'])} "
                     f"Первый шаг: {ends_sentence(item['first_step'])}")
    return "\n".join(parts).strip()


def render_html(data: dict) -> str:
    rows = [{**row, "blocker": ("" if row.get("blocker", "").strip().casefold()
                                      in {"", "нет", "-", "—"}
                                      else row["blocker"])} for row in data["plans"]]
    plans = "".join(
        "<tr>" + "".join(f"<td data-label=\"{label}\">{escape(str(row.get(key, '')))}</td>"
                          for key, label in (("product", "Продукт"), ("today", "Сегодня"),
                                             ("state", "Состояние"), ("blocker", "Что мешает"))) + "</tr>"
        for row in rows)
    questions = "".join(
        f"<li><p><strong>{escape(item['question'])}</strong></p>"
        f"<p>Рекомендация: {escape(item['recommendation'])}</p>"
        f"<p class=muted>Цена вариантов: {escape(item['tradeoff'])}</p></li>"
        for item in data["questions"])
    initiatives = "".join(
        f"<li><p><strong>{escape(item['idea'])}</strong></p>"
        f"<p>{escape(item['effect'])}</p><p class=muted>Первый шаг: {escape(item['first_step'])}</p></li>"
        for item in data["initiatives"])
    question_section = (f"<h2>Нужен ваш выбор</h2><ol>{questions}</ol>"
                        if questions else "")
    initiative_section = (f"<h2>Что ещё стоит попробовать</h2><ol>{initiatives}</ol>"
                          if initiatives else "")
    return f"""<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<style>
body{{margin:0;background:#f5f6f8;color:#202124;font:16px/1.45 Arial,sans-serif}}
.card{{max-width:680px;margin:0 auto;background:#fff;padding:24px}}
h1{{font-size:24px;line-height:1.2;margin:0 0 12px}}h2{{font-size:18px;margin:28px 0 10px}}
p{{margin:6px 0}}table{{border-collapse:collapse;width:100%;table-layout:fixed;font-size:14px}}
th,td{{border:1px solid #dfe1e5;padding:9px;text-align:left;vertical-align:top;overflow-wrap:anywhere}}
th{{background:#f1f3f4}}th:nth-child(1){{width:16%}}th:nth-child(3){{width:18%}}th:nth-child(4){{width:22%}}
ol{{padding-left:22px}}li{{margin:0 0 14px}}.muted{{color:#5f6368;font-size:14px}}
@media(max-width:520px){{.card{{padding:18px 14px}}h1{{font-size:22px}}h2{{font-size:18px}}
table,thead,tbody,tr,th,td{{display:block}}thead{{position:absolute;left:-9999px}}tr{{border:1px solid #dfe1e5;margin:0 0 10px;padding:5px 0}}
td{{border:0;padding:5px 8px 5px 38%;position:relative;min-height:20px}}td:before{{content:attr(data-label);position:absolute;left:8px;width:34%;font-weight:bold;color:#5f6368}}}}
</style></head><body><main class=card><h1>Продуктовая оперативка</h1>
<p>{escape(data['intro'])}</p><h2>Планы на сегодня</h2>
<table><thead><tr><th>Продукт</th><th>Сегодня</th><th>Состояние</th><th>Что мешает</th></tr></thead><tbody>{plans}</tbody></table>
{question_section}{initiative_section}</main></body></html>"""


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
               previous: dict | None = None) -> dict:
    moment = moment or datetime.now(timezone.utc)
    local = moment.astimezone(LOCAL_ZONE)
    if not force and local.hour < SEND_HOUR:
        return {"action": "skip", "reason": "before the 08:00 local timer firing"}
    event_id = f"daily:{local.date().isoformat()}"
    if sent_today(moment):
        if not force:
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
        if mechanically_empty(packet):
            return {"action": "skip", "reason": "all mechanical sources are empty"}
        stage = "composition"
        data = compose(packet, local.date().isoformat())
    except (startup_context.InputLimitError, product_memory.ContentError,
            RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        print(f"[daily {stage}] {type(error).__name__}: {error}", file=sys.stderr)
        reason = str(error) if isinstance(error, (startup_context.InputLimitError, RuntimeError)) else (
            "Не удалось прочитать источники; подробности — в журнале службы."
            if stage == "sources" else
            "Модель не вернула готовую оперативку; подробности — в журнале службы.")
        failure = record_failure(moment, stage, reason, code=type(error).__name__)
        # A failed composer cannot explain its own absence. Use the same daily
        # channel, but never receipt a refusal as a delivered daily edition.
        data = {"intro": "Оперативка не собрана: " + reason,
                "plans": [{"product": "Портфель", "today": "Восстановить сборку оперативки",
                           "state": "Планы не проверены", "blocker": reason}],
                "questions": [], "initiatives": []}
        try:
            packet = {"wake_health": wake_health()}
        except (startup_context.InputLimitError, OSError, ValueError):
            packet = {}
    data["plans"].extend({key: row[key] for key in ("product", "today", "state", "blocker")}
                         for row in packet.get("wake_health", []))
    plain, html = render_plain(data), render_html(data)
    subject = f"Продуктовая оперативка — {local:%d.%m}"
    delivery = thread_tick.deliver(
        "portfolio", "daily_refusal" if failure else "daily", subject, plain, moment,
        raw_message=raw_message(thread_tick.MAIL_TO, subject, plain, html),
        event_id=f"daily:{local.date().isoformat()}:refusal" if failure else event_id)
    if failure:
        return {**failure, "notice": delivery}
    if delivery.get("action") == "fail":
        return record_failure(moment, "delivery", delivery["reason"])
    return delivery


def record_failure(moment: datetime, stage: str, reason: str, *, code: str | None = None) -> dict:
    """Reuse the delivery journal for history and process state for current health."""
    failure = {"action": "fail", "at": moment.isoformat(), "stage": stage,
               "reason": reason, "code": code or stage,
               "thread": "portfolio", "kind": "daily"}
    with outbound.Ledger() as ledger:
        ledger.record(failure)
    return failure


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    result = maybe_send(force=args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("action") in {"send", "skip"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
