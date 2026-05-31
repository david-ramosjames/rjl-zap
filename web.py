import os
import threading
from functools import wraps

from flask import Flask, redirect, render_template, request, Response, url_for

import storage

flask_app = Flask(__name__)

SETTINGS = [
    {
        "key": "disbursement_authorized_user_ids",
        "label": "Disbursement — Authorized Users",
        "help": "Only these user IDs can trigger @Jamie start disbursement. Comma-separated.",
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
