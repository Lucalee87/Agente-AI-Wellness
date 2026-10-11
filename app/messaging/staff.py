"""Avvisi al responsabile del centro (email) per gli eventi importanti.

Esempi: un cliente chiede di essere ricontattato dal responsabile, una nuova
valutazione prenotata, una prenotazione annullata, un cliente non interessato,
un lead irraggiungibile.

Gli eventi che nascono durante una telefonata vengono messi in coda e inviati in
un'unica email a fine chiamata, insieme alla trascrizione della conversazione.
Se il resoconto della chiamata non arriva, la coda li invia comunque dopo un po'.
Destinatari ed eventi si configurano in config/centri.yaml (`responsabile` per
centro e sezione `notifiche_staff`).
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from .. import centers, db
from ..config import settings
from . import email as email_channel
from . import sms as sms_channel

log = logging.getLogger(__name__)

# Eventi che non nascono in una telefonata in corso: si inviano subito
IMMEDIATE_EVENTS = {"lead_irraggiungibile"}
# Dopo quanto inviare comunque gli avvisi se il resoconto della chiamata non arriva
STALE_MINUTES = 30

TITOLI = {
    "ricontatto_responsabile": "DA RICHIAMARE",
    "consulenza_prenotata": "NUOVA VALUTAZIONE PRENOTATA",
    "prenotazione_annullata": "PRENOTAZIONE ANNULLATA",
    "lead_irraggiungibile": "LEAD NON RAGGIUNGIBILE",
    "esito_chiamata": "NON INTERESSATO",
}

ESITI = {
    "non_interessato": "non interessato",
    "numero_errato": "numero errato",
    "non_chiamare_piu": "ha chiesto di non essere più contattato",
}

# L'evento più importante dà il titolo all'email quando ce ne sono più di uno
PRIORITA = ["ricontatto_responsabile", "prenotazione_annullata", "consulenza_prenotata", "esito_chiamata",
            "lead_irraggiungibile"]


def _tipo_label(tipo: str | None) -> str:
    for p in centers.load_config().brand.get("valutazione_per_obiettivo") or []:
        if p.get("tipo") == tipo:
            return p.get("obiettivo", tipo)
    return ""


def _event_lines(event: str, lead: dict, payload: dict) -> list[str]:
    righe = []
    if event == "ricontatto_responsabile":
        righe.append(f"Da richiamare: {payload.get('fascia_oraria') or 'fascia non indicata'}")
    if event == "consulenza_prenotata" and payload.get("data_ora"):
        start = centers.parse_local(payload["data_ora"])
        righe.append(f"Valutazione prenotata: {centers.format_it(start)}")
        if payload.get("tipo"):
            righe.append(f"Tipo di valutazione: {_tipo_label(payload['tipo'])}")
    if event == "prenotazione_annullata":
        righe.append(f"Prenotazione annullata. Motivo: {payload.get('motivo') or 'non indicato'}")
    if event == "esito_chiamata":
        righe.append(f"Esito: {ESITI.get(payload.get('esito'), payload.get('esito'))}")
    if event == "lead_irraggiungibile":
        righe.append(f"Nessuna risposta dopo {lead.get('attempts', 0)} tentativi di chiamata")
    if payload.get("note"):
        righe.append(f"Note: {payload['note']}")
    return righe


def _format_transcript(transcript: str) -> str:
    lines = []
    for line in (transcript or "").splitlines():
        line = line.strip()
        if line.lower().startswith("ai:") or line.lower().startswith("bot:") or line.lower().startswith("assistant:"):
            line = "Vittoria:" + line.split(":", 1)[1]
        elif line.lower().startswith("user:") or line.lower().startswith("customer:"):
            line = "Cliente:" + line.split(":", 1)[1]
        if line:
            lines.append(line)
    return "\n".join(lines)


def compose(events: list[tuple[str, dict]], lead: dict, transcript: str | None = None,
            recording_url: str | None = None, summary: str | None = None) -> tuple[str, str]:
    """Restituisce (oggetto, testo) dell'email per uno o più eventi dello stesso cliente."""
    cfg = centers.load_config()
    centro = cfg.centri.get(lead.get("centro_id") or "")
    sede = centro.nome if centro else cfg.brand.get("nome", "")
    principale = min((e for e, _ in events), key=lambda e: PRIORITA.index(e) if e in PRIORITA else 99)
    titolo = TITOLI.get(principale, principale.upper())
    if principale == "esito_chiamata":
        esito = next((p.get("esito") for e, p in events if e == "esito_chiamata"), None)
        titolo = ESITI.get(esito, titolo).upper()

    righe = [f"{sede} - {titolo}", "",
             f"Cliente: {lead.get('nome') or 'senza nome'}",
             f"Telefono: {lead.get('telefono', '')}"]
    if lead.get("email"):
        righe.append(f"Email: {lead['email']}")
    righe.append(f"Provenienza: {lead.get('source', '')}")
    if lead.get("obiettivo"):
        righe.append(f"Obiettivo: {lead['obiettivo']}")
    righe.append("")
    for event, payload in events:
        righe.extend(_event_lines(event, lead, payload))
    if summary:
        righe += ["", f"Riepilogo della chiamata: {summary}"]
    if recording_url:
        righe += ["", f"Registrazione: {recording_url}"]
    if transcript:
        righe += ["", "Trascrizione della chiamata:", _format_transcript(transcript)]
    subject = f"{titolo} - {lead.get('nome') or lead.get('telefono', '')} - {sede}"
    return subject, "\n".join(righe)


