import logging
import os

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import reminders
import storage
from config import COMPLETION_EMOJI, COMPLETION_REPLY, TRIGGERS, TriggerConfig

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("calendar-bot")

app = App(token=os.environ["SLACK_BOT_TOKEN"])


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
        trigger = _find_trigger(text)
        if trigger:
            _start_workflow(client, event["channel"], event["ts"], trigger)
        else:
            client.chat_postMessage(
                channel=event["channel"],
                thread_ts=event["ts"],
                text=(
                    "I didn't recognize a calendaring trigger in that message. "
                    "Try mentioning me with one of: "
                    + ", ".join(f"`{t.phrase}`" for t in TRIGGERS.values())
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
                text=":tada: All calendaring items marked complete. Closing checklist.",
            )


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
    log.info(
        "started workflow channel=%s parent_ts=%s trigger=%s items=%d",
        channel, parent_ts, trigger.name, len(item_records),
    )


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
        text=":tada: All calendaring items complete. Nice work!",
    )


def main() -> None:
    storage.init_db()
    reminders.start_reminder_loop(app.client)
    log.info("Starting bot in Socket Mode")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
