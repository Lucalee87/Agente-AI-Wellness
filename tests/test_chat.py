"""Chat con Vittoria dopo l'invio del modulo (configurazione reale MIFIT, Claude simulato)."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import centers, chat, db, dialer, llm
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
    monkeypatch.setattr(settings, "modalita_contatto", "chat")
    return cfg


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(settings, "messaggi_attivi", True)
    monkeypatch.setattr(staff.sms_channel, "send", lambda to, subj, text: out.append(("sms", to, subj, text)) or "SM1")
    monkeypatch.setattr(staff.email_channel, "send", lambda to, subj, text: out.append(("email", to, subj, text)) or "E1")
    return out


def _text(t):
    return SimpleNamespace(type="text", text=t)


def _tool(name, args, id_="tu1"):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=args)


@pytest.fixture
def claude(monkeypatch):
    """Claude finto: restituisce le risposte in coda e ricorda i messaggi ricevuti."""
    script, calls = [], []

    def fake(system, messages):
        calls.append({"system": system, "messages": messages})
        blocks = script.pop(0)
        stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
        return SimpleNamespace(content=blocks, stop_reason=stop)

    monkeypatch.setattr(llm, "ask_claude", fake)
    return SimpleNamespace(script=script, calls=calls)


def _slot():
    return centers.free_slots("casoria", limit=1)[0].strftime("%Y-%m-%dT%H:%M")


def test_form_submit_opens_chat_with_greeting(mifit, client):
    r = client.post("/consulenza", data={"nome": "Maria", "telefono": "3331234567", "centro": "casoria",
                                         "consenso": "1", "src": "ig_dm"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/chat?t=")
    token = r.headers["location"].split("t=", 1)[1]
    assert client.get(f"/chat?t={token}").status_code == 200

    data = client.get(f"/chat/api/{token}").json()
    [apertura] = data["messaggi"]
    assert apertura["da"] == "vittoria" and "Maria, sono Vittoria, la consulente virtuale di MIFIT" in apertura["testo"]
    assert "Grazie per aver richiesto informazioni" in apertura["testo"]
    lead = db.get_lead(chat.get_session(token)["lead_id"])
    assert lead["status"] == db.CHAT and lead["next_attempt_at"] is None


def test_chat_books_and_sends_manager_email_with_conversation(mifit, client, claude, sent):
    lead_id = create_lead(LeadIn(source="web", telefono="3331234567", nome="Maria", centro_id="casoria")).lead_id
    token = chat.start_session(lead_id, "Maria")
    slot = _slot()

    claude.script += [
        [_text("Perfetto! Qual è il suo obiettivo principale?")],
    ]
    out = client.post(f"/chat/api/{token}", json={"testo": "Sì certo"}).json()
    assert out == {"messaggi": [{"da": "vittoria", "testo": "Perfetto! Qual è il suo obiettivo principale?"}],
                   "conclusa": False}
    first = claude.calls[0]
    assert "chat sul sito" in first["system"] and "telefonata" not in first["system"]
    assert first["messages"] == [{"role": "user", "content": [{"type": "text", "text": "Sì certo"}]}]

    claude.script += [
        [_tool("prenota_consulenza", {"centro_id": "casoria", "data_ora": slot, "tipo_valutazione": "posturale",
                                      "obiettivo": "mal di schiena"})],
        [_tool("registra_esito", {"esito": "prenotato"}, "tu2")],
        [_text("La aspettiamo in Via Eleonora Pimentel Fonseca 25. Grazie e a presto!")],
    ]
    out = client.post(f"/chat/api/{token}", json={"testo": "Va bene, confermo"}).json()
    assert out["conclusa"] is True
    assert out["messaggi"][-1]["testo"].startswith("La aspettiamo")
    # I risultati degli strumenti tornano a Claude nello storico
    last = claude.calls[-1]["messages"]
    assert last[-1]["content"][0]["type"] == "tool_result"

    assert db.get_lead(lead_id)["status"] == db.BOOKED
    [(channel, to, subject, text)] = sent
    assert subject.startswith("NUOVA VALUTAZIONE PRENOTATA - Maria")
    assert "Conversazione in chat:\nVittoria: Buongiorno Maria" in text or "Conversazione in chat:\nVittoria: Buonasera Maria" in text
    assert "Cliente: Va bene, confermo" in text
    assert chat.get_session(token)["status"] == "conclusa"
    # Nella pagina si vedono solo i testi, non gli strumenti
    testi = [m["testo"] for m in client.get(f"/chat/api/{token}").json()["messaggi"]]
    assert testi[1:] == ["Sì certo", "Perfetto! Qual è il suo obiettivo principale?", "Va bene, confermo",
                         "La aspettiamo in Via Eleonora Pimentel Fonseca 25. Grazie e a presto!"]


def test_chat_from_meta_link_finds_lead_by_phone(mifit, client):
    meta = create_lead(LeadIn(source="meta", source_ref="L1", telefono="3331234567", nome="Maria Rossi",
                              obiettivo="dimagrire")).lead_id
    r = client.post("/chat/inizia", json={"nome": "Maria", "telefono": "+39 333 123 4567", "consenso": True})
    token = r.json()["token"]
    assert chat.get_session(token)["lead_id"] == meta
    assert db.get_lead(meta)["status"] == db.CHAT

    assert client.post("/chat/inizia", json={"nome": "X", "telefono": "3331234567"}).status_code == 400
    assert client.post("/chat/inizia", json={"nome": "X", "telefono": "12", "consenso": True}).status_code == 400


def test_chat_before_meta_webhook_is_completed_later(mifit, client):
    token = client.post("/chat/inizia", json={"nome": "Maria", "telefono": "3331234567", "consenso": True,
                                              "src": "meta"}).json()["token"]
    lead_id = chat.get_session(token)["lead_id"]
    res = create_lead(LeadIn(source="meta", source_ref="L2", telefono="3331234567", nome="Maria Rossi",
                             email="maria@example.com", obiettivo="tonificare"))
    assert res.lead_id == lead_id and res.created is False
    lead = db.get_lead(lead_id)
    assert lead["nome"] == "Maria" and lead["email"] == "maria@example.com" and lead["obiettivo"] == "tonificare"


def test_do_not_call_gets_no_chat(mifit, client):
    a = create_lead(LeadIn(source="web", telefono="3331234567")).lead_id
    db.update_lead(a, status=db.DO_NOT_CALL)
    r = client.post("/chat/inizia", json={"nome": "Maria", "telefono": "3331234567", "consenso": True})
    assert r.json()["token"] is None


def test_unknown_token_and_message_limits(mifit, client, claude):
    assert client.get("/chat/api/nope").status_code == 404
    assert client.post("/chat/api/nope", json={"testo": "ciao"}).status_code == 400
    token = chat.start_session(create_lead(LeadIn(source="web", telefono="3331234567")).lead_id)
    assert client.post(f"/chat/api/{token}", json={"testo": "   "}).status_code == 400


def test_claude_error_shows_polite_message(mifit, client, monkeypatch):
    def boom(system, messages):
        raise RuntimeError("API giù")
    monkeypatch.setattr(llm, "ask_claude", boom)
    token = chat.start_session(create_lead(LeadIn(source="web", telefono="3331234567")).lead_id)
    out = client.post(f"/chat/api/{token}", json={"testo": "ciao"}).json()
    assert out["messaggi"][0]["testo"] == chat.ERRORE_TECNICO


def test_idle_chat_goes_to_manager(mifit, client, claude, sent):
    lead_id = create_lead(LeadIn(source="web", telefono="3331234567", nome="Maria", centro_id="casoria")).lead_id
    token = chat.start_session(lead_id, "Maria")
    claude.script.append([_text("Qual è il suo obiettivo principale?")])
    client.post(f"/chat/api/{token}", json={"testo": "Sì"})

    assert chat.close_idle() == 0
    assert chat.close_idle(now=datetime.now(timezone.utc) + timedelta(minutes=31)) == 1
    assert db.get_lead(lead_id)["status"] == db.STAFF_CALLBACK
    [(_, _, subject, text)] = sent
    assert subject.startswith("CHAT INTERROTTA - DA RICONTATTARE - Maria")
    assert "Cliente: Sì" in text and "Vittoria: Qual è il suo obiettivo principale?" in text
    assert chat.get_session(token)["status"] == "scaduta"


def test_unopened_chat_and_no_automatic_calls(mifit, sent, monkeypatch):
    lead_id = create_lead(LeadIn(source="meta", telefono="3331234567", nome="Maria", centro_id="casoria")).lead_id
    called = []
    monkeypatch.setattr(dialer, "dial", lambda lead: called.append(lead) or "c1")
    monkeypatch.setattr(centers, "is_calling_time", lambda at=None: True)
    assert dialer.tick() == 0 and called == []

    assert chat.flag_unopened() == 0
    assert chat.flag_unopened(now=datetime.now(timezone.utc) + timedelta(minutes=31)) == 1
    assert db.get_lead(lead_id)["status"] == db.STAFF_CALLBACK
    assert sent[0][2].startswith("CHAT NON APERTA - DA RICONTATTARE - Maria")


def test_callback_request_in_chat_goes_to_manager(mifit):
    lead_id = create_lead(LeadIn(source="web", telefono="3331234567", nome="Maria", centro_id="casoria")).lead_id
    out = execute_tool(lead_id, "registra_esito", {"esito": "richiamare", "richiamare_il": "2099-01-05T18:00"})
    assert "responsabile" in out
    lead = db.get_lead(lead_id)
    assert lead["status"] == db.STAFF_CALLBACK and lead["next_attempt_at"] is None


def test_chat_prompt_keeps_same_script(mifit):
    lead = {"id": 1, "nome": "Maria", "source": "meta", "centro_id": None}
    sp_chat, sp_tel = prompt.system_prompt(lead, "chat"), prompt.system_prompt(lead)
    for frase in ("Qual è il suo obiettivo principale", "È più comoda per Lei la sede di Casoria o quella di Lusciano?",
                  "proponi SOLO DUE opzioni", "Risposta sui prezzi", "Se nessun orario va bene"):
        assert frase in sp_chat and frase in sp_tel
    assert "Niente emoji" in sp_chat and "segreteria" not in sp_chat
    assert "Stai facendo una telefonata" in sp_tel and "segreteria" in sp_tel
    assert prompt.first_message(lead).endswith("Ha due minuti da dedicarmi per prenotare una valutazione gratuita?")
    assert "La chiamo" not in prompt.first_message(lead, "chat")
