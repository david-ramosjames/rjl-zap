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


def _tick(client) -> None:
    now = time.time()

    # Fire any scheduled follow-up messages (e.g. mediation sequence)
    for msg in storage.due_scheduled_messages(now):
        try:
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
