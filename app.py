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
    NEW_CASE_DELAY_SECONDS,
    CHECK_PICKUP_AUTO_PHRASE, REVIEW_REQUEST_AUTO_PHRASE,
    CLIENT_INTAKE, CLIENT_INTAKE_DELAY_SECONDS,
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
    "whatsapp":  ":iphone:",
    "message":   ":speech_balloon:",
    "letter":    ":envelope:",
    "mail":      ":mailbox:",
    "fax":       ":fax:",
    "visit":     ":handshake:",
    "other":     ":pencil:",
}

# Phone calls / texts / voicemails are tracked automatically by the firm's
# phone system and feed into the Client Contact Status sheet directly, so
# they don't need to be logged here. Recent Contact covers the contact
# methods that aren't auto-tracked.
_AUTO_TRACKED_CONTACT_TYPES = {"call", "text", "voicemail"}

_RECENT_CONTACT_USAGE = (
    ":information_source: *Recent Contact* — log a client interaction to the tracker.\n"
    "Format: `@RJL-zap recent contact <type> - <details>`\n"
    "(`client contact` works too, and you can say `via <type>`.)\n"
    "Types: `email`, `whatsapp`, `message`, `letter`, `mail`, `fax`, `visit`, `other`\n"
    "_Phone calls, texts, and voicemails are tracked automatically — no need to log them here._\n\n"
    "Examples:\n"
    "• `@RJL-zap recent contact email - Re: discovery responses sent`\n"
    "• `@RJL-zap client contact via WhatsApp - sent client a case update`\n"
    "• `@RJL-zap recent contact visit - client came in to sign release`\n"
    "• `@RJL-zap client contact fax - records request faxed to provider`"
)


