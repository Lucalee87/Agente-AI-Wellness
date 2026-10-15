"""Chat sul sito: Vittoria scrive al cliente appena ha inviato il modulo.

Stesso copione, stessi strumenti e stessi avvisi della telefonata; cambia solo il
mezzo. Ogni chat è legata a un lead e identificata da un token casuale che resta
nel browser del cliente. La conversazione completa (strumenti compresi) è salvata
in `chat_messages`, così Claude riparte dal punto giusto a ogni messaggio.
"""

import json
import logging
import secrets
import threading
from datetime import datetime, timedelta, timezone

from . import centers, db, leads, llm
from .config import settings
from .messaging import staff
from .voice import prompt
from .voice.tools import execute_tool

log = logging.getLogger(__name__)

MAX_MESSAGGI_CLIENTE = 40
MAX_CARATTERI = 1000
MAX_GIRI_STRUMENTI = 8
# Strumenti che chiudono la conversazione: dopo, l'avviso al responsabile parte subito
STRUMENTI_DI_CHIUSURA = {"registra_esito", "richiedi_ricontatto_responsabile"}
ERRORE_TECNICO = ("Mi scusi, ho un piccolo problema tecnico. Può riscrivermi tra qualche istante? "
                  "In alternativa la faremo ricontattare dal nostro staff.")

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class ChatError(Exception):
    """Errore da mostrare al cliente (chat inesistente, troppi messaggi...)."""


