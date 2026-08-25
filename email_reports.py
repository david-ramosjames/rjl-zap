"""Email sending (Gmail API via the Google service account, or SMTP fallback)
plus HTML rendering for the per-bucket status emails. Pure/standalone: no
Slack, no Flask. Recipients and schedule come from admin settings (read by
the caller)."""

import base64
import json
import logging
import os
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


def permalink(workspace_url: str, channel_id: str, parent_ts: str) -> str:
    """Slack deep link: <workspace>/archives/<channel>/p<ts-without-dot>."""
    if not (workspace_url and channel_id and parent_ts):
        return ""
    return f"{workspace_url.rstrip('/')}/archives/{channel_id}/p{parent_ts.replace('.', '')}"


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
        'It drops off this list right away.'
        '</div>'
    )
    return head + howto + table + '</div>'


def _chip(label: str, value, color: str) -> str:
    return (
        f'<span style="display:inline-block;margin-right:10px;padding:6px 12px;'
        f'background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">'
        f'<b style="color:{color};font-size:16px">{value}</b> '
        f'<span style="color:#6b7280">{_e(label)}</span></span>'
    )