def _log_recent_contact(client, event: dict) -> None:
    channel = event["channel"]
    parent_ts = event["ts"]
    text = event.get("text") or ""

    contact_type, details = recent_contact.parse(text)
    if not contact_type:
        client.chat_postMessage(channel=channel, thread_ts=parent_ts, text=_RECENT_CONTACT_USAGE)
        return

    # Phone calls, texts, and voicemails are auto-tracked by the firm's
    # phone system and flow directly into the Client Contact Status sheet,
    # so logging them here would be redundant. Gently redirect.
    if contact_type in _AUTO_TRACKED_CONTACT_TYPES:
        client.chat_postMessage(
            channel=channel, thread_ts=parent_ts,
            text=(
                f":telephone_receiver: *{contact_type.title()}* contacts are tracked "
                f"automatically — no need to log them via `recent contact`. "
                f"Use this command for email, in-person, mail, fax, or other contact "
                f"types that aren't picked up by the phone system."
            ),
        )
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

    icon = _CONTACT_ICONS.get(contact_type, ":pencil:")
    summary = f" — _{details}_" if details else ""

    def _write_and_reply():
        try:
            ok = recent_contact.log_contact(client, event, contact_type, details)
        except Exception:
            log.exception("recent contact write crashed")
            ok = False
        if ok:
            reply = f"{icon} Logged *{contact_type}*{summary}"
        else:
            reply = (
                f":warning: Couldn't write the *{contact_type}* entry to the Recent Contact sheet. "
                f"Usually a Google config issue — ask an admin to check the Railway logs and that the "
                f"Sheets API is enabled for the service account's GCP project."
            )
        try:
            client.chat_postMessage(channel=channel, thread_ts=parent_ts, text=reply)
        except Exception:
            log.exception("could not post recent contact reply")

    threading.Thread(target=_write_and_reply, daemon=True).start()


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
            f"• `@RJL-zap recent contact <type> - <details>` (or `client contact`) — log a client interaction\n"
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
        # Note: Disbursement no longer responds to @-mentions — it is
        # triggered by an authorized user posting "start disbursement"
        # as a plain channel message. See handle_message.
        # Order matters: 'paralegal intro' and 'attorney intro' share the suffix "intro",
        # and 'check pickup' must be matched before any potential collisions.
        followup_matches = [
            (PARALEGAL_INTRO, "paralegal_intro_escalation_user_ids", "paralegal_intro"),
            (ATTORNEY_INTRO, "attorney_intro_escalation_user_ids", "attorney_intro"),
            (CHECK_PICKUP, "check_pickup_backup_user_ids", "check_pickup"),
            (CASE_SETUP, "case_setup_escalation_user_ids", "case_setup"),
            (DOC_VERIFICATION, "doc_verification_escalation_user_ids", "doc_verification"),
            (CLIENT_INTAKE, "client_intake_escalation_user_ids", "client_intake"),
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
                f"`{ATTORNEY_INTRO.phrase}`",
                f"`{PARALEGAL_INTRO.phrase}`",
                f"`{CASE_SETUP.phrase}`",
                f"`{CLIENT_INTAKE.phrase}`",
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

    # Accept @-mentions with `complete`, `completed`, or `done` as
    # workflow-close keywords (matches the no-@-mention close handler
    # in handle_message — keep the keyword set the same for consistency).
    lowered_text = text.lower()
    if any(kw in lowered_text for kw in ("complete", "done")):
        wf = storage.workflow_by_thread(event["channel"], parent_ts)
        if wf and not wf.get("completed_at"):
            storage.force_complete_workflow(wf["id"])
            log.info("workflow %s (id=%s) closed via @-mention close keyword in channel=%s",
                     wf["trigger_name"], wf["id"], event["channel"])
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


def _start_disbursement(client, channel: str, user_id: str) -> None:
    """Auto-fire the 30-day disbursement sequence as a brand-new top-level
    thread. Triggered by a configured user posting `start disbursement`
    in any channel — no @-mention needed."""
    authorized = _ids_from_config("disbursement_authorized_user_ids")
    if not authorized:
        log.warning(
            "disbursement phrase posted by user=%s but "
            "disbursement_authorized_user_ids is empty — trigger disabled",
            user_id,
        )
        return
    if user_id not in authorized:
        log.warning(
            "unauthorized disbursement attempt by user=%s (authorized=%s)",
            user_id, authorized,
        )
        return
    log.info("auto-firing disbursement triggered by user=%s in channel=%s", user_id, channel)

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

    # Specific mentions for the per-step templates and the master overview:
    # {paralegal} and {attorney} — pulled from the channel topic via the same
    # regexes used by the intro auto-triggers. Fall back to plain "@paralegal"
    # / "@attorney" labels if the topic doesn't name one.
    paralegal_id = _first_match(_TOPIC_PARALEGAL_RE, topic, exclude=bot_id)
    attorney_id  = _first_match(_TOPIC_ATTORNEY_RE,  topic, exclude=bot_id)
    paralegal_mention = f"<@{paralegal_id}>" if paralegal_id else "@paralegal"
    attorney_mention  = f"<@{attorney_id}>"  if attorney_id  else "@attorney"
    # {ana}, {jon}, {laura} — fixed firm contacts configured in admin settings.
    # Stored as single user IDs (not lists). Fall back to literal labels
    # so the overview still reads sensibly if an admin hasn't filled them in.
    ana_id = storage.get_config("disbursement_ana_user_id").strip()
    jon_id = storage.get_config("disbursement_jon_user_id").strip()
    laura_id = storage.get_config("disbursement_laura_user_id").strip()
    ana_mention = f"<@{ana_id}>" if ana_id else "@ana"
    jon_mention = f"<@{jon_id}>" if jon_id else "@jon"
    laura_mention = f"<@{laura_id}>" if laura_id else "@laura"
    # {legalassistants} — same subteam mention the reminder loop uses for
    # checklist nudges, falling back to a plain label if no group is set.
    group_id = storage.get_config("notify_group_id")
    group_name = storage.get_config("notify_group_name", default="legalassistants")
    legalassistants_mention = (
        f"<!subteam^{group_id}|{group_name}>" if group_id else "@legalassistants"
    )

    def _render(template: str) -> str:
        return template.format(
            mentions=mention_str,
            paralegal=paralegal_mention,
            attorney=attorney_mention,
            ana=ana_mention,
            jon=jon_mention,
            laura=laura_mention,
            legalassistants=legalassistants_mention,
        )

    resp = client.chat_postMessage(
        channel=channel,
        text=_render(DISBURSEMENT_MASTER_CHECKLIST),
    )
    parent_ts = resp["ts"]
    if storage.workflow_by_thread(channel, parent_ts):
        return

    # Register the workflow FIRST so the deadline messages' skip-if-complete
    # lookup (keyed on parent_ts) resolves to a real workflow row.
    storage.create_workflow(channel, parent_ts, "disbursement", [])

    # Each step in the sequence posts as its OWN top-level channel message
    # (thread_ts="") so replies stay scoped to that specific task instead of
    # piling up under the master checklist.
    now = time.time()
    for delay, template in DISBURSEMENT.sequence:
        storage.schedule_message(
            channel_id=channel,
            thread_ts="",
            send_after=now + delay,
            text=_render(template),
        )

    # Deadline messages post IN the master thread and self-cancel if the
    # workflow has been closed (reply "complete" / "@RJL-zap COMPLETE" in the
    # master thread) before their fire time.
    for delay, template in DISBURSEMENT.deadline_sequence:
        storage.schedule_message(
            channel_id=channel,
            thread_ts=parent_ts,
            send_after=now + delay,
            text=_render(template),
            skip_if_complete_parent_ts=parent_ts,
        )

    log.info("started disbursement workflow channel=%s parent_ts=%s triggered_by=%s "
             "participants=%s paralegal=%s steps=%d deadline_steps=%d",
             channel, parent_ts, user_id, participants, paralegal_id,
             len(DISBURSEMENT.sequence), len(DISBURSEMENT.deadline_sequence))


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


def fire_due_deferred_actions(client) -> None:
    """Drain the deferred_actions queue. Currently only attorney_intro is
    deferred (1 hour after the paralegal is pinged), but the same path will
    handle any future kind by dispatching on `row['kind']`."""
    now = time.time()
    for row in storage.due_deferred_actions(now):
        kind = row["kind"]
        channel_id = row["channel_id"]
        target = row["target_user_id"]
        log.info("firing deferred action kind=%s channel=%s target=%s",
                 kind, channel_id, target)
        try:
            if kind == "attorney_intro":
                _auto_start_followup(
                    client, channel_id, ATTORNEY_INTRO,
                    "attorney_intro_escalation_user_ids", "attorney_intro",
                    participants=[target] if target else None,
                )
            else:
                log.warning("unknown deferred action kind=%s id=%s", kind, row["id"])
        except Exception:
            log.exception("deferred action failed kind=%s channel=%s id=%s",
                          kind, channel_id, row["id"])
        storage.mark_deferred_action_fired(row["id"])


# How often the Client Contact Status sweep runs. Sheet only needs to be
# polled occasionally — 24 h is plenty for "30-day no-contact" alerts.
_CLIENT_CONTACT_SWEEP_INTERVAL_SECONDS = 24 * 60 * 60
_CLIENT_CONTACT_LAST_SWEPT_KEY = "client_contact_last_swept_at"

# Client Contact alerts only fire during firm-local business hours so the
# team doesn't get pinged at midnight or on weekends. Mon-Fri, inclusive
# start, exclusive end (so 17 means "through 4:59 PM").
_CLIENT_CONTACT_BUSINESS_HOUR_START = 9   # 9 AM Central
_CLIENT_CONTACT_BUSINESS_HOUR_END   = 17  # 5 PM Central
try:
    from zoneinfo import ZoneInfo
    _CLIENT_CONTACT_TZ: object | None = ZoneInfo("America/Chicago")
except Exception:
    _CLIENT_CONTACT_TZ = None


def _within_business_hours() -> bool:
    """True iff firm-local time is a weekday between 9 AM and 5 PM Central.
    Falls back to True (no gating) if zoneinfo isn't available, so the
    sweep still functions in a degraded environment."""
    if _CLIENT_CONTACT_TZ is None:
        return True
    from datetime import datetime
    now = datetime.now(_CLIENT_CONTACT_TZ)
    if now.weekday() >= 5:  # 5 = Sat, 6 = Sun
        return False
    return _CLIENT_CONTACT_BUSINESS_HOUR_START <= now.hour < _CLIENT_CONTACT_BUSINESS_HOUR_END


def _channel_id_by_case_number(client) -> dict[str, str]:
    """Walk every public channel and build a {case_no: channel_id} map by
    parsing the trailing -<digits> from each channel name (firm convention).
    One call to conversations.list per page (~1000 channels each) — fine
    even at the firm's 1465+ channels."""
    result: dict[str, str] = {}
    cursor: str | None = None
    while True:
        try:
            resp = client.conversations_list(
                types="public_channel",
                exclude_archived=True,
                limit=1000,
                cursor=cursor,
            )
        except Exception:
            log.exception("conversations.list failed in case-number lookup")
            return result
        for ch in resp.get("channels", []):
            name = ch.get("name", "") or ""
            m = _CASE_NUMBER_RE.search(name)
            if m:
                # Match was r"-\d+$" — strip the leading dash.
                case_no = name[m.start() + 1 :]
                result.setdefault(case_no, ch["id"])
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return result


def fire_client_contact_alerts(client) -> None:
    """Once-daily sweep over the Client Contact Status sheet. Posts a
    30-day no-contact warning to each case channel that's lapsed, and a
    45-day reminder when the lapse continues. Tags users named in the
    channel topic. Deduped per (case, threshold, last_interaction) so a
    fresh contact entry re-arms the alert."""
    # Bail before touching the gate timestamp if the spreadsheet ID isn't
    # configured — otherwise the first tick after deploy would record "just
    # swept", and the sweep would stay silent for 24 h after the admin sets
    # the ID. Silent here (no log) — startup config snapshot already shows
    # whether the sheet is configured.
    if not storage.get_config("client_contact_spreadsheet_id").strip():
        return

    # Only fire alerts during firm-local business hours (Mon-Fri, 9 AM - 5 PM
    # Central). The 24 h gate doesn't get touched outside that window, so the
    # next eligible business-hours tick runs the sweep cleanly.
    if not _within_business_hours():
        return

    last_swept_raw = storage.get_config(_CLIENT_CONTACT_LAST_SWEPT_KEY, default="0")
    try:
        last_swept = float(last_swept_raw)
    except ValueError:
        last_swept = 0.0
    now = time.time()
    if now - last_swept < _CLIENT_CONTACT_SWEEP_INTERVAL_SECONDS:
        return

    log.info("client contact status sweep starting")

    import client_contact_status as ccs

    rows = list(ccs.iter_rows())
    if not rows:
        # Configured but unreadable (auth failure, wrong ID, sheet shared
        # with the wrong account, empty data). Do NOT update the gate —
        # we want the admin to be able to fix it and see results next tick.
        log.warning(
            "client contact sweep: 0 readable rows — sheet misconfigured, "
            "shared with the wrong account, or empty. Will retry next tick."
        )
        return

    # Configured AND read successfully — commit the gate so we wait 24 h
    # before the next sweep, regardless of whether anything was overdue.
    storage.set_config(_CLIENT_CONTACT_LAST_SWEPT_KEY, str(now))

    # The sheet is the source of truth for what's "active". Any case the bot
    # has alerted on in the past that's no longer in column A is treated as
    # deactivated — no more alerts will ever fire for it (the sweep below
    # only iterates rows that ARE in the sheet). We don't purge the dedup
    # records: if the case is added back later with the same Last Interaction
    # value, we'd rather stay quiet than fire a duplicate alert.
    active_case_nos = {r.case_no for r in rows}
    previously_alerted = storage.alerted_case_numbers()
    deactivated = previously_alerted - active_case_nos
    if deactivated:
        log.info(
            "client contact: %d case(s) previously alerted on are no longer in "
            "the sheet — treated as deactivated, no further alerts will fire",
            len(deactivated),
        )

    overdue = [r for r in rows if r.days_since_contact >= _CLIENT_CONTACT_THRESHOLDS[0]]
    if not overdue:
        log.info("client contact sweep: %d row(s), none overdue", len(rows))
        return

    by_case = _channel_id_by_case_number(client)
    log.info(
        "client contact sweep: %d active case(s) in sheet, %d overdue, "
        "%d case channels indexed",
        len(active_case_nos), len(overdue), len(by_case),
    )

    # Track posts per threshold for the summary log
    posted: dict[int, int] = {t: 0 for t in _CLIENT_CONTACT_THRESHOLDS}

    for row in overdue:
        channel_id = by_case.get(row.case_no)
        if not channel_id:
            log.info("client contact: no channel for case %s (%s)",
                     row.case_no, row.client_name)
            continue

        # The HIGHEST threshold the case has crossed is the one that fires.
        # (e.g. at 62 days both 25/30/45/60 apply — fire 60.) All lower
        # crossed thresholds are then back-recorded so they can never
        # back-fire later for this same Last Interaction.
        crossed = [t for t in _CLIENT_CONTACT_THRESHOLDS if row.days_since_contact >= t]
        if not crossed:
            continue
        current = crossed[-1]  # _CLIENT_CONTACT_THRESHOLDS is ascending

        if not storage.should_send_client_contact_alert(
            row.case_no, current, row.last_interaction
        ):
            continue

        text = _format_client_contact_text(client, channel_id, row, threshold=current)
        try:
            client.chat_postMessage(channel=channel_id, text=text)
            # Mark current AND all lower crossed thresholds as alerted, keyed
            # on this Last Interaction. Re-arms automatically when column F
            # updates in the sheet (new contact logged).
            for t in crossed:
                storage.record_client_contact_alert(row.case_no, t, row.last_interaction)
            posted[current] += 1
            log.info("client contact %dd alert posted case=%s channel=%s days=%d",
                     current, row.case_no, channel_id, row.days_since_contact)
        except Exception:
            log.exception("client contact %dd post failed case=%s channel=%s",
                          current, row.case_no, channel_id)

    summary = " ".join(f"{t}d={posted[t]}" for t in _CLIENT_CONTACT_THRESHOLDS)
    log.info("client contact sweep done: %s", summary)


# Ascending list of "no-contact" day thresholds. A case fires the alert for
# the HIGHEST threshold it has crossed; lower crossed thresholds are silently
# back-recorded so they can never back-fire later. Tweak this list to add
# / drop thresholds — _format_client_contact_text falls back to a generic
# template for any threshold without dedicated copy.
_CLIENT_CONTACT_THRESHOLDS = [25, 30, 45, 60, 75]


def _format_client_contact_text(client, channel_id: str, row, threshold: int) -> str:
    """Render the alert body, tagging every user mentioned in the channel
    topic. (The shared _topic_user_ids helper now reads the topic only —
    description/purpose is ignored everywhere.) Severity language escalates
    with the threshold."""
    topic_users = _topic_user_ids(client, channel_id)
    mention = " ".join(f"<@{uid}>" for uid in topic_users) if topic_users else "team"
    client_label = f"*{row.client_name}*" if row.client_name else "the client"
    last_clause = f" since {row.last_interaction}" if row.last_interaction else ""
    days = row.days_since_contact

    if threshold == 25:
        return (
            f":hourglass_flowing_sand: *Heads up: {days} days without contact* — {mention}\n\n"
            f"{client_label} hasn't been contacted{last_clause}. Please reach out before the "
            f"30-day mark to keep the case on track. Log the outreach with "
            f"`@RJL-zap recent contact <type> - <details>`."
        )
    if threshold == 30:
        return (
            f":warning: *Client not contacted in {days} days* — {mention}\n\n"
            f"{client_label} has not been contacted{last_clause}. "
            f"Please reach out and log the outcome with "
            f"`@RJL-zap recent contact <type> - <details>`."
        )
    if threshold == 45:
        return (
            f":rotating_light: *REMINDER: Client not contacted in {days} days* "
            f":rotating_light: — {mention}\n\n"
            f"{client_label} still has not been contacted{last_clause}. "
            f"Please reach out today and log the contact in the Recent Contact tracker."
        )
    if threshold == 60:
        return (
            f":no_entry: *URGENT: {days} days without contact* — {mention}\n\n"
            f"{client_label} has now gone {days} days without contact{last_clause}. "
            f"This case needs immediate attention. Please reach out today and document any "
            f"blockers in this thread."
        )
    if threshold == 75:
        return (
            f":bangbang: *CRITICAL: {days} days without contact* :bangbang: — {mention}\n\n"
            f"{client_label} has gone {days}+ days without contact{last_clause}. "
            f"Escalate to the supervising attorney and document why this case has lapsed."
        )
    # Generic fallback for any future threshold added to _CLIENT_CONTACT_THRESHOLDS
    # without dedicated copy.
    return (
        f":warning: *Client not contacted in {days} days* — {mention}\n\n"
        f"{client_label} has not been contacted{last_clause}. Please reach out and log "
        f"the outcome via `@RJL-zap recent contact`."
    )


def fire_due_lifecycle_triggers(client) -> None:
    """Called from the reminder loop. Fires new_case (T+180s),
    case_setup (T+15min), calendar_sol (T+15min), client_intake (T+1h),
    and doc_verification (T+48h) for any channel that's past its delay
    but hasn't fired yet."""
    now = time.time()

    # All automatic lifecycle alerts are hard-gated to channels created within
    # _STALE_CHANNEL_SECONDS (5 days). A channel older than that never fires,
    # even if a lifecycle row somehow has an unfired column.
    age = _STALE_CHANNEL_SECONDS
    new_case_due   = storage.lifecycle_due(now, "new_case", NEW_CASE_DELAY_SECONDS, max_age_seconds=age)
    case_setup_due = storage.lifecycle_due(now, "case_setup", CASE_SETUP_DELAY_SECONDS, max_age_seconds=age)
    doc_ver_due    = storage.lifecycle_due(now, "doc_verification", DOC_VERIFICATION_DELAY_SECONDS, max_age_seconds=age)
    intake_due     = storage.lifecycle_due(now, "client_intake", CLIENT_INTAKE_DELAY_SECONDS, max_age_seconds=age)
    sol_due        = storage.lifecycle_due(now, "calendar_sol", CALENDAR_SOL_DELAY_SECONDS, max_age_seconds=age)
    intake_assignees = _ids_from_config("client_intake_assignee_user_ids")

    if new_case_due or case_setup_due or doc_ver_due or intake_due or sol_due:
        log.info(
            "lifecycle sweep — new_case=%d case_setup=%d doc_verification=%d "
            "client_intake=%d (assignees_configured=%s) calendar_sol=%d",
            len(new_case_due),
            len(case_setup_due), len(doc_ver_due),
            len(intake_due), bool(intake_assignees),
            len(sol_due),
        )

    if NEW_CASE_ON_FIRST_MESSAGE:
        for row in new_case_due:
            if storage.mark_lifecycle_fired(row["channel_id"], "new_case"):
                log.info("auto-firing new_case for #%s (%ds after creation)",
                         row["channel_id"], NEW_CASE_DELAY_SECONDS)
                try:
                    _do_simple_post_top_level(client, row["channel_id"], NEW_CASE)
                except Exception:
                    log.exception("auto new_case failed for channel=%s", row["channel_id"])

    for row in case_setup_due:
        if storage.mark_lifecycle_fired(row["channel_id"], "case_setup"):
            try:
                _auto_start_followup(
                    client, row["channel_id"], CASE_SETUP,
                    "case_setup_escalation_user_ids", "case_setup",
                )
            except Exception:
                log.exception("auto case_setup failed for channel=%s", row["channel_id"])

    for row in doc_ver_due:
        if storage.mark_lifecycle_fired(row["channel_id"], "doc_verification"):
            try:
                _auto_start_followup(
                    client, row["channel_id"], DOC_VERIFICATION,
                    "doc_verification_escalation_user_ids", "doc_verification",
                )
            except Exception:
                log.exception("auto doc_verification failed for channel=%s", row["channel_id"])

    if intake_due and not intake_assignees:
        log.warning(
            "%d channel(s) past the 1-hour Client Intake delay, but "
            "'client_intake_assignee_user_ids' is empty — auto-trigger disabled. "
            "Set it in admin settings to enable.",
            len(intake_due),
        )
    if intake_assignees:
        for row in intake_due:
            if storage.mark_lifecycle_fired(row["channel_id"], "client_intake"):
                try:
                    _auto_start_followup(
                        client, row["channel_id"], CLIENT_INTAKE,
                        "client_intake_escalation_user_ids", "client_intake",
                        participants=intake_assignees,
                    )
                except Exception:
                    log.exception("auto client_intake failed for channel=%s", row["channel_id"])

    sol_trigger = TRIGGERS.get("calendar_sol")
    if sol_trigger is not None:
        for row in sol_due:
            if storage.mark_lifecycle_fired(row["channel_id"], "calendar_sol"):
                try:
                    _auto_start_trigger_workflow(client, row["channel_id"], sol_trigger)
                except Exception:
                    log.exception("auto calendar_sol failed for channel=%s", row["channel_id"])


def _render_trigger_announcement(trigger: TriggerConfig) -> str:
    """Substitute `{mentions}` in a trigger's announcement with the user IDs
    configured under `trigger.mentions_setting_key`. Falls back to an empty
    string if no setting key is set (so non-mention triggers pass through
    unchanged)."""
    if not trigger.mentions_setting_key:
        return trigger.announcement
    ids = _ids_from_config(trigger.mentions_setting_key)
    mention_str = " ".join(f"<@{uid}>" for uid in ids)
    return trigger.announcement.replace("{mentions}", mention_str).replace("  ", " ").strip()


def _auto_start_trigger_workflow(client, channel: str, trigger: TriggerConfig) -> None:
    """Auto-start a TriggerConfig workflow at the top level of a channel
    (no @-mention required). Posts the announcement as a new parent
    message, then runs the same item/reaction flow as _start_workflow."""
    resp = client.chat_postMessage(channel=channel, text=_render_trigger_announcement(trigger))
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
            f"or reply *done* / *complete* in this thread to close the checklist."
        ),
    )
    storage.create_workflow(channel, parent_ts, trigger.name, item_records)
    log.info("auto-started %s workflow channel=%s parent_ts=%s items=%d",
             trigger.name, channel, parent_ts, len(item_records))


