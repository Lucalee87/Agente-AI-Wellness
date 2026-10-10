"""Meta Lead Ads (Facebook / Instagram) via webhook `leadgen`.

Il webhook di Meta invia solo l'id del lead: i dati (nome, telefono...) vanno
letti dalla Graph API con il token della Pagina.
"""

import hashlib
import hmac
import logging

import httpx

from .. import centers
from ..config import settings
from ..leads import LeadIn

log = logging.getLogger(__name__)

# Nomi dei campi standard dei moduli Meta
NAME_FIELDS = ("full_name", "nome_e_cognome", "nome")
FIRST_NAME_FIELDS = ("first_name",)
LAST_NAME_FIELDS = ("last_name",)
PHONE_FIELDS = ("phone_number", "telefono", "numero_di_telefono")
EMAIL_FIELDS = ("email",)
CENTRO_FIELDS = ("centro", "sede", "quale_centro")
GOAL_FIELDS = ("obiettivo", "qual_è_il_tuo_obiettivo?", "qual_e_il_tuo_obiettivo")


def verify_signature(raw_body: bytes, header: str | None) -> bool:
    """Verifica l'header X-Hub-Signature-256 firmato con l'App Secret."""
    if not settings.meta_app_secret:
        log.warning("META_APP_SECRET non impostato: firma del webhook Meta non verificata")
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(settings.meta_app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def leadgen_events(payload: dict) -> list[dict]:
    """Estrae gli eventi leadgen (leadgen_id, form_id, ...) dal payload del webhook."""
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") == "leadgen":
                out.append(change.get("value", {}))
    return out


def fetch_lead(leadgen_id: str) -> dict:
    url = f"https://graph.facebook.com/{settings.meta_graph_version}/{leadgen_id}"
    r = httpx.get(url, params={"access_token": settings.meta_page_access_token}, timeout=15)
    r.raise_for_status()
    return r.json()


def _first(fields: dict, names: tuple[str, ...]) -> str | None:
    for n in names:
        if fields.get(n):
            return fields[n]
    return None


def to_lead(event: dict, data: dict) -> LeadIn:
    fields = {}
    for f in data.get("field_data", []):
        values = f.get("values") or []
        fields[f.get("name", "").lower()] = ", ".join(str(v) for v in values)

    nome = _first(fields, NAME_FIELDS)
    if not nome:
        nome = " ".join(x for x in (_first(fields, FIRST_NAME_FIELDS), _first(fields, LAST_NAME_FIELDS)) if x) or None

    form_id = str(event.get("form_id") or data.get("form_id") or "")
    centro_id = centers.centro_for_form("meta", form_id) or _match_centro(_first(fields, CENTRO_FIELDS))
    known = set(NAME_FIELDS + FIRST_NAME_FIELDS + LAST_NAME_FIELDS + PHONE_FIELDS + EMAIL_FIELDS + CENTRO_FIELDS + GOAL_FIELDS)
    extra = {k: v for k, v in fields.items() if k not in known}
    extra.update({k: event[k] for k in ("form_id", "ad_id", "adgroup_id", "page_id") if event.get(k)})

    return LeadIn(
        source="meta",
        source_ref=str(event.get("leadgen_id") or data.get("id")),
        nome=nome,
        telefono=_first(fields, PHONE_FIELDS),
        email=_first(fields, EMAIL_FIELDS),
        centro_id=centro_id,
        obiettivo=_first(fields, GOAL_FIELDS),
        extra=extra,
    )


def _match_centro(value: str | None) -> str | None:
    """Associa la risposta libera 'centro' del modulo a un centro configurato."""
    if not value:
        return None
    v = value.lower().replace("_", " ").replace("-", " ")
    for c in centers.load_config().centri.values():
        if c.id.replace("-", " ") in v or c.nome.lower() in v or (len(v) >= 4 and v in c.nome.lower()):
            return c.id
    return None
