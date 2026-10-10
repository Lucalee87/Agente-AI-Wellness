from datetime import datetime, timedelta

import httpx
import pytest

from app import calendars, centers, db, dialer
from app.calendars import google as gcal
from app.leads import LeadIn, create_lead
from app.voice.tools import execute_tool


class FakeCalendar:
    def __init__(self):
        self.events: dict[str, tuple[datetime, datetime, dict]] = {}
        self.fail = False

    def busy(self, centro, start, end):
        return [(s, e) for s, e, _ in self.events.values() if s < end and e > start]

    def create_event(self, centro, appointment, lead):
        if self.fail:
            raise RuntimeError("Google non raggiungibile")
        start, end = calendars.appointment_span(appointment, centro.durata_consulenza_min)
        event_id = f"ev{len(self.events) + 1}"
        self.events[event_id] = (start, end, {"lead": lead["nome"]})
        return event_id

    def delete_event(self, centro, event_id):
        self.events.pop(event_id, None)


@pytest.fixture
def cal():
    fake = FakeCalendar()
    calendars.set_backend_override(fake)
    yield fake
    calendars.set_backend_override(None)


def _lead(phone="3331234567", nome="Marco Verdi"):
    return create_lead(LeadIn(source="web", telefono=phone, nome=nome, centro_id="napoli-centro")).lead_id


def _appointments():
    with db.tx() as c:
        return [dict(r) for r in c.execute("SELECT * FROM appointments ORDER BY id")]


def test_staff_events_block_slots(cal):
    first = centers.free_slots("napoli-centro", limit=1)[0]
    # Lo staff inserisce a mano un impegno che copre il primo slot libero
    cal.events["manuale"] = (first, first + timedelta(minutes=30), {})
    assert first not in centers.free_slots("napoli-centro", limit=5)
    assert not centers.is_slot_free("napoli-centro", first)


def test_booking_creates_event_without_double_counting(cal):
    a = _lead()
    slot = centers.free_slots("napoli-centro", limit=1)[0]
    out = execute_tool(a, "prenota_consulenza", {"centro_id": "napoli-centro", "data_ora": slot.strftime("%Y-%m-%dT%H:%M")})
    assert out.startswith("Prenotazione confermata")
    [appt] = _appointments()
    assert appt["calendar_event_id"] == "ev1" and appt["calendar_sync"] == "ok"
    assert cal.events["ev1"][0] == slot and cal.events["ev1"][2]["lead"] == "Marco Verdi"
    # Slot occupato una sola volta (evento Google), non anche dalla riga locale
    assert centers._occupancy(centers.load_config().centri["napoli-centro"], slot, slot + timedelta(hours=1))(slot) == 1


def test_reschedule_deletes_old_event(cal):
    a = _lead()
    s1, s2 = centers.free_slots("napoli-centro", limit=2)
    execute_tool(a, "prenota_consulenza", {"centro_id": "napoli-centro", "data_ora": s1.strftime("%Y-%m-%dT%H:%M")})
    execute_tool(a, "prenota_consulenza", {"centro_id": "napoli-centro", "data_ora": s2.strftime("%Y-%m-%dT%H:%M")})
    old, new = _appointments()
    assert old["status"] == "sostituito" and old["calendar_sync"] == "deleted"
    assert list(cal.events) == [new["calendar_event_id"]]


def test_failed_sync_keeps_slot_and_retries(cal, monkeypatch):
    cal.fail = True
    a, b = _lead("3331111111"), _lead("3332222222", "Anna")
    slot = centers.free_slots("napoli-centro", limit=1)[0].strftime("%Y-%m-%dT%H:%M")
    assert execute_tool(a, "prenota_consulenza", {"centro_id": "napoli-centro", "data_ora": slot}).startswith("Prenotazione")
    [appt] = _appointments()
    assert appt["calendar_sync"] == "error" and appt["calendar_event_id"] is None
    # Lo slot resta bloccato anche se Google non ha ancora l'evento
    assert "non è più disponibile" in execute_tool(b, "prenota_consulenza", {"centro_id": "napoli-centro", "data_ora": slot})

    cal.fail = False
    monkeypatch.setattr(centers, "is_calling_time", lambda at=None: False)
    dialer.tick()  # la coda riprova la sincronizzazione
    [appt] = _appointments()
    assert appt["calendar_sync"] == "ok" and appt["calendar_event_id"] == "ev1"


def test_calendar_down_falls_back_to_local(cal):
    def boom(*a, **k):
        raise RuntimeError("timeout")
    cal.busy = boom
    assert centers.free_slots("napoli-centro", limit=3)


def test_event_interval_rules():
    tz = centers.load_config().tz
    timed = {"start": {"dateTime": "2026-10-12T10:00:00+02:00"}, "end": {"dateTime": "2026-10-12T11:00:00+02:00"}}
    assert gcal.event_interval(timed, tz)[0].hour == 10
    assert gcal.event_interval({**timed, "transparency": "transparent"}, tz) is None
    assert gcal.event_interval({**timed, "status": "cancelled"}, tz) is None
    allday = {"start": {"date": "2026-10-12"}, "end": {"date": "2026-10-13"}}
    s, e = gcal.event_interval(allday, tz)
    assert (e - s) == timedelta(days=1) and s.tzinfo is tz


def test_google_client_requests(monkeypatch):
    class Creds:
        valid, token = True, "tok"

    calls = []

    def fake_request(method, url, headers=None, timeout=None, **kw):
        calls.append((method, url, kw))
        req = httpx.Request(method, url)
        if method == "GET":
            if "pageToken" not in kw["params"]:
                return httpx.Response(200, json={"items": [
                    {"start": {"dateTime": "2026-10-12T10:00:00+02:00"}, "end": {"dateTime": "2026-10-12T11:00:00+02:00"}},
                ], "nextPageToken": "p2"}, request=req)
            return httpx.Response(200, json={"items": [
                {"transparency": "transparent", "start": {"dateTime": "2026-10-12T12:00:00+02:00"},
                 "end": {"dateTime": "2026-10-12T13:00:00+02:00"}}]}, request=req)
        if method == "POST":
            return httpx.Response(200, json={"id": "abc"}, request=req)
        return httpx.Response(410, request=req)

    monkeypatch.setattr(gcal.httpx, "request", fake_request)
    centro = centers.load_config().centri["napoli-centro"]
    monkeypatch.setattr(centro, "google_calendar_id", "palestra@group.calendar.google.com")
    client = gcal.GoogleCalendar(Creds())
    tz = centers.load_config().tz

    busy = client.busy(centro, datetime(2026, 10, 12, tzinfo=tz), datetime(2026, 10, 13, tzinfo=tz))
    assert len(busy) == 1 and len(calls) == 2
    assert "palestra%40group.calendar.google.com" in calls[0][1]

    event_id = client.create_event(centro, {"id": 7, "lead_id": 3, "start_at": "2026-10-12T15:00"},
                                   {"nome": "Sara", "telefono": "+393331112222", "source": "meta"})
    body = calls[-1][2]["json"]
    assert event_id == "abc"
    assert body["summary"] == "Consulenza – Sara"
    assert body["start"]["timeZone"] == "Europe/Rome"
    assert body["end"]["dateTime"].startswith("2026-10-12T15:45")
    client.delete_event(centro, "abc")  # 410 = già cancellato: nessun errore
