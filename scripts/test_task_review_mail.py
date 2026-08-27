"""Mandatory review mail reuses the existing journal, Gmail identity and lessons."""

import argparse
import base64
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

import pytest


SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
if task_agent_scripts := os.environ.get("TASK_AGENT_SCRIPTS"):
    sys.path.insert(0, task_agent_scripts)

import lesson  # noqa: E402
import task_review_mail  # noqa: E402


def write_verbatim(task: Path, text: str = "original request") -> str:
    path = task_review_mail.product_review.verbatim_path(task)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 1,
        "messages": [{
            "channel": "cli", "source_id": "original",
            "occurred_at": "2026-08-26T00:00:00+00:00", "text": text,
        }],
    }) + "\n", encoding="utf-8")
    return task_review_mail.product_review.verbatim_sha256(task)


def seed_gmail_body(root: Path, source_id: str, text: str) -> Path:
    path = root / "inbox" / source_id / "body.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_mandatory_mail_body_contains_verdict_and_reviewer_conclusion(
    tmp_path: Path,
) -> None:
    task = tmp_path / "001-example"
    report = task / "deliverables" / "statement-review.html"
    report.parent.mkdir(parents=True)
    report.write_text("<html>full statement</html>\n", encoding="utf-8")
    verbatim_digest = write_verbatim(task)
    result = {
        "verdict": "satisfied",
        "conclusion_ru": "Проверяющий нашёл все требования и не нашёл подмен.",
        "reviewed_at": "2026-08-26T10:00:00+00:00",
        "packet_sha256": "a" * 64,
        "report_sha256": "b" * 64,
        "task_sha256": "c" * 64,
        "contract_sha256": "d" * 64,
        "verbatim_user_words_sha256": verbatim_digest,
        "requirement_comparison": [{
            "source_ids": ["original"], "requirement": "Сделать точную работу",
            "observed_result": "Точная работа сделана", "outcome": "satisfied",
        }],
    }
    with mock.patch.object(
        task_review_mail.product_review,
        "validate_result",
        return_value=(True, "ok", result),
    ), mock.patch.object(
        task_review_mail.product_review, "report_path", return_value=report
    ), mock.patch.object(
        task_review_mail.thread_tick, "deliver", return_value={"delivered": True}
    ) as deliver:
        task_review_mail.send(task, "statement", "process")
    body = deliver.call_args.args[3]
    assert "Заключение проверяющего: соответствует" in body
    assert result["conclusion_ru"] in body
    assert "Сделать точную работу — Точная работа сделана" in body
    assert "статус: выполнено" in body
    assert "После квитанции Gmail работа начнётся" in body
    assert deliver.call_args.kwargs["attachments"] == [str(report.resolve())]


def test_completion_mail_names_terminal_transition_instead_of_continuing_work(
    tmp_path: Path,
) -> None:
    task = tmp_path / "001-example"
    report = task / "deliverables" / "product-review.html"
    report.parent.mkdir(parents=True)
    report.write_text("<html>full result</html>\n", encoding="utf-8")
    verbatim_digest = write_verbatim(task)
    result = {
        "verdict": "satisfied",
        "conclusion_ru": "Результат соответствует запросу.",
        "reviewed_at": "2026-08-26T10:00:00+00:00",
        "packet_sha256": "a" * 64,
        "report_sha256": "b" * 64,
        "candidate_states": {"/candidate": "sha256:" + "c" * 64},
        "verbatim_user_words_sha256": verbatim_digest,
        "requirement_comparison": [{
            "source_ids": ["original"], "requirement": "Сделать работу",
            "observed_result": "Работа сделана", "outcome": "satisfied",
        }],
    }
    with mock.patch.object(
        task_review_mail.product_review,
        "validate_result",
        return_value=(True, "ok", result),
    ), mock.patch.object(
        task_review_mail.product_review, "report_path", return_value=report
    ), mock.patch.object(
        task_review_mail.thread_tick, "deliver", return_value={"delivered": True}
    ) as deliver:
        task_review_mail.send(task, "completion", "process")
    body = deliver.call_args.args[3]
    assert "задача сможет завершиться" in body
    assert "работа продолжится" not in body


