import logging
import threading
import time

import storage
from config import NOTIFY_GROUP_MENTION, REMINDER_CHECK_INTERVAL_SECONDS, REMINDER_INTERVAL_HOURS

log = logging.getLogger(__name__)


def start_reminder_loop(client) -> None:
    threading.Thread(target=_loop, args=(client,), daemon=True).start()


def _loop(client) -> None:
    while True:
        try:
            _tick(client)
        except Exception:
            log.exception("reminder tick failed")
        time.sleep(REMINDER_CHECK_INTERVAL_SECONDS)


def _thread_has_reply(client, channel_id: str, thread_ts: str, keyword: str) -> bool:
    try:
        resp = client.conversations_replies(channel=channel_id, ts=thread_ts)
        messages = resp.get("messages", [])
        kw = keyword.lower()
        # Skip the first message (the parent); ignore bot messages
        return any(
            kw in (m.get("text") or "").lower()
            for m in messages[1:]
            if not m.get("bot_id")
        )
    except Exception:
        log.exception("could not fetch thread replies channel=%s ts=%s", channel_id, thread_ts)
        return False


def _tick(client) -> None:
    now = time.time()

    # Fire any scheduled follow-up messages (e.g. mediation sequence, escalations)
    for msg in storage.due_scheduled_messages(now):
        try:
            if msg["check_replies_first"] and msg["done_keyword"]:
                if _thread_has_reply(client, msg["channel_id"], msg["thread_ts"], msg["done_keyword"]):
                    log.info("thread already has '%s' reply — skipping scheduled msg id=%s",
                             msg["done_keyword"], msg["id"])
                    storage.mark_scheduled_sent(msg["id"])
                    continue
            client.chat_postMessage(
                channel=msg["channel_id"],
                thread_ts=msg["thread_ts"],
                text=msg["text"],
            )
            storage.mark_scheduled_sent(msg["id"])
        except Exception:
            log.exception("failed to send scheduled message id=%s", msg["id"])

    # Periodic reminders for open reaction-tracked checklists
    cutoff = now - REMINDER_INTERVAL_HOURS * 3600
    for wf in storage.open_workflows_due_for_reminder(cutoff):
        if wf["trigger_name"] == "mediation_checklist":
            continue
        open_items = storage.workflow_open_items(wf["id"])
        if not open_items:
            storage.mark_workflow_complete(wf["id"])
            continue
        bullets = "\n".join(f"• {i['item_text']}" for i in open_items)
        mention = f"{NOTIFY_GROUP_MENTION} " if NOTIFY_GROUP_MENTION else ""
        client.chat_postMessage(
            channel=wf["channel_id"],
            thread_ts=wf["parent_ts"],
            text=(
                f":alarm_clock: {mention}Reminder — {len(open_items)} item(s) still need to be calendared:\n"
                f"{bullets}"
            ),
        )
        storage.update_last_reminded(wf["id"])
