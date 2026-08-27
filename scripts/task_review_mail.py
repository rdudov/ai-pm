#!/usr/bin/env python3
"""Mandatory task statement/completion mail through the existing Gmail door."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path

try:  # installed public engine
    from task_agent import product_review
except ImportError:  # direct repository verification before installation
    import product_review

import lesson
import product_memory
import thread_tick


HOME = Path(__file__).resolve().parents[1]
OUTBOUND_JOURNAL = HOME / "state" / "outbound-journal.jsonl"
MAIL_STATE = product_memory.tasks_repo() / ".state" / "gmail" / "product-owner"
MAIL_LEDGER = MAIL_STATE / "ledger.sqlite3"
KINDS = {"statement": "task_statement", "completion": "task_completion"}


def task_number(task_dir: Path) -> str:
    match = re.match(r"^(\d+)", task_dir.name)
    if not match:
        raise ValueError(f"task directory has no numeric identity: {task_dir}")
    return match.group(1)


def event_id(task_dir: Path, stage: str, result: dict) -> str:
    identity = {
        "stage": stage,
        "verdict": result.get("verdict"),
        "reviewed_at": result.get("reviewed_at"),
        "packet_sha256": result.get("packet_sha256"),
        "report_sha256": result.get("report_sha256"),
        "task_sha256": result.get("task_sha256"),
        "contract_sha256": result.get("contract_sha256"),
        "candidate_states": result.get("candidate_states"),
        "review_admission_id": result.get("review_admission_id"),
        "verbatim_user_words_sha256": result.get("verbatim_user_words_sha256"),
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return f"task-{stage}:{task_number(task_dir)}:{digest}"


def journal_rows() -> list[dict]:
    try:
        lines = OUTBOUND_JOURNAL.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def delivery_receipt(task_dir: Path, stage: str, result: dict) -> dict | None:
    expected = event_id(task_dir, stage, result)
    expected_kind = KINDS[stage]
    return next(
        (
            row
            for row in reversed(journal_rows())
            if row.get("event_id") == expected
            and row.get("kind") == expected_kind
            and row.get("action") == "send"
            and row.get("delivered") is True
            and isinstance(row.get("message_id"), str)
            and row["message_id"]
        ),
        None,
    )


def delivered_stage_receipts(task_dir: Path, stage: str) -> list[dict]:
    """Return every delivered review letter for this task and stage, newest first."""
    prefix = f"task-{stage}:{task_number(task_dir)}:"
    expected_kind = KINDS[stage]
    return [
        row
        for row in reversed(journal_rows())
        if str(row.get("event_id") or "").startswith(prefix)
        and row.get("kind") == expected_kind
        and row.get("action") == "send"
        and row.get("delivered") is True
        and isinstance(row.get("message_id"), str)
        and row["message_id"]
    ]


def feedback_receipt(
    task_dir: Path, stage: str, result: dict, gmail_id: str
) -> dict | None:
    """Find the exact delivered review letter that the Gmail message answers."""
    current_event = event_id(task_dir, stage, result)
    current = delivery_receipt(task_dir, stage, result)
    candidates: list[dict] = []
    if current is not None:
        candidates.append({**current, "event_id": current.get("event_id") or current_event})
    candidates.extend(
        receipt
        for receipt in delivered_stage_receipts(task_dir, stage)
        if receipt.get("event_id") != current_event
    )
    return next(
        (
            receipt
            for receipt in candidates
            if authenticated_reply(gmail_id, receipt["message_id"])
        ),
        None,
    )


def feedback_path(task_dir: Path, stage: str) -> Path:
    return task_dir / "product-review" / f"{stage}-feedback.json"


def load_feedback(task_dir: Path, stage: str) -> dict:
    try:
        value = json.loads(feedback_path(task_dir, stage).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def feedback_records(value: dict) -> list[dict]:
    records = value.get("records")
    if isinstance(records, list):
        if not all(isinstance(record, dict) for record in records):
            raise ValueError("recorded feedback entries are malformed")
        return records
    return [value] if value.get("classification") else []


def write_feedback_records(task_dir: Path, stage: str, records: list[dict]) -> None:
    path = feedback_path(task_dir, stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "records": records}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )


def defect_is_unresolved(task_dir: Path, record: dict) -> bool:
    if record.get("classification") != "defect":
        return False
    resolution = record.get("resolution")
    if not isinstance(resolution, dict):
        return True
    source_event = str(record.get("lesson_source_event") or "")
    evidence_name = resolution.get("held_out_evidence")
    if not source_event or not lesson.source_event_applied(source_event):
        return True
    if not isinstance(evidence_name, str) or Path(evidence_name).name != evidence_name:
        return True
    evidence = task_dir / "product-review" / evidence_name
    try:
        return resolution.get("held_out_sha256") != product_review.file_sha256(evidence)
    except OSError:
        return True


def unresolved_defect(task_dir: Path, stage: str) -> bool:
    return any(
        defect_is_unresolved(task_dir, record)
        for record in feedback_records(load_feedback(task_dir, stage))
    )


def check(task_dir: Path, stage: str) -> tuple[bool, str, dict | None]:
    passed, detail, result = product_review.validate_result(task_dir, stage)
    if not passed or result is None:
        return False, detail, None
    expected = event_id(task_dir, stage, result)
    if unresolved_defect(task_dir, stage):
        return False, f"authenticated feedback identified a defect in current {stage} review", None
    receipt = delivery_receipt(task_dir, stage, result)
    if receipt is None:
        return False, f"current {stage} review has no Gmail receipt through thread_tick.deliver()", None
    return True, f"current {stage} review and mandatory Gmail receipt are established", {
        "event_id": expected,
        "message_id": receipt["message_id"],
        "review": result,
    }


def send(task_dir: Path, stage: str, thread: str) -> dict:
    passed, detail, result = product_review.validate_result(
        task_dir, stage, require_passing=False
    )
    if not passed or result is None:
        raise ValueError(detail)
    report = product_review.report_path(task_dir, stage)
    number = task_number(task_dir)
    title = "постановка и независимая проверка" if stage == "statement" else "результат работы и независимая проверка"
    conclusion = str(result["conclusion_ru"]).strip()
    comparison = product_review.validate_requirement_comparison(
        product_review.verbatim_record_for_result(task_dir, stage, result),
        result,
        stage,
        require_passing=False,
    )
    comparison_lines = []
    outcome_labels = {
        "satisfied": "выполнено",
        "not_satisfied": "не выполнено",
        "not_a_requirement": "не является требованием",
        "out_of_scope": "отнесено к другой работе",
    }
    for item in comparison:
        requirement = str(item.get("requirement") or "").strip()
        observed = str(item.get("observed_result") or "").strip()
        outcome = outcome_labels.get(str(item.get("outcome") or ""), "не установлено")
        reason = str(item.get("reason") or "").strip()
        if requirement and observed:
            suffix = f" Причина: {reason}" if reason else ""
            comparison_lines.append(
                f"- {requirement} — {observed} (статус: {outcome}).{suffix}"
            )
    comparison_block = "\n".join(comparison_lines)
    verdict_labels = {
        "satisfied": "соответствует запросу пользователя",
        "not_satisfied": "не соответствует запросу пользователя",
        "not_established": "недостаточно установлено",
    }
    verdict = verdict_labels.get(str(result.get("verdict") or ""))
    if verdict is None:
        raise ValueError("product-review result has an unknown verdict")
    attachment_sentence = (
        "Полная постановка и короткое заключение находятся в одном HTML-файле. "
        if stage == "statement"
        else "Полный отчёт о результате и заключение находятся в одном HTML-файле. "
    )
    transition_sentence = (
        "После квитанции Gmail работа начнётся без ожидания подтверждения."
        if stage == "statement"
        else "После квитанции Gmail задача сможет завершиться без ожидания подтверждения."
    )
    body = (
        f"Над чем работаем\n\n- Задача {number}: {title}.\n\n"
        f"Заключение проверяющего: {verdict}.\n\n"
        f"{conclusion}\n\n"
        + ("Сверка с дословными требованиями\n\n" + comparison_block + "\n\n" if comparison_block else "")
        + attachment_sentence
        + transition_sentence
        + "\n\n"
        "От вас ничего не требуется. Если вы заметите отклонение, ответьте в этой цепочке."
    )
    return thread_tick.deliver(
        thread,
        KINDS[stage],
        f"Задача {number}: {title}",
        body,
        datetime.now(timezone.utc),
        attachments=[str(report.resolve())],
        event_id=event_id(task_dir, stage, result),
        selected_by="task_review_boundary",
    )


def authenticated_reply(gmail_id: str, sent_message_id: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", gmail_id):
        return False
    try:
        with sqlite3.connect(MAIL_LEDGER) as connection:
            row = connection.execute(
                "SELECT status FROM messages WHERE message_id = ?", (gmail_id,)
            ).fetchone()
    except sqlite3.Error:
        return False
    if row is None or row[0] not in {"claimed", "completed"}:
        return False
    try:
        incoming = json.loads(
            (MAIL_STATE / "inbox" / gmail_id / "metadata.json").read_text(encoding="utf-8")
        )
        sent = json.loads(
            (MAIL_STATE / "sent" / sent_message_id / "metadata.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return False
    rfc_id = str(sent.get("rfc_message_id") or "").strip()
    incoming_thread = str(incoming.get("thread_id") or "").strip()
    sent_thread = str(sent.get("thread_id") or "").strip()
    return bool(
        rfc_id
        and incoming_thread
        and incoming_thread == sent_thread
        and (
            str(incoming.get("in_reply_to") or "").strip() == rfc_id
            or rfc_id in str(incoming.get("references") or "").split()
        )
    )


def set_blocked(task_dir: Path) -> None:
    command = product_memory.tasks_repo() / "skills" / "task-creator" / "scripts" / "tasks_index.py"
    result = subprocess.run(
        [sys.executable, str(command), "set-status", str(task_dir), "blocked",
         "--detail", "authenticated_product_review_feedback_identified_defect"],
        cwd=product_memory.tasks_repo(),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())


def gmail_verbatim_text(source_id: str) -> str:
    """Read the exact newly authored Gmail text, excluding the quoted thread."""
    source = MAIL_STATE / "inbox" / source_id / "body.txt"
    try:
        body = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Gmail verbatim source is missing or unreadable: {source}") from exc
    quoted_reply = re.search(r"\n\n[^\n]+:\n\n>", body)
    return (body[: quoted_reply.start()] if quoted_reply else body).rstrip("\r\n")


def append_verbatim_message(
    task_dir: Path,
    *,
    channel: str,
    source_id: str,
    occurred_at: str,
    text: str,
    excluded_reason: str | None = None,
) -> dict:
    """Append one included or consciously excluded exact message idempotently."""
    channel = channel.strip()
    source_id = source_id.strip()
    if channel == "gmail":
        stored_text = gmail_verbatim_text(source_id)
        if text != stored_text:
            raise ValueError(
                "Gmail verbatim text differs from the authenticated stored reply body"
            )
    path = product_review.verbatim_path(task_dir)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        value = {"schema_version": 1, "messages": []}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"verbatim user words are unreadable: {path}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("verbatim user words must use schema_version 1")
    messages = value.get("messages")
    if not isinstance(messages, list):
        raise ValueError("verbatim user words messages must be an array")
    message = {
        "channel": channel,
        "source_id": source_id,
        "occurred_at": occurred_at.strip(),
        "text": text,
    }
    if not all(message.values()):
        raise ValueError("verbatim user message requires channel, source_id, occurred_at, and text")
    excluded = value.setdefault("excluded_messages", [])
    if not isinstance(excluded, list):
        raise ValueError("excluded verbatim messages must be an array")
    reason = str(excluded_reason or "").strip()
    if excluded_reason is not None and not reason:
        raise ValueError("an excluded verbatim message requires a reason")
    if reason:
        message["reason"] = reason
    matching = [
        item for item in [*messages, *excluded]
        if isinstance(item, dict)
        and item.get("channel") == message["channel"]
        and item.get("source_id") == message["source_id"]
    ]
    if matching:
        if matching[0] != message:
            raise ValueError("verbatim user message identity already has different content")
        return value
    (excluded if reason else messages).append(message)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    product_review.load_verbatim_messages(task_dir)
    return value


def record_feedback(args: argparse.Namespace) -> dict:
    task_dir = Path(args.task_dir).resolve()
    _passed, _detail, result = product_review.validate_result(
        task_dir, args.stage, require_passing=False
    )
    if result is None:
        # Feedback binds to the message that was actually delivered. A statement
        # edit or candidate change may already have made that historical review
        # non-current, but it must not make the authenticated reply disappear.
        result = product_review.load_result(task_dir, args.stage)
    receipt = feedback_receipt(task_dir, args.stage, result, args.gmail_id)
    if receipt is None:
        raise ValueError(
            "message is not an authenticated direct reply to a delivered task-review email"
        )
    message_dir = MAIL_STATE / "inbox" / args.gmail_id
    try:
        metadata = json.loads((message_dir / "metadata.json").read_text(encoding="utf-8"))
        exact_text = gmail_verbatim_text(args.gmail_id)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("authenticated feedback has no exact verbatim text or metadata") from exc
    if args.classification == "approval" and not product_review.is_plain_approval(exact_text):
        raise ValueError(
            "approval classification is inconsistent with the authenticated verbatim reply"
        )
    if args.classification in {"substantive", "defect"}:
        missing = [
            name for name in ("observation", "cost", "rule", "owner")
            if not str(getattr(args, name, "")).strip()
        ]
        if missing:
            raise ValueError(
                "substantive feedback requires a complete owned lesson: "
                + ", ".join(missing)
            )
    records = feedback_records(load_feedback(task_dir, args.stage))
    source_event = f"gmail:{args.gmail_id}"
    existing = next(
        (item for item in records if item.get("gmail_id") == args.gmail_id), None
    )
    if existing is not None:
        return existing
    verbatim_before_append = product_review.verbatim_sha256(task_dir)
    append_verbatim_message(
        task_dir,
        channel="gmail",
        source_id=args.gmail_id,
        occurred_at=str(metadata.get("date") or "").strip(),
        text=exact_text,
    )
    verbatim_after_append = product_review.verbatim_sha256(task_dir)
    target_event = str(receipt["event_id"])
    current_event = event_id(task_dir, args.stage, result)
    target_review = {}
    if target_event == current_event:
        target_review = {
            "task_sha256": result.get("task_sha256"),
            "contract_sha256": result.get("contract_sha256"),
            "candidate_states": result.get("candidate_states"),
            "verbatim_user_words_sha256": result.get("verbatim_user_words_sha256"),
        }
    record = {
        "schema_version": 1,
        "gmail_id": args.gmail_id,
        "stage": args.stage,
        "classification": args.classification,
        "target_event_id": target_event,
        "target_message_id": receipt["message_id"],
        "target_review": target_review,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    if args.classification == "approval":
        record["approval_verified"] = True
        record["preserves_review"] = {
            "verbatim_user_words_sha256": result.get("verbatim_user_words_sha256")
        }
        record["verbatim_text_sha256"] = hashlib.sha256(
            exact_text.encode("utf-8")
        ).hexdigest()
        record["verbatim_before_append_sha256"] = verbatim_before_append
        record["verbatim_after_append_sha256"] = verbatim_after_append
    if args.classification in {"substantive", "defect"}:
        record["lesson_source_event"] = source_event
        lesson.add(
            Namespace(
                observation=args.observation,
                cost=args.cost,
                rule=args.rule,
                owner=args.owner,
                source_event=source_event,
            )
        )
    if args.classification == "defect":
        set_blocked(task_dir)
    records.append(record)
    write_feedback_records(task_dir, args.stage, records)
    return record


def resolve_feedback(args: argparse.Namespace) -> dict:
    task_dir = Path(args.task_dir).resolve()
    records = feedback_records(load_feedback(task_dir, args.stage))
    feedback = next(
        (
            record
            for record in records
            if defect_is_unresolved(task_dir, record)
        ),
        None,
    )
    if feedback is None:
        raise ValueError("there is no recorded defect to resolve")
    source_event = str(feedback.get("lesson_source_event") or "")
    if not source_event or not lesson.source_event_applied(source_event):
        raise ValueError("the reviewer lesson has not been applied to its canonical owner")
    passed, detail, result = product_review.validate_result(task_dir, args.stage)
    if not passed or result is None:
        raise ValueError(detail)
    current_event = event_id(task_dir, args.stage, result)
    if current_event == feedback.get("target_event_id"):
        raise ValueError("the defect still points at the current review")
    previous = feedback.get("target_review")
    previous = previous if isinstance(previous, dict) else {}
    if args.stage == "statement":
        changed = any(
            previous.get(name) != result.get(name)
            for name in ("task_sha256", "contract_sha256")
        )
    else:
        changed = previous.get("candidate_states") != result.get("candidate_states")
    if not changed:
        raise ValueError("the task statement/result itself has not changed since the defect")
    receipt = delivery_receipt(task_dir, args.stage, result)
    if receipt is None:
        raise ValueError("the corrected review has no fresh Gmail receipt")
    evidence = Path(args.held_out_evidence).resolve()
    try:
        relative = evidence.relative_to((task_dir / "product-review").resolve())
    except ValueError:
        raise ValueError("held-out evidence must be inside task product-review/") from None
    if relative.name != str(relative) or not evidence.is_file():
        raise ValueError("held-out evidence must be one task-local JSON file")
    try:
        held_out = json.loads(evidence.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("held-out evidence is unreadable") from exc
    if not (
        isinstance(held_out, dict)
        and held_out.get("schema_version") == 1
        and held_out.get("target_event_id") == feedback.get("target_event_id")
        and held_out.get("caught_prior_defect") is True
        and isinstance(held_out.get("reviewer_owner_change"), str)
        and held_out["reviewer_owner_change"].strip()
    ):
        raise ValueError("held-out evidence does not prove the prior defect is now caught")
    feedback["resolution"] = {
        "resolved_by_event_id": current_event,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "held_out_evidence": relative.name,
        "held_out_sha256": product_review.file_sha256(evidence),
        "message_id": receipt["message_id"],
    }
    write_feedback_records(task_dir, args.stage, records)
    return feedback


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "send"):
        command = sub.add_parser(name)
        command.add_argument("task_dir")
        command.add_argument("--stage", required=True, choices=sorted(KINDS))
        if name == "send":
            command.add_argument("--thread", required=True)
    feedback = sub.add_parser("feedback")
    feedback.add_argument("task_dir")
    feedback.add_argument("--stage", required=True, choices=sorted(KINDS))
    feedback.add_argument("--gmail-id", required=True)
    feedback.add_argument("--classification", required=True, choices=("approval", "substantive", "defect"))
    feedback.add_argument("--observation", default="")
    feedback.add_argument("--cost", default="")
    feedback.add_argument("--rule", default="")
    feedback.add_argument("--owner", default="product reviewer")
    resolution = sub.add_parser("resolve-feedback")
    resolution.add_argument("task_dir")
    resolution.add_argument("--stage", required=True, choices=sorted(KINDS))
    resolution.add_argument("--held-out-evidence", required=True)
    verbatim = sub.add_parser("append-verbatim")
    verbatim.add_argument("task_dir")
    verbatim.add_argument("--channel", required=True)
    verbatim.add_argument("--source-id", required=True)
    verbatim.add_argument("--occurred-at", required=True)
    verbatim.add_argument("--text-file", required=True)
    verbatim.add_argument("--excluded-reason")
    args = parser.parse_args()
    task_dir = Path(args.task_dir).resolve()
    try:
        if args.command == "check":
            passed, detail, evidence = check(task_dir, args.stage)
            print(json.dumps({"passed": passed, "detail": detail, "evidence": evidence}, ensure_ascii=False))
            return 0 if passed else 1
        if args.command == "send":
            print(json.dumps(send(task_dir, args.stage, args.thread), ensure_ascii=False))
            return 0
        if args.command == "feedback":
            print(json.dumps(record_feedback(args), ensure_ascii=False))
            return 0
        if args.command == "append-verbatim":
            value = append_verbatim_message(
                task_dir,
                channel=args.channel,
                source_id=args.source_id,
                occurred_at=args.occurred_at,
                text=Path(args.text_file).resolve().read_text(encoding="utf-8"),
                excluded_reason=args.excluded_reason,
            )
            print(json.dumps(value, ensure_ascii=False))
            return 0
        print(json.dumps(resolve_feedback(args), ensure_ascii=False))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
