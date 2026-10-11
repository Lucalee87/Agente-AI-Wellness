"""Ricontatto dal responsabile e avvisi allo staff (configurazione reale MIFIT)."""

from pathlib import Path

import pytest

from app import centers, db, dialer
from app.config import settings
from app.leads import LeadIn, create_lead
from app.messaging import staff
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
    monkeypatch.setattr(staff.sms_channel, "send", lambda to, subj, text: out.append(("sms", to, text)) or "SM1")
    monkeypatch.setattr(staff.email_channel, "send", lambda to, subj, text: out.append(("email", to, text)) or "E1")
    return out


def _lead():
    return create_lead(LeadIn(source="meta", telefono="3331234567", nome="Luca Rossi", centro_id="casoria")).lead_id


def test_callback_request_notifies_manager_and_stops_dialing(mifit, sent, monkeypatch):
    a = _lead()
    out = execute_tool(a, "richiedi_ricontatto_responsabile", {
        "fascia_oraria": "mattina tra le 10 e le 12", "obiettivo": "mal di schiena", "note": "lavora il pomeriggio"})
    assert out.startswith("Richiesta inoltrata")
    lead = db.get_lead(a)
    assert lead["status"] == db.STAFF_CALLBACK and lead["next_attempt_at"] is None
    [(channel, to, text)] = sent  # email del responsabile non configurata
    assert channel == "sms" and to == "+393201720477"
    assert "MIFIT Casoria - DA RICHIAMARE" in text and "+393331234567" in text
    assert "mattina tra le 10 e le 12" in text and "mal di schiena" in text and "lavora il pomeriggio" in text
    # L'agente non lo richiama più in automatico, anche se registra un esito
    execute_tool(a, "registra_esito", {"esito": "richiamare"})
    assert db.get_lead(a)["status"] == db.STAFF_CALLBACK
    monkeypatch.setattr(centers, "is_calling_time", lambda at=None: True)
    assert all(l["id"] != a for l in dialer.due_leads(10))


def test_booking_notifies_manager(mifit, sent):
    a = _lead()
    slot = centers.free_slots("casoria", limit=1)[0].strftime("%Y-%m-%dT%H:%M")
    execute_tool(a, "prenota_consulenza", {"centro_id": "casoria", "data_ora": slot, "tipo_valutazione": "posturale",
                                           "note": "ernia L5"})
    texts = [t for c, _, t in sent if c == "sms"]
    assert any("NUOVA VALUTAZIONE PRENOTATA" in t and "Risolvere problemi posturali" in t and "ernia L5" in t
               for t in texts)


def test_events_not_listed_are_not_sent(mifit, sent):
    a = _lead()
    execute_tool(a, "registra_esito", {"esito": "non_interessato"})
    assert sent == []
