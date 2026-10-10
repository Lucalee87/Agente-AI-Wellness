"""Strumenti che l'agente può usare durante la chiamata.

Le definizioni (JSON Schema) sono condivise tra Vapi e il simulatore testuale;
`execute_tool` contiene la logica vera e propria.
"""

import logging
from datetime import date

from .. import centers, db, leads

log = logging.getLogger(__name__)

ESITI = ["prenotato", "non_interessato", "richiamare", "numero_errato", "non_chiamare_piu", "altro"]

TOOL_DEFINITIONS = [
    {
        "name": "trova_disponibilita",
        "description": (
            "Restituisce gli orari liberi per una consulenza in un centro. "
            "Usalo prima di proporre un appuntamento; non proporre mai orari che non ti ha restituito."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "centro_id": {"type": "string", "description": "Id del centro (es. 'napoli-centro')."},
                "dal_giorno": {
                    "type": "string",
                    "description": "Data da cui cercare, formato YYYY-MM-DD. Ometti per partire da oggi.",
                },
                "fascia": {
                    "type": "string",
                    "enum": ["mattina", "pomeriggio", "sera", "qualsiasi"],
                    "description": "Preferenza oraria del cliente.",
                },
            },
            "required": ["centro_id"],
        },
    },
    {
        "name": "prenota_consulenza",
        "description": "Prenota la consulenza nello slot scelto dal cliente, dopo che ha confermato giorno, ora e centro.",
        "parameters": {
            "type": "object",
            "properties": {
                "centro_id": {"type": "string"},
                "data_ora": {"type": "string", "description": "Inizio dello slot, formato YYYY-MM-DDTHH:MM (ora italiana)."},
                "obiettivo": {"type": "string", "description": "Obiettivo del cliente, in poche parole."},
                "note": {"type": "string", "description": "Informazioni utili per il trainer (infortuni, esperienza...)."},
            },
            "required": ["centro_id", "data_ora"],
        },
    },
    {
        "name": "registra_esito",
        "description": "Registra l'esito della chiamata. Va chiamato sempre una volta prima di chiudere la telefonata.",
        "parameters": {
            "type": "object",
            "properties": {
                "esito": {"type": "string", "enum": ESITI},
                "richiamare_il": {
                    "type": "string",
                    "description": "Solo per esito 'richiamare': quando richiamare, formato YYYY-MM-DDTHH:MM (ora italiana).",
                },
                "note": {"type": "string", "description": "Breve riassunto della conversazione."},
            },
            "required": ["esito"],
        },
    },
]

FASCE = {"mattina": (0, 13), "pomeriggio": (13, 18), "sera": (18, 24), "qualsiasi": (0, 24)}


def _trova_disponibilita(lead: dict, args: dict) -> str:
    cfg = centers.load_config()
    centro_id = args.get("centro_id") or lead.get("centro_id")
    if centro_id not in cfg.centri:
        return f"Centro non valido. Centri disponibili: {', '.join(cfg.centri)}."
    from_day = None
    if args.get("dal_giorno"):
        try:
            from_day = date.fromisoformat(args["dal_giorno"])
        except ValueError:
            return "Data non valida: usa il formato YYYY-MM-DD."
    lo, hi = FASCE.get(args.get("fascia") or "qualsiasi", (0, 24))
    slots = [s for s in centers.free_slots(centro_id, from_day, days=10, limit=40) if lo <= s.hour < hi][:6]
    if not slots:
        return "Nessuno slot libero in quel periodo. Prova con un altro giorno, un'altra fascia o un altro centro."
    righe = [f"- {centers.format_it(s)} (data_ora: {s.strftime('%Y-%m-%dT%H:%M')})" for s in slots]
    return f"Slot liberi presso {cfg.centri[centro_id].nome}:\n" + "\n".join(righe)


def _prenota_consulenza(lead: dict, args: dict) -> str:
    cfg = centers.load_config()
    centro_id = args.get("centro_id")
    if centro_id not in cfg.centri:
        return f"Centro non valido. Centri disponibili: {', '.join(cfg.centri)}."
    try:
        start = centers.parse_local(args.get("data_ora", ""))
    except ValueError:
        return "Orario non valido: usa il formato YYYY-MM-DDTHH:MM."
    key = start.strftime("%Y-%m-%dT%H:%M")
    with db.tx() as c:
        if not centers.is_slot_free(centro_id, start):
            return "Questo slot non è più disponibile. Usa trova_disponibilita e proponi un altro orario."
        c.execute(
            "UPDATE appointments SET status = 'sostituito' WHERE lead_id = ? AND status = 'confermato'",
            (lead["id"],),
        )
        c.execute(
            "INSERT INTO appointments (lead_id, centro_id, start_at, created_at) VALUES (?, ?, ?, ?)",
            (lead["id"], centro_id, key, db.now_utc()),
        )
        fields = {"status": db.BOOKED, "centro_id": centro_id, "next_attempt_at": None}
        if args.get("obiettivo"):
            fields["obiettivo"] = args["obiettivo"]
        if args.get("note"):
            fields["note"] = args["note"]
        db.update_lead(lead["id"], **fields)
    centro = cfg.centri[centro_id]
    leads.notify("consulenza_prenotata", lead["id"], centro=centro.nome, data_ora=key)
    return f"Prenotazione confermata: {centers.format_it(start)} presso {centro.nome}, {centro.indirizzo}."


def _registra_esito(lead: dict, args: dict) -> str:
    esito = args.get("esito")
    note = args.get("note")
    if note:
        db.update_lead(lead["id"], summary=note)
    current = db.get_lead(lead["id"]) or lead
    if esito == "prenotato" or current["status"] == db.BOOKED:
        return "Esito registrato. Saluta cordialmente e chiudi la chiamata."
    if esito == "richiamare":
        when = None
        if args.get("richiamare_il"):
            try:
                when = centers.parse_local(args["richiamare_il"])
            except ValueError:
                pass
        if when is None or when < centers.local_now():
            leads.schedule_retry(lead["id"], current["attempts"])
        else:
            leads.schedule_callback(lead["id"], when)
        return "Richiamata pianificata. Saluta e chiudi la chiamata."
    status = {
        "non_interessato": db.NOT_INTERESTED,
        "numero_errato": db.INVALID,
        "non_chiamare_piu": db.DO_NOT_CALL,
    }.get(esito)
    if status:
        db.update_lead(lead["id"], status=status, next_attempt_at=None)
        leads.notify("esito_chiamata", lead["id"], esito=esito)
    return "Esito registrato. Saluta e chiudi la chiamata."


_HANDLERS = {
    "trova_disponibilita": _trova_disponibilita,
    "prenota_consulenza": _prenota_consulenza,
    "registra_esito": _registra_esito,
}


def execute_tool(lead_id: int, name: str, args: dict) -> str:
    lead = db.get_lead(lead_id)
    if lead is None:
        return "Errore: lead non trovato."
    handler = _HANDLERS.get(name)
    if handler is None:
        return f"Strumento sconosciuto: {name}"
    try:
        result = handler(lead, args or {})
    except Exception:
        log.exception("Errore nello strumento %s (lead %s)", name, lead_id)
        return "Si è verificato un errore tecnico. Scusati e proponi di far richiamare il cliente da un collega."
    log.info("Lead %s: %s(%s) -> %s", lead_id, name, args, result[:120])
    return result

