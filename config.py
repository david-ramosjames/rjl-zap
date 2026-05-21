import os
from dataclasses import dataclass
from typing import Dict, List


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

# Reaction name (no colons) that marks an individual item complete
COMPLETION_EMOJI = "white_check_mark"

# Thread reply text that force-closes the entire checklist
COMPLETION_REPLY = "COMPLETE"

REMINDER_INTERVAL_HOURS = float(os.getenv("REMINDER_INTERVAL_HOURS", "24"))
REMINDER_CHECK_INTERVAL_SECONDS = int(os.getenv("REMINDER_CHECK_INTERVAL_SECONDS", "300"))
