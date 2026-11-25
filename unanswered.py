"""Daily email of unresolved Slack requests in case channels.

Weekdays at 4pm Central, covering everything since the previous working
day's 4pm. A case channel is a channel whose name ends in -digits
(smith-1559) and is not a leads / phone / general / intake channel.

A message is listed only when it asks a specific person for something:
a question, a task, an approval, guidance, a blocker, or a follow-up on
one of those. Status updates, completed work, FYI tags, and the sender's
own next step stay off. A later unrelated channel message does not clear
it. The request drops off when the thread shows it was answered, finished,
approved, withdrawn, or done by someone else.

conversations.history only returns top-level messages, so live message events
also record asks. That picks up a tag posted inside an older thread.
"""

import logging
import re
import threading
import time
from datetime import datetime, time as dtime, timedelta

import email_reports
import storage

log = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
    REPORT_TZ = ZoneInfo("America/Chicago")
except Exception:
    REPORT_TZ = None

_CASE_CHANNEL_RE = re.compile(r"-\d+$")
# Whole tokens only, so a client named Leadbetter still matches leadbetter-1559.
_NON_CASE_TOKENS = frozenset({
    "lead", "leads", "phone", "phones", "general", "intake",
    "random", "social", "marketing",
})

_USER_MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")
_SUBTEAM_RE = re.compile(r"<!subteam\^([A-Z0-9]+)(?:\|[^>]*)?>")
_MAYBE_ASK_RE = re.compile(r"<@([A-Z0-9]+)|<!subteam\^")

_SKIP_SUBTYPES = frozenset({
    "channel_join", "channel_leave", "channel_topic", "channel_purpose",
    "channel_name", "channel_archive", "channel_unarchive",
    "bot_add", "bot_remove", "pinned_item", "unpinned_item",
    "tombstone", "message_deleted", "reminder_add",
    "bot_message",
})

# Visibility wording. A mention in one of these clauses is not an owner.
_FYI_RE = re.compile(
    r"("
    r"\bfyi\b|"
    r"\bfor your (?:info|information|awareness|visibility|records)\b|"
    r"\bfor (?:visibility|awareness)\b|"
    r"\bjust (?:a |an )?heads?\s?up\b|"
    r"\bheads?\s?up\b|"
    r"\blooping (?:you |y'?all )?in\b|"
    r"\bkeeping you (?:posted|in the loop)\b|"
    r"\bno action (?:needed|required)\b|"
    r"\bno need to (?:respond|reply|do anything)\b|"
    r"\bno response needed\b|"
    r"\bjust so you know\b|"
    r"\bjsyk\b|"
    r"\bcc'?ing\b|"
    r"\bplease (?:note|be advised)\b|"
    r"\bpara (?:tu |su )?(?:info|informaci[oó]n|visibilidad)\b"
    r")",
    re.I,
)
# A tag that is only a thank-you or a one-word ack is not an assignment.
_ACK_WORDS = frozenset({
    "thanks", "thank", "you", "ty", "thx", "got", "it", "sounds", "good",
    "ok", "okay", "k", "kk", "perfect", "great", "noted", "awesome", "cool",
    "np", "welcome", "yep", "yup", "yeah", "yes", "no", "done", "copy",
    "that", "will", "do", "on", "gracias", "perfecto", "listo", "sale", "va",
    "de", "nada",
})

_MAX_LATENESS_SECONDS = 18 * 3600
_DIRECTORY_TTL_SECONDS = 6 * 3600
_HISTORY_PAUSE_SECONDS = 0.25
_LAST_SENT_KEY = "unanswered_report_last_sent_at"

_directory_lock = threading.Lock()
_directory_cache = {"at": 0.0, "directory": None}
_scan_lock = threading.Lock()
_scan_running = False
_off_logged = False
_unconfigured_logged_at = 0.0
_retry_not_before = 0.0


class TeamDirectory:
    def __init__(self, team_ids, bot_ids, names, groups=None):
        self.team_ids = set(team_ids)
        self.bot_ids = set(bot_ids)
        self.names = dict(names)
        self.groups = dict(groups or {})

    def name(self, uid: str) -> str:
        return self.names.get(uid) or uid


def is_case_channel(name: str) -> bool:
    """Client case channels are named like smith-1559. Leads, phone, general,
    and intake channels are not, even when a name happens to end in digits."""
    raw = (name or "").strip().lstrip("#").lower()
    if not raw or not _CASE_CHANNEL_RE.search(raw):
        return False
    tokens = re.split(r"[-_\s]+", raw)
    return not any(t in _NON_CASE_TOKENS for t in tokens)


def slack_to_plain(text: str) -> str:
    raw = text or ""

    def user(m):
        return "@" + (m.group(2) or m.group(1))

    raw = re.sub(r"<@([A-Z0-9]+)(?:\|([^>]*))?>", user, raw)
    raw = re.sub(
        r"<!subteam\^[A-Z0-9]+(?:\|([^>]*))?>",
        lambda m: "@" + (m.group(1) or "group"),
        raw,
    )
    raw = re.sub(
        r"<#([A-Z0-9]+)(?:\|([^>]*))?>",
        lambda m: "#" + (m.group(2) or "channel"),
        raw,
    )
    raw = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2", raw)
    raw = re.sub(r"<(https?://[^|>]+)>", r"\1", raw)
    raw = (raw.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
           .replace("&#39;", "'"))
    return re.sub(r"\s+", " ", raw).strip()


def may_be_ask(text: str) -> bool:
    """Cheap pre-check: a user or user-group mention is present."""
    return bool(_MAYBE_ASK_RE.search(text or ""))


def _message_body(plain: str) -> str:
    """Text with mentions, channel links, and URLs removed.

    Punctuation stays, so a tag like "@Bob?" is still a question.
    """
    body = re.sub(r"https?://\S+", " ", plain or "")
    body = re.sub(r"[@#][\w.|-]+", " ", body)
    return re.sub(r"\s+", " ", body).strip()


