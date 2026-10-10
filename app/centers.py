"""Centri fitness, orari delle consulenze e disponibilità degli slot."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import yaml

from . import db
from .config import settings

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


@dataclass
class Config:
    brand: dict
    orari_chiamate: dict
    tentativi_minuti: list[int]
    centri: dict[str, Centro]

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
        )
    return Config(
        brand=raw.get("brand", {}),
        orari_chiamate=raw.get("orari_chiamate", {}),
        tentativi_minuti=[int(x) for x in raw.get("tentativi_minuti", [0, 120, 1440])],
        centri=centri,
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


def booked_count(centro_id: str, start_at: str) -> int:
    with db.tx() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM appointments WHERE centro_id = ? AND start_at = ? AND status = 'confermato'",
            (centro_id, start_at),
        ).fetchone()
    return row[0]


def free_slots(centro_id: str, from_day: date | None = None, days: int = 7,
               limit: int = 8, min_notice_hours: int = 2) -> list[datetime]:
    """Slot liberi a partire da `from_day`, con un preavviso minimo."""
    cfg = load_config()
    centro = cfg.centri[centro_id]
    now = local_now()
    earliest = now + timedelta(hours=min_notice_hours)
    start_day = max(from_day or now.date(), now.date())
    out = []
    for i in range(days):
        for slot in slots_for_day(centro, start_day + timedelta(days=i)):
            if slot < earliest:
                continue
            if booked_count(centro_id, _fmt_key(slot)) < centro.posti_per_slot:
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
    return booked_count(centro_id, _fmt_key(start)) < centro.posti_per_slot


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
