import os
from dataclasses import dataclass, field
from typing import Dict, List, Tuple


@dataclass
class TriggerConfig:
    name: str
    phrase: str            # case-insensitive substring match against the @-mention text
    announcement: str      # parent message posted as a thread reply
    items: List[str]       # checklist items, each posted as its own thread reply


TRIGGERS: Dict[str, TriggerConfig] = {
    "answer_filed": TriggerConfig(
        name="answer_filed",
        phrase="answer filed",
        announcement=":rotating_light: *Answer filed detected* — starting calendaring checklist",
        items=[
            ":date: Calendar Initial Disclosures",
            ":date: Calendar Chapter 18 deadline",
            ":date: Calendar controverting affidavit deadline",
            ":date: Update case management calendar",
        ],
    ),
    "discovery_received": TriggerConfig(
        name="discovery_received",
        phrase="discovery requests received",
        announcement=":rotating_light: *Discovery requests received* — starting calendaring checklist",
        items=[
            ":date: Calendar 30-day response deadline",
            ":date: Calendar internal draft review deadline",
            ":date: Assign drafter for responses",
        ],
    ),
    "scheduling_order": TriggerConfig(
        name="scheduling_order",
        phrase="scheduling order agreed",
        announcement=":rotating_light: *Scheduling order agreed* — starting calendaring checklist",
        items=[
            ":date: Calendar discovery cutoff",
            ":date: Calendar expert disclosure deadlines",
            ":date: Calendar dispositive motion deadline",
            ":date: Calendar trial date",
        ],
    ),
}

@dataclass
class MediationConfig:
    phrase: str
    checklist: List[str]
    # (delay_seconds, message_text) — {mentions} is replaced with the tagged users
    followups: List[Tuple[float, str]] = field(default_factory=list)


MEDIATION = MediationConfig(
    phrase="mediation checklist",
    checklist=[
        ":white_check_mark: Confirm mediation is calendared",
        ":white_check_mark: Schedule client prep session",
        ":white_check_mark: Reserve conference room",
        ":white_check_mark: Send invoice to client",
        ":white_check_mark: Confirm mediator payment",
    ],
    followups=[
        (1 * 24 * 3600,  ":bell: {mentions} — quick check-in: have the prep steps above been completed?"),
        (3 * 24 * 3600,  ":receipt: {mentions} — what's the status of the invoice to the client?"),
        (14 * 24 * 3600, ":moneybag: {mentions} — has mediator payment been confirmed?"),
    ],
)

# Reaction name (no colons) that marks an individual item complete
COMPLETION_EMOJI = "white_check_mark"

# Thread reply text that force-closes the entire checklist
COMPLETION_REPLY = "COMPLETE"

REMINDER_INTERVAL_HOURS = float(os.getenv("REMINDER_INTERVAL_HOURS", "24"))
REMINDER_CHECK_INTERVAL_SECONDS = int(os.getenv("REMINDER_CHECK_INTERVAL_SECONDS", "300"))

# Optional Slack user group to @-mention in reminders (e.g. @legalassistants).
# Set NOTIFY_GROUP_ID to the group's Slack ID (find it: right-click the group name
# in Slack → Copy link — the ID looks like S12345678).
# Set NOTIFY_GROUP_NAME to the display name shown after the @.
_group_id = os.getenv("NOTIFY_GROUP_ID", "")
_group_name = os.getenv("NOTIFY_GROUP_NAME", "legalassistants")
NOTIFY_GROUP_MENTION = f"<!subteam^{_group_id}|{_group_name}>" if _group_id else ""
