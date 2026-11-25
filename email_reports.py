"""Email sending (Gmail API via the Google service account, or SMTP fallback)
plus HTML rendering for the per-bucket status emails. Pure/standalone: no
Slack, no Flask. Recipients and schedule come from admin settings (read by
the caller)."""

import base64
import json
import logging
import os
import re
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger(__name__)

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


def _email_sa_info() -> dict | None:
    """Service-account credential info dedicated to sending email. Resolution
    order (first that's usable wins):
      1. GMAIL_SERVICE_ACCOUNT_JSON — full service-account JSON.
      2. Individual fields — GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY plus the
         service-account email (GOOGLE_SERVICE_ACCOUNT_EMAIL /
         GOOGLE_CLIENT_EMAIL) and optional GOOGLE_CLIENT_ID.
      3. GOOGLE_SERVICE_ACCOUNT_JSON — the Sheets service account (fallback;
         only works if it too has Gmail domain-wide delegation).
    The dedicated one (1 or 2) is what you use when the Sheets SA isn't
    authorized for gmail.send."""
    j = os.getenv("GMAIL_SERVICE_ACCOUNT_JSON", "").strip()
    if j:
        try:
            return json.loads(j)
        except Exception:
            log.error("GMAIL_SERVICE_ACCOUNT_JSON is set but not valid JSON")

    pk = os.getenv("GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY", "").strip()
    email = (os.getenv("GOOGLE_SERVICE_ACCOUNT_EMAIL")
             or os.getenv("GOOGLE_CLIENT_EMAIL") or "").strip()
    if pk:
        if not email:
            log.error("GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY is set but the service "
                      "account email is missing — set GOOGLE_SERVICE_ACCOUNT_EMAIL "
                      "(the ...@...iam.gserviceaccount.com address).")
        else:
            return {
                "type": "service_account",
                "private_key": pk.replace("\\n", "\n"),
                "client_email": email,
                "client_id": os.getenv("GOOGLE_CLIENT_ID", "").strip() or None,
                "token_uri": "https://oauth2.googleapis.com/token",
            }

    j2 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if j2:
        try:
            return json.loads(j2)
        except Exception:
            log.error("GOOGLE_SERVICE_ACCOUNT_JSON is set but not valid JSON")
    return None


def _email_subject_user(sender: str) -> str:
    """The Workspace user to impersonate / send as. Env var wins so email can
    be configured entirely without the admin setting."""
    return (os.getenv("GOOGLE_WORKSPACE_IMPERSONATED_USER", "").strip()
            or (sender or "").strip())


def gmail_configured(sender: str) -> bool:
    """Gmail-API send is available when we have an email service account AND a
    Workspace user to send as."""
    return bool(_email_sa_info() and _email_subject_user(sender))


def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and _smtp_from_addr())


def configured(sender: str = "") -> bool:
    return gmail_configured(sender) or smtp_configured()


def _smtp_from_addr() -> str:
    return (os.getenv("SMTP_FROM") or os.getenv("SMTP_USER") or "").strip()


def _build_mime(to_addrs: list, subject: str, html: str, sender: str) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to_addrs)
    msg.attach(MIMEText("This report is best viewed in an HTML email client.", "plain"))
    msg.attach(MIMEText(html, "html"))
    return msg


def send_email(to_addrs: list, subject: str, html: str, sender: str = "") -> bool:
    """Send one HTML email. Prefers the Gmail API (service account) when a
    sender is configured, else SMTP. Returns True on success, never raises."""
    if not to_addrs:
        return False
    if gmail_configured(sender):
        return _gmail_send(to_addrs, subject, html, sender.strip())
    return _smtp_send(to_addrs, subject, html)