def _start_workflow(client, channel: str, parent_ts: str, trigger: TriggerConfig) -> None:
    if storage.workflow_by_thread(channel, parent_ts):
        return

    client.chat_postMessage(channel=channel, thread_ts=parent_ts, text=_render_trigger_announcement(trigger))

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
            f"or reply *done* / *complete* in this thread to close the checklist."
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
    """When the bot itself joins a channel (manually or via auto-join), record
    lifecycle (so we don't miss new_case if channel_created was missed) and
    peek at the existing topic/purpose so we don't miss intros that were set
    before the bot was present."""
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
        name = ch.get("name", "") or ""
        topic   = (ch.get("topic")   or {}).get("value", "") or ""
        purpose = (ch.get("purpose") or {}).get("value", "") or ""
        created_at = float(ch.get("created") or time.time())
    except Exception:
        log.debug("conversations.info failed for %s", channel_id, exc_info=True)
        return

    _ensure_channel_lifecycle(channel_id, created_at, channel_name=name)

    if topic:
        _maybe_fire_intros_from_topic(client, channel_id, topic, channel_name=name)
    if purpose:
        _maybe_fire_intros_from_topic(client, channel_id, purpose, channel_name=name)


# A channel only qualifies for automatic alerts (lifecycle schedule + topic
# intros) if it was created within this window. Channels older than this are
# treated as "settled / pre-existing" — the bot still joins them and manual
# @-mention commands still work, but no automation fires. All lifecycle delays
# (max 48h) are well inside this window, so on-time fires are never blocked.
_STALE_CHANNEL_SECONDS = 5 * 24 * 60 * 60  # 5 days


