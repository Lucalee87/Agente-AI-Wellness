"""Centri fitness, orari delle consulenze e disponibilità degli slot."""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import logging

import yaml

from . import db
from .config import settings

log = logging.getLogger(__name__)

GIORNI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
GIORNI_ESTESI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
        "agosto", "settembre", "ottobre", "novembre", "dicembre"]


@dataclass
class Centro:
    id: str
    nome: str
    indirizzo: str
    durata_consulenza_min: int
    posti_per_slot: int
    orari_consulenze: dict[str, list[str]]
    meta_form_ids: list[str]
    google_form_ids: list[str]
    google_calendar_id: str = ""


@dataclass
class Config:
    brand: dict
    orari_chiamate: dict
    tentativi_minuti: list[int]
    centri: dict[str, Centro]
    messaggi: dict = field(default_factory=dict)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.brand.get("timezone", "Europe/Rome"))


@lru_cache
def load_config(path: str | None = None) -> Config:
    with open(path or settings.centri_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    centri = {}
    for c in raw.get("centri", []):
        centri[c["id"]] = Centro(
            id=c["id"],
            nome=c["nome"],
            indirizzo=c.get("indirizzo", ""),
            durata_consulenza_min=int(c.get("durata_consulenza_min", 45)),
            posti_per_slot=int(c.get("posti_per_slot", 1)),
            orari_consulenze=c.get("orari_consulenze", {}),
            meta_form_ids=[str(x) for x in c.get("meta_form_ids", [])],
            google_form_ids=[str(x) for x in c.get("google_form_ids", [])],
            google_calendar_id=str(c.get("google_calendar_id") or ""),
        )
    return Config(
        brand=raw.get("brand", {}),
        orari_chiamate=raw.get("orari_chiamate", {}),
        tentativi_minuti=[int(x) for x in raw.get("tentativi_minuti", [0, 120, 1440])],
        centri=centri,
        messaggi=raw.get("messaggi") or {},
    )


def centro_for_form(source: str, form_id: str | None) -> str | None:
    """Associa un modulo Meta/Google al centro configurato."""
    if not form_id:
        return None
    cfg = load_config()
    for c in cfg.centri.values():
        ids = c.meta_form_ids if source == "meta" else c.google_form_ids
        if str(form_id) in ids:
            return c.id
    return None


def _parse_hhmm(s: str) -> time:
    h, m = s.strip().split(":")
    return time(int(h), int(m))


def local_now() -> datetime:
    return datetime.now(load_config().tz)


def is_calling_time(at: datetime | None = None) -> bool:
    """True se `at` (default: adesso) è dentro la fascia in cui si può chiamare."""
    cfg = load_config()
    at = (at or local_now()).astimezone(cfg.tz)
    oc = cfg.orari_chiamate
    if GIORNI[at.weekday()] not in oc.get("giorni", GIORNI[:6]):
        return False
    return _parse_hhmm(oc.get("dalle", "09:00")) <= at.time() < _parse_hhmm(oc.get("alle", "20:00"))


def next_calling_time(after: datetime) -> datetime:
    """Il primo istante >= `after` in cui è consentito chiamare."""
    cfg = load_config()
    at = after.astimezone(cfg.tz)
    if is_calling_time(at):
        return at
    start = _parse_hhmm(cfg.orari_chiamate.get("dalle", "09:00"))
    for days in range(0, 8):
        d = at.date() + timedelta(days=days)
        candidate = datetime.combine(d, start, tzinfo=cfg.tz)
        if candidate >= at and is_calling_time(candidate):
            return candidate
    return at  # configurazione senza giorni validi: non blocchiamo


def slots_for_day(centro: Centro, day: date) -> list[datetime]:
    """Tutti gli slot teorici di consulenza di un giorno (senza considerare le prenotazioni)."""
    tz = load_config().tz
    ranges = centro.orari_consulenze.get(GIORNI[day.weekday()], [])
    step = timedelta(minutes=centro.durata_consulenza_min)
    out = []
    for r in ranges:
        a, b = r.split("-")
        cur = datetime.combine(day, _parse_hhmm(a), tzinfo=tz)
        end = datetime.combine(day, _parse_hhmm(b), tzinfo=tz)
        while cur + step <= end:
            out.append(cur)
            cur += step
    return out


def _fmt_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def _occupancy(centro: Centro, start: datetime, end: datetime):
    """Restituisce una funzione slot -> numero di posti occupati nell'intervallo [start, end).

    Somma gli impegni del calendario esterno (Google Calendar, gestionale...) e le
    prenotazioni locali non ancora sincronizzate, così non contiamo due volte
    gli appuntamenti che abbiamo già scritto nel calendario.
    """
    from . import calendars

    tz = load_config().tz
    backend = calendars.backend_for(centro)
    busy: list[tuple[datetime, datetime]] = []
    if backend is not None:
        try:
            busy = backend.busy(centro, start, end)
        except Exception:
            log.exception("Calendario del centro %s non raggiungibile: uso solo le prenotazioni locali", centro.id)
    query = "SELECT start_at FROM appointments WHERE centro_id = ? AND status = 'confermato' AND start_at >= ? AND start_at < ?"
    if backend is not None:
        query += " AND calendar_event_id IS NULL"
    step = timedelta(minutes=centro.durata_consulenza_min)
    with db.tx() as c:
        for (s,) in c.execute(query, (centro.id, _fmt_key(start), _fmt_key(end))):
            s_dt = datetime.fromisoformat(s).replace(tzinfo=tz)
            busy.append((s_dt, s_dt + step))

    def count(slot: datetime) -> int:
        slot_end = slot + step
        return sum(1 for b_start, b_end in busy if b_start < slot_end and b_end > slot)

    return count


def free_slots(centro_id: str, from_day: date | None = None, days: int = 7,
               limit: int = 8, min_notice_hours: int = 2) -> list[datetime]:
    """Slot liberi a partire da `from_day`, con un preavviso minimo."""
    cfg = load_config()
    centro = cfg.centri[centro_id]
    now = local_now()
    earliest = now + timedelta(hours=min_notice_hours)
    start_day = max(from_day or now.date(), now.date())
    window_start = datetime.combine(start_day, time(0, 0), tzinfo=cfg.tz)
    occupied = _occupancy(centro, window_start, window_start + timedelta(days=days))
    out = []
    for i in range(days):
        for slot in slots_for_day(centro, start_day + timedelta(days=i)):
            if slot < earliest:
                continue
            if occupied(slot) < centro.posti_per_slot:
                out.append(slot)
                if len(out) >= limit:
                    return out
    return out


def is_slot_free(centro_id: str, start: datetime) -> bool:
    centro = load_config().centri[centro_id]
    if start < local_now():
        return False
    if start not in slots_for_day(centro, start.date()):
        return False
    step = timedelta(minutes=centro.durata_consulenza_min)
    occupied = _occupancy(centro, start - step, start + step)
    return occupied(start) < centro.posti_per_slot


def parse_local(s: str) -> datetime:
    """Interpreta 'YYYY-MM-DDTHH:MM' (o con spazio) come ora locale del brand."""
    s = s.strip().replace(" ", "T")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=load_config().tz)
    return dt.astimezone(load_config().tz)


def format_it(dt: datetime) -> str:
    """Es. 'martedì 14 ottobre alle 18:30' — leggibile a voce."""
    return f"{GIORNI_ESTESI[dt.weekday()]} {dt.day} {MESI[dt.month - 1]} alle {dt.strftime('%H:%M')}"