def _gmail_send(to_addrs: list, subject: str, html: str, sender: str) -> bool:
    """Send via the Gmail API, impersonating the Workspace user through the
    email service account's domain-wide delegation."""
    info = _email_sa_info()
    subj_user = _email_subject_user(sender)
    if not info or not subj_user:
        return False
    try:
        from google.oauth2.service_account import Credentials
        from google.auth.transport.requests import AuthorizedSession
    except Exception:
        log.exception("Gmail send: google-auth not installed")
        return False
    try:
        creds = Credentials.from_service_account_info(
            info, scopes=[GMAIL_SEND_SCOPE], subject=subj_user)
        session = AuthorizedSession(creds)
        raw = base64.urlsafe_b64encode(
            _build_mime(to_addrs, subject, html, subj_user).as_bytes()).decode()
        resp = session.post(
            f"https://gmail.googleapis.com/gmail/v1/users/{subj_user}/messages/send",
            json={"raw": raw}, timeout=30)
        if resp.status_code == 200:
            log.info("email sent (Gmail API): %r to %d recipient(s) as %s",
                     subject, len(to_addrs), subj_user)
            return True
        log.error("Gmail API send failed (%s) as %s: %s",
                  resp.status_code, subj_user, resp.text[:500])
        return False
    except Exception:
        log.exception("Gmail API send failed (subject=%r, as=%s) — if this is "
                      "'unauthorized_client', the service account's client ID "
                      "isn't authorized for the gmail.send scope in Workspace "
                      "domain-wide delegation", subject, subj_user)
        return False


def _smtp_send(to_addrs: list, subject: str, html: str) -> bool:
    host = os.getenv("SMTP_HOST", "").strip()
    if not host:
        return False
    port = int(os.getenv("SMTP_PORT", "587") or "587")
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    sender = _smtp_from_addr()
    msg = _build_mime(to_addrs, subject, html, sender)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as s:
                if user:
                    s.login(user, password)
                s.sendmail(sender, to_addrs, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.ehlo()
                try:
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                except smtplib.SMTPException:
                    log.debug("STARTTLS not available on %s:%s", host, port)
                if user:
                    s.login(user, password)
                s.sendmail(sender, to_addrs, msg.as_string())
        log.info("email sent (SMTP): %r to %d recipient(s)", subject, len(to_addrs))
        return True
    except Exception:
        log.exception("email send failed (subject=%r, host=%s:%s)", subject, host, port)
        return False


def permalink(workspace_url: str, channel_id: str, parent_ts: str,
              thread_ts: str = "") -> str:
    """Slack deep link: <workspace>/archives/<channel>/p<ts-without-dot>.

    A thread reply also carries thread_ts so the link opens that comment.
    """
    if not (workspace_url and channel_id and parent_ts):
        return ""
    url = (
        f"{workspace_url.rstrip('/')}/archives/{channel_id}/"
        f"p{str(parent_ts).replace('.', '')}"
    )
    thread = str(thread_ts or "")
    if thread and thread != str(parent_ts):
        url += f"?thread_ts={thread}&cid={channel_id}"
    return url


_ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}


def _e(s) -> str:
    return "".join(_ESC.get(c, c) for c in str(s if s is not None else ""))


def build_bucket_email_html(bucket_label: str, window_desc: str,
                            summary: dict, rows: list) -> str:
    """rows: dicts with channel_name, type_label, attorney, paralegal, la,
    opened, age, waiting, escalated, link (any may be empty)."""
    accent = "#e8385a"
    head = (
        f'<div style="font-family:Segoe UI,system-ui,Arial,sans-serif;color:#1a1a2e;'
        f'max-width:760px;margin:0 auto">'
        f'<h2 style="margin:0 0 .2rem">{_e(bucket_label)} — Open Items</h2>'
        f'<div style="color:#6b7280;font-size:13px;margin-bottom:14px">{_e(window_desc)}</div>'
        f'<div style="margin-bottom:16px">'
        f'{_chip("Created", summary.get("created",0), "#1a1a2e")}'
        f'{_chip("Open", summary.get("open",0), "#a16207")}'
        f'{_chip("Escalated", summary.get("escalated",0), "#b91c1c")}'
        f'</div>'
    )
    if not rows:
        return head + (
            '<div style="padding:18px;background:#f0fdf4;border-radius:8px;'
            'color:#166534;font-size:14px">Nothing open in this bucket for the window. '
            '✅</div></div>'
        )

    th = ('style="text-align:left;font-size:11px;text-transform:uppercase;'
          'letter-spacing:.05em;color:#6b7280;padding:6px 8px;'
          'border-bottom:1px solid #e5e7eb"')
    td = 'style="font-size:13px;padding:8px;border-bottom:1px solid #f3f4f6;vertical-align:top"'
    header = (
        f'<tr><th {th}>Channel</th><th {th}>Type</th><th {th}>Status</th>'
        f'<th {th}>Attorney</th><th {th}>Paralegal</th><th {th}>LA</th>'
        f'<th {th}>Opened</th><th {th}>Age</th><th {th}>Waiting on</th></tr>'
    )
    body = []
    for r in rows:
        chan = _e("#" + r["channel_name"])
        if r.get("link"):
            chan = f'<a href="{_e(r["link"])}" style="color:{accent};text-decoration:none">{chan}</a>'
        if r.get("escalated"):
            status = '<span style="color:#b91c1c;font-weight:700">Escalated</span>'
        else:
            status = '<span style="color:#a16207;font-weight:700">Open</span>'
        body.append(
            f'<tr><td {td}>{chan}</td><td {td}>{_e(r["type_label"])}</td>'
            f'<td {td}>{status}</td>'
            f'<td {td}>{_e(r.get("attorney") or "—")}</td>'
            f'<td {td}>{_e(r.get("paralegal") or "—")}</td>'
            f'<td {td}>{_e(r.get("la") or "—")}</td>'
            f'<td {td}>{_e(r.get("opened"))}</td>'
            f'<td {td}>{_e(r.get("age"))}</td>'
            f'<td {td}>{_e(r.get("waiting"))}</td></tr>'
        )
    table = (
        '<table style="border-collapse:collapse;width:100%">'
        f'<thead>{header}</thead><tbody>{"".join(body)}</tbody></table>'
    )
    howto = (
        '<div style="margin:0 0 14px;padding:12px 14px;background:#eff6ff;'
        'border-left:4px solid #3b82f6;border-radius:0 8px 8px 0;'
        'font-size:13px;color:#1e40af">'
        '<b>To close a task:</b> open its Slack thread (click the channel) and '
        'either reply <code>done</code> / <code>complete</code> — or the word '
        'shown under <b>Waiting on</b> (e.g. <code>scheduled</code>, '
        '<code>confirmed</code>) — or react ✅ on the bot’s message. '
        'It drops off this list right away. If the task no longer applies, '
        'reply <code>case closed</code> or <code>no longer needed</code> instead '
        '(it won’t count as completed).'
        '</div>'
    )
    return head + howto + table + '</div>'