def _ensure_channel_lifecycle(channel_id: str, created_at: float, channel_name: str = "") -> dict | None:
    """Idempotently record lifecycle for the channel. Pre-marks every
    lifecycle trigger as already fired in two cases:

      1. The channel is older than _STALE_CHANNEL_SECONDS (so joining via
         the startup auto-join sweep doesn't fire stale automations).
      2. The channel name doesn't end in -<digits> (firm convention for
         case channels — e.g. lacayoarauzjose-1559). Channels without a
         case number still get the bot, but lifecycle automations stay
         silent until someone runs a manual command.
    """
    storage.record_channel_created(channel_id, created_at)

    is_stale = time.time() - created_at > _STALE_CHANNEL_SECONDS
    # Only suppress on case-number rule when we actually know the name.
    # An empty name (e.g. from a fallback path that couldn't look it up)
    # falls through to the normal flow rather than silently suppressing.
    no_case_number = bool(channel_name) and not _has_case_number(channel_name)

    if is_stale or no_case_number:
        lc = storage.channel_lifecycle(channel_id) or {}
        if not lc.get("new_case_fired_at"):
            storage.mark_new_case_fired(channel_id)
        for kind in ("case_setup", "doc_verification", "calendar_sol", "client_intake"):
            if not lc.get(f"{kind}_fired_at"):
                storage.mark_lifecycle_fired(channel_id, kind)
        if no_case_number and not is_stale:
            log.info("lifecycle pre-suppressed — channel '%s' (%s) has no case number; "
                     "auto-triggers will stay silent",
                     channel_name, channel_id)

    return storage.channel_lifecycle(channel_id)


