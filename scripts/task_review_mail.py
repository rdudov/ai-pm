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
from email.utils import parsedate_to_datetime
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
    number = task_number(task_dir)
    prefix = f"task-{stage}:{number}:"
    expected_kind = KINDS[stage]
    bootstrap_prefix = f"reply:task-{number}:{stage}:"
    bootstrap_owner = f"task_{number}_bootstrap_boundary"
    return [
        row
        for row in reversed(journal_rows())
        if (
            (
                str(row.get("event_id") or "").startswith(prefix)
                and row.get("kind") == expected_kind
            )
            or (
                stage == "statement"
                and str(row.get("event_id") or "").startswith(bootstrap_prefix)
                and row.get("kind") == "reply"
                and row.get("decision_owner") == bootstrap_owner
            )
        )
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


def gmail_occurred_at(source_id: str) -> str:
    """Return the authenticated Gmail Date header as a normalized UTC instant."""
    source = MAIL_STATE / "inbox" / source_id / "metadata.json"
    try:
        metadata = json.loads(source.read_text(encoding="utf-8"))
        raw_date = str(metadata.get("date") or "")
        try:
            occurred_at = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        except ValueError:
            occurred_at = parsedate_to_datetime(raw_date)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError(f"Gmail Date is missing or unreadable: {source}") from exc
    if occurred_at.tzinfo is None:
        raise ValueError(f"Gmail Date has no timezone: {source}")
    return occurred_at.astimezone(timezone.utc).isoformat()


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
        occurred_at = gmail_occurred_at(source_id)
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
        existing = matching[0]
        identity_fields = ("text", "reason") if channel == "gmail" else (
            "text", "reason", "occurred_at"
        )
        if any(
            str(existing.get(name) or "") != str(message.get(name) or "")
            for name in identity_fields
        ):
            raise ValueError("verbatim user message identity already has different content")
        if channel == "gmail" and existing.get("occurred_at") != message["occurred_at"]:
            existing["occurred_at"] = message["occurred_at"]
            path.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            product_review.load_verbatim_messages(task_dir)
        return value
    (excluded if reason else messages).append(message)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    product_review.load_verbatim_messages(task_dir)
    return value


def feedback_target_snapshot(task_dir: Path, stage: str, result: dict) -> dict:
    """Capture the task state when feedback is recorded, not the replied-to letter."""
    snapshot = {
        **product_review.current_statement_digests(task_dir),
        "candidate_states": None,
        "result_states": None,
        "verbatim_user_words_sha256": product_review.verbatim_sha256(task_dir),
    }
    if stage == "completion":
        candidates = result.get("candidate_states")
        if not isinstance(candidates, dict) or not candidates:
            raise ValueError("completion feedback has no repository set to snapshot")
        snapshot["candidate_states"] = {
            raw_path: product_review.git_candidate_state(Path(raw_path))
            for raw_path in candidates
        }
        snapshot["result_states"] = declared_result_state(
            task_dir, result, verify_expected=False
        )
    return snapshot


def declared_result_state(
    task_dir: Path, result: dict, *, verify_expected: bool = True
) -> dict[str, list[dict]]:
    """Observe declared result files and optionally verify their reviewed digests."""
    packet_name = result.get("packet")
    if not isinstance(packet_name, str) or not packet_name:
        raise ValueError("completion review has no result-file packet")
    packet_path = (task_dir / packet_name).resolve()
    if product_review.task_relative_path(task_dir, packet_path) != packet_name:
        raise ValueError("completion review packet path is not normalized")
    try:
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("completion review packet is unreadable") from exc
    exact_candidate = packet.get("exact_candidate") if isinstance(packet, dict) else None
    manifests = (
        exact_candidate.get("result_files")
        if isinstance(exact_candidate, dict)
        else None
    )
    candidates = result.get("candidate_states")
    if not isinstance(candidates, dict) or not candidates:
        raise ValueError("completion review has no repository set")
    if not isinstance(manifests, dict):
        raise ValueError("completion packet has no result-file manifest")
    if manifests.keys() != candidates.keys():
        raise ValueError(
            "completion packet result-file repositories differ from the reviewed set"
        )
    state: dict[str, list[dict]] = {}
    for raw_repository, entries in manifests.items():
        repository = Path(raw_repository)
        if not repository.is_absolute():
            raise ValueError(
                "completion packet result-file repository path is not absolute"
            )
        if not isinstance(entries, list) or not entries:
            raise ValueError("completion packet has no declared result files")
        normalized: list[dict] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("completion packet result-file entry is malformed")
            raw_path = entry.get("path")
            expected = entry.get("sha256")
            if (
                not isinstance(raw_path, str)
                or not raw_path
                or Path(raw_path).is_absolute()
                or Path(raw_path).as_posix() != raw_path
                or ".." in Path(raw_path).parts
                or raw_path in seen
                or not isinstance(expected, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected) is None
            ):
                raise ValueError("completion packet result-file entry is malformed")
            path = (repository / raw_path).resolve()
            try:
                path.relative_to(repository.resolve())
                actual = product_review.file_sha256(path)
            except (OSError, ValueError) as exc:
                if verify_expected:
                    raise ValueError(
                        "a declared completion result file is unreadable"
                    ) from exc
                actual = None
            if verify_expected and actual != expected:
                raise ValueError("a declared completion result file differs from its packet")
            seen.add(raw_path)
            normalized.append({"path": raw_path, "sha256": actual})
        normalized.sort(key=lambda item: item["path"])
        state[raw_repository] = normalized
    return state


FEEDBACK_SNAPSHOT_FIELDS = {
    "task_sha256", "contract_sha256", "candidate_states", "result_states",
    "verbatim_user_words_sha256",
}


def feedback_snapshot_is_complete(value: object) -> bool:
    """Whether a feedback snapshot has every field used by resolution."""
    return isinstance(value, dict) and FEEDBACK_SNAPSHOT_FIELDS <= value.keys()


def legacy_statement_feedback_snapshot(task_dir: Path, feedback: dict) -> dict:
    """Recover the one pre-snapshot statement record from its frozen review packet."""
    target_event = str(feedback.get("target_event_id") or "")
    match = re.fullmatch(
        rf"reply:task-{re.escape(task_number(task_dir))}:statement:([0-9a-f]{{12,64}}):[0-9a-f]+",
        target_event,
    )
    if match is None:
        raise ValueError("the recorded defect has no complete task-state snapshot")
    task_prefix = match.group(1)
    matches: list[dict] = []
    for packet_path in sorted((task_dir / "reviews").glob("statement-review-packet-*.json")):
        try:
            packet = json.loads(packet_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        subject = packet.get("subject") if isinstance(packet, dict) else None
        if (
            isinstance(subject, dict)
            and str(subject.get("sha256") or "").startswith(task_prefix)
            and "contract_sha256" in subject
        ):
            matches.append(subject)
    if len(matches) != 1:
        raise ValueError("the recorded defect has no unique frozen task-state snapshot")
    verbatim = product_review.load_verbatim_record(task_dir)
    gmail_id = str(feedback.get("gmail_id") or "")
    without_feedback = {
        **verbatim,
        "messages": [
            item for item in verbatim["messages"]
            if not (item.get("channel") == "gmail" and item.get("source_id") == gmail_id)
        ],
    }
    if "excluded_messages" in verbatim:
        without_feedback["excluded_messages"] = [
            item for item in verbatim["excluded_messages"]
            if not (item.get("channel") == "gmail" and item.get("source_id") == gmail_id)
        ]
    verbatim_digest = hashlib.sha256(
        json.dumps(
            without_feedback, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()
    return {
        "task_sha256": matches[0]["sha256"],
        "contract_sha256": matches[0]["contract_sha256"],
        "candidate_states": None,
        "result_states": None,
        "verbatim_user_words_sha256": verbatim_digest,
    }


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
    verbatim_before_append = product_review.verbatim_sha256(task_dir)
    append_verbatim_message(
        task_dir,
        channel="gmail",
        source_id=args.gmail_id,
        occurred_at=str(metadata.get("date") or "").strip(),
        text=exact_text,
    )
    verbatim_after_append = product_review.verbatim_sha256(task_dir)
    if existing is not None:
        if (
            args.stage == "statement"
            and existing.get("classification") == "defect"
            and not feedback_snapshot_is_complete(existing.get("target_review"))
            and str(existing.get("target_event_id") or "").startswith(
                f"reply:task-{task_number(task_dir)}:statement:"
            )
        ):
            existing["target_review"] = legacy_statement_feedback_snapshot(task_dir, existing)
            existing.pop("resolution", None)
            write_feedback_records(task_dir, args.stage, records)
            set_blocked(task_dir)
        return existing
    target_event = str(receipt["event_id"])
    target_review = feedback_target_snapshot(task_dir, args.stage, result)
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
    if not feedback_snapshot_is_complete(previous):
        raise ValueError("the recorded defect has no complete task-state snapshot")
    if args.stage == "statement":
        changed = any(
            previous.get(name) != result.get(name)
            for name in ("task_sha256", "contract_sha256")
        )
    else:
        previous_candidates = previous.get("candidate_states")
        current_candidates = result.get("candidate_states")
        if not isinstance(previous_candidates, dict) or not isinstance(current_candidates, dict):
            raise ValueError("the recorded defect has no complete repository snapshot")
        if previous_candidates.keys() != current_candidates.keys():
            raise ValueError("the reviewed repository set differs from the defect snapshot")
        previous_results = previous.get("result_states")
        if not isinstance(previous_results, dict):
            raise ValueError("the recorded defect has no declared result snapshot")
        current_results = declared_result_state(task_dir, result)
        if previous_results.keys() != current_results.keys():
            raise ValueError("the declared result repository set differs from the defect snapshot")
        changed = previous_results != current_results
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