_MENTION_STYLE = (
    "color:#1264a3;background-color:#e8f5fa;border-radius:3px;padding:0 3px"
)
_LINK_STYLE = "color:#1264a3;text-decoration:underline"
_SLACK_TOKEN_RE = re.compile(r"<([^<>\n]+)>")


def slack_message_html(text: str, directory=None) -> str:
    """Render a Slack message without changing its wording.

    User tags become names on a blue chip. Links, phone numbers, and the
    small set of Slack formatting marks are rendered. Everything else is
    escaped.
    """
    raw = text or ""
    parts = []
    pos = 0
    for match in _SLACK_TOKEN_RE.finditer(raw):
        parts.append(_mrkdwn_html(raw[pos:match.start()]))
        parts.append(_slack_token_html(match.group(1), directory))
        pos = match.end()
    parts.append(_mrkdwn_html(raw[pos:]))
    return "".join(parts)


def _mrkdwn_html(text: str) -> str:
    esc = _e(text)
    esc = re.sub(
        r"`([^`\n]+)`",
        r'<code style="font-family:Consolas,monospace;background:#f3f4f6;'
        r'padding:0 3px;border-radius:3px">\1</code>',
        esc,
    )
    esc = re.sub(r"\*([^*\n]+)\*", r"<strong>\1</strong>", esc)
    esc = re.sub(r"(?<![\w])_([^_\n]+)_(?![\w])", r"<em>\1</em>", esc)
    esc = re.sub(r"~([^~\n]+)~", r"<s>\1</s>", esc)
    return esc.replace("\n", "<br>")


def _mention_html(name: str) -> str:
    return f'<span style="{_MENTION_STYLE}">@{_e(name)}</span>'


def _anchor(href: str, label: str) -> str:
    return f'<a href="{_e(href)}" style="{_LINK_STYLE}">{_e(label)}</a>'