@app.event("channel_created")
def handle_channel_created(event, client):
    auto_join.handle_channel_created(event, client)
    ch = event.get("channel") or {}
    channel_id = ch.get("id")
    if channel_id:
        # Slack gives `created` as epoch seconds; fall back to now if missing.
        created_at = float(ch.get("created") or time.time())
        name = ch.get("name", "") or ""
        _ensure_channel_lifecycle(channel_id, created_at, channel_name=name)
        log.info("recorded channel lifecycle for #%s (%s) — case_number=%s, "
                 "new_case will fire in %ds (suppressed if no case number)",
                 name or "?", channel_id, _has_case_number(name), NEW_CASE_DELAY_SECONDS)


# Slack renders user mentions as either `<@U123>` or `<@U123|displayName>` —
# match both forms.
_TOPIC_ATTORNEY_RE  = re.compile(r"attorney[^A-Za-z<]*<@([A-Z0-9]+)(?:\|[^>]*)?>", re.IGNORECASE)
_TOPIC_PARALEGAL_RE = re.compile(r"paralegal[^A-Za-z<]*<@([A-Z0-9]+)(?:\|[^>]*)?>", re.IGNORECASE)
# Case channels end in -<digits>, e.g. "lacayoarauzjose-1559". Auto-triggers
# only fire for matching channels; everything else still gets the bot but
# stays silent until someone runs a manual @-mention command.
_CASE_NUMBER_RE     = re.compile(r"-\d+$")
# Match a thread reply that *starts* with done / complete / completed (so
# "done" / "Done." / "complete!" / "COMPLETED" / "complete all 3" all close
# the workflow, but "I'm done" / "halfway done" / "almost complete" do not).
# @-mentions at the start of the message are stripped before matching, so
# "@RJL-zap done" works too.
_CLOSE_REPLY_RE     = re.compile(r"^\s*(?:done|complete|completed)\b", re.IGNORECASE)
_LEADING_MENTION_RE = re.compile(r"^\s*(?:<@[A-Z0-9]+(?:\|[^>]*)?>\s*)+")


