from dataclasses import dataclass, field
from typing import Dict, List, Tuple


@dataclass
class TriggerConfig:
    name: str
    phrase: str            # case-insensitive substring match against the @-mention text
    announcement: str      # parent message posted as a thread reply
    items: List[str]       # checklist items, each posted as its own thread reply
    mentions_setting_key: str = ""  # admin setting whose user IDs replace {mentions} in `announcement`


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
    "calendar_sol": TriggerConfig(
        name="calendar_sol",
        phrase="calendar sol",
        announcement=":rotating_light: *Calendar Statute of Limitations* — {mentions} please confirm the SOL is calendared",
        items=[
            ":date: Calendar Statute of Limitations deadline (note the exact date in the case file)",
            ":date: Set calendar reminder 30 days before SOL",
            ":date: Set calendar reminder 7 days before SOL",
        ],
        mentions_setting_key="calendar_sol_user_ids",
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

DISBURSEMENT_MASTER_CHECKLIST = """:spiral_calendar_pad: *DISBURSEMENT OVERVIEW* :spiral_calendar_pad:

The following tasks need to be completed for the disbursement.

:clipboard: *CASE SETTLEMENT*
1) {legalassistants} Cancel & remove all future Lit Events and SOL deadlines from the calendar
2) {paralegal} Cancel all pending orders (Skribe, mediation, experts)
3) {paralegal} Gather any outstanding invoices

:hospital: *MEDICAL BILLS & REDUCTIONS*
1) {paralegal} Open or update subro
2) {paralegal} Gather and confirm medical bills
3) {attorney} Propose reductions
4) {ana} Send reductions
5) {attorney} Approve reductions

:memo: *DRAFTING & RELEASE*
1) {ana} Send drafting instructions and W-9
2) {attorney} Obtain release
3) {ana} & {paralegal} Client sign release
4) {ana} Return release

:moneybag: *FUNDING*
1) {attorney} & {ana} Confirm check issued and tracking
2) {jon} & {ana} Confirm check received and deposited

:dollar: *DISBURSEMENT*
1) {jon} & {ana} Verify RJL expenses
2) {ana} Draft disbursement
3) {attorney} Review & Approve disbursement"""


@dataclass
class DisbursementConfig:
    phrase: str
    # (delay_seconds, message_text) — posted top-level, unconditionally.
    sequence: List[Tuple[float, str]] = field(default_factory=list)
    # (delay_seconds, message_text) — posted IN the master thread, but only
    # if the disbursement workflow is NOT yet marked complete. If someone has
    # closed the workflow (reply "complete" / "@RJL-zap COMPLETE" in the
    # master thread) before the fire time, these are silently skipped.
    deadline_sequence: List[Tuple[float, str]] = field(default_factory=list)


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
            ":clipboard: *CANCEL ORDERS AND GATHER INVOICES*\n\n"
            "{paralegal} please confirm all pending orders (Medical records, Skribe, mediation, experts, etc) have been cancelled to avoid cancellation fees.\n"
            "{paralegal} please confirm that any outstanding invoices have been obtained and saved in the case folder.\n"
            "Please note in a thread any orders or invoices that remain pending."
        )),
        (22 * _H, (
            ":hospital: *MEDICAL BILLS*\n\n"
            "{paralegal} please confirm if subro has been opened and requested final balance (please note in thread if there is no subro on this case).\n"
            "Please note any outstanding medical bills that have been requested."
        )),
        (24 * _H, (
            ":clipboard: *CASE SETTLEMENT*\n\n"
            "{legalassistants} please confirm that all future Litigation Events and SOL deadlines for this case have been removed from the calendar."
        )),
        # 2 hours after MEDICAL BILLS (22h) → 24h
        (24 * _H, (
            ":memo: *DRAFTING & RELEASE* — {ana}\n\n"
            "Please confirm drafting instructions and W-9 have been sent."
        )),
        (3 * _D, (
            ":scissors: *Reductions Update — {attorney} {ana}*\n\n"
            "• Status on medical bill reductions?\n"
            "• Have all lien holders responded?\n"
            "• Are final lien amounts confirmed?"
        )),
        # Release, split into two triggers:
        #   1) obtained / reviewed / approved  (at the Day-7 release slot)
        #   2) signed & returned               (+45h after #1)
        (7 * _D, (
            ":memo: *DRAFTING & RELEASE*\n\n"
            "{attorney} please confirm all releases have been obtained, reviewed and approved.\n\n"
            "{paralegal}, {ana} for visibility"
        )),
        (7 * _D + 45 * _H, (
            ":memo: *DRAFTING & RELEASE*\n\n"
            "{ana} & {paralegal} please confirm all releases have been signed and returned.\n\n"
            "{attorney} for visibility"
        )),
        (14 * _D, (
            ":moneybag: *Funding & Check Issuance — {attorney} {ana}*\n\n"
            "• Have settlement funds been received?\n"
            "• Have checks been issued for liens and expenses?\n"
            "• Have all deposits cleared?"
        )),
        (17 * _D, (
            ":bank: *Deposit Confirmation — {attorney} {ana}*\n\n"
            "• Confirm all checks have been deposited and cleared\n"
            "• Any outstanding items before final disbursement?"
        )),
        (21 * _D, (
            ":receipt: *Expense Reconciliation — {attorney} {ana}*\n\n"
            "• Reconcile all case expenses\n"
            "• Prepare the final disbursement statement\n"
            "• Review with supervising attorney before disbursing to client"
        )),
    ],
    deadline_sequence=[
        # These fire in the master thread ONLY if the disbursement isn't
        # already complete. Reply "complete" / "@RJL-zap COMPLETE" in the
        # master thread to close the workflow and cancel any that haven't
        # fired yet.
        (23 * _D, (
            ":warning: *DISBURSEMENT DEADLINE 7 DAYS AWAY* :warning:\n\n"
            "{attorney} we are 7 days away from 30 days since the settlement. "
            "Please confirm in the thread if the disbursement is complete or on "
            "track to be complete by the 30 day cut off.\n\n"
            "{ana} please confirm when disbursement is drafted and reviewed + "
            "approved with {attorney}.\n\n"
            "{attorney}, {ana} please :triangular_flag_on_post: any blockers to "
            "the disbursement being completed.\n\n"
            "{jon} {laura}"
        )),
        (30 * _D, (
            ":dollar: *DISBURSEMENT DEADLINE* :dollar:\n\n"
            "{attorney} we are 30 days since the settlement. Please confirm in "
            "the thread if the disbursement is complete.\n\n"
            "{attorney}, {ana} please :triangular_flag_on_post: any blockers to "
            "the disbursement being completed.\n\n"
            "{jon} {laura}"
        )),
    ],
)

