import os
import threading
from functools import wraps

from flask import Flask, redirect, render_template, request, Response, url_for

import storage

flask_app = Flask(__name__)

SETTINGS = [
    {
        "key": "disbursement_authorized_user_ids",
        "label": "Disbursement — Auto-Trigger Users",
        "help": "Slack user IDs whose messages auto-start the 30-day "
                "disbursement workflow when they include the phrase "
                "\"start disbursement\". Comma-separated. Leave empty "
                "to disable the auto-trigger entirely (no manual fallback).",
    },
    {
        "key": "disbursement_ana_user_id",
        "label": "Disbursement — Ana's Slack ID",
        "help": "Single Slack user ID tagged as 'Ana' in the disbursement "
                "overview (Send reductions, Send drafting instructions, "
                "etc.). Falls back to the literal text '@ana' if empty.",
    },
    {
        "key": "disbursement_jon_user_id",
        "label": "Disbursement — Jon's Slack ID",
        "help": "Single Slack user ID tagged as 'Jon' in the disbursement "
                "overview (Confirm check received and deposited, Verify "
                "RJL expenses). Falls back to the literal text '@jon' if empty.",
    },
    {
        "key": "disbursement_laura_user_id",
        "label": "Disbursement — Laura's Slack ID",
        "help": "Single Slack user ID tagged as 'Laura' on the 7-day and "
                "30-day disbursement deadline warnings. Falls back to the "
                "literal text '@laura' if empty.",
    },
    {
        "key": "attorney_intro_escalation_user_ids",
        "label": "Attorney Intro — Escalation Contacts",
        "help": "Tagged in the 48-hour reminder if no 'done' reply. Comma-separated.",
    },
    {
        "key": "case_setup_escalation_user_ids",
        "label": "Case Setup — Escalation Contacts",
        "help": "Tagged in the 24-hour reminder if no 'done' reply. Comma-separated.",
    },
    {
        "key": "doc_verification_escalation_user_ids",
        "label": "Document Verification — Escalation Contacts",
        "help": "Tagged in the 24-hour reminder if no 'verified' reply. Comma-separated.",
    },
    {
        "key": "calendar_sol_user_ids",
        "label": "Calendar SOL — Assigned User(s)",
        "help": "Tagged in the SOL announcement when the Calendar Statute "
                "of Limitations checklist auto-fires (48 hrs after channel "
                "creation) or is triggered manually. Comma-separated. "
                "Leave empty to omit the tag.",
    },
    {
        "key": "case_setup_participant_user_ids",
        "label": "Case Setup / Doc Verification — Auto-Tagged Participants",
        "help": "Comma-separated Slack user IDs to @-mention when the bot "
                "auto-fires Case Setup (15 min) and Document Verification "
                "(24 hr) in newly created channels.",
    },
    {
        "key": "paralegal_intro_escalation_user_ids",
        "label": "Paralegal Intro — Escalation Contacts",
        "help": "Tagged in the 24-hour reminder if no 'done' reply. Comma-separated.",
    },
    {
        "key": "check_pickup_backup_user_ids",
        "label": "Check Pickup — Backup Contacts",
        "help": "Tagged in the 5-day reminder if no 'scheduled' reply. Comma-separated.",
    },
    {
        "key": "check_pickup_trigger_user_ids",
        "label": "Check Pickup — Auto-Trigger Users",
        "help": "Slack user IDs whose messages auto-trigger Check Pickup "
                "when they include the phrase \"law firm can be paid\". "
                "The user who posted the message gets tagged in the "
                "scheduling reminder. Comma-separated. Leave empty to "
                "disable the auto-trigger.",
    },
    {
        "key": "client_intake_assignee_user_ids",
        "label": "Client Intake — Assigned Collector(s)",
        "help": "Slack user IDs tagged 1 hour after a new channel is created to collect "
                "client intake details (e.g. U07SDBC2146). Comma-separated. "
                "Leave empty to disable the auto Client Intake trigger.",
    },
    {
        "key": "client_intake_escalation_user_ids",
        "label": "Client Intake — Escalation Contacts",
        "help": "Tagged in the 24-hour reminder if no 'done' reply. Comma-separated.",
    },
    {
        "key": "review_request_trigger_user_ids",
        "label": "Review Request — Auto-Trigger Users",
        "help": "Slack user IDs whose messages auto-trigger the 5-star review "
                "prompt (3-minute delay) when they include \"RJL has been paid\". "
                "Comma-separated. Leave empty to disable the auto-trigger.",
    },
    {
        "key": "new_case_assignee_user_ids",
        "label": "New Case — Default Assignee(s)",
        "help": "Always tagged when @Jamie new case fires (e.g. Laura: UA63X86AJ). Comma-separated.",
    },
    {
        "key": "review_request_user_ids",
        "label": "Review Request — Additional Contacts",
        "help": "Extra people tagged in the 5-star review prompt. Comma-separated.",
    },
    {
        "key": "notify_group_id",
        "label": "@legalassistants — Group ID",
        "help": "Slack user group ID for checklist reminders (e.g. S12345678). "
                "Find it: right-click the group name in Slack → Copy link.",
    },
    {
        "key": "notify_group_name",
        "label": "@legalassistants — Group Name",
        "help": "Display name shown in the @-mention (default: legalassistants).",
    },
    {
        "key": "reminder_interval_hours",
        "label": "Checklist Reminder Interval (hours)",
        "help": "How often to nudge on open checklist items. Default: 24.",
    },
    {
        "key": "recent_contact_spreadsheet_id",
        "label": "Recent Contact — Google Sheet ID",
        "help": "Spreadsheet ID for logging client contacts (emails, calls, visits, etc). "
                "Found in the sheet URL between /d/ and /edit. Requires the "
                "GOOGLE_SERVICE_ACCOUNT_JSON env var and the sheet shared with "
                "the service account email.",
    },
    {
        "key": "recent_contact_sheet_name",
        "label": "Recent Contact — Tab Name",
        "help": "Worksheet tab name within the spreadsheet. Default: Recent Contact "
                "(auto-created with headers if it doesn't exist).",
    },
    {
        "key": "weekly_report_channel",
        "label": "Weekly Status Report — Channel",
        "help": "Where the weekly open-items digest is posted, e.g. "
                "daily-pulse (the # is optional; a channel ID also works). "
                "Leave empty to turn the weekly report off. The bot must be "
                "a member of the channel.",
    },
    {
        "key": "weekly_report_day",
        "label": "Weekly Status Report — Day",
        "help": "Day of week to post, e.g. friday / fri / 4 "
                "(Monday=0 … Sunday=6). Default: friday.",
    },
    {
        "key": "weekly_report_time",
        "label": "Weekly Status Report — Time (Central)",
        "help": "Local time to post, 24-hour or with AM/PM — e.g. 17:30 or "
                "5:30 PM. Central Time. Default: 17:30. Posts on the first "
                "check after this time (checks run every ~4 minutes).",
    },
    {
        "key": "weekly_report_lookback_days",
        "label": "Weekly Status Report — Lookback (days)",
        "help": "How far back to look for still-open items. Default: 7.",
    },
    {
        "key": "client_contact_spreadsheet_id",
        "label": "Client Contact Status — Google Sheet ID",
        "help": "Spreadsheet ID for the Client Contact Status tracker (case no in "
                "column A, days-since-contact in column G). Found in the sheet URL "
                "between /d/ and /edit. Read-only — same service account JSON as "
                "Recent Contact. Sheet must be shared with the service account email.",
    },
    {
        "key": "client_contact_sheet_name",
        "label": "Client Contact Status — Tab Name",
        "help": "Worksheet tab name within the spreadsheet. Default: Client Contact "
                "Status.",
    },
]


