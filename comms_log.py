"""Logs out-of-band client communications (email, text, letter, fax, etc.)
to a Google Sheet. Triggered when @RJL-zap is mentioned with a recognized
communication-type keyword."""

import json
import logging
import os
import re
import threading
from datetime import datetime
from typing import Optional

import storage

log = logging.getLogger(__name__)

# Keywords detected in the @-mention. First match wins. Order matters for
# substring collisions (e.g. "mailed" before "mail" is unnecessary because
# we use word boundaries, but "email" vs "mail" is — email is checked first
# so that "email subject" doesn't also match "mail").
COMM_KEYWORDS = [
    ("email",   r"\bemails?\b|\bemailed\b"),
    ("text",    r"\btexts?\b|\btexted\b|\bsms\b"),
    ("letter",  r"\bletters?\b"),
    ("mail",    r"\bmail\b|\bmailed\b|\busps\b"),
    ("fax",     r"\bfax(?:es|ed)?\b"),
]

# Phrases stripped from the cleaned subject/note so it reads cleanly in the sheet.
_STRIP_PHRASES = re.compile(
    r"\b(?:log|sent|send|received|got|received an?|sent an?)\s+(?:email|text|letter|mail|fax)\b[:\s]*|"
    r"\b(?:email|text|letter|mail|fax)(?:\s+client)?[:\s]+",
    re.IGNORECASE,
)

HEADERS = [
    "Logged At",
    "Slack Channel",
    "Case No",
    "Case",
    "Type",
    "Subject / Note",
    "Full Slack Text",
    "Slack Channel ID",
]


def detect_comm_type(text: str) -> Optional[str]:
    """Return the first communication-type keyword found in the text, or None."""
    for label, pattern in COMM_KEYWORDS:
        if re.search(pattern, text, re.IGNORECASE):
            return label
    return None


def _clean_note(text: str) -> str:
    cleaned = re.sub(r"<@[A-Z0-9]+>", "", text)
    cleaned = _STRIP_PHRASES.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" :-—")
    return cleaned


def _parse_case_from_channel(name: str) -> tuple[str, str]:
    """`smith-john-12345` → ('12345', 'Smith John'). Returns ('', '') if no match."""
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


def _open_sheet():
    """Returns a gspread Worksheet, or None if not configured / auth failed."""
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    spreadsheet_id = storage.get_config("comms_log_spreadsheet_id").strip()
    sheet_name = storage.get_config("comms_log_sheet_name", default="comms_log").strip() or "comms_log"

    if not raw or not spreadsheet_id:
        return None

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        log.warning("gspread/google-auth not installed — communication logging disabled")
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

        # Ensure headers exist
        first = ws.row_values(1)
        if not first:
            ws.append_row(HEADERS)
        return ws
    except Exception:
        log.exception("failed to open Google Sheet for comms log")
        return None


def log_communication(client, event: dict, comm_type: str) -> bool:
    """Append a row to the Google Sheet. Returns True on success."""
    ws = _open_sheet()
    if ws is None:
        return False

    text = event.get("text") or ""
    channel_id = event.get("channel", "")
    channel_name = _channel_name(client, channel_id)
    case_no, case_name = _parse_case_from_channel(channel_name)
    note = _clean_note(text)

    try:
        ws.append_row([
            datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            channel_name,
            case_no,
            case_name,
            comm_type,
            note,
            text,
            channel_id,
        ], value_input_option="USER_ENTERED")
        log.info("logged %s communication channel=%s case=%s",
                 comm_type, channel_name, case_no or "?")
        return True
    except Exception:
        log.exception("failed to append communication row")
        return False


def log_communication_async(client, event: dict, comm_type: str) -> None:
    """Fire-and-forget version so we don't block the Slack handler."""
    threading.Thread(
        target=log_communication, args=(client, event, comm_type), daemon=True
    ).start()