def _only_acknowledgement(body: str) -> bool:
    words = re.findall(r"[a-z0-9']+", (body or "").lower())
    return bool(words) and all(w in _ACK_WORDS for w in words)


# A request has to look like one. Everything else — status, completion,
# feedback, the sender's own plan — stays off the list.
_APPROVAL_RE = re.compile(
    r"\b(?:requesting approval|need(?:s|ed)? approval|please approve|"
    r"awaiting approval|approval (?:to|from|needed|required)|"
    r"can you approve|sign(?:ing)? off)\b",
    re.I,
)
_GUIDANCE_RE = re.compile(
    r"\b(?:provide guidance|your guidance|need guidance|how should|"
    r"what do you think|your thoughts|please advise|direction on|"
    r"how (?:do|should) (?:i|we) proceed)\b",
    re.I,
)
_BLOCKER_RE = re.compile(
    r"\b(?:blocked|stuck|can'?t (?:proceed|move forward)|waiting on you|"
    r"need(?:s)? you to|until you|holding us up)\b",
    re.I,
)
_YOU_REQUEST_RE = re.compile(
    r"("
    r"\b(?:can|could|would) you\b|"
    r"\b(?:please|pls|plz)\s+(?:produce|file|send|calendar|update|reopen|"
    r"draft|call|check|review|confirm|schedule|prepare|submit|notify|"
    r"forward|sign|help|take|look|start|follow|make|get|pull|reach|"
    r"contact|email|fax|mail|complete|finish|handle|assign|disengage|"
    r"approve|reassign)\b|"
    r"\bneed you to\b|"
    r"\b(?:can|could) (?:we|someone)\b|"
    r"\bpor favor\b|\bfavor de\b|"
    r"\b(?:puedes|podr[ií]as|puede)\b|"
    r"\blet me know\b|\blmk\b|"
    r"\btake a look\b|"
    r"\bwhen you (?:can|get a chance|have)\b"
    r")",
    re.I,
)
_FOLLOWUP_RE = re.compile(
    r"\b(?:follow(?:ing|ed)?\s*ups?|checking in|any update|just a reminder|"
    r"circling back|bump(?:ing)?|asap)\b",
    re.I,
)
_SENDER_OWNS_RE = re.compile(
    r"\b(?:i will|i'll|i’ll|i can|i am going to|i'm going to|let me)\b",
    re.I,
)
_VIS_LEAD_RE = re.compile(
    r"^\s*(?:fyi|cc|for (?:your )?(?:info(?:rmation)?|visibility|awareness)|"
    r"heads?\s?up|looping(?: you)? in|jsyk|just so you know)\b",
    re.I,
)
_VIS_TAIL_RE = re.compile(
    r"(?:\bfyi\b|\bcc\b|\bfor visibility\b|\bfor your info(?:rmation)?\b|"
    r"\bheads?\s?up\b)\s*$",
    re.I,
)
_TAG_RE = re.compile(
    r"<@([A-Z0-9]+)(?:\|[^>]*)?>|<!subteam\^([A-Z0-9]+)(?:\|[^>]*)?>"
)
_TOPIC_STOP = frozenset({
    "this", "that", "with", "from", "your", "have", "will", "just", "been",
    "please", "pls", "plz", "when", "what", "need", "needs", "about", "into",
    "they", "them", "then", "than", "also", "only", "some", "here", "there",
    "were", "would", "could", "should", "next", "week", "today", "tomorrow",
})


