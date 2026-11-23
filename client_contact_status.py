"""Reads active Case Tracker cases + client-contact summaries from Supabase
(the same source as https://rjl-case-tracker.vercel.app/client-contact).

Mirrors Case Tracker's qualifying-contact rules:
  - last interaction = latest qualifying call (60s+, non-Sona), 7-day text
    exchange, or manual "other" contact log
  - last attempt = latest of last interaction, outbound call, outbound text
  - status = contacted | attempted_no_response | not_attempted_recently

Triggered from app.fire_client_contact_alerts on the reminder tick. Read-only.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Iterator, NamedTuple

import requests

log = logging.getLogger(__name__)

CLIENT_CONTACT_WINDOW_DAYS = 30

_CLOSED_DB_STAGES = frozenset({"DISENGAGED", "Closed", "Disengaged", "Terminated", "Referred"})
_STATUS_LABEL = {
    "contacted": "Contacted",
    "attempted_no_response": "Attempted — No Response",
    "not_attempted_recently": "Not Attempted Recently",
}
_STATUS_HELP = {
    "contacted": "Confirmed two-way communication within 30 days.",
    "attempted_no_response": "Outreach occurred, but no qualifying contact.",
    "not_attempted_recently": "No recent outreach attempt recorded.",
}


class CaseStatusRow(NamedTuple):
    case_no: str
    client_name: str
    days_since_contact: int
    # Human-readable last qualifying contact ("Nov 1, 2026" or "").
    last_interaction: str
    # Dedup key for alert history — changes when contact or status changes.
    alert_key: str
    contact_status: str
    contact_status_label: str
    contact_status_help: str
    last_attempt: str
    last_other_details: str
    slack_channel_id: str


def _env(*keys: str) -> str:
    for key in keys:
        value = (os.getenv(key) or "").strip()
        if value:
            return value
    return ""


def is_configured() -> bool:
    return bool(
        _env("CASE_TRACKER_SUPABASE_URL", "SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL")
        and _env(
            "CASE_TRACKER_SUPABASE_SECRET_KEY",
            "SUPABASE_SECRET_KEY",
            "SUPABASE_SERVICE_ROLE_KEY",
        )
    )


def _headers() -> dict[str, str]:
    key = _env(
        "CASE_TRACKER_SUPABASE_SECRET_KEY",
        "SUPABASE_SECRET_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
    )
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }


def _base_url() -> str:
    return _env(
        "CASE_TRACKER_SUPABASE_URL", "SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL",
    ).rstrip("/")


def _get_all(path: str, params: dict | None = None) -> list[dict]:
    """Paginate PostgREST until exhausted (1k rows per page)."""
    url = f"{_base_url()}/rest/v1/{path}"
    headers = {**_headers(), "Prefer": "count=exact"}
    out: list[dict] = []
    start = 0
    page = 1000
    while True:
        req_headers = {
            **headers,
            "Range": f"{start}-{start + page - 1}",
        }
        resp = requests.get(url, headers=req_headers, params=params or {}, timeout=60)
        if resp.status_code >= 400:
            log.error(
                "Case Tracker Supabase GET %s failed: %s %s",
                path, resp.status_code, resp.text[:300],
            )
            resp.raise_for_status()
        chunk = resp.json()
        if not isinstance(chunk, list):
            raise RuntimeError(f"unexpected response for {path}: {type(chunk)}")
        out.extend(chunk)
        if len(chunk) < page:
            return out
        start += page


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        # Supabase returns ISO8601; fromisoformat needs +00:00 not Z.
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _latest(*values: str | None) -> str | None:
    best: str | None = None
    best_ts = float("-inf")
    for value in values:
        ts = _parse_ts(value)
        if ts is not None and ts > best_ts:
            best = value
            best_ts = ts
    return best


def _earlier(left: str | None, right: str | None) -> str | None:
    left_ts = _parse_ts(left)
    right_ts = _parse_ts(right)
    if left_ts is None:
        return right if right_ts is not None else None
    if right_ts is None:
        return left
    return left if left_ts <= right_ts else right


def _days_since(value: str | None, now: float) -> int | None:
    ts = _parse_ts(value)
    if ts is None:
        return None
    return max(0, int((now - ts) // 86400))


def _format_when(value: str | None) -> str:
    ts = _parse_ts(value)
    if ts is None:
        return ""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


def _contact_dates(summary: dict | None, qualifying: bool) -> tuple[str | None, str | None]:
    if not summary:
        return None, None
    if qualifying:
        last_interaction = _latest(
            summary.get("last_qualifying_call_at"),
            summary.get("last_text_exchange_at"),
            summary.get("last_other_at"),
        )
    else:
        last_interaction = _latest(
            summary.get("last_human_call_at"),
            summary.get("last_conversation_at"),
            summary.get("last_other_at"),
            _earlier(summary.get("last_inbound_text_at"), summary.get("last_outbound_text_at")),
        )
    last_attempt = _latest(
        last_interaction,
        summary.get("last_outbound_call_at"),
        summary.get("last_outbound_text_at"),
    )
    return last_interaction, last_attempt


def _contact_status(last_interaction: str | None, last_attempt: str | None, now: float) -> str:
    interaction_days = _days_since(last_interaction, now)
    if interaction_days is not None and interaction_days < CLIENT_CONTACT_WINDOW_DAYS:
        return "contacted"
    attempt_days = _days_since(last_attempt, now)
    if attempt_days is not None and attempt_days < CLIENT_CONTACT_WINDOW_DAYS:
        return "attempted_no_response"
    return "not_attempted_recently"


def _is_referred(entry: dict, topic_stage: str | None) -> bool:
    stage = (entry.get("case_stage") or "").strip()
    if stage.lower() in {"referred", "closed"}:
        return True
    if topic_stage and "referred" in topic_stage.lower():
        return True
    for key in ("attorney_name", "paralegal_name"):
        name = (entry.get(key) or "").strip()
        if name.lower().startswith("referred"):
            return True
    return False


def _is_fully_disbursed(result: dict | None) -> bool:
    if not result:
        return False
    return (
        (result.get("disbursed_status") or "").strip() == "Yes"
        and bool((result.get("disburse_date") or "").strip())
    )


def _qualifying_rule_ready() -> bool:
    try:
        rows = _get_all(
            "case_tracker_settings",
            {"key": "eq.client_contact_qualifying_rule", "select": "value"},
        )
    except Exception:
        log.exception("could not read client_contact_qualifying_rule")
        return True  # production is ready; prefer qualifying math
    if not rows:
        return False
    value = rows[0].get("value") or {}
    return bool(isinstance(value, dict) and value.get("ready") is True)


def iter_rows() -> Iterator[CaseStatusRow]:
    """Yield one CaseStatusRow per active Case Tracker case that still needs
    ongoing updates (same open set as /client-contact)."""
    if not _configured():
        return

    now = time.time()
    try:
        qualifying = _qualifying_rule_ready()
        entries = _get_all(
            "case_tracker_entries",
            {
                "select": (
                    "id,case_id,case_number,case_stage,client_name_snapshot,"
                    "attorney_name,paralegal_name,is_active,created_at"
                ),
            },
        )
        results = _get_all(
            "case_tracker_results",
            {"select": "tracker_entry_id,disbursed_status,disburse_date"},
        )
        summaries = _get_all("case_client_contact_summary", {"select": "*"})
        channels = _get_all(
            "case_slack_channels",
            {"select": "case_number,slack_channel_id,topic_stage"},
        )
        cases = _get_all("cases", {"select": "id,client_name,case_number"})
    except Exception:
        log.exception("failed to read Case Tracker client-contact tables")
        return

    results_by_entry = {r.get("tracker_entry_id"): r for r in results if r.get("tracker_entry_id")}
    summary_by_case = {str(s.get("case_number") or "").strip(): s for s in summaries}
    channel_by_case = {str(c.get("case_number") or "").strip(): c for c in channels}
    client_by_id = {c.get("id"): c for c in cases if c.get("id")}

    for entry in entries:
        if entry.get("is_active") is False:
            continue
        case_no = str(entry.get("case_number") or "").strip()
        if not case_no or not case_no.isdigit():
            continue
        stage = (entry.get("case_stage") or "").strip()
        if stage in _CLOSED_DB_STAGES:
            continue
        result = results_by_entry.get(entry.get("id"))
        # Settled + fully disbursed ≡ Closed in Case Tracker.
        if stage in {"Settlement", "Settled"} and _is_fully_disbursed(result):
            continue
        ch = channel_by_case.get(case_no) or {}
        if _is_referred(entry, ch.get("topic_stage")):
            continue

        summary = summary_by_case.get(case_no)
        last_interaction, last_attempt = _contact_dates(summary, qualifying)
        status = _contact_status(last_interaction, last_attempt, now)
        days = _days_since(last_interaction, now)
        if days is None:
            # Never a qualifying contact — still overdue. Prefer attempt age,
            # else age of the tracker row, else a high sentinel.
            days = _days_since(last_attempt, now)
            if days is None:
                days = _days_since(entry.get("created_at"), now) or 999

        case_row = client_by_id.get(entry.get("case_id")) or {}
        client_name = (
            (case_row.get("client_name") or "").strip()
            or (entry.get("client_name_snapshot") or "").strip()
            or case_no
        )
        last_interaction_display = _format_when(last_interaction)
        # Dedup key: bump when qualifying contact changes OR status flips.
        alert_key = f"{last_interaction or 'never'}|{status}"

        yield CaseStatusRow(
            case_no=case_no,
            client_name=client_name,
            days_since_contact=days,
            last_interaction=last_interaction_display,
            alert_key=alert_key,
            contact_status=status,
            contact_status_label=_STATUS_LABEL[status],
            contact_status_help=_STATUS_HELP[status],
            last_attempt=_format_when(last_attempt),
            last_other_details=((summary or {}).get("last_other_details") or "").strip(),
            slack_channel_id=(ch.get("slack_channel_id") or "").strip(),
        )
