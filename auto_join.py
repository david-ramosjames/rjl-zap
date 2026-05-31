"""Auto-joins the bot to every public Slack channel — both at startup
(catching up on existing channels) and live as new ones are created."""

import logging
import threading
import time

log = logging.getLogger(__name__)

# Slack rate-limits conversations.join at Tier 3 (~50/min). Be polite.
_JOIN_THROTTLE_SECONDS = 1.2


def join_all_public_channels(client) -> None:
    """Iterate every public channel in the workspace and join any the bot isn't in."""
    cursor = None
    joined = 0
    skipped = 0
    failed = 0

    while True:
        try:
            resp = client.conversations_list(
                types="public_channel",
                exclude_archived=True,
                limit=200,
                cursor=cursor,
            )
        except Exception:
            log.exception("conversations.list failed — aborting auto-join sweep")
            return

        for ch in resp.get("channels", []):
            if ch.get("is_archived"):
                continue
            if ch.get("is_member"):
                skipped += 1
                continue
            try:
                client.conversations_join(channel=ch["id"])
                log.info("auto-joined #%s (%s)", ch.get("name", "?"), ch["id"])
                joined += 1
            except Exception as e:
                # Already in / can't join / archived — log and move on
                log.warning("could not join #%s (%s): %s",
                            ch.get("name", "?"), ch["id"], e)
                failed += 1
            time.sleep(_JOIN_THROTTLE_SECONDS)

        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break

    log.info("auto-join sweep complete: joined=%d already_in=%d failed=%d",
             joined, skipped, failed)


def join_all_public_channels_async(client) -> None:
    """Run the startup sweep in a daemon thread so we don't block app boot."""
    threading.Thread(target=join_all_public_channels, args=(client,), daemon=True).start()


def handle_channel_created(event, client) -> None:
    """Slack `channel_created` event handler — join the new channel immediately."""
    ch = event.get("channel") or {}
    channel_id = ch.get("id")
    name = ch.get("name", "?")
    if not channel_id:
        return
    try:
        client.conversations_join(channel=channel_id)
        log.info("auto-joined new channel #%s (%s)", name, channel_id)
    except Exception as e:
        log.warning("could not auto-join new channel #%s (%s): %s", name, channel_id, e)