def test_non_passing_review_is_mailed_but_does_not_become_a_passing_gate(
    tmp_path: Path,
) -> None:
    task = tmp_path / "001-example"
    report = task / "deliverables" / "statement-review.html"
    report.parent.mkdir(parents=True)
    report.write_text("<html>rejected statement</html>\n", encoding="utf-8")
    verbatim_digest = write_verbatim(task)
    result = {
        "verdict": "not_satisfied",
        "conclusion_ru": "В постановке пропущено одно требование.",
        "reviewed_at": "2026-08-26T10:00:00+00:00",
        "packet_sha256": "a" * 64,
        "report_sha256": "b" * 64,
        "verbatim_user_words_sha256": verbatim_digest,
        "requirement_comparison": [{
            "source_ids": ["original"],
            "requirement": "Сделать точную работу",
            "observed_result": "Требование отсутствует",
            "outcome": "not_satisfied",
        }],
    }
    with mock.patch.object(
        task_review_mail.product_review,
        "validate_result",
        return_value=(True, "valid", result),
    ) as validate, mock.patch.object(
        task_review_mail.product_review, "report_path", return_value=report
    ), mock.patch.object(
        task_review_mail.thread_tick, "deliver", return_value={"delivered": True}
    ) as deliver:
        task_review_mail.send(task, "statement", "process")

    assert validate.call_args.kwargs == {"require_passing": False}
    assert "не соответствует запросу пользователя" in deliver.call_args.args[3]
    assert "статус: не выполнено" in deliver.call_args.args[3]


def test_completion_review_mail_then_terminal_recheck_is_reachable(
    tmp_path: Path,
) -> None:
    task = tmp_path / "001-example"
    task.mkdir()
    repository = tmp_path / "candidate"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Test"],
        check=True,
    )
    (repository / "result.txt").write_text("done\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "result.txt"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "result"], check=True)

    verbatim_digest = write_verbatim(task)
    packet = task / "completion-packet.json"
    packet.write_text("{}\n", encoding="utf-8")
    report = task_review_mail.product_review.report_path(task, "completion")
    report.parent.mkdir(parents=True)
    report.write_text("<html>complete result and verdict</html>\n", encoding="utf-8")
    admission_id = "completion-review-admission"
    admission = {
        "schema_version": 1,
        "admission_id": admission_id,
        "decision": "admitted_review",
        "review_kind": "completion",
        "classification": {"work_class": "review"},
        "pair": {
            "reviewer_runner": "claude",
            "reviewer_family": "Claude",
            "author_runner": "codex",
            "author_family": "Codex",
        },
        "access_profile": {
            "role": "reviewer",
            "sandbox_mode": "read-only",
            "target_repositories": [str(repository)],
            "grants_write": False,
        },
    }
    ledger = task / "reviews" / "admissions.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps(admission) + "\n", encoding="utf-8")
    candidate_states = {
        str(repository): task_review_mail.product_review.git_candidate_state(repository)
    }
    result = {
        "schema_version": 1,
        "stage": "completion",
        "verdict": "satisfied",
        "conclusion_ru": "Результат соответствует дословной просьбе.",
        "reviewed_at": "2026-08-26T20:00:00+00:00",
        "packet": packet.name,
        "packet_sha256": task_review_mail.product_review.file_sha256(packet),
        "report_sha256": task_review_mail.product_review.file_sha256(report),
        "review_admission_id": admission_id,
        "reviewer": {"runner": "claude", "family": "Claude"},
        "candidate_states": candidate_states,
        "verbatim_user_words_sha256": verbatim_digest,
        "requirement_comparison": [{
            "source_ids": ["original"],
            "requirement": "original request",
            "observed_result": "the exact result is installed",
            "outcome": "satisfied",
        }],
    }
    result_path = task_review_mail.product_review.result_path(task, "completion")
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result) + "\n", encoding="utf-8")
    (task / ".runner").mkdir()
    (task / ".runner" / "runner.json").write_text(
        json.dumps({
            "review_kind": "completion",
            "review_admission": admission,
            "access_grant": {"granted_directories": [str(repository)]},
        })
        + "\n",
        encoding="utf-8",
    )
    journal = tmp_path / "outbound-journal.jsonl"

    def deliver(_thread, kind, _subject, _body, _now, **kwargs):
        journal.write_text(
            json.dumps({
                "event_id": kwargs["event_id"],
                "kind": kind,
                "action": "send",
                "delivered": True,
                "message_id": "gmail-completion-receipt",
            })
            + "\n",
            encoding="utf-8",
        )
        return {"delivered": True, "message_id": "gmail-completion-receipt"}

    with mock.patch.object(task_review_mail, "OUTBOUND_JOURNAL", journal), mock.patch.object(
        task_review_mail.thread_tick, "deliver", side_effect=deliver
    ):
        task_review_mail.send(task, "completion", "process")
        (task / ".runner" / "runner.json").write_text(
            json.dumps({
                "runner": "codex",
                "workflow": "standard",
                "access_grant": {"granted_directories": [str(repository)]},
            })
            + "\n",
            encoding="utf-8",
        )
        passed, detail, evidence = task_review_mail.check(task, "completion")

    assert passed, detail
    assert evidence["message_id"] == "gmail-completion-receipt"


