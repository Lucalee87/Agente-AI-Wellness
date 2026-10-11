"""Sedi, ricontatto dal responsabile e avvisi via email allo staff (configurazione reale MIFIT)."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import centers, db, dialer
from app.config import settings
from app.leads import LeadIn, create_lead
from app.messaging import staff
from app.voice import prompt
from app.voice.tools import execute_tool

REAL = str(Path(__file__).resolve().parent.parent / "config" / "centri.yaml")


@pytest.fixture
def mifit(monkeypatch):
    cfg = centers.load_config(REAL)
    monkeypatch.setattr(centers, "load_config", lambda path=None: cfg)
    return cfg


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(settings, "messaggi_attivi", True)
    monkeypatch.setattr(staff.sms_channel, "send", lambda to, subj, text: out.append(("sms", to, subj, text)) or "SM1")
    monkeypatch.setattr(staff.email_channel, "send", lambda to, subj, text: out.append(("email", to, subj, text)) or "E1")
    return out


def _lead(centro=None):
    return create_lead(LeadIn(source="meta", telefono="3331234567", nome="Luca Rossi", centro_id=centro)).lead_id


def test_two_sedi_and_question_after_goal(mifit):
    assert set(mifit.centri) == {"casoria", "lusciano"}
    sp = prompt.system_prompt({"id": 1, "nome": "Luca", "source": "meta", "centro_id": None})
    i_goal = sp.index("Qual è il suo obiettivo principale")
    i_sede = sp.index("È più comoda per Lei la sede di Casoria o quella di Lusciano?")
    i_expl = sp.index("3. In base all'obiettivo")
    assert i_goal < i_sede < i_expl
    assert "MIFIT Lusciano, Via Carminiello 66, Lusciano (CE)" in sp


def test_lusciano_without_hours_goes_to_manager(mifit):
    a = _lead()
    out = execute_tool(a, "trova_disponibilita", {"centro_id": "lusciano"})
    assert "richiedi_ricontatto_responsabile" in out


def test_call_events_sent_by_email_with_transcript_at_end_of_call(mifit, sent):
    a = _lead()
    execute_tool(a, "richiedi_ricontatto_responsabile", {
        "centro_id": "lusciano", "fascia_oraria": "mattina tra le 10 e le 12", "obiettivo": "mal di schiena"})
    assert sent == []  # in attesa della fine della chiamata
    lead = db.get_lead(a)
    assert lead["status"] == db.STAFF_CALLBACK and lead["centro_id"] == "lusciano"

    dialer.handle_end_of_call(a, {
        "endedReason": "assistant-ended-call", "call": {"id": "c1"},
        "artifact": {"transcript": "AI: Buonasera Luca...\nUser: Ho mal di schiena", "recordingUrl": "https://rec/1.mp3"},
        "analysis": {"summary": "Da richiamare la mattina."},
    })
    [(channel, to, subject, text)] = sent  # solo email, niente SMS
    assert channel == "email" and to == "mifitassistenza@gmail.com"
    assert subject.startswith("DA RICHIAMARE - Luca Rossi - MIFIT Lusciano")
    assert "mattina tra le 10 e le 12" in text and "Obiettivo: mal di schiena" in text
    assert "Trascrizione della chiamata:\nVittoria: Buonasera Luca...\nCliente: Ho mal di schiena" in text
    assert "Registrazione: https://rec/1.mp3" in text
    # Il lead non torna nella coda automatica
    assert db.get_lead(a)["status"] == db.STAFF_CALLBACK


def test_not_interested_and_booking_are_notified(mifit, sent):
    a = _lead("casoria")
    execute_tool(a, "registra_esito", {"esito": "non_interessato", "note": "si è già iscritto altrove"})
    staff.flush_for_lead(a)
    assert sent[-1][2].startswith("NON INTERESSATO - Luca Rossi - MIFIT Casoria")

    b = create_lead(LeadIn(source="web", telefono="3339990000", nome="Anna", centro_id="casoria")).lead_id
    slot = centers.free_slots("casoria", limit=1)[0].strftime("%Y-%m-%dT%H:%M")
    execute_tool(b, "prenota_consulenza", {"centro_id": "casoria", "data_ora": slot, "tipo_valutazione": "posturale",
                                           "note": "ernia L5"})
    staff.flush_for_lead(b)
    subject, text = sent[-1][2], sent[-1][3]
    assert subject.startswith("NUOVA VALUTAZIONE PRENOTATA - Anna")
    assert "Risolvere problemi posturali" in text and "ernia L5" in text
    assert all(c == "email" for c, *_ in sent)


def test_stale_notifications_sent_without_transcript(mifit, sent):
    a = _lead("casoria")
    execute_tool(a, "registra_esito", {"esito": "non_interessato"})
    assert staff.flush_stale() == 0
    assert staff.flush_stale(now=datetime.now(timezone.utc) + timedelta(minutes=31)) == 1
    assert "Trascrizione" not in sent[0][3]


def test_unreachable_lead_notified_immediately(mifit, sent):
    a = _lead("casoria")
    attempts = len(mifit.tentativi_minuti)
    db.update_lead(a, status=db.CALLING, attempts=attempts)
    dialer.handle_end_of_call(a, {"endedReason": "customer-did-not-answer", "call": {"id": "z"}})
    assert db.get_lead(a)["status"] == db.UNREACHABLE
    assert sent and sent[0][2].startswith("LEAD NON RAGGIUNGIBILE")
