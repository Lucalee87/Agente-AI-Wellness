"""SMS tramite Twilio (lo stesso account usato per il numero di telefono)."""

import httpx

from ..config import settings


def recipient(lead: dict) -> str | None:
    return lead.get("telefono")


def send(to: str, subject: str, text: str) -> str:
    if not (settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_sms_from):
        raise RuntimeError("TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN o TWILIO_SMS_FROM non configurati")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Messages.json"
    r = httpx.post(
        url,
        data={"To": to, "From": settings.twilio_sms_from, "Body": text},
        auth=(settings.twilio_account_sid, settings.twilio_auth_token),
        timeout=20,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Twilio ha risposto {r.status_code}: {r.text[:300]}")
    return r.json().get("sid", "")
