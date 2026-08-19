import os
import threading
from functools import wraps

from flask import Flask, redirect, render_template, request, Response, url_for

import storage
from config import WORKFLOW_BUCKETS, WORKFLOW_BUCKET_OF


def build_scoreboard(start_ts: float, end_ts: float) -> list:
    """Per-bucket counts for the window (created / open / escalated /
    completed, where created == open + completed and escalated ⊆ open).
    Loads the full window (all statuses) so it's independent of the table's
    status filter. Attorney & Paralegal buckets also get a per-person
    breakdown (by the channel-topic role) under `people`."""
    rows = storage.workflows_in_window(start_ts, end_ts, status="all")
    roles = storage.get_channel_roles()
    names = storage.get_user_names()

    tally = {key: {"created": 0, "open": 0, "escalated": 0, "completed": 0}
             for key, _l, _t, _r in WORKFLOW_BUCKETS}
    role_of = {key: role for key, _l, _t, role in WORKFLOW_BUCKETS}
    # per-bucket: uid -> {open, escalated}
    people = {key: {} for key, _l, _t, role in WORKFLOW_BUCKETS if role}

    for r in rows:
        bkey = WORKFLOW_BUCKET_OF.get(r["trigger_name"])
        if not bkey:
            continue
        t = tally[bkey]
        t["created"] += 1
        is_open = not r.get("completed_at")
        if is_open:
            t["open"] += 1
            if r.get("escalations_sent"):
                t["escalated"] += 1
        else:
            t["completed"] += 1

        role_key = role_of.get(bkey)
        if role_key and is_open:
            uid = (roles.get(r["channel_id"]) or {}).get(role_key)
            if uid:
                p = people[bkey].setdefault(uid, {"open": 0, "escalated": 0})
                p["open"] += 1
                if r.get("escalations_sent"):
                    p["escalated"] += 1

    out = []
    for key, label, _t, role in WORKFLOW_BUCKETS:
        card = {"key": key, "label": label, **tally[key], "people": []}
        if role and people.get(key):
            card["people"] = sorted(
                ({"uid": uid, "name": names.get(uid, uid), **counts}
                 for uid, counts in people[key].items()),
                key=lambda p: (-p["open"], p["name"].lower()),
            )
        out.append(card)
    return out

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
    {
        "key": "email_sender_address",
        "label": "Bucket Emails — Send As (Google Workspace user)",
        "help": "The Workspace email address the reports are sent FROM, using the "
                "Google service account (GOOGLE_SERVICE_ACCOUNT_JSON). The service "
                "account must have domain-wide delegation authorized for the "
                "https://www.googleapis.com/auth/gmail.send scope, and this must be "
                "a real user in your domain (e.g. alerts@ramosjames.com). Leave "
                "empty to fall back to SMTP env vars instead.",
    },
    {
        "key": "email_lookback_days",
        "label": "Bucket Emails — Lookback (days)",
        "help": "How far back the bucket status emails look for still-open items. "
                "Applies to all four emails. Default: 60.",
    },
]

