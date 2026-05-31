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

_H = 3600       # seconds in an hour
_D = 86400      # seconds in a day

DISBURSEMENT_MASTER_CHECKLIST = """:hourglass_flowing_sand: *30-Day Disbursement Workflow Started* — {mentions}

*📋 Phase 1 — Case Settlement*
• Confirm settlement amount and authority
• Obtain signed settlement agreement
• Send settlement confirmation to all parties

*🏥 Phase 2 — Medical Bills & Reductions*
• Gather all outstanding medical bills
• Request reductions from providers
• Confirm final lien amounts

*📝 Phase 3 — Drafting & Release*
• Draft settlement release
• Obtain client signature on release
• Send executed release to defense counsel

*💰 Phase 4 — Funding*
• Confirm receipt of settlement funds
• Issue checks for liens and expenses
• Confirm all deposits cleared

*💵 Phase 5 — Disbursement*
• Reconcile all expenses
• Prepare final disbursement statement
• Disburse net proceeds to client

React ✅ on each phase above as it is completed."""


@dataclass
class DisbursementConfig:
    phrase: str
    # (delay_seconds, message_text) — {mentions} replaced with team extracted from channel topic
    sequence: List[Tuple[float, str]] = field(default_factory=list)


DISBURSEMENT = DisbursementConfig(
    phrase="start disbursement",
    sequence=[
        # Times are cumulative from trigger
        (30 * 60, (
            ":hourglass_flowing_sand: *Disbursement Overview* — {mentions}\n\n"
            "The 30-day disbursement clock is running. Here's a quick summary of what needs to happen:\n"
            "• Phase 1 (now): Lock in settlement details\n"
            "• Phase 2 (~Day 1): Gather and negotiate medical bills\n"
            "• Phase 3 (~Day 7): Draft and execute release\n"
            "• Phase 4 (~Day 14): Fund and issue checks\n"
            "• Phase 5 (~Day 21–30): Final disbursement to client\n\n"
            "Tag me with `COMPLETE` in this thread when everything is done."
        )),
        (3 * _H, (
            ":no_entry: *Action needed — {mentions}*\n\n"
            "• Cancel any outstanding orders related to this case\n"
            "• Begin gathering all invoices, liens, and expense records\n"
            "• Confirm settlement authority is in place"
        )),
        (22 * _H, (
            ":hospital: *Medical Bills Check-In — {mentions}*\n\n"
            "• Have all medical bills been collected?\n"
            "• Have reduction requests been sent to providers?\n"
            "• Any outstanding liens that need follow-up?"
        )),
        (24 * _H, (
            ":handshake: *Case Settlement Confirmation — {mentions}*\n\n"
            "• Is the settlement amount confirmed and agreed?\n"
            "• Has the signed settlement agreement been received?\n"
            "• Has confirmation gone to all parties?"
        )),
        (3 * _D, (
            ":scissors: *Reductions Update — {mentions}*\n\n"
            "• Status on medical bill reductions?\n"
            "• Have all lien holders responded?\n"
            "• Are final lien amounts confirmed?"
        )),
        (7 * _D, (
            ":pen: *Release Signatures — {mentions}*\n\n"
            "• Has the settlement release been drafted?\n"
            "• Has the client signed the release?\n"
            "• Has the executed release been sent to defense counsel?"
        )),
        (14 * _D, (
            ":moneybag: *Funding & Check Issuance — {mentions}*\n\n"
            "• Have settlement funds been received?\n"
            "• Have checks been issued for liens and expenses?\n"
            "• Have all deposits cleared?"
        )),
        (17 * _D, (
            ":bank: *Deposit Confirmation — {mentions}*\n\n"
            "• Confirm all checks have been deposited and cleared\n"
            "• Any outstanding items before final disbursement?"
        )),
        (21 * _D, (
            ":receipt: *Expense Reconciliation — {mentions}*\n\n"
            "• Reconcile all case expenses\n"
            "• Prepare the final disbursement statement\n"
            "• Review with supervising attorney before disbursing to client"
        )),
        (30 * _D, (
            ":rotating_light: *30-Day Disbursement Deadline — {mentions}*\n\n"
            "Today is the target completion date. Please confirm:\n"
            "• Net proceeds have been disbursed to the client\n"
            "• Final disbursement statement is signed\n"
            "• File is ready to close\n\n"
            "Reply `@Jamie COMPLETE` in this thread to close out the workflow."
        )),
    ],
)

# Slack user IDs allowed to trigger the disbursement workflow (comma-separated)
_raw_auth = os.getenv("DISBURSEMENT_AUTHORIZED_USER_IDS", "")
DISBURSEMENT_AUTHORIZED_USER_IDS: set[str] = {
    uid.strip() for uid in _raw_auth.split(",") if uid.strip()
}


@dataclass
class FollowUpConfig:
    """Generic config for workflows that post a task, then escalate if no 'done' reply."""
    phrase: str
    initial_message: str          # posted immediately (or after initial_delay_seconds)
    escalation_message: str       # posted if no done reply found; {mentions} and {escalation} substituted
    done_keyword: str
    initial_delay_seconds: float
    check_delay_seconds: float    # when to check for done and maybe escalate


_atty_escalation_raw = os.getenv("ATTORNEY_INTRO_ESCALATION_USER_IDS", "")
ATTORNEY_INTRO_ESCALATION_IDS: List[str] = [
    uid.strip() for uid in _atty_escalation_raw.split(",") if uid.strip()
]

ATTORNEY_INTRO = FollowUpConfig(
    phrase="attorney intro",
    done_keyword="done",
    initial_delay_seconds=60 * 60,      # 1 hour
    check_delay_seconds=48 * 60 * 60,   # 48 hours
    initial_message=(
        ":hourglass: *Client Contact Required* — {{mentions}}\n\n"
        "You have *72 hours* to make initial contact with the client.\n"
        "Please reply *done* in this thread once contact has been made."
    ),
    escalation_message=(
        ":warning: *REMINDER: Client Contact Still Pending* :warning:\n\n"
        "{{mentions}} — it has been 48 hours and no confirmation has been received.\n"
        "Please confirm the status of client contact immediately and reply *done* when complete.\n\n"
        "{{escalation}}Please follow up urgently."
    ),
)

_case_setup_escalation_raw = os.getenv("CASE_SETUP_ESCALATION_USER_IDS", "")
CASE_SETUP_ESCALATION_IDS: List[str] = [
    uid.strip() for uid in _case_setup_escalation_raw.split(",") if uid.strip()
]

CASE_SETUP = FollowUpConfig(
    phrase="case setup",
    done_keyword="done",
    initial_delay_seconds=0,            # immediate
    check_delay_seconds=24 * 60 * 60,   # 24 hours
    initial_message=(
        ":clipboard: *Case Setup Verification — {{mentions}}*\n\n"
        "Please confirm the following setup items have been completed and reply *done* in this thread:\n\n"
        ":white_check_mark: Signed contract saved in Dropbox\n"
        ":white_check_mark: Intake sheet completed\n"
        ":white_check_mark: CTA signed and saved (if applicable)"
    ),
    escalation_message=(
        ":warning: *REMINDER: Case Setup Not Yet Confirmed* :warning:\n\n"
        "{{mentions}} — it has been 24 hours and setup has not been confirmed.\n"
        "Please complete the checklist above and reply *done* in this thread.\n\n"
        "{{escalation}}"
    ),
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
