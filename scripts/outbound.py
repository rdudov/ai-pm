#!/usr/bin/env python3
"""Whether a letter to the user goes out at all, and merged with what.

The contour had four places that put mail in front of the user and no place
that decided whether it should. Measured on 2026-08-07 from the mirror of sent
mail (`.state/gmail/product-owner/sent/*/metadata.json`): 58 letters in the 21
hours from 6 August 06:27, of which 21 carried the literally identical subject
«Продакт: MOEX Strategy Lab» — about one an hour — 17 «Процессный контур» and
12 «Companion». The user's words for it were «меня например раздражают письма
примерно про одно и то же. Особенно если мы проговорили в чате CLI, а потом
приходит письмо „А знаешь, мы тут такое сделали за это время! …“».

Both sources of «что пользователь уже слышал» were observable from disk and
neither was read before sending. This module reads them, and it is the one
owner of the four rules that came out of that complaint:

1. **Порог отправки.** A letter goes when there is something to say: the user
   is being asked to choose, the usefulness of a product changed, or work they
   ordered finished with a document for them. «Прогон стартовал», «прогон
   закончился», «репозиторий двинулся» are not letters — they stay on the push
   and on the board, which is where `thread_tick` still puts them.
2. **Знание чата.** Before writing «мы сделали то-то» the owner's text is
   checked against what was actually said in the CLI conversations. Said
   already — not repeated.
3. **Склейка однотипного.** Sameness is content, not subject: two letters about
   the same run of the same task are one letter. A candidate about matter
   already covered inside the coalescing window is *held*, not dropped, and the
   held items ride out with the next letter that does go — or on their own once
   the oldest of them passes `HOLD_MAX_SECONDS`.
4. **Ничего не потерять.** A letter that asks the user something, or a reply to
   the user's incoming letter, is never held, deduplicated or held back by the
   threshold. The producer identifies a reply explicitly with `kind="reply"`;
   this module never guesses it from prose.

The mirror of sent mail deliberately keeps no body (`gmail_client.
record_sent_message`), so «то же самое по содержанию» cannot be answered from
it. What is answered from it is nothing; what answers it is the ledger this
module writes at the moment of sending, under `state/`, where the letters the
user was actually sent are the record and not a reconstruction.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
import product_memory  # noqa: E402
from process_map_state import tunable  # noqa: E402

HOME = Path(__file__).resolve().parent.parent
LEDGER = HOME / "state" / "outbound.json"
# One file per session of the product owner's own CLI, written as it speaks.
TRANSCRIPTS = Path("/root/.claude/projects/-opt-projects-product-owner")

# How long a letter keeps making a later letter about the same matter a repeat.
# The complaint was measured at about one letter an hour on one direction, so
# the window has to be wider than the twenty-minute tick by a lot.
COALESCE_SECONDS = tunable("PRODUCT_OWNER_COALESCE_SECONDS", 6 * 3600)
# How long held matter may wait for a letter of its own to ride out with. Past
# this the accumulated items go as one letter: coalescing is a merge, and a
# merge that never lands is a mute button.
HOLD_MAX_SECONDS = tunable("PRODUCT_OWNER_HOLD_MAX_SECONDS", 12 * 3600)
# How far back the CLI conversations are read for «это уже проговорено».
CHAT_LOOKBACK_SECONDS = tunable("PRODUCT_OWNER_CHAT_LOOKBACK_SECONDS", 12 * 3600)
# Share of a sentence's word pairs that must already be present for it to count
# as said. Word pairs rather than words: the conversations run to hundreds of
# kilobytes, and against a blob that size single Russian words match everything.
SAME_CONTENT_PERCENT = tunable("PRODUCT_OWNER_SAME_CONTENT_PERCENT", 60)
# How many past letters are kept per direction, for the repeat test and for the
# «что пользователь уже слышал» block of the wake-up prompt.
KEEP_LETTERS = tunable("PRODUCT_OWNER_KEEP_LETTERS", 20)
# How often standing idle may be put in a letter. Two statements of the user
# meet here and neither may be dropped: on 2026-08-07 «панель показывает, что в
# работе ничего нет… тогда почему ничего не делаешь?» made silence about idling
# a defect, and on the same day «раздражают письма примерно про одно и то же»
# made an hourly letter one. Idling is a standing state, not one of the three
# things a letter is for, so it keeps the push at its own twenty-minute cadence
# and the reminder at `PRODUCT_OWNER_IDLE_REMIND_SECONDS`, and the *letter*
# about it is this much rarer.
IDLE_LETTER_SECONDS = tunable("PRODUCT_OWNER_IDLE_LETTER_SECONDS", 6 * 3600)

# Kinds that do not describe proactive news. Faults carry their own rate limit
# where they are raised, and an explicit reply is the answer owed to an incoming
# user letter. None may be thresholded or coalesced into silence. `reply` is set
# by the mail-wake producer; no text heuristic in this gateway may infer it.
ALWAYS = ("alarm", "wake_failure", "reply")

# Prompts written by a machine, not typed by the user. A session that has a user
# turn matching none of these had a human in it, and only such a session counts
# as «проговорили в чате».
MACHINE_PREAMBLES = (
    "Ты продакт-агент на фоновом пробуждении",
    "Ты проснулся из-за новых писем",
    "Ты работаешь как самостоятельный продакт-владелец",
    "You are the continuous engineering owner for prepared task",
)

WORD = re.compile(r"[а-яёa-z0-9]{3,}")
TASK_ID = re.compile(r"\b(\d{3,4})\b")
SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")
# The one line the woken owner may use to name why it is writing. Two of its
# four values are acted on and they are the two that cannot make the contour
# quieter than the user asked: `вопрос` adds a letter that nothing else may
# hold, and `механика` removes one the threshold below would refuse anyway.
REASON_LINE = re.compile(r"^\s*ПОВОД\s*:\s*(вопрос|польза|готово|механика)\s*$",
                         re.IGNORECASE | re.MULTILINE)
# The structured answer to «спрашиваешь ли ты тут пользователя», separate from
# the reason above and carried by the one who composes the letter. Separate
# because `ПОВОД` is a single choice of four and its other three values were
# silently answering this question with «нет»: on 2026-08-09 a review showed
# `ПОВОД: механика` over «Пожалуйста, выберите: запускать задачу 861 сейчас или
# после ревью.» being dropped below the threshold. Only `да` is acted on. `нет`
# is recorded and deliberately powerless — see `asks_user`.
QUESTION_LINE = re.compile(r"^\s*ВОПРОС\s*:\s*(да|нет)\s*$",
                           re.IGNORECASE | re.MULTILINE)
# A request for the user's choice, read from the text and owing nothing to the
# label above. Punctuation alone was the whole of this check until 2026-08-09,
# and a normal Russian request to choose ends with a full stop: «Пожалуйста,
# выберите: …», «Нужно ваше решение по 861.» The forms here are second-person
# address and standing requests, not any occurrence of «выбрать» — a report that
# says the user can choose a strategy is not itself a question.
CHOICE_REQUEST = re.compile(
    r"выбер(?:и|ите)\b|выбира(?:й|йте)\b|подтверд(?:и|ите)\b|разреш(?:и|ите)\b"
    r"|согласу(?:й|йте)\b|скаж(?:и|ите)\b|сообщ(?:и|ите)\b|уточн(?:и|ите)\b"
    r"|ответ(?:ь|ьте)\b|реш(?:и|ите)\b|дай(?:те)?\s+знать"
    r"|(?:прошу|нужно|надо)\s+(?:вы|)брать|прошу\s+реш(?:ить|ения)"
    r"|нужен\s+ваш\s+(?:выбор|ответ)|нужно\s+ваше\s+решение"
    r"|жду\s+(?:вашего\s+)?(?:решения|ответа|выбора)"
    r"|на\s+ваше\s+усмотрение|что\s+выбрать|как\s+поступить",
    re.IGNORECASE)


def words(text: str) -> list[str]:
    return WORD.findall((text or "").lower().replace("ё", "е"))


def pairs(text: str) -> set[str]:
    """Adjacent word pairs — the unit sameness is measured in."""
    tokens = words(text)
    return {f"{a} {b}" for a, b in zip(tokens, tokens[1:])}


def task_ids(text: str) -> set[int]:
    """Task numbers named in a text.

    Three and four digits, because the catalogue passed 900 in August 2026 and
    is expected to pass 1000. Years are excluded by range: every letter here
    carries dates, and «2026» read as a task number would make two letters about
    unrelated work look like they name the same one.
    """
    found = set()
    for value in TASK_ID.findall(text or ""):
        number = int(value)
        if 100 <= number <= 9999 and not 1900 <= number <= 2100:
            found.add(number)
    return found


def overlap_percent(part: set[str], whole: set[str]) -> int:
    """How much of `part` is already inside `whole`, as a percentage."""
    if not part:
        return 0
    return round(100 * len(part & whole) / len(part))


def fingerprint(subject: str, body: str) -> dict:
    """What a letter is about, in the two signals sameness is judged on."""
    text = f"{subject}\n{body}"
    return {"tasks": sorted(task_ids(text)), "pairs": sorted(pairs(text))}


def same_matter(candidate: dict, previous: dict) -> bool:
    """Whether a candidate letter is about matter a past letter already covered.

    Content, not subject, because the subjects were identical and that was never
    the reason: «два письма про один и тот же прогон одной и той же задачи —
    одно письмо». A candidate naming a task the past letter did not name is new
    matter whatever its wording, which is what keeps a fresh task from being
    swallowed by an hour-old letter that happens to read like it.
    """
    new_tasks = set(candidate["tasks"]) - set(previous["tasks"])
    if new_tasks:
        return False
    return overlap_percent(set(candidate["pairs"]),
                           set(previous["pairs"])) >= SAME_CONTENT_PERCENT


def declared_question(body: str) -> bool | None:
    """What the composer said about this letter being a question, if anything."""
    marker = QUESTION_LINE.search(body or "")
    if marker is None:
        return None
    return marker.group(1).lower() == "да"


def asks_user(body: str) -> bool:
    """Whether this letter puts a question or a choice in front of the user.

    Decided before everything else and never overridden: coalescing has no right
    to swallow a question, so the doubtful case here is «да, это вопрос».

    Three independent sources, any one of which is enough, and none of which may
    veto another. Two are structural and come from whoever composed the letter —
    the `ВОПРОС: да` line and the older `ПОВОД: вопрос`. One is the text itself:
    a sentence that ends in a question mark, or a request for the user's choice
    whatever it ends in.

    Independence is the point, and it is why the composer's «нет» does not
    silence the text. The composer is a language model and it does mislabel: on
    2026-08-09 a review put `ПОВОД: механика` over a plain request to choose, and
    the letter was dropped. Asking that same model for a second structured field
    and then *trusting* it would move the defect rather than close it. So a
    declared «да» adds a question the text alone would have missed, a declared
    «нет» over a text that reads as a request loses nothing, and the invariant
    holds under either being wrong.
    """
    if declared_question(body):
        return True
    marker = REASON_LINE.search(body or "")
    if marker and marker.group(1).lower() == "вопрос":
        return True
    if any(line.strip().endswith("?") for line in (body or "").splitlines()):
        return True
    return CHOICE_REQUEST.search(body or "") is not None


def declared_reason(body: str) -> str | None:
    marker = REASON_LINE.search(body or "")
    return marker.group(1).lower() if marker else None


def value_marks(report: dict) -> dict:
    """A digest of what each product of this direction promises the user.

    «Пользовательские пути» is the section that says what the user can do and
    whether it works, so a change there *is* «изменилась польза» — observed in
    the product record rather than claimed in the letter.
    """
    marks = {}
    for product in report.get("products", []):
        try:
            text = product_memory.read_snapshot(product)
        except product_memory.ContentError:
            # A snapshot that cannot be read is not a snapshot whose user paths
            # did not move. Leaving the product out of the marks keeps the
            # previous digest in place, so the threshold neither fires on a
            # failed read nor silently records «польза не менялась».
            continue
        marks[product] = hashlib.sha256(
            product_memory.section_text(text, "Пользовательские пути")
            .encode("utf-8")).hexdigest()[:16]
    return marks


def marks_of(report: dict) -> dict:
    """Everything the threshold below compares between two ticks."""
    return {
        "waiting_user": sorted(item["id"] for item in report.get("waiting_user", [])),
        "undelivered": sorted(item["id"] for item in report.get("undelivered", [])),
        "value": value_marks(report),
    }


def warrant(report: dict, previous: dict | None) -> list[str]:
    """Why this is worth a letter, in the three cases the user named.

    «Письмо уходит, когда есть что сказать: нужен выбор пользователя, изменилась
    польза, или закончилась работа, которую он заказывал.» Each one is a
    difference between two observations of the same thing, so the answer comes
    from the report and the previous marks, never from the letter's prose. What
    is *not* here is the rest: a run that started, a run that finished, a
    repository that moved. Those keep going out on the push and onto the board.

    A direction with no previous marks does not warrant everything it can see.
    The first observation is a baseline, not news — and it cannot silence a
    question, because `asks_user` is decided before this function is called.
    """
    if previous is None:
        return []
    current = marks_of(report)
    reasons = []
    fresh = set(current["waiting_user"]) - set(previous.get("waiting_user", []))
    if fresh:
        reasons.append("нужен выбор пользователя: задачи "
                       + ", ".join(str(i) for i in sorted(fresh)))
    held = set(current["undelivered"]) - set(previous.get("undelivered", []))
    if held:
        reasons.append("закончилась заказанная работа и держит документ: задачи "
                       + ", ".join(str(i) for i in sorted(held)))
    before = previous.get("value", {})
    moved = [name for name, digest in current["value"].items()
             if before.get(name) not in (None, digest)]
    if moved:
        reasons.append("изменилась польза: " + ", ".join(sorted(moved))
                       + " — раздел «Пользовательские пути»")
    return reasons


def _session_text(path: Path, since: datetime) -> tuple[list[str], bool]:
    """The turns of one CLI session inside the window, and whether a human spoke."""
    said: list[str] = []
    human = False
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return [], False
    with handle:
        for line in handle:
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            kind = record.get("type")
            if kind not in ("user", "assistant") or record.get("isMeta"):
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, list):
                content = "\n".join(block.get("text", "") for block in content
                                    if isinstance(block, dict) and block.get("type") == "text")
            if not isinstance(content, str) or not content.strip():
                continue
            if kind == "user":
                if content.startswith(MACHINE_PREAMBLES) or content.lstrip().startswith("<command-name>"):
                    continue
                human = True
            stamp = record.get("timestamp")
            try:
                when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                when = None
            if when is not None and when < since:
                continue
            said.append(content)
    return said, human


def heard_in_chat(now: datetime, root: Path | None = None) -> dict:
    """What the user was actually told in conversation, inside the window.

    Only sessions a human spoke in. The background wake-ups live in the same
    directory and are the overwhelming majority of it — treating their text as
    «пользователь это слышал» would let the contour talk itself out of writing
    to a user who was never there.
    """
    root = root or TRANSCRIPTS
    since = now - timedelta(seconds=CHAT_LOOKBACK_SECONDS)
    said: list[str] = []
    sessions: list[str] = []
    try:
        files = sorted(root.glob("*.jsonl"))
    except OSError:
        files = []
    for path in files:
        try:
            if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < since:
                continue
        except OSError:
            continue
        turns, human = _session_text(path, since)
        if not human or not turns:
            continue
        sessions.append(path.stem)
        said.extend(turns)
    text = "\n".join(said)
    return {
        "sessions": sessions,
        "tasks": sorted(task_ids(text)),
        "pairs": pairs(text),
        "chars": len(text),
        "src": f"{root}/*.jsonl — реплики сессий, где говорил человек, "
               f"за последние {CHAT_LOOKBACK_SECONDS // 3600} ч",
    }


def no_chat() -> dict:
    """The conversation deliberately not read, for the paths that do not need it."""
    return {"sessions": [], "tasks": [], "pairs": set(), "chars": 0,
            "src": "разговоры не читались: этот путь не пересказывает работу"}


def already_heard(body: str, chat: dict) -> bool:
    """Whether every claim of this letter was already made in the conversation.

    Sentence by sentence, and every one of them has to be old: one genuinely new
    sentence makes the letter worth sending, and it is the letter's job — not
    this file's — to be only the difference. A letter with nothing said in it
    yet cannot be «already heard», so an empty conversation answers no.
    """
    if not chat["pairs"]:
        return False
    if not set(task_ids(body)) <= set(chat["tasks"]):
        return False
    checked = 0
    for sentence in SENTENCE.findall(body or ""):
        spoken = pairs(sentence)
        if len(spoken) < 3:
            # Too short to carry a claim: a heading or «Риск/долг: нет».
            continue
        checked += 1
        if overlap_percent(spoken, chat["pairs"]) < SAME_CONTENT_PERCENT:
            return False
    return checked > 0


class Ledger:
    """What was said to the user and what is waiting to be said.

    Held under an exclusive lock and replaced atomically: four timers wake this
    contour, staggered but not serialised, and two of them merging into the same
    letter is exactly the state this file exists to keep honest.

    The lock is taken on a file of its own that is never replaced, and that is
    the whole point of it being separate. Until 2026-08-09 the lock was taken on
    the ledger itself while the commit replaced that same pathname: a second
    writer blocked on the handle it had already opened, the first writer's
    `os.replace` left that handle pointing at an inode nobody owned any more,
    and the waiter then woke on an uncontested lock, read the state from before
    the first commit and wrote it back over it. A two-process probe put held
    matter for `process` and for `moex` through production `Ledger`; both exited
    0 and only `moex` survived. A held question is `pending` of exactly that
    shape, so the rule «ничего не потерять» was being lost by the mechanism that
    existed to keep it. One stable lock inode, the state read *after* the lock is
    held rather than through it, and a temporary named per writer.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or LEDGER)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Never replaced, so every writer queues on the same inode.
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        # The last committed state, kept because deleting the ledger otherwise
        # takes every held letter with it and leaves nothing to reconcile from.
        self.backup_path = self.path.with_name(self.path.stem + ".backup.json")
        # Append-only: a decision that only lives in the direction's state file
        # is gone at the next tick, twenty minutes later.
        self.journal_path = self.path.with_name(self.path.stem + "-journal.jsonl")
        self._handle = None
        self.data: dict = {}

    def _load(self, path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("version") == 1 else None

    def __enter__(self) -> "Ledger":
        self._handle = self.lock_path.open("a+", encoding="utf-8")
        fcntl.flock(self._handle, fcntl.LOCK_EX)
        # Read under the lock and by pathname: whatever the previous holder
        # committed is what this writer starts from.
        data = self._load(self.path)
        if data is None:
            recovered = self._load(self.backup_path)
            if recovered is not None:
                # Said out loud in the state itself, and kept there: a ledger
                # that quietly comes back from a copy is indistinguishable from
                # one that was never lost. The field means «last rebuilt from
                # the copy at», not «rebuilt just now» — it survives later
                # commits on purpose, so the fact does not expire in twenty
                # minutes the way the direction's own state file does.
                recovered["recovered_at"] = datetime.now(timezone.utc).isoformat()
                data = recovered
        self.data = data if data is not None else {"version": 1, "threads": {}}
        self.data.setdefault("threads", {})
        return self

    def _commit(self, path: Path, payload: str) -> None:
        # Unique per writer: a shared temporary name is a second race on top of
        # the first, and two writers may legitimately be here in turn.
        temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def record(self, entry: dict) -> None:
        """Append one gateway decision where the next tick cannot overwrite it."""
        try:
            line = json.dumps(entry, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return
        try:
            with self.journal_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            # The journal is evidence, not the decision. Neither an unwritable
            # file nor an unserializable field may stop a letter the user is
            # waiting for, so this path fails quiet and the letter goes on.
            pass

    def __exit__(self, *exc) -> None:
        if self._handle is None:
            return
        try:
            payload = json.dumps(self.data, ensure_ascii=False, indent=2) + "\n"
            # The ledger first and the copy second, so a death between them
            # leaves the authoritative file current and the copy one commit
            # behind — never the other way round.
            self._commit(self.path, payload)
            self._commit(self.backup_path, payload)
        finally:
            fcntl.flock(self._handle, fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None

    def thread(self, name: str) -> dict:
        entry = self.data["threads"].setdefault(
            name, {"letters": [], "pending": [], "marks": None})
        entry.setdefault("letters", [])
        entry.setdefault("pending", [])
        entry.setdefault("marks", None)
        return entry


def recent_letters(entry: dict, now: datetime) -> list[dict]:
    fresh = []
    for letter in entry["letters"]:
        try:
            when = datetime.fromisoformat(letter["at"])
        except (KeyError, TypeError, ValueError):
            continue
        if (now - when).total_seconds() <= COALESCE_SECONDS:
            fresh.append(letter)
    return fresh


def overdue_pending(entry: dict, now: datetime) -> bool:
    for item in entry["pending"]:
        try:
            when = datetime.fromisoformat(item["at"])
        except (KeyError, TypeError, ValueError):
            return True
        if (now - when).total_seconds() >= HOLD_MAX_SECONDS:
            return True
    return False


def last_of_kind(entry: dict, kind: str) -> datetime | None:
    """When a letter of this kind last went out, over the whole kept history."""
    stamps = []
    for letter in entry["letters"]:
        if letter.get("kind") != kind:
            continue
        try:
            stamps.append(datetime.fromisoformat(letter["at"]))
        except (KeyError, TypeError, ValueError):
            continue
    return max(stamps) if stamps else None


def merged_body(body: str, pending: list[dict]) -> str:
    """One letter out of the matter that accumulated since the last one."""
    if not pending:
        return body
    held = "\n\n".join(f"- {item['at'][:16].replace('T', ' ')} UTC — {item['body'].strip()}"
                       for item in pending)
    block = f"Накопилось с прошлого письма, одним сообщением:\n\n{held}"
    return f"{body.strip()}\n\n{block}" if body.strip() else block


def decide(thread: str, kind: str, subject: str, body: str, report: dict,
           now: datetime, entry: dict, chat: dict) -> dict:
    """Send this letter, hold it for the next one, or leave it to the push.

    The order is the order of the rules, and the first one is the one that may
    not be reordered: a question is decided before the threshold, before the
    conversation and before coalescing.
    """
    candidate = fingerprint(subject, body)
    pending = list(entry["pending"])

    if kind in ALWAYS:
        reason = ("ответ на входящее письмо пользователя доходит всегда"
                  if kind == "reply" else "сбой контура, не новость")
        return {"action": "send", "reason": reason, "body": body,
                "raw_body": body, "fingerprint": candidate, "flush": []}
    if asks_user(body):
        merged = merged_body(body, pending)
        return {"action": "send", "reason": "вопрос пользователю доходит всегда",
                "body": merged, "raw_body": body,
                "fingerprint": fingerprint(subject, merged), "flush": pending}

    if kind == "idle":
        last = last_of_kind(entry, "idle")
        if last is not None and (now - last).total_seconds() < IDLE_LETTER_SECONDS:
            return {"action": "drop",
                    "reason": f"о простое письмом сказано {last.isoformat()[:16]}, "
                              f"чаще раза в {IDLE_LETTER_SECONDS // 3600} ч это письмо "
                              f"не идёт — простой остаётся в пуше и на табло",
                    "body": body, "raw_body": body,
                    "fingerprint": candidate, "flush": []}
        merged = merged_body(body, pending)
        return {"action": "send", "reason": "простой при непустой очереди",
                "body": merged, "raw_body": body,
                "fingerprint": fingerprint(subject, merged), "flush": pending}

    reasons = warrant(report, entry["marks"])
    if declared_reason(body) == "механика":
        reasons = []
    if not reasons:
        if overdue_pending(entry, now):
            return {"action": "send",
                    "reason": f"склеенное ждёт дольше {HOLD_MAX_SECONDS // 3600} ч",
                    "body": merged_body("", pending), "raw_body": "",
                    "fingerprint": fingerprint("", merged_body("", pending)),
                    "flush": pending}
        return {"action": "drop", "reason":
                "порог отправки не пройден: ни выбора пользователю, ни изменения "
                "пользы, ни законченной заказанной работы — это пуш и табло",
                "body": body, "raw_body": body, "fingerprint": candidate, "flush": []}

    if already_heard(body, chat):
        return {"action": "drop",
                "reason": f"уже проговорено в чате ({len(chat['sessions'])} сессий, "
                          f"{chat['chars']} символов)",
                "body": body, "raw_body": body, "fingerprint": candidate, "flush": []}

    for letter in recent_letters(entry, now):
        if same_matter(candidate, letter["fingerprint"]):
            return {"action": "hold",
                    "reason": f"то же самое по содержанию, что письмо {letter['at'][:16]} "
                              f"«{letter['subject']}»",
                    "body": body, "raw_body": body,
                    "fingerprint": candidate, "flush": []}

    merged = merged_body(body, pending)
    return {"action": "send", "reason": "; ".join(reasons), "body": merged,
            "raw_body": body, "fingerprint": fingerprint(subject, merged),
            "flush": pending}


def already_said(entry: dict, now: datetime, limit: int = 6) -> list[dict]:
    """What the user has already been sent, for the wake-up prompt to read."""
    return [{"at": letter["at"], "subject": letter["subject"],
             "excerpt": letter.get("excerpt", "")}
            for letter in recent_letters(entry, now)[-limit:]]


def apply(entry: dict, decision: dict, subject: str, now: datetime, report: dict,
          kind: str) -> None:
    """Write down what came of the decision, so the next tick can see it."""
    if report is not None:
        # The alarm path speaks before the direction is observed at all, and a
        # baseline written from a report that does not exist would silence the
        # first real change instead of establishing it.
        entry["marks"] = marks_of(report)
    if decision["action"] == "hold":
        if not decision["body"].strip():
            # The overdue flush carries nothing of its own — its whole body is
            # the held items, which are still held because nothing was sent.
            # Holding it again would add an empty item that rides out forever.
            return
        entry["pending"].append({
            "at": now.isoformat(), "subject": subject, "kind": kind,
            "body": decision["body"], "reason": decision["reason"],
        })
        return
    if decision["action"] != "send":
        return
    entry["pending"] = [item for item in entry["pending"]
                        if item not in decision["flush"]]
    entry["letters"].append({
        "at": now.isoformat(), "subject": subject, "kind": kind,
        "excerpt": decision["body"].strip()[:400],
        "reason": decision["reason"],
        "fingerprint": {"tasks": decision["fingerprint"]["tasks"],
                        "pairs": decision["fingerprint"]["pairs"][:400]},
    })
    entry["letters"] = entry["letters"][-KEEP_LETTERS:]
