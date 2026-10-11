"""Email tramite SMTP (es. Brevo, Gmail con password per le app, il provider del tuo dominio)."""

import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

from ..config import settings


def recipient(lead: dict) -> str | None:
    email = (lead.get("email") or "").strip()
    return email if "@" in email else None


def send(to: str, subject: str, text: str) -> str:
    if not (settings.smtp_host and settings.email_from):
        raise RuntimeError("SMTP_HOST o EMAIL_FROM non configurati")
    msg = EmailMessage()
    msg["From"] = settings.email_from
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    msg.set_content(text.replace("\n", "\n\n"))
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)
    return msg["Message-ID"]
