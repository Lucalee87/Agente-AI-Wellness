from datetime import datetime, timedelta, timezone

from app import centers, db, dialer
from app.leads import LeadIn, create_lead
from app.voice import prompt, vapi
from app.voice.tools import execute_tool

VAPI_HEADERS = {"x-vapi-secret": "vapi-secret"}


def _new_lead(phone="3331234567", centro="napoli-centro", nome="Marco Verdi"):
    return create_lead(LeadIn(source="web", telefono=phone, nome=nome, centro_id=centro)).lead_id


def _first_free_slot(centro="napoli-centro"):
    return centers.free_slots(centro, limit=1)[0].strftime("%Y-%m-%dT%H:%M")


def test_prompt_and_assistant_config():
    lead = db.get_lead(_new_lead())
    assert prompt.first_message(lead).startswith("Ciao Marco!")
    assistant = vapi.build_assistant(lead)
    assert assistant["model"]["provider"] == "anthropic"
    names = {t.get("function", {}).get("name") for t in assistant["model"]["tools"]}
    assert {"trova_disponibilita", "prenota_consulenza", "registra_esito"} <= names
    assert "FitClub Napoli Centro" in assistant["model"]["messages"][0]["content"]


def test_availability_and_booking_prevents_double_booking():
    a, b = _new_lead("3331111111"), _new_lead("3332222222")
    out = execute_tool(a, "trova_disponibilita", {"centro_id": "napoli-centro"})
    assert "Slot liberi" in out
    slot = _first_free_slot()
    assert slot in out

    ok = execute_tool(a, "prenota_consulenza", {"centro_id": "napoli-centro", "data_ora": slot, "obiettivo": "dimagrire"})
    assert ok.startswith("Prenotazione confermata")
    lead = db.get_lead(a)
    assert lead["status"] == db.BOOKED and lead["obiettivo"] == "dimagrire"

    taken = execute_tool(b, "prenota_consulenza", {"centro_id": "napoli-centro", "data_ora": slot})
    assert "non è più disponibile" in taken
    assert slot not in execute_tool(b, "trova_disponibilita", {"centro_id": "napoli-centro"})


def test_booking_rejects_invalid_slot():
    a = _new_lead()
    assert "non valido" in execute_tool(a, "prenota_consulenza", {"centro_id": "boh", "data_ora": "2030-01-01T10:00"})
    past = "2020-01-06T10:00"
    assert "non è più disponibile" in execute_tool(a, "prenota_consulenza", {"centro_id": "napoli-centro", "data_ora": past})


def test_do_not_call_blocks_future_leads():
    a = _new_lead()
    execute_tool(a, "registra_esito", {"esito": "non_chiamare_piu"})
    assert db.get_lead(a)["status"] == db.DO_NOT_CALL
    res = create_lead(LeadIn(source="google", telefono="+39 333 1234567", source_ref="x"))
    assert res.created is False


def test_callback_request_schedules_retry():
    a = _new_lead()
    when = (centers.local_now() + timedelta(days=2)).replace(hour=18, minute=0).strftime("%Y-%m-%dT%H:%M")
    execute_tool(a, "registra_esito", {"esito": "richiamare", "richiamare_il": when, "note": "in riunione"})
    lead = db.get_lead(a)
    assert lead["status"] == db.RETRY
    assert lead["summary"] == "in riunione"
    assert lead["next_attempt_at"] > db.now_utc()


