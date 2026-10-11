"""Conferme e promemoria al cliente (configurazione reale MIFIT)."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import centers, db, messaging
from app.config import settings
from app.leads import LeadIn, create_lead

REAL = str(Path(__file__).resolve().parent.parent / "config" / "centri.yaml")


@pytest.fixture
def mifit(monkeypatch):
    cfg = centers.load_config(REAL)
    monkeypatch.setattr(centers, "load_config", lambda path=None: cfg)
    return cfg


def _appointment(start_local: str, tipo: str | None, email: str | None = "luca@example.com"):
    lead_id = create_lead(LeadIn(source="web", telefono="3331234567", nome="luca rossi",
                                 centro_id="casoria", email=email)).lead_id
    with db.tx() as c:
        appt_id = c.execute(
            "INSERT INTO appointments (lead_id, centro_id, start_at, created_at, tipo) VALUES (?, 'casoria', ?, ?, ?)",
            (lead_id, start_local, db.now_utc(), tipo),
        ).lastrowid
    return appt_id


def _messages(appt_id):
    with db.tx() as c:
        return [dict(r) for r in c.execute("SELECT * FROM messages WHERE appointment_id = ? ORDER BY id", (appt_id,))]


def _utc(local: str):
    return centers.parse_local(local).astimezone(timezone.utc)


def test_plan_three_messages_when_booked_two_days_before(mifit):
    appt = _appointment("2026-10-14T18:10", "dimagrimento_tono")  # mercoledì
    messaging.schedule_for_appointment(appt, now=_utc("2026-10-12T11:00"))  # lunedì
    msgs = _messages(appt)
    kinds = sorted({(m["kind"], m["send_at"]) for m in msgs})
    assert kinds == sorted({
        ("conferma", _utc("2026-10-12T11:00").isoformat()),
        ("giorno_prima", _utc("2026-10-13T10:00").isoformat()),
        ("ore_prima", _utc("2026-10-14T16:10").isoformat()),
    })
    assert {m["channel"] for m in msgs} == {"sms", "email"}


def test_no_day_before_reminder_if_booked_the_day_before(mifit):
    appt = _appointment("2026-10-14T18:10", "posturale")
    messaging.schedule_for_appointment(appt, now=_utc("2026-10-13T17:00"))
    assert {m["kind"] for m in _messages(appt)} == {"conferma", "ore_prima"}


def test_early_morning_reminder_not_before_8(mifit):
    appt = _appointment("2026-10-14T09:00", "posturale")
    messaging.schedule_for_appointment(appt, now=_utc("2026-10-10T12:00"))
    ore_prima = [m for m in _messages(appt) if m["kind"] == "ore_prima"][0]
    assert ore_prima["send_at"] == _utc("2026-10-14T08:00").isoformat()


def test_texts_by_type(mifit):
    lead = {"nome": "luca rossi"}
    base = {"centro_id": "casoria", "start_at": "2026-10-14T18:10"}
    _, dim = messaging.render("conferma", {**base, "tipo": "dimagrimento_tono"}, lead)
    assert dim.startswith("Gentile Luca, le confermiamo la valutazione gratuita dello stato di forma "
                          "mercoledì 14 ottobre alle 18:10 presso MIFIT Casoria in Via Eleonora Pimentel Fonseca 25")
    assert "digiuno da almeno 2 ore" in dim and "referti" not in dim
    assert dim.endswith("scrivendo al +39 320 172 0477. A presto")
    _, post = messaging.render("conferma", {**base, "tipo": "posturale"}, lead)
    assert "referti" in post and "digiuno" not in post
    _, comb = messaging.render("conferma", {**base, "tipo": "combinata"}, lead)
    assert "referti" in comb and "digiuno" in comb
    _, generic = messaging.render("conferma", {**base, "tipo": None}, lead)
    assert "\n\n" not in generic and "referti" not in generic
    _, domani = messaging.render("giorno_prima", {**base, "tipo": "posturale"}, lead)
    assert "domani, mercoledì 14 ottobre, alle 18:10" in domani
    # Apostrofi dritti: l'SMS resta nella codifica economica
    for text in (dim, post, comb, domani):
        assert "’" not in text


def test_send_due_simulated_and_email_skipped_without_address(mifit, monkeypatch):
    monkeypatch.setattr(settings, "messaggi_attivi", False)
    appt = _appointment("2099-10-14T18:10", "posturale", email=None)
    messaging.schedule_for_appointment(appt, now=datetime.now(timezone.utc))
    messaging.send_due()
    by_channel = {m["channel"]: m["status"] for m in _messages(appt) if m["kind"] == "conferma"}
    assert by_channel == {"sms": "simulato", "email": "saltato"}
    # I promemoria futuri restano in attesa
    assert all(m["status"] == "pending" for m in _messages(appt) if m["kind"] != "conferma")


def test_send_due_real_and_retry(mifit, monkeypatch):
    monkeypatch.setattr(settings, "messaggi_attivi", True)
    sent = []
    calls = {"n": 0}

    def flaky_send(to, subject, text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("rete giù")
        sent.append((to, text))
        return "SM123"

    monkeypatch.setattr(messaging.sms_channel, "send", flaky_send)
    monkeypatch.setitem(mifit.messaggi, "canali", ["sms"])
    appt = _appointment("2099-10-14T18:10", "dimagrimento_tono")
    now = datetime.now(timezone.utc)
    messaging.schedule_for_appointment(appt, now=now)
    assert messaging.send_due(now=now) == 0           # primo tentativo fallisce
    assert messaging.send_due(now=now + timedelta(minutes=6)) == 1
    conferma = [m for m in _messages(appt) if m["kind"] == "conferma"][0]
    assert conferma["status"] == "inviato" and conferma["provider_id"] == "SM123" and conferma["attempts"] == 2
    assert sent[0][0] == "+393331234567" and sent[0][1].startswith("MIFIT\nLuca, le confermo")


def test_cancel_and_reschedule_via_booking_tool(mifit, monkeypatch):
    from app.voice.tools import execute_tool
    monkeypatch.setattr(settings, "messaggi_attivi", False)
    lead_id = create_lead(LeadIn(source="web", telefono="3339998888", nome="Anna", centro_id="casoria")).lead_id
    s1, s2 = centers.free_slots("casoria", limit=2)
    for s in (s1, s2):
        out = execute_tool(lead_id, "prenota_consulenza", {"centro_id": "casoria", "data_ora": s.strftime("%Y-%m-%dT%H:%M"),
                                                           "tipo_valutazione": "posturale"})
        assert out.startswith("Prenotazione confermata")
    with db.tx() as c:
        appts = [dict(r) for r in c.execute("SELECT * FROM appointments WHERE lead_id = ? ORDER BY id", (lead_id,))]
    old, new = appts
    assert new["tipo"] == "posturale"
    assert all(m["status"] == "annullato" for m in _messages(old["id"]) if m["kind"] != "conferma")
    assert any(m["status"] == "pending" for m in _messages(new["id"]))


def test_short_sms_texts(mifit):
    lead = {"nome": "luca rossi"}
    appt = {"centro_id": "casoria", "start_at": "2026-10-14T18:10", "tipo": "dimagrimento_tono"}
    morning = _utc("2026-10-12T11:00")
    _, sms = messaging.render("conferma", appt, lead, channel="sms", now=morning)
    assert sms == ("MIFIT\nLuca, le confermo la Valutazione Gratuita mer 14/10 ore 18:10, "
                   "presso MIFIT in Via E. Pimentel Fonseca 25, Casoria. Buona Giornata")
    _, evening = messaging.render("conferma", appt, lead, channel="sms", now=_utc("2026-10-12T19:00"))
    assert evening.endswith("Buona Serata")
    for kind in ("conferma", "giorno_prima", "ore_prima"):
        _, text = messaging.render(kind, appt, lead, channel="sms", now=morning)
        assert len(text) <= 160, (kind, len(text))
    # L'email resta nella versione completa
    _, mail = messaging.render("conferma", appt, lead, channel="email", now=morning)
    assert mail.startswith("Gentile Luca") and "bioimpedenza" in mail


def test_two_hours_sms_reminds_fasting_only_for_bioimpedance(mifit):
    lead = {"nome": "Luca"}
    base = {"centro_id": "casoria", "start_at": "2026-10-14T18:10"}
    _, dim = messaging.render("ore_prima", {**base, "tipo": "dimagrimento_tono"}, lead, channel="sms")
    assert dim == ("MIFIT\nLuca, la aspettiamo oggi alle 18:10 presso MIFIT in Via E. Pimentel Fonseca 25, Casoria. "
                   "Ricordi: digiuno da 2h e 1 bicchiere d'acqua 1h prima. A presto")
    assert len(dim) <= 160
    _, comb = messaging.render("ore_prima", {**base, "tipo": "combinata"}, lead, channel="sms")
    assert "digiuno" in comb
    _, post = messaging.render("ore_prima", {**base, "tipo": "posturale"}, lead, channel="sms")
    assert post.endswith("Casoria. A presto") and "digiuno" not in post
