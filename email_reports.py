"""SMTP email sending + HTML rendering for the per-bucket status emails.
Pure/standalone: no Slack, no Flask. Credentials come from env vars; the
recipients and schedule come from admin settings (read by the caller)."""

import logging
import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger(__name__)


def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and _from_addr())


def _from_addr() -> str:
    return (os.getenv("SMTP_FROM") or os.getenv("SMTP_USER") or "").strip()


def send_email(to_addrs: list, subject: str, html: str) -> bool:
    """Send one HTML email. Returns True on success. Never raises."""
    host = os.getenv("SMTP_HOST", "").strip()
    if not host or not to_addrs:
        return False
    port = int(os.getenv("SMTP_PORT", "587") or "587")
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    sender = _from_addr()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to_addrs)
    # Plain-text fallback so the message isn't flagged as HTML-only.
    msg.attach(MIMEText("This report is best viewed in an HTML email client.", "plain"))
    msg.attach(MIMEText(html, "html"))

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
        log.info("email sent: %r to %d recipient(s)", subject, len(to_addrs))
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
    return head + table + '</div>'


def _chip(label: str, value, color: str) -> str:
    return (
        f'<span style="display:inline-block;margin-right:10px;padding:6px 12px;'
        f'background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">'
        f'<b style="color:{color};font-size:16px">{value}</b> '
        f'<span style="color:#6b7280">{_e(label)}</span></span>'
    )