def _enabled(event: str, payload: dict) -> bool:
    conf = centers.load_config().notifiche_staff or {}
    if event not in (conf.get("eventi") or []):
        return False
    if event == "esito_chiamata" and payload.get("esito") not in ESITI:
        return False
    return True


def notify_staff(event: str, lead_id: int, **payload) -> None:
    """Registra un evento da comunicare al responsabile (inviato a fine chiamata, o subito se non è in una chiamata)."""
    if not _enabled(event, payload):
        return
    with db.tx() as c:
        c.execute(
            "INSERT INTO staff_notifications (lead_id, event, payload, created_at) VALUES (?, ?, ?, ?)",
            (lead_id, event, json.dumps(payload, ensure_ascii=False, default=str), db.now_utc()),
        )
    if event in IMMEDIATE_EVENTS:
        flush_for_lead(lead_id)


def _recipients(lead: dict) -> dict:
    cfg = centers.load_config()
    centro = cfg.centri.get(lead.get("centro_id") or "")
    return (centro.responsabile if centro else None) or (cfg.notifiche_staff or {}).get("responsabile") or {}


def flush_for_lead(lead_id: int, transcript: str | None = None, recording_url: str | None = None,
                   summary: str | None = None) -> int:
    """Invia in un'unica email gli eventi in attesa per questo cliente. Restituisce 1 se ha inviato."""
    with db.tx() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM staff_notifications WHERE lead_id = ? AND status = 'pending' ORDER BY id", (lead_id,)
        )]
    if not rows:
        return 0
    lead = db.get_lead(lead_id) or {}
    events = [(r["event"], json.loads(r["payload"] or "{}")) for r in rows]
    subject, text = compose(events, lead, transcript, recording_url, summary)
    conf = centers.load_config().notifiche_staff or {}
    to = _recipients(lead)
    status, error, sent = "inviato", None, 0
    for channel_name, module, address in (("email", email_channel, to.get("email")),
                                          ("sms", sms_channel, to.get("telefono"))):
        if channel_name not in (conf.get("canali") or []) or not address:
            continue
        if not settings.messaggi_attivi:
            log.info("[avviso staff simulato] %s a %s:\n%s", channel_name, address, text)
            status, sent = "simulato", sent + 1
            continue
        try:
            module.send(address, subject, text)
            sent += 1
        except Exception as e:
            log.warning("Avviso al responsabile via %s non riuscito: %s", channel_name, e)
            status, error = "errore", str(e)[:500]
    if sent == 0 and error is None:
        status, error = "saltato", "nessun destinatario configurato"
    ids = [r["id"] for r in rows]
    with db.tx() as c:
        c.execute(
            f"UPDATE staff_notifications SET status = ?, sent_at = ?, error = ? WHERE id IN ({','.join('?' * len(ids))})",
            (status, db.now_utc(), error, *ids),
        )
    return 1 if sent else 0


def flush_stale(now: datetime | None = None) -> int:
    """Invia gli avvisi rimasti in attesa troppo a lungo (resoconto della chiamata mai arrivato)."""
    now = now or datetime.now(timezone.utc)
    limit = (now - timedelta(minutes=STALE_MINUTES)).replace(microsecond=0).isoformat()
    with db.tx() as c:
        lead_ids = [r[0] for r in c.execute(
            "SELECT DISTINCT lead_id FROM staff_notifications WHERE status = 'pending' AND created_at <= ?", (limit,)
        )]
    return sum(flush_for_lead(lead_id) for lead_id in lead_ids)
