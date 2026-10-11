"""Messaggi al cliente dopo la prenotazione: conferma subito e promemoria.

- `schedule_for_appointment`: pianifica conferma, promemoria del giorno prima e
  promemoria qualche ora prima, per ogni canale configurato (SMS, email).
- `cancel_for_appointment`: annulla i messaggi non ancora inviati (es. appuntamento spostato).
- `send_due`: invia i messaggi arrivati a scadenza (chiamato periodicamente dalla coda).

I testi sono in config/centri.yaml (sezione `messaggi`) e vengono composti al momento
dell'invio, così riflettono sempre i dati aggiornati dell'appuntamento.
"""

import logging
from datetime import datetime, time, timedelta, timezone

from .. import centers, db
from ..config import settings
from . import email as email_channel
from . import sms as sms_channel

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
CHANNELS = {"sms": sms_channel, "email": email_channel}


def _parse_hhmm(s: str) -> time:
    h, m = str(s).split(":")
    return time(int(h), int(m))


def _cfg() -> dict:
    return centers.load_config().messaggi or {}


def plan(start_local: datetime, booked_at: datetime) -> list[tuple[str, datetime]]:
    """Quali messaggi inviare e quando, per un appuntamento che inizia a `start_local`."""
    cfg = _cfg()
    tz = centers.load_config().tz
    booked_local = booked_at.astimezone(tz)
    not_before = _parse_hhmm(cfg.get("non_prima_delle", "08:00"))
    out = [("conferma", booked_at)]

    min_days = int(cfg.get("giorno_prima_se_almeno_giorni", 2))
    if (start_local.date() - booked_local.date()).days >= min_days:
        day_before = start_local.date() - timedelta(days=1)
        out.append(("giorno_prima", datetime.combine(day_before, _parse_hhmm(cfg.get("giorno_prima_ora", "10:00")), tzinfo=tz)))

    hours = cfg.get("ore_prima_quante", 2)
    if hours:
        at = start_local - timedelta(hours=float(hours))
        earliest = datetime.combine(at.date(), not_before, tzinfo=tz)
        if at < earliest:
            at = earliest
        # Se la conferma è appena partita a ridosso dell'appuntamento, il promemoria sarebbe un doppione
        if at > booked_at + timedelta(minutes=30) and at < start_local - timedelta(minutes=30):
            out.append(("ore_prima", at))
    return out


def schedule_for_appointment(appointment_id: int, now: datetime | None = None) -> int:
    """Crea i messaggi per un appuntamento appena prenotato. Restituisce quanti ne ha pianificati."""
    cfg = _cfg()
    channels = [c for c in cfg.get("canali", []) if c in CHANNELS]
    if not channels:
        return 0
    with db.tx() as c:
        appt = c.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
    if appt is None:
        return 0
    now = now or datetime.now(timezone.utc)
    start = centers.parse_local(appt["start_at"])
    rows = []
    for kind, when in plan(start, now):
        for channel in channels:
            rows.append((appointment_id, appt["lead_id"], kind, channel,
                         when.astimezone(timezone.utc).replace(microsecond=0).isoformat()))
    with db.tx() as c:
        c.executemany(
            "INSERT INTO messages (appointment_id, lead_id, kind, channel, send_at) VALUES (?, ?, ?, ?, ?)", rows
        )
    return len(rows)


def cancel_for_appointment(appointment_id: int) -> None:
    with db.tx() as c:
        c.execute(
            "UPDATE messages SET status = 'annullato' WHERE appointment_id = ? AND status = 'pending'",
            (appointment_id,),
        )


GIORNI_BREVI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]