def _maybe_fire_intros_from_topic(client, channel_id: str, topic_text: str,
                                  channel_name: str | None = None) -> None:
    """Parse the channel topic/purpose for `Attorney @X` / `Paralegal @Y`
    mentions and auto-fire the respective intro workflows for those people.

    Two gates:
      1. Case-number convention — channel name must end in -<digits>.
      2. Freshness — the channel must have been created recently (within
         _STALE_CHANNEL_SECONDS). Intros are meant to fire when a *new* case
         channel first gets its attorney/paralegal assigned. Editing the
         topic of an existing/settled channel (e.g. appending "(settled)")
         must NOT fire intros or kick off any lifecycle automation.
    """
    if not channel_id or not topic_text:
        return

    name, created_at = _lookup_channel_meta(client, channel_id)
    if not name and channel_name:
        name = channel_name

    if not _has_case_number(name):
        log.info(
            "intros skipped — channel '%s' (%s) has no case number; "
            "manual @-mention still works",
            name, channel_id,
        )
        return

    if created_at and (time.time() - created_at > _STALE_CHANNEL_SECONDS):
        age_h = (time.time() - created_at) / 3600
        log.info(
            "intros skipped — channel '%s' (%s) was created %.0fh ago, not a "
            "new case channel (topic edit, not initial assignment); "
            "manual @-mention still works",
            name, channel_id, age_h,
        )
        return

    # Make sure the lifecycle row reflects the channel's REAL creation time so
    # this topic event doesn't (re-)arm the time-based schedule. For a genuine
    # new case channel the row already exists from handle_channel_created and
    # this is a no-op; for anything else _ensure_channel_lifecycle suppresses.
    if created_at:
        _ensure_channel_lifecycle(channel_id, created_at, channel_name=name)

    bot_id = client.auth_test()["user_id"]
    attorney_id  = _first_match(_TOPIC_ATTORNEY_RE,  topic_text, exclude=bot_id)
    paralegal_id = _first_match(_TOPIC_PARALEGAL_RE, topic_text, exclude=bot_id)
    log.info(
        "topic/purpose parse for #%s — attorney=%s paralegal=%s text=%r",
        channel_id, attorney_id, paralegal_id, topic_text[:300],
    )

    # Paralegal intro fires immediately so the paralegal starts client contact
    # right away. Attorney intro is deferred by ATTORNEY_INTRO.initial_delay_seconds
    # (1 hour) so the attorney isn't pinged at the same instant — gives the
    # paralegal time to make first contact before the attorney is paged.
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

    if attorney_id and storage.set_intro_fired_for(channel_id, "attorney", attorney_id):
        delay = max(0.0, float(ATTORNEY_INTRO.initial_delay_seconds))
        if delay <= 0:
            log.info("auto-firing attorney_intro for %s in #%s", attorney_id, channel_id)
            try:
                _auto_start_followup(
                    client, channel_id, ATTORNEY_INTRO,
                    "attorney_intro_escalation_user_ids", "attorney_intro",
                    participants=[attorney_id],
                )
            except Exception:
                log.exception("auto attorney_intro failed for channel=%s", channel_id)
        else:
            log.info("deferring attorney_intro for %s in #%s by %.0fs",
                     attorney_id, channel_id, delay)
            storage.schedule_deferred_action(
                channel_id, "attorney_intro", attorney_id, time.time() + delay,
            )


def _lookup_channel_name(client, channel_id: str) -> str:
    return _lookup_channel_meta(client, channel_id)[0]


def _attorney_from_channel_topic(client, channel_id: str) -> str | None:
    """Pull the attorney user ID from the channel topic, e.g.
    "Attorney @Jesus | Paralegal @Lyliana" → U_JESUS. None on miss or
    lookup failure."""
    try:
        info = client.conversations_info(channel=channel_id)
        topic = ((info.get("channel") or {}).get("topic") or {}).get("value", "") or ""
    except Exception:
        log.debug("topic lookup failed for %s", channel_id, exc_info=True)
        return None
    if not topic:
        return None
    try:
        bot_id = client.auth_test()["user_id"]
    except Exception:
        bot_id = None
    return _first_match(_TOPIC_ATTORNEY_RE, topic, exclude=bot_id)