def test_mail_shows_consciously_out_of_scope_message_and_reason(tmp_path: Path) -> None:
    task = tmp_path / "001-example"
    report = task / "deliverables" / "statement-review.html"
    report.parent.mkdir(parents=True)
    report.write_text("<html>full statement</html>\n", encoding="utf-8")
    write_verbatim(task)
    verbatim_path = task_review_mail.product_review.verbatim_path(task)
    verbatim = json.loads(verbatim_path.read_text(encoding="utf-8"))
    verbatim["excluded_messages"] = [{
        "channel": "gmail",
        "source_id": "other-topic",
        "occurred_at": "2026-08-26T01:00:00+00:00",
        "text": "Комментарий о Telegram",
        "reason": "Относится к другому каналу.",
    }]
    verbatim_path.write_text(json.dumps(verbatim) + "\n", encoding="utf-8")
    verbatim_digest = task_review_mail.product_review.verbatim_sha256(task)
    result = {
        "verdict": "satisfied",
        "conclusion_ru": "Исключение проверено.",
        "reviewed_at": "2026-08-26T10:00:00+00:00",
        "packet_sha256": "a" * 64,
        "report_sha256": "b" * 64,
        "verbatim_user_words_sha256": verbatim_digest,
        "task_sha256": "c" * 64,
        "contract_sha256": "d" * 64,
        "requirement_comparison": [
            {
                "source_ids": ["original"],
                "requirement": "Сделать точную работу",
                "observed_result": "Точная работа сделана",
                "outcome": "satisfied",
            },
            {
                "source_ids": ["other-topic"],
                "requirement": "Проверить принадлежность сообщения",
                "observed_result": "Сообщение относится к Telegram",
                "outcome": "out_of_scope",
                "reason": "Относится к другому каналу.",
            },
        ],
    }
    with mock.patch.object(
        task_review_mail.product_review,
        "validate_result",
        return_value=(True, "ok", result),
    ), mock.patch.object(
        task_review_mail.product_review, "report_path", return_value=report
    ), mock.patch.object(
        task_review_mail.thread_tick, "deliver", return_value={"delivered": True}
    ) as deliver:
        task_review_mail.send(task, "statement", "process")
    body = deliver.call_args.args[3]
    assert "статус: отнесено к другой работе" in body
    assert "Причина: Относится к другому каналу." in body


def test_delivery_receipt_requires_the_dedicated_kind_and_event(tmp_path: Path) -> None:
    task = tmp_path / "001-example"
    task.mkdir()
    result = {"task_sha256": "a" * 64, "contract_sha256": None}
    expected = task_review_mail.event_id(task, "statement", result)
    rows = [
        {"event_id": expected, "kind": "reply", "action": "send", "delivered": True, "message_id": "wrong-kind"},
        {"event_id": expected, "kind": "task_statement", "action": "send", "delivered": True, "message_id": "accepted"},
    ]
    with mock.patch.object(task_review_mail, "journal_rows", return_value=rows):
        assert task_review_mail.delivery_receipt(task, "statement", result)["message_id"] == "accepted"


def test_feedback_binds_to_the_superseded_review_letter_it_answers(tmp_path: Path) -> None:
    task = tmp_path / "001-example"
    write_verbatim(task)
    current_result = {
        "reviewed_at": "2026-08-26T11:00:00+00:00",
        "packet_sha256": "a" * 64,
        "report_sha256": "b" * 64,
        "task_sha256": "c" * 64,
        "contract_sha256": "d" * 64,
        "verbatim_user_words_sha256": task_review_mail.product_review.verbatim_sha256(task),
    }
    current_event = task_review_mail.event_id(task, "statement", current_result)
    old_event = f"task-statement:{task_review_mail.task_number(task)}:{'e' * 24}"
    inbox = tmp_path / "mail" / "inbox" / "incoming"
    inbox.mkdir(parents=True)
    (inbox / "metadata.json").write_text(json.dumps({"date": "now"}), encoding="utf-8")
    (inbox / "body.txt").write_text("Получил, спасибо.", encoding="utf-8")
    args = argparse.Namespace(
        task_dir=str(task), stage="statement", gmail_id="incoming",
        classification="approval", observation="", cost="", rule="", owner="",
    )
    with mock.patch.object(
        task_review_mail.product_review,
        "validate_result",
        return_value=(True, "ok", current_result),
    ), mock.patch.object(
        task_review_mail,
        "delivery_receipt",
        return_value={"event_id": current_event, "message_id": "current-mail"},
    ), mock.patch.object(
        task_review_mail,
        "delivered_stage_receipts",
        return_value=[{"event_id": old_event, "message_id": "old-mail"}],
    ), mock.patch.object(
        task_review_mail,
        "authenticated_reply",
        side_effect=lambda _gmail_id, sent_id: sent_id == "old-mail",
    ), mock.patch.object(task_review_mail, "MAIL_STATE", tmp_path / "mail"):
        recorded = task_review_mail.record_feedback(args)

    assert recorded["target_event_id"] == old_event
    assert recorded["target_message_id"] == "old-mail"
    assert recorded["target_review"] == {}
    assert recorded["preserves_review"] == {
        "verbatim_user_words_sha256": current_result["verbatim_user_words_sha256"]
    }
    assert recorded["verbatim_before_append_sha256"] == current_result[
        "verbatim_user_words_sha256"
    ]
    assert recorded["verbatim_after_append_sha256"] == (
        task_review_mail.product_review.verbatim_sha256(task)
    )


