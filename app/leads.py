"""Creazione dei lead, deduplica e pianificazione dei tentativi di chiamata."""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from . import centers, db
from .config import settings
from .phone import normalize_phone

log = logging.getLogger(__name__)

# Stati in cui un lead è ancora "aperto": un nuovo modulo dallo stesso numero non crea un duplicato
ACTIVE_STATUSES = (db.NEW, db.RETRY, db.CALLING, db.CHAT)


@dataclass
class LeadIn:
    source: str
    telefono: str | None
    nome: str | None = None
    email: str | None = None
    centro_id: str | None = None
    obiettivo: str | None = None
    note: str | None = None
    source_ref: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class LeadResult:
    lead_id: int | None
    created: bool
    reason: str = ""


def first_attempt_at(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return centers.next_calling_time(now)


def create_lead(data: LeadIn) -> LeadResult:
    telefono = normalize_phone(data.telefono)
    if not telefono:
        return LeadResult(None, False, "numero di telefono mancante o non valido")

    cfg = centers.load_config()
    centro_id = data.centro_id if data.centro_id in cfg.centri else None
    if centro_id is None and len(cfg.centri) == 1:
        centro_id = next(iter(cfg.centri))

    with db.tx() as c:
        if data.source_ref:
            row = c.execute(
                "SELECT id FROM leads WHERE source = ? AND source_ref = ?", (data.source, data.source_ref)
            ).fetchone()
            if row:
                return LeadResult(row["id"], False, "lead già ricevuto")
        row = c.execute(
            f"SELECT * FROM leads WHERE telefono = ? AND status IN ({','.join('?' * len(ACTIVE_STATUSES))})",
            (telefono, *ACTIVE_STATUSES),
        ).fetchone()
        if row:
            # Completa i dati mancanti: ad esempio la chat è partita prima che arrivasse il lead da Meta
            missing = {k: v for k, v in (("nome", (data.nome or "").strip() or None), ("email", data.email),
                                         ("centro_id", centro_id), ("obiettivo", data.obiettivo))
                       if v and not row[k]}
            if missing:
                db.update_lead(row["id"], **missing)
            return LeadResult(row["id"], False, "lead già in lavorazione con lo stesso numero")
        row = c.execute(
            "SELECT id FROM leads WHERE telefono = ? AND status = ?", (telefono, db.DO_NOT_CALL)
        ).fetchone()
        if row:
            return LeadResult(row["id"], False, "il numero ha chiesto di non essere ricontattato")

        cur = c.execute(
            """INSERT INTO leads (created_at, source, source_ref, nome, telefono, email, centro_id,
                                  obiettivo, note, extra, status, next_attempt_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                db.now_utc(), data.source, data.source_ref, (data.nome or "").strip() or None, telefono,
                data.email, centro_id, data.obiettivo, data.note,
                json.dumps(data.extra, ensure_ascii=False) if data.extra else None,
                db.NEW, first_attempt_at().astimezone(timezone.utc).isoformat(),
            ),
        )
        lead_id = cur.lastrowid
    log.info("Nuovo lead %s da %s (centro=%s)", lead_id, data.source, centro_id)
    return LeadResult(lead_id, True)


def schedule_retry(lead_id: int, attempts_done: int) -> str:
    """Pianifica il prossimo tentativo, oppure chiude il lead come irraggiungibile."""
    delays = centers.load_config().tentativi_minuti
    if attempts_done >= len(delays):
        db.update_lead(lead_id, status=db.UNREACHABLE, next_attempt_at=None)
        notify("lead_irraggiungibile", lead_id)
        return db.UNREACHABLE
    when = datetime.now(timezone.utc) + timedelta(minutes=delays[attempts_done])
    when = centers.next_calling_time(when).astimezone(timezone.utc)
    db.update_lead(lead_id, status=db.RETRY, next_attempt_at=when.isoformat())
    return db.RETRY


def schedule_callback(lead_id: int, when_local: datetime) -> None:
    """Richiamata su richiesta del lead (non conta come tentativo fallito)."""
    when = centers.next_calling_time(when_local).astimezone(timezone.utc)
    db.update_lead(lead_id, status=db.RETRY, next_attempt_at=when.isoformat())


def notify(event: str, lead_id: int, **payload) -> None:
    """Avvisa il responsabile del centro (SMS/email) e invia l'evento a NOTIFY_WEBHOOK_URL, se configurato."""
    from .messaging.staff import notify_staff

    try:
        notify_staff(event, lead_id, **payload)
    except Exception:
        log.exception("Avviso al responsabile per %s non riuscito", event)
    if not settings.notify_webhook_url:
        return
    lead = db.get_lead(lead_id) or {}
    body = {"evento": event, "lead": lead, **payload}
    try:
        httpx.post(settings.notify_webhook_url, json=body, timeout=10)
    except httpx.HTTPError as e:
        log.warning("Notifica %s fallita: %s", event, e)