# Three settings per reporting bucket: who receives the email, and when it
# goes out. Generated so the four buckets stay in lock-step.
for _bkey, _blabel, _btrigs, _brole in WORKFLOW_BUCKETS:
    SETTINGS.extend([
        {
            "key": f"email_{_bkey}_recipients",
            "label": f"{_blabel} Email — Recipients",
            "help": f"Comma-separated email addresses that receive the weekly "
                    f"{_blabel} status email ({', '.join(_btrigs)}). "
                    f"Leave empty to turn this email off.",
        },
        {
            "key": f"email_{_bkey}_day",
            "label": f"{_blabel} Email — Day",
            "help": "Day of week to send, e.g. monday / mon / 0 "
                    "(Monday=0 … Sunday=6). Default: monday.",
        },
        {
            "key": f"email_{_bkey}_time",
            "label": f"{_blabel} Email — Time (Central)",
            "help": "Local send time, 24-hour or AM/PM — e.g. 08:00 or 8:00 AM. "
                    "Central Time. Default: 08:00.",
        },
    ])


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
    f_attorney = (request.args.get("attorney", "") or "").strip()
    f_paralegal = (request.args.get("paralegal", "") or "").strip()
    f_la = (request.args.get("la", "") or "").strip()
    wtype = (request.args.get("type", "") or "").strip()
    bucket = (request.args.get("bucket", "") or "").strip()
    sort = request.args.get("sort", "type")

    start_ts = datetime.combine(d_from, dtime.min).timestamp()
    # exclusive upper bound at midnight after d_to, so d_to is inclusive
    end_ts = datetime.combine(d_to + timedelta(days=1), dtime.min).timestamp()

    scoreboard = build_scoreboard(start_ts, end_ts)
    rows = storage.workflows_in_window(start_ts, end_ts, status=status)
    names = storage.get_user_names()
    roles = storage.get_channel_roles()
    workspace = storage.get_config("slack_workspace_url", default="").rstrip("/")

    def _role_of(channel_id: str, key: str):
        return (roles.get(channel_id) or {}).get(key)

    # Facet values from the unfiltered window so a filter can always be widened.
    # One person list per role, so each dropdown offers only people who
    # actually hold that role in the window.
    def _people_by(key: str) -> list:
        ids = {_role_of(r["channel_id"], key) for r in rows}
        ids.discard(None)
        return sorted(ids, key=lambda u: names.get(u, u).lower())

    all_types = sorted({r["trigger_name"] for r in rows})
    attorneys = _people_by("attorney_id")
    paralegals = _people_by("paralegal_id")
    las = _people_by("la_id")

    # Scoreboard cards link here with ?bucket=<key> to show just that bucket's
    # workflow types.
    bucket_trigs = {t for k, _l, trigs, _r in WORKFLOW_BUCKETS if k == bucket for t in trigs}
    if bucket_trigs:
        rows = [r for r in rows if r["trigger_name"] in bucket_trigs]
    bucket_label = next((l for k, l, _t, _r in WORKFLOW_BUCKETS if k == bucket), "")
    if wtype:
        rows = [r for r in rows if r["trigger_name"] == wtype]
    if f_attorney:
        rows = [r for r in rows if _role_of(r["channel_id"], "attorney_id") == f_attorney]
    if f_paralegal:
        rows = [r for r in rows if _role_of(r["channel_id"], "paralegal_id") == f_paralegal]
    if f_la:
        rows = [r for r in rows if _role_of(r["channel_id"], "la_id") == f_la]

    def _named(uid):
        return {"id": uid, "name": names.get(uid, uid)} if uid else None

    now = __import__("time").time()
    view = []
    for r in rows:
        cr = roles.get(r["channel_id"]) or {}
        pids = [u for u in (r.get("participants") or "").split(",") if u]
        # Best channel name: the one saved on the workflow, else the roles
        # table (backfilled from topics), else the raw ID.
        cname = r.get("channel_name") or cr.get("channel_name") or r["channel_id"]
        view.append({
            "channel_name": cname,
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
            "attorney": _named(cr.get("attorney_id")),
            "paralegal": _named(cr.get("paralegal_id")),
            "la": _named(cr.get("la_id")),
            "link": _slack_permalink(workspace, r["channel_id"], r["parent_ts"]),
        })

    def _rolename(v, key):
        return (v[key]["name"].lower() if v.get(key) else "~")  # "~" sorts blanks last

    sorters = {
        "type":      lambda v: (v["type_label"].lower(), -v["opened_ts"]),
        "channel":   lambda v: (v["channel_name"].lower(), -v["opened_ts"]),
        "oldest":    lambda v: v["opened_ts"],
        "newest":    lambda v: -v["opened_ts"],
        "status":    lambda v: (not v["escalated"], -v["opened_ts"]),
        "attorney":  lambda v: (_rolename(v, "attorney"), -v["opened_ts"]),
        "paralegal": lambda v: (_rolename(v, "paralegal"), -v["opened_ts"]),
        "la":        lambda v: (_rolename(v, "la"), -v["opened_ts"]),
    }
    view.sort(key=sorters.get(sort, sorters["type"]))

    return render_template(
        "open_items.html",
        rows=view,
        total=len(view),
        scoreboard=scoreboard,
        escalated_count=sum(1 for v in view if v["escalated"]),
        d_from=d_from.isoformat(), d_to=d_to.isoformat(),
        status=status, wtype=wtype, sort=sort,
        bucket=bucket, bucket_label=bucket_label,
        f_attorney=f_attorney, f_paralegal=f_paralegal, f_la=f_la,
        all_types=[(t, WORKFLOW_LABELS.get(t, t)) for t in all_types],
        attorneys=[(u, names.get(u, u)) for u in attorneys],
        paralegals=[(u, names.get(u, u)) for u in paralegals],
        las=[(u, names.get(u, u)) for u in las],
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
