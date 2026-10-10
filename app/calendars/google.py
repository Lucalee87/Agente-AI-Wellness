"""Google Calendar tramite account di servizio (service account).

Configurazione:
1. In Google Cloud crea un progetto, abilita la "Google Calendar API" e crea un
   account di servizio con una chiave JSON.
2. In Google Calendar, condividi il calendario di ogni centro con l'email
   dell'account di servizio, con il permesso "Apportare modifiche agli eventi".
3. Imposta GOOGLE_SERVICE_ACCOUNT_FILE (percorso del JSON) oppure
   GOOGLE_SERVICE_ACCOUNT_JSON (contenuto del JSON) e, in centri.yaml,
   `google_calendar_id` per ogni centro (lo trovi in Impostazioni calendario →
   "Integra calendario" → "ID calendario").

Gli eventi "Disponibile" (trasparenti) non bloccano gli slot; gli eventi "Occupato",
compresi quelli inseriti a mano dallo staff, sì.
"""

import json
import threading
from datetime import datetime, time, timedelta
from functools import lru_cache
from urllib.parse import quote

import httpx

from ..config import settings

API = "https://www.googleapis.com/calendar/v3"
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


class GoogleCalendarError(Exception):
    pass


class GoogleCalendar:
    def __init__(self, credentials):
        self._creds = credentials
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ auth
    def _token(self) -> str:
        from google.auth.transport.requests import Request

        with self._lock:
            if not self._creds.valid:
                self._creds.refresh(Request())
            return self._creds.token

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        r = httpx.request(method, f"{API}{path}", headers={"Authorization": f"Bearer {self._token()}"},
                          timeout=15, **kwargs)
        if r.status_code >= 400 and not (method == "DELETE" and r.status_code in (404, 410)):
            raise GoogleCalendarError(f"Google Calendar {method} {path}: {r.status_code} {r.text[:300]}")
        return r

    @staticmethod
    def _events_path(calendar_id: str) -> str:
        return f"/calendars/{quote(calendar_id, safe='')}/events"

    # ------------------------------------------------------------------ API
    def busy(self, centro, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        out: list[tuple[datetime, datetime]] = []
        params = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "singleEvents": "true",
            "maxResults": "2500",
        }
        while True:
            data = self._request("GET", self._events_path(centro.google_calendar_id), params=params).json()
            for ev in data.get("items", []):
                interval = event_interval(ev, start.tzinfo)
                if interval:
                    out.append(interval)
            if not data.get("nextPageToken"):
                return out
            params["pageToken"] = data["nextPageToken"]

    def create_event(self, centro, appointment: dict, lead: dict) -> str:
        from . import appointment_span

        start, end = appointment_span(appointment, centro.durata_consulenza_min)
        tz = str(start.tzinfo)
        nome = lead.get("nome") or "Nuovo cliente"
        righe = [
            f"Telefono: {lead.get('telefono', '')}",
            f"Email: {lead.get('email') or '-'}",
            f"Obiettivo: {lead.get('obiettivo') or '-'}",
            f"Note: {lead.get('note') or '-'}",
            f"Provenienza: {lead.get('source', '')}",
            "",
            "Prenotato dall'assistente AI.",
        ]
        body = {
            "summary": f"Consulenza – {nome}",
            "location": centro.indirizzo,
            "description": "\n".join(righe),
            "start": {"dateTime": start.isoformat(), "timeZone": tz},
            "end": {"dateTime": end.isoformat(), "timeZone": tz},
            "extendedProperties": {"private": {
                "lead_id": str(appointment["lead_id"]),
                "appointment_id": str(appointment["id"]),
                "origine": "agente-ai-wellness",
            }},
        }
        return self._request("POST", self._events_path(centro.google_calendar_id), json=body).json()["id"]

    def delete_event(self, centro, event_id: str) -> None:
        self._request("DELETE", f"{self._events_path(centro.google_calendar_id)}/{quote(event_id, safe='')}")


def event_interval(ev: dict, tz) -> tuple[datetime, datetime] | None:
    """Intervallo occupato da un evento Google, o None se non blocca gli slot."""
    if ev.get("status") == "cancelled" or ev.get("transparency") == "transparent":
        return None
    for att in ev.get("attendees", []):
        if att.get("self") and att.get("responseStatus") == "declined":
            return None
    s, e = ev.get("start", {}), ev.get("end", {})
    if "dateTime" in s and "dateTime" in e:
        return datetime.fromisoformat(s["dateTime"]), datetime.fromisoformat(e["dateTime"])
    if "date" in s and "date" in e:  # evento di un'intera giornata (es. "Chiuso per ferie")
        d0 = datetime.combine(datetime.fromisoformat(s["date"]).date(), time(0), tzinfo=tz)
        d1 = datetime.combine(datetime.fromisoformat(e["date"]).date(), time(0), tzinfo=tz)
        return d0, max(d1, d0 + timedelta(days=1))
    return None


@lru_cache
def google_calendar() -> GoogleCalendar:
    from google.oauth2 import service_account

    if settings.google_service_account_json:
        info = json.loads(settings.google_service_account_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    elif settings.google_service_account_file:
        creds = service_account.Credentials.from_service_account_file(settings.google_service_account_file, scopes=SCOPES)
    else:
        raise GoogleCalendarError(
            "google_calendar_id impostato ma mancano GOOGLE_SERVICE_ACCOUNT_FILE / GOOGLE_SERVICE_ACCOUNT_JSON"
        )
    return GoogleCalendar(creds)