def _request_kind(plain: str) -> str | None:
    """question, action, approval, guidance, blocker, followup, or None.

    The default is None. A status update, a finished task, feedback, or
    the sender's own plan is not a request.
    """
    text = _FYI_RE.sub(" ", plain or "")
    text = re.sub(r"@[\w.'|-]+", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -–—")
    if not text:
        return None
    if _SENDER_OWNS_RE.search(text) and not _YOU_REQUEST_RE.search(text) \
            and "?" not in text and "¿" not in text:
        return None
    if _APPROVAL_RE.search(text):
        return "approval"
    if _GUIDANCE_RE.search(text):
        return "guidance"
    if _BLOCKER_RE.search(text):
        return "blocker"
    if "?" in text or "¿" in text:
        return "question"
    if _YOU_REQUEST_RE.search(text):
        return "action"
    # "follow up, for visibility" is a note. A follow-up with no visibility
    # wording is still an open request.
    if _FOLLOWUP_RE.search(text) and not _FYI_RE.search(plain or ""):
        return "followup"
    return None


def is_pointer(text: str) -> bool:
    """A tag that only points at the previous message, like '@Ana @Bob ^'."""
    plain = slack_to_plain(text or "")
    body = re.sub(r"@[\w.'|-]+", " ", plain)
    words = re.findall(r"[a-z0-9^]+", body.lower())
    if not words:
        return False
    allowed = {"^", "this", "above", "see", "here", "up"}
    return all(w in allowed for w in words) and any(
        w in {"^", "above", "up"} for w in words)


def is_action_ask(text: str, context_text: str = "") -> bool:
    """True when the message is a request someone still has to handle.

    A caret tag counts only when the message it points at is a request.
    """
    if is_pointer(text):
        return _request_kind(slack_to_plain(context_text or "")) is not None
    return _request_kind(slack_to_plain(text or "")) is not None


def is_human_message(message: dict) -> bool:
    """A person wrote this. Apps and bots do not need a response, and a
    post from one is not an ask."""
    if not message:
        return False
    if (message.get("bot_id") or message.get("bot_profile")
            or message.get("app_id")):
        return False
    subtype = message.get("subtype")
    if subtype in _SKIP_SUBTYPES:
        return False
    return bool(message.get("user"))


def _sentence_tail(fragment: str) -> str:
    """Text after the last sentence break. Empty when the mention starts fresh."""
    parts = re.split(r"[.!?\n¿]", fragment or "")
    return parts[-1] if parts else ""


def _adjacent(fragment: str) -> bool:
    """True when nothing but a joiner sits between two mentions."""
    plain = slack_to_plain(fragment or "")
    plain = re.sub(r"\b(?:and|or)\b", " ", plain, flags=re.I)
    return not re.sub(r"[\s,;&/]+", "", plain)


def _visibility_mention(before: str, after: str) -> bool:
    head = slack_to_plain(_sentence_tail(before)).strip()
    tail = slack_to_plain(after).strip()
    if _VIS_LEAD_RE.search(tail) or _VIS_TAIL_RE.search(head):
        return True
    if _FYI_RE.search(tail) and _request_kind(tail) is None:
        return True
    return False


def _ids_for_tag(match, directory: TeamDirectory) -> list:
    user_id, group_id = match.group(1), match.group(2)
    if user_id:
        return [user_id]
    return list(directory.groups.get(group_id) or [])


def request_owners(text: str, author_id: str, directory: TeamDirectory,
                   context_text: str = "") -> list:
    """People the request is directed at.

    A mention is an owner only when a question, task, approval, guidance,
    blocker, or follow-up is aimed at that person. FYI and cc mentions are
    left off. A caret tag owns the previous message when that message is
    itself a request.
    """
    raw = text or ""
    if is_pointer(raw):
        if _request_kind(slack_to_plain(context_text or "")) is None:
            return []
        spans = list(_TAG_RE.finditer(raw))
        return _filter_people(
            [uid for span in spans for uid in _ids_for_tag(span, directory)],
            author_id, directory,
        )

    spans = list(_TAG_RE.finditer(raw))
    if not spans:
        return []
    owners = []
    for i, span in enumerate(spans):
        before = raw[(spans[i - 1].end() if i else 0):span.start()]
        after = raw[span.end():(spans[i + 1].start() if i + 1 < len(spans) else len(raw))]
        if _visibility_mention(before, after):
            continue
        head = slack_to_plain(_sentence_tail(before))
        tail = slack_to_plain(re.split(r"[.!?\n]", after, maxsplit=1)[0])
        owns = _request_kind(tail) is not None or _request_kind(head) is not None
        if not owns:
            j = i
            while j > 0 and _adjacent(raw[spans[j - 1].end():spans[j].start()]):
                j -= 1
            cluster_head = slack_to_plain(_sentence_tail(
                raw[(spans[j - 1].end() if j else 0):spans[j].start()]))
            k = i
            while k + 1 < len(spans) and _adjacent(
                    raw[spans[k].end():spans[k + 1].start()]):
                k += 1
            tail_end = spans[k + 1].start() if k + 1 < len(spans) else len(raw)
            cluster_tail = slack_to_plain(re.split(
                r"[.!?\n]", raw[spans[k].end():tail_end], maxsplit=1)[0])
            owns = (_request_kind(cluster_head) is not None
                    or _request_kind(cluster_tail) is not None)
        if not owns:
            continue
        for uid in _ids_for_tag(span, directory):
            if uid not in owners:
                owners.append(uid)
    return _filter_people(owners, author_id, directory)


def _filter_people(ids: list, author_id: str, directory: TeamDirectory) -> list:
    seen = []
    for uid in ids:
        if not uid or uid == author_id or uid in directory.bot_ids:
            continue
        if uid not in directory.team_ids:
            continue
        if uid not in seen:
            seen.append(uid)
    return seen


def target_user_ids(text: str, author_id: str, directory: TeamDirectory,
                    context_text: str = "") -> list:
    """Teammates who own the request, excluding the author and bots."""
    return request_owners(text, author_id, directory, context_text)


def excerpt(text: str, limit: int = 180) -> str:
    plain = slack_to_plain(text)
    if len(plain) <= limit:
        return plain
    return plain[: limit - 1].rstrip() + "…"


def personalized_excerpt(text: str, directory: TeamDirectory, limit: int = 180) -> str:
    def repl(match):
        return "@" + directory.name(match.group(1))

    return excerpt(_USER_MENTION_RE.sub(repl, text or ""), limit)


def parse_clock(raw: str, default: tuple = (16, 0)) -> tuple:
    """'16:00' / '4:00 PM' / '4pm' → (hour, minute). Default 4:00 PM."""
    s = (raw or "").strip().lower().replace(".", "")
    if not s:
        return default
    m = re.match(r"^(\d{1,2})\s*:?\s*(\d{2})?\s*(am|pm)?$", s)
    if not m:
        return default
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    meridiem = m.group(3)
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return default
    return hour, minute


def due_slot(now: datetime, hour: int, minute: int, last_sent_ts: float):
    """('send', slot) when a weekday 4pm report is due, ('skip', slot) when
    that slot is too old to send late, or None when nothing should happen.

    Catch-up is allowed for 18 hours, including Saturday morning for a missed
    Friday send. A new slot is never created on a weekend.
    """
    if now.tzinfo is None:
        return None
    slot = None
    for back in range(0, 4):
        day = (now - timedelta(days=back)).date()
        if day.weekday() >= 5:
            continue
        cand = datetime.combine(day, dtime(hour, minute), tzinfo=now.tzinfo)
        if cand <= now:
            slot = cand
            break
    if slot is None:
        return None
    if last_sent_ts >= slot.timestamp():
        return None
    if (now - slot).total_seconds() > _MAX_LATENESS_SECONDS:
        return ("skip", slot)
    return ("send", slot)


def window_start_before(now: datetime, hour: int, minute: int) -> datetime:
    """Most recent weekday at hour:minute that is at or before now.

    Wednesday morning points at Tuesday 4pm. Wednesday after 4pm points at
    today's 4pm. Monday morning points at Friday 4pm.
    """
    if now.tzinfo is None:
        return now
    for back in range(0, 5):
        day = (now - timedelta(days=back)).date()
        if day.weekday() >= 5:
            continue
        cand = datetime.combine(day, dtime(hour, minute), tzinfo=now.tzinfo)
        if cand <= now:
            return cand
    return now


def window_for_slot(slot: datetime) -> tuple:
    """(start, end) from the previous weekday at the same clock time through
    this slot. Monday 4pm starts Friday 4pm, so weekend messages are included
    Monday. End is exclusive."""
    day = slot.date()
    while True:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            break
    start = slot.replace(year=day.year, month=day.month, day=day.day)
    return start, slot


def _in_window(ts: str, start: datetime, end: datetime) -> bool:
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return False
    return start.timestamp() <= value < end.timestamp()


def candidates_from_message(message: dict, directory: TeamDirectory,
                            start: datetime, end: datetime,
                            channel_id: str, channel_name: str,
                            context_text: str = "") -> list:
    """Request rows for this single message, or [] if it isn't one."""
    if not is_human_message(message):
        return []
    ts = str(message.get("ts") or "")
    if not _in_window(ts, start, end):
        return []
    author = message.get("user") or ""
    if author in directory.bot_ids or author not in directory.team_ids:
        return []
    text = message.get("text") or ""
    pointer = is_pointer(text)
    if not is_action_ask(text, context_text):
        return []
    owners = target_user_ids(text, author, directory, context_text)
    if not owners:
        return []
    shown = context_text if pointer and context_text else text
    kind = "pointer" if pointer else (
        _request_kind(slack_to_plain(shown)) or "action")
    thread_ts = str(message.get("thread_ts") or ts)
    rows = []
    for uid in owners:
        rows.append({
            "channel_id": channel_id,
            "channel_name": channel_name,
            "message_ts": ts,
            "thread_ts": thread_ts,
            "author_id": author,
            "target_id": uid,
            "text": shown,
            "kind": kind,
            "status": "open",
            "created_at": float(ts),
        })
    return rows


_CHECK_REACTIONS = frozenset({
    "white_check_mark", "heavy_check_mark", "ballot_box_with_check",
})
_EYES_REACTIONS = frozenset({"eyes"})
_YES_REACTIONS = frozenset({"+1", "thumbsup", "thumbs_up"})
_NO_REACTIONS = frozenset({"-1", "thumbsdown", "thumbs_down"})
_DEFER_RE = re.compile(
    r"\b(?:i'll|i’ll|i will|tomorrow|later today|next week|before eow|"
    r"end of (?:the )?week|on it|will do|working on it|in progress|"
    r"let me (?:get|do|handle|check)|i can do (?:that|this|it))\b",
    re.I,
)
_STRONG_RESOLVE_RE = re.compile(
    r"("
    r"\b(?:done|completed|complete|approved|calendared|filed|faxed|sent|"
    r"emailed|mailed|deposited|saved|uploaded|submitted|signed|notified|"
    r"disengaged|reopened|produced|finished|handled)\b|"
    r"\b(?:here (?:it|they) (?:is|are)|attached|see attached)\b|"
    r"\bnever ?mind\b|\bdisregard\b|\bno longer needed\b|\bwithdrawn?\b"
    r")",
    re.I,
)
_ANSWER_RE = re.compile(
    r"\b(?:yes|yep|yeah|no|nope|correct|confirmed)\b",
    re.I,
)


def _topic_tokens(text: str) -> set:
    words = re.findall(r"[a-z]{4,}", slack_to_plain(text or "").lower())
    stems = set()
    for word in words:
        if word in _TOPIC_STOP:
            continue
        stem = word
        for suffix in ("ment", "ing", "ed", "es"):
            if len(stem) > len(suffix) + 2 and stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        if len(stem) > 5 and stem.endswith("e"):
            stem = stem[:-1]
        stems.add(stem)
    return stems


def _same_topic(a: str, b: str) -> bool:
    left, right = _topic_tokens(a), _topic_tokens(b)
    if not left or not right:
        return False
    shared = left & right
    return len(shared) / min(len(left), len(right)) >= 0.5


def _reply_effect(text: str, ask_text: str) -> str:
    """resolved, acknowledged, substantive, or none."""
    plain = slack_to_plain(text or "")
    if not plain:
        return "none"
    if _STRONG_RESOLVE_RE.search(plain) and not _DEFER_RE.search(plain):
        return "resolved"
    if _request_kind(slack_to_plain(ask_text or "")) == "question" \
            and _ANSWER_RE.search(plain):
        return "resolved"
    if _DEFER_RE.search(plain):
        return "acknowledged"
    if _only_acknowledgement(plain):
        return "acknowledged"
    body = _message_body(plain)
    if body and len(re.findall(r"[a-z0-9']+", body.lower())) >= 3:
        return "substantive"
    return "none"


def _reaction_effect(ask: dict, user_id: str, ask_text: str) -> str:
    """How this person's reactions on the request itself land."""
    names = set()
    for rx in ask.get("reactions") or []:
        if user_id in (rx.get("users") or []):
            names.add(rx.get("name") or "")
    if names & _CHECK_REACTIONS:
        return "resolved"
    if names & _NO_REACTIONS and _request_kind(slack_to_plain(ask_text)) == "question":
        return "resolved"
    if names & _YES_REACTIONS:
        if _request_kind(slack_to_plain(ask_text)) == "question":
            return "resolved"
        return "acknowledged"
    if names & _EYES_REACTIONS:
        return "acknowledged"
    return "none"


def _in_thread(message: dict, thread_ts: str) -> bool:
    parent = str(message.get("thread_ts") or message.get("ts") or "")
    return parent == str(thread_ts)


def still_waiting(candidates: list, threads: dict,
                  channel_messages: dict | None = None) -> list:
    """Keep a request until the thread shows it was handled.

    Resolved: the owner answers in the thread, someone confirms the task
    is done, the request is withdrawn, or ✅ is on the message. A 👀 or
    "I'll do it tomorrow" stays on the list as acknowledged. A later
    message elsewhere in the channel leaves it open unless that message
    clearly finishes the same task. A missing thread is kept. A thread
    that loaded without the message is dropped.
    """
    kept = []
    for row in candidates:
        key = (row["channel_id"], row["thread_ts"])
        messages = threads.get(key)
        ask_ts = row["message_ts"]
        channel_pool = (channel_messages or {}).get(row["channel_id"]) or []
        if messages is not None:
            ask = next(
                (m for m in messages if str(m.get("ts")) == str(ask_ts)),
                None,
            )
            if ask is None:
                continue
        else:
            ask = next(
                (m for m in channel_pool if str(m.get("ts")) == str(ask_ts)),
                None,
            )
        effect = "none"
        if ask:
            effect = _reaction_effect(ask, row["target_id"], row.get("text") or "")
        if effect != "resolved":
            seen_ts = set()
            sources = []
            if messages is not None:
                sources.append((messages, True))
            sources.append((channel_pool, False))
            for source, source_is_thread in sources:
                for message in source:
                    mid = str(message.get("ts") or "")
                    if mid in seen_ts:
                        continue
                    seen_ts.add(mid)
                    try:
                        later = float(message.get("ts") or 0) > float(ask_ts)
                    except (TypeError, ValueError):
                        later = False
                    if not later or not is_human_message(message):
                        continue
                    reply_effect = _reply_effect(
                        message.get("text") or "", row.get("text") or "")
                    in_thread = source_is_thread or _in_thread(
                        message, row["thread_ts"])
                    from_owner = message.get("user") == row["target_id"]
                    if in_thread and from_owner and reply_effect == "substantive":
                        reply_effect = "resolved"
                    if in_thread and reply_effect == "resolved":
                        effect = "resolved"
                        break
                    if in_thread and from_owner and reply_effect == "acknowledged":
                        effect = "acknowledged"
                        continue
                    if (not in_thread and reply_effect == "resolved"
                            and _same_topic(message.get("text") or "",
                                            row.get("text") or "")):
                        effect = "resolved"
                        break
                if effect == "resolved":
                    break
        if effect == "resolved":
            continue
        updated = dict(row)
        if effect == "acknowledged":
            updated["status"] = "acknowledged"
        kept.append(updated)
    return kept


def collapse_threads(rows: list) -> list:
    """One row per person per thread. A later follow-up keeps the original
    request text so a reminder does not become a second item."""
    best = {}
    for row in rows:
        key = (row["channel_id"], row["thread_ts"], row["target_id"])
        prev = best.get(key)
        if prev is None:
            best[key] = row
            continue
        newer = row if float(row["created_at"]) >= float(prev["created_at"]) else prev
        older = prev if newer is row else row
        if newer.get("kind") == "followup":
            kept = dict(older)
            if newer.get("status") == "acknowledged":
                kept["status"] = "acknowledged"
            best[key] = kept
        else:
            best[key] = newer
    return sorted(best.values(), key=lambda r: (r["target_id"], r["created_at"]))


def consolidate_requests(rows: list) -> list:
    """Fold reminders and same-subject requests into one item per person
    per case channel. Distinct tasks in that channel stay separate."""
    grouped = {}
    for row in sorted(rows, key=lambda r: float(r["created_at"])):
        grouped.setdefault((row["channel_id"], row["target_id"]), []).append(row)
    kept = []
    for items in grouped.values():
        merged = []
        for row in items:
            host = next((prev for prev in merged if _same_request(prev, row)), None)
            if host is None:
                merged.append(dict(row))
                continue
            extra = _topic_tokens(row.get("text") or "") - _topic_tokens(
                host.get("text") or "")
            if extra:
                host["text"] = (host.get("text") or "").rstrip() + "\n" + (
                    row.get("text") or "")
            if row.get("status") == "acknowledged":
                host["status"] = "acknowledged"
        kept.extend(merged)
    return kept


def _same_request(prev: dict, row: dict) -> bool:
    if row.get("kind") == "followup" or prev.get("kind") == "followup":
        return True
    if prev.get("thread_ts") and prev.get("thread_ts") == row.get("thread_ts"):
        return True
    return _same_topic(prev.get("text") or "", row.get("text") or "")


def format_clock(dt: datetime) -> str:
    hour = dt.strftime("%I").lstrip("0") or "12"
    return hour + dt.strftime(":%M %p")


def format_when(ts: float, tz) -> str:
    dt = datetime.fromtimestamp(float(ts), tz)
    return dt.strftime("%a ") + format_clock(dt)


def format_stamp(dt: datetime) -> str:
    hour = format_clock(dt)
    return dt.strftime("%a %b ") + str(dt.day) + " " + hour


def _people_only(row: dict, directory: TeamDirectory) -> bool:
    """Both the writer and the tagged person are teammates, not apps or bots."""
    author = row.get("author_id") or ""
    target = row.get("target_id") or ""
    if author in directory.bot_ids or author not in directory.team_ids:
        return False
    if target in directory.bot_ids or target not in directory.team_ids:
        return False
    return True


def _join_names(names: list) -> str:
    ordered = sorted({n for n in names if n}, key=str.lower)
    if not ordered:
        return ""
    if len(ordered) == 1:
        return ordered[0]
    if len(ordered) == 2:
        return f"{ordered[0]} and {ordered[1]}"
    return ", ".join(ordered[:-1]) + ", and " + ordered[-1]


def rows_for_email(waiting: list, directory: TeamDirectory, workspace: str,
                   tz) -> list:
    """One row per comment, grouped later by channel. Several people tagged
    on the same message stay on that one comment."""
    merged = {}
    for row in waiting:
        if not _people_only(row, directory):
            continue
        key = (row["channel_id"], row["message_ts"])
        item = merged.get(key)
        if item is None:
            channel_id = row["channel_id"]
            base = (workspace or "").rstrip("/")
            item = {
                "channel_id": channel_id,
                "channel_name": row.get("channel_name") or channel_id,
                "channel_link": (
                    f"{base}/archives/{channel_id}" if base and channel_id else ""
                ),
                "asked_by": directory.name(row["author_id"]),
                "author_id": row["author_id"],
                "when": format_when(row["created_at"], tz),
                "when_ts": float(row["created_at"]),
                "snippet": personalized_excerpt(row.get("text") or "", directory),
                "status": row.get("status") or "open",
                "link": email_reports.permalink(
                    workspace, channel_id, row["message_ts"],
                    row.get("thread_ts") or "",
                ),
                "waiting_ids": [],
                "waiting_names": [],
            }
            merged[key] = item
        target = row["target_id"]
        if target not in item["waiting_ids"]:
            item["waiting_ids"].append(target)
            item["waiting_names"].append(directory.name(target))
    out = []
    for item in merged.values():
        item["waiting_name"] = _join_names(item["waiting_names"])
        item["waiting_id"] = item["waiting_ids"][0]
        out.append(item)
    out.sort(key=lambda r: ((r["channel_name"] or "").lower(), r["when_ts"]))
    return out


# ── Slack collection ──────────────────────────────────────────────────

def _slack_call(fn, **kwargs):
    """Call a WebClient method, pausing between calls and backing off on 429."""
    from slack_sdk.errors import SlackApiError
    for attempt in range(6):
        try:
            resp = fn(**kwargs)
            time.sleep(_HISTORY_PAUSE_SECONDS)
            return resp
        except SlackApiError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 429 and attempt < 5:
                headers = getattr(e.response, "headers", {}) or {}
                try:
                    retry = int(headers.get("Retry-After", "1") or 1)
                except (TypeError, ValueError):
                    retry = 1
                time.sleep(retry + 0.5)
                continue
            raise
    raise RuntimeError("Slack rate limit persisted")


def _display_name(user: dict) -> str:
    profile = user.get("profile") or {}
    return (profile.get("display_name")
            or profile.get("real_name")
            or user.get("real_name")
            or user.get("name")
            or user.get("id")
            or "")


def load_directory(client, force: bool = False) -> TeamDirectory:
    """Workspace members who are not bots or guests. Cached for six hours."""
    now = time.time()
    with _directory_lock:
        cached = _directory_cache["directory"]
        if (not force and cached is not None
                and now - _directory_cache["at"] < _DIRECTORY_TTL_SECONDS):
            return cached
        team_ids = set()
        bot_ids = set()
        names = {}
        cursor = None
        while True:
            kwargs = {"limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = _slack_call(client.users_list, **kwargs)
            for user in resp.get("members") or []:
                uid = user.get("id")
                if not uid:
                    continue
                names[uid] = _display_name(user)
                if (user.get("is_bot") or user.get("is_app_user")
                        or uid == "USLACKBOT"):
                    bot_ids.add(uid)
                    continue
                if user.get("deleted") or user.get("is_restricted") \
                        or user.get("is_ultra_restricted") \
                        or user.get("is_stranger"):
                    continue
                team_ids.add(uid)
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        try:
            bot_id = client.auth_test().get("user_id")
            if bot_id:
                bot_ids.add(bot_id)
                team_ids.discard(bot_id)
        except Exception:
            log.debug("auth_test failed while building the team directory",
                      exc_info=True)
        groups = {}
        try:
            ug = _slack_call(client.usergroups_list, include_users=True)
            for group in ug.get("usergroups") or []:
                gid = group.get("id")
                if gid:
                    groups[gid] = list(group.get("users") or [])
        except Exception:
            log.warning(
                "unanswered report: could not read user groups "
                "(usergroups:read). @group tags will not be expanded."
            )
        directory = TeamDirectory(team_ids, bot_ids, names, groups)
        _directory_cache["at"] = time.time()
        _directory_cache["directory"] = directory
        log.info(
            "unanswered directory: %d teammates, %d bots, %d user groups",
            len(team_ids), len(bot_ids), len(groups),
        )
        return directory


def _iter_history(client, channel_id: str, oldest: float, latest: float):
    cursor = None
    while True:
        kwargs = {
            "channel": channel_id,
            "oldest": str(oldest),
            "latest": str(latest),
            "inclusive": True,
            "limit": 200,
        }
        if cursor:
            kwargs["cursor"] = cursor
        resp = _slack_call(client.conversations_history, **kwargs)
        for message in resp.get("messages") or []:
            yield message
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return


def _thread_messages(client, channel_id: str, thread_ts: str) -> list:
    messages = []
    cursor = None
    while True:
        kwargs = {"channel": channel_id, "ts": thread_ts, "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        resp = _slack_call(client.conversations_replies, **kwargs)
        messages.extend(resp.get("messages") or [])
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return messages


def _case_channels(client) -> list:
    """Public case channels, plus private ones when the bot has groups:read.
    The installed bot token can read private history but cannot list private
    channels, so a missing groups:read scope falls back to public channels."""
    from slack_sdk.errors import SlackApiError
    try:
        return _list_case_channels(client, "public_channel,private_channel")
    except SlackApiError as e:
        needed = (getattr(e, "response", None) or {}).get("error")
        if needed == "missing_scope":
            log.warning(
                "unanswered scan: no groups:read — public case channels only"
            )
            return _list_case_channels(client, "public_channel")
        raise


def _list_case_channels(client, types: str) -> list:
    found = []
    cursor = None
    while True:
        kwargs = {
            "types": types,
            "exclude_archived": True,
            "limit": 1000,
        }
        if cursor:
            kwargs["cursor"] = cursor
        resp = _slack_call(client.conversations_list, **kwargs)
        for ch in resp.get("channels") or []:
            name = ch.get("name") or ""
            if not is_case_channel(name):
                continue
            if ch.get("is_archived") or not ch.get("is_member"):
                continue
            found.append({"id": ch["id"], "name": name})
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return found


def collect_candidates(client, directory: TeamDirectory,
                       start: datetime, end: datetime,
                       seen_through: float | None = None) -> dict:
    """Scan case channels for asks in the window, and merge asks that were
    recorded live (thread replies on older parents).

    History is read through seen_through (default: the window end) so a
    channel reply posted after 4pm still clears an ask from this report.
    Only messages inside the window become new asks.
    """
    channels = _case_channels(client)
    if not channels:
        raise RuntimeError("no case channels visible to the bot")
    log.info("unanswered scan: %d case channels", len(channels))

    by_key = {}
    threads = {}
    channel_messages: dict = {}
    ok = 0
    failed = 0
    # Slack oldest is exclusive ("after"), so step back a hair to include
    # a message posted at the previous 4:00:00.
    oldest = start.timestamp() - 0.000001
    latest = seen_through if seen_through is not None else end.timestamp()

    for i, ch in enumerate(channels, start=1):
        try:
            history = list(_iter_history(client, ch["id"], oldest, latest))
        except Exception:
            failed += 1
            log.warning("unanswered scan: history failed for #%s", ch["name"],
                        exc_info=True)
            continue
        ok += 1
        channel_messages[ch["id"]] = history
        if i % 100 == 0:
            log.info("unanswered scan: %d/%d case channels", i, len(channels))
        parents_needing_replies = []
        ordered = sorted(history, key=lambda m: float(m.get("ts") or 0))
        for message in ordered:
            context = _prior_text(message, ordered)
            for row in candidates_from_message(
                    message, directory, start, end, ch["id"], ch["name"],
                    context):
                by_key[(row["channel_id"], row["message_ts"], row["target_id"])] = row
            if int(message.get("reply_count") or 0) > 0 and message.get("ts"):
                parents_needing_replies.append(str(message["ts"]))
        for parent_ts in parents_needing_replies:
            try:
                replies = _thread_messages(client, ch["id"], parent_ts)
            except Exception:
                log.warning(
                    "unanswered scan: replies failed channel=%s thread=%s",
                    ch["id"], parent_ts, exc_info=True,
                )
                continue
            threads[(ch["id"], parent_ts)] = replies
            ordered_replies = sorted(
                replies, key=lambda m: float(m.get("ts") or 0))
            for message in ordered_replies:
                context = _prior_text(message, ordered_replies)
                for row in candidates_from_message(
                        message, directory, start, end, ch["id"], ch["name"],
                        context):
                    by_key[(row["channel_id"], row["message_ts"], row["target_id"])] = row

    # Thread replies on parents older than the window are not in history.
    # Live capture stored them.
    try:
        stored = storage.unanswered_asks_between(
            start.timestamp(), end.timestamp())
    except Exception:
        log.exception("unanswered scan: could not read stored asks")
        stored = []
    for row in stored:
        if not _people_only(row, directory):
            continue
        key = (row["channel_id"], row["message_ts"], row["target_id"])
        by_key.setdefault(key, row)

    if ok == 0:
        raise RuntimeError(
            f"history scan failed for all {len(channels)} case channels"
        )

    return {
        "candidates": list(by_key.values()),
        "threads": threads,
        "channel_messages": channel_messages,
        "channels_seen": len(channels),
        "channels_ok": ok,
        "channels_failed": failed,
    }


def _prior_text(message: dict, ordered: list) -> str:
    """The message this one points at: the previous reply in its thread,
    otherwise the previous message in the channel."""
    try:
        ts = float(message.get("ts") or 0)
    except (TypeError, ValueError):
        return ""
    prior = []
    for older in ordered:
        try:
            older_ts = float(older.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if older_ts < ts and (older.get("text") or ""):
            prior.append(older)
    if not prior:
        return ""
    thread = str(message.get("thread_ts") or "")
    if thread:
        same = [
            older for older in prior
            if str(older.get("thread_ts") or older.get("ts") or "") == thread
        ]
        if same:
            return same[-1].get("text") or ""
    return prior[-1].get("text") or ""


def _ensure_threads(client, candidates: list, threads: dict) -> None:
    needed = {(r["channel_id"], r["thread_ts"]) for r in candidates}
    for key in needed:
        if key in threads:
            continue
        try:
            threads[key] = _thread_messages(client, key[0], key[1])
        except Exception:
            log.warning(
                "unanswered report: could not re-check thread %s/%s; "
                "leaving the ask on the list",
                key[0], key[1], exc_info=True,
            )


def _recipients() -> list:
    raw = storage.get_config("unanswered_report_recipients", default="") or ""
    return email_reports_addresses(raw)


def email_reports_addresses(raw: str) -> list:
    return re.findall(r"[^\s,;]+@[^\s,;]+", raw or "")


def note_live_message(client, event: dict, channel_name: str) -> None:
    """Record an ask as it happens so tags inside older threads are kept."""
    if not is_case_channel(channel_name):
        return
    if event.get("bot_id") or not is_human_message(event):
        return
    text = event.get("text") or ""
    if not may_be_ask(text):
        return
    directory = load_directory(client)
    author = event.get("user") or ""
    if author in directory.bot_ids or author not in directory.team_ids:
        return
    if not is_action_ask(text):
        return
    ts = str(event.get("ts") or "")
    if not ts:
        return
    try:
        created = float(ts)
    except ValueError:
        return
    rows = []
    thread_ts = str(event.get("thread_ts") or ts)
    for uid in target_user_ids(text, author, directory):
        rows.append({
            "channel_id": event.get("channel") or "",
            "channel_name": channel_name,
            "message_ts": ts,
            "thread_ts": thread_ts,
            "author_id": author,
            "target_id": uid,
            "text": text,
            "created_at": created,
        })
    storage.upsert_unanswered_asks(rows)


def note_live_message_async(client, event: dict, channel_name: str) -> None:
    """Don't block the message handler on a directory refresh or a write."""
    snapshot = {
        "text": event.get("text") or "",
        "user": event.get("user") or "",
        "ts": event.get("ts") or "",
        "thread_ts": event.get("thread_ts") or "",
        "channel": event.get("channel") or "",
        "bot_id": event.get("bot_id"),
        "app_id": event.get("app_id"),
        "bot_profile": event.get("bot_profile"),
        "subtype": event.get("subtype"),
    }

    def _run():
        try:
            note_live_message(client, snapshot, channel_name)
        except Exception:
            log.exception("unanswered ask note failed")

    threading.Thread(target=_run, daemon=True).start()


def _consume_slot(slot: datetime) -> None:
    storage.set_config(_LAST_SENT_KEY, str(slot.timestamp()))


def fire_daily_report(client) -> None:
    """Start the 4pm report if it's due. The scan runs off the reminder
    thread so a few hundred case channels don't stall other reminders."""
    global _scan_running, _off_logged, _unconfigured_logged_at
    if REPORT_TZ is None:
        return
    if time.time() < _retry_not_before:
        return
    recipients = _recipients()
    if not recipients:
        if not _off_logged:
            log.info(
                "unanswered report is off — set Unanswered Asks Email — "
                "Recipients in admin settings"
            )
            _off_logged = True
        return

    sender = storage.get_config(
        "email_sender_address", env_fallback="GMAIL_SENDER").strip()
    if not email_reports.configured(sender):
        now_ts = time.time()
        if now_ts - _unconfigured_logged_at > 3600:
            log.warning(
                "unanswered report: email sender is not configured; will retry"
            )
            _unconfigured_logged_at = now_ts
        return

    hour, minute = parse_clock(
        storage.get_config("unanswered_report_time", default="16:00"))
    try:
        last_sent = float(
            storage.get_config(_LAST_SENT_KEY, default="0") or 0)
    except ValueError:
        last_sent = 0.0
    decision = due_slot(datetime.now(REPORT_TZ), hour, minute, last_sent)
    if decision is None:
        return
    action, slot = decision
    if action == "skip":
        _consume_slot(slot)
        log.info(
            "unanswered report: slot %s missed by >18h — skipping",
            slot.strftime("%a %Y-%m-%d %H:%M"),
        )
        return

    with _scan_lock:
        if _scan_running:
            return
        _scan_running = True
    threading.Thread(
        target=_send_report,
        args=(client, slot, recipients, sender),
        daemon=True,
    ).start()


def queue_test_report(client) -> str:
    """Send the report now to the saved recipients. Does not count as the
    day's 4pm send. Returns started, off, no-email, or busy."""
    global _scan_running
    if REPORT_TZ is None:
        return "no-email"
    recipients = _recipients()
    if not recipients:
        return "off"
    sender = storage.get_config(
        "email_sender_address", env_fallback="GMAIL_SENDER").strip()
    if not email_reports.configured(sender):
        return "no-email"
    with _scan_lock:
        if _scan_running:
            return "busy"
        _scan_running = True
    now = datetime.now(REPORT_TZ)
    hour, minute = parse_clock(
        storage.get_config("unanswered_report_time", default="16:00"))
    start = window_start_before(now, hour, minute)
    threading.Thread(
        target=_send_report,
        args=(client, now, recipients, sender),
        kwargs={
            "mark_sent": False,
            "subject_prefix": "Test — ",
            "window": (start, now),
        },
        daemon=True,
    ).start()
    log.info(
        "unanswered test report queued for %d recipient(s), %s → %s",
        len(recipients), start.strftime("%a %H:%M"), now.strftime("%a %H:%M"),
    )
    return "started"


def _send_report(client, slot: datetime, recipients: list, sender: str, *,
                 mark_sent: bool = True, subject_prefix: str = "",
                 window: tuple | None = None) -> None:
    global _scan_running, _retry_not_before
    try:
        if window is not None:
            start, end = window
        else:
            start, end = window_for_slot(slot)
        log.info(
            "unanswered report scanning %s → %s",
            start.strftime("%a %H:%M"), end.strftime("%a %H:%M"),
        )
        directory = load_directory(client)
        gathered = collect_candidates(
            client, directory, start, end, seen_through=time.time())
        candidates = gathered["candidates"]
        threads = gathered["threads"]
        _ensure_threads(client, candidates, threads)
        waiting = consolidate_requests(collapse_threads(still_waiting(
            candidates, threads, gathered.get("channel_messages"))))
        try:
            storage.upsert_unanswered_asks(candidates)
            storage.prune_unanswered_asks(time.time() - 21 * 86400)
        except Exception:
            log.exception("unanswered report: could not persist asks")

        workspace = storage.get_config("slack_workspace_url", default="").rstrip("/")
        email_rows = rows_for_email(waiting, directory, workspace, REPORT_TZ)
        window_desc = f"{format_stamp(start)} – {format_stamp(end)} CT"
        failed = gathered["channels_failed"]
        note = ""
        if failed:
            note = (
                f"{failed} case channel"
                f"{'' if failed == 1 else 's'} could not be read and "
                f"{'is' if failed == 1 else 'are'} not included."
            )
        html = email_reports.build_unanswered_email_html(
            window_desc, email_rows,
            channels_scanned=gathered["channels_ok"],
            note=note,
        )
        counts = email_reports.unanswered_person_counts(email_rows)
        open_items = sum(n for _name, n in counts)
        people = len(counts)
        if email_rows:
            subject = (
                f"{subject_prefix}Unresolved Slack requests — "
                f"{slot.strftime('%a %b ')}{slot.day}"
                f" — {open_items} open across {people} "
                f"{'person' if people == 1 else 'people'}"
            )
        else:
            subject = (
                f"{subject_prefix}Unresolved Slack requests — "
                f"{slot.strftime('%a %b ')}{slot.day}"
                f" — all clear"
            )
        if email_reports.send_email(recipients, subject, html, sender=sender):
            if mark_sent:
                storage.set_config(_LAST_SENT_KEY, str(time.time()))
                _retry_not_before = 0.0
            log.info(
                "unanswered report sent to %d — %d ask(s), %d channel(s) scanned",
                len(recipients), len(email_rows), gathered["channels_ok"],
            )
        else:
            if mark_sent:
                _retry_not_before = time.time() + 900
            log.warning("unanswered report send failed — will retry in 15 min")
    except Exception:
        if mark_sent:
            _retry_not_before = time.time() + 900
        log.exception("unanswered report failed — will retry in 15 min")
    finally:
        with _scan_lock:
            _scan_running = False