def _slack_token_html(inner: str, directory) -> str:
    if inner.startswith("@"):
        body = inner[1:]
        uid, _, label = body.partition("|")
        name = label or (directory.name(uid) if directory is not None else uid)
        return _mention_html(name)
    if inner.startswith("!subteam^"):
        body = inner.split("^", 1)[1]
        _gid, _, label = body.partition("|")
        return _mention_html((label or "group").lstrip("@"))
    if inner.startswith("#"):
        body = inner[1:]
        _cid, _, label = body.partition("|")
        return f'<span style="color:#1264a3">#{_e(label or "channel")}</span>'
    if inner.startswith("tel:"):
        body = inner[4:]
        number, _, label = body.partition("|")
        shown = label or number
        href = "tel:" + re.sub(r"[^\d+]", "", number)
        return _anchor(href, shown)
    if inner.startswith("mailto:"):
        body = inner[7:]
        addr, _, label = body.partition("|")
        return _anchor("mailto:" + addr, label or addr)
    if inner.startswith("http://") or inner.startswith("https://"):
        url, _, label = inner.partition("|")
        return _anchor(url, label or url)
    if inner in ("!here", "!channel", "!everyone"):
        return _mention_html(inner[1:])
    if inner.startswith("!"):
        return ""
    return _e(inner)


def _request_identity(row: dict):
    if row.get("request_key"):
        return ("key", row["request_key"])
    return (
        "row",
        row.get("channel_id") or "",
        row.get("link") or "",
        row.get("snippet") or "",
        row.get("when_ts") or 0,
    )


def unique_open_count(rows: list) -> int:
    """Shared requests count once, even when listed under two people."""
    return len({_request_identity(row) for row in rows})


def overdue_open_count(rows: list) -> int:
    seen = {}
    for row in rows:
        ident = _request_identity(row)
        seen[ident] = seen.get(ident, False) or bool(row.get("overdue"))
    return sum(1 for flagged in seen.values() if flagged)


def build_unanswered_email_html(window_desc: str, rows: list,
                                channels_scanned: int = 0,
                                note: str = "") -> str:
    """Person-first list of the original Slack messages still waiting.

    The header total is unique requests. A request tagged to two people
    is shown under each of them and counted once.
    """
    counts = unanswered_person_counts(rows)
    unique = unique_open_count(rows)
    overdue = overdue_open_count(rows)
    overdue_color = "#b91c1c" if overdue else "#6b7280"
    head = (
        '<div style="font-family:Segoe UI,system-ui,Arial,sans-serif;'
        'color:#1f2937;background:#ffffff;max-width:680px;margin:0 auto;'
        'padding:8px 4px">'
        '<h2 style="margin:0 0 4px;font-size:20px;font-weight:700;'
        'color:#1f2937">Unresolved Slack Requests</h2>'
        f'<div style="color:#6b7280;font-size:13px;margin:0 0 12px">'
        f'{_e(window_desc)}</div>'
        '<div style="margin:0 0 14px;line-height:1.4">'
        f'<span style="display:inline-block;margin:0 18px 6px 0;font-size:13px;'
        f'color:#6b7280"><b style="color:#1f2937;font-size:18px">{unique}</b> '
        f'open</span>'
        f'<span style="display:inline-block;margin:0 18px 6px 0;font-size:13px;'
        f'color:#6b7280"><b style="color:#1f2937;font-size:18px">{len(counts)}</b> '
        f'{"person" if len(counts) == 1 else "people"}</span>'
        f'<span style="display:inline-block;margin:0 18px 6px 0;font-size:13px;'
        f'color:#6b7280"><b style="color:{overdue_color};font-size:18px">'
        f'{overdue}</b> overdue</span>'
        '</div>'
    )
    if not rows:
        body = (
            '<div style="padding:8px 0;color:#166534;font-size:14px">'
            'No unresolved requests.</div>'
        )
    else:
        body = _unanswered_summary_line(counts) + _unanswered_people(rows)
    footer_bits = []
    if channels_scanned:
        footer_bits.append(f"Scanned {channels_scanned} case channels.")
    if note:
        footer_bits.append(note)
    footer = ""
    if footer_bits:
        footer = (
            '<div style="margin-top:16px;color:#6b7280;font-size:12px">'
            + " ".join(_e(bit) for bit in footer_bits)
            + "</div>"
        )
    return head + body + footer + "</div>"


def unanswered_person_counts(rows: list) -> list:
    """[(person_name, open_count)] with the most open items first."""
    counts = {}
    order = []
    for pid, name, _row in _unanswered_assignments(rows):
        key = (pid, name)
        if key not in counts:
            counts[key] = 0
            order.append(key)
        counts[key] += 1
    ranked = [(key[1], counts[key]) for key in order]
    return sorted(ranked, key=lambda pair: (-pair[1], pair[0].lower()))