def _lookup_channel_meta(client, channel_id: str) -> tuple[str, float]:
    """Return (name, created_at_epoch) for a channel. created_at is 0.0 if
    the lookup fails or Slack omits it."""
    try:
        info = client.conversations_info(channel=channel_id)
        ch = info.get("channel") or {}
        return (ch.get("name", "") or "", float(ch.get("created") or 0))
    except Exception:
        log.debug("could not fetch channel meta for %s", channel_id, exc_info=True)
        return ("", 0.0)


def _has_case_number(channel_name: str) -> bool:
    return bool(_CASE_NUMBER_RE.search(channel_name or ""))


def _topic_user_ids(client, channel_id: str) -> list[str]:
    """Return the @-mentioned user IDs from the channel TOPIC only,
    de-duplicated, with the bot itself filtered out. The channel
    description/purpose is intentionally ignored — the firm uses the
    topic as the single source of truth for who works the case, and
    stale or different mentions in the description must never cause
    the wrong people to be pinged."""
    try:
        info = client.conversations_info(channel=channel_id)
        ch = info.get("channel") or {}
        topic = (ch.get("topic") or {}).get("value", "") or ""
    except Exception:
        log.debug("conversations.info failed for %s", channel_id, exc_info=True)
        return []
    try:
        bot_id = client.auth_test()["user_id"]
    except Exception:
        bot_id = None
    seen: list[str] = []
    for uid in re.findall(r"<@([A-Z0-9]+)(?:\|[^>]*)?>", topic):
        if uid != bot_id and uid not in seen:
            seen.append(uid)
    return seen


def _first_match(pattern, text: str, exclude: str | None = None) -> str | None:
    for m in pattern.finditer(text):
        uid = m.group(1)
        if uid != exclude:
            return uid
    return None


# Belt-and-suspenders subtype-specific routes. The general @app.event("message")
# handler below also catches these, but in some Bolt deployments the subtype
# router fires while the generic handler doesn't (or vice-versa). Both running
# is safe because _maybe_fire_intros_from_topic / set_intro_fired_for are
# idempotent — the second call is a no-op.
@app.event({"type": "message", "subtype": "channel_topic"})
def handle_channel_topic_event(event, client):
    channel_id = event.get("channel")
    if not channel_id:
        return
    log.info("explicit channel_topic event channel=%s topic=%r text=%r",
             channel_id, (event.get("topic") or "")[:200], (event.get("text") or "")[:200])
    combined = f"{event.get('text') or ''}\n{event.get('topic') or ''}"
    _maybe_fire_intros_from_topic(client, channel_id, combined)


@app.event({"type": "message", "subtype": "channel_purpose"})
def handle_channel_purpose_event(event, client):
    channel_id = event.get("channel")
    if not channel_id:
        return
    log.info("explicit channel_purpose event channel=%s purpose=%r text=%r",
             channel_id, (event.get("purpose") or "")[:200], (event.get("text") or "")[:200])
    combined = f"{event.get('text') or ''}\n{event.get('purpose') or ''}"
    _maybe_fire_intros_from_topic(client, channel_id, combined)


