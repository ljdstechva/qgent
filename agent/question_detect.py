# -*- coding: utf-8 -*-
"""Find the question an agent left for the user at the end of its reply.

The structured ``ask_user`` tool is the reliable path: it blocks the agent
and renders a QuestionCard. Models still end turns with plain prose such as
"Should I export it as PDF or PNG?" — the turn then simply stops and the
panel gives no sign that QGent is waiting. This module turns that trailing
question into the same answerable card.

STDLIB ONLY and Qt-free so it is unit-testable outside QGIS. Deliberately
conservative: only the *last* block of the reply counts, code and tables
never do, and pleasantries ("Anything else?") are ignored. A missed question
costs nothing (the user can still type); a false card would be noise.
"""
import re

MAX_QUESTION_CHARS = 300
MAX_OPTION_CHARS = 80
_MAX_OPTIONS = 5
# A longer list is a report, not a choice; offering five of nine would hide
# the rest, so such a list yields a free-text reply card instead.
_MAX_LIST_FOR_OPTIONS = 8

_FENCE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_LIST_ITEM = re.compile(
    r"^\s*(?:[-*•]|\d{1,2}[.)]|[A-Ha-h][.)]|\(\w{1,2}\))\s+(?P<text>\S.*)$")
_HEADING = re.compile(r"^\s*#{1,6}\s")
_REPLY_HINT = re.compile(
    r"^(?:just\s+)?(?:reply|respond|answer|tell me|let me know|say|choose|"
    r"pick|select|type)\b.{0,140}$", re.IGNORECASE)
_PLEASANTRY = re.compile(
    r"^(?:is there\s+)?(?:anything else|any other (?:questions?|changes?|"
    r"requests?)|need anything else|how else can i help|what else can i|"
    r"can i help with anything else|do you have any (?:other |more )?"
    r"questions?)\b", re.IGNORECASE)
_YES_NO_START = re.compile(
    r"^(?:would|should|shall|do|does|did|can|could|may|might|will|want|"
    r"is|are|was|were|have|has|had|need|okay|ok|proceed|ready|"
    r"is it ok(?:ay)?)\b", re.IGNORECASE)
_CHOICE_WORDS = re.compile(
    r"\b(?:which|choose|select|pick|prefer|one of|option)\b", re.IGNORECASE)
_TRAILING_DECOR = " \t*_`\"'”’)]}>….!\U0001F642\U0001F600"
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[*_A-Z0-9])")


def detect_question(text):
    """Return the trailing user-directed question in ``text``, or ``None``.

    The result is ``{"question": str, "options": [str], "allow_other": True,
    "kind": "choice" | "yes_no" | "open"}``. ``options`` may be empty: the
    card then shows only a reply box.
    """
    blocks = _blocks(text)
    if not blocks:
        return None

    # One trailing instruction line ("Reply with the number.") may follow
    # the question or its options without hiding them.
    if (len(blocks) > 1 and blocks[-1]["kind"] == "para"
            and len(blocks[-1]["lines"]) == 1
            and "?" not in blocks[-1]["lines"][0]
            and _REPLY_HINT.match(_plain(blocks[-1]["lines"][0]))):
        blocks = blocks[:-1]

    last = blocks[-1]
    if last["kind"] == "list":
        if len(blocks) < 2 or blocks[-2]["kind"] != "para":
            return None
        lead = _paragraph_text(blocks[-2])
        stripped = lead.rstrip(_TRAILING_DECOR.replace(".", ""))
        if not (stripped.endswith("?") or stripped.endswith(":")):
            return None
        if "?" not in lead and not _CHOICE_WORDS.search(lead):
            return None
        question = _last_question_sentence(lead) or _last_sentence(lead)
        options = _list_options(last["items"])
        if not question or _PLEASANTRY.match(question):
            return None
        return _result(question, options, "choice" if options else "open")

    if last["kind"] != "para":
        return None
    paragraph = _paragraph_text(last)
    if not paragraph.rstrip(_TRAILING_DECOR).endswith("?"):
        return None
    question = _last_question_sentence(paragraph)
    if not question or _PLEASANTRY.match(question):
        return None

    # "Which of these should I buffer?" right after a list reuses its items.
    if (len(blocks) > 1 and blocks[-2]["kind"] == "list"
            and _CHOICE_WORDS.search(question)):
        options = _list_options(blocks[-2]["items"])
        if options:
            return _result(question, options, "choice")

    alternatives = _or_alternatives(question)
    if alternatives:
        return _result(question, alternatives, "choice")
    if _YES_NO_START.match(_strip_lead_ins(question)):
        return _result(question, ["Yes", "No"], "yes_no")
    return _result(question, [], "open")