def test_each_fresh_review_gets_a_new_retry_stable_mail_identity(tmp_path: Path) -> None:
    task = tmp_path / "001-example"
    task.mkdir()
    result = {
        "reviewed_at": "2026-08-26T10:00:00+00:00",
        "packet_sha256": "a" * 64,
        "report_sha256": "b" * 64,
        "task_sha256": "c" * 64,
        "contract_sha256": "d" * 64,
    }
    first = task_review_mail.event_id(task, "statement", result)
    assert task_review_mail.event_id(task, "statement", dict(result)) == first
    result["reviewed_at"] = "2026-08-26T11:00:00+00:00"
    assert task_review_mail.event_id(task, "statement", result) != first


def test_authenticated_reply_requires_claim_and_rfc_thread_identity(tmp_path: Path) -> None:
    state = tmp_path / "mail"
    ledger = state / "ledger.sqlite3"
    state.mkdir()
    with sqlite3.connect(ledger) as connection:
        connection.execute("CREATE TABLE messages (message_id TEXT PRIMARY KEY, status TEXT)")
        connection.execute("INSERT INTO messages VALUES ('incoming', 'claimed')")
    incoming = state / "inbox" / "incoming"
    sent = state / "sent" / "sent"
    incoming.mkdir(parents=True)
    sent.mkdir(parents=True)
    (incoming / "metadata.json").write_text(
        json.dumps({"thread_id": "thread-1", "in_reply_to": "<review@example>"}),
        encoding="utf-8",
    )
    (sent / "metadata.json").write_text(
        json.dumps({"thread_id": "thread-1", "rfc_message_id": "<review@example>"}),
        encoding="utf-8",
    )
    with mock.patch.object(task_review_mail, "MAIL_LEDGER", ledger), mock.patch.object(
        task_review_mail, "MAIL_STATE", state
    ):
        assert task_review_mail.authenticated_reply("incoming", "sent")
        assert not task_review_mail.authenticated_reply("quoted-or-forged", "sent")
        (incoming / "metadata.json").write_text(
            json.dumps(
                {"thread_id": "forwarded-thread", "references": "<review@example>"}
            ),
            encoding="utf-8",
        )
        assert not task_review_mail.authenticated_reply("incoming", "sent")


def test_gmail_producers_create_records_that_authenticate_across_the_real_seam(
    tmp_path: Path,
) -> None:
    gmail_client_path = (
        task_review_mail.product_memory.tasks_repo()
        / "skills" / "gmail-client" / "scripts" / "gmail_client.py"
    )
    if not gmail_client_path.is_file():
        pytest.skip("installed task system has no Gmail client producer")
    scripts = str(gmail_client_path.parent)
    sys.path.insert(0, scripts)
    try:
        spec = importlib.util.spec_from_file_location("task_review_gmail_client", gmail_client_path)
        assert spec and spec.loader
        gmail_client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gmail_client)
    finally:
        sys.path.remove(scripts)

    class Request:
        def execute(self):
            return {
                "threadId": "thread-1",
                "payload": {"headers": [
                    {"name": "Message-Id", "value": "<review@example>"},
                    {"name": "Subject", "value": "Statement review"},
                ]},
            }

    class Service:
        def users(self):
            return self

        def messages(self):
            return self

        def get(self, **_kwargs):
            return Request()

    state = tmp_path / "mail"
    sent_dir = state / "sent"
    gmail_client.record_sent_message(Service(), "sent", store_dir=sent_dir)
    reply = EmailMessage()
    reply["From"] = "user@example.test"
    reply["To"] = "owner@example.test"
    reply["Subject"] = "Re: Statement review"
    reply["Message-Id"] = "<reply@example>"
    reply["In-Reply-To"] = "<review@example>"
    reply["References"] = "<review@example>"
    reply.set_content("Получил, спасибо.")
    gmail_client.export_raw_message_artifacts(
        {
            "raw": base64.urlsafe_b64encode(reply.as_bytes()).decode("ascii"),
            "threadId": "thread-1",
        },
        state / "inbox" / "incoming",
        message_id="incoming",
    )
    ledger = state / "ledger.sqlite3"
    with sqlite3.connect(ledger) as connection:
        connection.execute("CREATE TABLE messages (message_id TEXT PRIMARY KEY, status TEXT)")
        connection.execute("INSERT INTO messages VALUES ('incoming', 'claimed')")
    with mock.patch.object(task_review_mail, "MAIL_LEDGER", ledger), mock.patch.object(
        task_review_mail, "MAIL_STATE", state
    ):
        assert task_review_mail.authenticated_reply("incoming", "sent")