@app.event("message")
def handle_message(event, client):
    """Handles channel topic/purpose updates and the keyword-driven
    Mediation Checklist / Check Pickup / Review Request triggers."""
    subtype = event.get("subtype")
    channel_id = event.get("channel")
    if not channel_id:
        return

    # Diagnostic: log every subtype event the bot receives. Normal user
    # messages (no subtype) are NOT logged here to avoid spam — they'll
    # surface via the trigger-phrase-detected line further down if
    # applicable.
    if subtype:
        log.info("subtype message event — subtype=%s channel=%s", subtype, channel_id)

    # Channel description / topic was set or changed → parse for intros.
    # Slack populates the `text` field with @-mention substitutions (`<@USERID>`)
    # but the bare `topic`/`purpose` field is the literal stored value, which
    # may or may not have substitutions depending on how it was set. Pass both
    # so the regex has the best chance of matching.
    if subtype == "channel_topic":
        log.info("handling channel_topic in channel=%s topic=%r text=%r",
                 channel_id, (event.get("topic") or "")[:200], (event.get("text") or "")[:200])
        combined = f"{event.get('text') or ''}\n{event.get('topic') or ''}"
        _maybe_fire_intros_from_topic(client, channel_id, combined)
        return
    if subtype == "channel_purpose":
        log.info("handling channel_purpose in channel=%s purpose=%r text=%r",
                 channel_id, (event.get("purpose") or "")[:200], (event.get("text") or "")[:200])
        combined = f"{event.get('text') or ''}\n{event.get('purpose') or ''}"
        _maybe_fire_intros_from_topic(client, channel_id, combined)
        return

    # Skip bot messages and message subtypes (channel_join, file_share, etc.).
    # NOTE: we deliberately do NOT skip thread replies here — phrase triggers
    # (start disbursement / law firm can be paid / RJL has been paid /
    # mediation checklist) should fire even when posted inside a thread.
    if event.get("bot_id") or subtype:
        return

    text = event.get("text") or ""
    lowered = text.lower()

    # Thread reply starting with done / complete / completed → close the
    # workflow for this thread (same effect as @-mention COMPLETE, but no
    # @-mention required). Works for Calendar SOL, Calendar Checklists,
    # Mediation, and any followup workflow record. Leading @-mentions are
    # stripped so "@RJL-zap done" also closes.
    thread_ts = event.get("thread_ts")
    if thread_ts:
        cleaned = _LEADING_MENTION_RE.sub("", text)
        if _CLOSE_REPLY_RE.match(cleaned):
            log.info(
                "close keyword reply detected channel=%s thread_ts=%s author=%s text=%r",
                channel_id, thread_ts, event.get("user", ""), text[:120],
            )
            wf = storage.workflow_by_thread(channel_id, thread_ts)
            if wf and not wf.get("completed_at"):
                storage.force_complete_workflow(wf["id"])
                log.info("workflow %s (id=%s) closed via close-keyword reply in channel=%s",
                         wf["trigger_name"], wf["id"], channel_id)
                try:
                    client.chat_postMessage(
                        channel=channel_id, thread_ts=thread_ts,
                        text=":tada: Marked complete. Closing checklist.",
                    )
                except Exception:
                    log.exception("could not post done-close confirmation")
                return
            elif not wf:
                log.info(
                    "close keyword reply but no workflow found for channel=%s thread_ts=%s",
                    channel_id, thread_ts,
                )

    # Diagnostic: log whenever any auto-trigger phrase appears in a user
    # message, so a future "trigger didn't fire" report can be debugged
    # from Railway logs (author, thread context, raw text).
    if (MEDIATION.phrase in lowered
            or DISBURSEMENT.phrase in lowered
            or CHECK_PICKUP_AUTO_PHRASE in lowered
            or REVIEW_REQUEST_AUTO_PHRASE.lower() in lowered):
        log.info(
            "trigger phrase detected channel=%s author=%s in_thread=%s text=%r",
            channel_id, event.get("user", ""),
            bool(event.get("thread_ts")), text[:200],
        )

    # Auto-fire Mediation Checklist on any message containing the phrase
    # (matches the legacy Zapier "search for 'Mediation Checklist'" trigger).
    # Skip if the bot is @-mentioned in this message — app_mention is
    # already handling it.
    if MEDIATION.phrase in lowered and not _bot_is_mentioned(client, text):
        _start_mediation(client, channel_id, event["ts"], text)
        return

    # Auto-fire 30-Day Disbursement when an authorized user posts "start disbursement"
    # No @-mention required — the message itself is the trigger.
    if DISBURSEMENT.phrase in lowered:
        author = event.get("user", "")
        if author and not _bot_is_mentioned(client, text):
            try:
                _start_disbursement(client, channel_id, author)
            except Exception:
                log.exception("auto disbursement failed for channel=%s", channel_id)
            return

    # Auto-fire Check Pickup when a configured user posts the trigger phrase
    # (legacy Zapier "law firm can be paid" from:@paralegal trigger).
    if CHECK_PICKUP_AUTO_PHRASE in lowered:
        trigger_users = _ids_from_config("check_pickup_trigger_user_ids")
        author = event.get("user", "")
        if trigger_users and author in trigger_users and not _bot_is_mentioned(client, text):
            # Tag the triggering user AND the attorney from the channel topic
            # (if one is named), so the supervising attorney is in the loop
            # from the moment the pickup is needed — not only when the 5-day
            # backup escalation fires.
            attorney_id = _attorney_from_channel_topic(client, channel_id)
            participants = [author]
            if attorney_id and attorney_id != author:
                participants.append(attorney_id)
            log.info("auto-firing check_pickup from user=%s in #%s (attorney=%s)",
                     author, channel_id, attorney_id)
            try:
                _auto_start_followup(
                    client, channel_id, CHECK_PICKUP,
                    "check_pickup_backup_user_ids", "check_pickup",
                    participants=participants,
                )
            except Exception:
                log.exception("auto check_pickup failed for channel=%s", channel_id)
            return

    # New Case Assignment fires on channel creation (see handle_channel_created /
    # handle_member_joined) — no first-message hook here.

    # Auto-fire 5-star review prompt when a configured user posts "RJL has been paid"
    # (3-minute delay so the message lands after the case is settled in Slack)
    if REVIEW_REQUEST_AUTO_PHRASE.lower() in lowered:
        trigger_users = _ids_from_config("review_request_trigger_user_ids")
        author = event.get("user", "")
        if trigger_users and author in trigger_users and not _bot_is_mentioned(client, text):
            log.info("auto-firing review_request from user=%s in #%s", author, channel_id)
            try:
                topic_ids = _topic_user_ids(client, channel_id)
                mention_str = " ".join(f"<@{uid}>" for uid in topic_ids)
                extras = " ".join(f"<@{uid}>" for uid in _ids_from_config(REVIEW_REQUEST.extras_setting_key))
                msg_text = (
                    REVIEW_REQUEST.message
                    .replace("{mentions}", mention_str)
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
    _migrate_client_contact_gate()
    _log_startup_config()
    reminders.start_reminder_loop(app.client)
    threading.Thread(target=web.start, daemon=True).start()
    if os.getenv("AUTO_JOIN_CHANNELS", "1") not in ("0", "false", "False", ""):
        auto_join.join_all_public_channels_async(app.client)
    log.info("Starting bot in Socket Mode")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


def _migrate_client_contact_gate() -> None:
    """One-shot: the first deploy of the Client Contact sweep had a bug
    where the gate timestamp got recorded even when the spreadsheet ID
    wasn't yet configured — so the first tick locked the sweep out for
    24 hrs. This clears the stale timestamp once on the first boot after
    the fix lands, so the next tick can run the sweep cleanly."""
    marker = "client_contact_gate_v2_migrated"
    if storage.get_config(marker):
        return
    storage.set_config(_CLIENT_CONTACT_LAST_SWEPT_KEY, "0")
    storage.set_config(marker, str(time.time()))
    log.info("client contact gate timestamp reset (one-shot v2 migration)")


def _log_startup_config() -> None:
    """One-line snapshot of which auto-triggers will actually fire, so
    misconfigured settings are visible on every deploy."""
    keys = [
        ("client_intake_assignee_user_ids",  "Client Intake auto-trigger"),
        ("disbursement_authorized_user_ids", "Disbursement phrase trigger"),
        ("check_pickup_trigger_user_ids",    "Check Pickup phrase trigger"),
        ("review_request_trigger_user_ids",  "Review Request phrase trigger"),
        ("case_setup_participant_user_ids",  "Case Setup / Doc Verification participants"),
    ]
    for key, label in keys:
        ids = _ids_from_config(key)
        log.info("config check — %s: %s (key=%s)",
                 label, f"{len(ids)} user(s) configured" if ids else "DISABLED (empty)", key)


if __name__ == "__main__":
    main()