def _unanswered_assignments(rows: list):
    """One entry per person per request. A shared request is listed twice."""
    out = []
    for row in rows:
        ids = list(row.get("waiting_ids") or [])
        names = [n for n in (row.get("waiting_names") or []) if n]
        if len(ids) > 1 or (len(names) > 1 and not row.get("waiting_name")):
            for i, name in enumerate(names):
                pid = ids[i] if i < len(ids) and ids[i] else name
                out.append((pid, name, row))
            continue
        if row.get("waiting_name"):
            name = row["waiting_name"]
        elif names:
            name = names[0]
        else:
            name = "Someone"
        pid = row.get("waiting_id") or (ids[0] if ids else name)
        out.append((pid, name, row))
    return out


def _unanswered_summary_line(counts: list) -> str:
    bits = [
        f'<b style="color:#1f2937">{_e(name)}</b> '
        f'<span style="color:#6b7280">{count}</span>'
        for name, count in counts
    ]
    return (
        '<div style="font-size:14px;line-height:1.7;color:#374151;'
        'margin:0 0 8px">'
        + " &nbsp;·&nbsp; ".join(bits)
        + "</div>"
    )


def _message_block(row: dict) -> str:
    if row.get("message_html"):
        body = row["message_html"]
    else:
        body = _mrkdwn_html(row.get("snippet") or "Open this comment")
    if row.get("link"):
        body = (
            f'<a href="{_e(row["link"])}" style="color:#1f2937;'
            f'text-decoration:none">{body}</a>'
        )
    return body


def _unanswered_people(rows: list) -> str:
    by_person = {}
    for pid, name, row in _unanswered_assignments(rows):
        by_person.setdefault((pid, name), []).append(row)
    people = sorted(
        by_person.items(),
        key=lambda item: (-len(item[1]), item[0][1].lower()),
    )
    parts = []
    for (_pid, name), items in people:
        items = sorted(
            items,
            key=lambda r: (0 if r.get("overdue") else 1, float(r.get("when_ts") or 0)),
        )
        blocks = []
        for row in items:
            label = "#" + (row.get("channel_name") or row.get("channel_id") or "channel")
            channel = _e(label)
            if row.get("channel_link"):
                channel = (
                    f'<a href="{_e(row["channel_link"])}" '
                    f'style="color:#1264a3;text-decoration:none;font-weight:600">'
                    f'{channel}</a>'
                )
            else:
                channel = (
                    f'<span style="color:#1264a3;font-weight:600">{channel}</span>'
                )
            context = ""
            if row.get("context_html"):
                context = (
                    '<div style="margin:4px 0;padding-left:8px;'
                    'border-left:2px solid #e5e7eb;color:#6b7280;font-size:13px">'
                    f'{row["context_html"]}</div>'
                )
            status = row.get("status_label") or "Awaiting action"
            status_color = "#b91c1c" if row.get("overdue") or status == "Overdue" else "#6b7280"
            meta = " · ".join(
                part for part in (
                    row.get("asked_by") or "Someone",
                    row.get("when") or "",
                    row.get("age") or "",
                ) if part
            )
            link = ""
            if row.get("link"):
                link = (
                    f' · <a href="{_e(row["link"])}" style="{_LINK_STYLE}">'
                    f'Open in Slack</a>'
                )
            blocks.append(
                '<div style="padding:10px 0;border-bottom:1px solid #e5e7eb">'
                f'<div style="font-size:13px;word-break:break-word">{channel}</div>'
                f'{context}'
                f'<div style="margin:4px 0 2px;font-size:14px;line-height:1.45;'
                f'color:#1f2937;word-break:break-word">{_message_block(row)}</div>'
                f'<div style="font-size:12px;color:#6b7280;line-height:1.5">'
                f'{_e(meta)} · <span style="color:{status_color}">{_e(status)}</span>'
                f'{link}</div></div>'
            )
        parts.append(
            '<div style="margin:18px 0 0;padding-top:12px;border-top:2px solid #1f2937">'
            f'<div style="font-size:16px;font-weight:700;color:#1f2937">'
            f'{_e(name)}'
            f'<span style="font-weight:600;font-size:13px;color:#6b7280"> · '
            f'{len(items)} open</span></div>'
            + "".join(blocks)
            + "</div>"
        )
    return "".join(parts)


def _chip(label: str, value, color: str) -> str:
    return (
        f'<span style="display:inline-block;margin-right:10px;padding:6px 12px;'
        f'background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">'
        f'<b style="color:{color};font-size:16px">{value}</b> '
        f'<span style="color:#6b7280">{_e(label)}</span></span>'
    )
