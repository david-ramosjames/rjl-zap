import logging
import os
import re
import time
import threading

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import auto_join
import recent_contact
import reminders
import storage
import web
from config import (
    ATTORNEY_INTRO, CASE_SETUP, CHECK_PICKUP, PARALEGAL_INTRO,
    CALENDAR_SOL_DELAY_SECONDS,
    CASE_SETUP_DELAY_SECONDS, DOC_VERIFICATION_DELAY_SECONDS,
    CHECK_PICKUP_AUTO_PHRASE, REVIEW_REQUEST_AUTO_PHRASE,
    COMPLETION_EMOJI, COMPLETION_REPLY,
    DISBURSEMENT, DISBURSEMENT_MASTER_CHECKLIST,
    DOC_VERIFICATION,
    FollowUpConfig,
    MEDIATION, NEW_CASE, NEW_CASE_ON_FIRST_MESSAGE, REVIEW_REQUEST,
    SimplePostConfig,
    TRIGGERS, TriggerConfig,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("calendar-bot")

app = App(token=os.environ["SLACK_BOT_TOKEN"])

HELP_PHRASES = ("help", "faq", "commands", "how do i use", "what can you do", "what do you do")
FAQ_URL = os.getenv("FAQ_URL", "https://rjl-zap.up.railway.app/")


def _ids_from_config(key: str) -> list[str]:
    raw = storage.get_config(key)
    return [uid.strip() for uid in raw.split(",") if uid.strip()]


_CONTACT_ICONS = {
    "email":     ":email:",
    "text":      ":speech_balloon:",
    "voicemail": ":studio_microphone:",
    "call":      ":telephone_receiver:",
    "letter":    ":envelope:",
    "mail":      ":mailbox:",
    "fax":       ":fax:",
    "visit":     ":handshake:",
    "other":     ":pencil:",
}

_RECENT_CONTACT_USAGE = (
    ":information_source: *Recent Contact* — log a client interaction to the tracker.\n"
    "Format: `@RJL-zap recent contact <type> - <details>`\n"
    "Types: `email`, `text`, `call`, `voicemail`, `letter`, `mail`, `fax`, `visit`, `other`\n\n"
    "Examples:\n"
    "• `@RJL-zap recent contact email - Re: discovery responses sent`\n"
    "• `@RJL-zap recent contact call - 15 min, discussed depo prep`\n"
    "• `@RJL-zap recent contact visit - client came in to sign release`\n"
    "• `@RJL-zap recent contact voicemail - left vm re: settlement offer`"
)


def _log_recent_contact(client, event: dict) -> None:
    channel = event["channel"]
    parent_ts = event["ts"]
    text = event.get("text") or ""

    contact_type, details = recent_contact.parse(text)
    if not contact_type:
        client.chat_postMessage(channel=channel, thread_ts=parent_ts, text=_RECENT_CONTACT_USAGE)
        return

    if not storage.get_config("recent_contact_spreadsheet_id").strip():
        client.chat_postMessage(
            channel=channel,
            thread_ts=parent_ts,
            text=(
                f":warning: Got a *{contact_type}* to log, but the Recent Contact "
                f"spreadsheet isn't configured yet. Ask an admin to set it on the help page."
            ),
        )
        return

    recent_contact.log_contact_async(client, event, contact_type, details)
    icon = _CONTACT_ICONS.get(contact_type, ":pencil:")
    summary = f" — _{details}_" if details else ""
    client.chat_postMessage(
        channel=channel,
        thread_ts=parent_ts,
        text=f"{icon} Logged *{contact_type}*{summary}",
    )


def _post_help(client, channel: str, parent_ts: str) -> None:
    client.chat_postMessage(
        channel=channel,
        thread_ts=parent_ts,
        text=(
            f":book: *RJL-zap — Quick Help*\n"
            f"Full command reference, examples, and FAQ are on the help page:\n"
            f"<{FAQ_URL}|{FAQ_URL}>\n\n"
            f"*Common commands:*\n"
            f"• `@RJL-zap answer filed` — calendar checklist for an answer\n"
            f"• `@RJL-zap mediation checklist @people` — mediation prep + follow-ups\n"
            f"• `@RJL-zap attorney intro @attorney` — 72-hour client contact reminder\n"
            f"• `@RJL-zap case setup @person` — intake document checklist\n"
            f"• `@RJL-zap new case` — notify the case assignee\n"
            f"• `@RJL-zap recent contact <type> - <details>` — log a client interaction\n"
            f"   types: email, text, call, voicemail, letter, mail, fax, visit, other\n"
        ),
    )


def _find_trigger(text: str) -> TriggerConfig | None:
    lowered = text.lower()
    for t in TRIGGERS.values():
        if t.phrase in lowered:
            return t
    return None


@app.event("app_mention")
def handle_app_mention(event, client):
    text = event.get("text") or ""
    parent_ts = event.get("thread_ts")
    is_top_level = not parent_ts or parent_ts == event.get("ts")

    if is_top_level:
        lowered = text.lower()
        # Strip the bot's own @-mention so phrases like "help" don't collide with user names
        stripped = re.sub(r"<@[A-Z0-9]+>", "", lowered).strip()
        if stripped in HELP_PHRASES or any(stripped.startswith(p) for p in HELP_PHRASES):
            _post_help(client, event["channel"], event["ts"])
            return

        if recent_contact.is_recent_contact(lowered):
            _log_recent_contact(client, event)
            return

        if MEDIATION.phrase in lowered:
            _start_mediation(client, event["channel"], event["ts"], text)
            return
        if DISBURSEMENT.phrase in lowered:
            _start_disbursement(client, event["channel"], event["ts"], event.get("user", ""))
            return
        # Order matters: 'paralegal intro' and 'attorney intro' share the suffix "intro",
        # and 'check pickup' must be matched before any potential collisions.
        followup_matches = [
            (PARALEGAL_INTRO, "paralegal_intro_escalation_user_ids", "paralegal_intro"),
            (ATTORNEY_INTRO, "attorney_intro_escalation_user_ids", "attorney_intro"),
            (CHECK_PICKUP, "check_pickup_backup_user_ids", "check_pickup"),
            (CASE_SETUP, "case_setup_escalation_user_ids", "case_setup"),
            (DOC_VERIFICATION, "doc_verification_escalation_user_ids", "doc_verification"),
        ]
        for cfg, setting_key, name in followup_matches:
            if cfg.phrase in lowered:
                _start_followup_workflow(
                    client, event["channel"], event["ts"], text,
                    cfg, _ids_from_config(setting_key), name,
                )
                return

        for simple in (NEW_CASE, REVIEW_REQUEST):
            if simple.phrase in lowered:
                _do_simple_post(client, event["channel"], event["ts"], text, simple)
                return

        trigger = _find_trigger(text)
        if trigger:
            _start_workflow(client, event["channel"], event["ts"], trigger)
            return

        all_phrases = (
            [
                f"`{MEDIATION.phrase}`",
                f"`{DISBURSEMENT.phrase}`",
                f"`{ATTORNEY_INTRO.phrase}`",
                f"`{PARALEGAL_INTRO.phrase}`",
                f"`{CASE_SETUP.phrase}`",
                f"`{CHECK_PICKUP.phrase}`",
                f"`{NEW_CASE.phrase}`",
                f"`{REVIEW_REQUEST.phrase}`",
            ]
            + [f"`{t.phrase}`" for t in TRIGGERS.values()]
        )
        client.chat_postMessage(
            channel=event["channel"],
            thread_ts=event["ts"],
            text=(
                "I didn't recognize a trigger in that message. "
                "Try mentioning me with one of: " + ", ".join(all_phrases) +
                f"\n\nOr type `@RJL-zap help` — full reference at <{FAQ_URL}|{FAQ_URL}>"
            ),
        )
        return

    if COMPLETION_REPLY.lower() in text.lower():
        wf = storage.workflow_by_thread(event["channel"], parent_ts)
        if wf and not wf.get("completed_at"):
            storage.force_complete_workflow(wf["id"])
            client.chat_postMessage(
                channel=event["channel"],
                thread_ts=parent_ts,
                text=":tada: All items marked complete. Closing checklist.",
            )


def _start_mediation(client, channel: str, parent_ts: str, raw_text: str) -> None:
    if storage.workflow_by_thread(channel, parent_ts):
        return

    bot_id = client.auth_test()["user_id"]
    mentioned = re.findall(r"<@([A-Z0-9]+)>", raw_text)
    participants = [uid for uid in mentioned if uid != bot_id]
    mention_str = " ".join(f"<@{uid}>" for uid in participants) if participants else "team"

    checklist = "\n".join(MEDIATION.checklist)
    client.chat_postMessage(
        channel=channel,
        thread_ts=parent_ts,
        text=(
            f":scales: *Mediation Checklist* — {mention_str} please coordinate the following:\n\n"
            f"{checklist}"
        ),
    )

    now = time.time()
    for delay, template in MEDIATION.followups:
        storage.schedule_message(
            channel_id=channel,
            thread_ts=parent_ts,
            send_after=now + delay,
            text=template.format(mentions=mention_str),
        )

    storage.create_workflow(channel, parent_ts, "mediation_checklist", [])
    log.info("started mediation workflow channel=%s parent_ts=%s participants=%s",
             channel, parent_ts, participants)


def _start_disbursement(client, channel: str, parent_ts: str, user_id: str) -> None:
    authorized = _ids_from_config("disbursement_authorized_user_ids")
    if authorized and user_id not in authorized:
        client.chat_postMessage(
            channel=channel,
            thread_ts=parent_ts,
            text=":no_entry: Sorry, you're not authorized to start the disbursement workflow.",
        )
        log.warning("unauthorized disbursement attempt by user=%s", user_id)
        return

    if storage.workflow_by_thread(channel, parent_ts):
        return

    try:
        info = client.conversations_info(channel=channel)
        topic = info["channel"]["topic"]["value"] or ""
    except Exception:
        topic = ""
        log.debug("could not fetch channel topic", exc_info=True)

    bot_id = client.auth_test()["user_id"]
    mentioned = re.findall(r"<@([A-Z0-9]+)>", topic)
    participants = [uid for uid in mentioned if uid != bot_id][:3]
    mention_str = " ".join(f"<@{uid}>" for uid in participants) if participants else "team"

    client.chat_postMessage(
        channel=channel,
        thread_ts=parent_ts,
        text=DISBURSEMENT_MASTER_CHECKLIST.format(mentions=mention_str),
    )

    now = time.time()
    for delay, template in DISBURSEMENT.sequence:
        storage.schedule_message(
            channel_id=channel,
            thread_ts=parent_ts,
            send_after=now + delay,
            text=template.format(mentions=mention_str),
        )

    storage.create_workflow(channel, parent_ts, "disbursement", [])
    log.info("started disbursement workflow channel=%s parent_ts=%s triggered_by=%s participants=%s",
             channel, parent_ts, user_id, participants)


def _start_followup_workflow(
    client,
    channel: str,
    parent_ts: str,
    raw_text: str,
    cfg: FollowUpConfig,
    escalation_ids: list[str],
    trigger_name: str,
) -> None:
    if storage.workflow_by_thread(channel, parent_ts):
        return

    bot_id = client.auth_test()["user_id"]
    mentioned = re.findall(r"<@([A-Z0-9]+)>", raw_text)
    participants = [uid for uid in mentioned if uid != bot_id]
    mention_str = " ".join(f"<@{uid}>" for uid in participants) if participants else "team"
    escalation_str = " ".join(f"<@{uid}>" for uid in escalation_ids)

    def render(template: str) -> str:
        return (
            template
            .replace("{{mentions}}", mention_str)
            .replace("{{escalation}}", f"{escalation_str} " if escalation_str else "")
        )

    now = time.time()

    if cfg.initial_delay_seconds > 0:
        storage.schedule_message(
            channel_id=channel,
            thread_ts=parent_ts,
            send_after=now + cfg.initial_delay_seconds,
            text=render(cfg.initial_message),
        )
    else:
        client.chat_postMessage(
            channel=channel, thread_ts=parent_ts, text=render(cfg.initial_message)
        )

    storage.schedule_message(
        channel_id=channel,
        thread_ts=parent_ts,
        send_after=now + cfg.check_delay_seconds,
        text=render(cfg.escalation_message),
        check_replies_first=True,
        done_keyword=cfg.done_keyword,
    )

    storage.create_workflow(channel, parent_ts, trigger_name, [])
    log.info("started %s workflow channel=%s parent_ts=%s participants=%s",
             trigger_name, channel, parent_ts, participants)


def _do_simple_post(client, channel: str, parent_ts: str, raw_text: str, cfg: SimplePostConfig) -> None:
    bot_id = client.auth_test()["user_id"]
    mentioned = re.findall(r"<@([A-Z0-9]+)>", raw_text)
    participants = [uid for uid in mentioned if uid != bot_id]
    mention_str = " ".join(f"<@{uid}>" for uid in participants) if participants else ""
    extras = " ".join(f"<@{uid}>" for uid in _ids_from_config(cfg.extras_setting_key))

    text = cfg.message.replace("{mentions}", mention_str).replace("{extras}", extras).strip()
    client.chat_postMessage(channel=channel, thread_ts=parent_ts, text=text)
    log.info("posted simple workflow phrase=%s channel=%s parent_ts=%s",
             cfg.phrase, channel, parent_ts)


def _do_simple_post_top_level(client, channel: str, cfg: SimplePostConfig) -> None:
    """Post a SimplePost as a brand-new channel message (no thread, no @-mentioned participants)."""
    extras = " ".join(f"<@{uid}>" for uid in _ids_from_config(cfg.extras_setting_key))
    text = cfg.message.replace("{mentions}", "").replace("{extras}", extras).strip()
    text = re.sub(r"\s+\n", "\n", text)
    client.chat_postMessage(channel=channel, text=text)
    log.info("auto-posted simple workflow phrase=%s channel=%s", cfg.phrase, channel)


def _auto_start_followup(
    client,
    channel: str,
    cfg: FollowUpConfig,
    escalation_setting_key: str,
    trigger_name: str,
    participants: list[str] | None = None,
) -> None:
    """Like _start_followup_workflow but starts a brand-new top-level thread.

    If `participants` is None we fall back to the 'case_setup_participant_user_ids'
    setting (used by the auto Case Setup / Doc Verification triggers).
    """
    if participants is None:
        participants = _ids_from_config("case_setup_participant_user_ids")
    mention_str = " ".join(f"<@{uid}>" for uid in participants) if participants else "team"
    escalation_str = " ".join(f"<@{uid}>" for uid in _ids_from_config(escalation_setting_key))

    def render(template: str) -> str:
        return (
            template
            .replace("{{mentions}}", mention_str)
            .replace("{{escalation}}", f"{escalation_str} " if escalation_str else "")
        )

    resp = client.chat_postMessage(channel=channel, text=render(cfg.initial_message))
    parent_ts = resp["ts"]

    if storage.workflow_by_thread(channel, parent_ts):
        return

    storage.schedule_message(
        channel_id=channel,
        thread_ts=parent_ts,
        send_after=time.time() + cfg.check_delay_seconds,
        text=render(cfg.escalation_message),
        check_replies_first=True,
        done_keyword=cfg.done_keyword,
    )
    storage.create_workflow(channel, parent_ts, trigger_name, [])
    log.info("auto-started %s workflow channel=%s parent_ts=%s", trigger_name, channel, parent_ts)


def fire_due_lifecycle_triggers(client) -> None:
    """Called from the reminder loop. Fires case_setup (T+15min),
    doc_verification (T+24h), and calendar_sol (T+48h) for any
    channel that's past its delay but hasn't fired yet."""
    now = time.time()

    for row in storage.lifecycle_due(now, "case_setup", CASE_SETUP_DELAY_SECONDS):
        if storage.mark_lifecycle_fired(row["channel_id"], "case_setup"):
            try:
                _auto_start_followup(
                    client, row["channel_id"], CASE_SETUP,
                    "case_setup_escalation_user_ids", "case_setup",
                )
            except Exception:
                log.exception("auto case_setup failed for channel=%s", row["channel_id"])

    for row in storage.lifecycle_due(now, "doc_verification", DOC_VERIFICATION_DELAY_SECONDS):
        if storage.mark_lifecycle_fired(row["channel_id"], "doc_verification"):
            try:
                _auto_start_followup(
                    client, row["channel_id"], DOC_VERIFICATION,
                    "doc_verification_escalation_user_ids", "doc_verification",
                )
            except Exception:
                log.exception("auto doc_verification failed for channel=%s", row["channel_id"])

    sol_trigger = TRIGGERS.get("calendar_sol")
    if sol_trigger is not None:
        for row in storage.lifecycle_due(now, "calendar_sol", CALENDAR_SOL_DELAY_SECONDS):
            if storage.mark_lifecycle_fired(row["channel_id"], "calendar_sol"):
                try:
                    _auto_start_trigger_workflow(client, row["channel_id"], sol_trigger)
                except Exception:
                    log.exception("auto calendar_sol failed for channel=%s", row["channel_id"])


def _auto_start_trigger_workflow(client, channel: str, trigger: TriggerConfig) -> None:
    """Auto-start a TriggerConfig workflow at the top level of a channel
    (no @-mention required). Posts the announcement as a new parent
    message, then runs the same item/reaction flow as _start_workflow."""
    resp = client.chat_postMessage(channel=channel, text=trigger.announcement)
    parent_ts = resp["ts"]
    if storage.workflow_by_thread(channel, parent_ts):
        return

    item_records: list[tuple[str, str]] = []
    for item in trigger.items:
        ir = client.chat_postMessage(channel=channel, thread_ts=parent_ts, text=item)
        item_records.append((item, ir["ts"]))
        try:
            client.reactions_add(channel=channel, name="hourglass_flowing_sand", timestamp=ir["ts"])
        except Exception:
            log.debug("could not add hourglass reaction", exc_info=True)

    client.chat_postMessage(
        channel=channel,
        thread_ts=parent_ts,
        text=(
            f"React :{COMPLETION_EMOJI}: on each item above when done, "
            f"or reply by mentioning me with `{COMPLETION_REPLY}` to close the checklist."
        ),
    )
    storage.create_workflow(channel, parent_ts, trigger.name, item_records)
    log.info("auto-started %s workflow channel=%s parent_ts=%s items=%d",
             trigger.name, channel, parent_ts, len(item_records))


def _start_workflow(client, channel: str, parent_ts: str, trigger: TriggerConfig) -> None:
    if storage.workflow_by_thread(channel, parent_ts):
        return

    client.chat_postMessage(channel=channel, thread_ts=parent_ts, text=trigger.announcement)

    item_records: list[tuple[str, str]] = []
    for item in trigger.items:
        resp = client.chat_postMessage(channel=channel, thread_ts=parent_ts, text=item)
        item_records.append((item, resp["ts"]))
        try:
            client.reactions_add(
                channel=channel, name="hourglass_flowing_sand", timestamp=resp["ts"]
            )
        except Exception:
            log.debug("could not add hourglass reaction", exc_info=True)

    client.chat_postMessage(
        channel=channel,
        thread_ts=parent_ts,
        text=(
            f"React :{COMPLETION_EMOJI}: on each item above when done, "
            f"or reply by mentioning me with `{COMPLETION_REPLY}` to close the checklist."
        ),
    )

    storage.create_workflow(channel, parent_ts, trigger.name, item_records)
    log.info("started workflow channel=%s parent_ts=%s trigger=%s items=%d",
             channel, parent_ts, trigger.name, len(item_records))


@app.event("reaction_added")
def handle_reaction_added(event, client):
    if event.get("reaction") != COMPLETION_EMOJI:
        return
    item = event.get("item") or {}
    if item.get("type") != "message":
        return
    workflow_id = storage.mark_item_complete(item.get("ts"))
    if workflow_id is not None:
        _maybe_finalize(client, workflow_id)


@app.event("member_joined_channel")
def handle_member_joined(event, client):
    """When the bot itself joins a channel (manually or via auto-join), peek at
    the existing topic/purpose so we don't miss intros that were set before
    the bot was present."""
    try:
        bot_id = client.auth_test()["user_id"]
    except Exception:
        return
    if event.get("user") != bot_id:
        return
    channel_id = event.get("channel")
    if not channel_id:
        return
    try:
        info = client.conversations_info(channel=channel_id)
        ch = info.get("channel") or {}
        topic   = (ch.get("topic")   or {}).get("value", "") or ""
        purpose = (ch.get("purpose") or {}).get("value", "") or ""
    except Exception:
        log.debug("conversations.info failed for %s", channel_id, exc_info=True)
        return
    if topic:
        _maybe_fire_intros_from_topic(client, channel_id, topic)
    if purpose:
        _maybe_fire_intros_from_topic(client, channel_id, purpose)


@app.event("channel_created")
def handle_channel_created(event, client):
    auto_join.handle_channel_created(event, client)
    ch = event.get("channel") or {}
    channel_id = ch.get("id")
    if channel_id:
        # Slack gives `created` as epoch seconds; fall back to now if missing.
        created_at = ch.get("created") or time.time()
        storage.record_channel_created(channel_id, float(created_at))
        log.info("recorded channel lifecycle for #%s (%s)",
                 ch.get("name", "?"), channel_id)


_TOPIC_ATTORNEY_RE  = re.compile(r"attorney[^A-Za-z<]*<@([A-Z0-9]+)>", re.IGNORECASE)
_TOPIC_PARALEGAL_RE = re.compile(r"paralegal[^A-Za-z<]*<@([A-Z0-9]+)>", re.IGNORECASE)


def _maybe_fire_intros_from_topic(client, channel_id: str, topic_text: str) -> None:
    """Parse the channel topic/purpose for `Attorney @X` / `Paralegal @Y`
    mentions and auto-fire the respective intro workflows for those people.
    Re-fires if a different person is named than before."""
    if not channel_id or not topic_text:
        return

    bot_id = client.auth_test()["user_id"]
    attorney_id  = _first_match(_TOPIC_ATTORNEY_RE,  topic_text, exclude=bot_id)
    paralegal_id = _first_match(_TOPIC_PARALEGAL_RE, topic_text, exclude=bot_id)

    if attorney_id and storage.set_intro_fired_for(channel_id, "attorney", attorney_id):
        log.info("auto-firing attorney_intro for %s in #%s", attorney_id, channel_id)
        try:
            _auto_start_followup(
                client, channel_id, ATTORNEY_INTRO,
                "attorney_intro_escalation_user_ids", "attorney_intro",
                participants=[attorney_id],
            )
        except Exception:
            log.exception("auto attorney_intro failed for channel=%s", channel_id)

    if paralegal_id and storage.set_intro_fired_for(channel_id, "paralegal", paralegal_id):
        log.info("auto-firing paralegal_intro for %s in #%s", paralegal_id, channel_id)
        try:
            _auto_start_followup(
                client, channel_id, PARALEGAL_INTRO,
                "paralegal_intro_escalation_user_ids", "paralegal_intro",
                participants=[paralegal_id],
            )
        except Exception:
            log.exception("auto paralegal_intro failed for channel=%s", channel_id)


def _first_match(pattern, text: str, exclude: str | None = None) -> str | None:
    for m in pattern.finditer(text):
        uid = m.group(1)
        if uid != exclude:
            return uid
    return None


@app.event("message")
def handle_message(event, client):
    """Handles channel topic/purpose updates, the first-message new_case
    trigger, and the keyword-driven Mediation Checklist trigger."""
    subtype = event.get("subtype")
    channel_id = event.get("channel")
    if not channel_id:
        return

    # Channel description / topic was set or changed → parse for intros
    if subtype == "channel_topic":
        _maybe_fire_intros_from_topic(client, channel_id, event.get("topic") or "")
        return
    if subtype == "channel_purpose":
        _maybe_fire_intros_from_topic(client, channel_id, event.get("purpose") or "")
        return

    # Skip bot messages, message subtypes (channel_join, etc.), and thread replies
    if event.get("bot_id") or subtype or event.get("thread_ts"):
        return

    text = event.get("text") or ""
    lowered = text.lower()

    # Auto-fire Mediation Checklist on any message containing the phrase
    # (matches the legacy Zapier "search for 'Mediation Checklist'" trigger).
    # Skip if the bot is @-mentioned in this message — app_mention is
    # already handling it.
    if MEDIATION.phrase in lowered and not _bot_is_mentioned(client, text):
        _start_mediation(client, channel_id, event["ts"], text)
        return

    # Auto-fire Check Pickup when a configured user posts the trigger phrase
    # (legacy Zapier "law firm can be paid" from:@paralegal trigger).
    if CHECK_PICKUP_AUTO_PHRASE in lowered:
        trigger_users = _ids_from_config("check_pickup_trigger_user_ids")
        author = event.get("user", "")
        if trigger_users and author in trigger_users and not _bot_is_mentioned(client, text):
            log.info("auto-firing check_pickup from user=%s in #%s", author, channel_id)
            try:
                _auto_start_followup(
                    client, channel_id, CHECK_PICKUP,
                    "check_pickup_backup_user_ids", "check_pickup",
                    participants=[author],
                )
            except Exception:
                log.exception("auto check_pickup failed for channel=%s", channel_id)
            return

    # Auto-fire 5-star review prompt when a configured user posts "RJL has been paid"
    # (3-minute delay so the message lands after the case is settled in Slack)
    if REVIEW_REQUEST_AUTO_PHRASE.lower() in lowered:
        trigger_users = _ids_from_config("review_request_trigger_user_ids")
        author = event.get("user", "")
        if trigger_users and author in trigger_users and not _bot_is_mentioned(client, text):
            log.info("auto-firing review_request from user=%s in #%s", author, channel_id)
            try:
                extras = " ".join(f"<@{uid}>" for uid in _ids_from_config(REVIEW_REQUEST.extras_setting_key))
                msg_text = (
                    REVIEW_REQUEST.message
                    .replace("{mentions}", f"<@{author}>")
                    .replace("{extras}", extras)
                    .strip()
                )
                storage.schedule_message(
                    channel_id=channel_id,
                    thread_ts="",
                    send_after=time.time() + 3 * 60,
                    text=msg_text,
                )
            except Exception:
                log.exception("auto review_request scheduling failed for channel=%s", channel_id)
            return

    # First user message in a newly-created channel → fire new_case
    if not NEW_CASE_ON_FIRST_MESSAGE:
        return
    lc = storage.channel_lifecycle(channel_id)
    if not lc or lc.get("new_case_fired_at"):
        return
    if not storage.mark_new_case_fired(channel_id):
        return  # another worker beat us to it
    log.info("auto-firing new_case on first message in #%s", channel_id)
    _do_simple_post_top_level(client, channel_id, NEW_CASE)


_BOT_ID_CACHE: str | None = None


def _bot_is_mentioned(client, text: str) -> bool:
    global _BOT_ID_CACHE
    if _BOT_ID_CACHE is None:
        try:
            _BOT_ID_CACHE = client.auth_test()["user_id"]
        except Exception:
            return False
    return f"<@{_BOT_ID_CACHE}>" in text


@app.event("reaction_removed")
def handle_reaction_removed(event, client):
    if event.get("reaction") != COMPLETION_EMOJI:
        return
    item = event.get("item") or {}
    if item.get("type") != "message":
        return
    storage.mark_item_incomplete(item.get("ts"))


def _maybe_finalize(client, workflow_id: int) -> None:
    if storage.workflow_open_items(workflow_id):
        return
    wf = storage.workflow_by_id(workflow_id)
    if not wf or wf.get("completed_at"):
        return
    storage.mark_workflow_complete(workflow_id)
    client.chat_postMessage(
        channel=wf["channel_id"],
        thread_ts=wf["parent_ts"],
        text=":tada: All items complete. Nice work!",
    )


def main() -> None:
    storage.init_db()
    reminders.start_reminder_loop(app.client)
    threading.Thread(target=web.start, daemon=True).start()
    if os.getenv("AUTO_JOIN_CHANNELS", "1") not in ("0", "false", "False", ""):
        auto_join.join_all_public_channels_async(app.client)
    log.info("Starting bot in Socket Mode")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