@dataclass
class FollowUpConfig:
    """Generic config for workflows that post a task, then escalate if no 'done' reply."""
    phrase: str
    initial_message: str          # posted immediately (or after initial_delay_seconds)
    escalation_message: str       # posted if no done reply found; {mentions} and {escalation} substituted
    done_keyword: str
    initial_delay_seconds: float
    check_delay_seconds: float    # when to check for done and maybe escalate


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

PARALEGAL_INTRO = FollowUpConfig(
    phrase="paralegal intro",
    done_keyword="done",
    initial_delay_seconds=0,            # immediate
    check_delay_seconds=24 * 60 * 60,   # 24 hours
    initial_message=(
        ":telephone_receiver: *Client Contact Required — {{mentions}}*\n\n"
        "Please make initial contact with the client within *24 hours*.\n"
        "Reply *done* in this thread once contact has been made."
    ),
    escalation_message=(
        ":warning: *REMINDER: Client Contact Still Pending* :warning:\n\n"
        "{{mentions}} — it has been 24 hours and no confirmation has been received.\n"
        "Please confirm the status of client contact and reply *done* when complete.\n\n"
        "{{escalation}}Please follow up urgently."
    ),
)

CHECK_PICKUP = FollowUpConfig(
    phrase="check pickup",
    done_keyword="scheduled",
    initial_delay_seconds=0,            # immediate
    check_delay_seconds=5 * 86400,      # 5 days
    initial_message=(
        ":money_with_wings: *Client Check Pickup — {{mentions}}*\n\n"
        "Please schedule the client check pickup *within 7 days*.\n"
        "Reply *scheduled* in this thread once the pickup is on the calendar."
    ),
    escalation_message=(
        ":warning: *REMINDER: Check Pickup Not Yet Scheduled* :warning:\n\n"
        "{{mentions}} — it has been 5 days and pickup scheduling has not been confirmed.\n"
        "The 7-day deadline is approaching. Please schedule immediately and reply *scheduled* when done.\n\n"
        "{{escalation}}"
    ),
)


