"""Archivio SQLite per lead, chiamate e appuntamenti."""

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,              -- meta | google | dm | web | generic
    source_ref TEXT,                   -- id del lead nella piattaforma di origine
    nome TEXT,
    telefono TEXT NOT NULL,            -- formato E.164 (+39...)
    email TEXT,
    centro_id TEXT,
    obiettivo TEXT,
    note TEXT,
    extra TEXT,                        -- risposte aggiuntive del modulo (JSON)
    status TEXT NOT NULL DEFAULT 'new',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_call_at TEXT,
    summary TEXT,
    UNIQUE (source, source_ref)
);
CREATE INDEX IF NOT EXISTS idx_leads_queue ON leads (status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads (telefono);

CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    provider_call_id TEXT UNIQUE,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    ended_reason TEXT,
    outcome TEXT,
    transcript TEXT,
    recording_url TEXT,
    summary TEXT
);

CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    centro_id TEXT NOT NULL,
    start_at TEXT NOT NULL,            -- ora locale del centro, YYYY-MM-DDTHH:MM
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'confermato',
    calendar_event_id TEXT,            -- id dell'evento nel calendario esterno (Google, gestionale)
    calendar_sync TEXT,                -- pending | ok | error | deleted (NULL = nessun calendario)
    calendar_error TEXT,
    tipo TEXT                          -- tipo di valutazione (es. dimagrimento_tono, posturale, combinata)
);
CREATE INDEX IF NOT EXISTS idx_appt_slot ON appointments (centro_id, start_at);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER NOT NULL REFERENCES appointments(id),
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    kind TEXT NOT NULL,                -- conferma | giorno_prima | ore_prima
    channel TEXT NOT NULL,             -- sms | email | whatsapp
    send_at TEXT NOT NULL,             -- UTC
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | inviato | simulato | annullato | saltato | errore
    attempts INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT,
    provider_id TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_due ON messages (status, send_at);

CREATE TABLE IF NOT EXISTS staff_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    event TEXT NOT NULL,
    payload TEXT,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | inviato | simulato | errore | saltato
    sent_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_staff_pending ON staff_notifications (status, created_at);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT NOT NULL UNIQUE,        -- identifica la chat nel browser del cliente
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    nome TEXT,                         -- nome scritto dal cliente all'avvio della chat
    created_at TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'aperta'   -- aperta | conclusa | scaduta
);
CREATE INDEX IF NOT EXISTS idx_chat_lead ON chat_sessions (lead_id);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES chat_sessions(id),
    role TEXT NOT NULL,                -- apertura | user | assistant
    content TEXT NOT NULL,             -- blocchi del messaggio (JSON)
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_messages ON chat_messages (session_id, id);
"""

# Stati possibili di un lead
NEW = "new"                    # appena arrivato, da chiamare
RETRY = "retry"                # da richiamare (non ha risposto o ha chiesto di essere richiamato)
CALLING = "calling"            # chiamata in corso
BOOKED = "booked"              # consulenza prenotata
NOT_INTERESTED = "not_interested"
UNREACHABLE = "unreachable"    # tentativi esauriti
INVALID = "invalid"            # numero errato
DO_NOT_CALL = "do_not_call"    # ha chiesto di non essere più contattato
STAFF_CALLBACK = "staff_callback"  # da richiamare a mano dal responsabile del centro
CHAT = "chat"                  # conversazione in chat in corso

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_depth = threading.local()


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: str | None = None) -> sqlite3.Connection:
    """Apre (o riapre) la connessione condivisa e crea le tabelle."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = sqlite3.connect(path or settings.database_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA foreign_keys = ON")
        _conn.executescript(SCHEMA)
        _migrate(_conn)
        return _conn


# Colonne aggiunte dopo la prima versione: le creiamo sui database già esistenti
_ADDED_COLUMNS = {
    "appointments": {"calendar_event_id": "TEXT", "calendar_sync": "TEXT", "calendar_error": "TEXT", "tipo": "TEXT"},
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in _ADDED_COLUMNS.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in cols.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    conn.commit()


def ensure_connected() -> None:
    if _conn is None:
        connect()


@contextmanager
def tx():
    """Transazione serializzata sulla connessione condivisa (annidabile: commit solo all'esterno)."""
    ensure_connected()
    with _lock:
        depth = getattr(_depth, "n", 0)
        _depth.n = depth + 1
        try:
            yield _conn
            if depth == 0:
                _conn.commit()
        except Exception:
            if depth == 0:
                _conn.rollback()
            raise
        finally:
            _depth.n = depth


def get_lead(lead_id: int) -> dict | None:
    with tx() as c:
        row = c.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    return dict(row) if row else None


def update_lead(lead_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with tx() as c:
        c.execute(f"UPDATE leads SET {cols} WHERE id = ?", (*fields.values(), lead_id))
