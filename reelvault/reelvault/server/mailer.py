"""Outbound email (DESIGN-SAAS.md 11.1, 4.2, 4.3). WP1.

Email is optional. It works only when the host sets ONE of:
    SMTP_URL        smtp://user:pass@smtp.example.com:587   (STARTTLS)   or   smtps://user:pass@host:465 (TLS)
                    user and password are URL-encoded; add ?from=ReelVault%20%3Cno-reply%40example.com%3E to set From
    RESEND_API_KEY  re_...   (https://resend.com; From must be on a domain verified there)
    MAIL_FROM       optional From line, e.g. "ReelVault <no-reply@app.example.com>" (default no-reply@<PUBLIC_URL host>)

    mailer.configured() -> bool
    mailer.send(to, subject, text) -> None        # background thread; never raises, never blocks a request
    mailer.pending_signup(user) / youre_in(user) / reset_link(user, url)   # the fixed emails (plain words)

Secrets (SMTP password, API key) never reach logs: errors are passed through security.redact first.
The last delivery problem is kept in app_settings `internal.mail_last_error` for Admin > Health.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from urllib.parse import parse_qs, unquote, urlparse

from .settings import get_settings

log = logging.getLogger("reelvault.mail")

BACKGROUND = True          # tests set False to deliver in the calling thread
TIMEOUT_S = 20


class MailError(Exception):
    pass


def configured() -> bool:
    s = get_settings()
    return bool(s.smtp_url or s.resend_api_key)


def from_address() -> str:
    s = get_settings()
    if s.smtp_url:
        q = parse_qs(urlparse(s.smtp_url).query)
        if q.get("from"):
            return q["from"][0]
    if s.mail_from:
        return s.mail_from
    host = (urlparse(s.public_url).hostname or "localhost")
    return formataddr(("ReelVault", f"no-reply@{host}"))


def _secrets() -> list[str]:
    s = get_settings()
    out = [s.resend_api_key or ""]
    if s.smtp_url:
        u = urlparse(s.smtp_url)
        out += [unquote(u.password or ""), u.password or ""]
    return [x for x in out if x]


def _smtp(msg: EmailMessage) -> None:
    u = urlparse(get_settings().smtp_url or "")
    if u.scheme not in ("smtp", "smtps") or not u.hostname:
        raise MailError("SMTP_URL must start with smtp:// or smtps://")
    port = u.port or (465 if u.scheme == "smtps" else 587)
    ctx = ssl.create_default_context()
    if u.scheme == "smtps":
        server: smtplib.SMTP = smtplib.SMTP_SSL(u.hostname, port, timeout=TIMEOUT_S, context=ctx)
    else:
        server = smtplib.SMTP(u.hostname, port, timeout=TIMEOUT_S)
    try:
        server.ehlo()
        if u.scheme == "smtp" and server.has_extn("starttls"):
            server.starttls(context=ctx)
            server.ehlo()
        if u.username:
            server.login(unquote(u.username), unquote(u.password or ""))
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except smtplib.SMTPException:
            pass


def _resend(msg: EmailMessage) -> None:
    import httpx

    r = httpx.post("https://api.resend.com/emails", timeout=TIMEOUT_S,
                   headers={"Authorization": f"Bearer {get_settings().resend_api_key}"},
                   json={"from": msg["From"], "to": [msg["To"]], "subject": msg["Subject"],
                         "text": msg.get_content()})
    if r.status_code >= 300:
        raise MailError(f"Resend answered {r.status_code}: {r.text[:200]}")


def _deliver(msg: EmailMessage) -> None:
    """Send one message with the configured backend. Raises on failure."""
    if get_settings().smtp_url:
        _smtp(msg)
    else:
        _resend(msg)


def build(to: str, subject: str, text: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_address()
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=urlparse(get_settings().public_url).hostname or "localhost")
    msg.set_content(text)
    return msg


def _run(msg: EmailMessage) -> bool:
    from . import db, security

    try:
        _deliver(msg)
        log.info("email sent (%s)", msg["Subject"])
        return True
    except Exception as e:  # noqa: BLE001
        reason = security.redact(f"{type(e).__name__}: {e}", _secrets())[:300]
        log.warning("email failed: %s", reason)
        try:
            db.set_setting("internal.mail_last_error", {"at": db.iso(db.utcnow()), "error": reason})
        except Exception:  # noqa: BLE001
            pass
        return False


def send(to: str, subject: str, text: str) -> bool:
    """Queue one plain-text email. Returns False at once when email is not set up. Never raises."""
    if not configured() or not to:
        return False
    msg = build(to, subject, text)
    if not BACKGROUND:
        return _run(msg)
    threading.Thread(target=_run, args=(msg,), name="rv-mail", daemon=True).start()
    return True


# ============================================================================================== the fixed emails

def _url(fragment: str = "") -> str:
    return f"{get_settings().public_url}/{fragment}"


def pending_signup(user) -> int:
    """One email to every active admin about a new pending signup (needs notify_admin_email). Returns count."""
    from sqlalchemy import select

    from . import db

    if not configured() or not db.get_setting("notify_admin_email"):
        return 0
    with db.tx(immediate=False) as conn:
        admins = [r[0] for r in conn.execute(select(db.users.c.email).where(db.users.c.role == "admin",
                                                                           db.users.c.status == "active"))]
    skool = user.skool_url or "(no Skool link given)"
    text = (f"{user.name} wants to join ReelVault.\n\n"
            f"Email: {user.email}\nSkool profile: {skool}\n\n"
            "Check that they are in your Skool group, then approve or reject them here:\n"
            f"{_url('#admin/members')}\n")
    for a in admins:
        send(a, f"New member waiting: {user.name}", text)
    return len(admins)


def youre_in(user) -> bool:
    text = (f"Hi {user.name},\n\nYou're in. The admin approved your ReelVault account.\n\n"
            f"Sign in here: {_url()}\n\n"
            "You'll need Chrome, Edge or Brave on a computer to read Instagram reels.\n")
    return send(user.email, "You're in: your ReelVault account is ready", text)


def reset_link(user, url: str) -> bool:
    text = (f"Hi {user.name},\n\nSomeone asked to reset your ReelVault password. If it was you, open this link "
            f"within 24 hours and pick a new password:\n\n{url}\n\n"
            "If it wasn't you, ignore this email. Your password stays the same.\n")
    return send(user.email, "Reset your ReelVault password", text)
