"""Calendari esterni in cui scrivere le consulenze prenotate.

Ogni centro può avere un "backend" di calendario: oggi Google Calendar, domani il
gestionale della palestra. Un backend deve saper fare tre cose:

- `busy(centro, start, end)`: gli intervalli occupati, per calcolare gli slot liberi;
- `create_event(centro, appointment, lead)`: scrivere la consulenza e restituirne l'id;
- `delete_event(centro, event_id)`: cancellarla (es. se il cliente sposta l'appuntamento).
"""

import logging
from datetime import datetime, timedelta
from typing import Protocol

from .. import db

log = logging.getLogger(__name__)


class CalendarBackend(Protocol):
    def busy(self, centro, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]: ...

    def create_event(self, centro, appointment: dict, lead: dict) -> str: ...

    def delete_event(self, centro, event_id: str) -> None: ...


# Permette ai test (o a integrazioni future) di sostituire il backend
_override: CalendarBackend | None = None


def set_backend_override(backend: CalendarBackend | None) -> None:
    global _override
    _override = backend


def backend_for(centro) -> CalendarBackend | None:
    if _override is not None:
        return _override
    if getattr(centro, "google_calendar_id", ""):
        from .google import google_calendar
        return google_calendar()
    return None


def sync_appointment(appointment_id: int) -> bool:
    """Scrive (o cancella) nel calendario esterno un singolo appuntamento. True se tutto ok."""
    from .. import centers

    with db.tx() as c:
        row = c.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
    if row is None:
        return True
    appt = dict(row)
    centro = centers.load_config().centri.get(appt["centro_id"])
    backend = backend_for(centro) if centro else None
    if backend is None:
        return True
    try:
        if appt["status"] == "confermato" and not appt["calendar_event_id"]:
            lead = db.get_lead(appt["lead_id"]) or {}
            event_id = backend.create_event(centro, appt, lead)
            _set(appointment_id, calendar_event_id=event_id, calendar_sync="ok")
        elif appt["status"] != "confermato" and appt["calendar_event_id"] and appt["calendar_sync"] != "deleted":
            backend.delete_event(centro, appt["calendar_event_id"])
            _set(appointment_id, calendar_sync="deleted")
        return True
    except Exception as e:
        log.exception("Sincronizzazione calendario fallita per l'appuntamento %s", appointment_id)
        _set(appointment_id, calendar_sync="error", calendar_error=str(e)[:500])
        return False


def sync_pending() -> None:
    """Riprova le sincronizzazioni non riuscite (chiamato periodicamente dalla coda)."""
    with db.tx() as c:
        ids = [r[0] for r in c.execute(
            """SELECT id FROM appointments
               WHERE (status = 'confermato' AND calendar_event_id IS NULL AND calendar_sync IN ('pending', 'error'))
                  OR (status != 'confermato' AND calendar_event_id IS NOT NULL AND calendar_sync != 'deleted')"""
        )]
    for appointment_id in ids:
        sync_appointment(appointment_id)


def appointment_span(appointment: dict, durata_min: int) -> tuple[datetime, datetime]:
    from .. import centers
    start = centers.parse_local(appointment["start_at"])
    return start, start + timedelta(minutes=durata_min)


def _set(appointment_id: int, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.tx() as c:
        c.execute(f"UPDATE appointments SET {cols} WHERE id = ?", (*fields.values(), appointment_id))
