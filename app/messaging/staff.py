"""Avvisi al responsabile del centro (SMS ed email) per gli eventi importanti.

Esempi: un cliente chiede di essere ricontattato dal responsabile, una nuova
valutazione prenotata, una prenotazione annullata, un lead irraggiungibile.
Destinatari e eventi si configurano in config/centri.yaml (`responsabile` per
centro e sezione `notifiche_staff`).
"""

import logging

from .. import centers, db
from ..config import settings
from . import email as email_channel
from . import sms as sms_channel

log = logging.getLogger(__name__)

TITOLI = {
    "ricontatto_responsabile": "DA RICHIAMARE",
    "consulenza_prenotata": "NUOVA VALUTAZIONE PRENOTATA",
    "prenotazione_annullata": "PRENOTAZIONE ANNULLATA",
    "lead_irraggiungibile": "LEAD NON RAGGIUNGIBILE",
    "esito_chiamata": "ESITO CHIAMATA",
}

ESITI = {
    "non_interessato": "non interessato",
    "numero_errato": "numero errato",
    "non_chiamare_piu": "ha chiesto di non essere più contattato",
}


def _tipo_label(tipo: str | None) -> str:
    for p in centers.load_config().brand.get("valutazione_per_obiettivo") or []:
        if p.get("tipo") == tipo:
            return p.get("obiettivo", tipo)
    return ""


def compose(event: str, lead: dict, payload: dict) -> tuple[str, str]:
    """Restituisce (oggetto, testo) dell'avviso."""
    cfg = centers.load_config()
    centro = cfg.centri.get(lead.get("centro_id") or "")
    sede = centro.nome if centro else cfg.brand.get("nome", "")
    righe = [f"{sede} - {TITOLI.get(event, event.upper())}",
             f"{lead.get('nome') or 'Senza nome'} {lead.get('telefono', '')}"]
    if event == "ricontatto_responsabile":
        righe.append(f"Quando chiamare: {payload.get('fascia_oraria') or 'non indicato'}")
    if event == "consulenza_prenotata" and payload.get("data_ora"):
        start = centers.parse_local(payload["data_ora"])
        righe.append(f"Quando: {centers.format_it(start)}")
        if payload.get("tipo"):
            righe.append(f"Valutazione: {_tipo_label(payload['tipo'])}")
    if event == "prenotazione_annullata" and payload.get("motivo"):
        righe.append(f"Motivo: {payload['motivo']}")
    if event == "esito_chiamata":
        righe.append(f"Esito: {ESITI.get(payload.get('esito'), payload.get('esito'))}")
    if event == "lead_irraggiungibile":
        righe.append(f"Nessuna risposta dopo {lead.get('attempts', 0)} tentativi")
    if lead.get("obiettivo"):
        righe.append(f"Obiettivo: {lead['obiettivo']}")
    note = payload.get("note") or lead.get("note")
    if note:
        righe.append(f"Note: {note}")
    if lead.get("email"):
        righe.append(f"Email: {lead['email']}")
    righe.append(f"Provenienza: {lead.get('source', '')}")
    return f"{TITOLI.get(event, event)} - {lead.get('nome') or ''} - {sede}", "\n".join(righe)


def notify_staff(event: str, lead_id: int, **payload) -> int:
    """Invia l'avviso al responsabile del centro del lead. Restituisce quanti invii sono riusciti."""
    cfg = centers.load_config()
    conf = cfg.notifiche_staff or {}
    if event not in (conf.get("eventi") or []):
        return 0
    if event == "esito_chiamata" and payload.get("esito") not in ESITI:
        return 0
    lead = db.get_lead(lead_id) or {}
    centro = cfg.centri.get(lead.get("centro_id") or "")
    if centro is None and len(cfg.centri) == 1:
        centro = next(iter(cfg.centri.values()))
    responsabile = (centro.responsabile if centro else None) or conf.get("responsabile") or {}
    subject, text = compose(event, lead, payload)
    sent = 0
    for channel_name, module, to in (
        ("sms", sms_channel, responsabile.get("telefono")),
        ("email", email_channel, responsabile.get("email")),
    ):
        if channel_name not in (conf.get("canali") or []) or not to:
            continue
        if not settings.messaggi_attivi:
            log.info("[avviso staff simulato] %s a %s:\n%s", channel_name, to, text)
            sent += 1
            continue
        try:
            module.send(to, subject, text)
            sent += 1
        except Exception as e:
            log.warning("Avviso staff %s via %s fallito: %s", event, channel_name, e)
    return sent
