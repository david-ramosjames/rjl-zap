"""Reads the Client Contact Status Google Sheet and yields cases past their
no-contact thresholds. Sheet layout (case-sensitive headers don't matter — we
read by column index):

    A  Case No
    B  Case (client name)
    C  Lead Attorney
    D  Paralegal
    E  Case Status
    F  Last Interaction (date string from the sheet)
    G  Days Since Contact (integer / number)
    ...

Triggered from app.fire_client_contact_alerts on the reminder tick, gated to
~once per 24 h. Read-only — never writes to the sheet."""

import json
import logging
import os
from typing import Iterator, NamedTuple

import storage

log = logging.getLogger(__name__)


class CaseStatusRow(NamedTuple):
    case_no: str          # numeric string parsed from column A
    client_name: str      # column B
    days_since_contact: int  # column G, coerced
    last_interaction: str    # column F, raw string


def _open_sheet():
    """Returns a gspread Worksheet, or None if not configured / auth failed."""
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    spreadsheet_id = storage.get_config("client_contact_spreadsheet_id").strip()
    sheet_name = (
        storage.get_config("client_contact_sheet_name", default="").strip()
        or "Client Contact Status"
    )
    if not raw or not spreadsheet_id:
        return None

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        log.warning("gspread/google-auth not installed — client contact disabled")
        return None

    try:
        info = json.loads(raw)
        creds = Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
        )
        gc = gspread.authorize(creds)
        ss = gc.open_by_key(spreadsheet_id)
        try:
            return ss.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            log.warning(
                "Client Contact Status: tab '%s' not found in spreadsheet %s",
                sheet_name, spreadsheet_id,
            )
            return None
    except Exception:
        log.exception("failed to open Client Contact Status sheet")
        return None


def iter_rows() -> Iterator[CaseStatusRow]:
    """Yield one CaseStatusRow per usable data row in the sheet. Silently
    skips rows whose Case No isn't numeric or whose Days column can't be
    coerced — covers the header, blank rows, and free-form notes."""
    ws = _open_sheet()
    if ws is None:
        return
    try:
        rows = ws.get_all_values()
    except Exception:
        log.exception("failed to read Client Contact Status rows")
        return

    for raw in rows[1:]:  # skip the header row
        if not raw or not (raw[0] or "").strip():
            continue
        case_no = str(raw[0]).strip()
        if not case_no.isdigit():
            continue  # ignores anything but pure digits — header, totals, notes
        client_name = str(raw[1]).strip() if len(raw) > 1 else ""
        last_interaction = str(raw[5]).strip() if len(raw) > 5 else ""
        days_raw = str(raw[6]).strip() if len(raw) > 6 else ""
        try:
            days = int(float(days_raw))
        except (ValueError, TypeError):
            continue
        yield CaseStatusRow(case_no, client_name, days, last_interaction)