# -- parsing ------------------------------------------------------------------
def _blocks(text):
    """Split markdown into paragraph / list / opaque blocks, code removed.

    A fenced code block becomes an opaque block, so a reply ending in code
    never reads as a question.
    """
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _FENCE.sub("\n\n\x00code\x00\n\n", text)
    blocks = []
    current = None
    for raw in text.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            if current is not None and current["kind"] == "para":
                current = None
            continue
        if line.strip() == "\x00code\x00" or line.lstrip().startswith("|") \
                or _HEADING.match(line) or line.strip() in ("---", "***"):
            current = {"kind": "opaque", "lines": [line]}
            blocks.append(current)
            current = None
            continue
        match = _LIST_ITEM.match(line)
        if match:
            if current is None or current["kind"] != "list":
                current = {"kind": "list", "items": [], "lines": []}
                blocks.append(current)
            current["items"].append(match.group("text").strip())
            current["lines"].append(line)
            continue
        if current is not None and current["kind"] == "list":
            # Indented continuation of the previous item.
            if raw[:1] in (" ", "\t") and current["items"]:
                current["items"][-1] += " " + line.strip()
                continue
            current = None
        if current is None or current["kind"] != "para":
            current = {"kind": "para", "lines": []}
            blocks.append(current)
        current["lines"].append(line.strip())
    # Blank lines inside a list leave it open; merge list blocks they split.
    merged = []
    for block in blocks:
        if (merged and block["kind"] == "list"
                and merged[-1]["kind"] == "list"):
            merged[-1]["items"].extend(block["items"])
            merged[-1]["lines"].extend(block["lines"])
        else:
            merged.append(block)
    return merged


def _paragraph_text(block):
    return _plain(" ".join(block["lines"]))


def _plain(text):
    """Drop inline markdown emphasis/code markers, keep the words."""
    text = re.sub(r"\*\*|__|`", "", str(text or ""))
    text = re.sub(r"(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)", "", text)
    text = re.sub(r"\[([^\]]+)\]\((?:[^)]+)\)", r"\1", text)
    return " ".join(text.split())


def _last_question_sentence(paragraph):
    for sentence in reversed(_SENTENCE_SPLIT.split(paragraph)):
        sentence = sentence.strip()
        if sentence.rstrip(_TRAILING_DECOR).endswith("?"):
            return _clip(sentence, MAX_QUESTION_CHARS)
    return ""


def _last_sentence(paragraph):
    parts = [part.strip() for part in _SENTENCE_SPLIT.split(paragraph)
             if part.strip()]
    return _clip(parts[-1], MAX_QUESTION_CHARS) if parts else ""


def _strip_lead_ins(question):
    """'Great — should I…' / 'Also, would you…' → the auxiliary-led clause."""
    text = question.strip(" *_\"'")
    text = re.sub(r"^(?:ok(?:ay)?|great|sure|alright|also|and|so|now|"
                  r"next|finally|lastly|one more thing)\b[\s,:;—–-]*",
                  "", text, flags=re.IGNORECASE)
    return text


def _list_options(items):
    if not 2 <= len(items) <= _MAX_LIST_FOR_OPTIONS:
        return []
    options = []
    seen = set()
    for item in items[:_MAX_OPTIONS]:
        option = _option_label(item)
        key = option.casefold()
        if option and key not in seen:
            seen.add(key)
            options.append(option)
    return options if len(options) >= 2 else []


def _option_label(item):
    """'**Esri imagery** — best for site context' → 'Esri imagery'."""
    text = _plain(item)
    bold = re.match(r"^\s*(?:\*\*|__)(.+?)(?:\*\*|__)", str(item))
    if bold and len(text) > MAX_OPTION_CHARS:
        text = _plain(bold.group(1))
    if len(text) > MAX_OPTION_CHARS:
        for separator in (" — ", " – ", " - ", ": ", " (", ", "):
            head = text.split(separator, 1)[0].strip()
            if 2 <= len(head) <= MAX_OPTION_CHARS:
                text = head
                break
    text = text.rstrip(" .;,:")
    return _clip(text, MAX_OPTION_CHARS)


def _or_alternatives(question):
    """'…as PDF or PNG?' → ['PDF', 'PNG']; '…A3, A4, or Letter?' → three.

    Only short alternatives qualify: the right-hand side fixes the word count
    and the left-hand side takes the same number of words, so a phrase the
    heuristic cannot bound cleanly is not turned into a misleading button.
    """
    body = question.rstrip(_TRAILING_DECOR).rstrip("?").strip()
    match = re.search(r"^(?P<head>.*\S)\s+or\s+(?P<tail>[^,;:]+)$", body,
                      re.IGNORECASE)
    if not match:
        return []
    tail = match.group("tail").strip()
    tail_words = tail.split()
    if not 1 <= len(tail_words) <= 3 or tail.lower() in (
            "not", "no", "something else", "otherwise", "both"):
        return []
    head = match.group("head").rstrip(",").strip()
    pieces = [piece.strip() for piece in head.split(",")]
    alternatives = []
    if len(pieces) > 1 and all(
            1 <= len(piece.split()) <= 3 for piece in pieces[1:]):
        first = pieces[0].split()[-len(pieces[1].split()):]
        alternatives = [" ".join(first)] + pieces[1:]
    else:
        head_words = head.split()
        if len(head_words) <= len(tail_words):
            return []
        alternatives = [" ".join(head_words[-len(tail_words):])]
    alternatives.append(tail)
    cleaned = []
    for alternative in alternatives:
        alternative = re.sub(r"^(?:the|a|an|to|in|as|into|with|using|by)\s+",
                             "", alternative.strip(), flags=re.IGNORECASE)
        if not alternative or len(alternative) > MAX_OPTION_CHARS:
            return []
        cleaned.append(alternative)
    lowered = [item.casefold() for item in cleaned]
    if len(set(lowered)) != len(lowered) or not 2 <= len(cleaned) <= 4:
        return []
    return cleaned


def _clip(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _result(question, options, kind):
    return {
        "question": _clip(question, MAX_QUESTION_CHARS),
        "options": [_clip(option, MAX_OPTION_CHARS) for option in options],
        "allow_other": True,
        "kind": kind,
    }
