"""Coda di chiamata: decide chi chiamare, quando, e gestisce l'esito a fine chiamata."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from . import centers, db, leads
from .config import settings
from .voice import vapi

log = logging.getLogger(__name__)

POLL_SECONDS = 20
# Se non riceviamo il report di fine chiamata entro questo tempo, consideriamo la chiamata persa
STUCK_CALL_MINUTES = 20


def due_leads(limit: int) -> list[dict]:
    now = db.now_utc()
    with db.tx() as c:
        rows = c.execute(
            """SELECT * FROM leads
               WHERE status IN (?, ?) AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?
               ORDER BY status = 'new' DESC, next_attempt_at
               LIMIT ?""",
            (db.NEW, db.RETRY, now, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def active_calls() -> int:
    with db.tx() as c:
        return c.execute("SELECT COUNT(*) FROM leads WHERE status = ?", (db.CALLING,)).fetchone()[0]


def dial(lead: dict) -> str | None:
    """Avvia una chiamata verso il lead e registra il tentativo."""
    attempts = lead["attempts"] + 1
    db.update_lead(lead["id"], status=db.CALLING, attempts=attempts, last_call_at=db.now_utc())
    try:
        call_id = vapi.start_call(lead)
    except vapi.CallStartError as e:
        log.error("Impossibile chiamare il lead %s: %s", lead["id"], e)
        leads.schedule_retry(lead["id"], attempts)
        return None
    with db.tx() as c:
        c.execute(
            "INSERT INTO calls (lead_id, provider_call_id, started_at) VALUES (?, ?, ?)",
            (lead["id"], call_id, db.now_utc()),
        )
    log.info("Chiamata %s avviata verso lead %s (tentativo %s)", call_id, lead["id"], attempts)
    return call_id


def release_stuck_calls() -> None:
    limit = (datetime.now(timezone.utc) - timedelta(minutes=STUCK_CALL_MINUTES)).isoformat()
    with db.tx() as c:
        rows = c.execute(
            "SELECT id, attempts FROM leads WHERE status = ? AND last_call_at < ?", (db.CALLING, limit)
        ).fetchall()
    for r in rows:
        log.warning("Chiamata del lead %s senza esito: la ripianifico", r["id"])
        leads.schedule_retry(r["id"], r["attempts"])


def tick() -> int:
    """Un giro della coda. Restituisce il numero di chiamate avviate."""
    release_stuck_calls()
    if not centers.is_calling_time():
        return 0
    free = settings.max_concurrent_calls - active_calls()
    if free <= 0:
        return 0
    started = 0
    for lead in due_leads(free):
        if dial(lead):
            started += 1
    return started


def handle_end_of_call(lead_id: int, message: dict) -> None:
    """Aggiorna lead e chiamata con il report finale inviato da Vapi."""
    call = message.get("call") or {}
    artifact = message.get("artifact") or {}
    analysis = message.get("analysis") or {}
    ended_reason = message.get("endedReason") or call.get("endedReason")
    transcript = artifact.get("transcript") or message.get("transcript")
    recording = artifact.get("recordingUrl") or message.get("recordingUrl")
    summary = analysis.get("summary") or message.get("summary")

    lead = db.get_lead(lead_id)
    if lead is None:
        return

    if lead["status"] == db.CALLING:
        # L'agente non ha registrato un esito (nessuna risposta, segreteria o chiamata
        # interrotta): pianifichiamo un nuovo tentativo
        leads.schedule_retry(lead_id, lead["attempts"])
        outcome = "nessuna_risposta" if vapi.is_no_answer(ended_reason) else "senza_esito"
    else:
        outcome = lead["status"]

    with db.tx() as c:
        call_id = call.get("id")
        params = (db.now_utc(), ended_reason, outcome, transcript, recording, summary)
        updated = 0
        if call_id:
            updated = c.execute(
                """UPDATE calls SET ended_at = ?, ended_reason = ?, outcome = ?, transcript = ?,
                                    recording_url = ?, summary = ?
                   WHERE provider_call_id = ?""",
                (*params, call_id),
            ).rowcount
        if not updated:
            c.execute(
                """INSERT INTO calls (lead_id, provider_call_id, started_at, ended_at, ended_reason,
                                      outcome, transcript, recording_url, summary)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (lead_id, call_id, db.now_utc(), *params),
            )
    if summary and not lead.get("summary"):
        db.update_lead(lead_id, summary=summary)
    log.info("Fine chiamata lead %s: %s (%s)", lead_id, outcome, ended_reason)


async def run_forever() -> None:
    log.info("Coda chiamate avviata (provider: %s)", settings.voice_provider)
    while True:
        try:
            await asyncio.to_thread(tick)
        except Exception:
            log.exception("Errore nella coda chiamate")
        await asyncio.sleep(POLL_SECONDS)