def _lock(token: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(token, threading.Lock())


def _lead_for_session(session: dict) -> dict:
    lead = db.get_lead(session["lead_id"]) or {}
    # Il saluto usa il nome scritto in chat: chi conosce solo il numero non vede i dati del modulo
    if session.get("nome"):
        lead = {**lead, "nome": session["nome"]}
    return lead


def start_session(lead_id: int, nome: str | None = None) -> str:
    """Apre una nuova chat per il lead e salva il messaggio di apertura. Restituisce il token."""
    token = secrets.token_urlsafe(24)
    now = db.now_utc()
    with db.tx() as c:
        session_id = c.execute(
            "INSERT INTO chat_sessions (token, lead_id, nome, created_at, last_activity_at) VALUES (?, ?, ?, ?, ?)",
            (token, lead_id, (nome or "").strip() or None, now, now),
        ).lastrowid
        lead = db.get_lead(lead_id) or {}
        if lead.get("status") in (db.NEW, db.RETRY):
            db.update_lead(lead_id, status=db.CHAT, next_attempt_at=None)
    session = get_session(token)
    opening = prompt.first_message(_lead_for_session(session), "chat")
    _save(session_id, "apertura", [{"type": "text", "text": opening}])
    log.info("Chat %s aperta per il lead %s", session_id, lead_id)
    return token


def get_session(token: str) -> dict | None:
    with db.tx() as c:
        row = c.execute("SELECT * FROM chat_sessions WHERE token = ?", (token,)).fetchone()
    return dict(row) if row else None


def _save(session_id: int, role: str, content: list) -> None:
    with db.tx() as c:
        c.execute(
            "INSERT INTO chat_messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (session_id, role, json.dumps(content, ensure_ascii=False), db.now_utc()),
        )
        c.execute("UPDATE chat_sessions SET last_activity_at = ? WHERE id = ?", (db.now_utc(), session_id))


def _rows(session_id: int) -> list[dict]:
    with db.tx() as c:
        rows = c.execute("SELECT role, content FROM chat_messages WHERE session_id = ? ORDER BY id",
                         (session_id,)).fetchall()
    return [{"role": r["role"], "content": json.loads(r["content"])} for r in rows]


def _texts(content: list) -> list[str]:
    return [b["text"].strip() for b in content if b.get("type") == "text" and b.get("text", "").strip()]


def visible_messages(session_id: int) -> list[dict]:
    """Messaggi da mostrare nella pagina: solo i testi, senza gli strumenti."""
    out = []
    for r in _rows(session_id):
        da = "cliente" if r["role"] == "user" else "vittoria"
        out += [{"da": da, "testo": t} for t in _texts(r["content"])]
    return out


def transcript(session_id: int) -> str:
    nome = centers.load_config().brand.get("nome_assistente", "Vittoria")
    return "\n".join(f"{'Cliente' if m['da'] == 'cliente' else nome}: {m['testo']}"
                     for m in visible_messages(session_id))


def _api_messages(session_id: int) -> list:
    # L'apertura è nel prompt di sistema: all'API passano solo i turni successivi
    return [{"role": r["role"], "content": r["content"]} for r in _rows(session_id) if r["role"] != "apertura"]


def _blocks(response) -> list:
    out = []
    for b in response.content:
        if b.type == "text":
            out.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return out


def reply(token: str, testo: str) -> dict:
    """Registra il messaggio del cliente, fa rispondere Vittoria e restituisce i nuovi messaggi."""
    testo = (testo or "").strip()[:MAX_CARATTERI]
    if not testo:
        raise ChatError("Messaggio vuoto.")
    with _lock(token):
        session = get_session(token)
        if session is None:
            raise ChatError("Chat non trovata.")
        n_cliente = sum(1 for m in visible_messages(session["id"]) if m["da"] == "cliente")
        if n_cliente >= MAX_MESSAGGI_CLIENTE:
            raise ChatError("Abbiamo raggiunto il limite di messaggi: la ricontatteremo noi a breve.")
        _save(session["id"], "user", [{"type": "text", "text": testo}])
        nuovi, chiusa = _run_agent(session)
        if chiusa and session["status"] == "aperta":
            with db.tx() as c:
                c.execute("UPDATE chat_sessions SET status = 'conclusa' WHERE id = ?", (session["id"],))
        if chiusa or session["status"] != "aperta":
            _flush_staff(session)
        return {"messaggi": [{"da": "vittoria", "testo": t} for t in nuovi],
                "conclusa": chiusa or session["status"] == "conclusa"}


def _run_agent(session: dict) -> tuple[list[str], bool]:
    lead_id = session["lead_id"]
    system = prompt.system_prompt(_lead_for_session(session), "chat")
    nuovi: list[str] = []
    chiusa = False
    for _ in range(MAX_GIRI_STRUMENTI):
        try:
            response = llm.ask_claude(system, _api_messages(session["id"]))
        except Exception:
            log.exception("Risposta di Claude non riuscita (chat %s)", session["id"])
            return nuovi + [ERRORE_TECNICO], chiusa
        if response.stop_reason == "refusal":
            return nuovi + ["Mi scusi, non ho capito bene. Può riformulare?"], chiusa
        content = _blocks(response)
        if content:
            _save(session["id"], "assistant", content)
        nuovi += _texts(content)
        tool_uses = [b for b in content if b["type"] == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_uses:
            break
        results = []
        for tu in tool_uses:
            out = execute_tool(lead_id, tu["name"], tu["input"])
            results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": out})
            chiusa = chiusa or tu["name"] in STRUMENTI_DI_CHIUSURA
        _save(session["id"], "user", results)
    return nuovi, chiusa


def _flush_staff(session: dict) -> None:
    try:
        lead = db.get_lead(session["lead_id"]) or {}
        staff.flush_for_lead(session["lead_id"], transcript=transcript(session["id"]),
                             summary=lead.get("summary"), canale="chat")
    except Exception:
        log.exception("Avviso al responsabile per la chat %s non riuscito", session["id"])


def close_idle(now: datetime | None = None) -> int:
    """Chiude le chat ferme da troppo tempo: se manca un esito, il responsabile ricontatta il cliente."""
    now = now or datetime.now(timezone.utc)
    limit = (now - timedelta(minutes=settings.chat_inattivita_minuti)).replace(microsecond=0).isoformat()
    with db.tx() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM chat_sessions WHERE status = 'aperta' AND last_activity_at <= ?", (limit,))]
    for s in rows:
        with db.tx() as c:
            c.execute("UPDATE chat_sessions SET status = 'scaduta' WHERE id = ?", (s["id"],))
        lead = db.get_lead(s["lead_id"]) or {}
        if lead.get("status") == db.CHAT:
            db.update_lead(s["lead_id"], status=db.STAFF_CALLBACK, next_attempt_at=None)
            leads.notify("chat_interrotta", s["lead_id"])
        _flush_staff(s)
    return len(rows)


def flag_unopened(now: datetime | None = None) -> int:
    """Lead arrivati da un po' che non hanno mai aperto la chat: avviso al responsabile."""
    now = now or datetime.now(timezone.utc)
    limit = (now - timedelta(minutes=settings.chat_attesa_minuti)).replace(microsecond=0).isoformat()
    with db.tx() as c:
        ids = [r[0] for r in c.execute(
            """SELECT id FROM leads l WHERE status = ? AND created_at <= ?
               AND NOT EXISTS (SELECT 1 FROM chat_sessions s WHERE s.lead_id = l.id)""",
            (db.NEW, limit))]
    for lead_id in ids:
        db.update_lead(lead_id, status=db.STAFF_CALLBACK, next_attempt_at=None,
                       summary="Non ha aperto la chat dopo il modulo")
        leads.notify("chat_non_aperta", lead_id)
    return len(ids)