def test_lesson_source_event_is_idempotent(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.json"
    archive = tmp_path / "applied.md"
    args = argparse.Namespace(
        observation="review missed the user's actor",
        cost="wrong work started",
        rule="name the actor before approval",
        owner="statement reviewer",
        source_event="gmail:message-1",
    )
    with mock.patch.object(lesson, "INBOX", inbox), mock.patch.object(lesson, "ARCHIVE", archive):
        lesson.add(args)
        lesson.add(args)
        assert len(json.loads(inbox.read_text(encoding="utf-8"))) == 1


def test_approval_cannot_overwrite_substantive_feedback_for_same_review(tmp_path: Path) -> None:
    task = tmp_path / "001-example"
    (task / "product-review").mkdir(parents=True)
    write_verbatim(task)
    result = {
        "reviewed_at": "2026-08-26T10:00:00+00:00",
        "packet_sha256": "a" * 64,
        "report_sha256": "b" * 64,
        "task_sha256": "c" * 64,
        "contract_sha256": "d" * 64,
    }
    target = task_review_mail.event_id(task, "statement", result)
    task_review_mail.feedback_path(task, "statement").write_text(
        json.dumps({"classification": "substantive", "target_event_id": target}),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        task_dir=str(task), stage="statement", gmail_id="incoming",
        classification="approval", observation="", cost="", rule="", owner="",
    )
    with mock.patch.object(
        task_review_mail.product_review, "validate_result", return_value=(True, "ok", result)
    ), mock.patch.object(
        task_review_mail, "delivery_receipt", return_value={"message_id": "sent"}
    ), mock.patch.object(
        task_review_mail, "authenticated_reply", return_value=True
    ), mock.patch.object(
        task_review_mail, "MAIL_STATE", tmp_path / "mail"
    ):
        inbox = tmp_path / "mail" / "inbox" / "incoming"
        inbox.mkdir(parents=True)
        (inbox / "metadata.json").write_text(json.dumps({"date": "now"}), encoding="utf-8")
        (inbox / "body.txt").write_text("Да, согласен.", encoding="utf-8")
        recorded = task_review_mail.record_feedback(args)

    assert recorded["classification"] == "approval"
    stored = task_review_mail.load_feedback(task, "statement")
    records = task_review_mail.feedback_records(stored)
    assert [item["classification"] for item in records] == ["substantive", "approval"]
    assert records[1]["gmail_id"] == "incoming"
    assert records[1]["recorded_at"]


@pytest.mark.parametrize("classification", ["approval", "substantive"])
def test_later_reply_on_corrected_review_cannot_erase_unresolved_defect(
    tmp_path: Path, classification: str
) -> None:
    task = tmp_path / "001-example"
    (task / "product-review").mkdir(parents=True)
    write_verbatim(task)
    old_result = {
        "reviewed_at": "2026-08-26T10:00:00+00:00",
        "packet_sha256": "a" * 64,
        "report_sha256": "b" * 64,
        "task_sha256": "c" * 64,
        "contract_sha256": "d" * 64,
    }
    corrected_result = {**old_result, "reviewed_at": "2026-08-26T11:00:00+00:00"}
    old_event = task_review_mail.event_id(task, "statement", old_result)
    task_review_mail.feedback_path(task, "statement").write_text(
        json.dumps({
            "classification": "defect",
            "gmail_id": "original-defect",
            "target_event_id": old_event,
            "lesson_source_event": "gmail:original-defect",
        }),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        task_dir=str(task),
        stage="statement",
        gmail_id="later-reply",
        classification=classification,
        observation="later observation",
        cost="later cost",
        rule="later rule",
        owner="review owner",
    )
    inbox = tmp_path / "mail" / "inbox" / "later-reply"
    inbox.mkdir(parents=True)
    (inbox / "metadata.json").write_text(json.dumps({"date": "now"}), encoding="utf-8")
    reply_text = (
        "Получил, спасибо."
        if classification == "approval"
        else "Получил; ещё один комментарий."
    )
    (inbox / "body.txt").write_text(reply_text, encoding="utf-8")
    with mock.patch.object(
        task_review_mail.product_review,
        "validate_result",
        return_value=(True, "ok", corrected_result),
    ), mock.patch.object(
        task_review_mail, "delivery_receipt", return_value={"message_id": "sent"}
    ), mock.patch.object(
        task_review_mail, "authenticated_reply", return_value=True
    ), mock.patch.object(
        task_review_mail, "MAIL_STATE", tmp_path / "mail"
    ), mock.patch.object(lesson, "add"):
        task_review_mail.record_feedback(args)

    current_event = task_review_mail.event_id(task, "statement", corrected_result)
    records = task_review_mail.feedback_records(
        task_review_mail.load_feedback(task, "statement")
    )
    assert [item["gmail_id"] for item in records] == [
        "original-defect",
        "later-reply",
    ]
    assert task_review_mail.unresolved_defect(task, "statement")


def test_feedback_is_recorded_after_statement_change_stales_the_review(
    tmp_path: Path,
) -> None:
    task = tmp_path / "001-example"
    write_verbatim(task)
    result = {
        "reviewed_at": "2026-08-26T10:00:00+00:00",
        "packet_sha256": "a" * 64,
        "report_sha256": "b" * 64,
        "task_sha256": "c" * 64,
        "contract_sha256": "d" * 64,
    }
    result_path = task_review_mail.product_review.result_path(task, "statement")
    result_path.parent.mkdir(parents=True)
    result_path.write_text(json.dumps(result) + "\n", encoding="utf-8")
    inbox = tmp_path / "mail" / "inbox" / "incoming"
    inbox.mkdir(parents=True)
    (inbox / "metadata.json").write_text(
        json.dumps({"date": "2026-08-26T12:00:00+00:00"}), encoding="utf-8"
    )
    (inbox / "body.txt").write_text("Получил, спасибо.", encoding="utf-8")
    args = argparse.Namespace(
        task_dir=str(task), stage="statement", gmail_id="incoming",
        classification="approval", observation="", cost="", rule="", owner="",
    )
    with mock.patch.object(
        task_review_mail.product_review,
        "validate_result",
        return_value=(False, "task_sha256 changed after statement review", None),
    ), mock.patch.object(
        task_review_mail, "delivery_receipt", return_value={"message_id": "sent"}
    ), mock.patch.object(
        task_review_mail, "authenticated_reply", return_value=True
    ), mock.patch.object(task_review_mail, "MAIL_STATE", tmp_path / "mail"):
        recorded = task_review_mail.record_feedback(args)

    assert recorded["gmail_id"] == "incoming"
    verbatim = json.loads(
        task_review_mail.product_review.verbatim_path(task).read_text(encoding="utf-8")
    )
    assert verbatim["messages"][-1]["text"] == "Получил, спасибо."


def test_feedback_uses_authenticated_mail_body_as_verbatim_text(tmp_path: Path) -> None:
    task = tmp_path / "001-example"
    write_verbatim(task)
    result = {
        "reviewed_at": "2026-08-26T10:00:00+00:00",
        "packet_sha256": "a" * 64,
        "report_sha256": "b" * 64,
        "task_sha256": "c" * 64,
        "contract_sha256": "d" * 64,
    }
    inbox = tmp_path / "mail" / "inbox" / "incoming"
    inbox.mkdir(parents=True)
    (inbox / "metadata.json").write_text(
        json.dumps({"date": "2026-08-26T12:00:00+00:00"}), encoding="utf-8"
    )
    (inbox / "body.txt").write_text("Точный текст из Gmail.", encoding="utf-8")
    args = argparse.Namespace(
        task_dir=str(task), stage="statement", gmail_id="incoming",
        classification="substantive", observation="точный текст", cost="цена",
        rule="правило", owner="reviewer",
    )
    with mock.patch.object(
        task_review_mail.product_review, "validate_result", return_value=(True, "ok", result)
    ), mock.patch.object(
        task_review_mail, "delivery_receipt", return_value={"message_id": "sent"}
    ), mock.patch.object(
        task_review_mail, "authenticated_reply", return_value=True
    ), mock.patch.object(task_review_mail, "MAIL_STATE", tmp_path / "mail"), mock.patch.object(
        lesson, "add"
    ):
        task_review_mail.record_feedback(args)
    verbatim = json.loads(
        task_review_mail.product_review.verbatim_path(task).read_text(encoding="utf-8")
    )
    assert verbatim["messages"][-1]["text"] == "Точный текст из Gmail."


def test_objection_cannot_be_labelled_as_approval(tmp_path: Path) -> None:
    task = tmp_path / "001-example"
    write_verbatim(task)
    result = {
        "reviewed_at": "2026-08-26T10:00:00+00:00",
        "packet_sha256": "a" * 64,
        "report_sha256": "b" * 64,
        "task_sha256": "c" * 64,
        "contract_sha256": "d" * 64,
    }
    inbox = tmp_path / "mail" / "inbox" / "incoming"
    inbox.mkdir(parents=True)
    (inbox / "metadata.json").write_text(json.dumps({"date": "now"}), encoding="utf-8")
    (inbox / "body.txt").write_text("Стоп, это не то, что я просил.", encoding="utf-8")
    args = argparse.Namespace(
        task_dir=str(task), stage="completion", gmail_id="incoming",
        classification="approval", observation="", cost="", rule="", owner="",
    )
    with mock.patch.object(
        task_review_mail.product_review, "validate_result", return_value=(True, "ok", result)
    ), mock.patch.object(
        task_review_mail, "delivery_receipt", return_value={"message_id": "sent"}
    ), mock.patch.object(
        task_review_mail, "authenticated_reply", return_value=True
    ), mock.patch.object(
        task_review_mail, "MAIL_STATE", tmp_path / "mail"
    ), pytest.raises(ValueError, match="inconsistent with the authenticated verbatim reply"):
        task_review_mail.record_feedback(args)


def test_verbatim_message_append_is_idempotent_and_rejects_identity_rewrite(
    tmp_path: Path,
) -> None:
    task = tmp_path / "001-example"
    write_verbatim(task)
    kwargs = {
        "channel": "gmail", "source_id": "incoming",
        "occurred_at": "2026-08-26T01:00:00+00:00", "text": "Новый комментарий",
    }
    mail = tmp_path / "mail"
    seed_gmail_body(mail, "incoming", kwargs["text"])
    with mock.patch.object(task_review_mail, "MAIL_STATE", mail):
        task_review_mail.append_verbatim_message(task, **kwargs)
        task_review_mail.append_verbatim_message(task, **kwargs)
        with pytest.raises(ValueError, match="authenticated stored reply body"):
            task_review_mail.append_verbatim_message(
                task, **{**kwargs, "text": "Подмена"}
            )
    value = json.loads(task_review_mail.product_review.verbatim_path(task).read_text())
    assert len(value["messages"]) == 2


def test_gmail_verbatim_keeps_exact_new_reply_and_excludes_quoted_thread(
    tmp_path: Path,
) -> None:
    mail = tmp_path / "mail"
    direct = "Первая строка.\nВторая строка — без пересказа."
    seed_gmail_body(
        mail,
        "incoming",
        direct + "\n\nвт, 26 авг. 2026 г. в 10:00, Sender <sender@example.com>:\n\n> quoted",
    )
    with mock.patch.object(task_review_mail, "MAIL_STATE", mail):
        assert task_review_mail.gmail_verbatim_text("incoming") == direct


def test_verbatim_message_can_be_consciously_excluded_with_a_reason(
    tmp_path: Path,
) -> None:
    task = tmp_path / "001-example"
    write_verbatim(task)
    kwargs = {
        "channel": "gmail",
        "source_id": "noise-feedback",
        "occurred_at": "2026-08-26T01:00:00+00:00",
        "text": "Эта жалоба относится к другому каналу",
        "excluded_reason": "Сообщение проверено и относится только к повтору в Telegram.",
    }
    mail = tmp_path / "mail"
    seed_gmail_body(mail, "noise-feedback", kwargs["text"])
    with mock.patch.object(task_review_mail, "MAIL_STATE", mail):
        task_review_mail.append_verbatim_message(task, **kwargs)
        task_review_mail.append_verbatim_message(task, **kwargs)
    value = json.loads(task_review_mail.product_review.verbatim_path(task).read_text())
    assert len(value["messages"]) == 1
    assert value["excluded_messages"] == [{
        "channel": "gmail",
        "source_id": "noise-feedback",
        "occurred_at": "2026-08-26T01:00:00+00:00",
        "text": "Эта жалоба относится к другому каналу",
        "reason": "Сообщение проверено и относится только к повтору в Telegram.",
    }]


def test_defect_resolution_requires_changed_task_lesson_held_out_and_fresh_mail(
    tmp_path: Path,
) -> None:
    task = tmp_path / "001-example"
    review_dir = task / "product-review"
    review_dir.mkdir(parents=True)
    old_result = {
        "reviewed_at": "2026-08-26T10:00:00+00:00",
        "packet_sha256": "a" * 64,
        "report_sha256": "b" * 64,
        "task_sha256": "c" * 64,
        "contract_sha256": "d" * 64,
    }
    current_result = {
        **old_result,
        "reviewed_at": "2026-08-26T11:00:00+00:00",
        "task_sha256": "e" * 64,
        "report_sha256": "f" * 64,
    }
    target = task_review_mail.event_id(task, "statement", old_result)
    task_review_mail.feedback_path(task, "statement").write_text(
        json.dumps(
            {
                "classification": "defect",
                "target_event_id": target,
                "target_review": {
                    "task_sha256": old_result["task_sha256"],
                    "contract_sha256": old_result["contract_sha256"],
                },
                "lesson_source_event": "gmail:incoming",
            }
        ),
        encoding="utf-8",
    )
    held_out = review_dir / "statement-held-out.json"
    held_out.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target_event_id": target,
                "caught_prior_defect": True,
                "reviewer_owner_change": "statement-review instruction now checks the actor",
            }
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        task_dir=str(task), stage="statement", held_out_evidence=str(held_out)
    )
    unchanged_statement = {
        **current_result,
        "task_sha256": old_result["task_sha256"],
    }
    with mock.patch.object(
        task_review_mail.product_review,
        "validate_result",
        return_value=(True, "ok", unchanged_statement),
    ), mock.patch.object(
        lesson, "source_event_applied", return_value=True
    ), pytest.raises(ValueError, match="statement/result itself has not changed"):
        task_review_mail.resolve_feedback(args)

    with mock.patch.object(
        task_review_mail.product_review,
        "validate_result",
        return_value=(True, "ok", current_result),
    ), mock.patch.object(
        task_review_mail, "delivery_receipt", return_value={"message_id": "fresh-mail"}
    ), mock.patch.object(
        lesson, "source_event_applied", return_value=True
    ):
        resolved = task_review_mail.resolve_feedback(args)
        current_event = task_review_mail.event_id(task, "statement", current_result)
        assert resolved["resolution"]["resolved_by_event_id"] == current_event
        assert not task_review_mail.unresolved_defect(task, "statement")
        later_event = task_review_mail.event_id(
            task, "statement", {**current_result, "reviewed_at": "2026-08-26T12:00:00+00:00"}
        )
        assert not task_review_mail.unresolved_defect(task, "statement")


