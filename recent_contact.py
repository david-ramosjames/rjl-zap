"""Logs out-of-band client contacts (email, text, call, letter, fax, visit, etc.)
to a Google Sheet. Triggered by `@RJL-zap recent contact <type> - <details>`."""

import json
import logging
import os
import re
from datetime import datetime
from typing import Optional

import storage

log = logging.getLogger(__name__)

PHRASE = "recent contact"

# Canonical type label -> aliases. Order matters: more specific aliases
# (e.g. "left voicemail") are listed before single-word variants.
CONTACT_TYPES: list[tuple[str, list[str]]] = [
    ("email",     ["email", "emailed", "e-mail"]),
    ("text",      ["text message", "texted", "text", "sms"]),
    ("voicemail", ["left voicemail", "left vm", "voicemail", "vm"]),
    ("call",      ["phone call", "spoke with", "spoke", "called", "call", "phone"]),
    ("letter",    ["letter"]),
    ("mail",      ["mailed", "mail", "usps"]),
    ("fax",       ["faxed", "fax"]),
    ("visit",     ["in-person", "in person", "came in", "dropped by", "stopped by",
                   "met with", "visited", "visit", "met"]),
    ("other",     ["other"]),
]

HEADERS = [
    "Logged At",
    "Slack Channel",
    "Case No",
    "Case",
    "Type",
    "Details",
    "Full Slack Text",
    "Slack Channel ID",
    "Logged By",
]


def is_recent_contact(text: str) -> bool:
    """True if the @-mention contains the trigger phrase."""
    return PHRASE in text.lower()


def parse(text: str) -> tuple[Optional[str], str]:
    """Extract (canonical_type, details) from a `recent contact ...` message.

    Returns (None, "") if no type can be detected so the caller can
    prompt the user with usage. Otherwise returns the matched type and
    whatever details remain after stripping the phrase + type + separator.
    """
    cleaned = re.sub(r"<@[A-Z0-9]+>", "", text)
    lowered = cleaned.lower()

    idx = lowered.find(PHRASE)
    if idx < 0:
        return None, ""
    after_phrase = cleaned[idx + len(PHRASE):].lstrip(" :—-\"'")
    after_lower = after_phrase.lower()

    matched_type: Optional[str] = None
    consumed = 0
    for canonical, aliases in CONTACT_TYPES:
        for alias in aliases:
            if after_lower.startswith(alias):
                end = len(alias)
                next_ch = after_lower[end:end + 1]
                if next_ch == "" or not next_ch.isalnum():
                    matched_type = canonical
                    consumed = end
                    break
        if matched_type:
            break

    if not matched_type:
        return None, ""

    details = after_phrase[consumed:].lstrip(" :—-\"'").rstrip(" \"'")
    details = re.sub(r"\s+", " ", details).strip()
    return matched_type, details


def _parse_case_from_channel(name: str) -> tuple[str, str]:
    """`smith-john-12345` → ('12345', 'Smith John'). Returns ('', '') on no match."""
    if not name:
        return "", ""
    m = re.match(r"^(.*)-(\d+)$", name.strip().lower())
    if not m:
        return "", ""
    raw, num = m.groups()
    title = " ".join(w.capitalize() for w in re.split(r"[-_\s]+", raw) if w)
    return num, title


def _channel_name(client, channel_id: str) -> str:
    if not channel_id:
        return ""
    try:
        resp = client.conversations_info(channel=channel_id)
        return (resp.get("channel") or {}).get("name", "") or ""
    except Exception:
        log.debug("could not fetch channel info for %s", channel_id, exc_info=True)
        return ""


def _user_label(client, user_id: str) -> str:
    if not user_id:
        return ""
    try:
        resp = client.users_info(user=user_id)
        u = resp.get("user") or {}
        return u.get("real_name") or u.get("name") or user_id
    except Exception:
        return user_id


def _open_sheet():
    """Returns a gspread Worksheet, or None if not configured / auth failed."""
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    spreadsheet_id = storage.get_config("recent_contact_spreadsheet_id").strip()
    sheet_name = storage.get_config(
        "recent_contact_sheet_name", default="Recent Contact"
    ).strip() or "Recent Contact"

    if not raw or not spreadsheet_id:
        return None

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        log.warning("gspread/google-auth not installed — recent contact logging disabled")
        return None

    try:
        info = json.loads(raw)
        creds = Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        gc = gspread.authorize(creds)
        ss = gc.open_by_key(spreadsheet_id)
        try:
            ws = ss.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            ws = ss.add_worksheet(title=sheet_name, rows=1000, cols=len(HEADERS))

        first = ws.row_values(1)
        if not first:
            ws.append_row(HEADERS)
        return ws
    except Exception:
        log.exception("failed to open Google Sheet for recent contact log")
        return None


def log_contact(client, event: dict, contact_type: str, details: str) -> bool:
    """Append a row to the Google Sheet. Returns True on success."""
    ws = _open_sheet()
    if ws is None:
        return False

    text = event.get("text") or ""
    channel_id = event.get("channel", "")
    channel_name = _channel_name(client, channel_id)
    case_no, case_name = _parse_case_from_channel(channel_name)
    logged_by = _user_label(client, event.get("user", ""))

    try:
        ws.append_row([
            datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            channel_name,
            case_no,
            case_name,
            contact_type,
            details,
            text,
            channel_id,
            logged_by,
        ], value_input_option="USER_ENTERED")
        log.info("logged recent contact type=%s channel=%s case=%s by=%s",
                 contact_type, channel_name, case_no or "?", logged_by)
        return True
    except Exception:
        log.exception("failed to append recent contact row")
        return False