def test_dialer_and_vapi_webhooks(client, monkeypatch):
    monkeypatch.setattr(centers, "is_calling_time", lambda at=None: True)
    a = _new_lead()
    db.update_lead(a, next_attempt_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())

    assert dialer.tick() == 1
    lead = db.get_lead(a)
    assert lead["status"] == db.CALLING and lead["attempts"] == 1
    with db.tx() as c:
        call_id = c.execute("SELECT provider_call_id FROM calls WHERE lead_id = ?", (a,)).fetchone()[0]

    assert client.post("/webhooks/vapi", json={"message": {"type": "tool-calls"}}).status_code == 403

    # L'agente cerca disponibilità durante la chiamata
    r = client.post("/webhooks/vapi", headers=VAPI_HEADERS, json={"message": {
        "type": "tool-calls",
        "call": {"id": call_id},
        "toolCallList": [{"id": "t1", "type": "function",
                          "function": {"name": "trova_disponibilita", "arguments": '{"centro_id": "napoli-centro"}'}}],
    }})
    assert r.status_code == 200
    [res] = r.json()["results"]
    assert res["toolCallId"] == "t1" and "Slot liberi" in res["result"]

    # Il lead non risponde: report di fine chiamata -> nuovo tentativo pianificato
    r = client.post("/webhooks/vapi", headers=VAPI_HEADERS, json={"message": {
        "type": "end-of-call-report", "endedReason": "customer-did-not-answer", "call": {"id": call_id},
    }})
    assert r.status_code == 200
    lead = db.get_lead(a)
    assert lead["status"] == db.RETRY
    with db.tx() as c:
        call = dict(c.execute("SELECT * FROM calls WHERE provider_call_id = ?", (call_id,)).fetchone())
    assert call["outcome"] == "nessuna_risposta" and call["ended_at"]


def test_end_of_call_keeps_booked_status(client):
    a = _new_lead()
    slot = _first_free_slot()
    r = client.post("/webhooks/vapi", headers=VAPI_HEADERS, json={"message": {
        "type": "tool-calls",
        "call": {"id": "c-meta", "metadata": {"lead_id": a}},
        "toolCallList": [{"id": "t1", "function": {"name": "prenota_consulenza",
                                                   "arguments": {"centro_id": "napoli-centro", "data_ora": slot}}}],
    }})
    assert r.json()["results"][0]["result"].startswith("Prenotazione confermata")
    client.post("/webhooks/vapi", headers=VAPI_HEADERS, json={"message": {
        "type": "end-of-call-report", "endedReason": "assistant-ended-call",
        "call": {"id": "c-meta", "metadata": {"lead_id": a}},
        "artifact": {"transcript": "AI: Ciao...", "recordingUrl": "https://example.com/r.mp3"},
        "analysis": {"summary": "Prenotata consulenza."},
    }})
    lead = db.get_lead(a)
    assert lead["status"] == db.BOOKED
    assert lead["summary"] == "Prenotata consulenza."


def test_retries_exhausted_marks_unreachable():
    a = _new_lead()
    attempts = len(centers.load_config().tentativi_minuti)
    db.update_lead(a, status=db.CALLING, attempts=attempts)
    dialer.handle_end_of_call(a, {"endedReason": "customer-busy", "call": {"id": "z"}})
    assert db.get_lead(a)["status"] == db.UNREACHABLE


def test_next_calling_time_skips_night_and_sunday():
    tz = centers.load_config().tz
    sunday_night = datetime(2026, 10, 11, 23, 0, tzinfo=tz)  # domenica
    nxt = centers.next_calling_time(sunday_night)
    assert nxt.weekday() == 0 and nxt.hour == 9 and nxt.minute == 0


def test_admin_requires_password(client):
    _new_lead()
    assert client.get("/admin").status_code == 401
    r = client.get("/admin", auth=("admin", "admin-pass"))
    assert r.status_code == 200 and "Marco Verdi" in r.text


def test_cancel_booking_when_person_wants_to_think():
    a = _new_lead()
    slot = _first_free_slot()
    assert execute_tool(a, "prenota_consulenza", {"centro_id": "napoli-centro", "data_ora": slot}).startswith("Prenotazione")
    out = execute_tool(a, "annulla_prenotazione", {"motivo": "ci vuole pensare"})
    assert out.startswith("Prenotazione annullata")
    assert db.get_lead(a)["status"] == db.RETRY
    assert slot in execute_tool(a, "trova_disponibilita", {"centro_id": "napoli-centro"})  # lo slot torna libero
    assert "Non c'è nessuna prenotazione" in execute_tool(a, "annulla_prenotazione", {})
    when = (centers.local_now() + timedelta(days=2)).replace(hour=18, minute=0).strftime("%Y-%m-%dT%H:%M")
    execute_tool(a, "registra_esito", {"esito": "richiamare", "richiamare_il": when})
    assert db.get_lead(a)["next_attempt_at"] > db.now_utc()