def test_two_resolved_defects_stay_discharged_after_later_review(tmp_path: Path) -> None:
    task = tmp_path / "001-example"
    review_dir = task / "product-review"
    review_dir.mkdir(parents=True)
    records = []
    for number in (1, 2):
        evidence = review_dir / f"held-out-{number}.json"
        evidence.write_text("{}\n", encoding="utf-8")
        records.append({
            "classification": "defect",
            "lesson_source_event": f"gmail:defect-{number}",
            "resolution": {
                "resolved_by_event_id": f"corrected-{number}",
                "held_out_evidence": evidence.name,
                "held_out_sha256": task_review_mail.product_review.file_sha256(evidence),
            },
        })
    task_review_mail.write_feedback_records(task, "statement", records)

    with mock.patch.object(lesson, "source_event_applied", return_value=True):
        assert not task_review_mail.unresolved_defect(task, "statement")


def test_duplicate_defect_feedback_does_not_reblock(tmp_path: Path) -> None:
    task = tmp_path / "001-example"
    write_verbatim(task)
    result = {
        "reviewed_at": "2026-08-26T10:00:00+00:00",
        "packet_sha256": "a" * 64,
        "report_sha256": "b" * 64,
    }
    task_review_mail.write_feedback_records(task, "completion", [{
        "gmail_id": "incoming",
        "classification": "defect",
        "target_event_id": task_review_mail.event_id(task, "completion", result),
    }])
    inbox = tmp_path / "mail" / "inbox" / "incoming"
    inbox.mkdir(parents=True)
    (inbox / "metadata.json").write_text(json.dumps({"date": "now"}), encoding="utf-8")
    (inbox / "body.txt").write_text("Повтор того же замечания.", encoding="utf-8")
    args = argparse.Namespace(
        task_dir=str(task), stage="completion", gmail_id="incoming",
        classification="defect", observation="o", cost="c", rule="r", owner="x",
    )
    with mock.patch.object(
        task_review_mail.product_review, "validate_result", return_value=(True, "ok", result)
    ), mock.patch.object(
        task_review_mail, "delivery_receipt", return_value={"message_id": "sent"}
    ), mock.patch.object(
        task_review_mail, "authenticated_reply", return_value=True
    ), mock.patch.object(
        task_review_mail, "MAIL_STATE", tmp_path / "mail"
    ), mock.patch.object(task_review_mail, "set_blocked") as set_blocked:
        task_review_mail.record_feedback(args)

    set_blocked.assert_not_called()