def _requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        password = os.getenv("WEB_PASSWORD", "")
        if not password:
            return f(*args, **kwargs)
        auth = request.authorization
        if not auth or auth.password != password:
            return Response(
                "Authentication required", 401,
                {"WWW-Authenticate": 'Basic realm="Jamie Admin"'},
            )
        return f(*args, **kwargs)
    return decorated


@flask_app.route("/", methods=["GET"])
@_requires_auth
def index():
    cfg = storage.get_all_config()
    saved = request.args.get("saved") == "1"
    return render_template("index.html", settings=SETTINGS, cfg=cfg, saved=saved)


# trigger_name → label shown on the Open Items page. Kept here (rather than
# imported from app) so the web thread never imports the Slack app module.
WORKFLOW_LABELS = {
    "attorney_intro":      "Attorney Intro",
    "paralegal_intro":     "Paralegal Intro",
    "case_setup":          "Case Setup",
    "client_intake":       "Client Intake",
    "doc_verification":    "Document Verification",
    "check_pickup":        "Check Pickup",
    "calendar_sol":        "Calendar SOL",
    "mediation_checklist": "Mediation Checklist",
    "disbursement":        "30-Day Disbursement",
    "answer_filed":        "Calendar — Answer Filed",
    "discovery_received":  "Calendar — Discovery Requests",
    "scheduling_order":    "Calendar — Scheduling Order",
}
WORKFLOW_DONE_WORD = {"doc_verification": "confirmed", "check_pickup": "scheduled"}


