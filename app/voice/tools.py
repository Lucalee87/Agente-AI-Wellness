"""Strumenti che l'agente può usare durante la chiamata.

Le definizioni (JSON Schema) sono condivise tra Vapi e il simulatore testuale;
`execute_tool` contiene la logica vera e propria.
"""

import logging
from datetime import date

from .. import calendars, centers, db, leads, messaging

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
                    "description": "Fascia oraria scelta dal cliente: chiedila sempre prima di cercare gli orari.",
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
                "note": {"type": "string", "description": "Tutto ciò che il coach deve sapere: problemi fisici, dolori, infortuni, esigenze, esperienza, preferenze."},
                "tipo_valutazione": {
                    "type": "string",
                    "description": "Tipo di valutazione in base all'obiettivo del cliente: uno dei codici `tipo` indicati nelle istruzioni (es. dimagrimento_tono, posturale, combinata). Serve per i messaggi di conferma.",
                },
            },
            "required": ["centro_id", "data_ora"],
        },
    },
    {
        "name": "richiedi_ricontatto_responsabile",
        "description": (
            "Da usare quando nessuno degli orari disponibili va bene alla persona: il responsabile del centro "
            "la richiamerà per trovare insieme un giorno e un orario. Prima chiedi in quale fascia oraria preferisce essere chiamata."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "centro_id": {"type": "string", "description": "Id della sede scelta dalla persona, se nota."},
                "fascia_oraria": {"type": "string", "description": "Quando la persona preferisce essere richiamata (es. 'mattina tra le 10 e le 12', 'dopo le 18')."},
                "obiettivo": {"type": "string", "description": "Obiettivo della persona, in poche parole."},
                "tipo_valutazione": {"type": "string", "description": "Codice tipo di valutazione, se già chiaro (es. dimagrimento_tono, posturale, combinata)."},
                "note": {"type": "string", "description": "Disponibilità, esigenze, problemi fisici e tutto ciò che serve al responsabile."},
            },
            "required": ["fascia_oraria"],
        },
    },
    {
        "name": "annulla_prenotazione",
        "description": "Annulla la prenotazione fatta in questa chiamata, se la persona ci ripensa e chiede di annullarla.",
        "parameters": {
            "type": "object",
            "properties": {
                "motivo": {"type": "string", "description": "Perché la persona annulla, in poche parole."},
            },
            "required": [],
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
    if not cfg.centri[centro_id].orari_consulenze:
        return ("Per questa sede gli appuntamenti vengono fissati direttamente dal responsabile: chiedi alla persona "
                "in quale fascia oraria preferisce essere chiamata e usa richiedi_ricontatto_responsabile.")
    lo, hi = FASCE.get(args.get("fascia") or "qualsiasi", (0, 24))
    slots = [s for s in centers.free_slots(centro_id, from_day, days=14, limit=60) if lo <= s.hour < hi]
    if not slots:
        return "Nessuno slot libero in quel periodo. Prova con un altro giorno, un'altra fascia o un altro centro."
    # Due proposte, possibilmente in giorni diversi; le altre restano di riserva
    first = slots[0]
    second = next((x for x in slots[1:] if x.date() != first.date()), slots[1] if len(slots) > 1 else None)
    proposte = [x for x in (first, second) if x]
    riserva = [x for x in slots if x not in proposte][:6]

    def riga(x):
        return f"- {centers.format_it(x)} (data_ora: {x.strftime('%Y-%m-%dT%H:%M')})"

    out = [f"Orari liberi presso {cfg.centri[centro_id].nome}.",
           "PROPONI ORA SOLO QUESTE DUE OPZIONI:", *map(riga, proposte)]
    if riserva:
        out += ["ALTRE OPZIONI DI RISERVA (non elencarle: proponine due alla volta solo se le prime non vanno bene):",
                *map(riga, riserva)]
    return "\n".join(out)


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
    centro = cfg.centri[centro_id]
    sync = "pending" if calendars.backend_for(centro) else None
    with db.tx() as c:
        if not centers.is_slot_free(centro_id, start):
            return "Questo slot non è più disponibile. Usa trova_disponibilita e proponi un altro orario."
        # Se il cliente aveva già una consulenza, questa la sostituisce
        replaced = [r[0] for r in c.execute(
            "SELECT id FROM appointments WHERE lead_id = ? AND status = 'confermato'", (lead["id"],)
        )]
        c.execute(
            "UPDATE appointments SET status = 'sostituito' WHERE lead_id = ? AND status = 'confermato'",
            (lead["id"],),
        )
        tipi = {p.get("tipo") for p in cfg.brand.get("valutazione_per_obiettivo") or []}
        tipo = args.get("tipo_valutazione") if args.get("tipo_valutazione") in tipi else None
        appointment_id = c.execute(
            "INSERT INTO appointments (lead_id, centro_id, start_at, created_at, calendar_sync, tipo) VALUES (?, ?, ?, ?, ?, ?)",
            (lead["id"], centro_id, key, db.now_utc(), sync, tipo),
        ).lastrowid
        fields = {"status": db.BOOKED, "centro_id": centro_id, "next_attempt_at": None}
        if args.get("obiettivo"):
            fields["obiettivo"] = args["obiettivo"]
        if args.get("note"):
            fields["note"] = args["note"]
        db.update_lead(lead["id"], **fields)
    # Scrittura nel calendario: se fallisce, la coda riprova in automatico (lo slot resta comunque bloccato)
    for old_id in replaced:
        calendars.sync_appointment(old_id)
        messaging.cancel_for_appointment(old_id)
    calendars.sync_appointment(appointment_id)
    # Conferma subito + promemoria (inviati dalla coda)
    messaging.schedule_for_appointment(appointment_id)
    leads.notify("consulenza_prenotata", lead["id"], centro=centro.nome, data_ora=key, tipo=tipo,
                 note=args.get("note"))
    return f"Prenotazione confermata: {centers.format_it(start)} presso {centro.nome}, {centro.indirizzo}."


def _registra_esito(lead: dict, args: dict) -> str:
    esito = args.get("esito")
    note = args.get("note")
    if note:
        db.update_lead(lead["id"], summary=note)
    current = db.get_lead(lead["id"]) or lead
    if current["status"] == db.STAFF_CALLBACK:
        return "Esito registrato: la persona verrà richiamata dal responsabile. Saluta e chiudi la chiamata."
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


def _annulla_prenotazione(lead: dict, args: dict) -> str:
    with db.tx() as c:
        ids = [r[0] for r in c.execute(
            "SELECT id FROM appointments WHERE lead_id = ? AND status = 'confermato'", (lead["id"],)
        )]
        if not ids:
            return "Non c'è nessuna prenotazione attiva da annullare."
        c.execute("UPDATE appointments SET status = 'annullato' WHERE lead_id = ? AND status = 'confermato'", (lead["id"],))
    for appointment_id in ids:
        messaging.cancel_for_appointment(appointment_id)
        calendars.sync_appointment(appointment_id)
    # Il lead torna "da ricontattare": l'esito finale lo registra `registra_esito`
    db.update_lead(lead["id"], status=db.RETRY, next_attempt_at=None,
                   summary=f"Prenotazione annullata: {args.get('motivo') or 'ci vuole pensare'}")
    leads.notify("prenotazione_annullata", lead["id"], motivo=args.get("motivo"))
    return "Prenotazione annullata. Chiedi quando preferisce essere ricontattata e usa registra_esito."


def _richiedi_ricontatto_responsabile(lead: dict, args: dict) -> str:
    fields = {"status": db.STAFF_CALLBACK, "next_attempt_at": None}
    if args.get("centro_id") in centers.load_config().centri:
        fields["centro_id"] = args["centro_id"]
    if args.get("obiettivo"):
        fields["obiettivo"] = args["obiettivo"]
    nota = "; ".join(x for x in (f"Richiamare: {args.get('fascia_oraria')}", args.get("note")) if x)
    fields["note"] = nota
    fields["summary"] = "Nessun orario adatto: da ricontattare dal responsabile"
    db.update_lead(lead["id"], **fields)
    leads.notify("ricontatto_responsabile", lead["id"], fascia_oraria=args.get("fascia_oraria"),
                 note=args.get("note"), tipo=args.get("tipo_valutazione"))
    return "Richiesta inoltrata al responsabile del centro. Rassicura la persona con la frase indicata e saluta."


_HANDLERS = {
    "richiedi_ricontatto_responsabile": _richiedi_ricontatto_responsabile,
    "annulla_prenotazione": _annulla_prenotazione,
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

