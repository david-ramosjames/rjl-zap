import logging
import os
import re
import time
import threading

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import reminders
import storage
import web
from config import (
    ATTORNEY_INTRO, CASE_SETUP,
    COMPLETION_EMOJI, COMPLETION_REPLY,
    DISBURSEMENT, DISBURSEMENT_MASTER_CHECKLIST,
    FollowUpConfig,
    MEDIATION, TRIGGERS, TriggerConfig,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("calendar-bot")

app = App(token=os.environ["SLACK_BOT_TOKEN"])


def _ids_from_config(key: str) -> list[str]:
    raw = storage.get_config(key)
    return [uid.strip() for uid in raw.split(",") if uid.strip()]


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
        if MEDIATION.phrase in lowered:
            _start_mediation(client, event["channel"], event["ts"], text)
            return
        if DISBURSEMENT.phrase in lowered:
            _start_disbursement(client, event["channel"], event["ts"], event.get("user", ""))
            return
        if ATTORNEY_INTRO.phrase in lowered:
            _start_followup_workflow(
                client, event["channel"], event["ts"], text,
                ATTORNEY_INTRO, _ids_from_config("attorney_intro_escalation_user_ids"),
                "attorney_intro",
            )
            return
        if CASE_SETUP.phrase in lowered:
            _start_followup_workflow(
                client, event["channel"], event["ts"], text,
                CASE_SETUP, _ids_from_config("case_setup_escalation_user_ids"),
                "case_setup",
            )
            return
        trigger = _find_trigger(text)
        if trigger:
            _start_workflow(client, event["channel"], event["ts"], trigger)
        else:
            all_phrases = (
                [
                    f"`{MEDIATION.phrase}`",
                    f"`{DISBURSEMENT.phrase}`",
                    f"`{ATTORNEY_INTRO.phrase}`",
                    f"`{CASE_SETUP.phrase}`",
                ]
                + [f"`{t.phrase}`" for t in TRIGGERS.values()]
            )
            client.chat_postMessage(
                channel=event["channel"],
                thread_ts=event["ts"],
                text=(
                    "I didn't recognize a trigger in that message. "
                    "Try mentioning me with one of: " + ", ".join(all_phrases)
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
    log.info("Starting bot in Socket Mode")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