def render(kind: str, appt: dict, lead: dict, channel: str = "email",
           now: datetime | None = None) -> tuple[str, str]:
    """Restituisce (oggetto, testo) del messaggio. Per gli SMS usa i testi brevi `sms_testo_*`, se presenti."""
    cfg = _cfg()
    conf = centers.load_config()
    centro = conf.centri.get(appt["centro_id"])
    start = centers.parse_local(appt["start_at"])
    istruzioni = istruzioni_sms = ""
    for p in conf.brand.get("valutazione_per_obiettivo") or []:
        if p.get("tipo") and p.get("tipo") == appt.get("tipo"):
            istruzioni = p.get("istruzioni_messaggio", "")
            istruzioni_sms = p.get("istruzioni_sms", "")
    nome = ((lead.get("nome") or "").split(" ")[0] or "cliente").capitalize()
    sent_local = (now or datetime.now(timezone.utc)).astimezone(conf.tz)
    values = {
        "nome": nome,
        "giorno_breve": f"{GIORNI_BREVI[start.weekday()]} {start.strftime('%d/%m')}",
        "indirizzo_breve": (centro.indirizzo_breve or centro.indirizzo) if centro else "",
        "saluto_finale": "Buona Giornata" if sent_local.hour < 17 else "Buona Serata",
        "data": centers.format_it(start).rsplit(" alle ", 1)[0],
        "ora": start.strftime("%H:%M"),
        "centro": centro.nome if centro else "",
        "indirizzo": centro.indirizzo if centro else "",
        "istruzioni": istruzioni,
        "istruzioni_sms": istruzioni_sms,
        "chiusura": cfg.get("chiusura", ""),
    }
    template = (channel == "sms" and cfg.get(f"sms_testo_{kind}")) or cfg.get(f"testo_{kind}", "")
    text = template.format(**values)
    # Righe vuote (es. nessuna istruzione specifica) non servono
    text = "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())
    subject = (cfg.get("oggetto_email") or {}).get(kind, "Il suo appuntamento")
    return subject, text


def _mark(message_id: int, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.tx() as c:
        c.execute(f"UPDATE messages SET {cols} WHERE id = ?", (*fields.values(), message_id))


def send_due(now: datetime | None = None, limit: int = 20) -> int:
    """Invia i messaggi scaduti. Restituisce quanti ne ha inviati (o simulati)."""
    now = now or datetime.now(timezone.utc)
    with db.tx() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM messages WHERE status = 'pending' AND send_at <= ? ORDER BY send_at LIMIT ?",
            (now.replace(microsecond=0).isoformat(), limit),
        )]
    sent = 0
    for m in rows:
        with db.tx() as c:
            appt = c.execute("SELECT * FROM appointments WHERE id = ?", (m["appointment_id"],)).fetchone()
        lead = db.get_lead(m["lead_id"]) or {}
        if appt is None or appt["status"] != "confermato":
            _mark(m["id"], status="annullato")
            continue
        appt = dict(appt)
        if m["kind"] != "conferma" and centers.parse_local(appt["start_at"]) <= now:
            _mark(m["id"], status="saltato", error="appuntamento già iniziato")
            continue
        channel = CHANNELS[m["channel"]]
        to = channel.recipient(lead)
        if not to:
            _mark(m["id"], status="saltato", error="recapito mancante")
            continue
        subject, text = render(m["kind"], appt, lead, channel=m["channel"], now=now)
        if not settings.messaggi_attivi:
            log.info("[messaggi simulati] %s a %s:\n%s", m["channel"], to, text)
            _mark(m["id"], status="simulato", sent_at=db.now_utc())
            sent += 1
            continue
        try:
            provider_id = channel.send(to, subject, text)
        except Exception as e:
            attempts = m["attempts"] + 1
            log.warning("Invio %s %s fallito (tentativo %s): %s", m["channel"], m["id"], attempts, e)
            if attempts >= MAX_ATTEMPTS:
                _mark(m["id"], status="errore", attempts=attempts, error=str(e)[:500])
            else:
                retry = (now + timedelta(minutes=5 * attempts)).isoformat()
                _mark(m["id"], attempts=attempts, error=str(e)[:500], send_at=retry)
            continue
        _mark(m["id"], status="inviato", sent_at=db.now_utc(), provider_id=provider_id, attempts=m["attempts"] + 1)
        sent += 1
    return sent