def _slack_permalink(workspace_url: str, channel_id: str, parent_ts: str) -> str:
    """https://acme.slack.com/archives/C123/p1780000000000100 — Slack's
    permalink form is the ts with the dot removed, prefixed with 'p'."""
    if not (workspace_url and channel_id and parent_ts):
        return ""
    return f"{workspace_url}/archives/{channel_id}/p{parent_ts.replace('.', '')}"


def _parse_date(raw: str):
    from datetime import datetime
    try:
        return datetime.strptime((raw or "").strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


@flask_app.route("/open-items", methods=["GET"])
@_requires_auth
def open_items():
    from datetime import date, datetime, time as dtime, timedelta

    today = date.today()
    d_from = _parse_date(request.args.get("from", "")) or (today - timedelta(days=30))
    d_to = _parse_date(request.args.get("to", "")) or today
    if d_from > d_to:
        d_from, d_to = d_to, d_from

    status = request.args.get("status", "open")
    if status not in ("open", "escalated", "completed", "all"):
        status = "open"
    person = (request.args.get("person", "") or "").strip()
    wtype = (request.args.get("type", "") or "").strip()
    sort = request.args.get("sort", "type")

    start_ts = datetime.combine(d_from, dtime.min).timestamp()
    # exclusive upper bound at midnight after d_to, so d_to is inclusive
    end_ts = datetime.combine(d_to + timedelta(days=1), dtime.min).timestamp()

    rows = storage.workflows_in_window(start_ts, end_ts, status=status)
    names = storage.get_user_names()
    workspace = storage.get_config("slack_workspace_url", default="").rstrip("/")

    # Facet values are computed from the unfiltered window so a filter can
    # always be widened again from the dropdowns.
    all_types = sorted({r["trigger_name"] for r in rows})
    all_people = sorted(
        {u for r in rows for u in (r.get("participants") or "").split(",") if u},
        key=lambda u: names.get(u, u).lower(),
    )

    if wtype:
        rows = [r for r in rows if r["trigger_name"] == wtype]
    if person:
        rows = [r for r in rows if person in (r.get("participants") or "").split(",")]

    now = __import__("time").time()
    view = []
    for r in rows:
        pids = [u for u in (r.get("participants") or "").split(",") if u]
        view.append({
            "channel_name": r.get("channel_name") or r["channel_id"],
            "channel_id": r["channel_id"],
            "type": r["trigger_name"],
            "type_label": WORKFLOW_LABELS.get(r["trigger_name"], r["trigger_name"]),
            "opened_ts": r["created_at"],
            "opened": datetime.fromtimestamp(r["created_at"]).strftime("%b %-d, %Y"),
            "age_days": max(0, int((now - r["created_at"]) // 86400)),
            "escalated": bool(r.get("escalations_sent")),
            "completed": bool(r.get("completed_at")),
            "open_items": r.get("open_items") or 0,
            "done_word": WORKFLOW_DONE_WORD.get(r["trigger_name"], "done"),
            "people": [{"id": u, "name": names.get(u, u)} for u in pids],
            "link": _slack_permalink(workspace, r["channel_id"], r["parent_ts"]),
        })

    sorters = {
        "type":    lambda v: (v["type_label"].lower(), -v["opened_ts"]),
        "channel": lambda v: (v["channel_name"].lower(), -v["opened_ts"]),
        "oldest":  lambda v: v["opened_ts"],
        "newest":  lambda v: -v["opened_ts"],
        "status":  lambda v: (not v["escalated"], -v["opened_ts"]),
    }
    view.sort(key=sorters.get(sort, sorters["type"]))

    return render_template(
        "open_items.html",
        rows=view,
        total=len(view),
        escalated_count=sum(1 for v in view if v["escalated"]),
        d_from=d_from.isoformat(), d_to=d_to.isoformat(),
        status=status, person=person, wtype=wtype, sort=sort,
        all_types=[(t, WORKFLOW_LABELS.get(t, t)) for t in all_types],
        all_people=[(u, names.get(u, u)) for u in all_people],
        has_workspace=bool(workspace),
    )


@flask_app.route("/settings", methods=["POST"])
@_requires_auth
def save_settings():
    for s in SETTINGS:
        value = request.form.get(s["key"], "").strip()
        storage.set_config(s["key"], value)
    return redirect(url_for("index", saved=1))


def start():
    port = int(os.getenv("PORT", "8080"))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)