@dataclass
class SimplePostConfig:
    """One-shot post: immediately drops a message tagging participants + extra contacts from settings."""
    phrase: str
    message: str  # {mentions} and {extras} substituted
    extras_setting_key: str  # DB setting key for the extra user IDs to tag


NEW_CASE = SimplePostConfig(
    phrase="new case",
    message=":file_folder: *Here is a new case to assign* — {extras}\n{mentions}",
    extras_setting_key="new_case_assignee_user_ids",
)

REVIEW_REQUEST = SimplePostConfig(
    phrase="ready for review",
    message=":star: *Is this a client we will ask for a 5-star review?* :star:\n{mentions} {extras}",
    extras_setting_key="review_request_user_ids",
)


DOC_VERIFICATION = FollowUpConfig(
    phrase="document verification",
    done_keyword="confirmed",
    initial_delay_seconds=0,
    check_delay_seconds=24 * 60 * 60,
    initial_message=(
        ":file_folder: *CASE DOCUMENT VERIFICATION* :file_folder:\n\n"
        "{{mentions}} Please confirm you have received and saved the following "
        "documents into the Dropbox case folder:\n\n"
        "1) Medicare/Medicaid Card :white_check_mark:\n"
        "2) Health Insurance Card :white_check_mark:\n"
        "3) Outstanding Bills :white_check_mark:\n"
        "4) 1P Insurance Card :white_check_mark:\n"
        "5) 1P Policy Dec Page :white_check_mark:\n"
        "6) 3P Insurance Card :white_check_mark:\n\n"
        "Please reply *confirmed* in this thread once all documents have been "
        "confirmed and saved to the case folder. Also alert the paralegal of "
        "any items that remain outstanding."
    ),
    escalation_message=(
        ":warning: *REMINDER: Document Verification Outstanding* :warning:\n\n"
        "{{mentions}} — it has been 24 hours since the verification request and "
        "no confirmation has been received. Please complete the verification and "
        "reply *confirmed* in this thread.\n\n"
        "{{escalation}}"
    ),
)


CLIENT_INTAKE = FollowUpConfig(
    phrase="client intake",
    done_keyword="done",
    initial_delay_seconds=0,            # auto-fire scheduling handled at the lifecycle level
    check_delay_seconds=24 * 60 * 60,   # 24 hours
    initial_message=(
        ":clipboard: *Client Intake — {{mentions}}*\n\n"
        "Please collect the following from the new client and reply *done* in this thread "
        "once everything is captured:\n\n"
        ":bust_in_silhouette: Full legal name\n"
        ":telephone_receiver: Phone number\n"
        ":iphone: Text-capable? (yes / no)\n"
        ":email: Email address\n"
        ":house: Mailing address\n"
        ":id: Government ID (DL, passport, etc.)\n"
        ":lock: SSN\n"
        ":sos: Emergency contact (name + number)\n"
        ":globe_with_meridians: Preferred language"
    ),
    escalation_message=(
        ":warning: *REMINDER: Client Intake Still Pending* :warning:\n\n"
        "{{mentions}} — it has been 24 hours and no confirmation has been received.\n"
        "Please collect the client intake details above and reply *done* in this thread.\n\n"
        "{{escalation}}"
    ),
)


COMPLETION_EMOJI = "white_check_mark"
COMPLETION_REPLY = "COMPLETE"
REMINDER_CHECK_INTERVAL_SECONDS = 250

# ── Channel-lifecycle automatic triggers ──
# When a new public channel is created, the bot auto-joins it and fires these
# (all timed from channel creation):
#   - new_case        → +180 seconds
#   - case_setup      → +15 minutes
#   - calendar_sol    → +15 minutes  (moved up from +48h — calendar the SOL
#                       during the assignment phase, not days later)
#   - client_intake   → +1 hour
#   - doc_verification→ +48 hours    (moved back from +24h to the old SOL slot)
NEW_CASE_ON_FIRST_MESSAGE = True
NEW_CASE_DELAY_SECONDS = 180
CASE_SETUP_DELAY_SECONDS = 15 * 60
CALENDAR_SOL_DELAY_SECONDS = 15 * 60
CLIENT_INTAKE_DELAY_SECONDS = 60 * 60
DOC_VERIFICATION_DELAY_SECONDS = 48 * 60 * 60

# Phrase that auto-triggers Check Pickup. Only fires when the message
# author is in the `check_pickup_trigger_user_ids` admin setting.
CHECK_PICKUP_AUTO_PHRASE = "law firm can be paid"
REVIEW_REQUEST_AUTO_PHRASE = "RJL has been paid"
