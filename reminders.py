import logging
import threading
import time

import storage
from config import REMINDER_CHECK_INTERVAL_SECONDS

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


def _thread_has_reply(client, channel_id: str, thread_ts: str, keyword: str):
    """Returns True if the thread has a non-bot reply containing `keyword`,
    False if it definitely does not, or None if we couldn't tell (API error).
    Callers should treat None as 'don't escalate yet — try again next tick'
    so a transient Slack outage doesn't fire a false escalation."""
    try:
        resp = client.conversations_replies(channel=channel_id, ts=thread_ts, limit=200)
    except Exception:
        log.exception("could not fetch thread replies channel=%s ts=%s", channel_id, thread_ts)
        return None

    try:
        bot_user_id = client.auth_test().get("user_id")
    except Exception:
        bot_user_id = None

    messages = resp.get("messages", []) or []
    kw = keyword.lower()
    matches: list[str] = []
    skipped_bot = 0
    for m in messages[1:]:  # messages[0] is the parent
        if m.get("bot_id") or (bot_user_id and m.get("user") == bot_user_id):
            skipped_bot += 1
            continue
        text = (m.get("text") or "").lower()
        if kw in text:
            matches.append(m.get("ts", "?"))

    log.info(
        "thread reply check channel=%s ts=%s keyword=%s — replies=%d "
        "skipped_bot=%d matches=%s",
        channel_id, thread_ts, keyword,
        max(0, len(messages) - 1),
        skipped_bot, matches or "[]",
    )
    return bool(matches)


def _sol_assignee_mention() -> str:
    """Return the space-joined @-mention string for users configured under
    Calendar SOL — Assigned User(s). Empty string if nobody is set."""
    raw = storage.get_config("calendar_sol_user_ids", default="") or ""
    ids = [uid.strip() for uid in raw.split(",") if uid.strip()]
    return " ".join(f"<@{uid}>" for uid in ids)


def _tick(client) -> None:
    now = time.time()

    # Fire any auto channel-lifecycle triggers (case setup, doc verification),
    # any deferred per-channel actions (currently: attorney_intro, scheduled
    # 1 hr after the paralegal intro fires), and the once-daily Client Contact
    # Status sweep (30 / 45 day no-contact alerts read from the Google Sheet).
    try:
        import app  # late import — avoids circular dep at module load
        app.fire_due_lifecycle_triggers(client)
        app.fire_due_deferred_actions(client)
        app.fire_client_contact_alerts(client)
    except Exception:
        log.exception("lifecycle/deferred/client-contact trigger sweep failed")

    # Fire any scheduled follow-up messages (e.g. mediation sequence, escalations)
    for msg in storage.due_scheduled_messages(now):
        try:
            if msg["check_replies_first"] and msg["done_keyword"]:
                result = _thread_has_reply(
                    client, msg["channel_id"], msg["thread_ts"], msg["done_keyword"],
                )
                if result is None:
                    # API failure — don't escalate blind. Leave the row unsent
                    # so we retry on the next tick.
                    log.warning(
                        "deferring scheduled msg id=%s — thread reply check failed",
                        msg["id"],
                    )
                    continue
                if result:
                    log.info("thread already has '%s' reply — skipping scheduled msg id=%s",
                             msg["done_keyword"], msg["id"])
                    storage.mark_scheduled_sent(msg["id"])
                    continue
            post_kwargs: dict = {"channel": msg["channel_id"], "text": msg["text"]}
            if msg["thread_ts"]:
                post_kwargs["thread_ts"] = msg["thread_ts"]
            client.chat_postMessage(**post_kwargs)
            storage.mark_scheduled_sent(msg["id"])
        except Exception:
            log.exception("failed to send scheduled message id=%s", msg["id"])

    # Periodic reminders for open reaction-tracked checklists
    reminder_hours = float(storage.get_config("reminder_interval_hours", default="24"))
    cutoff = now - reminder_hours * 3600
    group_id = storage.get_config("notify_group_id")
    group_name = storage.get_config("notify_group_name", default="legalassistants")
    notify_mention = f"<!subteam^{group_id}|{group_name}> " if group_id else ""

    skip_triggers = {
        "mediation_checklist", "disbursement",
        "attorney_intro", "case_setup", "paralegal_intro", "check_pickup",
        "doc_verification", "client_intake",
    }
    for wf in storage.open_workflows_due_for_reminder(cutoff):
        if wf["trigger_name"] in skip_triggers:
            continue
        open_items = storage.workflow_open_items(wf["id"])
        if not open_items:
            storage.mark_workflow_complete(wf["id"])
            continue
        bullets = "\n".join(f"• {i['item_text']}" for i in open_items)
        # Calendar SOL reminder tags the same users as the initial announcement;
        # everything else falls back to the legalassistants group.
        if wf["trigger_name"] == "calendar_sol":
            sol_mention = _sol_assignee_mention()
            mention = f"{sol_mention} " if sol_mention else notify_mention
        else:
            mention = notify_mention
        client.chat_postMessage(
            channel=wf["channel_id"],
            thread_ts=wf["parent_ts"],
            text=(
                f":alarm_clock: {mention}Reminder — {len(open_items)} item(s) still need to be calendared:\n"
                f"{bullets}"
            ),
        )
        storage.update_last_reminded(wf["id"])
