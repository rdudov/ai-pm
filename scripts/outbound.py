#!/usr/bin/env python3
"""Whether a letter to the user goes out at all, and merged with what.

The contour had four places that put mail in front of the user and no place
that decided whether it should. Measured on 2026-08-07 from the mirror of sent
mail (`.state/gmail/product-owner/sent/*/metadata.json`): 58 letters in the 21
hours from 6 August 06:27, of which 21 carried the literally identical subject
«Продакт: Продукт» — about one an hour — 17 «Процессный контур» and
12 «Клиент». The user said outright that letters about roughly the same
thing irritate them — especially a letter recapping what was already talked
through in the console.

Both sources of «что пользователь уже слышал» were observable from disk and
neither was read before sending. This module reads them, and it is the one
owner of the four rules that came out of that complaint:

1. **Порог отправки.** A letter goes when there is something to say: the user
   is being asked to choose, the usefulness of a product changed, or work they
   ordered finished with a document for them. «Прогон стартовал», «прогон
   закончился», «репозиторий двинулся» are not letters — they stay on the board,
   which is where `thread_tick` still puts them. Until 2026-08-23 they also went
   to the user's Telegram; that channel now carries a broken contour and nothing
   else, because the user asked for the reports to stop coming there.
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
   this module never guesses it from prose. The user narrowed this on
   2026-08-23, after the same choice was put to them twice inside an hour: «можно
   напомнить вопрос, если новое письмо содержит какие-то новые факты, но вот так
   2 раза писать про одно и то же смысла нет». So the *first* question on a
   matter still outranks everything here, and the second asking of that same
   question goes out only when the letter names a fact the first one did not —
   see `repeated_question`. Every question is compared, long or short; what
   sameness is measured in changes with the length, and `same_question` says
   both units, both numbers and what neither of them can catch. Nothing is lost
   by that: the question the user has not answered is still in their mailbox and
   on the board, and the gateway journal keeps the refusal with its number.

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
# The CLI keeps them under the home directory, in a folder named after the
# working directory with every separator turned into a dash — derived here from
# this checkout rather than written out, so the path is right in any home and any
# installation directory instead of on one server.
TRANSCRIPTS = (Path.home() / ".claude" / "projects"
               / str(HOME).replace("/", "-").replace(".", "-"))

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
# Share of a question that the already-sent question had too, for it to be the
# same question asked again. Its own knob and not `SAME_CONTENT_PERCENT`, because
# it answers a different question about a different unit — see `same_question`
# for both units, and for the numbers the default came from. One knob for both:
# 60% separates a repeat from a new question in each of them.
SAME_QUESTION_PERCENT = tunable("PRODUCT_OWNER_SAME_QUESTION_PERCENT", 60)
# From this many named things on, a repeat is measured in what the question
# names. Not a policy dial and not a floor below which nothing is compared: it is
# the switch between the two units of one measure. A question naming one task
# number matches every other question about that task at 100%, so with less than
# this the same share is counted over the words of the letter instead — see
# `same_question` for both units and the numbers each was chosen on. The pair
# this came from names eighteen and twenty-one things.
ENOUGH_NAMED = 6
# How many past letters are kept per direction for «что пользователь уже слышал»
# — the wake-up prompt block, coalescing and the idle cadence. All three ask what
# was sent *recently*, so a count is the right bound for them. It is not the
# bound for the repeat test: a question outlives this window, see `kept_letters`.
KEEP_LETTERS = tunable("PRODUCT_OWNER_KEEP_LETTERS", 20)
# How often standing idle may be put in a letter. Two statements of the user
# meet here and neither may be dropped: asked why nothing was being done
# while the board showed nothing in progress, the user made silence about idling
# a defect; on the same day, saying that near-identical letters irritate them
# made an hourly letter one. Idling is a standing state, not one of the three
# things a letter is for, so it keeps the push at its own twenty-minute cadence
# and the reminder at `PRODUCT_OWNER_IDLE_REMIND_SECONDS`, and the *letter*
# about it is this much rarer.
IDLE_LETTER_SECONDS = tunable("PRODUCT_OWNER_IDLE_LETTER_SECONDS", 6 * 3600)

# Kinds that do not describe proactive news, each with the one reason it carries.
# Faults have their own rate limit where they are raised, an explicit reply is the
# answer owed to an incoming user letter, and `instruction` is the door's own
# letter about an accepted external instruction — a path and a digest the user
# asked for twice, which no threshold about «новость ли это» may judge. None of
# them may be thresholded or coalesced into silence. `reply` is set by the
# mail-wake producer; no text heuristic in this gateway may infer it.
ALWAYS = {
    "alarm": "сбой контура, не новость",
    "wake_failure": "сбой контура, не новость",
    "reply": "ответ на входящее письмо пользователя доходит всегда",
    "daily": "ежедневная оперативка приходит по расписанию",
    "instruction": "путь к принятой инструкции внешнему исполнителю доходит всегда",
}

# Причина, с которой уходит письмо-вопрос. Названа здесь, потому что её читают
# двое: `decide` пишет её в реестр, а `was_question` узнаёт по ней вопрос в
# строке, записанной кодом старше поля `asks_user`.
QUESTION_REASON = "вопрос пользователю доходит всегда"

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
# The things a letter names: task numbers and the Latin names of the tools,
# files, fields and products it is about — `dev-pipeline`, `README`, `metadata`,
# `runtime`, `1257`. Punctuation and hyphens are not part of a name here, so
# «Cursor.» and «cursor», «runtime-правило» and «runtime» are the same thing
# named twice. See `same_question` for why this is what a repeat is measured in.
NAMED = re.compile(r"[a-zA-Z][a-zA-Z0-9]{2,}|\b\d{3,4}\b")
# Разметка, которой составитель иногда выделяет служебную строку: `**ПОВОД:
# готово**`, `**ВОПРОС:** нет`, `## НОВОЕ: …`. Письмо пишет языковая модель, и
# 23 августа 2026 два живых письма из двадцати девяти пришли с жирными маркерами
# (`live-evidence/measure_markers.py` задачи 1260). Просьба в промпте писать
# строку голой уже стоит и уже не выполняется, а от того, увидела дверь эти
# строки или нет, зависят три её решения: считать ли письмо вопросом, назван ли
# новый факт и какой у письма повод. Поэтому разметка вокруг строки
# пропускается здесь, а не остаётся ещё одной просьбой к модели.
MARKUP = r"[\s*_#]*"
# Чем письмо называет новый факт, из-за которого уже заданный вопрос уходит
# второй раз. Отдельной строкой и по своему имени — как `ПОВОД` и `ВОПРОС`:
# составитель обязан назвать новизну явно, а не спрятать её в пересказе.
NEW_FACT_LINE = re.compile(rf"^{MARKUP}НОВОЕ{MARKUP}:{MARKUP}(\S.*?){MARKUP}$",
                           re.MULTILINE)
# The one line the woken owner may use to name why it is writing. Two of its
# four values are acted on and they are the two that cannot make the contour
# quieter than the user asked: `вопрос` adds a letter that nothing else may
# hold, and `механика` removes one the threshold below would refuse anyway.
REASON_LINE = re.compile(
    rf"^{MARKUP}ПОВОД{MARKUP}:{MARKUP}(вопрос|польза|готово|механика){MARKUP}$",
    re.IGNORECASE | re.MULTILINE)
# The structured answer to «спрашиваешь ли ты тут пользователя», separate from
# the reason above and carried by the one who composes the letter. Separate
# because `ПОВОД` is a single choice of four and its other three values were
# silently answering this question with «нет»: on 2026-08-09 a review showed
# `ПОВОД: механика` over «Пожалуйста, выберите: запускать задачу 861 сейчас или
# после ревью.» being dropped below the threshold. Only `да` is acted on. `нет`
# is recorded and deliberately powerless — see `asks_user`.
QUESTION_LINE = re.compile(rf"^{MARKUP}ВОПРОС{MARKUP}:{MARKUP}(да|нет){MARKUP}$",
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


def named_things(text: str) -> set[str]:
    """Tools, files, fields and task numbers a text names, lower-cased."""
    found = set()
    for token in NAMED.findall(text or ""):
        token = token.lower()
        if token.isdigit():
            number = int(token)
            if not 100 <= number <= 9999 or 1900 <= number <= 2100:
                continue
        found.add(token)
    return found


def fingerprint(subject: str, body: str) -> dict:
    """What a letter is about, in the three signals sameness is judged on."""
    text = f"{subject}\n{body}"
    return {"tasks": sorted(task_ids(text)), "pairs": sorted(pairs(text)),
            "names": sorted(named_things(text))}


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


def spoken_words(letter: dict) -> set[str]:
    """The words of a letter, recovered from the word pairs of its fingerprint.

    The ledger keeps pairs and not words, so this is where the second unit of the
    measures below comes from. A letter long enough to have its pairs cut at 400
    gives back only part of its words, and that direction is deliberate: an
    incomplete previous letter can only lower a share and therefore only let a
    question through.
    """
    return {word for pair in (letter.get("pairs") or []) for word in pair.split()}


def named_in(letter: dict) -> set[str]:
    """Что письмо назвало: из его отпечатка, а у старой строки — из её же слов.

    Поле `names` появилось в отпечатке 2026-08-23. У строки, записанной раньше,
    оно пустое, и мера в названном давала против неё ровно 0% — то есть любой
    повтор прежнего вопроса читался как новый вопрос. Это не решение о политике,
    а разница в том, что успел записать реестр.

    Восстанавливаем из того, что записано. Пары слов у такой строки есть,
    `spoken_words` уже достаёт из них слова, а латинские имена и номера задач в
    словах остаются — `named_things` находит их там так же, как в тексте письма.
    Замер на боевой строке 08:40 (`live-evidence/measure_recovered_names.py`
    задачи 1260): пересчёт с полного текста письма даёт 18 названных, возврат из
    пар — те же 18 плюс `gpt` и `sol` из служебной первой строки. Доля повтора
    09:40 одинакова в обоих случаях, 71%; 35 неродственных живых писем того же
    дня дают от 0% до 19%, ни одного на пороге.

    Восстановление неполно там, где пары обрезаны четырьмястами: тогда часть
    названного не вернётся. Направление этой неполноты то же, что у
    `spoken_words`: доля может только упасть, то есть вопрос может только уйти.
    """
    recorded = set(letter.get("names") or [])
    if recorded:
        return recorded
    return named_things(" ".join(spoken_words(letter)))


def same_question(candidate: dict, previous: dict) -> tuple[int, str]:
    """How much of this question the already-sent question had, and in what unit.

    The reason for measuring anything here is the pair of letters the user
    complained about on 2026-08-23: Gmail `message-id` at 08:40 UTC and
    `message-id` at 09:40 UTC asked the same choice about the same three
    files in different words. Measured on those exact bytes (`live-evidence/` of
    task 1260):

        word pairs, as `same_matter` measures      18%   — reads as a new matter
        words                                      34%   — real controls 2-28%
        five-letter stems                          43%   — control letters 42%
        things named, as measured here             71%   — control letters 19-38%

    A retelling of a letter with a lot in it keeps what the letter is about and
    changes the prose around it, so the prose is what has to be left out: the
    unit there is the things named. `overlap_percent` answers «сколько из
    названного здесь уже называли», which is the asymmetric question worth
    asking — a shorter second letter about the same three files is fully
    contained in the first one and is still the same question.

    A short question names too little for that unit to mean anything: one task
    number in common is 100%, and «Запускать 1257 сейчас?» followed by «1257
    упала, чинить или откатить?» is a second question, not a repeat. Until
    2026-08-23 such a question was returned as 0% and therefore sent without any
    comparison at all — «нечем измерить» decided in favour of the question. The
    user did not ask for that exception and an independent review named it: what
    they asked is that the same question not come twice, not that a long one not
    come twice. So the same share is counted over the words of the letter when
    there is not enough named to count over. On the pairs the tests hold, with
    the letter's subject in both as the door sees it:

        слово в слово                             100%   — повтор
        пересказ теми же словами о том же выборе   75%   — повтор
        тот же выбор другими корнями               67%   — повтор
        другой вопрос о той же задаче              50%   — уходит
        неродственные живые письма                2-28%  — уходят

    What no share of words can catch is a question rewritten with nothing in
    common but the subject line. `AGENTS.md` and `docs/reference.md` say exactly
    this and no more.

    A question that names a task the sent one did not name is a new matter
    whatever the share — the same rule `same_matter` already lives by, and the
    one that keeps «Запускать 1259?» from being swallowed by «Запускать 1257
    сейчас?». It costs the real pair nothing: neither of those two letters named
    a single task number, which is half of why the user could not read them.
    """
    if set(candidate.get("tasks") or []) - set(previous.get("tasks") or []):
        return 0, "названного"
    names = named_in(candidate)
    if len(names) >= ENOUGH_NAMED:
        return overlap_percent(names, named_in(previous)), "названного"
    return overlap_percent(spoken_words(candidate), spoken_words(previous)), "сказанного"


def new_fact(body: str, previous: dict) -> str | None:
    """The new fact this letter claims, when the claim survives being checked.

    The user allowed exactly one reason to ask again: «можно напомнить вопрос,
    если новое письмо содержит какие-то новые факты». Whoever composes the letter
    is the only one who can know that, and the composer is a language model, so
    the claim is not taken on its word — the same asymmetry `asks_user` already
    lives by. It has to be *said* in a `НОВОЕ:` line, and the line itself has to
    be new: most of its words may not already be in the question that went out.
    «Напоминаю, что metadata и README всё ещё называют только Codex» is the sent
    letter with a new heading and does not pass; «прогон 1257 упал на живой
    проверке» is four words that letter never contained and does.

    Words rather than named things, because a genuinely new fact is often about
    a thing already named — the run that failed, the file that turned out
    unreadable — and the sameness of what a letter is *about* is measured a
    function earlier, not here. The share is `SAME_CONTENT_PERCENT`, the one this
    module already uses for «это уже проговорено».

    What this cannot do is tell a truthful novelty from an invented one. What it
    can do, and does, is refuse a repeat that claims no novelty at all — which is
    what both letters of 2026-08-23 were.
    """
    claim = NEW_FACT_LINE.search(body or "")
    if claim is None:
        return None
    spoken = set(words(claim.group(1)))
    if not spoken:
        return None
    if overlap_percent(spoken, spoken_words(previous)) >= SAME_CONTENT_PERCENT:
        return None
    return claim.group(1).strip()


def was_question(letter: dict) -> bool:
    """Ставило ли это уже отправленное письмо вопрос перед пользователем.

    Поле `asks_user` записывается с 2026-08-23 и решает само за себя: его считали
    с полного текста письма в момент отправки, а это самый точный источник.

    У строки, записанной кодом старше этого поля, спрашиваем то, что в ней есть.
    Реестр живёт долго — на 2026-08-24 в нём 89 строк, и 80 из них старше поля,
    включая обе строки 08:40 и 09:40, с которых началась жалоба пользователя.
    Считать их «не вопросами» значит не защитить от повтора ровно те вопросы,
    ради которых правка делалась.

    Прежних признаков два, и хватает любого. Причина `QUESTION_REASON` — её
    пишет только ветка вопроса в `decide`. Обрезанный до 400 символов `excerpt`,
    прочитанный тем же `asks_user`, которым читается свежее письмо. Замер на
    боевом реестре (`live-evidence/legacy-rows-signals.txt` задачи 1260): из 80
    старых строк причина узнаёт 28, excerpt — 28, вместе 30. Расходятся они
    четырежды, и оба раза по делу. Два письма-ответа спрашивали пользователя,
    но ушли с причиной ответа, — их узнаёт excerpt. У двух вопросов сам вопрос
    оказался ниже обрезки в 400 символов, — их узнаёт причина. Сегодняшний
    `apply` записал бы `asks_user=true` всем четырём.
    """
    if "asks_user" in letter:
        return bool(letter["asks_user"])
    if (letter.get("reason") or "").strip() == QUESTION_REASON:
        return True
    return asks_user(letter.get("excerpt") or "")


def repeated_question(entry: dict, candidate: dict,
                      body: str) -> tuple[dict, int, str] | None:
    """The question already in front of the user that this one asks again.

    Newest first, over the letters kept for this direction rather than over the
    coalescing window: «повторённый вопрос перестаёт быть вопросом и становится
    шумом» is the user's own rule about the question itself, not about an hour of
    it. Nothing bounds how far back this reaches — `kept_letters` keeps every
    question a direction ever sent, for exactly this reason.

    Only letters that were themselves questions count. A verdict about the same
    matter may not swallow a question — that half of the 2026-08-09 rule is
    unchanged, and only the second asking of the same question is new.
    """
    for letter in reversed(entry["letters"]):
        if not was_question(letter):
            continue
        previous = letter.get("fingerprint") or {}
        if not previous.get("names") and not previous.get("pairs"):
            # Nothing was written down about that letter to compare against.
            continue
        percent, unit = same_question(candidate, previous)
        if percent < SAME_QUESTION_PERCENT:
            continue
        if new_fact(body, previous):
            return None
        return letter, percent, unit
    return None


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


# ---------------------------------------------------------------------------
# Инструкция внешнему стенду: путь и sha256 — не со слов
# ---------------------------------------------------------------------------
#
# 2026-08-20 18:15 UTC письмо `message-id` сказало «передайте ему
# инструкцию редакции 3 или дайте маршрут запуска». Принятая инструкция в эту
# минуту лежала на диске под своим абсолютным путём, а её sha256 считался одной
# командой. Пользователь ответил: «пришло письмо на почту, что нужно передать
# инструкцию, но ссылки на инструкцию нет. Не надо так присылать».
#
# Правило «внешнему стенду уходит путь и sha256» к этому моменту уже стояло в
# `AGENTS.md`, и письмо всё равно ушло без него: продакт — языковая модель, и
# просьба в промпте столько же стоит, сколько просьба поставить `ВОПРОС: да` —
# см. `asks_user`, где ровно этот урок уже записан.
#
# Поэтому прозу продакта здесь не читают вовсе. Первая редакция этой правки
# искала номер задачи в тексте письма — тот самый, которым `fingerprint` уже
# отвечает на вопрос «о чём это письмо», — и на первом же живом письме
# 2026-08-20 19:05 UTC не сработала: продакт написал «внешний A100-прогон Deep
# Research готов», не назвав номера, и письмо снова ушло бы без пути. Зацепка за
# прозу — это та же просьба к модели, просто прочитанная с другой стороны.
#
# Вторая редакция дописывала путь и цифру в ближайшее письмо направления, у
# которого есть непоказанная принятая редакция. Независимое ревью показало, чем
# это плохо, двумя разными концами одной ошибки. Свойство «у направления есть
# принятая инструкция» истинно бессрочно, поэтому блок приписывался бы и к
# сводке качества полгода спустя; а событие «редакцию ещё не называли» доставалось
# первому попавшемуся письму — несвязанный ответ «сводка принята» уносил путь и
# цифру, и настоящее письмо про готовый внешний прогон уходило следом голым.
# Обратная сторона того же: нечитаемая инструкция удерживала чужое письмо,
# которое к внешнему прогону отношения не имеет.
#
# Обе половины — одна причина: у двери не было связи между готовой инструкцией и
# конкретным сообщением, и она пыталась угадать её по соседству. Здесь эта связь
# заявляется прямо — письмо о принятой инструкции дверь **пишет сама**, из
# наблюдения, и редакции, которые оно называет, приходят в `deliver` отдельным
# аргументом (`names_instructions`). Тогда порядок писем ничего не решает: чужое
# письмо не может ни унести это событие, ни быть им удержано, а сбой наблюдения
# стоит одного письма двери, а не самодостаточности сообщения о handoff.
#
# Цена — одно письмо на принятую редакцию за всю её жизнь (за всю историю
# каталога таких файлов пять). Оно самодостаточно: путь, полный sha256 и
# ни одной просьбы что-нибудь найти.
#
# Новых владельцев тут нет ни одного: состав направления отдаёт
# `process_map_state.thread_tasks`, принятость — тот же индекс задач, что и у
# доски, файл лежит в `deliverables/` задачи, письмо уходит прежней дверью
# `thread_tick.deliver`, а сказанное помнит прежний `state/outbound.json`.

# Имя, которым этот контур называет инструкцию внешнему исполнителю. Наблюдаемое
# соглашение, а не догадка: во всём каталоге задач ему отвечают ровно пять
# файлов — 953, 1098, 1162, 1216 и 1233, — и все пять действительно инструкции
# внешнему стенду. `external-model-instruction-adherence-findings.md` соседней задачи
# под него не попадает, потому что заканчивается не на имя роли.
EXTERNAL_INSTRUCTION = re.compile(r"(?:^|[-_])instruction\.md$", re.IGNORECASE)
# Статусы задачи, при которых инструкция считается принятой. Ровно один:
# `completed` в этом контуре означает пройденное независимое кросс-ревью, а
# незакрытая задача отдаёт черновик. Наблюдение на 2026-08-20: 1216 стоит
# `blocked` со своей инструкцией в `deliverables/`, и как готовая она не уйдёт.
ACCEPTED_STATUSES = frozenset({"completed"})
HANDOFF_HEADING = "Инструкция внешнему исполнителю (собрано при отправке):"
# Почему письмо самодостаточно, одной строкой в нём самом: пользователь его
# пересылает, и получатель не должен ничего доспрашивать.
HANDOFF_NOTE = ("Файл лежит по этому пути на этой машине; у внешнего исполнителя "
                "есть к ней доступ и он забирает документ сам. sha256 посчитан с "
                "байтов файла в минуту отправки этого письма — если ревью "
                "перепишет инструкцию, придёт письмо о новой редакции.")
# Сколько отданных редакций помнит направление. Не размер очереди: это список
# уже сказанного, по которому дверь отличает «пользователю этого ещё не
# называли» от «называли». За всю историю каталога таких файлов пять, так что
# предел здесь — страховка от бесконечного роста файла, а не режим работы.
KEEP_INSTRUCTIONS = tunable("PRODUCT_OWNER_KEEP_INSTRUCTIONS", 50)


def external_instructions(thread: str) -> list[dict]:
    """Текущая принятая инструкция внешнему стенду этого направления.

    Одна, самая свежая: задачи направления уже приходят от новой к старой, и
    первая принятая с инструкцией в `deliverables/` — та самая редакция, по
    которой внешний прогон идёт сейчас. Старые редакции того же пути человеку
    не нужны, а перечень из четырёх файлов — это уже не «переслать исполнителю»,
    а выбор, которого он не просил.

    sha256 считается здесь и сейчас, с байтов файла: в памяти плана лежит
    редакция, которую ревью могло переписать после того, как её туда записали.
    Файл, который виден в каталоге, но не читается, возвращается с `sha256:
    None` и не выбрасывается молча: «принятой инструкции нет» и «она есть, и мы
    не можем назвать её цифру» — разные факты, и второй решается не здесь.
    """
    # Импорт внутри функции: оба модуля тянут за собой наблюдение всей доски, а
    # этот читают все четыре направления на каждом тике. `_file_sha256` берётся
    # оттуда же, а не пишется здесь второй раз: доска считает им те же байты,
    # когда сверяет доставленное вложение с квитанцией.
    from process_map_state import REPO, _file_sha256, thread_tasks  # noqa: PLC0415
    from thread_state import load_thread  # noqa: PLC0415

    try:
        config = load_thread(thread)
    except (SystemExit, OSError, ValueError):
        # Направление, которого эта установка не знает (`alarm` умеет писать и о
        # таком). Письмо уходит как прежде.
        return []
    for task in thread_tasks(config):
        if task.get("status") not in ACCEPTED_STATUSES:
            continue
        box = REPO / str(task.get("path") or "") / "deliverables"
        try:
            entries = sorted(box.iterdir())
        except OSError:
            continue
        found = []
        for path in entries:
            if not path.is_file() or not EXTERNAL_INSTRUCTION.search(path.name):
                continue
            found.append({"task": task.get("id"), "path": str(path.resolve()),
                          "sha256": _file_sha256(path)})
        if found:
            return found
    return []


def instructions_said(entry: dict) -> set[str]:
    """Редакции, которые пользователю уже назвали путём и цифрой."""
    return {str(item.get("sha256")) for item in entry.get("instructions") or []}


def remember_instructions(entry: dict, items: list[dict], now: datetime) -> None:
    """Записать отданную редакцию туда же, где реестр держит сказанное.

    Только после успешной отправки и только про те байты, которые ушли. Это не
    новый реестр: `state/outbound.json` ровно для того и существует, чтобы
    следующий тик знал, что пользователь уже слышал, — до сих пор он знал это
    про письма, а теперь и про названную редакцию.
    """
    said = list(entry.get("instructions") or [])
    known = {str(item.get("sha256")) for item in said}
    for item in items:
        digest = str(item.get("sha256"))
        if digest in known:
            continue
        known.add(digest)
        said.append({"at": now.isoformat(), "task": item.get("task"),
                     "path": item.get("path"), "sha256": digest})
    entry["instructions"] = said[-KEEP_INSTRUCTIONS:]


def unnamed_instructions(entry: dict, items: list[dict]) -> list[dict]:
    """Те из редакций, которых пользователю ещё не называли путём и цифрой."""
    said = instructions_said(entry)
    return [item for item in items if str(item.get("sha256")) not in said]


def instruction_letter(thread: str) -> dict | None:
    """Письмо о принятой инструкции внешнему исполнителю — целиком из наблюдения.

    Дверь пишет его сама и не трогает ни одного чужого письма. Это и есть та
    связь между готовой инструкцией и конкретным сообщением, которой у прошлых
    редакций не было: сообщение здесь одно, и оно про инструкцию по построению,
    а не по догадке о соседстве или о прозе продакта.

    Отдаёт текст и редакции, которые этот текст называет; называть их в реестре
    сказанного будет `deliver`, и только если письмо действительно ушло.
    `None` — говорить нечего или сказать нечем:

    - принятой инструкции у направления нет — обычное состояние трёх контуров
      из четырёх;
    - направления или системы задач не видно — ничего не утверждаем, пишем
      в журнал службы, чего не увидели, и пробуем на следующем тике;
    - принятая инструкция видна и **не читается** — письма о ней без её цифры
      не бывает, потому что это ровно та просьба найти неназванный файл, ради
      которой всё это написано. Ничего при этом не теряется и ничего чужого не
      удерживается: следующий тик через двадцать минут посмотрит снова.

    sha256 считается в минуту сборки письма, с байтов файла: в памяти плана
    лежит редакция, которую ревью могло переписать после того, как её туда
    записали.
    """
    try:
        found = external_instructions(thread)
    except Exception as error:  # noqa: BLE001
        print(f"продакт: инструкция направления «{thread}» не наблюдается, "
              f"письма о ней в этот тик нет: {error}", file=sys.stderr)
        return None
    if not found:
        return None
    unreadable = [item["path"] for item in found if not item["sha256"]]
    if unreadable:
        print(f"продакт: принятая инструкция направления «{thread}» видна и не "
              f"читается, письмо без её sha256 не собирается: "
              f"{', '.join(unreadable)}", file=sys.stderr)
        return None
    lines = [f"- {item['task']} — {item['path']}\n  sha256: {item['sha256']}"
             for item in found]
    return {"body": "\n".join([HANDOFF_HEADING, *lines, "", HANDOFF_NOTE]),
            "names": found}


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
    matter for `process` and for `product` through production `Ledger`; both exited
    0 and only `product` survived. A held question is `pending` of exactly that
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
            name, {"letters": [], "pending": [], "marks": None, "instructions": []})
        entry.setdefault("letters", [])
        entry.setdefault("pending", [])
        entry.setdefault("marks", None)
        # Редакции внешней инструкции, уже названные пользователю. Реестр,
        # который был написан ещё до этой правки, отвечает на тот же вопрос, что
        # и раньше: что пользователю уже сказано.
        entry.setdefault("instructions", [])
        return entry


def kept_letters(letters: list[dict]) -> list[dict]:
    """The history the next tick may read: the recent letters, and every question.

    Two different memories share this one list, and until 2026-08-23 one bound
    served both. «Что пользователь уже слышал» is about the last few letters:
    the wake-up prompt block, coalescing and the idle cadence all ask about a
    recent stretch, and `KEEP_LETTERS` is their bound. `repeated_question` asks
    something with no horizon in it. The user said the same question must not
    come twice and named the one thing that lets it come again — a new fact —
    without putting any expiry beside it.

    Measured on the real pair before this changed (`live-evidence/horizon-before.txt`
    of task 1260): with the 08:40 question still in the list the 09:40 repeat is
    dropped at 71%, and with twenty letters in between it is sent, because the
    trim had removed the only copy of the question the measure compares against.
    The same journal says the process direction sent twenty letters in as little
    as 18.8 hours, so the question was forgotten inside a day.

    The cost is that question rows accumulate. That is what they are for, and it
    is small in the unit that matters: the ledger holds about 8 KB per letter,
    and the fourteen observed days carried 108 questions across five directions —
    about 65 KB a day of memory that may not be thrown away. Everything else
    still leaves the list on schedule.
    """
    if len(letters) <= KEEP_LETTERS:
        return letters
    older = [letter for letter in letters[:-KEEP_LETTERS] if was_question(letter)]
    return older + letters[-KEEP_LETTERS:]


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
        return {"action": "send", "reason": ALWAYS[kind], "body": body,
                "raw_body": body, "fingerprint": candidate, "flush": []}
    if asks_user(body):
        repeat = repeated_question(entry, candidate, body)
        if repeat is not None:
            letter, percent, unit = repeat
            return {"action": "drop",
                    "reason": f"этот вопрос уже задан письмом {letter['at'][:16]} "
                              f"«{letter['subject']}»: {percent}% {unit} в нём "
                              f"же, нового факта письмо не называет. Вопрос "
                              f"остаётся открытым в том письме и на табло",
                    "body": body, "raw_body": body,
                    "fingerprint": candidate, "flush": []}
        merged = merged_body(body, pending)
        return {"action": "send", "reason": QUESTION_REASON,
                "body": merged, "raw_body": body,
                "fingerprint": fingerprint(subject, merged), "flush": pending}

    if kind == "idle":
        last = last_of_kind(entry, "idle")
        if last is not None and (now - last).total_seconds() < IDLE_LETTER_SECONDS:
            return {"action": "drop",
                    "reason": f"о простое письмом сказано {last.isoformat()[:16]}, "
                              f"чаще раза в {IDLE_LETTER_SECONDS // 3600} ч это письмо "
                              f"не идёт — простой остаётся на табло",
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
                "пользы, ни законченной заказанной работы — это табло",
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
        # Whether this letter put a question in front of the user, written down
        # at the moment it went: the excerpt is cut to 400 characters and the
        # choice is usually below that cut, so the next tick cannot re-read it.
        "asks_user": asks_user(decision.get("raw_body") or decision["body"]),
        "fingerprint": {"tasks": decision["fingerprint"]["tasks"],
                        "pairs": decision["fingerprint"]["pairs"][:400],
                        "names": decision["fingerprint"]["names"]},
    })
    entry["letters"] = kept_letters(entry["letters"])
